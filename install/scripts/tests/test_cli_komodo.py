#!/usr/bin/env python3
"""Tests for the Komodo parts of scripts/voipbin-cli.py.

No real Komodo, docker or network. The Komodo bridge (komodo_lib) is replaced by
a recording fake where the CLI only has to call it, `docker` is a small script
placed first on PATH (it logs every call and can be told to fail), and the
komodo-setup.sh step of `install` is replaced through the test-only
VOIPBIN_KOMODO_SETUP override.

Two kinds of tests:
  * none mode (no Komodo files): the CLI behaves exactly as before. The bridge
    must never be called, no COMPOSE_ENV_FILES is set, exit codes are unchanged.
  * adopted mode (komodo/.adopted exists): compose actions go through the bridge
    and its exit codes (14, 16, 21) reach the caller.

Run: python3 scripts/tests/test_cli_komodo.py      (no pytest needed)
"""
import contextlib
import gzip
import importlib.util
import io
import os
import re
import shutil
import stat
import sys
import tempfile

try:
    import yaml  # noqa: F401
except ImportError:
    print("SKIP: PyYAML not installed (voipbin-cli.py imports yaml at module level)")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
INSTALL = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)
import komodo_lib as K  # noqa: E402

results = []
SENTINEL = "SENTINEL"  # every secret-looking fixture value contains this word
TMP = tempfile.mkdtemp(prefix="voipbin-cli-komodo-test.")
LOG = os.path.join(TMP, "docker.log")
ENVLOG = os.path.join(TMP, "docker.env.log")
STUB_BIN = os.path.join(TMP, "stub_bin")


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  %s  %s" % ("PASS" if cond else "FAIL", name) + (("  (%s)" % (str(detail)[:600],)) if detail and not cond else ""))


