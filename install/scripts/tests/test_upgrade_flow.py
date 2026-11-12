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


# The provider key steps run `docker compose run` containers. The flow tests below
# are about the order of the update steps, so by default they see steps that pass;
# the K* tests at the end put the real methods back and test them.
REAL_PRE = m.VoIPBinCLI._provider_preflight_step
REAL_IMP = m.VoIPBinCLI._provider_import_step
REAL_STORED = m.VoIPBinCLI._provider_stored
m.VoIPBinCLI._provider_preflight_step = lambda self, project_dir, mode: True
m.VoIPBinCLI._provider_import_step = lambda self, project_dir, mode, check_only=False: True
m.VoIPBinCLI._provider_stored = lambda self, project_dir: (set(), {r[0] for r in m.PROVIDER_CONFIG_KEYS})


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
execv_pin_active = []


def fake_execv(exe, argv):
    execv_calls.append(argv)
    # record whether the op lock exists AT THE MOMENT of exec (in production
    # execv replaces the process image, so this is the last observable moment)
    execv_lock_state.append(os.path.exists(os.path.join(tmp, ".voipbin-op.lock")))
    # the interactive TUI's pinned header region must be released before the process image
    # is replaced (the re-executed CLI runs without the TUI and would leave the region set)
    execv_pin_active.append(m._PIN["active"])
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
m._PIN.update({"enabled": True, "active": True, "geom": (100, 30, 7)})
try:
    with contextlib.redirect_stdout(io.StringIO()):
        cli2.cmd_update(["all"])
except RuntimeError as e:
    seq.append("execv")
finally:
    m._PIN.update({"enabled": False, "active": False, "geom": None})
check("B3 order backup->scripts->execv", seq == ["backup", "scripts", "execv"], str(seq))
argv = execv_calls[0] if execv_calls else []
check("B3 pinned TUI header region is released before execv", execv_pin_active == [False], str(execv_pin_active))
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

# =====================================================================
# K*: provider keys (.env value -> stored config of the service)
# A fake docker plays compose config and the control binaries of the five
# services; secrets are canary strings that must never show up anywhere.
# =====================================================================
m.VoIPBinCLI._provider_preflight_step = REAL_PRE
m.VoIPBinCLI._provider_import_step = REAL_IMP
m.VoIPBinCLI._provider_stored = REAL_STORED

CANARY = {}   # (service, compose env name) -> canary secret
for _svc, _ctl, _master, _cname, _env, _key, _grp in m.PROVIDER_CONFIG_KEYS:
    CANARY[(_svc, _cname)] = f"canary-{_svc}-{_cname}-Zq9"
ALL_CANARIES = list(CANARY.values())


