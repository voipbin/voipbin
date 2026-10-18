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
  C*: exit codes of `update` (0/2/10-19/64/130), the UPDATE_RESULT line,
      _verify_stack ping-even-when-unhealthy, main() exit-code plumbing,
      lock release on the unpin path, new-file ownership helper

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


import tempfile
import json

# a fake pinned project dir
tmp = tempfile.mkdtemp(prefix="vbtest-")
with open(os.path.join(tmp, "versions.lock"), "w") as f:
    json.dump({"target_commit": "deadbeef", "dbscheme_monorepo_commit": "deadbeef"}, f)
with open(os.path.join(tmp, "docker-compose.yml"), "w") as f:
    f.write("services: {}\n")
with open(os.path.join(tmp, "versions.lock.dist"), "w") as f:
    json.dump({"images": {}}, f)
os.makedirs(os.path.join(tmp, "scripts"), exist_ok=True)
SYNC_SCRIPT = os.path.join(tmp, "scripts", "sync-compose-images.sh")
open(SYNC_SCRIPT, "w").write("#!/bin/bash\n")

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


# =====================================================================
# C*: exit codes, UPDATE_RESULT, verify, main() plumbing
# =====================================================================
import time
from unittest import mock

MIGRATE = os.path.join(tmp, "scripts", "migrate.sh")


def rc_of(cli):
    # getattr: a clear FAIL (not a crash) against code without exit codes
    return getattr(cli, "last_update_rc", None)


def quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            r = fn(*a, **kw)
        except BaseException as e:  # noqa: BLE001 - returned for assertions
            return None, buf.getvalue(), e
    return r, buf.getvalue(), None


def resumed(call_rc, verify=True, verify_rc=0):
    """Run the resumed flow with per-step subprocess exit codes."""
    def fake_call(cmd, **kw):
        s = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        for key, rc in call_rc.items():
            if key in s:
                return rc
        return 0
    m.subprocess.call = fake_call
    c = mkcli(tmp)

    def vs(pd, timeout=120):
        c._verify_rc = verify_rc
        return verify
    c._verify_stack = vs
    _, out, err = quiet(c.cmd_update, ["all", "--resume-from=pull", "--backup-ts=t"])
    return c, out, err


open(MIGRATE, "w").write("#!/bin/bash\n")
c, out, err = resumed({})
check("C1 success -> rc 0, UPDATE_RESULT rc=0 stage=done",
      rc_of(c) == 0 and "UPDATE_RESULT rc=0 stage=done" in out, f"{rc_of(c)} {out[-200:]}")
c, out, _ = resumed({"sync-compose-images": 1})
check("C2 sync failure -> 13", rc_of(c) == 13 and "stage=sync" in out, str(rc_of(c)))
os.rename(SYNC_SCRIPT, SYNC_SCRIPT + ".x")
c, out, _ = resumed({})
check("C2b missing sync script -> 13", rc_of(c) == 13, str(rc_of(c)))
os.rename(SYNC_SCRIPT + ".x", SYNC_SCRIPT)
c, out, _ = resumed({"compose pull": 1})
check("C3 compose pull failure -> 14", rc_of(c) == 14, str(rc_of(c)))
c, out, _ = resumed({"migrate.sh": 1})
check("C4 migrate failure -> 15", rc_of(c) == 15, str(rc_of(c)))
os.rename(MIGRATE, MIGRATE + ".x")
c, out, _ = resumed({})
check("C4b missing migrate.sh -> 15", rc_of(c) == 15, str(rc_of(c)))
os.rename(MIGRATE + ".x", MIGRATE)
c, out, _ = resumed({"up -d": 1})
check("C5 up -d failure -> 16", rc_of(c) == 16 and "UPDATE_RESULT rc=16 stage=up" in out, str(rc_of(c)))
c, out, _ = resumed({}, verify=False, verify_rc=0)
check("C6 verify False with no recorded code -> derived 17 (never success)", rc_of(c) == 17, str(rc_of(c)))
c, out, _ = resumed({}, verify=False, verify_rc=18)
check("C6b verify rc 18 passes through", rc_of(c) == 18, str(rc_of(c)))
c, out, _ = resumed({}, verify=False, verify_rc=19)
check("C6c verify rc 19 passes through", rc_of(c) == 19, str(rc_of(c)))