# ==============================================================================
# harness
# ==============================================================================
def load_cli():
    spec = importlib.util.spec_from_file_location("voipbin_cli_k", os.path.join(SCRIPTS, "voipbin-cli.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CLI = load_cli()
# The tests never reach the provider-key containers (test_upgrade_flow covers them).
CLI.VoIPBinCLI._provider_preflight_step = lambda self, project_dir, mode: True
CLI.VoIPBinCLI._provider_import_step = lambda self, project_dir, mode, check_only=False: True
CLI.INSTALL_REACHABILITY_URLS = ()
CLI.INSTALL_VOIP_INTERFACES = ("voipbin-test-none",)

DOCKER_STUB = r'''#!/bin/bash
echo "DOCKER:$*" >> "__LOG__"
echo "ENV:${COMPOSE_ENV_FILES-unset}:$1 $2" >> "__ENVLOG__"
T="__TMP__"
if [[ -s "$T/docker_fail" ]] && printf '%s' "$*" | grep -qEf "$T/docker_fail"; then exit 1; fi
case "$1 $2" in
  "info "*) exit 0;;
  "ps -a") cat "$T/docker_ps_all" 2>/dev/null; exit 0;;
  "volume ls") cat "$T/docker_volumes" 2>/dev/null; exit 0;;
  "compose config") if [[ "$3" == "--services" ]]; then cat "$T/compose_services" 2>/dev/null; fi; exit 0;;
  "compose ps")
     if [[ "$3" == "-q" ]]; then echo "cid-$4"; exit 0; fi
     if [[ "$3" == "--services" ]]; then cat "$T/ps_services" 2>/dev/null; exit 0; fi
     cat "$T/ps_status" 2>/dev/null; exit 0;;
  "ps --format")
     if [[ "$*" == *"--filter"* ]]; then cat "$T/docker_ps_stack" 2>/dev/null; else cat "$T/docker_ps" 2>/dev/null; fi
     exit 0;;
  "inspect cid-"*) echo healthy; exit 0;;
  "inspect voipbin-db") echo "proj"; exit 0;;
  "run --rm") echo 200; exit 0;;
  "exec -i") cat > /dev/null; exit 0;;
  "exec "*) if [[ "$*" == *dump* ]]; then echo "dump-bytes"; fi; exit 0;;
esac
exit 0
'''


def setup_stubs():
    os.makedirs(STUB_BIN, exist_ok=True)
    p = os.path.join(STUB_BIN, "docker")
    with open(p, "w") as f:
        f.write(DOCKER_STUB.replace("__LOG__", LOG).replace("__ENVLOG__", ENVLOG).replace("__TMP__", TMP))
    os.chmod(p, 0o755)
    os.environ["PATH"] = STUB_BIN + os.pathsep + os.environ.get("PATH", "")


def docker_cfg(**files):
    """Set the content of the stub's data files (None removes the file)."""
    for name in ("docker_fail", "docker_ps_all", "docker_ps", "docker_volumes", "compose_services",
                 "ps_services", "ps_status", "docker_ps_stack"):
        path = os.path.join(TMP, name)
        if name in files and files[name] is not None:
            with open(path, "w") as f:
                f.write(files[name])
        elif name not in files and os.path.exists(path):
            os.remove(path)
        elif name in files and files[name] is None and os.path.exists(path):
            os.remove(path)
    for p in (LOG, ENVLOG):
        open(p, "w").close()


def dlog():
    with open(LOG) as f:
        return f.read()


def denv():
    with open(ENVLOG) as f:
        return f.read()


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def script(project, name, body):
    write(os.path.join(project, "scripts", name), "#!/bin/bash\n" + body + "\n", 0o755)


INTERNAL_ENV = "DOMAIN_MODE=internal\nBASE_DOMAIN=voipbin.test\nHOST_EXTERNAL_IP=192.168.1.100\n"


def project(name, adopted=False, env=INTERNAL_ENV, versions_lock=False):
    d = os.path.join(TMP, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "scripts"))
    if env is not None:
        write(os.path.join(d, ".env"), env, 0o600)
    if adopted:
        write(os.path.join(d, "komodo", ".adopted"), "")
        write(os.path.join(d, ".env.komodo"), "JWT_KEY=%s-jwt\n" % SENTINEL, 0o600)
        write(os.path.join(d, "komodo", "cli-key"), "K-id:%s-secret\n" % SENTINEL, 0o600)
    if versions_lock:
        write(os.path.join(d, "versions.lock"), "{}")
    os.environ["VOIPBIN_PROJECT_DIR"] = d
    return d


def capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rv = fn(*a, **kw)
    return buf.getvalue() if rv is None else (rv, buf.getvalue())


def run_main(argv, stdin_text=""):
    """Run main() with argv (root check bypassed); (exit code, stdout)."""
    orig_check = CLI.check_root
    orig = (sys.argv, sys.stdin)
    prev = os.getcwd()
    CLI.check_root = lambda: None
    sys.argv = ["voipbin"] + list(argv)
    sys.stdin = io.StringIO(stdin_text)
    buf = io.StringIO()
    rc = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                CLI.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        CLI.check_root = orig_check
        sys.argv, sys.stdin = orig
        os.chdir(prev)
    return rc, buf.getvalue()


def last_line(out):
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else ""


class Fake(object):
    """Recording replacement of the komodo_lib bridge functions the CLI calls."""

    NAMES = ("exec_verb", "ping", "health_fields", "health", "check_core", "key_client",
             "ensure_stack", "_server_state")

    def __init__(self, **returns):
        self.calls = []
        self.codes = {}      # verb -> exit code (default 0); verb may be a tuple key (verb, n)
        self.ping_rc = 0
        self.health = {"adopted": "yes", "core": "ok", "db": "ok", "auth": "ok", "periphery_unit": "active",
                       "server": "Ok", "stack": "present", "envfile": "ok"}
        self.core_ok = True
        self.server = "Ok"
        self.stack_error = None
        self.events = []
        self.__dict__.update(returns)

    def exec_verb(self, project_dir, verb, services):
        self.calls.append((verb, list(services)))
        self.events.append("exec:" + verb)
        n = sum(1 for c in self.calls if c[0] == verb)
        return self.codes.get((verb, n), self.codes.get(verb, 0))

    def ping(self, project_dir, quiet=False):
        self.events.append("ping")
        return self.ping_rc

    def health_fields(self, project_dir, quick=False):
        self.events.append("health_fields:" + ("quick" if quick else "full"))
        return dict(self.health)

    def health_line(self, project_dir, quick=False):
        self.events.append("health:" + ("quick" if quick else "full"))
        print("KOMODO_HEALTH: adopted=yes core=ok")
        return 0

    def check_core(self, timeout=None):
        self.events.append("check_core")
        return self.core_ok

    def key_client(self, project_dir):
        return object()

    def ensure_stack(self, client, project_dir):
        self.events.append("ensure_stack")
        if self.stack_error:
            raise self.stack_error

    def server_state(self, client, timeout):
        return self.server

    def __enter__(self):
        self.saved = {n: getattr(K, n) for n in self.NAMES}
        K.exec_verb = self.exec_verb
        K.ping = self.ping
        K.health_fields = self.health_fields
        K.health = self.health_line
        K.check_core = self.check_core
        K.key_client = self.key_client
        K.ensure_stack = self.ensure_stack
        K._server_state = self.server_state
        return self

    def __exit__(self, *a):
        for n, v in self.saved.items():
            setattr(K, n, v)


class NoBridge(Fake):
    """For none mode: any use of the bridge is a failure."""

    def exec_verb(self, project_dir, verb, services):
        self.calls.append((verb, list(services)))
        return 0

    def ping(self, project_dir, quiet=False):
        self.calls.append(("ping", []))
        return 0

    def health_fields(self, project_dir, quick=False):
        self.calls.append(("health_fields", []))
        return dict(self.health)


@contextlib.contextmanager
def in_dir(d):
    prev = os.getcwd()
    os.chdir(d)
    try:
        yield
    finally:
        os.chdir(prev)


def new_cli():
    return CLI.VoIPBinCLI()


# ==============================================================================
# G1 install: guard, step order, step 3
# ==============================================================================
def install_stubs(project_dir, setup_rc=0):
    calls = os.path.join(TMP, "install_calls.log")
    open(calls, "w").close()
    script(project_dir, "doctor.sh", 'echo doctor >> "%s"\necho "DOCKER: pass"\nexit 0' % calls)
    script(project_dir, "init.sh", 'echo "init $*" >> "%s"\ncat > .env <<EOF\nDOMAIN_MODE=internal\n'
           'BASE_DOMAIN=voipbin.test\nHOST_EXTERNAL_IP=192.168.1.100\nEOF\ntouch docker-compose.yml versions.lock\nexit 0' % calls)
    script(project_dir, "setup-host.sh", 'echo setup-host >> "%s"\nexit 0' % calls)
    script(project_dir, "start.sh", 'echo start >> "%s"\nexit 0' % calls)
    script(project_dir, "create-admin.sh", 'echo create-admin >> "%s"\necho "VOIPBIN_ADMIN: status=ok customer_id=c1 agent_id=a1"' % calls)
    fake = os.path.join(TMP, "fake-komodo-setup.sh")
    write(fake, '#!/bin/bash\necho komodo-setup >> "%s"\nexit %d\n' % (calls, setup_rc), 0o755)
    os.environ[CLI.KOMODO_SETUP_ENV] = fake
    return calls


def calls_of(path):
    with open(path) as f:
        return [l.split()[0] for l in f.read().splitlines() if l.strip()]


def group_install():
    print("G1 install")
    orig_verify = CLI.VoIPBinCLI._install_wait_and_verify
    CLI.VoIPBinCLI._install_wait_and_verify = lambda self, d: (True, "3 containers", [], [])
    try:
        # --- order: init, komodo setup, host setup, start, admin; [n/7]
        docker_cfg()
        d = project("g1_order", env=None)
        calls = install_stubs(d)
        rc, out = run_main(["install", "--yes", "--admin-email", "a@example.com", "--mode", "internal"])
        seq = calls_of(calls)
        check("G1.1 order: doctor, init, komodo-setup, setup-host, start, create-admin",
              rc == 0 and seq == ["doctor", "init", "komodo-setup", "setup-host", "start", "create-admin"], (rc, seq, out[-300:]))
        marks = re.findall(r"\[(\d)/(\d)\]", out)
        check("G1.2 steps are numbered [1/7] to [7/7]", marks == [(str(i), "7") for i in range(1, 8)], marks)
        i3 = out.index("[3/7]")
        check("G1.3 step 3 is Komodo and sits between configuration and host setup",
              out.index("[2/7]") < i3 < out.index("[4/7]") and "Komodo" in out[i3:out.index("[4/7]")])

        # --- step 3 failure: error line, exit code, main stack untouched
        for setup_rc, want in ((2, 2), (1, 1), (21, 1), (7, 1)):
            docker_cfg()
            d = project("g1_fail%d" % setup_rc, env=None)
            calls = install_stubs(d, setup_rc=setup_rc)
            rc, out = run_main(["install", "--yes", "--admin-email", "a@example.com"])
            line = last_line(out)
            seq = calls_of(calls)
            check("G1.4 komodo setup exit %d -> install exit %d, step=3 line with --resume" % (setup_rc, want),
                  rc == want and line.startswith("VOIPBIN_INSTALL: status=error step=3 reason=") and 'next="sudo ./voipbin install --resume' in line, (rc, line))
            check("G1.5 komodo setup exit %d: host setup and start never run" % setup_rc,
                  seq == ["doctor", "init", "komodo-setup"], seq)

        # --- guard: an existing install is refused before anything changes
        base = "proj"
        signals = {
            "container": dict(docker_ps_all="voipbin-db|proj|/elsewhere\n"),
            "foreign-label container with a fixed name": dict(docker_ps_all="voipbin-db|other|\n"),
            "install marker": dict(),
        }
        for label, cfg in signals.items():
            docker_cfg(**cfg)
            d = project("g1_guard_" + re.sub(r"\W", "_", label), env=None)
            if label == "install marker":
                write(os.path.join(d, ".voipbin-installed"), "{}")
            # the directory name drives the compose project name
            d2 = os.path.join(TMP, base)
            shutil.rmtree(d2, ignore_errors=True)
            shutil.move(d, d2)
            os.environ["VOIPBIN_PROJECT_DIR"] = d2
            calls = install_stubs(d2)
            before = None
            rc, out = run_main(["install", "--yes", "--admin-email", "a@example.com"])
            line = last_line(out)
            check("G1.6 guard refuses (%s): exit 2, nothing run" % label,
                  rc == 2 and "status=error" in line and calls_of(calls) == [] and not os.path.exists(os.path.join(d2, ".env")), (rc, line, calls_of(calls)))
            shutil.rmtree(d2, ignore_errors=True)
        # database volume (with a .env, as left by a started install): refused too
        docker_cfg(docker_volumes="proj_db_data\n")
        d2 = os.path.join(TMP, base)
        shutil.rmtree(d2, ignore_errors=True)
        project(base, env=INTERNAL_ENV)
        calls = install_stubs(d2)
        rc, out = run_main(["install", "--resume", "--yes", "--admin-email", "a@example.com"])
        check("G1.6 guard refuses (database volume, --resume): exit 2, nothing run",
              rc == 2 and "status=error step=0" in last_line(out) and calls_of(calls) == [], (rc, last_line(out), calls_of(calls)))
        # existing install with .env: --resume does not move it either
        docker_cfg(docker_ps_all="voipbin-db|proj|/elsewhere\n")
        d2 = os.path.join(TMP, base)
        shutil.rmtree(d2, ignore_errors=True)
        project(base, env=INTERNAL_ENV)
        calls = install_stubs(d2)
        env_before = open(os.path.join(d2, ".env"), "rb").read()
        rc, out = run_main(["install", "--resume", "--yes", "--admin-email", "a@example.com"])
        check("G1.7 guard also stops --resume: exit 2, .env bytes unchanged, no komodo call",
              rc == 2 and open(os.path.join(d2, ".env"), "rb").read() == env_before and calls_of(calls) == [], (rc, last_line(out)))
        # a resumed Komodo install (artifacts present) passes the guard with a container signal
        write(os.path.join(d2, ".env.komodo"), "X=1\n", 0o600)
        os.makedirs(os.path.join(d2, "certs", "api"), exist_ok=True)
        for n in ("cert.pem", "privkey.pem"):
            open(os.path.join(d2, "certs", "api", n), "w").close()
        open(os.path.join(d2, "docker-compose.yml"), "w").close()
        open(os.path.join(d2, "versions.lock"), "w").close()
        rc, out = run_main(["install", "--resume", "--yes", "--admin-email", "a@example.com"])
        check("G1.8 Komodo artifacts let a resumed install through the guard (step 3 runs again)",
              rc == 0 and "komodo-setup" in calls_of(calls), (rc, calls_of(calls), last_line(out)))
        # a missing .env next to a database volume: the Komodo files are named in the advice
        docker_cfg(docker_volumes="proj_db_data\n")
        d2 = os.path.join(TMP, base)
        shutil.rmtree(d2, ignore_errors=True)
        project(base, env=None)
        write(os.path.join(d2, ".env.komodo"), "X=1\n", 0o600)
        calls = install_stubs(d2)
        rc, out = run_main(["install", "--yes", "--admin-email", "a@example.com"])
        check("G1.10 volume without .env on a Komodo install: exit 1 and the advice names .env.komodo",
              rc == 1 and ".env.komodo .env.komodo" in out and calls_of(calls) == [], (rc, out[-400:]))
        shutil.rmtree(d2, ignore_errors=True)
        project(base, env=None)
        calls = install_stubs(d2)
        rc, out = run_main(["install", "--yes", "--admin-email", "a@example.com"])
        check("G1.11 the same without Komodo files keeps the old advice only", rc == 1 and ".env.komodo" not in out, (rc, out[-300:]))
        # marker and .env present but .env.komodo gone, with a database volume: same warning
        shutil.rmtree(d2, ignore_errors=True)
        project(base, adopted=True)
        os.remove(os.path.join(d2, ".env.komodo"))
        calls = install_stubs(d2)
        rc, out = run_main(["install", "--resume", "--yes", "--admin-email", "a@example.com"])
        check("G1.12 adopted install without .env.komodo next to a database volume: refused (exit 1), advice names the file",
              rc == 1 and "there is no .env.komodo" in out and "status=error step=0" in last_line(out) and calls_of(calls) == [], (rc, out[-400:]))
        docker_cfg()
        # docker lookup failure refuses (closed)
        docker_cfg(docker_fail="^ps -a")
        d3 = os.path.join(TMP, "g1_closed")
        project("g1_closed", env=None)
        calls = install_stubs(d3)
        rc, out = run_main(["install", "--yes", "--admin-email", "a@example.com"])
        check("G1.9 guard refuses when the container list cannot be read", rc == 2 and calls_of(calls) == [], (rc, last_line(out)))
        docker_cfg()
    finally:
        CLI.VoIPBinCLI._install_wait_and_verify = orig_verify
        os.environ.pop(CLI.KOMODO_SETUP_ENV, None)


# ==============================================================================
# G2/G3 start, stop, restart
# ==============================================================================
SIDECARS = [("asterisk-call", "asterisk-call-proxy"), ("asterisk-conference", "asterisk-conference-proxy"),
            ("asterisk-registrar", "asterisk-registrar-proxy"), ("pipecat-manager", "pipecat-script-runner")]


def group_none_lifecycle():
    print("G2 none mode keeps the direct compose path")
    docker_cfg(docker_fail=r"^compose (stop|restart|up)", ps_services="db\nredis\napi-manager\nkamailio\n")
    d = project("g2_none")
    with NoBridge() as fake:
        rc, out = run_main(["stop", "api-manager"])
        check("G2.1 stop <svc> with failing compose still exits 0 (unchanged)", rc == 0, (rc, out))
        rc, out = run_main(["stop", "--all"])
        check("G2.2 stop --all exits 0", rc == 0, rc)
        rc, out = run_main(["stop"])
        check("G2.3 stop (app services) exits 0", rc == 0, rc)
        rc, out = run_main(["restart", "api-manager"])
        check("G2.4 restart <svc> exits 0", rc == 0, rc)
        rc, out = run_main(["restart"])
        check("G2.5 restart (all) exits 0", rc == 0, rc)
        rc, out = run_main(["start", "api-manager"])
        check("G2.6 start <svc> failure keeps exit 1", rc == 1, (rc, out))
        log = dlog()
        for want in ("compose stop api-manager", "compose stop api-manager kamailio",
                     "compose restart api-manager", "compose up -d api-manager"):
            check("G2.7 docker call kept: %s" % want, "DOCKER:" + want in log, log[:400])
        check("G2.8 the bridge is never used", fake.calls == [], fake.calls)
        check("G2.9 COMPOSE_ENV_FILES is not set for compose calls", "ENV:unset:" in denv() and ":.env" not in denv().replace("unset", ""), denv()[:300])
    # backup/restore: no Komodo step in none mode
    docker_cfg(docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n")
    d = project("g2_backup", versions_lock=True)
    cli = new_cli()
    with NoBridge() as fake, in_dir(d):
        out = capture(cli.cmd_backup, [])
        log = dlog()
        ts_dirs = os.listdir(os.path.join(d, "backups"))
        files = sorted(os.listdir(os.path.join(d, "backups", ts_dirs[0]))) if ts_dirs else []
        check("G2.10 none backup: no mongodump, no Komodo files in the backup",
              "mongodump" not in log and "Komodo" not in out and files == ["config", "manifest.json", "mysql.sql.gz"]
              and not os.path.exists(os.path.join(d, "backups", ts_dirs[0], "config", ".env.komodo")), (files, out[-300:]))
        ts = ts_dirs[0]
        docker_cfg(docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n")
        out = capture(cli.cmd_restore, [ts, "--force"])
        log = dlog()
        check("G2.11 none restore: same steps and next-steps text as before",
              "Restore complete." in out and "1. voipbin start          (or: docker compose up -d)" in out
              and "mongorestore" not in log and "docker stop" not in log and cli.last_rc == 0, out[-400:])
        docker_cfg(docker_ps="voipbin-redis\n")
        out = capture(cli.cmd_restore, [ts, "--force"])
        check("G2.12 none restore hint keeps the compose command", "  docker compose up -d db redis" in out, out)
        check("G2.13 none: bridge untouched by backup and restore", fake.calls == [], fake.calls)
    docker_cfg()


def group_adopted_lifecycle():
    print("G3 adopted: compose actions go through the bridge")
    docker_cfg(ps_services="db\nredis\napi-manager\nkamailio\n", docker_ps="voipbin-db\n")
    d = project("g3_adopted", adopted=True)
    with Fake() as fake:
        rc, out = run_main(["start", "api-manager"])
        check("G3.1 start <svc> -> exec up [svc], exit 0", rc == 0 and fake.calls == [("up", ["api-manager"])], (rc, fake.calls))
        for code in (16, 21, 1):
            fake.calls.clear()
            fake.codes = {"up": code}
            rc, out = run_main(["start", "api-manager"])
            check("G3.2 start <svc> bridge exit %d reaches the caller unchanged" % code, rc == code, (rc, out))
        fake.codes = {}
        fake.calls.clear()
        rc, out = run_main(["start"])  # no scripts/start.sh: fallback
        check("G3.3 start fallback (no start.sh) -> exec up []", rc == 0 and fake.calls == [("up", [])], (rc, fake.calls))
        fake.codes = {"up": 21}
        rc, out = run_main(["start"])
        check("G3.4 start fallback exit 21 is not squashed", rc == 21, rc)
        fake.codes = {}
        fake.calls.clear()
        rc, out = run_main(["stop", "api-manager"])
        check("G3.5 stop <svc> -> exec stop [svc]", rc == 0 and fake.calls == [("stop", ["api-manager"])], (rc, fake.calls))
        fake.calls.clear()
        rc, out = run_main(["stop", "--all"])
        check("G3.6 stop --all -> exec stop []", rc == 0 and fake.calls == [("stop", [])], (rc, fake.calls))
        fake.calls.clear()
        rc, out = run_main(["stop"])
        check("G3.7 stop keeps db and redis, stops the app services only",
              rc == 0 and fake.calls == [("stop", ["api-manager", "kamailio"])], (rc, fake.calls))
        fake.codes = {"stop": 21}
        rc, out = run_main(["stop", "api-manager"])
        check("G3.8 stop exit 21 is the process exit code", rc == 21, (rc, out))
        fake.codes = {"stop": 1}
        rc, out = run_main(["stop"])
        check("G3.9 stop failure exit 1 is the process exit code", rc == 1, rc)
        fake.codes = {}
        fake.calls.clear()
        rc, out = run_main(["restart", "api-manager"])
        check("G3.10 restart <svc> -> exec restart [svc]", rc == 0 and fake.calls == [("restart", ["api-manager"])], (rc, fake.calls))
        fake.codes = {"restart": 21}
        rc, out = run_main(["restart", "api-manager"])
        check("G3.11 restart exit 21 is the process exit code", rc == 21, (rc, out))
        # full restart: each owner, then its proxy, then everything else; health wait kept
        fake.codes = {}
        fake.calls.clear()
        docker_cfg(ps_services="db\nasterisk-call\nasterisk-call-proxy\napi-manager\n")
        rc, out = run_main(["restart", "asterisk-call"])
        check("G3.12 sidecar owner: owner, health wait, then proxy (one exec each, in order)",
              rc == 0 and fake.calls == [("restart", ["asterisk-call"]), ("restart", ["asterisk-call-proxy"])]
              and "Waiting for asterisk-call to become healthy" in out, (rc, fake.calls, out))
        order = [i for i, l in enumerate(dlog().splitlines()) if "ps -q asterisk-call" in l or "inspect cid-asterisk-call" in l]
        check("G3.13 the health wait (compose ps -q, inspect) happens between the two restarts", len(order) >= 2, dlog())
        fake.calls.clear()
        docker_cfg(ps_services="db\nasterisk-call\nasterisk-call-proxy\napi-manager\nkamailio\n")
        rc, out = run_main(["restart"])
        restarts = [c[1] for c in fake.calls]
        flat = [s for c in restarts for s in c]
        ok = all(flat.index(o) < flat.index(p) for o, p in SIDECARS)
        check("G3.14 full restart: owner before proxy for every pair, other services last, each call is a restart",
              rc == 0 and ok and restarts[-1] == ["db", "api-manager", "kamailio"] and all(c[0] == "restart" for c in fake.calls),
              (rc, restarts))
        fake.calls.clear()
        fake.codes = {"restart": 21}
        rc, out = run_main(["restart"])
        check("G3.15 full restart stops at the first unreachable (21): one call, exit 21", rc == 21 and len(fake.calls) == 1, (rc, fake.calls))
        # REPL: a failed earlier command does not leak its code into the next one
        fake.codes = {}
        cli = new_cli()
        cli.last_rc = 21
        with in_dir(d):
            capture(cli.cmd_stop, ["api-manager"])
            a = cli.last_rc
            cli.last_rc = 21
            capture(cli.cmd_restart, ["api-manager"])
            b = cli.last_rc
        check("G3.16 stop and restart reset last_rc on entry", a == 0 and b == 0, (a, b))
        check("G3.17 adopted compose actions never run docker compose directly",
              not re.search(r"DOCKER:compose (up|stop|restart|pull)", dlog()), dlog())
    docker_cfg()


# ==============================================================================
# G4 update
# ==============================================================================
def update_project(name, adopted):
    d = project(name, adopted=adopted, versions_lock=True)
    write(os.path.join(d, "versions.lock.dist"), "{}")
    script(d, "common.sh", 'sync_cert_vars() { echo sync >> "%s"; return ${SYNC_RC:-0}; }' % os.path.join(TMP, "events.log"))
    script(d, "sync-compose-images.sh", 'echo sync-compose >> "%s"' % os.path.join(TMP, "events.log"))
    script(d, "migrate.sh", 'echo migrate >> "%s"; exit ${MIGRATE_RC:-0}' % os.path.join(TMP, "events.log"))
    write(os.path.join(d, "scripts", "komodo-hook.sh"), "#!/bin/bash\n# komodo-hook-format: 1\n", 0o755)
    open(os.path.join(TMP, "events.log"), "w").close()
    return d


def script_events():
    with open(os.path.join(TMP, "events.log")) as f:
        return f.read().split()


def group_update_preexec():
    print("G4 update: step 1.5 (before the backup)")
    docker_cfg()
    for adopted in (True, False):
        d = update_project("g4_pre_%s" % adopted, adopted)
        cli = new_cli()
        events = []
        cli._do_backup = lambda pd, _e=events: (_e.append("backup") or None)
        with (Fake() if adopted else NoBridge()) as fake, in_dir(d):
            fake.ping_rc = 0
            out = capture(cli.cmd_update, ["all"])
            seq = script_events()
            if adopted:
                check("G4.1 adopted: ping, sync_cert_vars, then the backup", fake.events[:1] == ["ping"] and seq == ["sync"] and events == ["backup"],
                      (fake.events, seq, events))
                check("G4.2 adopted: lock released when the later step aborts", not os.path.exists(os.path.join(d, ".voipbin-op.lock")))
            else:
                check("G4.3 none: no ping, no sync_cert_vars, backup runs (current flow)",
                      fake.calls == [] and seq == [] and events == ["backup"] and "Step 1.5" not in out, (fake.calls, seq, events))
    # unreachable Komodo: abort before the backup
    d = update_project("g4_pre_down", True)
    cli = new_cli()
    events = []
    cli._do_backup = lambda pd: (events.append("backup") or "ts")
    with Fake() as fake, in_dir(d):
        fake.ping_rc = 21
        out = capture(cli.cmd_update, ["all"])
        check("G4.4 ping 21: no backup, lock released, UPDATE_RESULT rc=21 stage=komodo-preflight, exit code 21",
              events == [] and not os.path.exists(os.path.join(d, ".voipbin-op.lock"))
              and "UPDATE_RESULT rc=21 stage=komodo-preflight" in out and cli.last_update_rc == 21, (events, out[-300:]))
    # sync_cert_vars fails
    d = update_project("g4_pre_sync", True)
    cli = new_cli()
    events = []
    cli._do_backup = lambda pd: (events.append("backup") or "ts")
    os.environ["SYNC_RC"] = "21"
    try:
        with Fake() as fake, in_dir(d):
            out = capture(cli.cmd_update, ["all"])
    finally:
        os.environ.pop("SYNC_RC")
    check("G4.5 sync_cert_vars failure: no backup, lock released, stage komodo-preflight",
          events == [] and not os.path.exists(os.path.join(d, ".voipbin-op.lock"))
          and "stage=komodo-preflight" in out and cli.last_update_rc == 21, (events, out[-300:]))
    d = update_project("g4_pre_sync2", True)
    cli = new_cli()
    os.environ["SYNC_RC"] = "2"
    try:
        with Fake() as fake, in_dir(d):
            out = capture(cli.cmd_update, ["all"])
    finally:
        os.environ.pop("SYNC_RC")
    check("G4.6 sync_cert_vars exit 2 (value too long) is passed on", cli.last_update_rc == 2 and "stage=komodo-preflight" in out, out[-200:])


def run_resumed(d, fake, hook, scripts=True):
    """Run the resumed (new code) half of `update all`; returns (cli, output)."""
    cli = new_cli()
    K_hook = K.HOOK_COMMAND
    K.HOOK_COMMAND = hook
    try:
        with in_dir(d):
            out = capture(cli.cmd_update, ["all", "--resume-from=pull", "--backup-ts=t1"])
    finally:
        K.HOOK_COMMAND = K_hook
    return cli, out


def group_update_resumed():
    print("G4 update: steps 4.5, pull, up, verify")
    docker_cfg()
    hook = os.path.join(TMP, "hook-dir", "komodo-hook.sh")
    shutil.rmtree(os.path.dirname(hook), ignore_errors=True)
    # --- 4.5: hook is installed when missing, and the order is hook, ping, pull, migrate, up
    d = update_project("g4_res_ok", True)
    with Fake() as fake:
        cli, out = run_resumed(d, fake, hook)
        want = open(os.path.join(d, "scripts", "komodo-hook.sh"), "rb").read()
        st = os.stat(hook) if os.path.exists(hook) else None
        check("G4.7 4.5: a missing installed hook is copied from the checkout (mode 755)",
              st is not None and open(hook, "rb").read() == want and stat.S_IMODE(st.st_mode) == 0o755, out[-300:])
        check("G4.8 order: ping (4.5), pull, migrate, up, then verify's health",
              fake.events[:2] == ["ping", "exec:pull"] and "exec:up" in fake.events and script_events() == ["sync-compose", "migrate"]
              and fake.events.index("exec:pull") < fake.events.index("exec:up"), (fake.events, script_events()))
        check("G4.9 komodo steps are the only compose actions: no direct docker compose pull/up",
              not re.search(r"DOCKER:compose (pull|up)", dlog()), dlog())
        ino = os.stat(hook).st_ino
    d = update_project("g4_res_same", True)
    with Fake() as fake:
        mt = os.stat(hook).st_mtime_ns
        cli, out = run_resumed(d, fake, hook)
        check("G4.10 4.5: an identical installed hook is left alone", os.stat(hook).st_ino == ino and os.stat(hook).st_mtime_ns == mt)
    # a different installed hook is replaced
    d = update_project("g4_res_old", True)
    with open(hook, "w") as f:
        f.write("old\n")
    os.chmod(hook, 0o755)
    with Fake() as fake:
        cli, out = run_resumed(d, fake, hook)
        check("G4.11 4.5: a different installed hook is replaced", open(hook, "rb").read() == open(os.path.join(d, "scripts", "komodo-hook.sh"), "rb").read())
    # 4.5 failures: 21, stage komodo-converge, nothing migrated, lock released
    for label, setup in (("Komodo unreachable", lambda fake, d: setattr(fake, "ping_rc", 21)),
                         ("hook source missing", lambda fake, d: os.remove(os.path.join(d, "scripts", "komodo-hook.sh")))):
        d = update_project("g4_res_fail", True)
        write(os.path.join(d, ".voipbin-op.lock"), "update all pid=%d t\n" % os.getpid())
        with Fake() as fake:
            setup(fake, d)
            cli, out = run_resumed(d, fake, hook)
            check("G4.12 4.5 %s: exit 21 stage=komodo-converge, no pull, no migrate, lock released" % label,
                  cli.last_update_rc == 21 and "stage=komodo-converge" in out and "exec:pull" not in fake.events
                  and script_events() == [] and not os.path.exists(os.path.join(d, ".voipbin-op.lock")), (cli.last_update_rc, out[-300:], fake.events))
    # pull / up bridge codes
    for verb, code, want, stage in (("pull", 14, 14, "pull"), ("pull", 21, 21, "pull"), ("pull", 1, 14, "pull"),
                                    ("up", 16, 16, "up"), ("up", 21, 21, "up"), ("up", 1, 16, "up")):
        d = update_project("g4_res_code", True)
        with Fake() as fake:
            fake.codes = {verb: code}
            cli, out = run_resumed(d, fake, hook)
            migrated = "migrate" in script_events()
            check("G4.13 bridge %s exit %d -> update exit %d, stage %s%s" % (verb, code, want, stage, ", migrate not run" if verb == "pull" else ""),
                  cli.last_update_rc == want and ("stage=%s" % stage) in out and (verb != "pull" or not migrated), (cli.last_update_rc, out[-250:]))
    # none: the same flow with direct compose
    d = update_project("g4_res_none", False)
    with NoBridge() as fake:
        cli, out = run_resumed(d, fake, hook)
        log = dlog()
        check("G4.14 none: pull and up are direct docker compose calls, no bridge, no step 4.5",
              "DOCKER:compose pull" in log and "DOCKER:compose up -d" in log and fake.calls == [] and "Step 4.5" not in out, (log, fake.calls))
    docker_cfg()
    d = update_project("g4_res_none_fail", False)
    docker_cfg(docker_fail=r"^compose pull")
    with NoBridge() as fake:
        cli, out = run_resumed(d, fake, hook)
        check("G4.15 none: a failed pull keeps exit 14 (unchanged)", cli.last_update_rc == 14 and "stage=pull" in out, out[-200:])
    docker_cfg()


def group_verify():
    print("G4 verify: Komodo counts as containers")
    docker_cfg()
    cases = (("core down", dict(core="down", db="unknown", auth="unknown", server="unknown", stack="unknown"), ["komodo-core"]),
             ("db timeout", dict(db="timeout", auth="unknown", server="unknown", stack="unknown"), ["komodo-db"]),
             ("periphery unit failed", dict(periphery_unit="failed"), ["komodo-periphery"]),
             ("server NotOk", dict(server="NotOk"), ["komodo-periphery"]),
             ("all healthy", dict(), []))
    for label, patch, names in cases:
        d = update_project("g4_verify", True)
        cli = new_cli()
        with Fake() as fake, in_dir(d):
            fake.health.update(patch)
            _rv, out = capture(lambda: cli._verify_stack(d, timeout=1))
            rc = cli._verify_rc
            check("G4.16 verify (%s): containers_bad counts %d, exit %s" % (label, len(names), 17 if names else 0),
                  cli._verify_bad == len(names) and rc == (17 if names else 0)
                  and all(n in out for n in names) and [n for n, _w in cli._verify_komodo_bad] == names, (cli._verify_bad, rc, out))
    # an unreachable API next to a Komodo problem is exit 19 (both), as for containers
    docker_cfg(docker_fail=r"^run ")
    d = update_project("g4_verify_both", True)
    cli = new_cli()
    with Fake() as fake, in_dir(d):
        fake.health["core"] = "down"
        capture(lambda: cli._verify_stack(d, timeout=1))
        check("G4.16b Komodo problem and failed API ping: exit 19", cli._verify_rc == 19 and cli._verify_ping == "fail", (cli._verify_rc, cli._verify_ping))
    docker_cfg()
    # the failure text points at doctor and Komodo, not at a data rollback
    d = update_project("g4_verify_msg", True)
    with Fake() as fake:
        fake.health["core"] = "down"
        cli, out = run_resumed(d, fake, os.path.join(TMP, "hook-dir", "komodo-hook.sh"))
        check("G4.17 Komodo-only failure: exit 17, containers_bad=1, doctor and komodo setup advice, no rollback advice",
              cli.last_update_rc == 17 and "containers_bad=1" in out and "voipbin doctor" in out and "komodo setup" in out
              and "Data rollback" not in out and "git checkout" not in out, out[-600:])
    d = update_project("g4_verify_none", False)
    with NoBridge() as fake:
        cli, out = run_resumed(d, fake, os.path.join(TMP, "hook-dir", "komodo-hook.sh"))
        check("G4.18 none: verify never asks for Komodo health", fake.calls == [] and cli._verify_bad == 0 and cli.last_update_rc == 0, (fake.calls, out[-200:]))
    docker_cfg()


def group_unpinned():
    print("G4 unpinned update paths")
    docker_cfg()
    d = project("g4_unpinned", adopted=True)
    cli = new_cli()
    with Fake() as fake, in_dir(d):
        cli._compose_pull(d)
        cli._compose_up(d)
        check("G4.19 unpinned pull and up go through the bridge (adopted)", fake.calls == [("pull", []), ("up", [])] and "DOCKER:compose" not in dlog(), (fake.calls, dlog()))
        fake.codes = {"pull": 21}
        capture(cli._compose_pull, d)
        check("G4.20 a failed unpinned pull is recorded in the update exit code", cli.last_update_rc == 21)
    d = project("g4_unpinned_none")
    cli = new_cli()
    with NoBridge() as fake, in_dir(d):
        cli._compose_pull(d)
        cli._compose_up(d)
        check("G4.21 none: direct docker compose pull and up -d", fake.calls == [] and "DOCKER:compose pull" in dlog() and "DOCKER:compose up -d" in dlog(), dlog())
    # rollback restore path
    d = project("g4_restore_version", adopted=True)
    cli = new_cli()
    import builtins
    orig_input = builtins.input
    builtins.input = lambda prompt="": "y"
    try:
        from datetime import datetime
        v = {"timestamp": datetime.now(), "path": os.path.join(d, "v.yml")}
        write(v["path"], "services: {}\n")
        with Fake() as fake, in_dir(d):
            capture(cli._restore_version, d, v, os.path.join(d, "docker-compose.override.yml"))
            check("G4.22 rollback restore uses pull and up through the bridge (adopted)", fake.calls == [("pull", []), ("up", [])], fake.calls)
    finally:
        builtins.input = orig_input
    docker_cfg()


# ==============================================================================
# G5 komodo command, compose env files
# ==============================================================================
def group_komodo_command():
    print("G5 komodo command and environment files")
    docker_cfg()
    d = project("g5_cmd", adopted=True)
    calls = os.path.join(TMP, "k_calls.log")
    open(calls, "w").close()
    fake_setup = os.path.join(TMP, "k-setup.sh")
    for rc_want in (0, 2, 21):
        write(fake_setup, '#!/bin/bash\necho "setup" >> "%s"\nexit %d\n' % (calls, rc_want), 0o755)
        os.environ[CLI.KOMODO_SETUP_ENV] = fake_setup
        rc, out = run_main(["komodo", "setup"])
        check("G5.1 komodo setup runs the script and passes exit code %d through" % rc_want, rc == rc_want, (rc, out))
    os.environ.pop(CLI.KOMODO_SETUP_ENV)
    with Fake() as fake:
        rc, out = run_main(["komodo", "health"])
        check("G5.2 komodo health prints the health line, exit 0", rc == 0 and "KOMODO_HEALTH:" in out and fake.events == ["health:full"], (rc, out, fake.events))
        rc, out = run_main(["komodo", "health", "--quick"])
        check("G5.3 komodo health --quick uses the quick limits", fake.events[-1] == "health:quick")
        rc, out = run_main(["komodo", "bogus"])
        check("G5.4 an unknown komodo subcommand prints usage, exit 1", rc == 1 and "Usage: komodo setup | komodo health" in out, (rc, out))
        rc, out = run_main(["komodo"])
        check("G5.5 komodo without arguments prints usage, exit 0", rc == 0 and "Usage:" in out)
    cli = new_cli()
    comp = CLI.Completer(cli)
    check("G5.6 completion offers setup and health, and env offers check",
          sorted(c.strip() for c in comp.get_matches("komodo ", "")) == ["health", "setup"] and [c.strip() for c in comp.get_matches("env ", "")] == ["check"]
          and "komodo " in comp.get_matches("kom", "kom") and "env " in comp.get_matches("en", "en"))
    help_text = CLI.render_help()
    check("G5.7 help lists komodo and env check", "komodo setup|health" in help_text and "env check" in help_text)
    check("G5.8 help <command> exists for both", "komodo" in cli.help_text and "env" in cli.help_text)

    # --- COMPOSE_ENV_FILES follows .env.komodo
    docker_cfg()
    d = project("g5_env", adopted=True)
    run_main(["status"])
    seen = denv()
    check("G5.9 compose calls get COMPOSE_ENV_FILES=.env,.env.komodo when .env.komodo exists",
          "ENV:.env,.env.komodo:compose ps" in seen, seen[:300])
    docker_cfg()
    d = project("g5_env_none")
    run_main(["status"])
    check("G5.10 without .env.komodo nothing is set (unchanged)", "ENV:unset:compose ps" in denv() and ".env.komodo" not in denv(), denv()[:300])
    if os.geteuid() != 0:
        docker_cfg()
        d = project("g5_env_unreadable", adopted=True)
        os.chmod(os.path.join(d, ".env.komodo"), 0)
        run_main(["status"])
        os.chmod(os.path.join(d, ".env.komodo"), 0o600)
        check("G5.11 an unreadable .env.komodo is not named (current behavior)", "ENV:unset:compose ps" in denv(), denv()[:200])
    check("G5.12 the variable does not leak after the files are gone", os.environ.get("COMPOSE_ENV_FILES") in (None, ".env,.env.komodo"))
    prev = os.getcwd()
    os.chdir(project("g5_env_gone"))
    try:
        os.environ["COMPOSE_ENV_FILES"] = ".env,.env.komodo"
        CLI.compose_env_apply()
        gone = "COMPOSE_ENV_FILES" not in os.environ
    finally:
        os.chdir(prev)
    check("G5.13 compose_env_apply removes its own value when .env.komodo disappears", gone)
    # --- read_env_file_var
    d = project("g5_read", env="A=1\nB=env\nQ=x\n")
    check("G5.14 read_env_file_var without .env.komodo is unchanged", CLI.read_env_file_var(d, "B") == "env" and CLI.read_env_file_var(d, "Z", "dflt") == "dflt")
    write(os.path.join(d, ".env.komodo"), "B=komodo\nC=3\n", 0o600)
    check("G5.15 read_env_file_var: .env.komodo wins, other keys still come from .env",
          CLI.read_env_file_var(d, "B") == "komodo" and CLI.read_env_file_var(d, "A") == "1" and CLI.read_env_file_var(d, "C") == "3")
    docker_cfg()


# ==============================================================================
# G6 env check
# ==============================================================================
def init_heredoc_keys():
    keys = []
    in_here = False
    delim = None
    for line in open(os.path.join(INSTALL, "scripts", "init.sh")).read().split("\n"):
        if not in_here:
            if re.search(r'(cat|tee)[^|;&]*>>?\s*"?\$(ENV_FILE|env_file)"?[^|;&]*<<-?', line):
                delim = re.sub(r"[\"']", "", re.sub(r".*<<-?\s*", "", line))
                in_here = True
            continue
        if line == delim:
            in_here = False
            continue
        m = re.match(r"^([A-Z0-9_]+)=", line)
        if m:
            keys.append(m.group(1))
    return keys


def template_keys():
    return [m.group(1) for l in open(os.path.join(INSTALL, ".env.template")) for m in [re.match(r"^([A-Z_][A-Z0-9_]*)=", l)] if m]


def env_dir(name, env_text, komodo_text=None, template=None):
    d = os.path.join(TMP, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    shutil.copy(os.path.join(INSTALL, ".env.template"), os.path.join(d, ".env.template"))
    if template is not None:
        write(os.path.join(d, ".env.template"), template)
    write(os.path.join(d, ".env"), env_text, 0o600)
    if komodo_text is not None:
        write(os.path.join(d, ".env.komodo"), komodo_text, 0o600)
    return d


def full_env(skip=(), override=None, value="v"):
    tpl = template_keys()
    init = init_heredoc_keys()
    lines = ["%s=%s-%s" % (k, SENTINEL, value) for k in init if k not in skip]
    if override:
        lines = [l for l in lines if l.split("=")[0] not in override] + ["%s=%s" % kv for kv in override.items()]
    return "\n".join(lines) + "\n"


def group_env_check():
    print("G6 env check")
    tpl = template_keys()
    init = init_heredoc_keys()
    exempt = set(CLI.ENV_CHECK_EXEMPT)
    # --- the fixture comes from init.sh itself, so a new template key without an exemption fails here
    d = env_dir("g6_fresh", full_env())
    rc, lines = CLI._env_check(d)
    warn = [l for l in lines if l.startswith(("WARN", "ERROR"))]
    check("G6.1 a fresh install .env (keys taken from init.sh) has no WARN or ERROR, exit 0", rc == 0 and warn == [], (rc, warn))
    check("G6.2 the exemption list is exactly the template keys init.sh does not write",
          set(tpl) - set(init) == exempt and len(CLI.ENV_CHECK_EXEMPT) == 16, (sorted(set(tpl) - set(init) ^ exempt)))
    missing_info = sorted(l.split()[2] for l in lines if l.startswith("INFO MISSING"))
    check("G6.3 every exempt key shows as INFO MISSING", missing_info == sorted(exempt), missing_info)
    check("G6.4 the last line is the machine summary, with matching counts",
          lines[-1] == "VOIPBIN_ENV: status=ok warn=0 error=0 info=%d" % (len(lines) - 1), lines[-1])
    # --- compose-only list is in sync with the compose file
    refs = K.compose_refs(INSTALL)
    check("G6.5 compose references = template keys + the six compose-only names (no others)",
          refs - set(tpl) == set(CLI.ENV_CHECK_COMPOSE_ONLY) and set(CLI.ENV_CHECK_COMPOSE_ONLY) <= refs, sorted(refs - set(tpl) ^ set(CLI.ENV_CHECK_COMPOSE_ONLY)))
    # --- MISSING: not exempt -> WARN, exit 30
    victim = "JWT_KEY"
    d = env_dir("g6_missing", full_env(skip=(victim,)))
    rc, lines = CLI._env_check(d)
    check("G6.6 a missing non-exempt key is WARN MISSING, exit 30, status=warn",
          rc == 30 and "WARN MISSING %s" % victim in lines and lines[-1].startswith("VOIPBIN_ENV: status=warn warn=1 error=0"), (rc, lines[-3:]))
    # --- UNKNOWN is INFO only
    d = env_dir("g6_unknown", full_env() + "MY_OWN_SETTING=1\nASTERISK_CALL_ID=x\nPOD_IP=1.1.1.1\n")
    rc, lines = CLI._env_check(d)
    check("G6.7 a key outside the template is INFO UNKNOWN, the compose-only names are not reported, exit 0",
          rc == 0 and "INFO UNKNOWN MY_OWN_SETTING" in lines and not any("ASTERISK_CALL_ID" in l or "POD_IP" in l for l in lines), lines)
    # --- EMPTY
    for label, env in (("empty", full_env(override={"API_SSL_CERT_BASE64": ""})),
                       ("absent", full_env(skip=("API_SSL_CERT_BASE64",))),
                       ("quoted empty", full_env(override={"API_SSL_CERT_BASE64": '""'}))):
        d = env_dir("g6_empty", env)
        rc, lines = CLI._env_check(d)
        errs = [l for l in lines if l.startswith("ERROR")]
        check("G6.8 API_SSL_CERT_BASE64 %s: exactly one ERROR EMPTY, exit 30, status=error" % label,
              errs == ["ERROR EMPTY API_SSL_CERT_BASE64"] and rc == 30 and "status=error warn=0 error=1" in lines[-1], (rc, lines[-4:]))
    d = env_dir("g6_empty2", full_env(override={"API_SSL_CERT_BASE64": "", "API_SSL_PRIVKEY_BASE64": ""}))
    rc, lines = CLI._env_check(d)
    check("G6.9 both keys empty: two errors", [l for l in lines if l.startswith("ERROR")] == ["ERROR EMPTY API_SSL_CERT_BASE64", "ERROR EMPTY API_SSL_PRIVKEY_BASE64"])
    d = env_dir("g6_hook_empty", full_env(override={"HOOK_SSL_CERT_BASE64": ""}))
    rc, lines = CLI._env_check(d)
    check("G6.10 only the two start.sh keys are errors (an empty hook certificate is not)", rc == 0 and not any(l.startswith("ERROR") for l in lines), lines[-3:])
    # --- last occurrence wins
    d = env_dir("g6_last", full_env() + "API_SSL_CERT_BASE64=\nAPI_SSL_CERT_BASE64=%s-later\n" % SENTINEL)
    rc, lines = CLI._env_check(d)
    check("G6.11 the last occurrence of a key decides (earlier empty, later set: no error)", rc == 0, lines[-3:])
    # --- export prefix
    d = env_dir("g6_export", full_env(skip=(victim,)) + "export %s=abc\n" % victim)
    rc, lines = CLI._env_check(d)
    check("G6.12 `export KEY=` counts as present", rc == 0 and "WARN MISSING %s" % victim not in lines, lines[-3:])
    # --- OVERWRITTEN: fewer than half of the template keys
    half = len(tpl) // 2
    d = env_dir("g6_over", "".join("%s=%s-x\n" % (k, SENTINEL) for k in tpl[:half - 1]))
    rc, lines = CLI._env_check(d)
    check("G6.13 under half of the template keys: ERROR OVERWRITTEN keys=n/m, exit 30",
          rc == 30 and "ERROR OVERWRITTEN keys=%d/%d" % (half - 1, len(tpl)) in lines, lines[:2])
    d = env_dir("g6_notover", "".join("%s=%s-x\n" % (k, SENTINEL) for k in tpl[:len(tpl) - len(tpl) // 2]))
    rc, lines = CLI._env_check(d)
    check("G6.14 exactly half is not an overwrite", not any("OVERWRITTEN" in l for l in lines), lines[:2])
    # --- the half boundary, with a template of four keys
    tiny = "A=1\nB=1\nC=1\nD=1\n"
    d = env_dir("g6_edge_ok", "A=x\nB=x\n", template=tiny)
    rc, lines = CLI._env_check(d)
    check("G6.14b 2 of 4 keys is not an overwrite", not any("OVERWRITTEN" in l for l in lines), lines)
    d = env_dir("g6_edge_bad", "A=x\n", template=tiny)
    rc, lines = CLI._env_check(d)
    check("G6.14c 1 of 4 keys is an overwrite", "ERROR OVERWRITTEN keys=1/4" in lines, lines)
    # --- values are never printed
    d = env_dir("g6_values", full_env(skip=(victim,)) + "MY_OWN_SETTING=%s-leak\n" % SENTINEL)
    rc, lines = CLI._env_check(d)
    check("G6.15 no value appears in the output", SENTINEL not in "\n".join(lines), [l for l in lines if SENTINEL in l])
    # --- unreadable
    d = os.path.join(TMP, "g6_none")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    rc, lines = CLI._env_check(d)
    check("G6.16 no .env: exit 2 and the summary line is last", rc == 2 and lines[-1].startswith("VOIPBIN_ENV: status=error"), (rc, lines))
    d = env_dir("g6_notpl", full_env())
    os.remove(os.path.join(d, ".env.template"))
    rc, lines = CLI._env_check(d)
    check("G6.17 no template: exit 2", rc == 2)
    if os.geteuid() != 0:
        d = env_dir("g6_unr", full_env(), komodo_text="JWT_KEY=x\n")
        os.chmod(os.path.join(d, ".env.komodo"), 0)
        rc, lines = CLI._env_check(d)
        os.chmod(os.path.join(d, ".env.komodo"), 0o600)
        check("G6.18 an unreadable .env.komodo is exit 2", rc == 2, (rc, lines))
    # --- both files are read: moved keys are not MISSING, the later file wins
    moved = ("JWT_KEY", "AMI_PASSWORD")
    d = env_dir("g6_two", full_env(skip=moved), komodo_text="".join("%s=%s-k\n" % (k, SENTINEL) for k in moved))
    rc, lines = CLI._env_check(d)
    check("G6.19 keys that only exist in .env.komodo are not MISSING", rc == 0 and not any("MISSING" in l and l.split()[0] == "WARN" for l in lines), lines[-4:])
    d = env_dir("g6_two_empty", full_env(override={"API_SSL_CERT_BASE64": ""}), komodo_text="API_SSL_CERT_BASE64=%s-from-komodo\n" % SENTINEL)
    rc, lines = CLI._env_check(d)
    check("G6.20 a value in .env.komodo fills an empty .env value", rc == 0, lines[-3:])
    d = env_dir("g6_two_empty2", full_env(), komodo_text="API_SSL_CERT_BASE64=\n")
    rc, lines = CLI._env_check(d)
    check("G6.21 an empty .env.komodo value wins over .env (compose reads the later file)", "ERROR EMPTY API_SSL_CERT_BASE64" in lines, lines[-3:])
    # --- command and exit code mapping
    d = env_dir("g6_cmd", full_env(skip=(victim,)))
    os.environ["VOIPBIN_PROJECT_DIR"] = d
    rc, out = run_main(["env", "check"])
    check("G6.22 `env check` prints the lines and exits 30 on a warning", rc == 30 and "WARN MISSING %s" % victim in out and last_line(out).startswith("VOIPBIN_ENV:"), (rc, out[-200:]))
    d = env_dir("g6_cmd_ok", full_env())
    os.environ["VOIPBIN_PROJECT_DIR"] = d
    rc, out = run_main(["env", "check"])
    check("G6.23 `env check` exits 0 with INFO only", rc == 0, (rc, out[-200:]))
    rc, out = run_main(["env"])
    check("G6.24 `env` without check prints usage, exit 2", rc == 2 and "Usage: env check" in out, (rc, out))
    cli = new_cli()
    cli.last_env_rc = 30
    with in_dir(d):
        capture(cli.cmd_env, ["check"])
    check("G6.25 last_env_rc is reset on entry", cli.last_env_rc == 0)
    # --- doctor: separate section, exit code untouched, skipped without .env
    d = env_dir("g6_doctor", full_env(skip=(victim,)))
    os.environ["VOIPBIN_PROJECT_DIR"] = d
    script(d, "doctor.sh", 'exit 0')
    real_call = CLI.subprocess.call
    CLI.subprocess.call = lambda argv, **kw: (print("DOCTOR-SH-RAN"), 0)[1] if argv[0] == "bash" else real_call(argv, **kw)
    try:
        rc, out = run_main(["doctor"])
    finally:
        CLI.subprocess.call = real_call
    check("G6.26 doctor prints the env section before doctor.sh runs, exit code unchanged (0)",
          rc == 0 and "WARN MISSING %s" % victim in out and "DOCTOR-SH-RAN" in out
          and out.index("VOIPBIN_ENV:") < out.index("DOCTOR-SH-RAN"), (rc, out[-300:]))
    script(d, "doctor.sh", 'exit 1')
    rc, out = run_main(["doctor"])
    check("G6.27 doctor.sh's exit code 1 is not changed by the env section", rc == 1)
    os.remove(os.path.join(d, ".env"))
    script(d, "doctor.sh", 'exit 0')
    rc, out = run_main(["doctor"])
    check("G6.28 without .env the env section is skipped", "VOIPBIN_ENV" not in out and rc == 0, out)


# ==============================================================================
# G7 status: Komodo section
# ==============================================================================
def group_status():
    print("G7 status")
    docker_cfg()
    d = project("g7_none")
    with NoBridge() as fake:
        rc, out = run_main(["status"])
        check("G7.1 none: one yellow line, no komodo setup advice, shown even without containers",
              "Control plane (Komodo)" in out and "Komodo: not adopted (existing installs run compose directly)" in out
              and "komodo setup" not in out and "No services running" in out
              and out.index("Control plane") < out.index("No services running") and fake.calls == [], out)
    docker_cfg()
    d = project("g7_ok", adopted=True)
    with Fake() as fake:
        rc, out = run_main(["status"])
        check("G7.2 adopted and healthy: core, database, periphery, stack all shown, uses the quick check",
              "Komodo  " in out and out.count("●") >= 4 and "○" not in out.split("Control plane")[1].split("No services")[0]
              and fake.events[:1] == ["health_fields:quick"] and "voipbin doctor" not in out.split("No services")[0], (out, fake.events))
        check("G7.3 the section comes before the compose query (still shown with no containers)", out.index("Control plane") < out.index("No services running"))
        for label, patch, want in (("core down", dict(core="down", db="unknown", stack="unknown", server="unknown"), "core (down)"),
                                   ("db timeout", dict(db="timeout"), "database (timeout)"),
                                   ("unit failed", dict(periphery_unit="failed"), "periphery (unit failed)"),
                                   ("server NotOk", dict(server="NotOk"), "periphery (server NotOk)"),
                                   ("stack missing", dict(stack="missing"), "stack (missing)")):
            fake.health = dict(Fake().health, **patch)
            rc, out = run_main(["status"])
            head = out.split("No services")[0]
            check("G7.4 %s: red marker with the reason and the doctor hint" % label, want in head and "voipbin doctor" in head, head)
    d = project("g7_broken")
    write(os.path.join(d, ".env.komodo"), "A=1\n", 0o600)
    with NoBridge() as fake:
        rc, out = run_main(["status"])
        check("G7.5 broken (no marker, Komodo files): red line and the setup advice", "Komodo state is incomplete" in out and "sudo ./voipbin komodo setup" in out and fake.calls == [], out)
    docker_cfg()


# ==============================================================================
# G8 backup
# ==============================================================================
def group_backup():
    print("G8 backup (adopted)")
    docker_cfg(docker_ps="voipbin-db\n")
    d = project("g8_backup", adopted=True, versions_lock=True)
    cli = new_cli()
    with Fake() as fake, in_dir(d):
        out = capture(cli._do_backup, d)
        ts = os.listdir(os.path.join(d, "backups"))[0]
        bdir = os.path.join(d, "backups", ts)
        log = dlog()
        mongo_lines = [l for l in log.splitlines() if "mongodump" in l]
        check("G8.1 adopted backup holds the Komodo archive and both Komodo files (600)",
              os.path.exists(os.path.join(bdir, K_ARCHIVE)) and os.path.getsize(os.path.join(bdir, K_ARCHIVE)) > 0
              and os.path.exists(os.path.join(bdir, "config", ".env.komodo")) and os.path.exists(os.path.join(bdir, "config", "komodo", "cli-key"))
              and stat.S_IMODE(os.stat(os.path.join(bdir, "config", "komodo", "cli-key")).st_mode) == 0o600
              and stat.S_IMODE(os.stat(os.path.join(bdir, K_ARCHIVE)).st_mode) == 0o600, (os.listdir(bdir), out[-300:]))
        check("G8.2 one mongodump call into the Komodo database container, --db komodo, config from stdin",
              len(mongo_lines) == 1 and "voipbin-komodo-mongo" in mongo_lines[0] and "--db komodo" in mongo_lines[0]
              and "--config=/dev/stdin" in mongo_lines[0] and "--archive --gzip" in mongo_lines[0], mongo_lines)
        piped = mongo_lines[0].split("| mongodump", 1)[1]
        check("G8.3 the password only feeds mongodump's stdin (printf, container env); no -p option, no value in any argument",
              "printf 'password: %s" in mongo_lines[0] and "$MONGO_INITDB_ROOT_PASSWORD" in mongo_lines[0].split("| mongodump")[0]
              and "PASSWORD" not in piped and " -p" not in piped and SENTINEL not in log, mongo_lines)
        man = open(os.path.join(bdir, "manifest.json")).read()
        check("G8.4 the manifest sizes include the new files", K_ARCHIVE in man and "config/.env.komodo" in man and "config/komodo/cli-key" in man)
    # failures remove the partial backup
    for label, cfg in (("mongodump fails", dict(docker_fail=r"mongodump")),):
        docker_cfg(docker_ps="voipbin-db\n", **cfg)
        d = project("g8_fail", adopted=True, versions_lock=True)
        cli = new_cli()
        with Fake() as fake, in_dir(d):
            res = [None]
            out = capture(lambda: res.__setitem__(0, cli._do_backup(d)))
            left = os.listdir(os.path.join(d, "backups")) if os.path.exists(os.path.join(d, "backups")) else []
            check("G8.5 %s: no backup is left, None is returned, the message names the Komodo database and how to retry" % label,
                  res[0] is None and left == [] and "Komodo database dump failed" in out and "voipbin doctor" in out, (res, left, out))
    docker_cfg(docker_ps="voipbin-db\n")
    d = project("g8_missing_key", adopted=True, versions_lock=True)
    os.remove(os.path.join(d, "komodo", "cli-key"))
    cli = new_cli()
    with Fake() as fake, in_dir(d):
        res = [None]
        out = capture(lambda: res.__setitem__(0, cli._do_backup(d)))
        left = os.listdir(os.path.join(d, "backups"))
        check("G8.6 a missing komodo/cli-key fails the backup and removes it", res[0] is None and left == [], (res, left, out))
    docker_cfg()


K_ARCHIVE = "komodo-mongo.archive.gz"


# ==============================================================================
# G9 restore
# ==============================================================================
def make_backup(d, komodo, ts="20260101-000000", extra_env="ENV_BACKUP=1\n"):
    b = os.path.join(d, "backups", ts)
    os.makedirs(os.path.join(b, "config", "komodo"))
    with gzip.open(os.path.join(b, "mysql.sql.gz"), "wb") as f:
        f.write(b"SELECT 1;")
    write(os.path.join(b, "manifest.json"), "{}")
    write(os.path.join(b, "config", ".env"), extra_env, 0o600)
    if komodo:
        with gzip.open(os.path.join(b, K_ARCHIVE), "wb") as f:
            f.write(b"mongo-archive")
        write(os.path.join(b, "config", ".env.komodo"), "FROM_BACKUP=%s\n" % SENTINEL, 0o600)
        write(os.path.join(b, "config", "komodo", "cli-key"), "K-backup:%s-bk\n" % SENTINEL, 0o600)
    return ts


def restore_env(name, adopted):
    d = project(name, adopted=adopted, versions_lock=True)
    docker_cfg(docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n",
               docker_ps_all="voipbin-db\nvoipbin-redis\nvoipbin-komodo-core\nvoipbin-komodo-mongo\n")
    return d


def restore(d, ts, fake, cli=None):
    cli = cli or new_cli()
    saved = CLI.KOMODO_CORE_WAIT
    CLI.KOMODO_CORE_WAIT = 1
    try:
        with in_dir(d):
            out = capture(cli.cmd_restore, [ts, "--force"])
    finally:
        CLI.KOMODO_CORE_WAIT = saved
    return cli, out


def group_restore():
    print("G9 restore")
    # --- the four combinations
    for adopted, backup_has in ((False, False), (False, True), (True, False), (True, True)):
        d = restore_env("g9_combo", adopted)
        ts = make_backup(d, backup_has)
        with Fake() as fake:
            before = open(os.path.join(d, ".env")).read()
            cli, out = restore(d, ts, fake)
            log = dlog()
            mysql = "exec -i voipbin-db" in log
            if adopted == backup_has:
                check("G9.1 adopted=%s backup has Komodo=%s: restore runs (exit %d)" % (adopted, backup_has, cli.last_rc),
                      mysql and "Restore complete." in out and cli.last_rc == 0, (cli.last_rc, out[-400:]))
            else:
                check("G9.2 adopted=%s backup has Komodo=%s: refused, exit 1, nothing changed" % (adopted, backup_has),
                      not mysql and cli.last_rc == 1 and "Refusing to restore" in out and "mongorestore" not in log and "docker stop" not in log
                      and open(os.path.join(d, ".env")).read() == before and "DOCKER:cp" not in log, (cli.last_rc, out[-300:], log))
    # --- adopted success: order and effects
    d = restore_env("g9_ok", True)
    ts = make_backup(d, True)
    old_env_komodo = open(os.path.join(d, ".env.komodo")).read()
    old_key = open(os.path.join(d, "komodo", "cli-key")).read()
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        log = dlog().splitlines()
        idx = lambda needle: next((i for i, l in enumerate(log) if needle in l), -1)
        i_stop, i_cp, i_restore, i_start, i_mysql = idx("DOCKER:stop voipbin-komodo-core"), idx("DOCKER:cp"), idx("mongorestore"), idx("DOCKER:start voipbin-komodo-core"), idx("exec -i voipbin-db")
        check("G9.3 order: stop core, copy the archive in, mongorestore, start core, then MySQL",
              -1 < i_stop < i_cp < i_restore < i_start < i_mysql, log)
        check("G9.4 mongorestore: nsInclude komodo.*, --drop, password only through a 600 temp config in the container (nothing in argv)",
              "--nsInclude 'komodo.*'" in log[i_restore] and "--drop" in log[i_restore] and "umask 077" in log[i_restore]
              and "--config=" in log[i_restore] and "$MONGO_INITDB_ROOT_PASSWORD" in log[i_restore] and SENTINEL not in "\n".join(log), log[i_restore])
        check("G9.5 the temporary archive is removed from the container", any("exec voipbin-komodo-mongo rm -f /tmp/komodo-restore.archive.gz" in l for l in log), log)
        check("G9.6 .env.komodo and cli-key come from the backup (600); the previous ones are kept as *.pre-restore",
              open(os.path.join(d, ".env.komodo")).read() == "FROM_BACKUP=%s\n" % SENTINEL
              and open(os.path.join(d, "komodo", "cli-key")).read() == "K-backup:%s-bk\n" % SENTINEL
              and open(os.path.join(d, ".env.komodo.pre-restore")).read() == old_env_komodo
              and open(os.path.join(d, "komodo", "cli-key.pre-restore")).read() == old_key
              and stat.S_IMODE(os.stat(os.path.join(d, ".env.komodo")).st_mode) == 0o600
              and stat.S_IMODE(os.stat(os.path.join(d, "komodo", "cli-key")).st_mode) == 0o600, os.listdir(d))
        check("G9.7 .env is restored, then Komodo converges (core wait, stack update, server, PullStack) as the last step",
              open(os.path.join(d, ".env")).read() == "ENV_BACKUP=1\n"
              and fake.events[-3:] == ["check_core", "ensure_stack", "exec:pull"] and fake.calls == [("pull", [])]
              and out.rindex("Bringing Komodo in line") > out.rindex("Restoring .env") and "Restore complete." in out
              and "1. voipbin start\n" in out and "docker compose" not in out.split("Restore complete.")[1], (fake.events, out[-500:]))
    # --- redis not running: the hint is a voipbin command
    d = restore_env("g9_hint", True)
    ts = make_backup(d, True)
    docker_cfg(docker_ps="voipbin-db\n", docker_ps_stack="voipbin-db\n", docker_ps_all="voipbin-db\nvoipbin-komodo-core\nvoipbin-komodo-mongo\n")
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.8 hints use voipbin start instead of docker compose up", "voipbin start redis && docker exec" in out and "docker compose up" not in out, out)
    docker_cfg(docker_ps="voipbin-redis\n")
    d = restore_env("g9_hint2", True)
    docker_cfg(docker_ps="voipbin-redis\n", docker_ps_stack="voipbin-redis\n")
    ts = make_backup(d, True)
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.9 'database is not running' hint is voipbin start db redis", "  voipbin start db redis" in out and "docker compose" not in out, out)
    # --- failure table
    # (2) fails: nothing touches MySQL, core is started, files are put back
    for label, fail_re, expect_stop in (("core cannot be stopped", r"^stop ", True),
                                        ("archive copy fails", r"^cp ", False),
                                        ("mongorestore fails", r"mongorestore", False)):
        d = restore_env("g9_f2", True)
        ts = make_backup(d, True)
        docker_cfg(docker_fail=fail_re, docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n",
                   docker_ps_all="voipbin-db\nvoipbin-redis\nvoipbin-komodo-core\nvoipbin-komodo-mongo\n")
        old_a = open(os.path.join(d, ".env.komodo")).read()
        old_b = open(os.path.join(d, "komodo", "cli-key")).read()
        old_env = open(os.path.join(d, ".env")).read()
        with Fake() as fake:
            cli, out = restore(d, ts, fake)
            log = dlog()
            check("G9.10 step 2 failure (%s): MySQL untouched, core started, previous files back, exit 1, nothing converged" % label,
                  "exec -i voipbin-db" not in log and "DOCKER:start voipbin-komodo-core" in log and cli.last_rc == 1
                  and open(os.path.join(d, ".env.komodo")).read() == old_a and open(os.path.join(d, "komodo", "cli-key")).read() == old_b
                  and open(os.path.join(d, ".env")).read() == old_env and fake.calls == [] and "Restore complete" not in out, (log, out[-300:]))
    # core start fails after a successful restore: reported, MySQL untouched
    d = restore_env("g9_f2b", True)
    ts = make_backup(d, True)
    docker_cfg(docker_fail=r"^start ", docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n",
               docker_ps_all="voipbin-db\nvoipbin-redis\nvoipbin-komodo-core\nvoipbin-komodo-mongo\n")
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.11 core cannot be started again: said so with the command to run, MySQL untouched",
              "docker start voipbin-komodo-core" in out and "exec -i voipbin-db" not in dlog() and cli.last_rc == 1, out[-300:])
    # (2) ok, (3) fails: partial state like today, no convergence
    d = restore_env("g9_f3", True)
    ts = make_backup(d, True)
    docker_cfg(docker_fail=r"exec -i voipbin-db", docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n",
               docker_ps_all="voipbin-db\nvoipbin-redis\nvoipbin-komodo-core\nvoipbin-komodo-mongo\n")
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.12 MySQL import fails after the Komodo step: today's message, no convergence, Komodo state stays at the backup",
              "MySQL import failed" in out and fake.calls == [] and "check_core" not in fake.events
              and open(os.path.join(d, ".env.komodo")).read() == "FROM_BACKUP=%s\n" % SENTINEL, out[-300:])
    # (4) fails: warning, komodo setup advice, non-zero exit
    for label, setup in (("core does not answer", lambda f: setattr(f, "core_ok", False)),
                         ("the Stack cannot be updated", lambda f: setattr(f, "stack_error", K.KomodoError("http", 500))),
                         ("the server is not Ok", lambda f: setattr(f, "server", "NotOk")),
                         ("PullStack fails", lambda f: setattr(f, "codes", {"pull": 14}))):
        d = restore_env("g9_f4", True)
        ts = make_backup(d, True)
        with Fake() as fake:
            setup(fake)
            cli, out = restore(d, ts, fake)
            check("G9.13 convergence fails (%s): warning with `komodo setup`, exit code non-zero, no 'Restore complete'" % label,
                  cli.last_rc != 0 and "sudo ./voipbin komodo setup" in out and "Restore complete" not in out and "exec -i voipbin-db" in dlog(), (cli.last_rc, out[-300:]))
    # (1) pre-validation
    d = restore_env("g9_pre", True)
    ts = make_backup(d, True)
    with open(os.path.join(d, "backups", ts, K_ARCHIVE), "wb") as f:
        f.write(b"not gzip")
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.14 a corrupt archive is refused before anything changes", cli.last_rc == 1 and "corrupt" in out and "DOCKER:stop" not in dlog() and "exec -i voipbin-db" not in dlog(), out[-300:])
    d = restore_env("g9_pre2", True)
    ts = make_backup(d, True)
    docker_cfg(docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n", docker_ps_all="voipbin-db\nvoipbin-redis\nvoipbin-komodo-core\n")
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.15 a missing Komodo database container is refused up front", cli.last_rc == 1 and "voipbin-komodo-mongo" in out and "DOCKER:stop" not in dlog() and "exec -i voipbin-db" not in dlog(), out[-300:])
    d = restore_env("g9_pre3", True)
    ts = make_backup(d, True)
    os.remove(os.path.join(d, "backups", ts, "config", "komodo", "cli-key"))
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.16 an incomplete Komodo backup (no cli-key) is refused", cli.last_rc == 1 and "incomplete" in out and "DOCKER:stop" not in dlog(), out[-300:])
    d = restore_env("g9_broken", False)
    write(os.path.join(d, ".env.komodo"), "A=1\n", 0o600)
    ts = make_backup(d, False)
    with Fake() as fake:
        cli, out = restore(d, ts, fake)
        check("G9.17 broken install state: refused with the komodo setup advice", cli.last_rc == 1 and "komodo setup" in out and "exec -i voipbin-db" not in dlog(), out[-300:])
    # exit code through main
    d = restore_env("g9_main", True)
    ts = make_backup(d, False)
    with Fake() as fake:
        rc, out = run_main(["restore", ts, "--force"])
        check("G9.18 restore refusal is the process exit code 1", rc == 1, (rc, out))
    d = restore_env("g9_main_none", False)
    ts = make_backup(d, False)
    docker_cfg(docker_fail=r"exec -i voipbin-db", docker_ps="voipbin-db\nvoipbin-redis\n", docker_ps_stack="voipbin-db\nvoipbin-redis\n")
    with NoBridge() as fake:
        rc, out = run_main(["restore", ts, "--force"])
        check("G9.19 none: a failed MySQL import still exits 0 (unchanged)", rc == 0 and "MySQL import failed" in out, (rc, out[-200:]))
    docker_cfg()


# ==============================================================================
# G10 notes for the public repository
# ==============================================================================
def group_hygiene():
    print("G10 source hygiene")
    me = open(os.path.abspath(__file__), encoding="utf-8").read()
    home = "/ho" + "me/"
    ticket = "VOIP" + "-"
    check("G10.1 this test file has no home path, ticket key, em dash or en dash",
          home not in me and ticket not in me and "\u2014" not in me and "\u2013" not in me)
    src = open(os.path.join(SCRIPTS, "voipbin-cli.py"), encoding="utf-8").read()
    check("G10.2 the only Komodo environment variable of the CLI is the test-only setup override",
          re.findall(r'"VOIPBIN_KOMODO[A-Z_]*"', src) == ['"VOIPBIN_KOMODO_SETUP"'], re.findall(r'"VOIPBIN_KOMODO[A-Z_]*"', src))


def run_group(fn):
    """A group that dies on an exception counts as one failure (its other
    checks never ran), so one broken group cannot hide the rest."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        check("%s raised %s: %s" % (fn.__name__, type(e).__name__, str(e)[:200]), False)


def main():
    setup_stubs()
    try:
        for group in (group_install, group_none_lifecycle, group_adopted_lifecycle, group_update_preexec,
                      group_update_resumed, group_verify, group_unpinned, group_komodo_command,
                      group_env_check, group_status, group_backup, group_restore, group_hygiene):
            run_group(group)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED:")
        for name, _ok, detail in failed:
            print("  - " + name)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