class FakeDocker:
    def __init__(self):
        self.reset()

    def reset(self, values=None, masters=True, env_style="dict"):
        self.calls = []       # argv lists
        self.inputs = []      # (argv, input)
        self.sets = []        # (service, key, value)
        self.fail = {}        # (service, sub) -> (rc, stderr)
        self.timeout = set()  # (service, sub)
        self.db = {svc: {} for svc in {r[0] for r in m.PROVIDER_CONFIG_KEYS}}
        for svc, ctl, master, cname, env, key, grp in m.PROVIDER_CONFIG_KEYS:
            self.db[svc][key] = "none"
        self.values = dict(CANARY) if values is None else values
        self.masters = masters
        self.env_style = env_style
        self.compose_rc = 0
        self.compose_out = None

    def compose_json(self):
        services = {}
        for svc, ctl, master, cname, env, key, grp in m.PROVIDER_CONFIG_KEYS:
            e = services.setdefault(svc, {"environment": {}})["environment"]
            if self.masters:
                e[master] = "1:masterkeymaterial"
            v = self.values.get((svc, cname))
            e[cname] = v if v is not None else ""
        if self.env_style == "list":
            for svc in services:
                services[svc]["environment"] = [f"{k}={v}" for k, v in services[svc]["environment"].items()]
        services["db"] = {"image": "x"}
        return json.dumps({"services": services})

    def run(self, cmd, **kw):
        argv = [str(c) for c in cmd]
        self.calls.append(argv)
        if kw.get("input") is not None:
            self.inputs.append((argv, kw["input"]))
        if argv[:2] == ["docker", "rm"]:
            return _FakeCompleted()
        if argv[:3] == ["docker", "compose", "config"]:
            r = _FakeCompleted()
            r.returncode = self.compose_rc
            r.stdout = self.compose_json() if self.compose_out is None else self.compose_out
            r.stderr = "The \"cd\" variable is not set"
            return r
        if argv[:3] == ["docker", "compose", "run"]:
            assert "--no-deps" in argv and "--rm" in argv and "-T" in argv, argv
            i = argv.index("--entrypoint")
            ctl = argv[i + 1].rsplit("/", 1)[1]
            svc = argv[i + 2]
            args = argv[i + 3:]
            sub = args[0] + " " + args[1]
            r = _FakeCompleted()
            if (svc, sub) in self.timeout:
                raise m.subprocess.TimeoutExpired(argv, 1)
            if (svc, sub) in self.fail:
                r.returncode, r.stderr = self.fail[(svc, sub)]
                return r
            if sub == "migrate up":
                return r
            if sub == "config list":
                r.stdout = "time=x level=info msg=started\n" + json.dumps(
                    [{"key": k, "source": src, "has_secret": src == "db"} for k, src in self.db[svc].items()], indent=2)
                return r
            if args[:2] == ["config", "set"]:
                key = args[args.index("--key") + 1]
                self.sets.append((svc, key, kw.get("input")))
                if (svc, "set " + key) in self.fail:
                    r.returncode, r.stderr = self.fail[(svc, "set " + key)]
                    return r
                self.db[svc][key] = "db"
                r.stdout = json.dumps({"key": key, "source": "db"})
                return r
        return _FakeCompleted()

    def text_all(self):
        """Everything that could leak: argv of every call."""
        return "\n".join(" ".join(c) for c in self.calls)


fd = FakeDocker()
kdir = tempfile.mkdtemp(prefix="vbk-")
os.makedirs(os.path.join(kdir, "scripts"), exist_ok=True)
envfile = os.path.join(kdir, ".env")
open(envfile, "w").write("TTS_CONFIG_ENCRYPTION_KEYS=1:masterkeymaterial\n")


def kcli():
    c = mkcli(kdir)
    c.config = {"project_dir": kdir, "db_container": "voipbin-db"}
    return c


def krun(fn, *a, **kw):
    with mock.patch.object(m.subprocess, "run", fd.run):
        return quiet(fn, *a, **kw)


def canary_leaks(text):
    return [c for c in ALL_CANARIES if c in text]


# K1 nothing to store: compose is read once, no container is started
fd.reset(values={})
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
check("K1 no values -> True, only compose config was called, status none",
      r is True and c._keys_status == "none" and [x[:3] for x in fd.calls] == [["docker", "compose", "config"]]
      and "No provider keys to store" in out, f"{r} {fd.calls} {out}")

# K2 AWS pair with one side only is skipped, nothing is set for it
vals = dict(CANARY)
vals.pop(("tts-manager", "AWS_SECRET_KEY"))
vals.pop(("transcribe-manager", "AWS_SECRET_KEY"))
fd.reset(values=vals)
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
stored_keys = {(s_, k_) for s_, k_, _v in fd.sets}
check("K2 half AWS pair is skipped, never set",
      ("tts-manager", "aws.access_key") not in stored_keys and ("transcribe-manager", "aws.access_key") not in stored_keys
      and "incomplete pair" in out and ("tts-manager", "elevenlabs.api_key") in stored_keys, f"{stored_keys} {out}")

c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "keys")
check("K2b a half AWS pair is a skip, not an old image: update keys still succeeds and does not blame the image",
      r is True and "old image" not in out and c._keys_status == "ok", f"{r} {c._keys_status} {out}")

# K3 + K4 + K22: first run stores with the value on stdin only; second run stores nothing (idempotent)
fd.reset()
before = open(envfile, "rb").read()
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
check("K4 first run stores all 11 keys, True, status ok",
      r is True and len(fd.sets) == 11 and c._keys_status == "ok", f"{r} {len(fd.sets)} {c._keys_status} {out}")
check("K4b every value arrived on stdin as value + newline, for the right key",
      all(v == CANARY[(svc, [r_[3] for r_ in m.PROVIDER_CONFIG_KEYS if r_[0] == svc and r_[5] == key][0])] + "\n"
          for svc, key, v in fd.sets), str(len(fd.sets)))
