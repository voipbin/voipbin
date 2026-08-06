#!/usr/bin/env python3
"""Behavioral tests for the pinned upgrade flow / backup / rollback guard.

Imports voipbin-cli.py as a module (no root, no interactive main) and
monkeypatches subprocess/os.execv/docker helpers to assert the CONTRACTS:

  B1: cmd_rollback on a pinned repo REFUSES (no snapshot restore attempted)
  B2: update all --check on a pinned repo changes nothing (no subprocess calls)
  B3: fresh 'update all': order = backup -> update_scripts -> os.execv
      (with --resume-from=pull and --backup-ts), NO pull/migrate in this process
  B4: backup failure ABORTS the upgrade (no git pull, no execv)
  B5: resumed process (--resume-from=pull): NEVER calls os.execv again;
      order = pull -> migrate -> up -d -> verify
  B6: migrate failure in resumed process aborts BEFORE 'up -d'

Run: /tmp/vbcli-venv/bin/python scripts/tests/test_upgrade_flow.py <worktree>
"""
import importlib.util
import os
import sys
import types

WORKTREE = sys.argv[1] if len(sys.argv) > 1 else "."
CLI_PATH = os.path.join(WORKTREE, "scripts", "voipbin-cli.py")

# --- import the CLI module without running main() ---
sys.argv = ["voipbin", "__noop__"]
spec = importlib.util.spec_from_file_location("vbcli", CLI_PATH)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def mkcli(project_dir):
    cli = m.VoIPBinCLI.__new__(m.VoIPBinCLI)
    cli.config = {"project_dir": project_dir}
    return cli


import tempfile, json

# a fake pinned project dir
tmp = tempfile.mkdtemp(prefix="vbtest-")
with open(os.path.join(tmp, "versions.lock"), "w") as f:
    json.dump({"target_commit": "deadbeef", "dbscheme_monorepo_commit": "deadbeef"}, f)
with open(os.path.join(tmp, "docker-compose.yml"), "w") as f:
    f.write("services: {}\n")

calls = []


def fake_subprocess_call(cmd, **kw):
    calls.append(("call", cmd if isinstance(cmd, str) else " ".join(map(str, cmd))))
    return 0


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def fake_subprocess_run(cmd, **kw):
    calls.append(("run", cmd if isinstance(cmd, str) else " ".join(map(str, cmd))))
    return _FakeCompleted()


def fake_run_cmd(cmd, **kw):
    calls.append(("run_cmd", cmd))
    return ""


execv_calls = []
execv_lock_state = []


def fake_execv(exe, argv):
    execv_calls.append(argv)
    # record whether the op lock exists AT THE MOMENT of exec (in production
    # execv replaces the process image, so this is the last observable moment)
    execv_lock_state.append(os.path.exists(os.path.join(tmp, ".voipbin-op.lock")))
    raise RuntimeError("EXECV")  # execv never returns; simulate by raising


m.subprocess.call = fake_subprocess_call
m.subprocess.run = fake_subprocess_run
m.run_cmd = fake_run_cmd
m.os.execv = fake_execv

# ---------------- B1: rollback pinned guard ----------------
import io, contextlib
calls.clear()
cli = mkcli(tmp)
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    cli.cmd_rollback([])
check("B1 rollback refuses on pinned repo (no docker/git calls)",
      not any("docker" in c[1] or "git" in c[1] for c in calls), str(calls))
# a silent no-op body would also pass the check above - require the guard
# to actually EXPLAIN the refusal (pinned/versions.lock mentioned).
_b1_txt = _out.getvalue().lower()
check("B1 refusal message names the pinned/versions.lock guard",
      ("pinned" in _b1_txt) or ("versions.lock" in _b1_txt), _b1_txt[:200])

# ---------------- B2: update all --check = no changes ----------------
calls.clear()
execv_calls.clear()
cli.cmd_update(["all", "--check"])
check("B2 update all --check performs no subprocess call/run and no execv",
      not calls and not execv_calls, f"calls={calls} execv={execv_calls}")

# ---------------- B3: fresh update all = backup -> scripts -> execv ----------------
seq = []
cli2 = mkcli(tmp)
cli2._do_backup = lambda pd: (seq.append("backup"), "20990101-000000")[1]
cli2._update_scripts = lambda pd, check_only=False: (seq.append("scripts"), True)[1]
calls.clear()
execv_calls.clear()
try:
    cli2.cmd_update(["all"])
