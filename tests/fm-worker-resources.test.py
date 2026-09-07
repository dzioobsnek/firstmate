"""Behavioral tests of the on-demand sampler; fixtures never contact Herdr."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("resources", ROOT / "bin/fm-worker-resources.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class Resources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "state").mkdir()
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()
        self.work = self.home / "work"
        self.work.mkdir()
        self.file = self.home / "state/test-one.meta"
        self.file.write_text(f"kind=ship\nbackend=herdr\nwindow=default:w1:p2\n"
                             f"project={self.work}\nworktree={self.work}\nharness=pi\n")

    def session(self, entries, **header):
        f = self.sessions / "s.jsonl"
        f.write_text("\n".join(json.dumps(e) for e in
                     [dict(type="session", cwd=str(self.work), **header)] + entries) + "\n")
        return f

    def usage(self, ident="a", role="assistant", **extra):
        return dict(type="message", id=ident, message=dict(
            role=role, content="PRIVATE_PROMPT_NOT_FOR_OUTPUT",
            usage=dict(input=10, output=2, cacheRead=30, cacheWrite=4,
                       cost={"total": "PRIVATE_BILL_NOT_FOR_OUTPUT"})), **extra)

    def test_numeric_session_counts_summaries_not_retained_tail(self):
        f = self.session([
            self.usage(), self.usage(), self.usage("b", "toolResult"),
            dict(type="compaction", id="c", usage=dict(input=1, output=2, cacheRead=3, cacheWrite=4),
                 retainedTail=[self.usage()], summary="PRIVATE_SUMMARY"),
            dict(type="message", id="d", message=dict(role="toolResult", content="PRIVATE_FILE")),
        ])
        out = r.pi_usage(str(f), str(self.work), self.sessions)
        self.assertEqual(out["totals"], dict(input=21, output=6, cacheRead=63, cacheWrite=12))
        self.assertTrue(out["nested_usage"])
        self.assertEqual(out["records"], 3)
        self.assertNotIn("PRIVATE", json.dumps(out))
        self.assertEqual(out["missing"], 0)

    def test_session_boundary_fork_missing_and_corrupt(self):
        f = self.session([self.usage()], parentSession="old-session.jsonl")
        self.assertIsNone(r.pi_usage(str(f), self.work, self.sessions)["totals"])
        f = self.session([self.usage()])
        self.assertIsNone(r.pi_usage(str(f), self.home, self.sessions)["totals"])
        self.assertIsNone(r.pi_usage(str(f), self.work, self.work)["totals"])
        with f.open("a") as stream:
            stream.write('{"partial":')
        out = r.pi_usage(str(f), self.work, self.sessions)
        self.assertEqual(out["totals"]["input"], 10)
        self.assertEqual(out["missing"], 1)
        self.assertTrue(out["partial"])

    def test_unknown_usage_not_zero(self):
        e = self.usage()
        del e["message"]["usage"]["cacheRead"]
        f = self.session([e])
        out = r.pi_usage(str(f), self.work, self.sessions)
        self.assertIsNone(out["totals"])
        self.assertEqual(out["missing"], 1)
        e["message"]["usage"]["cacheRead"] = float("nan")
        f = self.session([e])
        self.assertIsNone(r.pi_usage(str(f), self.work, self.sessions)["totals"])
        self.assertFalse(r.number(10**1000))

    def test_inventory_does_not_adopt_secondmates_or_symlinks(self):
        (self.home / "state/second.meta").write_text("kind=secondmate\nbackend=herdr\n")
        (self.home / "state/alias.meta").symlink_to(self.file)
        (self.home / "state/unsupported.meta").write_text("kind=ship\nbackend=tmux\n")
        ts = r.tasks(self.home)
        self.assertEqual([t["id"] for t in ts], ["test-one", "unsupported"])
        self.assertFalse(ts[1]["supported"])

    def metric(self, ticks=1, start=1, pss=20, io=10):
        return dict(start=start, ticks=ticks, pss=pss, rss=40,
                    io=None if io is None else dict(read_bytes=io, write_bytes=io))

    def test_metric_rates_and_permission_churn(self):
        a = {1: self.metric()}
        b = {1: self.metric(ticks=251, io=1010)}
        m = r.summarize(a, b, 5, 100)
        self.assertEqual(m["cpu_percent"], 50)
        self.assertEqual(m["read_bytes_per_second"], 200)
        self.assertEqual(m["pss_bytes"], 20)
        b[2] = self.metric(pss=None, io=None)
        m = r.summarize(a, b, 5, 100)
        self.assertIsNone(m["pss_bytes"])
        self.assertIsNone(m["read_bytes_per_second"])
        self.assertTrue(m["partial"])
        m = r.summarize(a, {1: self.metric(start=2)}, 5, 100)
        self.assertIsNone(m["cpu_percent"])
        self.assertEqual(m["churn"], 1)
        m = r.summarize(a, {1: self.metric(ticks=0, io=0)}, 5, 100)
        self.assertIsNone(m["read_bytes_per_second"])
        self.assertIsNone(m["cpu_percent"])

    def database(self):
        db = self.home / "state.sqlite"
        with closing(sqlite3.connect(db)) as c:
            c.executescript("""
              CREATE TABLE repos(id,working_path);
              CREATE TABLE runs(id,repo_id,branch,worktree_dir);
              CREATE TABLE step_results(run_id,status,agent_pid);
              CREATE TABLE agent_invocations(run_id,agent,delta_input_tokens,
                delta_output_tokens,delta_cache_read_tokens,cache_creation_tokens);
            """)
            c.execute("INSERT INTO repos VALUES(1,?)", (str(self.work),))
            c.execute("INSERT INTO runs VALUES(1,1,'fm/task',?)", (str(self.work),))
            c.execute("INSERT INTO runs VALUES(2,1,'other',?)", (str(self.work),))
            c.executemany("INSERT INTO agent_invocations VALUES(?,?,?,?,?,?)", [
                (1, "pi", 10, 2, 30, 4), (1, "codex", 100, 3, 80, None),
                (1, "other", 99, 99, 99, 99), (1, "pi", None, 2, 3, 4),
                (2, "pi", 99999, 99999, 99999, 99999)])
            c.executemany("INSERT INTO step_results VALUES(?,?,?)", [(1, "running", 123), (1, "running", 456)])
            c.commit()
        return db

    def test_validation_adapter_cache_semantics_and_run_scope(self):
        db = self.database()
        proc = r.Proc()
        with patch.object(r, "command", return_value="fm/task\n"), \
                patch.object(proc, "cwd", side_effect=lambda pid: self.work if pid == 123 else self.home):
            out, roots = r.validation(r.tasks(self.home)[0], db, proc)
        self.assertEqual(roots, [123])
        self.assertEqual(out["covered"], 2)
        self.assertEqual(out["invocations"], 4)
        self.assertEqual(out["totals"], dict(input=30, output=5, cacheRead=110, cacheWrite=4))
        self.assertTrue(out["unknown_cache_write"])
        self.assertEqual(out["active_steps"], 2)

    def test_nested_usage_is_not_added_twice(self):
        task = dict(id="t", worker_usage=dict(totals=dict(input=10), nested_usage=True),
                    validation_usage=dict(totals=dict(input=10)))
        out = r.output_row(task, {})
        self.assertEqual(out["known_tokens"], 10)
        self.assertTrue(out["possible_usage_overlap"])

    def test_publication_checks_and_only_metadata_command(self):
        t = r.tasks(self.home)[0]
        t.update(roots={123: 9}, shell_pid=123, reference="session.jsonl")
        calls = []
        def fake(command, **kwargs):
            self.assertEqual(command[0], "herdr")
            self.assertEqual(command[-2:], ["--session", t["session"]])
            args = tuple(command[1:-2])
            calls.append(args)
            if args[:2] == ("pane", "process-info"):
                result = dict(process_info=dict(pane_id=t["pane"], shell_pid=123))
            elif args[:2] == ("agent", "get"):
                result = dict(agent=dict(pane_id=t["pane"], agent="pi",
                    agent_session=dict(kind="path", value=t["reference"])))
            else:
                return subprocess.CompletedProcess(command, publication_status, stdout=b"")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"result": result}).encode())
        proc = r.Proc()
        publication_status = 0
        with patch.object(r.subprocess, "run", side_effect=fake), patch.object(proc, "identity", return_value=(9, 0, 0)):
            self.assertTrue(r.publish(t, dict(badge="C1% P2M T3K~"), proc))
            self.assertEqual([c[:2] for c in calls],
                             [("pane", "process-info"), ("agent", "get"), ("pane", "report-metadata")])
            self.assertIn("--ttl-ms", calls[-1])
            self.assertIn("60000", calls[-1])
            publication_status = 1
            self.assertFalse(r.publish(t, dict(badge="C1% P2M T3K~"), proc))
            self.file.write_text(self.file.read_text() + "spawn_gen=replaced\n")
            calls.clear()
            self.assertFalse(r.publish(t, dict(badge="old"), proc))
            self.assertEqual(calls, [])

    def test_config_cli_never_contacts_herdr_or_writes_config(self):
        p = subprocess.run([sys.executable, str(ROOT / "bin/fm-worker-resources.py"),
                            "--home", str(self.home), "--herdr-config"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('["agent", "$fm_resources"]', p.stdout)
        self.assertIn('type = "popup"', p.stdout)
        self.assertIn("sidebar_width = 44", p.stdout)
        self.assertFalse((self.home / "config").exists())

    def test_cli_rejects_removed_tuning_options(self):
        for option, value in (("--interval", "2"), ("--ttl", "30"), ("--sidebar-width", "48")):
            with self.subTest(option=option):
                p = subprocess.run([sys.executable, str(ROOT / "bin/fm-worker-resources.py"),
                                    "--home", str(self.home), option, value],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 2)
                self.assertIn("unrecognized arguments", p.stderr)
                self.assertEqual(p.stdout, "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux proc only")
    def test_real_child_process_sample_and_no_publish_by_default(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        self.addCleanup(cleanup)
        proc = r.Proc()
        def bind(task, p, *_):
            task.update(roots={child.pid: p.identity(child.pid)[0]}, reference=None,
                        worker_usage=dict(totals=None), validation_usage=dict(totals=None))
        with patch.object(r, "bind", side_effect=bind), patch.object(r, "publish") as publish:
            report = r.collect(self.home, self.sessions, self.home / "missing")
        self.assertGreaterEqual(report["interval_seconds"], 5)
        self.assertEqual(report["rows"][0]["metrics"]["pids"], 1)
        self.assertGreater(report["rows"][0]["metrics"]["rss_bytes"], 0)
        publish.assert_not_called()
        child.terminate()
        child.wait(timeout=5)

    def test_compact_badges_remain_single_short_line(self):
        row = dict(metrics=dict(cpu_percent=12345, pss_bytes=900 * 1024**3), known_tokens=9e12)
        value = r.badge(row)
        self.assertLessEqual(len(value), 26)
        self.assertNotIn("\n", value)
        self.assertTrue(value.endswith("~"))

    def test_shared_process_and_usage_ownership_is_withheld(self):
        (self.home / "state/test-two.meta").write_text(self.file.read_text())
        def bind(task, *_):
            task.update(roots={123: 1}, reference="shared-session",
                        validation_run_ids={"shared-run"},
                        worker_usage=dict(totals=dict(input=10)),
                        validation_usage=dict(totals=dict(input=20)))
        with patch.object(r, "bind", side_effect=bind), \
                patch.object(r.Proc, "snapshot", return_value=({123: self.metric()}, False)), \
                patch.object(r.time, "sleep"):
            out = r.collect(self.home, self.sessions, self.home / "missing")
        self.assertEqual(out["shared_pids_excluded"], 1)
        for row in out["rows"]:
            self.assertEqual(row["metrics"]["pids"], 0)
            self.assertIsNone(row["known_tokens"])
            self.assertTrue(row["worker_usage"]["shared_identity"])
            self.assertTrue(row["validation_usage"]["shared_identity"])

    def test_reused_root_pid_never_traverses_unrelated_children(self):
        proc = r.Proc()
        with patch.object(proc, "identity", return_value=(999, 0, 0)), \
                patch.object(proc, "tree", return_value=(set(), False)) as tree:
            rows, incomplete = proc.snapshot({123: 1})
        tree.assert_called_once_with([])
        self.assertEqual(rows, {})
        self.assertTrue(incomplete)

    def test_unsupported_worker_does_not_query_global_processes(self):
        self.file.write_text("kind=ship\nbackend=tmux\n")
        with patch.object(r, "command") as cmd:
            t = r.tasks(self.home)[0]
            r.bind(t, r.Proc(), self.sessions, self.home / "missing")
        cmd.assert_not_called()
        self.assertEqual(t["roots"], {})


if __name__ == "__main__":
    unittest.main()