check("K4c no secret in argv or printed output",
      not canary_leaks(fd.text_all()) and not canary_leaks(out) and not canary_leaks(str(err)),
      str(canary_leaks(fd.text_all() + out)))
check("K4d the actor id is the installer constant", all(
    c_[c_.index("--actor-id") + 1] == m.PROVIDER_ACTOR_ID for c_ in fd.calls if "set" in c_ and "--actor-id" in c_), "")
check("K22 .env is not touched", open(envfile, "rb").read() == before)
n_sets = len(fd.sets)
fd.calls.clear()
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
check("K3/K9 second run: nothing set, 11 already stored, True",
      r is True and len(fd.sets) == n_sets and "already stored 11" in out, f"{len(fd.sets)} {out[-200:]}")

# K5 old image: unknown command -> skipped, not a failure for update; keys mode reports it
fd.reset()
for svc in ("tts-manager", "transcribe-manager", "message-manager"):
    fd.fail[(svc, "migrate up")] = (1, 'Error: unknown command "migrate" for "x-control"\n')
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
check("K5 old image skipped with a warning; update goes on; ai/email stored",
      r is True and c._keys_status == "skipped" and {s_ for s_, _k, _v in fd.sets} == {"ai-manager", "email-manager"}
      and "old image" in out, f"{r} {c._keys_status} {out}")
fd.reset()
for svc in ("tts-manager", "transcribe-manager", "message-manager"):
    fd.fail[(svc, "config list")] = (1, 'Error: unknown command "config" for "x-control"\n')
r, out, err = krun(kcli()._provider_import_step, kdir, "keys")
check("K5b update keys with skipped (old image) keys -> False", r is False, out)
fd.reset()
fd.fail[("tts-manager", "migrate up")] = (1, 'docker: Error response: exec: "/app/bin/tts-control": stat /app/bin/tts-control: no such file or directory')
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
check("K5c missing control binary counts as old image", r is True and "old image" in out, out)

# K6/K19/K21 failures are failures, with the cause and without the secret
for label, rcerr in (("connection refused", "dial tcp 192.0.2.1:3306: connect: connection refused"),
                     ("not found", "record not found"),
                     ("pull", "manifest for voipbin/x not found: manifest unknown"),
                     ("secret in error", "failed to store canary-email-manager-SENDGRID_API_KEY-Zq9")):
    fd.reset()
    fd.fail[("email-manager", "config list")] = (1, rcerr + "\n")
    c = kcli()
    r, out, err = krun(c._provider_import_step, kdir, "update")
    check(f"K19 {label} -> failed (not an old image), update blocked",
          r is False and c._keys_status == "failed" and "old image" not in out.split("email-manager")[1][:200]
          and not canary_leaks(out), f"{r} {out}")
fd.reset()
fd.fail[("ai-manager", "config list")] = (0, "")
fd.timeout.add(("email-manager", "config list"))
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
idx_run = [i for i, x in enumerate(fd.calls) if x[:3] == ["docker", "compose", "run"] and "email-manager" in x and x[-2:] == ["config", "list"]]
after = fd.calls[idx_run[0] + 1:idx_run[0] + 2] if idx_run else []
check("K21 timeout -> failed and the throwaway container is removed right after the timed-out call",
      r is False and idx_run and after and after[0][:3] == ["docker", "rm", "-f"] and after[0][3] == "voipbin-keys-email-manager", f"{after} {out}")
fd.reset()
orig_budget = m.PROVIDER_TOTAL_BUDGET
m.PROVIDER_TOTAL_BUDGET = -1
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
m.PROVIDER_TOTAL_BUDGET = orig_budget
check("K21b exhausted time budget -> every service failed, no container call",
      r is False and "time budget exhausted" in out and not any(x[:3] == ["docker", "compose", "run"] for x in fd.calls), out)
fd.reset(); fd.db["tts-manager"]["aws.secret_key"] = "none"
fd.fail[("tts-manager", "set aws.secret_key")] = (1, "Error: failed to store the value: boom")
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
fd.reset()
fd.fail[("tts-manager", "set aws.access_key")] = (1, "Error: failed to store the value: boom")
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
check("K16c first set of an AWS pair fails -> both members listed as failed",
      r is False and out.count("tts-manager: aws.") == 2 and "not attempted" in out, out)