# pre-exec abort codes
os.path.exists(lockfile) and os.remove(lockfile)
c = mkcli(tmp)
c._do_backup = lambda pd: None
_, out, _ = quiet(c.cmd_update, ["all"])
check("C7 backup failure -> 10", rc_of(c) == 10 and "UPDATE_RESULT rc=10 stage=backup" in out, str(rc_of(c)))
c = mkcli(tmp)
c._do_backup = lambda pd: "ts"
c._update_scripts = lambda pd, check_only=False: False   # legacy mock: no code set
_, out, _ = quiet(c.cmd_update, ["all"])
check("C8 scripts failure with legacy mock -> default 11", rc_of(c) == 11, str(rc_of(c)))
check("C8b op lock released after scripts failure", not os.path.exists(lockfile))
open(lockfile, "w").write("update all pid=99999\n")
c = mkcli(tmp)
c._do_backup = lambda pd: "ts"
_, out, _ = quiet(c.cmd_update, ["all"])
check("C8c op lock held by another operation -> 2", rc_of(c) == 2, str(rc_of(c)))
os.remove(lockfile)

# unpin release after re-exec: rc 12 and the lock is released
dist = os.path.join(tmp, "versions.lock.dist")
os.rename(dist, dist + ".x")
open(lockfile, "w").write("update all pid=1\n")
c = mkcli(tmp)
_, out, _ = quiet(c.cmd_update, ["all", "--resume-from=pull"])
check("C9 unpin after pull -> 12", rc_of(c) == 12, str(rc_of(c)))
check("C9b unpin path releases the op lock", not os.path.exists(lockfile))
os.rename(dist + ".x", dist)

# --check is success and says so
c = mkcli(tmp)
_, out, _ = quiet(c.cmd_update, ["all", "--check"])
check("C10 update all --check -> 0 with UPDATE_RESULT stage=check",
      rc_of(c) == 0 and "UPDATE_RESULT rc=0 stage=check" in out, out[-160:])

# unknown subcommand
c = mkcli(tmp)
_, out, _ = quiet(c.cmd_update, ["bogus"])
check("C11 unknown update subcommand -> 64", rc_of(c) == 64, str(rc_of(c)))

# update scripts failure -> 11 (and the code is reset on the next call)
c = mkcli(tmp)
c._update_scripts = lambda pd, check_only=False: False
_, out, _ = quiet(c.cmd_update, ["scripts"])
check("C12 update scripts failure -> 11", rc_of(c) == 11, str(rc_of(c)))
c._update_scripts = lambda pd, check_only=False: True
_, out, _ = quiet(c.cmd_update, ["scripts"])
check("C12b rc is reset per command", rc_of(c) == 0, str(rc_of(c)))

# unpinned `update all`: a failed script update must not proceed to images
unpinned = tempfile.mkdtemp(prefix="vbtest-unpinned-")
c = mkcli(unpinned)
imgs = []
c._update_scripts = lambda pd, check_only=False: False
c._update_images = lambda pd, check_only=False: imgs.append("images")
_, out, _ = quiet(c.cmd_update, ["all"])
check("C13 unpinned update all: scripts failure -> 11 and no image update",
      rc_of(c) == 11 and not imgs, f"{rc_of(c)} {imgs}")
c._update_scripts = lambda pd, check_only=False: True
_, out, _ = quiet(c.cmd_update, ["all"])
check("C13b unpinned update all with good scripts still updates images", imgs == ["images"], str(imgs))

# ---- _verify_stack: ping runs even when containers are unhealthy ----
PS_BAD = json.dumps({"Name": "voipbin-x", "Health": "unhealthy", "State": "running"})
PS_OK = json.dumps({"Name": "voipbin-x", "Health": "healthy", "State": "running"})


def verify_case(ps_line, ping_ok):
    docker_runs = []

    def rc_(cmd, **kw):
        return ps_line if "compose ps" in cmd else ""

    class R:
        returncode = 0 if ping_ok else 7
        stdout = "200" if ping_ok else "000"
        stderr = ""

    def run_(cmd, **kw):
        docker_runs.append(cmd)
        return R()

    c = mkcli(tmp)
    c.config["db_container"] = "voipbin-db"
    real_sleep = time.sleep
    with mock.patch.object(m, "run_cmd", rc_), \
            mock.patch.object(m.subprocess, "run", run_), \
            mock.patch.object(m.time, "sleep", lambda s: real_sleep(0.25)):
        res, out, err = quiet(c._verify_stack, tmp, 0.1)
    return c, res, docker_runs, err


