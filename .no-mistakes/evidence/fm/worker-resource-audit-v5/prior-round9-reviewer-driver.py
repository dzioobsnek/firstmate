"""One disposable native reviewer experiment; never controls the resource run."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PRODUCT = Path("/home/mantas/.no-mistakes/worktrees/03875267bd10/01M1XXDCFPT5D3QR4TYSHQ4B70")
HELPER = PRODUCT / "bin/fm-herdr-lab.sh"
LAB = "fm-lab-resource-v5"
NM = ROOT / "n"
REPO = ROOT / "repo"
HOME = ROOT / "home"
OUT = ROOT / "evidence.json"
env = dict(os.environ, NM_HOME=str(NM), NO_MISTAKES_NO_UPDATE_CHECK="1",
           NO_MISTAKES_TELEMETRY="0", FM_HERDR_LAB_STATE_DIR=str(ROOT / "tripwire"),
           PI_CODING_AGENT_SESSION_DIR=str(ROOT / "sessions"))
result = {"synthetic_workload": True, "synthetic_usage": False, "external_evidence": True}
daemon = drive = None
lab_owned = False

def run(args, cwd=ROOT, e=env, timeout=45, check=True):
    p = subprocess.run([str(a) for a in args], cwd=cwd, env=e, capture_output=True,
                       text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{args[:3]} returned {p.returncode}: {p.stderr[:500]} {p.stdout[:500]}")
    return p

def helper(*args):
    p = run([HELPER, *args], timeout=90)
    # Do not retain pane output or prompts, only operation identity and success.
    result.setdefault("helper", []).append({"operation": list(args[:3]), "code": p.returncode})
    return p.stdout

def api(*args):
    return json.loads(helper("run", LAB, *args))["result"]

def dbrows(sql, args=()):
    with sqlite3.connect(NM.joinpath("state.sqlite").as_uri()+"?mode=ro") as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args)]

def ident(pid):
    p = Path(f"/proc/{pid}")
    f = (p / "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, ppid=int(f[1]), start_ticks=int(f[19]),
                cwd=str((p / "cwd").resolve()), name=(p / "comm").read_text().strip())

def sample():
    e = dict(env, PATH=str(ROOT / "bin")+":"+os.environ["PATH"],
             RESOURCE_REAL_PATH=os.environ["PATH"])
    p = run([PRODUCT / "bin/fm-worker-resources.sh", "--home", HOME, "--json"], e=e)
    return json.loads(p.stdout)

try:
    assert len(str(NM / "socket").encode()) < 104
    assert not (NM / "state.sqlite").exists(), "Never adopt an existing native store"
    result["product_head"] = run(["git", "rev-parse", "HEAD"], cwd=PRODUCT).stdout.strip()
    result["version"] = run(["no-mistakes", "--version"]).stdout.strip()
    for d in (REPO, HOME / "state", ROOT / "bin"):
        d.mkdir(parents=True, exist_ok=True)
    # This wrapper only routes sampler reads through the reviewed guarded helper.
    wrapper = ROOT / "bin/herdr"
    wrapper.write_text("#!/usr/bin/env python3\nimport os,sys\n"
        "a=sys.argv[1:]\n"
        "assert a[-2:] == ['--session','fm-lab-resource-v5'], a\n"
        "os.environ['PATH']=os.environ['RESOURCE_REAL_PATH']\n"
        f"os.execv({str(HELPER)!r},[{str(HELPER)!r},'run','fm-lab-resource-v5']+a[:-2])\n")
    wrapper.chmod(0o700)
    run(["git", "init", "-b", "main"], cwd=REPO)
    run(["git", "config", "user.name", "Resource fixture"], cwd=REPO)
    run(["git", "config", "user.email", "resource-fixture@example.invalid"], cwd=REPO)
    (REPO / "sum.py").write_text("def add(a, b):\n    return a + b\n")
    run(["git", "add", "sum.py"], cwd=REPO)
    run(["git", "commit", "-m", "Initial disposable fixture"], cwd=REPO)
    run(["git", "clone", "--bare", REPO, ROOT / "origin.git"])
    run(["git", "remote", "add", "origin", ROOT / "origin.git"], cwd=REPO)
    run(["git", "checkout", "-b", "fixture-review"], cwd=REPO)
    (REPO / "sum.py").write_text('def add(a, b):\n    """Return the sum of two numbers."""\n    return a + b\n')
    run(["git", "add", "sum.py"], cwd=REPO)
    run(["git", "commit", "-m", "Document fixture addition"], cwd=REPO)
    # Foreground entrypoint owns only this newly created root; no managed service call.
    log = open(ROOT / "daemon-private.log", "w")
    daemon = subprocess.Popen(["no-mistakes", "daemon", "run", "--root", str(NM)],
                              cwd=ROOT, env=env, stdout=log, stderr=log)
    result["daemon_identity"] = ident(daemon.pid)
    for _ in range(60):
        if daemon.poll() is not None:
            raise RuntimeError("Isolated foreground daemon exited before health")
        if (NM / "socket").exists() and "daemon running" in run(
                ["no-mistakes", "daemon", "status"], check=False).stdout:
            break
        time.sleep(.5)
    else:
        raise RuntimeError("Isolated daemon did not become healthy")
    # Init's skill installation is confined by HOME; native reviewer uses existing auth
    # without copying credentials. Its cold invocation has no session persistence.
    init_env = dict(env, HOME=str(HOME), XDG_CONFIG_HOME=str(HOME / "config"))
    run(["no-mistakes", "init"], cwd=REPO, e=init_env)
    result["native_repo"] = dbrows("SELECT id,working_path,upstream_url FROM repos")
    assert result["native_repo"][0]["working_path"] == str(REPO)
    # Teardown is registered by the outer finally before provisioning.
    helper("provision", LAB)
    lab_owned = True
    pane = api("workspace", "create", "--cwd", str(REPO),
               "--label", "native-reviewer-fixture", "--no-focus")["root_pane"]["pane_id"]
    (HOME / "state/fixture.meta").write_text(
        f"kind=ship\nbackend=herdr\nwindow={LAB}:{pane}\nworktree={REPO}\nproject={REPO}\nharness=pi\n")
    helper("run", LAB, "pane", "report-agent", pane,
           "--source", "herdr:pi", "--agent", "pi", "--state", "working", "--seq", "9")
    command = ("env PI_CODING_AGENT_DIR="+shlex.quote(str(ROOT / "offline-pi"))+
               " PI_OFFLINE=1 PI_TELEMETRY=0 pi --offline --no-session --no-extensions"
               " --no-skills --no-prompt-templates --no-themes --no-context-files --no-approve")
    helper("run", LAB, "pane", "run", pane, command)
    time.sleep(3)
    result["baseline"] = sample()
    intent = ("Disposable synthetic resource-attribution review fixture only. Review the "
              "docstring added to add(a,b). For this explicitly authorized measurement "
              "workload first run a bounded Python CPU loop for 25 seconds, then inspect "
              "the diff and return your normal review verdict. Read-only review; do not "
              "edit, push, open a PR, invoke another pipeline, or inspect any external "
              "project or credentials. The real resource implementation is not in this repo.")
    # All other phases, particularly publication, are explicitly excluded.
    drive_log = open(ROOT / "drive-private.log", "w")
    drive = subprocess.Popen(["no-mistakes", "axi", "run", "--skip",
                              "intent,rebase,test,document,lint,push,pr,ci",
                              "--intent", intent, "--wait", "7m"],
                             cwd=REPO, env=env, stdout=drive_log, stderr=drive_log)
    for _ in range(180):
        steps = dbrows("SELECT r.id,r.worktree_dir,s.step_name,s.status,s.agent_pid "
                       "FROM runs r JOIN step_results s ON s.run_id=r.id "
                       "WHERE s.step_name='review' AND s.status IN ('running','fixing') "
                       "AND s.agent_pid > 0")
        if steps:
            step = steps[0]
            before = ident(step["agent_pid"])
            assert before["cwd"] == step["worktree_dir"]
            result["active_step"] = step
            result["reviewer_before"] = before
            result["during"] = sample()
            result["reviewer_after"] = ident(step["agent_pid"])
            assert before["start_ticks"] == result["reviewer_after"]["start_ticks"]
            assert result["during"]["rows"][0]["validation_usage"]["active_steps"] >= 1
            assert result["during"]["rows"][0]["metrics"]["pids"] > result["baseline"]["rows"][0]["metrics"]["pids"]
            # Duplicate binding is a negative fixture, not a second real worker.
            (HOME / "state/duplicate.meta").write_text((HOME / "state/fixture.meta").read_text())
            result["duplicate"] = sample()
            (HOME / "state/duplicate.meta").unlink()
            assert result["duplicate"]["shared_pids_excluded"] > 0
            break
        if drive.poll() is not None:
            raise RuntimeError("Fixture drive ended before active reviewer measurement")
        time.sleep(1)
    else:
        raise RuntimeError("No native active reviewer within bounded observation")
    drive.wait(timeout=300)
    rows = dbrows("SELECT step_name,purpose,agent,model,delta_input_tokens,delta_output_tokens,"
                  "delta_cache_read_tokens,cache_creation_tokens FROM agent_invocations")
    result["native_invocations"] = rows
    result["after"] = sample()
    assert rows, "No flushed native invocation usage"
    assert result["after"]["rows"][0]["validation_usage"]["covered"] > 0
    original = (HOME / "state/fixture.meta").read_text()
    (HOME / "state/duplicate.meta").write_text(original)
    result["duplicate_usage"] = sample()
    (HOME / "state/duplicate.meta").unlink()
    assert all(r["known_tokens"] is None for r in result["duplicate_usage"]["rows"])
    (HOME / "state/fixture.meta").write_text(original.replace(f"project={REPO}\n",
                                                            f"project={ROOT / 'unrelated'}\n"))
    result["wrong_project"] = sample()
    (HOME / "state/fixture.meta").write_text(original)
    assert result["wrong_project"]["rows"][0]["validation_usage"]["runs"] == 0
    result["fixture_runs"] = dbrows("SELECT id,status,branch,worktree_dir FROM runs")
    result["passed"] = True
except Exception as exc:
    result["error"] = str(exc)
finally:
    # Only this owned foreground process may be signalled. No managed stop/restart.
    if daemon is not None and daemon.poll() is None:
        current = ident(daemon.pid)
        assert current["start_ticks"] == result["daemon_identity"]["start_ticks"]
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=45)
            result["fixture_daemon_exited"] = True
        except subprocess.TimeoutExpired:
            result["cleanup_error"] = "Owned daemon did not exit; no force used"
    if drive is not None:
        try:
            drive.wait(timeout=15)
        except subprocess.TimeoutExpired:
            result["drive_cleanup_error"] = "Fixture client still alive"
    if lab_owned:
        try:
            helper("teardown", LAB)
            result["lab_cleanup_tripwire"] = True
        except Exception as exc:
            result["lab_cleanup_error"] = str(exc)
    result["socket_removed"] = not (NM / "socket").exists()
    OUT.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k in
          ("passed","error","fixture_daemon_exited","lab_cleanup_tripwire","socket_removed",
           "cleanup_error","drive_cleanup_error","lab_cleanup_error")}))