fd.reset(); fd.db["tts-manager"]["aws.secret_key"] = "none"
fd.fail[("tts-manager", "set aws.secret_key")] = (1, "Error: failed to store the value: boom")
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
check("K16b first of an AWS pair stored, second fails -> failed", r is False and ("tts-manager", "aws.access_key") in {(a, b) for a, b, _ in fd.sets}, out)

# K15/K16 unusable stored rows are left alone: reported with the fix, update goes on, keys mode says no
for src in ("db_invalid", "db_undecryptable", "something_new"):
    fd.reset()
    fd.db["email-manager"]["sendgrid.api_key"] = src
    c = kcli()
    r, out, err = krun(c._provider_import_step, kdir, "update")
    check(f"K15 {src} -> not overwritten, attention, update goes on",
          r is True and c._keys_status == "attention" and ("email-manager", "sendgrid.api_key") not in {(a, b) for a, b, _ in fd.sets}
          and "needs your attention" in out and "config set --key sendgrid.api_key" in out, out)
    r, out, err = krun(kcli()._provider_import_step, kdir, "keys")
    check(f"K15b {src} -> update keys says no", r is False)
fd.reset()
fd.db["tts-manager"]["aws.access_key"] = "db"
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
check("K16 AWS pair with one side stored: both left alone, attention",
      r is True and c._keys_status == "attention" and not any(a == "tts-manager" and b.startswith("aws.") for a, b, _ in fd.sets), out)

# values that cannot be stored are not sent
fd.reset()
fd.values[("email-manager", "SENDGRID_API_KEY")] = "line1\nline2"
fd.values[("ai-manager", "GOOGLE_API_KEY")] = "x" * 600
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
check("K15c multi-line and over-long values -> attention, never set",
      r is True and c._keys_status == "attention"
      and not any(k_ in ("sendgrid.api_key", "llm.gemini.api_key") for _s, k_, _v in fd.sets), out)

# K10 compose environment shapes: list form, null, $$ -> $
fd.reset(env_style="list")
fd.values[("ai-manager", "GOOGLE_API_KEY")] = "ab$$cd"
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
got = {(s_, k_): v for s_, k_, v in fd.sets}
check("K10 list-style environment works and '$$' becomes '$'",
      got.get(("ai-manager", "llm.gemini.api_key")) == "ab$cd\n" and len(fd.sets) == 11, str(len(fd.sets)))
check("K10b JSON null / missing values count as empty",
      m.provider_compose_services({"services": {"ai-manager": {"environment": {"GOOGLE_API_KEY": None}}}})["ai-manager"]["GOOGLE_API_KEY"] == "")

# compose that cannot be read: blocking for the import, no verdict in the preflight
for label, kw_, want in (("fails", dict(compose_rc=1, compose_out=""), "docker compose config failed (exit 1)"),
                         ("prints nothing", dict(compose_out="  "), "docker compose config printed nothing"),
                         ("is not JSON", dict(compose_out="services: {}"), "docker compose config did not print JSON")):
    fd.reset()
    fd.compose_rc = kw_.get("compose_rc", 0)
    fd.compose_out = kw_.get("compose_out")
    c = kcli()
    ok_pre, out_pre, _ = krun(c._provider_preflight_step, kdir, "update")
    r, out, err = krun(c._provider_import_step, kdir, "update")
    check(f"K24 compose config {label}: preflight passes, import fails with a reason, nothing started",
          ok_pre is True and r is False and c._keys_status == "failed"
          and not any(x[:3] == ["docker", "compose", "run"] for x in fd.calls) and want in out, out)
    check(f"K24b compose stderr is never printed ({label})", "variable is not set" not in out + out_pre)

# K7 preflight: a service with a key to store must get its master key
fd.reset()
orig_cj = fd.compose_json
for label, mval in (("empty", ""), ("null", None)):
    def cj(mval=mval):
        data = json.loads(orig_cj())
        data["services"]["tts-manager"]["environment"]["TTS_CONFIG_ENCRYPTION_KEYS"] = mval
        return json.dumps(data)
    fd.compose_json = cj
    ok_pre, out, _ = krun(kcli()._provider_preflight_step, kdir, "update")
    check(f"K7d {label} master key value counts as not passed", ok_pre is False and "TTS_CONFIG_ENCRYPTION_KEYS" in out
          and "AI_CONFIG_ENCRYPTION_KEYS" not in out, out)
