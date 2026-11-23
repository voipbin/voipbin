#!/usr/bin/env python3
"""Komodo API client and CLI for the install scripts (stdlib only).

Importable (`from komodo_lib import ...`) by other Python code and runnable as
`python3 komodo_lib.py <sub> ...`. Every feature is a plain function; `main(argv)`
is only the CLI front end.

Sub commands:
  mode                         none | adopted | broken (file checks only, no docker)
  guard                        new-install guard: exit 0 pass, 2 refuse
  ping [--quiet]               pre-change availability check: exit 0 or 21
  exec <verb> [service...]     up|rm|down|restart|stop|start|pull through Komodo
  var-set                      KEY=VALUE lines on stdin (host-written keys only)
  health [--quick]             one KOMODO_HEALTH line, always exit 0
  provision                    import -> render -> verify -> strip -> marker
  onboard --out-key F --out-pubkey F
                               create a periphery onboarding key and fetch the
                               core public key into two 600 files (no output)

Two layers: low-level API functions (no mode check, used by provision and
onboard) and mode-checking bridges (ping, exec_verb, var_set).

Exit codes: 0 ok, 1 other failure, 2 precondition or verification violation,
14 pull failed, 16 up failed, 21 Komodo unavailable.

Secrets are never written to stdout or stderr: only key names, counts and
Komodo's own (already masked) update logs are printed.
"""
import errno
import http.client
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- exit codes ---------------------------------------------------------------
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INVALID = 2
EXIT_PULL = 14
EXIT_UP = 16
EXIT_UNAVAILABLE = 21

# --- fixed names --------------------------------------------------------------
STACK_NAME = "voipbin"
SERVER_NAME = "Host"
VAR_PREFIX = "VOIPBIN__"
DEFAULT_URL = "http://127.0.0.1:9120"
URL_ENV = "VOIPBIN_KOMODO_URL"  # test only; honored for loopback hosts only
HOOK_COMMAND = "/usr/local/lib/voipbin/komodo-hook.sh"
ENV_COPY = ".env.komodo"

# Keys the host scripts write after init (single write entry point: var-set).
ENV_HOST_WRITTEN = (
    "KAMAILIO_EXTERNAL_IP",
    "RTPENGINE_EXTERNAL_IP",
    "HOST_EXTERNAL_IP",
    "API_URL",
    "WEBSOCKET_URL",
    "API_SSL_CERT_BASE64",
    "API_SSL_PRIVKEY_BASE64",
    "HOOK_SSL_CERT_BASE64",
    "HOOK_SSL_PRIVKEY_BASE64",
)
IP_KEYS = (
    "KAMAILIO_EXTERNAL_IP",
    "RTPENGINE_EXTERNAL_IP",
    "API_URL",
    "WEBSOCKET_URL",
    "HOST_EXTERNAL_IP",
)
CERT_KEYS = (
    "API_SSL_CERT_BASE64",
    "API_SSL_PRIVKEY_BASE64",
    "HOOK_SSL_CERT_BASE64",
    "HOOK_SSL_PRIVKEY_BASE64",
)
LAST_KEY = "HOST_EXTERNAL_IP"  # commit marker of an IP change, always written last
# Keys only the host reads (compose never references them).
ENV_HOST_ONLY = (
    "COMPOSE_PROFILES",
    "DOMAIN_MODE",
    "EXTERNAL_IP_PINNED",
    "TLS_MODE",
    "WEB_REVERSE_PROXY",
)

# Container keys that init.sh always writes non-empty:
# (keys init.sh writes into .env) & (compose "${KEY" references)
#   - the five host-only keys - keys whose literal value in init.sh is empty.
# 47 keys; the four certificate keys may be empty in external mode (see
# required_vars_missing). A unit test pins this tuple to init.sh and compose.
KOMODO_REQUIRED_VARS = (
    "AI_CONFIG_ENCRYPTION_KEYS",
    "AMI_PASSWORD",
    "AMI_USERNAME",
    "API_PUBLIC_BASE_URL",
    "API_SSL_CERT_BASE64",
    "API_SSL_PRIVKEY_BASE64",
    "API_URL",
    "BASE_DOMAIN",
    "BASE_HOSTNAME",
    "CERTS_PATH",
    "CLICKHOUSE_ADDRESS",
    "CLICKHOUSE_DATABASE",
    "CONFERENCE_DOMAIN",
    "CONFERENCE_URL",
    "DATABASE_ASTERISK_PASSWORD",
    "DATABASE_ASTERISK_USERNAME",
    "DOMAIN_NAME_EXTENSION",
    "DOMAIN_NAME_TRUNK",
    "DOMAIN_SHORT_LABEL_ENABLED",
    "EMAIL_CONFIG_ENCRYPTION_KEYS",
    "EMAIL_VERIFY_BASE_URL",
    "ENGINE_BASE_KEY_ENCRYPTION_KEYS",
    "GCP_BUCKET_NAME_MEDIA",
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HOOK_SSL_CERT_BASE64",
    "HOOK_SSL_PRIVKEY_BASE64",
    "HOST_EXTERNAL_IP",
    "JWT_KEY",
    "KAMAILIO_EXTERNAL_IP",
    "MCP_SECRET_ENCRYPTION_KEYS",
    "MESSAGE_CONFIG_ENCRYPTION_KEYS",
    "MYSQL_ROOT_PASSWORD",
    "NUMBER_SECRET_ENCRYPTION_KEYS",
    "PIPECAT_CONFIG_ENCRYPTION_KEYS",
    "POSTGRES_PASSWORD",
    "RABBITMQ_DEFAULT_PASS",
    "RABBITMQ_DEFAULT_USER",
    "RAG_TOP_K",
    "REGISTRAR_DOMAIN",
    "REGISTRAR_URL",
    "RTPENGINE_EXTERNAL_IP",
    "SQUARE_BIND_ADDR",
    "TRANSCRIBE_CONFIG_ENCRYPTION_KEYS",
    "TTS_CONFIG_ENCRYPTION_KEYS",
    "WEBSOCKET_URL",
)

MAX_VALUE_LEN = 10000  # Komodo's Variable value limit

# --- time limits (seconds, module constants) ------------------------------------
TIMEOUT_CORE = 3
TIMEOUT_DB = 5
TIMEOUT_WRITE = 10
TIMEOUT_READ = 10
TIMEOUT_AUTH = 15
TIMEOUT_DOCKER = 120
TIMEOUT_PULL_ACTION = 1800
TIMEOUT_ACTION = 600
POLL_INTERVAL = 2
HEALTH_BUDGET = 8
HEALTH_QUICK_CORE = 2
HEALTH_QUICK_DB = 3
HEALTH_QUICK_BUDGET = 5
TOKEN_TTL = 60  # hook token lifetime

