#!/usr/bin/env python3
"""One-shot worker resources. See fm-worker-resources.sh --help.

Only recorded direct reports are inspected. Read-only paths: metadata, exact
Herdr process/session lookups, attributable /proc descendants, Pi numeric usage,
SQLite run/invocation fields. Publishing is a separate, explicit metadata opt-in.
No session loading, daemon activation, subprocess command lines or environments.
"""

import argparse
from contextlib import closing
import datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import time


FIELDS = ("input", "output", "cacheRead", "cacheWrite")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SOURCE = "firstmate:worker-resources"
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_PIDS = 10000


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def command(args):
    """Bounded, captured output; never surface stderr or raw vendor data."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=5, check=False)
        return p.stdout.decode("utf-8") if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def herdr(task, *args):
    raw = command(["herdr", *args, "--session", task["session"]])
    try:
        result = json.loads(raw)["result"]
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError, KeyError):
        return {}


def tasks(home):
    """No source/eval of metadata, no discovery outside the selected home."""
    rows = []
    for file in sorted((home / "state").glob("*.meta")):
        if not TASK_ID.fullmatch(file.stem) or file.is_symlink():
            continue
        try:
            raw = file.read_text()
        except (OSError, UnicodeError):
            continue
        if len(raw) > 65536:
            continue
        meta = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if meta.get("kind") == "secondmate":
            continue
        session, sep, pane = meta.get("window", "").partition(":")
        supported = (meta.get("backend") == "herdr" and sep and
                     re.fullmatch(r"[A-Za-z0-9_.-]+", session) and
                     re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", pane))
        rows.append(dict(id=file.stem, meta=meta, file=file, original=raw,
                         session=session, pane=pane, supported=bool(supported)))
    return rows


class Proc:
    """Linux process identities and metrics, never argv/environ."""

    def __init__(self, root=Path("/proc")):
        self.root = Path(root)
        self.clock = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")

    def identity(self, pid):
        try:
            fields = (self.root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            return int(fields[19]), int(fields[11]) + int(fields[12]), int(fields[21]) * self.page
        except (OSError, ValueError, IndexError):
            return None

    def cwd(self, pid):
        try:
            return (self.root / str(pid) / "cwd").resolve(strict=True)
        except (OSError, RuntimeError):
            return None

    def tree(self, roots):
        seen, todo, incomplete = set(), list(roots), False
        while todo and len(seen) < MAX_PIDS:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                for thread in (self.root / str(pid) / "task").iterdir():
                    todo.extend(int(v) for v in (thread / "children").read_text().split())
            except (OSError, ValueError):
                incomplete = True
        return seen, incomplete or bool(todo)

    def row(self, pid):
        ident = self.identity(pid)
        if ident is None:
            return None
        root = self.root / str(pid)
        io, pss = None, None
        try:
            io = dict((k, int(v)) for k, v in
                      (line.split(":") for line in (root / "io").read_text().splitlines()))
            if any(io.get(k, -1) < 0 for k in ("read_bytes", "write_bytes")):
                io = None
        except (OSError, ValueError):
            pass
        try:
            pss = next(int(line.split()[1]) * 1024 for line in
                       (root / "smaps_rollup").read_text().splitlines() if line.startswith("Pss:"))
        except (OSError, ValueError, StopIteration):
            pass
        # Ticks can advance during a read; only starttime is identity.
        after = self.identity(pid)
        if after is None or after[0] != ident[0]:
            return None
        return dict(start=ident[0], ticks=ident[1], rss=ident[2], pss=pss, io=io)

    def snapshot(self, bound_roots):
        valid = []
        incomplete = False
        for pid, start in bound_roots.items():
            ident = self.identity(pid)
            if ident is not None and ident[0] == start:
                valid.append(pid)
            else:
                incomplete = True
        pids, truncated = self.tree(valid)
        rows = {}
        for pid in pids:
            row = self.row(pid)
            if row is not None:
                rows[pid] = row
            else:
                incomplete = True
        return rows, incomplete or truncated


def summarize(a, b, dt, clock, incomplete=False):
    stable = [p for p in a.keys() & b.keys() if a[p]["start"] == b[p]["start"]]
    churn = len(a.keys() ^ b.keys()) + sum(a[p]["start"] != b[p]["start"] for p in a.keys() & b.keys())
    pss = [v["pss"] for v in b.values() if v["pss"] is not None]
    io_ids = [p for p in stable if a[p]["io"] is not None and b[p]["io"] is not None]
    io_reset = any(b[p]["io"][k] < a[p]["io"][k] for p in io_ids for k in ("read_bytes", "write_bytes"))
    cpu_reset = any(b[p]["ticks"] < a[p]["ticks"] for p in stable)
    return dict(
        cpu_percent=(100 * sum(b[p]["ticks"] - a[p]["ticks"] for p in stable) / clock / dt
                     if stable and not cpu_reset else None),
        rss_bytes=sum(v["rss"] for v in b.values()) if b else None,
        pss_bytes=sum(pss) if b and len(pss) == len(b) else None,
        pids=len(b), stable_pids=len(stable), pss_covered=len(pss),
        io_covered=len(io_ids), churn=churn,
        read_bytes_per_second=(sum(b[p]["io"]["read_bytes"] - a[p]["io"]["read_bytes"] for p in io_ids) / dt
                               if io_ids and len(io_ids) == len(stable) and not churn and not io_reset else None),
        write_bytes_per_second=(sum(b[p]["io"]["write_bytes"] - a[p]["io"]["write_bytes"] for p in io_ids) / dt
                                if io_ids and len(io_ids) == len(stable) and not churn and not io_reset else None),
        partial=bool(incomplete or churn or not b or len(pss) != len(b) or len(io_ids) != len(stable) or cpu_reset or io_reset))


def pi_usage(reference, worktree, sessions_root):
    """Stream numeric fields only. Exact current file, never a directory total."""
    result = dict(totals=None, records=0, missing=0, nested_usage=False, partial=True)
    try:
        path = Path(reference).resolve(strict=True)
        path.relative_to(sessions_root.resolve(strict=True))
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_SESSION_BYTES:
            return result
        totals = dict.fromkeys(FIELDS, 0)
        seen = set()
        with path.open() as f:
            header = json.loads(f.readline())
            if (header.get("type") != "session" or
                    Path(header["cwd"]).resolve() != Path(worktree).resolve()):
                return result
            # Forked history cannot be claimed as this task's newly incurred usage.
            if header.get("parentSession"):
                return result
            size = 0
            for line in f:
                size += len(line.encode("utf-8"))
                if size > MAX_SESSION_BYTES:
                    result["missing"] += 1
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    result["missing"] += 1
                    continue
                if not isinstance(entry, dict):
                    result["missing"] += 1
                    continue
                entry_id = entry.get("id")
                if not isinstance(entry_id, str) or entry_id in seen:
                    continue
                seen.add(entry_id)
                usage = None
                if entry.get("type") == "message":
                    message = entry.get("message", {})
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")
                    if role not in ("assistant", "toolResult"):
                        continue
                    usage = message.get("usage")
                    if role == "toolResult" and usage is None:
                        continue
                    if role == "toolResult":
                        result["nested_usage"] = True
                elif entry.get("type") in ("compaction", "branch_summary"):
                    usage = entry.get("usage")
                else:
                    continue
                if not isinstance(usage, dict) or not all(number(usage.get(k)) for k in FIELDS):
                    result["missing"] += 1
                    continue
                for k in FIELDS:
                    totals[k] += usage[k]
                result["records"] += 1
        result["totals"] = totals if result["records"] else None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        pass
    return result


def validation(task, db_path, proc):
    """Known branch runs, not a global daemon charge or exhaustive lifetime ledger."""
    out = dict(totals=None, invocations=0, covered=0, runs=0, active_steps=0, partial=True)
    task["validation_run_ids"] = set()
    roots = []
    worktree, project = task["meta"].get("worktree"), task["meta"].get("project")
    if not worktree or not project or not db_path.is_file():
        return out, roots
    branch = command(["git", "-C", worktree, "branch", "--show-current"])
    if not branch or not branch.strip():
        return out, roots
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as c:
            c.row_factory = sqlite3.Row
            runs = c.execute("""
              SELECT r.id,r.worktree_dir FROM runs r JOIN repos p ON p.id=r.repo_id
              WHERE p.working_path=? AND r.branch=?
            """, (project, branch.strip())).fetchall()
            out["runs"] = len(runs)
            task["validation_run_ids"] = {run["id"] for run in runs}
            totals = dict.fromkeys(FIELDS, 0)
            for run in runs:
                for step in c.execute("""
                  SELECT agent_pid FROM step_results WHERE run_id=?
                    AND status IN ('running','fixing')
                """, (run["id"],)):
                    out["active_steps"] += 1
                    pid = step["agent_pid"]
                    if type(pid) is int and pid > 0 and run["worktree_dir"]:
                        if proc.cwd(pid) == Path(run["worktree_dir"]).resolve():
                            roots.append(pid)
                for row in c.execute("""
                  SELECT agent,delta_input_tokens,delta_output_tokens,
                    delta_cache_read_tokens,cache_creation_tokens
                  FROM agent_invocations WHERE run_id=?
                """, (run["id"],)):
                    out["invocations"] += 1
                    inp, output, cache, write = (row[k] for k in
                        ("delta_input_tokens", "delta_output_tokens",
                         "delta_cache_read_tokens", "cache_creation_tokens"))
                    # Pi is cache-exclusive; Codex is inclusive. Never subtract
                    # cached input twice or pretend missing creation counts are zero.
                    if row["agent"] not in ("pi", "codex") or not all(number(v) for v in (inp, output, cache)):
                        continue
                    if row["agent"] == "codex":
                        if cache > inp:
                            continue
                        inp -= cache
                    elif not number(write):
                        continue
                    if not number(write):
                        # Known subtotal only; missing cache-write stays visible.
                        write = 0
                        out["unknown_cache_write"] = True
                    for k, value in zip(FIELDS, (inp, output, cache, write)):
                        totals[k] += value
                    out["covered"] += 1
            out["totals"] = totals if out["covered"] else None
    except (OSError, sqlite3.Error, ValueError):
        out["unavailable"] = True
    return out, roots


def bind(task, proc, sessions_root, db_path):
    task["roots"] = {}
    task["reference"] = None
    task["validation_run_ids"] = set()
    task["worker_usage"] = dict(totals=None, partial=True)
    task["validation_usage"] = dict(totals=None, partial=True)
    if not task["supported"]:
        return
    info = herdr(task, "pane", "process-info", "--pane", task["pane"]).get("process_info", {})
    agent = herdr(task, "agent", "get", task["pane"]).get("agent", {})
    if not isinstance(info, dict) or info.get("pane_id") != task["pane"] or not isinstance(agent, dict):
        return
    if agent.get("pane_id") != task["pane"]:
        return
    roots = []
    pid = info.get("shell_pid")
    task["shell_pid"] = pid
    if type(pid) is int and pid > 0:
        roots.append(pid)
    ref = agent.get("agent_session")
    if (agent.get("agent") == "pi" and isinstance(ref, dict) and
            ref.get("agent") == "pi" and ref.get("kind") == "path" and isinstance(ref.get("value"), str)):
        task["reference"] = ref["value"]
        task["worker_usage"] = pi_usage(ref["value"], task["meta"].get("worktree"), sessions_root)
    usage, extra = validation(task, db_path, proc)
    task["validation_usage"] = usage
    for pid in roots + extra:
        ident = proc.identity(pid)
        if ident:
            task["roots"][pid] = ident[0]


def compact(value, base=1000):
    if value is None:
        return "?"
    for suffix in ("", "K", "M", "G", "T"):
        if value < base or suffix == "T":
            return (str(int(value)) if value >= 10 or suffix == "" else f"{value:.1f}") + suffix
        value /= base


def badge(row):
    m = row["metrics"]
    cpu = "?" if m["cpu_percent"] is None else (">999" if m["cpu_percent"] > 999 else str(round(m["cpu_percent"])))
    if m.get("partial") and m["cpu_percent"] is not None:
        cpu = "~" + cpu
    # All token totals are explicitly incomplete task history (~).
    return f"C{cpu}% P{compact(m['pss_bytes'], 1024)} T{compact(row['known_tokens'])}~"


def output_row(task, metrics):
    worker, review = task["worker_usage"], task["validation_usage"]
    wt, rt = worker.get("totals"), review.get("totals")
    # If the worker reports nested usage, reviewers may already be included.
    # Show both sources in details, but decline to add ambiguous overlapping work.
    overlap = bool(worker.get("nested_usage"))
    known = sum(wt.values()) if wt else None
    if not overlap and rt:
        known = (known or 0) + sum(rt.values())
    return dict(task=task["id"], metrics=metrics, worker_usage=worker, validation_usage=review,
                known_tokens=known, token_history="partial", possible_usage_overlap=overlap,
                badge=None, published=False)


def publish(task, row, ttl, proc):
    """Verify current identity before a display-only write. No lifecycle calls."""
    try:
        if task["file"].read_text() != task["original"] or not task["roots"] or not task["reference"]:
            return False
        for pid, start in task["roots"].items():
            ident = proc.identity(pid)
            if ident is None or ident[0] != start:
                return False
        info = herdr(task, "pane", "process-info", "--pane", task["pane"]).get("process_info", {})
        if info.get("pane_id") != task["pane"] or info.get("shell_pid") != task.get("shell_pid"):
            return False
        agent = herdr(task, "agent", "get", task["pane"]).get("agent", {})
        ref = agent.get("agent_session", {})
        if (agent.get("pane_id") != task["pane"] or agent.get("agent") != "pi" or
                ref.get("kind") != "path" or ref.get("value") != task["reference"]):
            return False
        result = herdr(task, "pane", "report-metadata", task["pane"],
                       "--source", SOURCE, "--agent", "pi", "--token", "fm_resources=" + row["badge"],
                       "--ttl-ms", str(ttl * 1000), "--seq", str(time.time_ns()))
        return bool(result)
    except (OSError, AttributeError, TypeError):
        return False


def collect(home, interval, sessions_root, db_path, publish_badges=False, ttl=60):
    started = utc()
    proc = Proc()
    workers = tasks(home)
    for task in workers:
        bind(task, proc, sessions_root, db_path)
    session_owners, run_owners = {}, {}
    for i, task in enumerate(workers):
        if task["reference"]:
            session_owners.setdefault(task["reference"], set()).add(i)
        for run_id in task.get("validation_run_ids", set()):
            run_owners.setdefault(run_id, set()).add(i)
    for i, task in enumerate(workers):
        if task["reference"] and len(session_owners[task["reference"]]) > 1:
            task["worker_usage"].update(totals=None, shared_identity=True)
        if any(len(run_owners[run_id]) > 1 for run_id in task.get("validation_run_ids", set())):
            task["validation_usage"].update(totals=None, shared_identity=True)
    a0 = time.monotonic()
    a = [proc.snapshot(t["roots"]) for t in workers]
    a1 = time.monotonic()
    time.sleep(interval)
    b0 = time.monotonic()
    b = [proc.snapshot(t["roots"]) for t in workers]
    b1 = time.monotonic()
    dt = (b0 + b1 - a0 - a1) / 2
    # A PID shared by different tasks is excluded from both, not double charged.
    owners = {}
    for index, pair in enumerate(zip(a, b)):
        for pid in pair[0][0].keys() | pair[1][0].keys():
            owners.setdefault(pid, set()).add(index)
    shared = {pid for pid, ids in owners.items() if len(ids) > 1}
    rows = []
    for t, (before, ai), (after, bi) in zip(workers, a, b):
        had_shared = bool(shared.intersection(before.keys() | after.keys()))
        before = {p: v for p, v in before.items() if p not in shared}
        after = {p: v for p, v in after.items() if p not in shared}
        m = summarize(before, after, dt, proc.clock, ai or bi or had_shared)
        row = output_row(t, m)
        row["badge"] = badge(row)
        if publish_badges:
            row["published"] = publish(t, row, ttl, proc)
        rows.append(row)
    return dict(schema=1, started_at=started, finished_at=utc(), interval_seconds=dt,
                scan_seconds=(a1 - a0) + (b1 - b0), shared_pids_excluded=len(shared),
                scope="Linux only; sampled CPU/I/O; PSS RAM; current Pi + known validation; partial history",
                rows=rows)


def text_report(report, sort_key):
    def key(row):
        value = {"cpu": row["metrics"]["cpu_percent"], "ram": row["metrics"]["pss_bytes"],
                 "tokens": row["known_tokens"], "disk": row["metrics"]["write_bytes_per_second"]}[sort_key]
        return (value is not None, value or 0)
    lines = [f"Worker resources - {report['finished_at']}",
             f"Sample {report['interval_seconds']:.2f}s; CPU: % of one core; RAM: PSS (RSS in details).",
             "T~: incomplete token history; C~: partial CPU sample; ?: unknown; not a subscription bill.",
             "Disk order uses write bytes/s; shared services and remote/host/GPU work are excluded.", ""]
    for row in sorted(report["rows"], key=key, reverse=True):
        m = row["metrics"]
        lines.append(f"{row['task']}  {row['badge']}")
        lines.append(f"  RSS {compact(m['rss_bytes'], 1024)}; disk R/W "
                     f"{compact(m['read_bytes_per_second'], 1024)}/{compact(m['write_bytes_per_second'], 1024)} B/s; "
                     f"PIDs {m['pids']}; PSS {m['pss_covered']}/{m['pids']}; churn {m['churn']}; "
                     f"sample {'partial' if m['partial'] else 'observed trees'}")
        for title, usage in (("Pi", row["worker_usage"]), ("validation", row["validation_usage"])):
            totals = usage.get("totals")
            values = ", ".join(f"{k}={totals[k]:g}" for k in FIELDS) if totals else "unknown"
            lines.append(f"  {title}: {values}")
            if usage.get("shared_identity"):
                lines.append(f"  {title} identity is claimed by multiple tasks; subtotal withheld.")
        lines.append(f"  Pi recorded usage entries {row['worker_usage'].get('records', 0)}; "
                     f"missing/unreadable entries {row['worker_usage'].get('missing', 0)}.")
        v = row["validation_usage"]
        lines.append(f"  Validation coverage {v.get('covered', 0)}/{v.get('invocations', 0)} returned calls; "
                     f"{v.get('active_steps', 0)} active steps; totals can lag.")
        if row["possible_usage_overlap"]:
            lines.append("  Nested usage may overlap validation; T excludes the separate validation subtotal.")
        if v.get("unknown_cache_write"):
            lines.append("  Validation cache-write coverage is incomplete; shown subtotal excludes unknown writes.")
    if not report["rows"]:
        lines.append("No recorded direct reports.")
    return "\n".join(lines)


def config(home, width):
    entry = Path(__file__).resolve().with_suffix(".sh")
    cmd = " ".join(shlex.quote(str(x)) for x in (entry, "--home", home, "--wait"))
    return f"""# Suggested fragment only: merge manually; preserve existing custom identity rows.