fd.compose_json = orig_cj
for label, kw_ in (("master key missing", dict(masters=False)),):
    fd.reset(**kw_)
    c = kcli()
    ok_pre, out, _ = krun(c._provider_preflight_step, kdir, "update")
    check("K7 preflight fails when the master key is not passed", ok_pre is False and c._keys_status == "preflight"
          and "AI_CONFIG_ENCRYPTION_KEYS" in out and "no image was changed" in out and "update keys" not in out.split("Fix it")[0], out)
    ok_pre, out, _ = krun(kcli()._provider_preflight_step, kdir, "keys")
    check("K23 preflight also guards update keys and install, with its own re-run command",
          ok_pre is False and "sudo ./voipbin update keys" in out and "no image was changed" not in out, out)
fd.reset(values={}, masters=False)
ok_pre, out, _ = krun(kcli()._provider_preflight_step, kdir, "update")
check("K23b nothing to store -> preflight passes even without master keys", ok_pre is True)
fd.reset()
ok_pre, out, _ = krun(kcli()._provider_preflight_step, kdir, "update")
check("K7b preflight passes when the master keys are passed", ok_pre is True)

# K8 / K7c in the update flow
def flow(preflight_masters=True, import_fail=False):
    fd.reset(masters=preflight_masters)
    if import_fail:
        fd.fail[("email-manager", "config list")] = (1, "connection refused\n")
    seqk = []

    def fake_call(cmd, **kw):
        sx = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        for key, name in (("sync-compose-images", "sync"), ("compose pull", "pull"), ("migrate.sh", "migrate"), ("up -d", "up")):
            if key in sx:
                seqk.append(name)
        return 0

    c = kcli()
    c._verify_stack = lambda pd, timeout=120: (seqk.append("verify"), True)[1]
    real_import = REAL_IMP

    def spy_import(self, pd, mode, check_only=False):
        seqk.append("keys")
        return real_import(self, pd, mode, check_only)

    def spy_pre(self, pd, mode):
        seqk.append("preflight")
        return REAL_PRE(self, pd, mode)
    with mock.patch.object(m.subprocess, "call", fake_call), mock.patch.object(m.VoIPBinCLI, "_provider_import_step", spy_import), \
            mock.patch.object(m.VoIPBinCLI, "_provider_preflight_step", spy_pre):
        _, out, err = krun(c.cmd_update, ["all", "--resume-from=pull", "--backup-ts=t"])
    return c, seqk, out


open(MIGRATE, "w").write("#!/bin/bash\n") if os.path.dirname(MIGRATE) == os.path.join(tmp, "scripts") else None
# the flow needs the same project layout as the B/C tests (tmp), keys steps read it through kdir config
kdir_backup, kdir = kdir, tmp
c, seqk, out = flow()
check("K8 update order: preflight, sync, pull, migrate, keys, up, verify; rc 0; keys=ok",
      seqk == ["preflight", "sync", "pull", "migrate", "keys", "up", "verify"] and rc_of(c) == 0 and "keys=ok" in out
      and not canary_leaks(out) and not canary_leaks(fd.text_all()), f"{seqk} {rc_of(c)} {out[-300:]}")
c, seqk, out = flow(import_fail=True)
check("K8b key store failure: no 'up -d', no verify, rc 20, stage=keys, keys=failed, old containers untouched",
      seqk == ["preflight", "sync", "pull", "migrate", "keys"] and rc_of(c) == 20 and "stage=keys" in out
      and "keys=failed" in out and "no restore is needed" in out, f"{seqk} {rc_of(c)} {out[-400:]}")
c, seqk, out = flow(preflight_masters=False)
check("K7c missing master key: stops before sync/pull/migrate/up, rc 20, stage=keys-preflight",
      seqk == ["preflight"] and rc_of(c) == 20 and "stage=keys-preflight" in out and "keys=preflight" in out, f"{seqk} {rc_of(c)} {out[-300:]}")
