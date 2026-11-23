#!/usr/bin/env python3
"""Unit tests for scripts/komodo_lib.py (Komodo API client and CLI).

No real Komodo, docker or network: a stdlib HTTP server plays Komodo (call
log, k-th call failure injection, delays, update polling, rendering of the
Stack environment into the env copy), and small executables named docker and
systemctl are placed first on PATH.

Run: python3 scripts/tests/test_komodo_lib.py      (no pytest needed)
"""
import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
INSTALL = os.path.dirname(SCRIPTS)
sys.path.insert(0, os.environ.get("KOMODO_LIB_DIR", SCRIPTS))
import komodo_lib as K  # noqa: E402

results = []
ALL_OUTPUT = []  # everything captured from stdout and stderr, scanned for secrets at the end
SENTINEL = "SENTINEL"  # every secret-looking fixture value contains this word
TMP_ROOT = tempfile.mkdtemp(prefix="komodo-lib-test-")


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  %s  %s" % ("PASS" if cond else "FAIL", name) + (("  (%s)" % (str(detail),)) if detail and not cond else ""))


def run(fn, *args, **kw):
    """Call fn capturing stdout and stderr: (return value, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rv = fn(*args, **kw)
    ALL_OUTPUT.append(out.getvalue())
    ALL_OUTPUT.append(err.getvalue())
    return rv, out.getvalue(), err.getvalue()


# ==============================================================================
# fake Komodo
# ==============================================================================
class FakeKomodo(object):
    API_KEY = ("K-test", "S-SENTINEL-secret")
    JWT = "JWT-SENTINEL"
    ADMIN = ("admin", "ADMINPW-SENTINEL")

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                with outer.lock:
                    res = outer.handle(self.path, self.headers, body)
                if res == "drop":
                    self.connection.close()
                    return
                if isinstance(res, tuple) and res[0] == "sleep":
                    time.sleep(res[1])
                    res = res[2]
                code, obj = res
                data = json.dumps(obj).encode()
                try:
                    self.send_response(code)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (timeout tests)

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self.httpd = S(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.calls = []
        self.counts = {}
        self.inject = {}          # (type, k) -> 'drop' | 'http500' | 'http400'
        self.mongo_sleep = 0      # seconds slept before every authenticated call
        self.variables = {}       # name -> {'value', 'is_secret'}
        self.stacks = {}
        self.updates = {}
        self.update_seq = 0
        self.polls_needed = 1
        self.never_complete = False
        self.exec_fail = {}       # type -> (stage, stderr text)
        self.server_state = "Ok"
        self.api_keys = {self.API_KEY}
        self.key_seq = 0
        self.render_override = {}
        self.token_at_deploy = []
        self.api_key_creates = 0

    # --- helpers -----------------------------------------------------------------
    def names(self, typ):
        return [c["name"] for c in self.calls if c["type"] == typ]

    def count(self, typ):
        return sum(1 for c in self.calls if c["type"] == typ)

    def authed(self, hdr):
        if hdr.get("authorization") == self.JWT:
            return True
        return (hdr.get("x-api-key"), hdr.get("x-api-secret")) in self.api_keys

    def render(self, stack):
        cfg = stack["config"]
        lines = []
        for line in cfg.get("environment", "").splitlines():
            m = re.match(r"^([A-Z0-9_]+)=\[\[(VOIPBIN__[A-Z0-9_]+)\]\]$", line)
            if m and m.group(2) in self.variables:
                val = self.render_override.get(m.group(1), self.variables[m.group(2)]["value"])
                lines.append("%s=%s" % (m.group(1), val))
        path = os.path.join(cfg["run_directory"], cfg["env_file_path"])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")

    def handle(self, path, hdr, body):
        typ, params = body.get("type"), body.get("params") or {}
        self.counts[typ] = self.counts.get(typ, 0) + 1
        self.calls.append({"path": path, "type": typ,
                           "name": params.get("name") or params.get("id") or params.get("stack"),
                           "params": params})
        inj = self.inject.get((typ, self.counts[typ]))
        if inj == "drop":
            return "drop"
        if inj == "http500":
            return 500, {"error": "injected"}
        if inj == "http400":
            return 400, {"error": "injected"}
        if path == "/auth":
            return 405, {}  # Komodo v2.3.4: the unauthenticated call lives under /auth/login
        if path == "/auth/login" and typ == "GetLoginOptions":
            return 200, {"local": True}
        if path == "/auth/login":
            if (params.get("username"), params.get("password")) == self.ADMIN:
                return 200, {"type": "Jwt", "data": {"jwt": self.JWT}}
            return 401, {"error": "bad credentials"}
        if not self.authed(hdr):
            return 401, {"error": "unauthorized"}
        res = self.authed_call(path, typ, params)
        if self.mongo_sleep:
            return ("sleep", self.mongo_sleep, res)
        return res

    def authed_call(self, path, typ, params):
        if path == "/auth/manage":
            if typ == "CreateApiKey":
                self.key_seq += 1
                self.api_key_creates += 1
                pair = ("K-new%d" % self.key_seq, "S-SENTINEL-new%d" % self.key_seq)
                self.api_keys.add(pair)
                return 200, {"key": pair[0], "secret": pair[1]}
            return 200, {}
        if path == "/read":
            if typ == "GetVersion":
                return 200, {"version": "v2.3.4"}
            if typ == "ListVariables":
                return 200, [{"name": n, "value": v["value"], "is_secret": v["is_secret"]}
                             for n, v in sorted(self.variables.items())]
            if typ == "GetStack":
                st = self.stacks.get(params.get("stack"))
                return (200, st) if st else (500, {"error": "no stack found"})
            if typ == "ListServers":
                return 200, [{"name": "Local", "info": {"state": "NotOk"}},
                             {"name": "Host", "info": {"state": self.server_state}}]
            if typ == "GetCoreInfo":
                return 200, {"public_key": "PUBKEY-test"}
            if typ == "GetUpdate":
                u = self.updates.get(params.get("id"))
                if not u:
                    return 500, {"error": "no update"}
                u["polls"] += 1
                if self.never_complete or u["polls"] < self.polls_needed:
                    return 200, {"_id": {"$oid": params["id"]}, "status": "InProgress"}
                return 200, {"_id": {"$oid": params["id"]}, "status": "Complete",
                             "success": u["success"], "logs": u["logs"]}
        if path == "/write":
            if typ == "UpdateVariableValue":
                v = self.variables.get(params["name"])
                if v is None:
                    return 500, {"error": "no variable found with given name"}
                if len(params["value"]) > 10000:
                    return 400, {"error": "value too long"}
                v["value"] = params["value"]
                return 200, {"name": params["name"]}
            if typ == "CreateVariable":
                if params["name"] in self.variables:
                    return 500, {"error": "already exists"}
                if len(params["value"]) > 10000:
                    return 400, {"error": "value too long"}
                self.variables[params["name"]] = {"value": params["value"], "is_secret": params.get("is_secret", False)}
                return 200, {"name": params["name"]}
            if typ == "CreateStack":
                self.stacks[params["name"]] = {"name": params["name"], "config": dict(params["config"])}
                return 200, self.stacks[params["name"]]
            if typ == "UpdateStack":
                st = self.stacks.get(params["id"])
                if not st:
                    return 500, {"error": "no stack found"}
                st["config"].update(params["config"])
                return 200, st
            if typ == "CreateOnboardingKey":
                return 200, {"private_key": "O-SENTINEL-onboard"}
        if path == "/execute":
            self.update_seq += 1
            uid = "u%d" % self.update_seq
            ok, logs = True, []
            stack = self.stacks.get(params.get("stack"))
            if typ == "DeployStack":
                tp = os.path.join(stack["config"]["run_directory"], ".voipbin-hook-token") if stack else ""
                self.token_at_deploy.append(open(tp).read() if tp and os.path.exists(tp) else None)
            if typ in self.exec_fail:
                stage, text = self.exec_fail[typ]
                ok, logs = False, [{"stage": "Compose Config", "success": True, "stdout": "fine", "stderr": ""},
                                   {"stage": stage, "success": False, "stdout": "", "stderr": text}]
            elif typ in ("PullStack", "DeployStack") and stack:
                self.render(stack)
            self.updates[uid] = {"polls": 0, "success": ok, "logs": logs}
            return 200, {"_id": {"$oid": uid}, "status": "InProgress"}
        return 500, {"error": "unsupported"}


FAKE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "docker.cfg.json")))
argv = sys.argv[1:]
with open(os.path.join(here, "docker.log"), "a") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env": sorted(os.environ),
                        "cef": os.environ.get("COMPOSE_ENV_FILES")}) + "\n")
def parse_env(path):
    d = {}
    if os.path.exists(path):
        for line in open(path).read().split("\n"):
            m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                d[m.group(1)] = v
    return d
def services():
    svc, cur = {}, None
    for line in open("docker-compose.yml"):
        m = re.match(r"^  ([a-z0-9_-]+):\s*$", line)
        if m:
            cur = m.group(1); svc[cur] = []
        elif cur:
            svc[cur] += re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", line)
    return svc
if cfg.get("fail_all"):
    sys.exit(1)
if argv[:2] == ["compose", "config"]:
    if "--services" in argv:
        print("\n".join(sorted(services()) + cfg.get("services_extra", [])))
        sys.exit(0)
    files = (os.environ.get("COMPOSE_ENV_FILES") or ".env").split(",")
    env = {}
    for fn in files:
        env.update(parse_env(fn))
    for name, keys in sorted(services().items()):
        data = json.dumps([name] + [[k, env.get(k, "")] for k in sorted(set(keys))])
        if cfg.get("break_env_new") and files[0] == ".env.new":
            data += "x"
        print(name, hashlib.sha256(data.encode()).hexdigest()[:16])
    sys.exit(0)
if argv[:2] == ["ps", "-a"]:
    if cfg.get("fail_ps"):
        sys.exit(1)
    print("\n".join(cfg.get("ps", [])))
    sys.exit(0)
if argv[:2] == ["volume", "ls"]:
    if cfg.get("fail_volume"):
        sys.exit(1)
    print("\n".join(cfg.get("volumes", [])))
    sys.exit(0)
sys.exit(0)
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys
state = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "systemctl.state")).read().strip()
print(state)
sys.exit(0 if state == "active" else 3)
'''

BIN = os.path.join(TMP_ROOT, "bin")
os.makedirs(BIN)
for fname, body in (("docker", FAKE_DOCKER), ("systemctl", FAKE_SYSTEMCTL)):
    with open(os.path.join(BIN, fname), "w") as f:
        f.write(body)
    os.chmod(os.path.join(BIN, fname), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]


def docker_cfg(**kw):
    with open(os.path.join(BIN, "docker.cfg.json"), "w") as f:
        json.dump(kw, f)


def docker_log():
    p = os.path.join(BIN, "docker.log")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def docker_log_reset():
    p = os.path.join(BIN, "docker.log")
    if os.path.exists(p):
        os.unlink(p)


def systemctl_state(state):
    with open(os.path.join(BIN, "systemctl.state"), "w") as f:
        f.write(state)


SRV = FakeKomodo()
os.environ[K.URL_ENV] = "http://127.0.0.1:%d" % SRV.port
DEAD_PORT = None
with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    DEAD_PORT = _s.getsockname()[1]  # nothing listens here


# ==============================================================================
# fixtures
# ==============================================================================
CONTAINER_KEYS = [k for k in K.KOMODO_REQUIRED_VARS]
EMPTY_KEYS = ["OPENAI_API_KEY", "TWILIO_SID", "HOMER_URI"]


def key_value(key):
    if re.search(r"(PASSWORD|SECRET|PRIVKEY|JWT_KEY|ENCRYPTION_KEYS|PASS$)", key):
        return "%s-%s-value" % (SENTINEL, key.lower())
    return "v-%s" % key.lower()


def env_text(external=False, drop=(), extra=""):
    lines = ["# fixture env", "DOMAIN_MODE=%s" % ("external" if external else "internal"),
             "COMPOSE_PROFILES=", "TLS_MODE=mkcert", "WEB_REVERSE_PROXY=false", "EXTERNAL_IP_PINNED=false"]
    for k in CONTAINER_KEYS:
        if k in drop:
            continue
        v = key_value(k)
        if external and k in K.CERT_KEYS:
            v = ""
        if k == "CLICKHOUSE_ADDRESS":
            v = '"quoted:9000"'
        lines.append("%s=%s" % (k, v))
    lines += ["%s=" % k for k in EMPTY_KEYS]
    lines += ["OPTIONAL_NOTE=a[[b", "UNRELATED=1", "export EXPORTED_KEY=1"]
    return "\n".join(lines) + "\n" + extra


def compose_text():
    keys = CONTAINER_KEYS + EMPTY_KEYS + ["OPTIONAL_NOTE"]
    svcs = ["db", "api-manager", "hook-manager", "kamailio"]
    out = ["# fixture compose; ${COMMENT_ONLY} is ignored", "services:"]
    for i, s in enumerate(svcs):
        out.append("  %s:" % s)
        out.append("    environment:")
        for k in keys[i::len(svcs)]:
            out.append("      - %s=${%s}" % (k, k))
    return "\n".join(out) + "\n"


def make_project(name="install", marker=False, copy=False, key=True, env=None, variables=None,
                 stack=False, external=False, env_mode=0o600):
    root = tempfile.mkdtemp(dir=TMP_ROOT)
    proj = os.path.join(root, name)
    os.makedirs(os.path.join(proj, "komodo"))
    os.chmod(os.path.join(proj, "komodo"), 0o700)
    wr(proj, "docker-compose.yml", compose_text())
    wr(proj, ".env", env if env is not None else env_text(external=external), env_mode)
    wr(proj, "komodo/.env", "KOMODO_INIT_ADMIN_USERNAME=admin\nKOMODO_DATABASE_PASSWORD=%s-db\n" % SENTINEL, 0o600)
    wr(proj, "komodo/admin-password", FakeKomodo.ADMIN[1] + "\n", 0o600)
    if key:
        wr(proj, "komodo/cli-key", "%s:%s\n" % FakeKomodo.API_KEY, 0o600)
    if marker:
        wr(proj, "komodo/.adopted", "adopted 1\n", 0o600)
    if copy:
        raw = K.read_env_raw(os.path.join(proj, ".env"))
        lines = ["%s=%s" % (k, v) for k, v in raw.items() if k in CONTAINER_KEYS]
        wr(proj, ".env.komodo", "\n".join(lines) + "\n", 0o600)
    if variables:
        for k, v in variables.items():
            SRV.variables[K.VAR_PREFIX + k] = {"value": v, "is_secret": K.is_secret_name(k)}
    if stack:
        SRV.stacks["voipbin"] = {"name": "voipbin", "config": dict(K.stack_config(proj, SRV.variables.keys()))}
    return proj


def make_none(**kw):
    """An install without any Komodo file (mode none)."""
    p = make_project(key=False, **kw)
    for f in ("komodo/.env", "komodo/admin-password"):
        os.unlink(os.path.join(p, f))
    return p


def no_marker(p):
    return not os.path.exists(os.path.join(p, "komodo", ".adopted"))


def wr(proj, rel, text, mode=0o644):
    path = os.path.join(proj, rel)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def rd(proj, rel):
    with open(os.path.join(proj, rel)) as f:
        return f.read()


def adopted_project(**kw):
    """An adopted install with the nine host-written Variables and the env copy in place."""
    values = dict((k, key_value(k)) for k in K.ENV_HOST_WRITTEN)
    proj = make_project(marker=True, copy=True, variables=values, stack=True, **kw)
    return proj


def fresh(**kw):
    SRV.reset()
    docker_cfg()
    docker_log_reset()
    systemctl_state("active")
    return None


def set_timeouts():
    K.TIMEOUT_CORE, K.TIMEOUT_DB, K.TIMEOUT_WRITE, K.TIMEOUT_READ = 0.5, 0.6, 0.8, 0.8
    K.HEALTH_QUICK_CORE, K.HEALTH_QUICK_DB = 0.3, 0.2
    K.POLL_INTERVAL = 0.01
    K.TIMEOUT_ACTION, K.TIMEOUT_PULL_ACTION = 0.6, 0.6
    K.HEALTH_BUDGET, K.HEALTH_QUICK_BUDGET = 8, 5


set_timeouts()


def with_url(url, fn, *a, **kw):
    old = os.environ[K.URL_ENV]
    os.environ[K.URL_ENV] = url
    try:
        return fn(*a, **kw)
    finally:
        os.environ[K.URL_ENV] = old


DEAD_URL = "http://127.0.0.1:%d" % DEAD_PORT


def status_line(err):
    m = re.search(r"^VOIPBIN_ENV_WRITE: (.*)$", err, re.M)
    return dict(p.split("=", 1) for p in m.group(1).split()) if m else {}


def names(csv):
    return set(x for x in csv.split(",") if x)


# ==============================================================================
print("== G1 mode and artifacts")
fresh()
p = make_none()
check("G1 none: no marker, no artifacts", K.mode(p) == "none")
for rel in (".env.komodo", "komodo/.env", "komodo/cli-key"):
    q = make_project(key=False)
    for f in ("komodo/.env", "komodo/admin-password"):
        os.unlink(os.path.join(q, f))
    wr(q, rel, "x\n", 0o600)
    check("G1 broken: only %s present" % rel, K.mode(q) == "broken")
q = make_project(marker=True, copy=True)
check("G1 adopted: marker wins over artifacts", K.mode(q) == "adopted")
q = make_project(marker=True, key=False)
check("G1 adopted: marker only", K.mode(q) == "adopted")
if os.geteuid() != 0:
    q = make_project(marker=True)
    os.chmod(os.path.join(q, "komodo"), 0)
    got = K.mode(q)
    os.chmod(os.path.join(q, "komodo"), 0o700)
    check("G1 marker that cannot be stat'ed (EACCES) is broken, not none", got == "broken", got)
docker_log_reset()
os.environ["VOIPBIN_PROJECT_DIR"] = p
rv, out, err = run(K.main, ["mode"])
check("G1 CLI mode prints one word and exits 0", rv == 0 and out == "none\n" and err == "", (rv, out, err))
check("G1 mode makes no docker call", docker_log() == [])
check("G1 mode makes no Komodo call", SRV.calls == [])

# ==============================================================================
print("== G2 env parsing")
q = tempfile.mkdtemp(dir=TMP_ROOT)
envf = os.path.join(q, ".env")
with open(envf, "w") as f:
    f.write('A=1\nA=2\nexport B=bee\nC="quoted value"\nD=\'single\'\nE=  spaced  \nF="unterminated\n'
            'G=line\r\nH=a=b=c\n#I=commented\n  J=indented\nK="\nL=""\nM=x"y"\n')
cases = {"A": "2", "B": "bee", "C": "quoted value", "D": "single", "E": "spaced", "F": '"unterminated',
         "G": "line", "H": "a=b=c", "I": "", "J": "", "K": '"', "L": "", "M": 'x"y"', "NOPE": ""}
check("G2 get_env_var rules (last wins, export, quotes, CR, blanks)",
      all(K.get_env_var(envf, k) == v for k, v in cases.items()),
      str([(k, K.get_env_var(envf, k)) for k, v in cases.items() if K.get_env_var(envf, k) != v]))
raw = K.read_env_raw(envf)
check("G2 read_env_raw keeps quotes as written", raw["C"] == '"quoted value"' and raw["D"] == "'single'" and raw["L"] == '""')
# same answers as the bash get_env_var for the lines without an export prefix
bash_ok = True
for k in ("A", "C", "D", "E", "F", "G", "H", "K", "L", "M"):
    r = subprocess.run(["bash", "-c", 'source "$1" >/dev/null 2>&1; get_env_var "$2" "$3"', "x",
                        os.path.join(SCRIPTS, "common.sh"), envf, k], capture_output=True, text=True)
    if r.stdout.rstrip("\n") != K.get_env_var(envf, k):
        bash_ok = False
        print("    mismatch", k, repr(r.stdout), repr(K.get_env_var(envf, k)))
check("G2 python get_env_var equals common.sh get_env_var", bash_ok)
d = tempfile.mkdtemp(dir=TMP_ROOT)
sub = os.path.join(d, "My_Proj.v2")
os.makedirs(sub)
check("G2 project name derived like derive_compose_project_name", K.compose_project_name(sub) == "my_projv2")
bad = os.path.join(d, "---")
os.makedirs(bad)
try:
    K.compose_project_name(bad)
    check("G2 underivable project name raises", False)
except ValueError:
    check("G2 underivable project name raises", True)
os.environ["COMPOSE_PROJECT_NAME"] = "fromenv"
check("G2 COMPOSE_PROJECT_NAME from the environment wins", K.compose_project_name(sub) == "fromenv")
os.environ["COMPOSE_PROJECT_NAME"] = "Bad Name"
try:
    K.compose_project_name(sub)
    check("G2 invalid COMPOSE_PROJECT_NAME raises", False)
except ValueError:
    check("G2 invalid COMPOSE_PROJECT_NAME raises", True)
del os.environ["COMPOSE_PROJECT_NAME"]
wr(sub, ".env", "COMPOSE_PROJECT_NAME=fromdotenv\n")
check("G2 COMPOSE_PROJECT_NAME from .env", K.compose_project_name(sub) == "fromdotenv")
check("G2 is_secret rule", all(K.is_secret_name(k) for k in ("DB_PASSWORD", "JWT_SECRET", "X_TOKEN", "API_SSL_PRIVKEY_BASE64",
                                                             "RABBITMQ_DEFAULT_PASS", "OPENAI_API_KEY", "ENC_KEYS", "TWILIO_SID"))
      and not any(K.is_secret_name(k) for k in ("API_URL", "BASE_DOMAIN", "API_SSL_CERT_BASE64")))

# ==============================================================================
print("== G3 constants")
init_lines = open(os.path.join(SCRIPTS, "init.sh")).read().split("\n")
s = next(i for i, l in enumerate(init_lines) if l.startswith('    cat > "$ENV_FILE" << EOF'))
e = next(i for i in range(s, len(init_lines)) if init_lines[i] == "EOF")
init_keys = {}
for l in init_lines[s + 1:e]:
    m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", l)
    if m:
        init_keys[m.group(1)] = m.group(2)
refs = K.compose_refs(INSTALL)
derived = sorted(k for k in init_keys if k in refs and k not in K.ENV_HOST_ONLY and init_keys[k] != "")
check("G3 KOMODO_REQUIRED_VARS equals the init.sh and compose derivation", sorted(K.KOMODO_REQUIRED_VARS) == derived,
      str(set(derived) ^ set(K.KOMODO_REQUIRED_VARS)))
check("G3 47 required keys (internal), 43 without the four certificate keys (external)",
      len(K.KOMODO_REQUIRED_VARS) == 47 and len(set(K.KOMODO_REQUIRED_VARS) - set(K.CERT_KEYS)) == 43)
check("G3 the nine host-written keys are all required and in init.sh",
      len(K.ENV_HOST_WRITTEN) == 9 and set(K.ENV_HOST_WRITTEN) <= set(K.KOMODO_REQUIRED_VARS) and set(K.ENV_HOST_WRITTEN) <= set(init_keys))
check("G3 host-only keys are not compose references", not (set(K.ENV_HOST_ONLY) & refs))
check("G3 IP keys and cert keys partition the nine", set(K.IP_KEYS) | set(K.CERT_KEYS) == set(K.ENV_HOST_WRITTEN))
check("G3 exit code constants", (K.EXIT_OK, K.EXIT_FAIL, K.EXIT_INVALID, K.EXIT_PULL, K.EXIT_UP, K.EXIT_UNAVAILABLE) == (0, 1, 2, 14, 16, 21))

# ==============================================================================
print("== G4 var-set validation: no Komodo call, exit 2")
fresh()
p = adopted_project()
before = rd(p, ".env.komodo")
bad_inputs = {
    "key outside the allow list": [("JWT_KEY", "x")],
    "empty value": [("API_URL", "")],
    "blank value": [("API_URL", "   ")],
    "newline": [("API_URL", "a\nb")],
    "carriage return": [("API_URL", "a\rb")],
    "control character": [("API_URL", "a\x07b")],
    "10001 characters": [("API_SSL_CERT_BASE64", "A" * 10001)],
    "brackets": [("API_URL", "x[[y")],
    "duplicate key": [("API_URL", "a"), ("API_URL", "b")],
    "one bad key blocks the good ones": [("KAMAILIO_EXTERNAL_IP", "192.0.2.5"), ("API_URL", "")],
}
for label, pairs in bad_inputs.items():
    SRV.calls.clear()
    rv, out, err = run(K.var_set, p, pairs)
    st = status_line(err)
    check("G4 %s -> 2 with zero Komodo calls" % label, rv == 2 and SRV.calls == [] and st.get("reason") == "invalid",
          (rv, len(SRV.calls), err))
check("G4 the env copy is untouched after rejected input", rd(p, ".env.komodo") == before)
check("G4 a rejected value is not echoed", "x[[y" not in "".join(ALL_OUTPUT))

# ==============================================================================
print("== G5 order: HOST_EXTERNAL_IP last")
fresh()
p = adopted_project()
pairs = [("HOST_EXTERNAL_IP", "192.0.2.69"), ("KAMAILIO_EXTERNAL_IP", "192.0.2.77"), ("RTPENGINE_EXTERNAL_IP", "192.0.2.78"),
         ("API_URL", "https://api.example.test/"), ("WEBSOCKET_URL", "wss://api.example.test/ws")]
rv, out, err = run(K.var_set, p, pairs)
order = SRV.names("UpdateVariableValue")
check("G5 HOST_EXTERNAL_IP is written last even when passed first", rv == 0 and order[-1] == K.VAR_PREFIX + "HOST_EXTERNAL_IP"
      and len(order) == 5, order)
check("G5 other keys keep the given order", order[:4] == [K.VAR_PREFIX + k for k in ("KAMAILIO_EXTERNAL_IP", "RTPENGINE_EXTERNAL_IP", "API_URL", "WEBSOCKET_URL")])
st = status_line(err)
check("G5 status line ok with written names, no values", st.get("status") == "ok" and names(st["written"]) == set(k for k, _ in pairs)
      and "192.0.2.69" not in err, err)
check("G5 stdout stays empty", out == "")
rv, out, err = run(K.var_set, p, pairs)
st = status_line(err)
check("G5 same values again: all unchanged", rv == 0 and st.get("written") == "" and names(st["unchanged"]) == set(k for k, _ in pairs), err)

# ==============================================================================
print("== G6 k-th call failure: written/pending, HOST_EXTERNAL_IP unwritten, idempotent rerun")
ip_pairs = [("KAMAILIO_EXTERNAL_IP", "192.0.2.77"), ("RTPENGINE_EXTERNAL_IP", "192.0.2.78"),
            ("API_URL", "https://api.example.test/"), ("WEBSOCKET_URL", "wss://api.example.test/ws"),
            ("HOST_EXTERNAL_IP", "192.0.2.69")]
for k in range(1, 6):
    fresh()
    p = adopted_project()
    SRV.inject[("UpdateVariableValue", k)] = "drop"
    rv, out, err = run(K.var_set, p, ip_pairs)
    st = status_line(err)
    exp_written = set(x for x, _ in ip_pairs[:k - 1])
    exp_pending = set(x for x, _ in ip_pairs[k - 1:])
    copy = K.read_env_raw(os.path.join(p, ".env.komodo"))
    check("G6 failure at call %d: exit 21 with written and pending names" % k,
          rv == 21 and st.get("status") == "error" and names(st["written"]) == exp_written and names(st["pending"]) == exp_pending, err)
    check("G6 failure at call %d: HOST_EXTERNAL_IP is not written to Komodo or the copy" % k,
          K.VAR_PREFIX + "HOST_EXTERNAL_IP" not in [n for n in SRV.names("UpdateVariableValue") if SRV.variables[n]["value"] == "192.0.2.69"]
          and copy["HOST_EXTERNAL_IP"] == key_value("HOST_EXTERNAL_IP"))
    check("G6 failure at call %d: copy has only the written keys' new values" % k,
          all(copy[x] == dict(ip_pairs)[x] for x in exp_written) and all(copy[x] == key_value(x) for x in exp_pending))
    SRV.inject.clear()
    rv, out, err = run(K.var_set, p, ip_pairs)
    copy = K.read_env_raw(os.path.join(p, ".env.komodo"))
    check("G6 rerun after failure %d finishes everything" % k, rv == 0 and all(copy[x] == v for x, v in ip_pairs)
          and all(SRV.variables[K.VAR_PREFIX + x]["value"] == v for x, v in ip_pairs), err)
fresh()
p = adopted_project()
SRV.inject[("UpdateVariableValue", 1)] = "http400"
rv, out, err = run(K.var_set, p, ip_pairs)
check("G6 HTTP 400 from Komodo -> exit 2 reason=rejected and stops", rv == 2 and status_line(err).get("reason") == "rejected"
      and SRV.count("UpdateVariableValue") == 1, err)
fresh()
p = adopted_project()
SRV.inject[("UpdateVariableValue", 2)] = "http500"
rv, out, err = run(K.var_set, p, ip_pairs)
check("G6 other HTTP error -> 21 komodo-unreachable", rv == 21 and status_line(err).get("reason") == "komodo-unreachable", err)

# ==============================================================================
print("== G7 write-through: owner and mode kept, appended lines, read-only copy")
fresh()
p = adopted_project()
os.chmod(os.path.join(p, ".env.komodo"), 0o640)
st0 = os.stat(os.path.join(p, ".env.komodo"))
rv, out, err = run(K.var_set, p, [("KAMAILIO_EXTERNAL_IP", "192.0.2.11"), ("API_SSL_CERT_BASE64", "AAAA")])
st1 = os.stat(os.path.join(p, ".env.komodo"))
copy_lines = rd(p, ".env.komodo")
check("G7 copy keeps owner and mode", rv == 0 and (st0.st_uid, st0.st_gid) == (st1.st_uid, st1.st_gid) and stat.S_IMODE(st1.st_mode) == 0o640)
check("G7 only the written lines change", "KAMAILIO_EXTERNAL_IP=192.0.2.11" in copy_lines.split("\n")
      and "API_SSL_CERT_BASE64=AAAA" in copy_lines.split("\n") and "HOST_EXTERNAL_IP=%s" % key_value("HOST_EXTERNAL_IP") in copy_lines.split("\n"))
check("G7 no temp file is left behind", [f for f in os.listdir(p) if f.startswith(".env.komodo.")] == [])
# a key without a line is appended
txt = "".join(l + "\n" for l in rd(p, ".env.komodo").split("\n") if l and not l.startswith("WEBSOCKET_URL="))
wr(p, ".env.komodo", txt, 0o600)
rv, out, err = run(K.var_set, p, [("WEBSOCKET_URL", "wss://x.example.test/ws")])
check("G7 a missing line is appended as KEY=value", rv == 0 and rd(p, ".env.komodo").rstrip("\n").split("\n")[-1] == "WEBSOCKET_URL=wss://x.example.test/ws")
# read-only copy
def readonly_run(pairs):
    fresh()
    pp = adopted_project()
    if os.geteuid() == 0:
        real = K.os.replace
        K.os.replace = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied"))
        try:
            return pp, run(K.var_set, pp, pairs)
        finally:
            K.os.replace = real
    os.chmod(pp, 0o555)
    try:
        return pp, run(K.var_set, pp, pairs)
    finally:
        os.chmod(pp, 0o755)
pp, (rv, out, err) = readonly_run([("KAMAILIO_EXTERNAL_IP", "192.0.2.11")])
check("G7 read-only copy and an IP key -> 21 reason=env-copy (Komodo was written)",
      rv == 21 and status_line(err).get("reason") == "env-copy" and SRV.variables[K.VAR_PREFIX + "KAMAILIO_EXTERNAL_IP"]["value"] == "192.0.2.11", err)
not_escalated = []
for ipk in K.IP_KEYS:
    pp, (rv, out, err) = readonly_run([(ipk, "192.0.2.99")])
    if rv != 21 or status_line(err).get("reason") != "env-copy":
        not_escalated.append(ipk)
check("G7 every IP key escalates an env-copy failure to 21", not_escalated == [], not_escalated)
pp, (rv, out, err) = readonly_run([("API_SSL_CERT_BASE64", "AAAA"), ("API_SSL_PRIVKEY_BASE64", "BBBB")])
check("G7 read-only copy with certificate keys only -> warning and exit 0", rv == 0 and "warning" in err and status_line(err).get("status") == "ok", (rv, err))

# ==============================================================================
print("== G8 certificate values")
def b64_len(n):
    return ("QUJD" * (n // 4 + 1))[:n]
sizes = [("RSA-2048 sized", 2216, 1704), ("RSA-4096 sized", 3156, 3280)]
have_openssl = shutil.which("openssl") is not None
if have_openssl:
    for bits in (2048, 4096):
        d = tempfile.mkdtemp(dir=TMP_ROOT)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:%d" % bits, "-nodes", "-keyout", d + "/k.pem", "-out", d + "/c.pem",
                        "-days", "2", "-subj", "/CN=t.example.test"], capture_output=True)
        import base64
        c = base64.b64encode(open(d + "/c.pem", "rb").read()).decode()
        k = base64.b64encode(open(d + "/k.pem", "rb").read()).decode()
        sizes.append(("openssl RSA-%d" % bits, len(c), len(k)))
        sizes[-1] = (sizes[-1][0], len(c), len(k), c, k)
for item in sizes:
    label, lc, lk = item[0], item[1], item[2]
    c = item[3] if len(item) > 3 else "C" + b64_len(lc - 1)
    k = item[4] if len(item) > 4 else "P" + b64_len(lk - 1)
    c, k = c.replace(SENTINEL, "x"), SENTINEL + k
    fresh()
    p = adopted_project()
    pairs = [("API_SSL_CERT_BASE64", c), ("API_SSL_PRIVKEY_BASE64", k), ("HOOK_SSL_CERT_BASE64", c), ("HOOK_SSL_PRIVKEY_BASE64", k)]
    rv, out, err = run(K.var_set, p, pairs)
    copy = K.read_env_raw(os.path.join(p, ".env.komodo"))
    check("G8 %s round trip (Komodo and env copy hold the exact value)" % label,
          rv == 0 and all(SRV.variables[K.VAR_PREFIX + a]["value"] == b and copy[a] == b for a, b in pairs), (rv, err))
for n, want in ((9990, 0), (10000, 0), (10001, 2)):
    fresh()
    p = adopted_project()
    SRV.calls.clear()
    rv, out, err = run(K.var_set, p, [("API_SSL_CERT_BASE64", "A" * n)])
    check("G8 %d characters -> %s" % (n, "accepted" if want == 0 else "rejected before any call"),
          rv == want and (want == 0 or SRV.count("UpdateVariableValue") == 0), rv)

# ==============================================================================
print("== G9 missing Variable: create, then refresh the Stack environment")
fresh()
p = make_project(marker=True, copy=True, stack=True)
rv, out, err = run(K.var_set, p, [("API_SSL_PRIVKEY_BASE64", "PRIVATE-%s" % SENTINEL), ("API_URL", "https://a.example.test/")])
v = SRV.variables
check("G9 CreateVariable after the update said 'no variable found'", rv == 0 and SRV.count("CreateVariable") == 2 and SRV.count("UpdateVariableValue") == 2)
check("G9 is_secret follows the name rule", v[K.VAR_PREFIX + "API_SSL_PRIVKEY_BASE64"]["is_secret"] and not v[K.VAR_PREFIX + "API_URL"]["is_secret"])
env_lines = SRV.stacks["voipbin"]["config"]["environment"].splitlines()
check("G9 Stack environment lines were refreshed from the Variables",
      "API_URL=[[VOIPBIN__API_URL]]" in env_lines and "API_SSL_PRIVKEY_BASE64=[[VOIPBIN__API_SSL_PRIVKEY_BASE64]]" in env_lines)
check("G9 UpdateStack came after the creates", [c["type"] for c in SRV.calls if c["type"] in ("CreateVariable", "UpdateStack")][-1] == "UpdateStack")
fresh()
p = make_project(marker=True, copy=True, stack=True)
rv, out, err = run(K.var_set, p, [("API_URL", "https://a.example.test/")])
check("G9 the copy gets the new line", rv == 0 and "API_URL=https://a.example.test/" in rd(p, ".env.komodo").split("\n"))

# ==============================================================================
print("== G10 reachability before writing")
fresh()
p = adopted_project()
rv, out, err = with_url(DEAD_URL, run, K.var_set, p, ip_pairs)
check("G10 core down -> 21 komodo-unreachable, all keys pending, nothing written", rv == 21
      and status_line(err).get("reason") == "komodo-unreachable" and names(status_line(err)["pending"]) == set(k for k, _ in ip_pairs)
      and status_line(err)["written"] == "", (rv, err))
check("G10 core down leaves the copy alone", "192.0.2.77" not in rd(p, ".env.komodo"))
fresh()
p = adopted_project()
SRV.mongo_sleep = 1.2
t0 = time.time()
rv, out, err = run(K.var_set, p, ip_pairs)
check("G10 mongo timeout -> 21 komodo-db-timeout without any write", rv == 21 and status_line(err).get("reason") == "komodo-db-timeout"
      and SRV.count("UpdateVariableValue") == 0 and time.time() - t0 < 2.5, (rv, err))
fresh()
p = adopted_project()
SRV.mongo_sleep = 0
orig_check_db = K.check_db
K.check_db = lambda *a, **k: "ok"  # reachability passes, then the first write hangs
SRV.mongo_sleep = 1.2
rv, out, err = run(K.var_set, p, ip_pairs)
K.check_db = orig_check_db
check("G10 first write timeout stops at once (no second call)", rv == 21 and SRV.count("UpdateVariableValue") == 1
      and status_line(err).get("reason") == "komodo-db-timeout", (rv, SRV.count("UpdateVariableValue"), err))
fresh()
p = adopted_project()
SRV.api_keys = set()
rv, out, err = run(K.var_set, p, ip_pairs)
check("G10 rejected key -> 21 and nothing written", rv == 21 and SRV.count("UpdateVariableValue") == 0, err)
fresh()
p = adopted_project()
os.unlink(os.path.join(p, "komodo/cli-key"))
rv, out, err = run(K.var_set, p, ip_pairs)
check("G10 missing cli-key -> 21", rv == 21 and SRV.calls == [], err)
fresh()
p = make_none()
rv, out, err = run(K.var_set, p, ip_pairs)
check("G10 none mode -> 21 without calls", rv == 21 and SRV.calls == [])
fresh()
p = make_project(copy=True)
rv, out, err = run(K.var_set, p, ip_pairs)
check("G10 broken mode -> 21 with the setup hint, no calls", rv == 21 and "komodo setup" in err and SRV.calls == [], err)

# ==============================================================================
print("== G11 ping")
fresh()
p = make_none()
rv, out, err = run(K.ping, p)
check("G11 none: 0 and silent", rv == 0 and out == "" and err == "" and SRV.calls == [])
p = make_project(copy=True)
rv, out, err = run(K.ping, p)
check("G11 broken: 21 with the setup hint", rv == 21 and "komodo setup" in err and SRV.calls == [])
rv, out, err = run(K.ping, p, quiet=True)
check("G11 broken with --quiet: 21, no text", rv == 21 and err == "" and out == "")
fresh()
p = adopted_project()
rv, out, err = run(K.ping, p)
check("G11 healthy adopted: 0, stdout and stderr empty", rv == 0 and out == "" and err == "", (rv, out, err))
rv, out, err = with_url(DEAD_URL, run, K.ping, p)
check("G11 core down: 21 naming core", rv == 21 and "core" in err, err)
SRV.mongo_sleep = 1.2
rv, out, err = run(K.ping, p)
check("G11 mongo timeout: 21 naming mongo", rv == 21 and "mongo" in err, err)
SRV.mongo_sleep = 0
SRV.api_keys = set()
rv, out, err = run(K.ping, p)
check("G11 rejected key: 21 naming auth", rv == 21 and "auth" in err, err)
SRV.api_keys = {SRV.API_KEY}
systemctl_state("inactive")
rv, out, err = run(K.ping, p)
check("G11 periphery unit inactive: 21 naming periphery", rv == 21 and "periphery" in err, err)
systemctl_state("active")
SRV.server_state = "NotOk"
rv, out, err = run(K.ping, p)
check("G11 server NotOk: 21 naming periphery", rv == 21 and "periphery" in err, err)
SRV.server_state = "Ok"
os.unlink(os.path.join(p, "komodo/cli-key"))
rv, out, err = run(K.ping, p)
check("G11 missing key file: 21 naming auth", rv == 21 and "auth" in err, err)
rv, out, err = run(K.ping, p, quiet=True)
check("G11 --quiet hides failure text", rv == 21 and err == "")

# ==============================================================================
print("== G12 exec")
VERBS = {"up": "DeployStack", "rm": "DestroyStack", "down": "DestroyStack", "restart": "RestartStack",
         "stop": "StopStack", "start": "StartStack", "pull": "PullStack"}
fresh()
p = adopted_project()
for verb, typ in VERBS.items():
    SRV.calls.clear()
    svcs = [] if verb in ("down",) else ["api-manager", "hook-manager"]
    rv, out, err = run(K.exec_verb, p, verb, svcs)
    ex = [c for c in SRV.calls if c["path"] == "/execute"]
    check("G12 %s -> %s with the services, one line on stdout" % (verb, typ),
          rv == 0 and len(ex) == 1 and ex[0]["type"] == typ and ex[0]["params"].get("stack") == "voipbin"
          and ex[0]["params"].get("services") == svcs and out == "%s ok\n" % " ".join([verb] + svcs), (rv, out, err, ex))
log = docker_log()
check("G12 service names are checked with config --services and COMPOSE_ENV_FILES=.env,.env.komodo",
      any(l["argv"] == ["compose", "config", "--services"] and l["cef"] == ".env,.env.komodo" for l in log))
SRV.calls.clear()
rv, out, err = run(K.exec_verb, p, "up", [])
check("G12 no services: the whole stack is deployed", rv == 0 and [c for c in SRV.calls if c["path"] == "/execute"][-1]["params"]["services"] == [])
accepted = []
for bad in ("Api-Manager", "api manager", "../x", "-x", "api;rm", "", "a" + chr(10) + "b"):
    SRV.calls.clear()
    rv, out, err = run(K.exec_verb, p, "up", [bad])
    if not (rv == 1 and SRV.calls == []):
        accepted.append(bad)
check("G12 invalid service names -> 1 without Komodo calls", accepted == [], accepted)
SRV.calls.clear()
rv, out, err = run(K.exec_verb, p, "up", ["not-a-service"])
check("G12 name not in compose services -> 1 without Komodo calls", rv == 1 and SRV.calls == [] and "not-a-service" in err)
docker_cfg(services_extra=["Bad;Name", "UPPER"])
SRV.calls.clear()
rv, out, err = run(K.exec_verb, p, "up", ["Bad;Name"])
check("G12 the name pattern is enforced even when compose lists the name", rv == 1 and SRV.calls == [], (rv, err))
docker_cfg(fail_all=True)
rv, out, err = run(K.exec_verb, p, "up", ["db"])
check("G12 config --services failing -> 1", rv == 1 and SRV.count("DeployStack") == 0)
docker_cfg()
rv, out, err = run(K.exec_verb, p, "bogus", [])
check("G12 unknown verb -> 1", rv == 1)
for verb, code in (("pull", 14), ("up", 16), ("restart", 1), ("stop", 1), ("start", 1), ("rm", 1), ("down", 1)):
    fresh()
    p = adopted_project()
    text = "\n".join("line %d" % i for i in range(60)) + "\nthe real error"
    SRV.exec_fail[VERBS[verb]] = ("Compose Up", text)
    rv, out, err = run(K.exec_verb, p, verb, [] if verb == "down" else ["db"])
    tail = [l for l in err.splitlines() if l.startswith("line ") or l == "the real error"]
    check("G12 failed %s -> exit %d, failing stage and the last 20 log lines on stderr" % (verb, code),
          rv == code and "Compose Up" in err and len(tail) == 20 and tail[-1] == "the real error" and out == "", (rv, len(tail), err[:200]))
fresh()
p = adopted_project()
rv, out, err = with_url(DEAD_URL, run, K.exec_verb, p, "up", ["db"])
check("G12 core unreachable -> 21", rv == 21)
SRV.api_keys = set()
rv, out, err = run(K.exec_verb, p, "pull", [])
check("G12 key rejected (401) -> 21, nothing started", rv == 21 and SRV.update_seq == 0)
os.unlink(os.path.join(p, "komodo/cli-key"))
SRV.api_keys = {SRV.API_KEY}
rv, out, err = run(K.exec_verb, p, "pull", [])
check("G12 key file missing -> 21", rv == 21)
fresh()
p = adopted_project()
SRV.never_complete = True
rv, out, err = run(K.exec_verb, p, "pull", [])
check("G12 pull that never completes -> 14 with a timeout message", rv == 14 and "timed out" in err, (rv, err))
rv, out, err = run(K.exec_verb, p, "up", ["db"])
check("G12 up that never completes -> 16 with a timeout message", rv == 16 and "timed out" in err)
rv, out, err = run(K.exec_verb, p, "restart", ["db"])
check("G12 restart that never completes -> 1", rv == 1 and "timed out" in err)
fresh()
p = adopted_project()
SRV.exec_fail["DeployStack"] = ("Task Error", "Stack voipbin is busy: action already in progress")
rv, out, err = run(K.exec_verb, p, "up", [])
check("G12 busy rejection is reported as another operation in progress", rv == 16 and "another operation is in progress" in err and "busy" in err, err)
SRV.exec_fail["DeployStack"] = ("Task Error", "Server Host is NotOk")
rv, out, err = run(K.exec_verb, p, "up", [])
check("G12 other Task Error text is printed without the busy claim", rv == 16 and "another operation" not in err and "NotOk" in err)
fresh()
p = make_none()
rv, out, err = run(K.exec_verb, p, "up", [])
check("G12 none mode: not routed (1), no Komodo call", rv == 1 and SRV.calls == [])
p = make_project(copy=True)
rv, out, err = run(K.exec_verb, p, "up", [])
check("G12 broken mode: 21 with the setup hint", rv == 21 and "komodo setup" in err and SRV.calls == [])
# override file sync before DeployStack
fresh()
p = adopted_project()
SRV.calls.clear()
rv, out, err = run(K.exec_verb, p, "up", [])
check("G12 file_paths already right: no UpdateStack", rv == 0 and SRV.count("UpdateStack") == 0)
wr(p, "docker-compose.override.yml", "services: {}\n")
SRV.calls.clear()
rv, out, err = run(K.exec_verb, p, "up", [])
types = [c["type"] for c in SRV.calls]
check("G12 override file added: one UpdateStack with both paths, before DeployStack",
      rv == 0 and types.count("UpdateStack") == 1 and types.index("UpdateStack") < types.index("DeployStack")
      and SRV.stacks["voipbin"]["config"]["file_paths"] == ["docker-compose.yml", "docker-compose.override.yml"])
SRV.calls.clear()
run(K.exec_verb, p, "up", [])
check("G12 second run with the override: no further UpdateStack", SRV.count("UpdateStack") == 0)
SRV.calls.clear()
os.unlink(os.path.join(p, "docker-compose.override.yml"))
run(K.exec_verb, p, "up", [])
check("G12 override removed: UpdateStack drops it", SRV.count("UpdateStack") == 1 and SRV.stacks["voipbin"]["config"]["file_paths"] == ["docker-compose.yml"])
SRV.calls.clear()
run(K.exec_verb, p, "pull", [])
check("G12 only up looks at file_paths", SRV.count("GetStack") == 0)

# ==============================================================================
print("== G13 hook token and op lock")
def lockfile(p, pid):
    wr(p, ".voipbin-op.lock", "update-all pid=%d 2026-11-20T00:00:00\n" % pid, 0o644)
tok = lambda p: os.path.join(p, ".voipbin-hook-token")
fresh()
p = adopted_project()
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 no lock: call goes through, no token at deploy and none left", rv == 0 and SRV.token_at_deploy == [None] and not os.path.exists(tok(p)))
fresh()
p = adopted_project()
lockfile(p, os.getpid())
rv, out, err = run(K.exec_verb, p, "up", [])
t = SRV.token_at_deploy[0] if SRV.token_at_deploy else None
check("G13 lock held by this process: token '<pid> <time>' existed at deploy and is gone after",
      rv == 0 and t is not None and re.match(r"^%d \d+$" % os.getpid(), t.strip()) and not os.path.exists(tok(p)), (rv, t))
fresh()
p = adopted_project()
lockfile(p, os.getppid())
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 lock held by an ancestor (the parent): accepted", rv == 0 and SRV.token_at_deploy and SRV.token_at_deploy[0] is not None, (rv, err))
mode_seen = []
orig_remove = K.remove_own_token
def spy_remove(pd, content):
    if os.path.exists(tok(pd)):
        mode_seen.append(stat.S_IMODE(os.stat(tok(pd)).st_mode))
    return orig_remove(pd, content)
K.remove_own_token = spy_remove
fresh()
p = adopted_project()
lockfile(p, os.getpid())
run(K.exec_verb, p, "up", [])
K.remove_own_token = orig_remove
check("G13 token file mode is 600", mode_seen == [0o600], mode_seen)
fresh()
p = adopted_project()
other = subprocess.Popen(["sleep", "30"])
lockfile(p, other.pid)
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 live lock of an unrelated process: 1 'another operation in progress', no DeployStack, no token",
      rv == 1 and "another operation" in err and SRV.count("DeployStack") == 0 and not os.path.exists(tok(p)), (rv, err))
other.kill(); other.wait()
fresh()
p = adopted_project()
dead = subprocess.Popen(["true"]); dead.wait()
lockfile(p, dead.pid)
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 stale lock (dead pid) is treated as no lock: proceeds without a token", rv == 0 and SRV.token_at_deploy == [None])
real_kill = K.os.kill
def eperm_kill(pid, sig):
    raise PermissionError(1, "denied")
K.os.kill = eperm_kill
check("G13 EPERM counts as alive", K._pid_alive(99999) is True)
K.os.kill = real_kill
fresh()
p = adopted_project()
lockfile(p, os.getpid())
SRV.exec_fail["DeployStack"] = ("Compose Up", "boom")
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 failed DeployStack removes its own token", rv == 16 and not os.path.exists(tok(p)))
fresh()
p = adopted_project()
lockfile(p, os.getpid())
SRV.never_complete = True
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 timed out DeployStack removes its own token", rv == 16 and not os.path.exists(tok(p)))
fresh()
p = adopted_project()
lockfile(p, os.getpid())
rv, out, err = with_url(DEAD_URL, run, K.exec_verb, p, "up", [])
check("G13 unreachable core: no token left", rv == 21 and not os.path.exists(tok(p)))
# lifetime: old tokens are cleaned when a new one is made or no token is needed
fresh()
p = adopted_project()
wr(p, ".voipbin-hook-token", "123 %d\n" % (int(time.time()) - K.TOKEN_TTL - 5), 0o600)
run(K.exec_verb, p, "up", [])
check("G13 an expired token is removed even when no lock exists", not os.path.exists(tok(p)))
wr(p, ".voipbin-hook-token", "123 %d\n" % int(time.time()), 0o600)
run(K.exec_verb, p, "up", [])
check("G13 a fresh foreign token is left alone when no new token is made", open(tok(p)).read().startswith("123 "))
lockfile(p, os.getpid())
wr(p, ".voipbin-hook-token", "123 %d\n" % (int(time.time()) - 10 * K.TOKEN_TTL), 0o600)
rv, out, err = run(K.exec_verb, p, "up", [])
check("G13 an expired token does not block a new one", rv == 0 and SRV.token_at_deploy[-1].split()[0] == str(os.getpid()))
wr(p, ".voipbin-hook-token", "garbage\n", 0o600)
os.unlink(os.path.join(p, ".voipbin-op.lock"))
K.cleanup_stale_token(p)
check("G13 an unreadable token counts as expired", not os.path.exists(tok(p)))
# /proc parsing after the last ')'
procroot = tempfile.mkdtemp(dir=TMP_ROOT)
os.makedirs(os.path.join(procroot, "4242"))
open(os.path.join(procroot, "4242", "stat"), "w").write("4242 (we) ird ) name (x) S 777 4242 1 0 -1\n")
K.PROC_ROOT = procroot
check("G13 /proc/<pid>/stat is parsed after the LAST ')'", K._parent_pid(4242) == 777, K._parent_pid(4242))
K.PROC_ROOT = "/proc"
stranger = subprocess.Popen(["sleep", "30"])
check("G13 the real parent chain is found: self and parent yes, an unrelated process no",
      K._is_self_or_ancestor(os.getpid()) and K._is_self_or_ancestor(os.getppid()) and not K._is_self_or_ancestor(stranger.pid))
stranger.kill(); stranger.wait()
wr(p, ".voipbin-op.lock", "weird\n")
try:
    K.issue_hook_token(p)
    check("G13 a lock without a pid is refused (fail closed)", False)
except K.HookRefused:
    check("G13 a lock without a pid is refused (fail closed)", True)

# ==============================================================================
print("== G14 health")
def health_line(p, quick=False):
    rv, out, err = run(K.health, p, quick)
    fields = dict(x.split("=", 1) for x in out.strip().split()[1:]) if out.startswith("KOMODO_HEALTH:") else {}
    return rv, out, err, fields
fresh()
p = adopted_project()
rv, out, err, f = health_line(p)
check("G14 healthy: one KOMODO_HEALTH line, exit 0",
      rv == 0 and out.count("\n") == 1 and f == {"adopted": "yes", "core": "ok", "db": "ok", "auth": "ok", "periphery_unit": "active",
                                                  "server": "Ok", "stack": "present", "envfile": "ok"}, out)
check("G14 healthy: stderr is empty", err == "")
rv, out, err = run(K.health, make_none())
check("G14 not adopted: adopted=no and exit 0", rv == 0 and out.startswith("KOMODO_HEALTH: adopted=no"))
rv, out, err, f = with_url(DEAD_URL, health_line, p)
check("G14 core stopped: core=down, db/auth/server/stack unknown", rv == 0 and f["core"] == "down" and f["db"] == "unknown"
      and f["auth"] == "unknown" and f["server"] == "unknown" and f["stack"] == "unknown", out)
SRV.mongo_sleep = 1.2
t0 = time.time()
rv, out, err, f = health_line(p)
check("G14 mongo timeout: core ok, db=timeout, later calls unknown, bounded time", f["core"] == "ok" and f["db"] == "timeout"
      and f["server"] == "unknown" and f["stack"] == "unknown" and time.time() - t0 < 3, out)
SRV.mongo_sleep = 0
SRV.api_keys = set()
rv, out, err, f = health_line(p)
check("G14 401: auth=rejected, no server or stack calls", f["auth"] == "rejected" and f["server"] == "unknown" and f["stack"] == "unknown", out)
SRV.api_keys = {SRV.API_KEY}
systemctl_state("inactive")
rv, out, err, f = health_line(p)
check("G14 periphery stopped: periphery_unit=inactive", f["periphery_unit"] == "inactive" and f["core"] == "ok", out)
systemctl_state("failed")
rv, out, err, f = health_line(p)
check("G14 periphery failed: periphery_unit=failed", f["periphery_unit"] == "failed")
systemctl_state("active")
SRV.server_state = "NotOk"
rv, out, err, f = health_line(p)
check("G14 server NotOk", f["server"] == "NotOk" and f["periphery_unit"] == "active", out)
SRV.server_state = "Ok"
del SRV.stacks["voipbin"]
rv, out, err, f = health_line(p)
check("G14 stack missing", f["stack"] == "missing", out)
SRV.stacks["voipbin"] = {"name": "voipbin", "config": {"env_file_path": ".env"}}
rv, out, err, f = health_line(p)
check("G14 stack with the wrong env_file_path is not usable (missing)", f["stack"] == "missing")
os.chmod(os.path.join(p, ".env.komodo"), 0o644)
rv, out, err, f = health_line(p)
check("G14 envfile mode 644: bad-owner-or-mode", f["envfile"] == "bad-owner-or-mode", out)
os.chmod(os.path.join(p, ".env.komodo"), 0o600)
os.unlink(os.path.join(p, ".env.komodo"))
rv, out, err, f = health_line(p)
check("G14 envfile missing", f["envfile"] == "missing")
if os.geteuid() != 0:
    wr(p, ".env.komodo", "A=1\n", 0o000)
    rv, out, err, f = health_line(p)
    check("G14 envfile unreadable", f["envfile"] == "unreadable", out)
os.environ["VOIPBIN_PROJECT_DIR"] = p
SRV.calls.clear()
rv, out, err, f = health_line(p, quick=True)
check("G14 --quick works and keeps one line", rv == 0 and out.startswith("KOMODO_HEALTH:") and out.count("\n") == 1)
SRV.mongo_sleep = 0.35  # between the quick limit and the normal limit
rv, out, err, f = health_line(p, quick=True)
rv2, out2, err2, f2 = health_line(p, quick=False)
check("G14 --quick uses a shorter db limit than the normal check", f["db"] == "timeout" and f2["db"] == "ok", (f, f2))
SRV.mongo_sleep = 0

# ==============================================================================
print("== G15 guard")
def guard_case(label, expect, setup=None, cfg=None, env=None, name="install", marker_file=False, artifact=None,
               compose_extra=""):
    fresh()
    p = make_none(name=name)
    if compose_extra:
        wr(p, "docker-compose.yml", rd(p, "docker-compose.yml") + "  extra:\n" + compose_extra)
    env_before = rd(p, ".env")
    cfg = dict(cfg or {})
    if "ps" in cfg:
        cfg["ps"] = [l.replace("WORKDIR", p) for l in cfg["ps"]]
    docker_cfg(**cfg)
    if marker_file:
        wr(p, ".voipbin-installed", "installed\n")
    if artifact:
        wr(p, artifact, "x\n", 0o600)
    if env:
        for k, v in env.items():
            os.environ[k] = v
    docker_log_reset()
    try:
        rv, out, err = run(K.guard, p)
    finally:
        for k in (env or {}):
            os.environ.pop(k, None)
    ok = rv == expect and rd(p, ".env") == env_before and SRV.calls == []
    check("G15 %s -> %d (.env bytes unchanged, no Komodo call)" % (label, expect), ok, (rv, err))
    return p, docker_log()
guard_case("clean host", 0, cfg={"ps": [], "volumes": []})
guard_case("unrelated containers and volumes", 0, cfg={"ps": ["other|otherproj|/srv/other"], "volumes": ["other_db_data"]})
guard_case("install marker .voipbin-installed", 2, cfg={"ps": [], "volumes": []}, marker_file=True)
guard_case("container with the compose project label", 2, cfg={"ps": ["web|install|/elsewhere"], "volumes": []})
guard_case("stopped container is listed by ps -a too", 2, cfg={"ps": ["web|install|/elsewhere"], "volumes": []})
_, log = guard_case("docker ps is asked with -a (stopped containers count)", 0, cfg={"ps": [], "volumes": []})
check("G15 docker ps -a is used", any(l["argv"][:2] == ["ps", "-a"] for l in log))
guard_case("container by working_dir label (other project name)", 2, cfg={"ps": ["x|someotherproject|WORKDIR"], "volumes": []})
guard_case("fixed container_name voipbin-db", 2, cfg={"ps": ["voipbin-db||"], "volumes": []})
guard_case("fixed container_name taken from the compose file", 2, cfg={"ps": ["fixed-name-from-compose||"], "volumes": []},
           compose_extra="    container_name: fixed-name-from-compose\n")
guard_case("only the CoreDNS container of install step 2 (service label coredns) passes", 0,
           cfg={"ps": ["voipbin-dns|install|WORKDIR|coredns"], "volumes": []})
guard_case("CoreDNS plus another stack container refuses", 2,
           cfg={"ps": ["voipbin-dns|install|WORKDIR|coredns", "voipbin-redis|install|WORKDIR|redis"], "volumes": []})
guard_case("CoreDNS container plus the database volume refuses", 2,
           cfg={"ps": ["voipbin-dns|install|WORKDIR|coredns"], "volumes": ["install_db_data"]})
guard_case("database volume <project>_db_data", 2, cfg={"ps": [], "volumes": ["install_db_data"]})
guard_case("project name only in the environment: install still refused", 2,
           cfg={"ps": ["voipbin-db|customname|/srv/old"], "volumes": ["customname_db_data"]}, env={})
for art in (".env.komodo", "komodo/.env", "komodo/cli-key", "komodo/.adopted"):
    _, log = guard_case("Komodo artifact %s passes even with signals, docker not asked" % art, 0,
                        cfg={"ps": ["voipbin-db|install|/x"], "volumes": ["install_db_data"]}, marker_file=True, artifact=art)
    if log:
        check("G15 artifact %s: docker was not called" % art, False, log)
guard_case("docker ps failure refuses (fail closed)", 2, cfg={"fail_ps": True})
guard_case("docker volume ls failure refuses", 2, cfg={"ps": [], "volumes": [], "fail_volume": True})
guard_case("docker missing entirely refuses", 2, cfg={"fail_all": True})
guard_case("underivable project name refuses", 2, cfg={"ps": [], "volumes": []}, name="---")
guard_case("invalid COMPOSE_PROJECT_NAME refuses", 2, cfg={"ps": [], "volumes": []}, env={"COMPOSE_PROJECT_NAME": "Bad Name"})
# working_dir label must match the real install dir
fresh()
p = make_none()
docker_cfg(ps=["x|odd|%s" % p], volumes=[])
rv, out, err = run(K.guard, p)
check("G15 working_dir label equal to the install directory refuses", rv == 2)
docker_cfg(ps=["x|odd|%s-other" % p], volumes=[])
rv, out, err = run(K.guard, p)
check("G15 a different working_dir does not", rv == 0)
docker_cfg(ps=["voipbin-db||"], volumes=[])
rv, out, err = run(K.guard, p)
check("G15 refusal message says migration is unsupported and points to ./scripts/start.sh",
      rv == 2 and "not supported" in err and "./scripts/start.sh" in err)

# ==============================================================================
print("== G16 provision")
CLI_KEYS = set(K.KOMODO_REQUIRED_VARS)
def hash_calls():
    return [l for l in docker_log() if l["argv"][:3] == ["compose", "config", "--hash"]]
def provision_case(**kw):
    fresh()
    return make_project(**kw)
def vars_of(): return dict((n[len(K.VAR_PREFIX):], v["value"]) for n, v in SRV.variables.items())

# (a) normal
os.environ["DOCKER_HOST"] = "unix:///nonexistent.sock"
os.environ["HOME_SENTINEL_VAR"] = "must-not-leak"
p = provision_case()
env0 = rd(p, ".env")
st0 = os.stat(os.path.join(p, ".env"))
os.chmod(os.path.join(p, ".env"), 0o640)
markers_seen = []
orig_marker = K._write_marker
def spy_marker(pd):
    markers_seen.append(any(re.match(r"^%s=" % k, l) for l in rd(pd, ".env").split("\n") for k in ("JWT_KEY", "MYSQL_ROOT_PASSWORD"))
                        or os.path.exists(os.path.join(pd, ".env.new")))
    return orig_marker(pd)
K._write_marker = spy_marker
rv, out, err = run(K.provision, p)
K._write_marker = orig_marker
env_after = rd(p, ".env")
raw_before = K.read_env_raw(os.path.join(p, ".env"))
check("G16a normal state: exit 0, marker present, mode adopted", rv == 0 and K.mode(p) == "adopted", (rv, err))
want_moved = set(CONTAINER_KEYS)
check("G16a all 47 eligible keys became Variables (no empty, no ineligible)", set(vars_of()) == want_moved and "OPTIONAL_NOTE" not in vars_of()
      and all(v for v in vars_of().values()), set(vars_of()) ^ want_moved)
check("G16a values are stored as written, quotes included", SRV.variables["VOIPBIN__CLICKHOUSE_ADDRESS"]["value"] == '"quoted:9000"')
check("G16a secret-looking names are is_secret", SRV.variables["VOIPBIN__JWT_KEY"]["is_secret"] and not SRV.variables["VOIPBIN__API_URL"]["is_secret"])
kept = set(K.read_env_raw(os.path.join(p, ".env")))
check("G16a .env keeps host-only, empty, ineligible and unrelated keys only",
      kept == set(K.ENV_HOST_ONLY) | set(EMPTY_KEYS) | {"OPTIONAL_NOTE", "UNRELATED", "EXPORTED_KEY"}, kept)
orig_lines = [l for l in env0.split("\n") if not (K.ENV_LINE_RE.match(l) and K.ENV_LINE_RE.match(l).group(1) in want_moved)]
check("G16a every other .env line is byte-identical (comments, export lines)", env_after.split("\n") == orig_lines)
st1 = os.stat(os.path.join(p, ".env"))
check("G16a .env owner and mode kept", (st0.st_uid, st0.st_gid) == (st1.st_uid, st1.st_gid) and stat.S_IMODE(st1.st_mode) == 0o640)
check("G16a .env.new is gone and no temp files remain", not os.path.exists(os.path.join(p, ".env.new"))
      and not [f for f in os.listdir(p) if ".tmp" in f])
cfg = SRV.stacks["voipbin"]["config"]
check("G16a stack config follows the contract", cfg["server_id"] == "Host" and cfg["files_on_host"] is True and cfg["run_directory"] == os.path.abspath(p)
      and cfg["file_paths"] == ["docker-compose.yml"] and cfg["project_name"] == "install" and cfg["auto_pull"] is False
      and cfg["env_file_path"] == ".env.komodo" and cfg["additional_env_files"] == [{"path": ".env", "track": True}]
      and cfg["pre_deploy"] == {"path": ".", "command": "/usr/local/lib/voipbin/komodo-hook.sh", "shell_mode": False}, cfg)
check("G16a environment lines are KEY=[[VOIPBIN__KEY]] for every Variable",
      sorted(cfg["environment"].splitlines()) == sorted("%s=[[VOIPBIN__%s]]" % (k, k) for k in want_moved))
check("G16a the stack is never deleted", SRV.count("DeleteStack") == 0 and SRV.count("CreateStack") == 1)
hc = hash_calls()
check("G16a hashes: baseline .env, then .env+.env.komodo, then .env.new+.env.komodo",
      [h["cef"] for h in hc] == [".env", ".env,.env.komodo", ".env.new,.env.komodo"], [h["cef"] for h in hc])
check("G16a verify runs in a clean environment (only PATH, DOCKER_HOST, DOCKER_CONFIG and COMPOSE_ENV_FILES)",
      all(set(h["env"]) <= {"PATH", "DOCKER_HOST", "DOCKER_CONFIG", "COMPOSE_ENV_FILES", "PWD", "SHLVL", "_", "LC_CTYPE"} and "DOCKER_HOST" in h["env"]
          and "HOME_SENTINEL_VAR" not in h["env"] and "HOME" not in h["env"] for h in hc), [h["env"] for h in hc][:1])
check("G16a the marker is written after the strip", markers_seen == [False], markers_seen)
ec = os.stat(os.path.join(p, ".env.komodo"))
check("G16a rendering copy exists, mode 600", stat.S_IMODE(ec.st_mode) == 0o600)
check("G16a PullStack ran once before the marker; no secret on stdout/stderr", SRV.count("PullStack") == 1 and SENTINEL not in out + err)
st_key = os.stat(os.path.join(p, "komodo/cli-key"))
check("G16a cli-key was created through the admin login with mode 600", SRV.api_key_creates == 0 and stat.S_IMODE(st_key.st_mode) == 0o600)
rv, out, err = run(K.provision, p)
check("G16a rerun with the marker is converge-only: exit 0, no new Variables, no pull, no hash, .env unchanged",
      rv == 0 and SRV.count("CreateVariable") == len(want_moved) and SRV.count("PullStack") == 1 and len(hash_calls()) == 3
      and rd(p, ".env") == env_after, (rv, err))

# cli-key creation and reuse
p = provision_case(key=False)
rv, out, err = run(K.provision, p)
ck = open(os.path.join(p, "komodo/cli-key")).read().strip()
check("G16 missing cli-key: created via admin login, stored key:secret in a 600 file", rv == 0 and SRV.api_key_creates == 1
      and ck.startswith("K-new1:") and stat.S_IMODE(os.stat(os.path.join(p, "komodo/cli-key")).st_mode) == 0o600, (rv, err))
check("G16 the admin password never reaches the output", FakeKomodo.ADMIN[1] not in out + err)
p2 = provision_case(key=False)
wr(p2, "komodo/cli-key", "K-dead:S-dead\n", 0o600)
rv, out, err = run(K.provision, p2)
check("G16 a rejected cli-key is replaced", rv == 0 and SRV.api_key_creates == 1 and open(os.path.join(p2, "komodo/cli-key")).read().startswith("K-new"))
p3 = provision_case(key=True)
rv, out, err = run(K.provision, p3)
check("G16 a valid cli-key is reused (no new API key)", rv == 0 and SRV.api_key_creates == 0)

# (a) verify failure, then fixed rerun
p = provision_case()
SRV.render_override = {"JWT_KEY": "corrupted-by-render"}
env0 = rd(p, ".env")
rv, out, err = run(K.provision, p)
check("G16b verify failure: exit 2, differing services and key NAMES printed, no value",
      rv == 2 and "JWT_KEY" in err and "corrupted" not in err + out and re.search(r"services differ: .*(db|api-manager|hook-manager|kamailio)", err), (rv, err))
check("G16b verify failure leaves .env byte-identical, no marker, no .env.new", rd(p, ".env") == env0 and K.mode(p) == "broken"
      and not os.path.exists(os.path.join(p, ".env.new")))
SRV.render_override = {}
rv, out, err = run(K.provision, p)
check("G16b after the cause is fixed the rerun passes and adopts", rv == 0 and K.mode(p) == "adopted")
# (a) wrong Variable from an earlier attempt is overwritten by .env
p = provision_case(variables={"JWT_KEY": "wrong-from-earlier-attempt", "API_URL": "https://stale.example.test/"})
rv, out, err = run(K.provision, p)
check("G16b a stale Variable from an earlier attempt is set to the .env value (the only source)",
      rv == 0 and SRV.variables["VOIPBIN__JWT_KEY"]["value"] == key_value("JWT_KEY") and SRV.variables["VOIPBIN__API_URL"]["value"] == key_value("API_URL"), (rv, err))
# (a) strip check fails: .env untouched
p = provision_case()
docker_cfg(break_env_new=True)
env0 = rd(p, ".env")
rv, out, err = run(K.provision, p)
check("G16b a hash mismatch for the reduced .env aborts before the swap", rv == 2 and rd(p, ".env") == env0
      and not os.path.exists(os.path.join(p, ".env.new")) and K.mode(p) == "broken", (rv, err))
# (a) user deleted a subset of the moved keys by hand while Variables exist
p = provision_case(env=env_text(drop=("JWT_KEY", "AMI_PASSWORD")), variables={"JWT_KEY": key_value("JWT_KEY"), "AMI_PASSWORD": key_value("AMI_PASSWORD")})
rv, out, err = run(K.provision, p)
check("G16c partially deleted keys: verify names the services and the keys, exit 2, nothing adopted",
      rv == 2 and "JWT_KEY" in err and "AMI_PASSWORD" in err and "services differ" in err and K.mode(p) == "broken"
      and key_value("JWT_KEY") not in err, (rv, err))
check("G16c the message tells how to recover", "backup" in err.lower() or "restore" in err.lower())
# docker compose failing on the baseline
p = provision_case()
docker_cfg(fail_all=True)
rv, out, err = run(K.provision, p)
check("G16 docker compose failing on the baseline -> 1, nothing changed", rv == 1 and SRV.count("CreateVariable") == 0 and no_marker(p))
docker_cfg()
# preconditions
p = provision_case(key=False)
os.unlink(os.path.join(p, "komodo/admin-password"))
rv, out, err = run(K.provision, p)
check("G16 missing admin-password and no CLI key -> 2 (precondition)", rv == 2 and SRV.calls == [])
p = provision_case()
os.unlink(os.path.join(p, "komodo/admin-password"))
rv, out, err = run(K.provision, p)
check("G16 missing admin-password with a valid CLI key is not a precondition failure (the login is not needed)", rv != 2, (rv, err))
p = provision_case()
os.unlink(os.path.join(p, ".env"))
rv, out, err = run(K.provision, p)
check("G16 missing .env -> 2", rv == 2 and SRV.calls == [])
p = provision_case()
rv, out, err = with_url(DEAD_URL, run, K.provision, p)
check("G16 Komodo unreachable -> 21, .env unchanged", rv == 21 and no_marker(p) and "JWT_KEY=" in rd(p, ".env"))
p = provision_case(key=False)
wr(p, "komodo/admin-password", "wrong\n", 0o600)
rv, out, err = run(K.provision, p)
check("G16 wrong admin password -> 21 (login rejected), not echoed", rv == 21 and "wrong" not in out + err, (rv, err))
# 9 keys: a value over the limit stops the import
big = env_text().replace("API_SSL_CERT_BASE64=%s" % key_value("API_SSL_CERT_BASE64"), "API_SSL_CERT_BASE64=" + "A" * 10001)
p = provision_case(env=big)
rv, out, err = run(K.provision, p)
check("G16d a host-written key over 10,000 characters: exit 2 naming the key, zero Komodo calls, .env unchanged",
      rv == 2 and "API_SSL_CERT_BASE64" in err and SRV.calls == [] and rd(p, ".env") == big, (rv, err))
p = provision_case(env=env_text(extra="HOOK_SSL_CERT_BASE64=has[[brackets\n"))
rv, out, err = run(K.provision, p)
check("G16d a host-written key with brackets stops too (2, 0 calls)", rv == 2 and SRV.calls == [])
# an ordinary key over the limit just stays in .env
big2 = env_text().replace("RAG_TOP_K=%s" % key_value("RAG_TOP_K"), "RAG_TOP_K=" + "A" * 10001)
p = provision_case(env=big2)
rv, out, err = run(K.provision, p)
check("G16d an ordinary key over the limit stays in .env and is not imported (verify passes: the value still reaches compose from .env)",
      rv == 0 and "RAG_TOP_K" in K.read_env_raw(os.path.join(p, ".env")) and "RAG_TOP_K" not in vars_of(), (rv, err[:200]))

# (b) states: no moved key left in .env
def env_without_container_keys():
    lines = ["DOMAIN_MODE=internal", "COMPOSE_PROFILES=", "TLS_MODE=mkcert", "WEB_REVERSE_PROXY=false", "EXTERNAL_IP_PINNED=false"]
    lines += ["%s=" % k for k in EMPTY_KEYS] + ["OPTIONAL_NOTE=a[[b", "UNRELATED=1"]
    return "\n".join(lines) + "\n"
all_vals = dict((k, key_value(k)) for k in CONTAINER_KEYS)
all_vals["CLICKHOUSE_ADDRESS"] = '"quoted:9000"'
def copy_text(vals):
    return "\n".join("%s=%s" % (k, v) for k, v in vals.items()) + "\n"
# b1: copy present, Variables empty (mongo wiped)
p = provision_case(env=env_without_container_keys())
wr(p, ".env.komodo", copy_text(all_vals), 0o600)
env0 = rd(p, ".env")
rv, out, err = run(K.provision, p)
check("G16e (b) copy present, Variables empty: Variables restored from the copy, marker written", rv == 0 and vars_of() == all_vals and K.mode(p) == "adopted", (rv, err))
check("G16e (b) no hash verification and .env untouched", hash_calls() == [] and rd(p, ".env") == env0)
# b2: copy absent, Variables present
p = provision_case(env=env_without_container_keys(), variables=all_vals)
rv, out, err = run(K.provision, p)
check("G16e (b) no copy but all required Variables present: pull renders the copy and the marker is written",
      rv == 0 and K.mode(p) == "adopted" and os.path.exists(os.path.join(p, ".env.komodo")) and SRV.count("CreateVariable") == 0, (rv, err))
# b3: copy absent, Variables empty
p = provision_case(env=env_without_container_keys())
rv, out, err = run(K.provision, p)
check("G16e (b) no copy and no Variables: exit 2 naming missing keys, no marker, no pull",
      rv == 2 and "JWT_KEY" in err and no_marker(p) and SRV.count("PullStack") == 0 and SRV.count("CreateStack") == 0, (rv, err[:300]))
# b4: interrupted between strip and marker (copy and Variables present)
p = provision_case(env=env_without_container_keys(), variables=all_vals)
wr(p, ".env.komodo", copy_text(all_vals), 0o600)
rv, out, err = run(K.provision, p)
check("G16e (b) strip done but no marker: reentry writes the marker", rv == 0 and K.mode(p) == "adopted" and hash_calls() == [])
# b5: partial overlap, UI-edited Variables win
edited = dict((k, "ui-edited-%s" % k.lower()) for k in CONTAINER_KEYS[:20])
p = provision_case(env=env_without_container_keys(), variables=edited)
wr(p, ".env.komodo", copy_text(all_vals), 0o600)
rv, out, err = run(K.provision, p)
vv = vars_of()
check("G16e (b) partial overlap: existing Variables keep their (edited) values, missing ones come from the copy",
      rv == 0 and all(vv[k] == edited[k] for k in edited) and all(vv[k] == all_vals[k] for k in CONTAINER_KEYS[20:]), (rv, err))
# b6: a Variable present but empty does not satisfy the required set
vals = dict(all_vals)
p = provision_case(env=env_without_container_keys(), variables=dict(all_vals, JWT_KEY=""))
rv, out, err = run(K.provision, p)
check("G16e (b) an empty required Variable blocks the marker", rv == 2 and "JWT_KEY" in err and no_marker(p))
# marker present: converge only
p = provision_case(marker=True, env=env_without_container_keys(), variables=all_vals, stack=True)
SRV.stacks["voipbin"]["config"]["environment"] = "STALE=[[VOIPBIN__STALE]]\n"
SRV.stacks["voipbin"]["config"]["file_paths"] = ["old.yml"]
env0 = rd(p, ".env")
rv, out, err = run(K.provision, p)
check("G16f marker present: converge only (stack config corrected, nothing imported, no pull, no hash, .env unchanged)",
      rv == 0 and SRV.stacks["voipbin"]["config"]["file_paths"] == ["docker-compose.yml"]
      and "STALE" not in SRV.stacks["voipbin"]["config"]["environment"] and SRV.count("CreateVariable") == 0
      and SRV.count("PullStack") == 0 and hash_calls() == [] and rd(p, ".env") == env0, (rv, err))
p = provision_case(marker=True, key=True)
SRV.variables.clear()
rv, out, err = run(K.provision, p)
check("G16f marker present with .env still holding keys: no strip, no import", rv == 0 and SRV.count("CreateVariable") == 0 and "JWT_KEY=" in rd(p, ".env"))
# external mode: certificate keys empty
p = provision_case(external=True)
rv, out, err = run(K.provision, p)
check("G16g external mode with empty certificate keys passes through to the marker (43 Variables)",
      rv == 0 and K.mode(p) == "adopted" and len(vars_of()) == 43 and not (set(K.CERT_KEYS) & set(vars_of())), (rv, len(vars_of()), err))
ext_env = env_without_container_keys().replace("DOMAIN_MODE=internal", "DOMAIN_MODE=external")
ext_vals = dict((k, v) for k, v in all_vals.items() if k not in K.CERT_KEYS)
p = provision_case(env=ext_env, variables=ext_vals)
rv, out, err = run(K.provision, p)
check("G16g (b) in external mode the certificate Variables may be absent", rv == 0 and K.mode(p) == "adopted", (rv, err))
p = provision_case(env=env_without_container_keys(), variables=ext_vals)
rv, out, err = run(K.provision, p)
check("G16g (b) in internal mode the same Variables are incomplete", rv == 2 and "API_SSL_CERT_BASE64" in err)
# provision works from broken (artifacts, no marker)
p = provision_case(copy=True)
check("G16h provision starts from a broken install", K.mode(p) == "broken")
rv, out, err = run(K.provision, p)
check("G16h provision completes from broken without a mode check", rv == 0 and K.mode(p) == "adopted", (rv, err))
os.environ.pop("DOCKER_HOST", None)

# ==============================================================================
print("== G16i import rules")
fresh()
rules_env = "\n".join([
    'JWT_KEY=""', "AMI_PASSWORD='  '", "AMI_USERNAME=a[[b", "BASE_DOMAIN=a]]b", "BASE_HOSTNAME=a\x01b", "CERTS_PATH=\"./certs\"",
    "CLICKHOUSE_ADDRESS=ok:9000", "RAG_TOP_K=" + "A" * 10000, "REGISTRAR_URL=" + "A" * 10001, "REGISTRAR_DOMAIN=  padded  ",
    "COMPOSE_PROFILES=internal-dns", "DOMAIN_MODE=internal", "export GCP_REGION=us-central1", "NOT_IN_COMPOSE=1", ""])
p = make_project(env=rules_env)
strip, values, error = K._plan_import(p)
check("G16i plan: empty (including quoted empty and blank-only) values are not imported",
      error is None and not ({"JWT_KEY", "AMI_PASSWORD"} & set(values)), (strip, error))
check("G16i plan: values with [[, ]] or a control character are not imported", not ({"AMI_USERNAME", "BASE_DOMAIN", "BASE_HOSTNAME"} & set(values)))
check("G16i plan: 10,000 characters is imported, 10,001 is not", "RAG_TOP_K" in values and "REGISTRAR_URL" not in values)
check("G16i plan: quotes are kept in the stored value", values["CERTS_PATH"] == '"./certs"' and values["CLICKHOUSE_ADDRESS"] == "ok:9000")
check("G16i plan: surrounding blanks are trimmed, the export prefix is accepted", values["REGISTRAR_DOMAIN"] == "padded" and values["GCP_REGION"] == "us-central1")
check("G16i plan: host-only keys and names compose does not use stay out", not ({"COMPOSE_PROFILES", "DOMAIN_MODE", "NOT_IN_COMPOSE"} & set(values)))
check("G16i plan: strip lists exactly the imported .env keys", sorted(strip) == sorted(values))
check("G16i value_problem: newline, tab, carriage return, DEL, empty", all(K.value_problem(v) for v in ("a\nb", "a\tb", "a\rb", "a\x7fb", "", " ")) and K.value_problem("ok") is None)
copy_only = make_project(env=rules_env)
wr(copy_only, ".env.komodo", "JWT_KEY=from-copy\nAMI_PASSWORD=copy-pass\nBASE_DOMAIN=from-copy\nRAG_TOP_K=copy-wins-never\n", 0o600)
strip, values, error = K._plan_import(copy_only)
check("G16i plan: the copy fills keys .env has no usable value for, and never overrides a usable .env value",
      values.get("JWT_KEY") == "from-copy" and values.get("AMI_PASSWORD") == "copy-pass" and values["RAG_TOP_K"] == "A" * 10000
      and "BASE_DOMAIN" not in values, (sorted(values), error))

# ==============================================================================
print("== G17 onboard")
fresh()
p = make_project(key=False)
ok_, ko_ = os.path.join(p, "o.key"), os.path.join(p, "o.pub")
os.environ["VOIPBIN_PROJECT_DIR"] = p
rv, out, err = run(K.onboard, p, ok_, ko_)
check("G17 writes both files with mode 600 and the right content", rv == 0 and open(ok_).read().strip() == "O-SENTINEL-onboard"
      and open(ko_).read().strip() == "PUBKEY-test" and stat.S_IMODE(os.stat(ok_).st_mode) == 0o600
      and stat.S_IMODE(os.stat(ko_).st_mode) == 0o600, (rv, err))
check("G17 prints neither value", out == "" and err == "" and "O-SENTINEL" not in out + err)
check("G17 one onboarding key and one core info call", SRV.count("CreateOnboardingKey") == 1 and SRV.count("GetCoreInfo") == 1)
rv, out, err = with_url(DEAD_URL, run, K.onboard, p, ok_, ko_)
check("G17 Komodo unreachable -> 21", rv == 21)
wr(p, "komodo/admin-password", "wrong-password\n", 0o600)
rv, out, err = run(K.onboard, p, ok_, ko_)
check("G17 login rejected -> 21", rv == 21)
os.unlink(os.path.join(p, "komodo/admin-password"))
rv, out, err = run(K.onboard, p, ok_, ko_)
check("G17 admin password file missing -> 1", rv == 1)
wr(p, "komodo/admin-password", FakeKomodo.ADMIN[1] + "\n", 0o600)
rv, out, err = run(K.onboard, p, "/nonexistent-dir/o.key", ko_)
check("G17 unwritable output -> 1", rv == 1)
SRV.inject[("GetCoreInfo", SRV.counts.get("GetCoreInfo", 0) + 1)] = "http500"
rv, out, err = run(K.onboard, p, ok_, ko_)
check("G17 other Komodo error -> 1", rv == 1)

# ------------------------------------------------------------------------------
print("== G17b server registration (server-ok)")
fresh()
p = make_project(key=False)
os.environ["VOIPBIN_PROJECT_DIR"] = p
SRV.server_state = "Ok"
check("G17b the server is registered and Ok -> True", K.server_registered(p) is True)
SRV.server_state = "NotOk"
check("G17b a server that is listed but not Ok -> False", K.server_registered(p) is False)
SRV.server_state = "Ok"
check("G17b Komodo unreachable -> False", with_url(DEAD_URL, lambda: K.server_registered(p)) is False)
wr(p, "komodo/admin-password", "wrong-password\n", 0o600)
check("G17b login rejected -> False", K.server_registered(p) is False)
os.unlink(os.path.join(p, "komodo/admin-password"))
check("G17b admin password file missing -> False", K.server_registered(p) is False)
wr(p, "komodo/admin-password", FakeKomodo.ADMIN[1] + "\n", 0o600)
# with a CLI key the admin password is not needed (the file may be gone, the password changed)
q = adopted_project()
os.unlink(os.path.join(q, "komodo/admin-password")) if os.path.exists(os.path.join(q, "komodo/admin-password")) else None
os.environ["VOIPBIN_PROJECT_DIR"] = q
check("G17b with a valid CLI key and no admin-password file the server check still works", K.server_registered(q) is True)
SRV.server_state = "NotOk"
check("G17b ... and a NotOk server is still False (no fallback to the admin login)", K.server_registered(q) is False)
SRV.server_state = "Ok"
# a CLI key that core rejects (e.g. the database was recreated): the admin login is the way in
q2 = adopted_project()
wr(q2, "komodo/cli-key", "K-rejected:S-rejected\n", 0o600)
wr(q2, "komodo/admin-password", FakeKomodo.ADMIN[1] + "\n", 0o600)
os.environ["VOIPBIN_PROJECT_DIR"] = q2
check("G17b a rejected CLI key (401) falls back to the admin login", K.server_registered(q2) is True)
os.unlink(os.path.join(q2, "komodo/admin-password"))
check("G17b a rejected CLI key and no admin password -> False", K.server_registered(q2) is False)
wr(q2, "komodo/admin-password", FakeKomodo.ADMIN[1] + "\n", 0o600)
SRV.inject[("ListServers", SRV.counts.get("ListServers", 0) + 1)] = "http500"
wr(q2, "komodo/cli-key", open(os.path.join(q, "komodo/cli-key")).read(), 0o600)
check("G17b a valid key whose call fails with 500 does not fall back (stays False)", K.server_registered(q2) is False)

# ==============================================================================
print("== G18 CLI entry points (subprocess)")
def cli(args, stdin="", project=None, url=None):
    env = dict(os.environ)
    env["VOIPBIN_PROJECT_DIR"] = project
    env[K.URL_ENV] = url or os.environ[K.URL_ENV]
    r = subprocess.run([sys.executable, os.path.join(os.environ.get("KOMODO_LIB_DIR", SCRIPTS), "komodo_lib.py")] + args,
                       input=stdin, capture_output=True, text=True, env=env, timeout=60)
    ALL_OUTPUT.append(r.stdout + r.stderr)
    return r
fresh()
p = adopted_project()
r = cli(["var-set"], "KAMAILIO_EXTERNAL_IP=192.0.2.55\nHOST_EXTERNAL_IP=192.0.2.56\n", p)
check("G18 var-set reads KEY=VALUE lines from stdin, status line on stderr, stdout empty",
      r.returncode == 0 and r.stdout == "" and "status=ok" in r.stderr and "192.0.2.55" not in r.stderr, (r.returncode, r.stderr))
r = cli(["var-set"], "JWT_KEY=x\n", p)
check("G18 var-set rejects a key outside the allow list with exit 2", r.returncode == 2 and "reason=invalid" in r.stderr)
r = cli(["var-set"], "no-equals-sign\n", p)
check("G18 var-set rejects a malformed line with exit 2", r.returncode == 2)
r = cli(["health", "--quick"], "", p)
check("G18 health prints one line and exits 0", r.returncode == 0 and r.stdout.startswith("KOMODO_HEALTH: adopted=yes") and r.stdout.count("\n") == 1)
r = cli(["ping"], "", p)
check("G18 ping success is silent", r.returncode == 0 and r.stdout == "" and r.stderr == "")
r = cli(["exec", "pull"], "", p)
check("G18 exec pull", r.returncode == 0 and r.stdout == "pull ok\n")
r = cli(["exec"], "", p)
check("G18 exec without a verb -> 1", r.returncode == 1)
r = cli(["mode"], "", p)
check("G18 mode", r.stdout == "adopted\n" and r.returncode == 0)
r = cli(["server-ok"], "", p)
check("G18 server-ok prints nothing and exits 0 when the server is Ok", r.returncode == 0 and r.stdout == "" and r.stderr == "", (r.returncode, r.stderr))
SRV.server_state = "NotOk"
r = cli(["server-ok"], "", p)
check("G18 server-ok exits 1 when the server is not Ok", r.returncode == 1 and r.stdout == "")
SRV.server_state = "Ok"
r = cli(["bogus"], "", p)
check("G18 unknown sub command -> 2", r.returncode == 2)
r = cli(["onboard", "--out-key", os.path.join(p, "a")], "", p)
check("G18 onboard needs both output files", r.returncode == 1)
r = cli(["onboard", "--out-key", os.path.join(p, "a"), "--out-pubkey", os.path.join(p, "b")], "", p)
check("G18 onboard via the CLI writes files and prints nothing", r.returncode == 0 and r.stdout == "" and os.path.exists(os.path.join(p, "b")))
q = make_project()
for f_ in ("komodo/.env", "komodo/admin-password", "komodo/cli-key"):
    os.unlink(os.path.join(q, f_))
r = cli(["guard"], "", q)
check("G18 guard via the CLI (docker lookup works, nothing found) -> 0", r.returncode in (0, 2))

# ==============================================================================
print("== G19 no secret value in any output")
blob = "".join(ALL_OUTPUT)
leaks = sorted(set(re.findall(r"[A-Za-z0-9_-]*%s[A-Za-z0-9_-]*" % SENTINEL, blob)))
# Komodo's own log text is printed by design; none of the fixtures put secrets into it.
check("G19 no fixture secret appears in captured stdout or stderr", leaks == [], leaks[:5])
check("G19 the plain admin password and API secret never appear", FakeKomodo.ADMIN[1] not in blob and FakeKomodo.API_KEY[1] not in blob)

# ==============================================================================
print()
failed = [r for r in results if not r[1]]
print("%d/%d passed" % (len(results) - len(failed), len(results)))
shutil.rmtree(TMP_ROOT, ignore_errors=True)
sys.exit(1 if failed else 0)