[ui]
sidebar_width = {width}
sidebar_max_width = {width + 8}

[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab"], ["agent", "$fm_resources"]]

[[keys.command]]
key = "prefix+alt+r"
type = "popup"
command = {json.dumps(cmd)}
width = "90%"
height = "80%"
# The popup is read-only. Refresh badges separately with --publish.
# Badges expire after --ttl seconds; blank means not sampled or stale.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=os.environ.get("FM_HOME"), help="Explicit owning Firstmate home")
    parser.add_argument("--interval", type=float, default=5, help="CPU/I/O sample seconds (1..30; default 5)")
    parser.add_argument("--json", action="store_true", help="Numeric/identity report on stdout")
    parser.add_argument("--sort", choices=("cpu", "ram", "tokens", "disk"), default="cpu")
    parser.add_argument("--publish", action="store_true", help="Opt in to expiring display-only Herdr Pi badges")
    parser.add_argument("--ttl", type=int, default=60, help="Badge expiry seconds (5..300; default 60)")
    parser.add_argument("--herdr-config", action="store_true", help="Print opt-in config only; never apply it")
    parser.add_argument("--sidebar-width", type=int, default=44, help="Suggested sidebar columns (28..80)")
    parser.add_argument("--wait", action="store_true", help="Keep detail popup open until Enter (TTY only)")
    args = parser.parse_args()
    if not args.home:
        parser.error("--home or FM_HOME is required")
    if not 1 <= args.interval <= 30 or not 5 <= args.ttl <= 300 or not 28 <= args.sidebar_width <= 80:
        parser.error("interval, ttl or sidebar width is out of range")
    home = Path(args.home).resolve()
    if not (home / "state").is_dir():
        parser.error("selected home has no state directory")
    if args.herdr_config:
        if args.publish:
            parser.error("--herdr-config cannot publish")
        print(config(home, args.sidebar_width))
        return 0
    if not sys.platform.startswith("linux"):
        parser.error("sampling is Linux-only; no Windows host or GPU measurements")
    report = collect(home, args.interval, Path.home() / ".pi/agent/sessions",
                     Path(os.environ.get("NM_HOME", str(Path.home() / ".no-mistakes"))) / "state.sqlite",
                     args.publish, args.ttl)
    print(json.dumps(report, indent=2, allow_nan=False) if args.json else text_report(report, args.sort))
    if args.wait and sys.stdin.isatty():
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