kdir = kdir_backup

# attention does not stop the upgrade
fd.reset()
fd.db["email-manager"]["sendgrid.api_key"] = "db_undecryptable"
seqk2 = []
c = kcli()
c._verify_stack = lambda pd, timeout=120: (seqk2.append("verify"), True)[1]
kdir, kdir_backup = tmp, kdir
c = kcli()
c._verify_stack = lambda pd, timeout=120: (seqk2.append("verify"), True)[1]
with mock.patch.object(m.subprocess, "call", lambda cmd, **kw: (seqk2.append("up") if "up -d" in (cmd if isinstance(cmd, str) else " ".join(cmd)) else None, 0)[1]):
    _, out, err = krun(c.cmd_update, ["all", "--resume-from=pull", "--backup-ts=t"])
kdir = kdir_backup
check("K20 attention: upgrade goes on to up and verify, rc 0, keys=attention",
      seqk2 == ["up", "verify"] and rc_of(c) == 0 and "keys=attention" in out, f"{seqk2} {rc_of(c)} {out[-300:]}")

# K12 update keys
fd.reset()
c = kcli()
_, out, err = krun(c.cmd_update, ["keys"])
check("K12 update keys: stores, rc 0, UPDATE_RESULT stage=keys keys=ok, lock released",
      rc_of(c) == 0 and len(fd.sets) == 11 and "UPDATE_RESULT rc=0 stage=keys" in out and "keys=ok" in out
      and not os.path.exists(os.path.join(kdir, ".voipbin-op.lock")), f"{rc_of(c)} {out[-300:]}")
fd.reset()
c = kcli()
_, out, err = krun(c.cmd_update, ["keys", "--check"])
check("K12b update keys --check: no container run, nothing set",
      rc_of(c) == 0 and not fd.sets and not any(x[:3] == ["docker", "compose", "run"] for x in fd.calls) and "would store" in out, out)
fd.reset(masters=False)
c = kcli()
_, out, err = krun(c.cmd_update, ["keys"])
check("K12c update keys without master keys: rc 20 stage=keys-preflight", rc_of(c) == 20 and "stage=keys-preflight" in out, out)
open(os.path.join(kdir, ".voipbin-op.lock"), "w").write("update all pid=1\n")
fd.reset()
c = kcli()
_, out, err = krun(c.cmd_update, ["keys"])
os.remove(os.path.join(kdir, ".voipbin-op.lock"))
check("K12d update keys refused while another operation holds the lock", rc_of(c) == 2 and not fd.sets, str(rc_of(c)))

# K14 unpinned update images: the key step follows the restart; a failure is a loud warning
src_unp = open(CLI_PATH).read()
i_up = src_unp.index('print(green("\u2713 Services restarted"))')
i_call = src_unp.index("self._provider_keys_unpinned(project_dir)", i_up)
check("K14 unpinned _update_images calls the key step right after the restart",
      i_call - i_up < 200)
fd.reset(masters=False)
c = kcli()
r, out, err = krun(c._provider_keys_unpinned, kdir)
check("K14b unpinned key step failing -> loud warning with the re-run command, no exception, no rc",
      err is None and "the provider keys are NOT stored" in out and "update keys" in out.split("WARNING")[1], str(err))
fd.reset()
c = kcli()
r, out, err = krun(c._provider_keys_unpinned, kdir)
check("K14c unpinned key step succeeding stores the keys without a warning",
      len(fd.sets) == 11 and "NOT stored" not in out, out[-200:])

# K11 status row states
S, D = m.provider_row_state, set()
allsvc = {r[0] for r in m.PROVIDER_CONFIG_KEYS}
stored_ai = {("ai-manager", "llm.openai_compat.api_key")}
check("K11 stored -> set", S(("OPENAI_API_KEY",), set(), stored_ai, D) == "set")
check("K11b in .env only -> pending", S(("OPENAI_API_KEY",), {"OPENAI_API_KEY"}, set(), D) == "pending")
check("K11c nowhere -> empty", S(("OPENAI_API_KEY",), set(), set(), D) == "empty")
check("K11d service not reachable -> falls back to .env",
      S(("OPENAI_API_KEY",), {"OPENAI_API_KEY"}, set(), allsvc) == "set" and S(("OPENAI_API_KEY",), set(), set(), allsvc) == "empty")