except RuntimeError as e:
    seq.append("execv")
check("B3 order backup->scripts->execv", seq == ["backup", "scripts", "execv"], str(seq))
argv = execv_calls[0] if execv_calls else []
check("B3 execv argv carries --resume-from=pull",
      any(a == "--resume-from=pull" for a in argv), str(argv))
check("B3 execv argv carries --backup-ts",
      any(a.startswith("--backup-ts=") for a in argv), str(argv))
check("B3 no pull/migrate ran in the old process",
      not any("compose pull" in c[1] or "migrate" in c[1] for c in calls), str(calls))

# ---------------- B4: backup failure aborts ----------------
lockfile = os.path.join(tmp, ".voipbin-op.lock")
check("B3b op lock held AT the moment of execv",
      bool(execv_lock_state) and execv_lock_state[0] is True, str(execv_lock_state))
# the simulated execv RAISED (unlike production, where it never returns), so
# the pre-exec exception guard correctly released the lock afterwards.
check("B3c raising execv releases the lock (exception guard)",
      not os.path.exists(lockfile))
os.path.exists(lockfile) and os.remove(lockfile)
seq2 = []
cli3 = mkcli(tmp)
cli3._do_backup = lambda pd: (seq2.append("backup"), None)[1]  # backup FAILS
cli3._update_scripts = lambda pd, check_only=False: (seq2.append("scripts"), True)[1]
execv_calls.clear()
cli3.cmd_update(["all"])
check("B4 backup failure -> no git pull, no execv",
      seq2 == ["backup"] and not execv_calls, f"seq={seq2} execv={execv_calls}")
check("B4b op lock released after backup-failure abort",
      not os.path.exists(lockfile))

# ---------------- B4c: concurrent op refused while lock held ----------------
open(lockfile, "w").write("update all pid=99999\n")
seq2b = []
cli3b = mkcli(tmp)
cli3b._do_backup = lambda pd: (seq2b.append("backup"), "x")[1]
cli3b.cmd_update(["all"])
check("B4c second 'update all' refused while lock held", seq2b == [], str(seq2b))
os.remove(lockfile)

# ---------------- B5: resumed process order, no re-exec ----------------
seq3 = []
cli4 = mkcli(tmp)
cli4._do_backup = lambda pd: (seq3.append("backup"), "x")[1]
cli4._update_scripts = lambda pd, check_only=False: (seq3.append("scripts"), True)[1]
cli4._verify_stack = lambda pd, timeout=120: (seq3.append("verify"), True)[1]


def fake_call_order(cmd, **kw):
    s = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    if "compose pull" in s:
        seq3.append("pull")
    elif "migrate.sh" in s:
        seq3.append("migrate")
    elif "up -d" in s:
        seq3.append("up")
    return 0


m.subprocess.call = fake_call_order
execv_calls.clear()
# migrate.sh must exist for the resumed path
os.makedirs(os.path.join(tmp, "scripts"), exist_ok=True)
open(os.path.join(tmp, "scripts", "migrate.sh"), "w").write("#!/bin/bash\n")
cli4.cmd_update(["all", "--resume-from=pull", "--backup-ts=20990101-000000"])
check("B5 resumed order pull->migrate->up->verify",
      seq3 == ["pull", "migrate", "up", "verify"], str(seq3))
check("B5 resumed process never re-execs", not execv_calls, str(execv_calls))

# ---------------- B6: migrate failure aborts before up -d ----------------
seq4 = []


def fake_call_migfail(cmd, **kw):
    s = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    if "compose pull" in s:
        seq4.append("pull")
        return 0
    if "migrate.sh" in s:
        seq4.append("migrate")
        return 1  # FAIL
    if "up -d" in s:
        seq4.append("up")
        return 0
    return 0


m.subprocess.call = fake_call_migfail
cli5 = mkcli(tmp)
cli5._verify_stack = lambda pd, timeout=120: True
cli5.cmd_update(["all", "--resume-from=pull", "--backup-ts=t"])
check("B6 migrate failure -> no 'up -d'", seq4 == ["pull", "migrate"], str(seq4))

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