c, res, runs, err = verify_case(PS_BAD, True)
check("C14 unhealthy container, ping ok -> False, rc 17, ping still attempted",
      res is False and c._verify_rc == 17 and len(runs) == 1 and c._verify_ping == "ok", f"{res} {c._verify_rc} {runs} {err}")
c, res, runs, err = verify_case(PS_BAD, False)
check("C14b unhealthy + ping fail -> 19", res is False and c._verify_rc == 19 and c._verify_bad == 1, f"{c._verify_rc} {err}")
c, res, runs, err = verify_case(PS_OK, False)
check("C14c healthy + ping fail -> 18", res is False and c._verify_rc == 18, f"{c._verify_rc} {err}")
c, res, runs, err = verify_case(PS_OK, True)
check("C14d healthy + ping ok -> True, rc 0", res is True and c._verify_rc == 0, f"{c._verify_rc} {err}")

# ---- main(): exit-code plumbing (check_root bypassed, CLI faked) ----
class FakeCLI:
    def __init__(self, handler):
        self.config = {"project_dir": ""}
        self.last_doctor_rc = 0
        self.last_update_rc = 0
        self.commands = {"update": lambda args: handler(self)}


def h_fail(cli):
    cli.last_update_rc = 14


def h_ok(cli):
    cli.last_update_rc = 0


def h_int(cli):
    raise KeyboardInterrupt


def h_boom(cli):
    raise ValueError("boom")


def exit_code(handler):
    with mock.patch.object(m, "check_root", lambda: None), \
            mock.patch.object(m, "VoIPBinCLI", lambda: FakeCLI(handler)), \
            mock.patch.object(sys, "argv", ["voipbin", "update", "all"]):
        _, _, err = quiet(m.main)
    return err.code if isinstance(err, SystemExit) else "no-exit"


check("C15 main exits with the update rc", exit_code(h_fail) == 14, str(exit_code(h_fail)))
check("C15b main returns normally on rc 0", exit_code(h_ok) == "no-exit", str(exit_code(h_ok)))
check("C15c Ctrl-C -> 130", exit_code(h_int) == 130, str(exit_code(h_int)))
check("C15d unexpected exception -> 1", exit_code(h_boom) == 1, str(exit_code(h_boom)))

# ---- new files take the owner of their directory (root only) ----
chowns = []
fake_st = types.SimpleNamespace(st_uid=1000, st_gid=1001)
with mock.patch.object(m.os, "geteuid", lambda: 0), \
        mock.patch.object(m.os, "stat", lambda p: fake_st), \
        mock.patch.object(m.os, "chown", lambda p, u, g: chowns.append((p, u, g))):
    m._match_parent_owner("/x/y/.voipbin-op.lock")
check("C16 root creating a file in a user-owned dir hands it to that user",
      chowns == [("/x/y/.voipbin-op.lock", 1000, 1001)], str(chowns))
chowns.clear()
with mock.patch.object(m.os, "geteuid", lambda: 0), \
        mock.patch.object(m.os, "stat", lambda p: types.SimpleNamespace(st_uid=0, st_gid=0)), \
        mock.patch.object(m.os, "chown", lambda p, u, g: chowns.append((p, u, g))):
    m._match_parent_owner("/x/y/.voipbin-op.lock")
check("C16b root-owned directory: no chown", chowns == [], str(chowns))
with mock.patch.object(m.os, "geteuid", lambda: 1000), \
        mock.patch.object(m.os, "stat", lambda p: fake_st), \
        mock.patch.object(m.os, "chown", lambda p, u, g: chowns.append((p, u, g))):
    m._match_parent_owner("/x/y/.voipbin-op.lock")
check("C16c non-root process: no chown", chowns == [], str(chowns))

# The lock is created through _acquire_op_lock; it must end up owned by the
# directory owner when root creates it.
lock_dir = tempfile.mkdtemp(prefix="vbus-lock-")
chowns = []
with mock.patch.object(m.os, "geteuid", lambda: 0), \
        mock.patch.object(m.os, "chown", lambda p, u, g: chowns.append((p, u, g))), \
        mock.patch.object(m.os, "stat", lambda p: types.SimpleNamespace(st_uid=1000, st_gid=1001)):
    got_lock = mkcli(lock_dir)._acquire_op_lock(lock_dir, "upgrade")
check("C17 _acquire_op_lock hands the new lock file to the directory owner",
      got_lock is not None and chowns == [(got_lock, 1000, 1001)], str(chowns))
if got_lock and os.path.exists(got_lock):
    os.remove(got_lock)
os.rmdir(lock_dir)

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