PROC_ROOT = "/proc"
LOG_TAIL_LINES = 20

VERB_ACTIONS = {
    "up": "DeployStack",
    "rm": "DestroyStack",
    "down": "DestroyStack",
    "restart": "RestartStack",
    "stop": "StopStack",
    "start": "StartStack",
    "pull": "PullStack",
}
VERB_FAIL_CODE = {"pull": EXIT_PULL, "up": EXIT_UP}
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SECRET_NAME_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|PRIVKEY|PASS$|_KEY$|KEYS$|SID$)")
ENV_LINE_RE = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
BUSY_RE = re.compile(r"(already|in progress|busy)", re.I)


# ==============================================================================
# paths and modes
# ==============================================================================
def default_project_dir():
    env = os.environ.get("VOIPBIN_PROJECT_DIR")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(project_dir, *parts):
    return os.path.join(project_dir, *parts)


def marker_path(project_dir):
    return _p(project_dir, "komodo", ".adopted")


def _err(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")


def _lstat_state(path):
    """'yes' | 'no' | 'error' (cannot tell, e.g. permission denied)."""
    try:
        os.lstat(path)
        return "yes"
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return "no"
        return "error"


def artifacts_present(project_dir):
    """File signals only (no docker): rendering copy, komodo/.env, komodo/cli-key."""
    for rel in (ENV_COPY, os.path.join("komodo", ".env"), os.path.join("komodo", "cli-key")):
        if _lstat_state(_p(project_dir, rel)) != "no":
            return True
    return False


def mode(project_dir):
    """adopted (marker) | broken (no marker but artifacts, or marker unreadable) | none."""
    state = _lstat_state(marker_path(project_dir))
    if state == "yes":
        return "adopted"
    if state == "error":
        return "broken"
    return "broken" if artifacts_present(project_dir) else "none"


# ==============================================================================
# env files
# ==============================================================================
def _unquote(value):
    """Same rules as common.sh get_env_var after the key is matched."""
    value = value.rstrip("\r")
    value = value.strip(" \t\n\v\f")
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        value = value[1:-1]
    return value


def read_env_raw(path):
    """Ordered {key: raw value}: quotes kept, CR and surrounding blanks stripped,
    last occurrence wins, `export` prefix accepted. Missing file -> {}."""
    out = {}
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "surrogateescape")
    except OSError:
        return out
    for line in text.split("\n"):
        m = ENV_LINE_RE.match(line)
        if m:
            out.pop(m.group(1), None)
            out[m.group(1)] = m.group(2).rstrip("\r").strip(" \t")
    return out


def get_env_var(path, key):
    """Python twin of common.sh get_env_var (export prefix accepted)."""
    raw = read_env_raw(path)
    if key not in raw:
        return ""
    return _unquote(raw[key])