check("K11e AWS pair: one in .env -> incomplete, both -> pending, stored in tts -> set",
      S(("AWS_ACCESS_KEY", "AWS_SECRET_KEY"), {"AWS_ACCESS_KEY"}, set(), D) == "incomplete"
      and S(("AWS_ACCESS_KEY", "AWS_SECRET_KEY"), {"AWS_ACCESS_KEY", "AWS_SECRET_KEY"}, set(), D) == "pending"
      and S(("AWS_ACCESS_KEY", "AWS_SECRET_KEY"), set(), {("tts-manager", "aws.access_key"), ("tts-manager", "aws.secret_key")}, D) == "set")
check("K11f keys the services do not store keep the .env rule",
      S(("XAI_API_KEY",), {"XAI_API_KEY"}, set(), D) == "set" and S(("XAI_API_KEY",), set(), set(), D) == "empty")
fd.reset()


def fake_inspect_run(cmd, **kw):
    argv = [str(c) for c in cmd]
    if argv[:2] == ["docker", "inspect"]:
        r = _FakeCompleted()
        r.stdout = "false\n"
        return r
    return fd.run(cmd, **kw)


with mock.patch.object(m.subprocess, "run", fake_inspect_run), mock.patch.object(m.shutil, "which", lambda x: "/usr/bin/docker"):
    stored_q, down_q = REAL_STORED(kcli(), kdir)
check("K11g status asks the services only when the database container runs",
      stored_q == set() and down_q == allsvc and not any(x[:3] == ["docker", "compose", "run"] for x in fd.calls))


# K25 messages by cause and mode (round-2 review)
fd.reset()
fd.fail[("tts-manager", "set aws.secret_key")] = (1, "Error: failed to store the value: boom")
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
check("K25 half stored AWS pair: tells that the next run reports it, with the commands for both keys",
      r is False and "Only part of an AWS pair was stored" in out and "--key aws.access_key" in out and "--key aws.secret_key" in out
      and "safe to repeat" not in out, out)
fd.reset()
fd.values[("email-manager", "SENDGRID_API_KEY")] = "line1\nline2"
r, out, err = krun(kcli()._provider_import_step, kdir, "update")
check("K25b a value that cannot be stored is not described as a stored value and has no replace command",
      "not stored" in out and "fix or empty it in .env" in out and "A stored value that cannot be used" not in out
      and "config set --key sendgrid.api_key" not in out, out)
fd.reset()
for svc in ("tts-manager", "transcribe-manager", "message-manager"):
    fd.fail[(svc, "migrate up")] = (1, 'Error: unknown command "migrate" for "x-control"\n')
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "update")
check("K25c old image during update all: no 'run update all' advice, status skipped", "run 'sudo ./voipbin update keys'" in out
      and "Update the image pin" not in out and c._keys_status == "skipped", out)
c = kcli()
r, out, err = krun(c._provider_import_step, kdir, "keys")
check("K25d old image in update keys: asks for the image update, rc False, status skipped",
      r is False and "Update the image pin ('sudo ./voipbin update all')" in out and c._keys_status == "skipped", out)
fd.reset(masters=False)
c = kcli()
_, out, err = krun(c.cmd_update, ["keys", "--check"])
check("K25e update keys --check with a missing master key: rc 20 stage=keys-preflight, nothing run",
      rc_of(c) == 20 and "stage=keys-preflight" in out and not any(x[:3] == ["docker", "compose", "run"] for x in fd.calls), out)

# K26 the defences against unexpected errors print the type only, never a traceback or a value
class Boom(Exception):
    pass


def boom(*a, **k):
    raise Boom("secret-" + CANARY[("ai-manager", "GOOGLE_API_KEY")])


c = kcli()
with mock.patch.object(m.VoIPBinCLI, "_provider_import", boom):
    r, out, err = krun(c._provider_import_step, kdir, "update")
check("K26 an unexpected error in the key step -> False, type name only, no traceback, no value",
      r is False and "Boom" in out and "Traceback" not in out and not canary_leaks(out) and c._keys_status == "failed", out)
with mock.patch.object(m.VoIPBinCLI, "_provider_preflight", boom):
    r, out, err = krun(c._provider_preflight_step, kdir, "update")
