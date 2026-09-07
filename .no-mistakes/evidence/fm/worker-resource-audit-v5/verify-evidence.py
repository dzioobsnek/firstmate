"""Current CLI verification and explicitly historical integration evidence audit."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path.cwd()
OUT = Path(__file__).parent
OLD = Path("/home/mantas/.no-mistakes/evidence/01M1XXDCFPT5D3QR4TYSHQ4B70")
events = []

def run(args, env=None):
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=40)
    events.append(dict(command=list(map(str, args)), code=p.returncode,
                       stdout=p.stdout, stderr=p.stderr))
    return p

head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
corrected = "a46f9ff3df8124515a669968c45c22bbd3ec81e3"
trees = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}", corrected+"^{tree}"], text=True).split()
assert trees[0] == trees[1]
for ancestor in (corrected, "0605a64d4cf2b4b02bc3c700943da3fdda05a5a2"):
    subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, "HEAD"], check=True)

with tempfile.TemporaryDirectory(prefix=".resource-cli-", dir=ROOT) as tmp:
    home = Path(tmp)
    (home / "state").mkdir()
    env = dict(os.environ, HOME=tmp, NM_HOME=str(home / "nm"), PYTHONDONTWRITEBYTECODE="1")
    cli = [str(ROOT / "bin/fm-worker-resources.sh"), "--home", tmp]
    empty = run(cli + ["--json"], env)
    report = json.loads(empty.stdout)
    assert empty.returncode == 0 and report["rows"] == []
    assert report["interval_seconds"] >= 5
    cfg = run(cli + ["--herdr-config"], env)
    parsed = tomllib.loads(cfg.stdout)
    assert parsed["ui"]["sidebar"]["agents"]["rows"][1] == ["agent", "$fm_resources"]
    assert parsed["ui"]["sidebar_width"] == 44
    assert parsed["keys"]["command"][0]["type"] == "popup"
    assert parsed["keys"]["command"][0]["key"] == "prefix+alt+r"
    assert not (home / ".config").exists()
    for args in (["--interval", "2"], ["--ttl", "30"], ["--sidebar-width", "48"],
                 ["--herdr-config", "--publish"]):
        assert run(cli + args, env).returncode == 2
    (home / "state/unsupported.meta").write_text("kind=ship\nbackend=tmux\n")
    unsupported = json.loads(run(cli + ["--json"], env).stdout)["rows"][0]
    assert unsupported["metrics"]["pids"] == 0
    assert unsupported["known_tokens"] is None and not unsupported["published"]
    for sort in ("cpu", "ram", "disk", "tokens"):
        p = run(cli + ["--sort", sort], env)
        assert p.returncode == 0 and "not a subscription bill" in p.stdout
        assert "unknown" in p.stdout and "PSS (RSS in details)" in p.stdout
    helper = str(ROOT / "bin/fm-herdr-lab.sh")
    for args in (["attach", "default"], ["attach", "fm-lab-other"],
                 ["attach", "fm-lab-resource-v5", "--session", "default"],
                 ["run", "fm-lab-resource-v5", "server", "stop"],
                 ["run", "fm-lab-resource-v5", "status", "--session", "default"]):
        assert run([helper] + args, env).returncode != 0
(OUT / "current-cli-transcript.json").write_text(json.dumps(events, indent=2))

# Execute the supplied serialized-evidence checker with only its obsolete
# HEAD-equality check replaced by the exact tree-equivalence check above.
checker = (OLD / "reviewer-handoff-check.py").read_text()
checker = checker.replace(
    'subprocess.check_output(\n    ["git", "rev-parse", "HEAD"], text=True).strip()',
    repr(corrected))
exec(compile(checker, str(OLD / "reviewer-handoff-check.py"), "exec"),
     {"__file__": str(OLD / "reviewer-handoff-check.py")})

b = json.loads((OLD / "round8-native-results.json").read_text())
assert b["head"] == corrected and b["native_browser_started"]
before = {p["pid"]: p for p in b["identities_before_sample"]}
after = {p["pid"]: p for p in b["identities_after_sample"]}
shell = b["process_before"]["process_info"]["shell_pid"]
chrome = [p for p in before.values() if p["name"] == "chrome"]
assert chrome
for proc in chrome:
    assert after[proc["pid"]]["start_ticks"] == proc["start_ticks"]
    parent = proc["ppid"]
    while parent != shell:
        parent = before[parent]["ppid"]
base, active, ended = (b[k]["rows"][0] for k in ("baseline", "during", "after_browser_exit"))
assert active["metrics"]["pss_bytes"] > base["metrics"]["pss_bytes"]
assert active["metrics"]["cpu_percent"] > base["metrics"]["cpu_percent"]
assert ended["metrics"]["pids"] < active["metrics"]["pids"]
assert b["shared_exclusion"]["shared_pids_excluded"] > 0
assert all(r["metrics"]["pids"] == 0 for r in b["shared_exclusion"]["rows"])
assert b["teardown_default_tripwire"] == "passed"

v = json.loads((OLD / "round6-live-results.json").read_text())
assert all(not r["published"] for r in v["baseline"]["rows"])
assert all(r["published"] for r in v["publication"]["rows"])
assert not next(r for r in v["stale_identity"]["rows"] if r["task"] == "busy")["published"]
assert all("fm_resources" not in json.dumps(a) for a in v["after_expiry"].values())
assert v["teardown_default_tripwire"] == "passed"

names = ["round6-badges.svg", "round6-popup.svg", "round6-expiry.svg",
         "round8-browser.png", "round8-native-results.json", "round9-reviewer-results.json",
         "round9-reviewer-driver.py", "round6-live-results.json", "round5-results.json",
         "round5-badges.txt", "round5-popup.txt", "round5-expiry.txt",
         "round5-helper.json", "round6-details-cpu.txt", "round6-details-disk.txt",
         "round6-details-ram.txt", "round6-details-tokens.txt"]
for name in names:
    shutil.copyfile(OLD / name, OUT / ("prior-" + name))
(OUT / "handoff-audit.json").write_text(json.dumps(dict(
    current_head=head, historical_head=corrected, identical_tree=trees[0],
    current_live="CLI empty inventory, unsupported worker, config output, option rejection and helper refusal",
    historical_only="Prior-run browser and terminal visuals; outer-worker native reviewer. None re-driven here.",
    reviewer="Matching run/worktree/PID/start/cwd; 6177 input + 412 output + 20480 cache read + 0 cache write = 27069 actual tokens; duplicate/wrong-project exclusions and cleanup checked",
    browser="Stable native Chromium descendants, CPU/PSS increase, shared exclusion, process-exit reduction and teardown checked",
    visuals="Prior rendered terminal cell captures, not new screenshots; seeded round6 usage is synthetic",
    copied_artifacts=names), indent=2))
print("Current CLI and historical serialized integration checks completed.")