def compose_refs(project_dir):
    """Names referenced as ${NAME in the live compose file (comments excluded)."""
    refs = set()
    for name in ("docker-compose.yml", "docker-compose.yml.dist"):
        path = _p(project_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip().startswith("#"):
                    continue
                refs |= set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", line.split(" #")[0]))
        break
    return refs


def compose_project_name(project_dir):
    """COMPOSE_PROJECT_NAME (environment, then .env) else the directory name,
    derived like common.sh derive_compose_project_name. ValueError on failure."""
    name = os.environ.get("COMPOSE_PROJECT_NAME") or get_env_var(_p(project_dir, ".env"), "COMPOSE_PROJECT_NAME")
    if name:
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
            raise ValueError("invalid COMPOSE_PROJECT_NAME")
        return name
    base = os.path.basename(os.path.abspath(project_dir)).lower()
    derived = re.sub(r"[^a-z0-9_-]", "", base).lstrip("-_")
    if not derived:
        raise ValueError("cannot derive a compose project name")
    return derived


def is_secret_name(key):
    return bool(SECRET_NAME_RE.search(key))


def value_problem(value):
    """None when `value` may be stored as a Variable, else a short reason."""
    if value == "" or value.strip() == "":
        return "empty"
    if len(value) > MAX_VALUE_LEN:
        return "too-long"
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return "control-char"
    if "[[" in value or "]]" in value:
        return "brackets"
    return None


def _install_owner(project_dir):
    st = os.stat(project_dir)
    return st.st_uid, st.st_gid


def _adjust_owner(project_dir, path):
    """Give `path` to the install directory owner (only root can; others already match)."""
    try:
        uid, gid = _install_owner(project_dir)
        if os.geteuid() == 0:
            os.chown(path, uid, gid, follow_symlinks=False)
    except OSError:
        pass


def _write_atomic(path, data, mode_bits=0o600, owner_from=None):
    """Temp file + rename. owner_from: copy uid/gid/mode from this stat result."""
    d = os.path.dirname(path)
    tmp = os.path.join(d, "." + os.path.basename(path) + ".tmp%d" % os.getpid())
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if owner_from is not None:
            os.chmod(tmp, stat.S_IMODE(owner_from.st_mode))
            try:
                os.chown(tmp, owner_from.st_uid, owner_from.st_gid, follow_symlinks=False)
            except OSError:
                pass
        else:
            os.chmod(tmp, mode_bits)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ==============================================================================
# HTTP client
# ==============================================================================
class KomodoError(Exception):
    """kind: unreachable | timeout | http | proto. The body is kept for
    decisions but is never part of str(), so it cannot leak into output."""

    def __init__(self, kind, code=None, body=""):
        Exception.__init__(self, "%s%s" % (kind, " %s" % code if code else ""))
        self.kind = kind
        self.code = code
        self.body = body


class Unavailable(Exception):
    """Komodo cannot be used (no key, unreadable key, ...)."""


def base_url():
    url = os.environ.get(URL_ENV, "")
    if url:
        m = re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/?$", url)
        if m:
            return url.rstrip("/")
    return DEFAULT_URL


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(path, typ, params, headers, timeout):
    data = json.dumps({"type": typ, "params": params or {}}).encode("utf-8")
    hdrs = {"content-type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base_url() + path, data, hdrs, method="POST")
    try:
        # No proxies: the API is on loopback and must never go through one.
        with _OPENER.open(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            text = e.read()[:4000].decode("utf-8", "replace")
        except Exception:
            text = ""
        raise KomodoError("http", e.code, text)
    except (socket.timeout, TimeoutError):
        raise KomodoError("timeout")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise KomodoError("timeout")
        raise KomodoError("unreachable")
    except (OSError, http.client.HTTPException):
        raise KomodoError("unreachable")
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise KomodoError("proto")


class Client(object):
    def __init__(self, headers=None):
        self.headers = headers or {}

    # Limits are looked up at call time so the module constants stay adjustable.
    def read(self, typ, params=None, timeout=None):
        return _post("/read", typ, params, self.headers, TIMEOUT_READ if timeout is None else timeout)

    def write(self, typ, params=None, timeout=None):
        return _post("/write", typ, params, self.headers, TIMEOUT_WRITE if timeout is None else timeout)

    def execute(self, typ, params=None, timeout=None):
        return _post("/execute", typ, params, self.headers, TIMEOUT_WRITE if timeout is None else timeout)

    def auth_manage(self, typ, params=None, timeout=None):
        return _post("/auth/manage", typ, params, self.headers, TIMEOUT_AUTH if timeout is None else timeout)


def login_options(timeout):
    """Unauthenticated liveness call."""
    return _post("/auth", "GetLoginOptions", {}, None, timeout)


def login(username, password):
    """Local user login -> Client with the JWT. KomodoError on failure."""
    r = _post("/auth/login", "LoginLocalUser", {"username": username, "password": password}, None, TIMEOUT_AUTH)
    jwt = None
    if isinstance(r, dict):
        data = r.get("data")
        if isinstance(data, dict):
            jwt = data.get("jwt")
        jwt = jwt or r.get("jwt")
    if not jwt:
        raise KomodoError("proto")
    return Client({"authorization": jwt})


def load_cli_key(project_dir):
    """(key, secret) from komodo/cli-key (`key:secret`). Unavailable when missing or unreadable."""
    path = _p(project_dir, "komodo", "cli-key")
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
    except OSError:
        raise Unavailable("cannot read komodo/cli-key (run as the install owner or root)")
    key, sep, secret = raw.partition(":")
    if not sep or not key or not secret:
        raise Unavailable("komodo/cli-key is malformed")
    return key, secret


def key_client(project_dir):
    key, secret = load_cli_key(project_dir)
    return Client({"x-api-key": key, "x-api-secret": secret})


def check_core(timeout=None):
    try:
        login_options(TIMEOUT_CORE if timeout is None else timeout)
        return True
    except KomodoError:
        return False


def check_db(client, timeout=None):
    """'ok' | 'timeout' | 'rejected' | 'down'."""
    try:
        client.read("GetVersion", {}, TIMEOUT_DB if timeout is None else timeout)
        return "ok"
    except KomodoError as e:
        if e.kind == "timeout":
            return "timeout"
        if e.kind == "http" and e.code in (401, 403):
            return "rejected"
        return "down"


def list_variables(client):
    """{name: {'value', 'is_secret'}} for the VOIPBIN__ prefix."""
    r = client.read("ListVariables", {}) or []
    out = {}
    if isinstance(r, dict):
        r = r.get("variables", [])
    for v in r:
        name = v.get("name", "")
        if name.startswith(VAR_PREFIX):
            out[name] = {"value": v.get("value", ""), "is_secret": bool(v.get("is_secret"))}
    return out


def environment_text(var_names):
    keys = sorted(n[len(VAR_PREFIX):] for n in var_names if n.startswith(VAR_PREFIX))
    return "".join("%s=[[%s%s]]\n" % (k, VAR_PREFIX, k) for k in keys)


def desired_file_paths(project_dir):
    paths = ["docker-compose.yml"]
    if os.path.isfile(_p(project_dir, "docker-compose.override.yml")):
        paths.append("docker-compose.override.yml")
    return paths


def stack_config(project_dir, var_names):
    return {
        "server_id": SERVER_NAME,
        "files_on_host": True,
        "run_directory": os.path.abspath(project_dir),
        "file_paths": desired_file_paths(project_dir),
        "project_name": compose_project_name(project_dir),
        "auto_pull": False,
        "environment": environment_text(var_names),
        "env_file_path": ENV_COPY,
        "additional_env_files": [{"path": ".env", "track": True}],
        "pre_deploy": {"path": ".", "command": HOOK_COMMAND, "shell_mode": False},
    }


def get_stack(client):
    """Stack dict or None when it does not exist (HTTP error other than auth)."""
    try:
        return client.read("GetStack", {"stack": STACK_NAME})
    except KomodoError as e:
        if e.kind == "http" and e.code not in (401, 403):
            return None
        raise


def ensure_stack(client, project_dir):
    """Create the Stack or bring its config in line (never deletes)."""
    cfg = stack_config(project_dir, list_variables(client).keys())
    if get_stack(client) is None:
        client.write("CreateStack", {"name": STACK_NAME, "config": cfg})
    else:
        client.write("UpdateStack", {"id": STACK_NAME, "config": cfg})


def refresh_stack_environment(client):
    names = list_variables(client).keys()
    client.write("UpdateStack", {"id": STACK_NAME, "config": {"environment": environment_text(names)}})


# ==============================================================================
# running Komodo actions
# ==============================================================================
class ActionResult(object):
    def __init__(self):
        self.ok = False
        self.timed_out = False
        self.api_error = None
        self.stages = []
        self.tail = []
        self.busy = False


def _update_id(update):
    try:
        return update["_id"]["$oid"]
    except (KeyError, TypeError):
        return None


def run_action(client, typ, params, timeout):
    """Execute, then poll GetUpdate every POLL_INTERVAL until Complete or `timeout`."""
    res = ActionResult()
    try:
        update = client.execute(typ, params)
        uid = _update_id(update)
        if not uid:
            res.api_error = KomodoError("proto")
            return res
        deadline = time.monotonic() + timeout
        while True:
            update = client.read("GetUpdate", {"id": uid})
            if isinstance(update, dict) and update.get("status") == "Complete":
                break
            if time.monotonic() >= deadline:
                res.timed_out = True
                return res
            time.sleep(POLL_INTERVAL)
    except KomodoError as e:
        res.api_error = e
        return res
    res.ok = bool(update.get("success"))
    if not res.ok:
        logs = update.get("logs") or []
        failed = [l for l in logs if not l.get("success", True)] or logs
        res.stages = [str(l.get("stage", "")) for l in failed]
        lines = []
        for l in failed:
            for fld in ("stderr", "stdout"):
                lines.extend((l.get(fld) or "").splitlines())
        res.tail = lines[-LOG_TAIL_LINES:]
        if any(s in ("Task Error", "Spawn Error") for s in res.stages):
            res.busy = bool(BUSY_RE.search("\n".join(lines)))
    return res


# ==============================================================================
# docker helpers
# ==============================================================================
def _docker(args, cwd=None, env=None):
    """(rc, stdout, stderr); rc 127 when docker is missing, 124 on timeout."""
    try:
        r = subprocess.run(["docker"] + list(args), cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=TIMEOUT_DOCKER)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "docker not found"
    except subprocess.TimeoutExpired:
        return 124, "", "docker timed out"
    except OSError as e:
        return 126, "", str(e)


def compose_env_files(project_dir):
    files = [".env"]
    if os.path.exists(_p(project_dir, ENV_COPY)):
        files.append(ENV_COPY)
    return files


def compose_services(project_dir):
    env = dict(os.environ)
    env["COMPOSE_ENV_FILES"] = ",".join(compose_env_files(project_dir))
    rc, out, _ = _docker(["compose", "config", "--services"], cwd=project_dir, env=env)
    if rc != 0:
        return None
    return [s.strip() for s in out.splitlines() if s.strip()]


def compose_hashes(project_dir, env_files):
    """{service: hash} from `docker compose config --hash "*"` in a clean
    environment (host exports would override --env-file values)."""
    env = {"PATH": os.environ.get("PATH", ""), "COMPOSE_ENV_FILES": ",".join(env_files)}
    for k in ("DOCKER_HOST", "DOCKER_CONFIG"):
        if k in os.environ:
            env[k] = os.environ[k]
    rc, out, _ = _docker(["compose", "config", "--hash", "*"], cwd=project_dir, env=env)
    if rc != 0:
        return None
    hashes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            hashes[parts[0]] = parts[1]
    return hashes


# ==============================================================================
# guard
# ==============================================================================
GUARD_REFUSED = (
    "Migrating an existing install to Komodo is not supported in this release. "
    "Existing installs keep running compose directly.\n"
    "If an install started before Komodo support was interrupted after step 4, "
    "continue it with ./scripts/start.sh."
)


def _fixed_container_names(project_dir):
    names = {"voipbin-db"}
    for fn in ("docker-compose.yml", "docker-compose.yml.dist"):
        path = _p(project_dir, fn)
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^\s*container_name:\s*([A-Za-z0-9_.-]+)", line)
                    if m:
                        names.add(m.group(1))
            break
    return names


def guard(project_dir):
    """New-install guard. 0 pass, 2 refuse (nothing is changed either way).
    Komodo artifacts mean a resumed Komodo install: pass without asking docker.
    Lookup failures refuse (fail closed)."""
    if mode(project_dir) != "none":
        return EXIT_OK
    signals = []
    if os.path.exists(_p(project_dir, ".voipbin-installed")):
        signals.append("install marker")
    try:
        project = compose_project_name(project_dir)
    except ValueError:
        _err("Install guard: cannot determine the compose project name; refusing.")
        return EXIT_INVALID
    rc, out, _ = _docker(["ps", "-a", "--format",
                          '{{.Names}}|{{.Label "com.docker.compose.project"}}|'
                          '{{.Label "com.docker.compose.project.working_dir"}}'])
    if rc != 0:
        _err("Install guard: cannot list containers; refusing.")
        return EXIT_INVALID
    fixed = _fixed_container_names(project_dir)
    here = os.path.realpath(project_dir)
    for line in out.splitlines():
        cname, _, rest = line.partition("|")
        label, _, wdir = rest.partition("|")
        if (label == project or cname in fixed
                or (wdir and os.path.realpath(wdir) == here)):
            signals.append("container")
            break
    rc, out, _ = _docker(["volume", "ls", "-q"])
    if rc != 0:
        _err("Install guard: cannot list volumes; refusing.")
        return EXIT_INVALID
    if (project + "_db_data") in out.split():
        signals.append("database volume")
    if signals:
        _err(GUARD_REFUSED)
        return EXIT_INVALID
    return EXIT_OK


# ==============================================================================
# hook token (op lock handshake)
# ==============================================================================
class HookRefused(Exception):
    pass


def lock_path(project_dir):
    return _p(project_dir, ".voipbin-op.lock")


def token_path(project_dir):
    return _p(project_dir, ".voipbin-hook-token")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def _parent_pid(pid):
    """Parent pid from /proc/<pid>/stat, parsed after the LAST ')' (the command
    name may contain parentheses and spaces)."""
    try:
        with open(os.path.join(PROC_ROOT, str(pid), "stat"), "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    rest = data[data.rfind(")") + 1:].split()
    try:
        return int(rest[1])
    except (IndexError, ValueError):
        return None


def _is_self_or_ancestor(pid):
    cur = os.getpid()
    seen = set()
    while cur and cur not in seen:
        if cur == pid:
            return True
        seen.add(cur)
        cur = _parent_pid(cur)
    return False


def _read_token(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return None


def cleanup_stale_token(project_dir, now=None):
    path = token_path(project_dir)
    content = _read_token(path)
    if content is None:
        return
    now = time.time() if now is None else now
    parts = content.split()
    try:
        age = now - float(parts[1])
    except (IndexError, ValueError):
        age = TOKEN_TTL + 1
    if age > TOKEN_TTL:
        try:
            os.unlink(path)
        except OSError:
            pass


def issue_hook_token(project_dir):
    """Token content (str) when one was written, None when no token is needed
    (no lock, or a stale lock). HookRefused when a live lock belongs to
    someone else (neither this process nor an ancestor)."""
    try:
        with open(lock_path(project_dir), "r") as f:
            lock = f.read()
    except OSError:
        cleanup_stale_token(project_dir)
        return None
    m = re.search(r"\bpid=(\d+)", lock)
    if not m:
        raise HookRefused("another operation is in progress (unreadable lock)")
    pid = int(m.group(1))
    if not _pid_alive(pid):
        cleanup_stale_token(project_dir)
        return None
    if not _is_self_or_ancestor(pid):
        raise HookRefused("another operation is in progress")
    cleanup_stale_token(project_dir)
    content = "%d %d\n" % (pid, int(time.time()))
    _write_atomic(token_path(project_dir), content.encode("ascii"), 0o600)
    _adjust_owner(project_dir, token_path(project_dir))
    return content


def remove_own_token(project_dir, content):
    if content is None:
        return
    path = token_path(project_dir)
    if _read_token(path) == content:
        try:
            os.unlink(path)
        except OSError:
            pass


# ==============================================================================
# bridges: ping, exec, var-set
# ==============================================================================
BROKEN_HINT = "Komodo state is incomplete. Run: sudo ./voipbin komodo setup"


def _periphery_unit():
    try:
        r = subprocess.run(["systemctl", "is-active", "periphery"], capture_output=True, text=True, timeout=5)
        word = (r.stdout.strip().splitlines() or [""])[0]
    except (OSError, subprocess.TimeoutExpired):
        return "missing"
    if word in ("active", "inactive", "failed"):
        return word
    if word in ("activating", "reloading", "deactivating"):
        return "inactive"
    return "missing"


def _server_state(client, timeout):
    """'Ok' | 'NotOk' | 'unknown' for the server named SERVER_NAME."""
    try:
        servers = client.read("ListServers", {}, timeout)
    except KomodoError:
        return "unknown"
    for s in servers or []:
        if s.get("name") == SERVER_NAME:
            state = (s.get("info") or {}).get("state")
            return "Ok" if state == "Ok" else "NotOk"
    return "NotOk"


def server_registered(project_dir):
    """True when the server named SERVER_NAME is registered and its state is Ok.
    Uses the admin login (the CLI key does not exist yet when periphery is set up)."""
    try:
        admin = admin_login(project_dir)
    except (Unavailable, KomodoError):
        return False
    return _server_state(admin, TIMEOUT_READ) == "Ok"


def ping(project_dir, quiet=False):
    m = mode(project_dir)
    if m == "none":
        return EXIT_OK
    say = (lambda s: None) if quiet else _err
    if m == "broken":
        say(BROKEN_HINT)
        return EXIT_UNAVAILABLE
    failed = []
    try:
        client = key_client(project_dir)
    except Unavailable as e:
        failed.append("auth")
        client = None
        say(str(e))
    if not check_core():
        failed.append("core")
    elif client is not None:
        db = check_db(client)
        if db == "timeout" or db == "down":
            failed.append("mongo")
        elif db == "rejected":
            failed.append("auth")
        else:
            if _periphery_unit() != "active":
                failed.append("periphery")
            elif _server_state(client, TIMEOUT_READ) != "Ok":
                failed.append("periphery")
    if failed:
        say("Komodo is not available (%s). Check with: voipbin doctor" % ", ".join(failed))
        return EXIT_UNAVAILABLE
    return EXIT_OK


def _action_params(verb, services):
    p = {"stack": STACK_NAME}
    if verb in ("up", "rm", "restart", "stop", "start", "pull", "down"):
        p["services"] = list(services)
    return p


def _report_failure(verb, res):
    if res.timed_out:
        _err("%s timed out; the Komodo task may still be running" % verb)
        return
    if res.stages:
        _err("%s failed at stage: %s" % (verb, ", ".join(res.stages)))
    else:
        _err("%s failed" % verb)
    if res.busy:
        _err("another operation is in progress")
    for line in res.tail:
        _err(line)


def exec_verb(project_dir, verb, services):
    services = list(services or [])
    if verb not in VERB_ACTIONS:
        _err("unknown verb: %s" % verb)
        return EXIT_FAIL
    m = mode(project_dir)
    if m == "broken":
        _err(BROKEN_HINT)
        return EXIT_UNAVAILABLE
    if m == "none":
        _err("Komodo is not set up on this install; run compose directly")
        return EXIT_FAIL
    for s in services:
        if not SERVICE_RE.match(s):
            _err("invalid service name")
            return EXIT_FAIL
    if services:
        known = compose_services(project_dir)
        if known is None:
            _err("cannot list compose services")
            return EXIT_FAIL
        unknown = [s for s in services if s not in known]
        if unknown:
            _err("unknown service: %s" % ", ".join(unknown))
            return EXIT_FAIL
    try:
        client = key_client(project_dir)
    except Unavailable as e:
        _err(str(e))
        return EXIT_UNAVAILABLE
    if not check_core():
        _err("Komodo core is not reachable")
        return EXIT_UNAVAILABLE
    fail_code = VERB_FAIL_CODE.get(verb, EXIT_FAIL)
    token = None
    try:
        if verb == "up":
            try:
                stack = get_stack(client)
                if stack is not None:
                    have = (stack.get("config") or {}).get("file_paths")
                    want = desired_file_paths(project_dir)
                    if have != want:
                        client.write("UpdateStack", {"id": STACK_NAME, "config": {"file_paths": want}})
            except KomodoError as e:
                _err("Komodo call failed (%s)" % e)
                return EXIT_UNAVAILABLE
            try:
                token = issue_hook_token(project_dir)
            except HookRefused as e:
                _err(str(e))
                return EXIT_FAIL
            except OSError:
                _err("cannot write the hook token")
                return EXIT_FAIL
        limit = TIMEOUT_PULL_ACTION if verb == "pull" else TIMEOUT_ACTION
        res = run_action(client, VERB_ACTIONS[verb], _action_params(verb, services), limit)
    finally:
        remove_own_token(project_dir, token)
    if res.api_error is not None:
        e = res.api_error
        if e.kind in ("unreachable", "timeout") or (e.kind == "http" and e.code in (401, 403)):
            _err("Komodo call failed (%s)" % e)
            return EXIT_UNAVAILABLE
        _err("Komodo call failed (%s)" % e)
        return fail_code
    if not res.ok:
        _report_failure(verb, res)
        return fail_code
    sys.stdout.write("%s ok\n" % " ".join([verb] + services))
    return EXIT_OK


def validate_pairs(pairs):
    """Problem string for the first bad pair, else None."""
    seen = set()
    for key, value in pairs:
        if key not in ENV_HOST_WRITTEN:
            return "key not allowed: %s" % key
        if key in seen:
            return "duplicate key: %s" % key
        seen.add(key)
        problem = value_problem(value)
        if problem:
            return "invalid value for %s (%s)" % (key, problem)
    return None


def _status(ok, written, unchanged=(), pending=(), reason=None):
    if ok:
        line = "VOIPBIN_ENV_WRITE: status=ok written=%s unchanged=%s" % (",".join(written), ",".join(unchanged))
    else:
        line = "VOIPBIN_ENV_WRITE: status=error reason=%s written=%s pending=%s" % (
            reason, ",".join(written), ",".join(pending))
    _err(line)


def _patch_env_copy(project_dir, values):
    """Rewrite `.env.komodo` once (temp file + rename, owner and mode kept):
    replace every line of each key, append keys without a line."""
    path = _p(project_dir, ENV_COPY)
    st = os.stat(path)
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "surrogateescape")
    lines = text.split("\n")
    remaining = dict(values)
    out = []
    for line in lines:
        m = ENV_LINE_RE.match(line)
        if m and m.group(1) in values:
            key = m.group(1)
            if key in remaining:
                out.append("%s=%s" % (key, values[key]))
                del remaining[key]
            # a duplicate line of the same key is dropped
        else:
            out.append(line)
    if remaining:
        if out and out[-1] == "":
            out.pop()
        out.extend("%s=%s" % (k, v) for k, v in remaining.items())
        out.append("")
    _write_atomic(path, "\n".join(out).encode("utf-8", "surrogateescape"), owner_from=st)


def var_set(project_dir, pairs):
    pairs = [(k, v) for k, v in pairs]
    names = [k for k, _ in pairs]
    problem = validate_pairs(pairs)
    if problem:
        _err("var-set: " + problem)  # names and a reason word only, never a value
        _status(False, [], pending=names, reason="invalid")
        return EXIT_INVALID
    # HOST_EXTERNAL_IP is the completion marker of an IP change: always last.
    pairs = [p for p in pairs if p[0] != LAST_KEY] + [p for p in pairs if p[0] == LAST_KEY]
    names = [k for k, _ in pairs]
    m = mode(project_dir)
    if m != "adopted":
        _err(BROKEN_HINT if m == "broken" else "Komodo is not set up on this install")
        _status(False, [], pending=names, reason="komodo-unreachable")
        return EXIT_UNAVAILABLE
    try:
        client = key_client(project_dir)
    except Unavailable as e:
        _err(str(e))
        _status(False, [], pending=names, reason="komodo-unreachable")
        return EXIT_UNAVAILABLE
    if not check_core():
        _status(False, [], pending=names, reason="komodo-unreachable")
        return EXIT_UNAVAILABLE
    db = check_db(client)
    if db != "ok":
        _status(False, [], pending=names, reason="komodo-db-timeout" if db == "timeout" else "komodo-unreachable")
        return EXIT_UNAVAILABLE

    copy_now = read_env_raw(_p(project_dir, ENV_COPY))
    written, unchanged = [], []
    created = False
    failure = None  # (reason, exit code)
    for key, value in pairs:
        name = VAR_PREFIX + key
        try:
            try:
                client.write("UpdateVariableValue", {"name": name, "value": value}, TIMEOUT_WRITE)
            except KomodoError as e:
                if e.kind == "http" and e.code == 500 and "no variable found with given name" in e.body:
                    client.write("CreateVariable", {"name": name, "value": value, "description": "",
                                                    "is_secret": is_secret_name(key)}, TIMEOUT_WRITE)
                    created = True
                else:
                    raise
        except KomodoError as e:
            if e.kind == "timeout":
                failure = ("komodo-db-timeout", EXIT_UNAVAILABLE)
            elif e.kind == "http" and e.code == 400:
                failure = ("rejected", EXIT_INVALID)
            else:
                failure = ("komodo-unreachable", EXIT_UNAVAILABLE)
            break
        (unchanged if copy_now.get(key) == value else written).append(key)
    done = written + unchanged
    done_order = [k for k, _ in pairs if k in done]
    pending = [k for k in names if k not in done]

    if created:
        try:
            refresh_stack_environment(client)
        except KomodoError:
            if failure is None:
                failure = ("komodo-unreachable", EXIT_UNAVAILABLE)

    copy_failed = False
    if done_order:
        try:
            _patch_env_copy(project_dir, dict((k, dict(pairs)[k]) for k in done_order))
        except OSError:
            copy_failed = True
    if copy_failed:
        if any(k in IP_KEYS for k in done_order):
            _status(False, written, pending=pending, reason="env-copy")
            return EXIT_UNAVAILABLE
        _err("warning: could not update %s; the next render will fix it" % ENV_COPY)
    if failure:
        _status(False, written + unchanged, pending=pending, reason=failure[0])
        return failure[1]
    _status(True, written, unchanged=unchanged)
    return EXIT_OK


# ==============================================================================
# health
# ==============================================================================
class _Budget(object):
    def __init__(self, total):
        self.end = time.monotonic() + total

    def cap(self, limit):
        return max(0.2, min(limit, self.end - time.monotonic()))


def _envfile_state(project_dir):
    path = _p(project_dir, ENV_COPY)
    try:
        st = os.stat(path)
    except OSError as e:
        return "missing" if e.errno in (errno.ENOENT, errno.ENOTDIR) else "unreadable"
    if not os.access(path, os.R_OK):
        return "unreadable"
    try:
        owner = os.stat(project_dir).st_uid
    except OSError:
        return "unreadable"
    if st.st_uid != owner or stat.S_IMODE(st.st_mode) != 0o600:
        return "bad-owner-or-mode"
    return "ok"


def health_fields(project_dir, quick=False):
    f = {"adopted": "no", "core": "down", "db": "unknown", "auth": "unknown",
         "periphery_unit": "missing", "server": "unknown", "stack": "unknown", "envfile": "missing"}
    if mode(project_dir) != "adopted":
        return f
    f["adopted"] = "yes"
    budget = _Budget(HEALTH_QUICK_BUDGET if quick else HEALTH_BUDGET)
    f["envfile"] = _envfile_state(project_dir)
    f["periphery_unit"] = _periphery_unit()
    if check_core(budget.cap(HEALTH_QUICK_CORE if quick else TIMEOUT_CORE)):
        f["core"] = "ok"
    else:
        return f
    try:
        client = key_client(project_dir)
    except Unavailable:
        f["auth"] = "rejected"
        return f
    db = check_db(client, budget.cap(HEALTH_QUICK_DB if quick else TIMEOUT_DB))
    if db == "timeout" or db == "down":
        f["db"] = "timeout" if db == "timeout" else "unknown"
        return f
    f["db"] = "ok"
    if db == "rejected":
        f["auth"] = "rejected"
        return f
    f["auth"] = "ok"
    f["server"] = _server_state(client, budget.cap(2))
    try:
        stack = get_stack(client)
        if stack is None:
            f["stack"] = "missing"
        else:
            cfg = stack.get("config") or {}
            f["stack"] = "present" if cfg.get("env_file_path") == ENV_COPY else "missing"
    except KomodoError:
        f["stack"] = "unknown"
    return f


def health(project_dir, quick=False):
    f = health_fields(project_dir, quick)
    if f["adopted"] == "no":
        line = "KOMODO_HEALTH: adopted=no"
    else:
        line = "KOMODO_HEALTH: " + " ".join(
            "%s=%s" % (k, f[k]) for k in ("adopted", "core", "db", "auth", "periphery_unit",
                                          "server", "stack", "envfile"))
    sys.stdout.write(line + "\n")
    return EXIT_OK


# ==============================================================================
# CLI key, onboarding (low level, no mode check)
# ==============================================================================
def admin_login(project_dir):
    """Login as the initial admin. Password comes from komodo/admin-password."""
    user = get_env_var(_p(project_dir, "komodo", ".env"), "KOMODO_INIT_ADMIN_USERNAME")
    try:
        with open(_p(project_dir, "komodo", "admin-password"), "r") as f:
            password = f.read().strip()
    except OSError:
        raise Unavailable("cannot read komodo/admin-password")
    if not user or not password:
        raise Unavailable("admin credentials are missing")
    return login(user, password)


def _safe_komodo_dir(project_dir):
    d = _p(project_dir, "komodo")
    if os.path.islink(d) or not os.path.isdir(d):
        raise Unavailable("komodo/ is missing or a symbolic link")
    return d


def ensure_cli_key(project_dir):
    """Client for the CLI key; an existing valid key is reused, otherwise a new
    API key is created through an admin login and stored in komodo/cli-key (600)."""
    _safe_komodo_dir(project_dir)
    try:
        client = key_client(project_dir)
        client.read("GetVersion", {}, TIMEOUT_DB)
        return client
    except Unavailable:
        pass
    except KomodoError as e:
        if not (e.kind == "http" and e.code in (401, 403)):
            raise
    admin = admin_login(project_dir)
    r = admin.auth_manage("CreateApiKey", {"name": "voipbin-cli", "expires": 0})
    key, secret = (r or {}).get("key"), (r or {}).get("secret")
    if not key or not secret:
        raise KomodoError("proto")
    path = _p(project_dir, "komodo", "cli-key")
    _write_atomic(path, ("%s:%s\n" % (key, secret)).encode("ascii"), 0o600)
    _adjust_owner(project_dir, path)
    return Client({"x-api-key": key, "x-api-secret": secret})


def onboard(project_dir, out_key, out_pubkey):
    """Create an onboarding key and fetch the core public key into two 600 files.
    Exit 0, 1 (other failure) or 21 (Komodo unreachable or login rejected)."""
    try:
        admin = admin_login(project_dir)
        r = admin.write("CreateOnboardingKey", {"name": "voipbin-periphery-%d" % int(time.time())})
        info = admin.read("GetCoreInfo", {})
    except Unavailable as e:
        _err(str(e))
        return EXIT_FAIL
    except KomodoError as e:
        if e.kind in ("unreachable", "timeout") or (e.kind == "http" and e.code in (401, 403)):
            _err("onboard: Komodo is not available (%s)" % e)
            return EXIT_UNAVAILABLE
        _err("onboard: Komodo call failed (%s)" % e)
        return EXIT_FAIL
    priv = (r or {}).get("private_key")
    pub = (info or {}).get("public_key")
    if not priv or not pub:
        _err("onboard: unexpected Komodo response")
        return EXIT_FAIL
    try:
        _write_atomic(out_key, (priv + "\n").encode("utf-8"), 0o600)
        _write_atomic(out_pubkey, (pub + "\n").encode("utf-8"), 0o600)
    except OSError as e:
        _err("onboard: cannot write output files (%s)" % errno.errorcode.get(e.errno, "error"))
        return EXIT_FAIL
    return EXIT_OK


# ==============================================================================
# provision: import -> render -> verify -> strip -> marker
# ==============================================================================
def _eligible(value):
    """Value (raw, quotes kept) that may move into a Variable."""
    return value_problem(_unquote(value)) is None


def _plan_import(project_dir):
    """Returns (strip, values, error). strip: keys eligible in .env (moved out of
    .env); values: {key: raw value} to import in order of preference .env first,
    then .env.komodo for keys .env has no usable value for. error: message or None."""
    env = read_env_raw(_p(project_dir, ".env"))
    copy = read_env_raw(_p(project_dir, ENV_COPY))
    refs = compose_refs(project_dir)
    strip, values = [], {}
    for key, raw in env.items():
        if key in refs and key not in ENV_HOST_ONLY and _eligible(raw):
            strip.append(key)
            values[key] = raw
        elif key in ENV_HOST_WRITTEN and _unquote(raw).strip() != "":
            return None, None, "%s has a value Komodo cannot store; shorten it and retry" % key
    for key, raw in copy.items():
        if key in values or key not in refs or key in ENV_HOST_ONLY:
            continue
        if _unquote(env.get(key, "")).strip() != "":
            continue  # .env keeps its own (unusable) value
        if _eligible(raw):
            values[key] = raw
        elif key in ENV_HOST_WRITTEN and _unquote(raw).strip() != "":
            return None, None, "%s has a value Komodo cannot store" % key
    return strip, values, None


def required_vars_missing(project_dir, var_values):
    """Required keys that are absent or empty as Variables. In external mode the
    four certificate keys may be empty."""
    domain_mode = get_env_var(_p(project_dir, ".env"), "DOMAIN_MODE") or get_env_var(
        _p(project_dir, ENV_COPY), "DOMAIN_MODE") or "internal"
    missing = []
    for key in KOMODO_REQUIRED_VARS:
        v = var_values.get(VAR_PREFIX + key, {}).get("value", "")
        if _unquote(v).strip() == "":
            if domain_mode == "external" and key in CERT_KEYS:
                continue
            missing.append(key)
    return missing


def _sync_variables(client, values, overwrite):
    """Create missing Variables; with overwrite also set the existing ones to the
    given value (.env is the only source in that state). Returns names touched."""
    existing = list_variables(client)
    for key, raw in values.items():
        name = VAR_PREFIX + key
        if name in existing:
            if overwrite and existing[name]["value"] != raw:
                client.write("UpdateVariableValue", {"name": name, "value": raw})
        else:
            client.write("CreateVariable", {"name": name, "value": raw, "description": "",
                                            "is_secret": is_secret_name(key)})


def _differing(base, after):
    return sorted(s for s in set(base) | set(after) if base.get(s) != after.get(s))


def _strip_env(project_dir, strip_keys):
    """Write .env.new (same owner and mode) without the moved keys; return its path."""
    src = _p(project_dir, ".env")
    st = os.stat(src)
    with open(src, "rb") as f:
        text = f.read().decode("utf-8", "surrogateescape")
    keep = []
    for line in text.split("\n"):
        m = ENV_LINE_RE.match(line)
        if m and m.group(1) in strip_keys:
            continue
        keep.append(line)
    new = _p(project_dir, ".env.new")
    try:
        os.unlink(new)
    except OSError:
        pass
    _write_atomic_noreplace(new, "\n".join(keep).encode("utf-8", "surrogateescape"), st)
    return new


def _write_atomic_noreplace(path, data, owner_from):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, stat.S_IMODE(owner_from.st_mode))
    try:
        os.chown(path, owner_from.st_uid, owner_from.st_gid, follow_symlinks=False)
    except OSError:
        pass


def _write_marker(project_dir):
    path = marker_path(project_dir)
    _write_atomic(path, ("adopted %d\n" % int(time.time())).encode("ascii"), 0o600)
    _adjust_owner(project_dir, path)


def provision(project_dir):
    """Run the setup steps that need the API. Preconditions: core, mongo and
    periphery are up and komodo/.env and komodo/admin-password exist."""
    for rel in (".env", "docker-compose.yml", os.path.join("komodo", ".env"),
                os.path.join("komodo", "admin-password")):
        if not os.path.isfile(_p(project_dir, rel)):
            _err("provision: missing %s" % rel)
            return EXIT_INVALID
    adopted = os.path.exists(marker_path(project_dir))

    strip, values, error = _plan_import(project_dir)
    if error:
        _err("provision: " + error)
        if error.split()[0] in CERT_KEYS:
            _err("Shorten the certificate chain, then run: sudo ./voipbin install --resume")
        return EXIT_INVALID
    state_a = (not adopted) and bool(strip)

    baseline = None
    if state_a:
        baseline = compose_hashes(project_dir, [".env"])
        if baseline is None:
            _err("provision: docker compose config failed on .env")
            return EXIT_FAIL

    try:
        client = ensure_cli_key(project_dir)
        if adopted:
            ensure_stack(client, project_dir)
            return EXIT_OK
        _sync_variables(client, values, overwrite=state_a)
        if not state_a:
            missing = required_vars_missing(project_dir, list_variables(client))
            if missing:
                _err("provision: required Variables are missing: %s" % ", ".join(missing))
                _err("Restore the backup .env or %s, then run: sudo ./voipbin komodo setup" % ENV_COPY)
                return EXIT_INVALID
        ensure_stack(client, project_dir)
        res = run_action(client, "PullStack", {"stack": STACK_NAME}, TIMEOUT_PULL_ACTION)
        if not res.ok:
            _report_failure("pull", res)
            return EXIT_FAIL
    except Unavailable as e:
        _err("provision: %s" % e)
        return EXIT_UNAVAILABLE
    except KomodoError as e:
        _err("provision: Komodo call failed (%s)" % e)
        if e.kind in ("unreachable", "timeout") or (e.kind == "http" and e.code in (401, 403)):
            return EXIT_UNAVAILABLE
        return EXIT_FAIL

    copy_path = _p(project_dir, ENV_COPY)
    if not os.path.exists(copy_path):
        _err("provision: the rendering copy %s was not created" % ENV_COPY)
        return EXIT_FAIL
    try:
        os.chmod(copy_path, 0o600)
    except OSError:
        pass
    _adjust_owner(project_dir, copy_path)

    if state_a:
        after = compose_hashes(project_dir, [".env", ENV_COPY])
        if after is None:
            _err("provision: docker compose config failed after rendering")
            return EXIT_FAIL
        bad = _differing(baseline, after)
        if bad:
            rendered = read_env_raw(copy_path)
            env = read_env_raw(_p(project_dir, ".env"))
            keys = sorted(k for k in set(rendered) | set(strip) if rendered.get(k) != env.get(k))
            _err("provision: verification failed; services differ: %s" % ", ".join(bad))
            _err("keys differing: %s" % (", ".join(keys) if keys else "(none by value)"))
            _err("Fix the listed keys in .env (or restore .env from a backup) and run: sudo ./voipbin komodo setup")
            return EXIT_INVALID
        try:
            new = _strip_env(project_dir, set(strip))
        except OSError as e:
            _err("provision: cannot prepare .env.new (%s)" % errno.errorcode.get(e.errno, "error"))
            return EXIT_FAIL
        check = compose_hashes(project_dir, [".env.new", ENV_COPY])
        if check is None or _differing(baseline, check):
            try:
                os.unlink(new)
            except OSError:
                pass
            _err("provision: verification of the reduced .env failed; .env was left unchanged")
            return EXIT_INVALID
        os.replace(new, _p(project_dir, ".env"))
    try:
        _write_marker(project_dir)
    except OSError as e:
        _err("provision: cannot write the marker (%s)" % errno.errorcode.get(e.errno, "error"))
        return EXIT_FAIL
    return EXIT_OK


# ==============================================================================
# CLI
# ==============================================================================
def _read_pairs(stream):
    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    pairs = []
    for line in raw.split("\n"):
        if line == "":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError("bad line")
        pairs.append((key, value))
    return pairs


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _err("usage: komodo_lib.py mode|guard|ping|exec|var-set|health|provision|onboard|server-ok ...")
        return EXIT_INVALID
    sub, rest = argv[0], argv[1:]
    project_dir = default_project_dir()
    if sub == "mode":
        print(mode(project_dir))
        return EXIT_OK
    if sub == "guard":
        return guard(project_dir)
    if sub == "ping":
        return ping(project_dir, quiet="--quiet" in rest)
    if sub == "exec":
        if not rest:
            _err("usage: exec <verb> [service...]")
            return EXIT_FAIL
        return exec_verb(project_dir, rest[0], rest[1:])
    if sub == "var-set":
        try:
            pairs = _read_pairs(sys.stdin.buffer)
        except (ValueError, UnicodeDecodeError):
            _err("var-set: input must be KEY=VALUE lines")
            _status(False, [], reason="invalid")
            return EXIT_INVALID
        return var_set(project_dir, pairs)
    if sub == "server-ok":
        return EXIT_OK if server_registered(project_dir) else EXIT_FAIL
    if sub == "health":
        return health(project_dir, quick="--quick" in rest)
    if sub == "provision":
        return provision(project_dir)
    if sub == "onboard":
        opts = {}
        i = 0
        while i < len(rest):
            if rest[i] in ("--out-key", "--out-pubkey") and i + 1 < len(rest):
                opts[rest[i]] = rest[i + 1]
                i += 2
            else:
                _err("usage: onboard --out-key FILE --out-pubkey FILE")
                return EXIT_FAIL
        if "--out-key" not in opts or "--out-pubkey" not in opts:
            _err("usage: onboard --out-key FILE --out-pubkey FILE")
            return EXIT_FAIL
        return onboard(project_dir, opts["--out-key"], opts["--out-pubkey"])
    _err("unknown sub command: %s" % sub)
    return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