check("K26b an unexpected error in the pre-flight -> True with a warning, no value",
      r is True and "Boom" in out and not canary_leaks(out), out)

# K27 status asks each service with the short status timeout and falls back per service
fd.reset()
seen_timeouts = []
real_run = fd.run


def inspect_up(cmd, **kw):
    argv = [str(x) for x in cmd]
    if argv[:2] == ["docker", "inspect"]:
        r_ = _FakeCompleted()
        r_.stdout = "true\n"
        return r_
    if argv[:3] == ["docker", "compose", "run"]:
        seen_timeouts.append(kw.get("timeout"))
    return real_run(cmd, **kw)


fd.db["ai-manager"]["llm.openai_compat.api_key"] = "db"
fd.fail[("tts-manager", "config list")] = (1, "dial tcp: connection refused")
with mock.patch.object(m.subprocess, "run", inspect_up), mock.patch.object(m.shutil, "which", lambda x: "/usr/bin/docker"):
    stored_q, down_q = REAL_STORED(kcli(), kdir)
check("K27 status: stored keys are read from the services that answer, a failing service is marked down, short timeout",
      ("ai-manager", "llm.openai_compat.api_key") in stored_q and down_q == {"tts-manager"}
      and seen_timeouts and set(seen_timeouts) == {m.PROVIDER_STATUS_TIMEOUT}, f"{stored_q} {down_q} {seen_timeouts}")

# K28 an AWS pair split over two services is not "set"
pair = ("AWS_ACCESS_KEY", "AWS_SECRET_KEY")
envs = {"AWS_ACCESS_KEY", "AWS_SECRET_KEY"}
check("K28 AWS row: pair complete in one service -> set",
      m.provider_row_state(pair, envs, {("tts-manager", "aws.access_key"), ("tts-manager", "aws.secret_key")}, set()) == "set")
check("K28b AWS row: access key in transcribe, secret key in tts -> pending, not set",
      m.provider_row_state(pair, envs, {("transcribe-manager", "aws.access_key"), ("tts-manager", "aws.secret_key")}, set()) == "pending")

# K17 the compose names the installer reads exist in docker-compose.yml.dist, with the master key variable
dist = m.yaml.safe_load(open(os.path.join(WORKTREE, "docker-compose.yml.dist")))["services"]
missing = []
for svc, ctl, master, cname, env, key, grp in m.PROVIDER_CONFIG_KEYS:
    environment = dist[svc]["environment"]
    names = {e.split("=", 1)[0] for e in environment} if isinstance(environment, list) else set(environment)
    for need in (cname, master):
        if need not in names:
            missing.append((svc, need))
check("K17 docker-compose.yml.dist passes every compose name and master key the installer relies on", not missing, str(missing))

# K18 classification
cl = m.provider_classify
check("K18 classify: ok / unsupported / failed",
      cl(0, "", "x-control") == "ok"
      and cl(1, 'Error: unknown command "config" for "tts-control"', "tts-control") == "unsupported"
      and cl(126, 'exec: "/app/bin/tts-control": stat /app/bin/tts-control: no such file or directory', "tts-control") == "unsupported"
      and cl(1, "no such file or directory", "tts-control") == "failed"
      and cl(1, "unknown config key: x", "tts-control") == "failed"
      and cl(127, "docker: command not found", "tts-control") == "failed")
check("K18b list parser skips log lines and tolerates a '[' log line before the JSON",
      m.provider_parse_list("[warn] noise\n[\n {\"key\": \"a\", \"source\": \"db\", \"has_secret\": true}\n]\n") == ({"a": "db"}, {"a": True})
      and m.provider_parse_list("nothing") is None)

# install step 5: the key step runs after verify and before the admin step, a failure aborts at step 5
src = open(CLI_PATH).read()
i_verify = src.index("[5/6] verify")
i_keys = src.index('self._provider_import_step(project_dir, "install")')
i_admin = src.index("# [6/6] admin account")
check("K13 install: key step sits between verify (5/6) and the admin account (6/6) and aborts at step 5",
      i_verify < i_keys < i_admin and 'InstallAbort(1, 5, "provider keys could not be stored"' in src[i_keys:i_admin])

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
