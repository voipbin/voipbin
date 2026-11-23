#!/usr/bin/env python3
"""Gate for compose calls (state changes must go through the Komodo bridge).

Scans install/scripts/*.sh, install/scripts/voipbin-cli.py and install/voipbin
for `docker compose` / `docker-compose` and fails when:

  1. an executed call whose first non-option argument is not a read verb
     (ps, logs, config, images, ls, top, port, events, version) sits outside
     the allow list (file and function name, never line numbers),
  2. the verb is not a literal (a variable, a placeholder, a starred list
     element): the call cannot be judged, so it fails closed,
  3. `exec` runs outside an allowed function (it cannot be judged as read),
  4. a message (a string the user may copy) names a state changing compose
     command (up, rm, down, restart, stop, start, run, create, pull) without
     being replaced by a CLI command (compose_hint) or carrying a
     COMPOSE_ENV_FILES= prefix,
  5. a script that runs docker compose does not call compose_env_setup.

Test files (install/tests, install/scripts/tests) are not scanned.

The scanner also checks itself: injecting one violating line into the real
files must be reported (mutation checks at the end).

Run: python3 scripts/tests/test_komodo_gates.py [--root <install-dir>] [--list]
  --root   scan another tree (used to run the gate against an older checkout)
  --list   print every finding with its file, function and text
"""
import ast
import glob
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(os.path.dirname(HERE))

READ_VERBS = {"ps", "logs", "config", "images", "ls", "top", "port", "events", "version"}
MESSAGE_VERBS = {"up", "rm", "down", "restart", "stop", "start", "run", "create", "pull"}
# global compose options that take a value as the next argument
VALUE_OPTIONS = {"-f", "--file", "-p", "--project-name", "--env-file", "--profile",
                 "--project-directory", "--ansi", "--parallel", "--progress"}

# Where a state changing or unjudgeable call is allowed to appear.
# (file name, function name); "*" matches every function of the file.
ALLOW_EXEC = {
    # the bridge itself: none mode hands the arguments to docker compose
    ("common.sh", "compose_exec"),
    # komodo-setup.sh manages the komodo project only (its own compose file)
    ("komodo-setup.sh", "*"),
    # clean.sh: direct cleanup that Komodo cannot do (volumes, images) and the
    # fallback for an unreachable Komodo; both name the environment files
    ("clean.sh", "main"),
    ("clean.sh", "clean_down_adopted"),
    # start.sh: setup_mkcert keeps the historic restart in place for installs
    # without Komodo; with Komodo the restart moves to restart_cert_consumers
    ("start.sh", "setup_mkcert"),
    # voipbin-cli.py: the none branches of the helpers that call the bridge
    # when Komodo is in use (class: direct execution kept for installs
    # without Komodo), the clean command, and the secret key tool runner
    ("voipbin-cli.py", "_compose_restart"),
    ("voipbin-cli.py", "_compose_stop"),
    ("voipbin-cli.py", "_compose_pull"),
    ("voipbin-cli.py", "_compose_up"),
    ("voipbin-cli.py", "cmd_clean"),
    # the none branches of start and the pinned upgrade pull keep the historic
    # direct call; the Komodo branch of the same function uses the bridge
    ("voipbin-cli.py", "cmd_start"),
    ("voipbin-cli.py", "_upgrade_pinned"),
    ("voipbin-cli.py", "_provider_run"),
}
# docker compose exec runs a command in a service: not a read, so only here
ALLOW_COMPOSE_EXEC = {
    ("doctor.sh", "check_rabbitmq_plugin"),
    ("doctor.sh", "check_queue_consumers"),
}
# messages that print a state changing command on purpose
ALLOW_MESSAGE = {
    ("common.sh", "compose_hint"),
    # status views: the none branch keeps the historic hint, the Komodo
    # branch of the same function prints the voipbin command instead
    ("voipbin-cli.py", "dns_status"),
    ("voipbin-cli.py", "network_status"),
    # key store guidance: the environment file prefix is a variable (pfx),
    # empty for installs without Komodo, so the literal check cannot see it
    ("voipbin-cli.py", "replace_cmds"),
    # restore guidance: the none branch keeps the historic hint for the
    # databases it starts, the Komodo branch prints the voipbin command
    ("voipbin-cli.py", "cmd_restore"),
    # the provider key hint carries the environment file prefix in a variable;
    # the progress label names the pull that the helper routes by mode;
    # the rollback hint prints the voipbin command when Komodo is in use
    ("voipbin-cli.py", "_provider_report"),
    ("voipbin-cli.py", "_update_images"),
    ("voipbin-cli.py", "cmd_rollback"),
    ("komodo-setup.sh", "*"),
}

SCRIPT_GLOB = "scripts/*.sh"
CLI_FILE = "scripts/voipbin-cli.py"
WRAPPER_FILE = "voipbin"

OCCURRENCE = re.compile(r"docker[ \t]+compose\b|docker-compose(?![.\w-])")
ENV_ASSIGNMENTS = r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*"
# a position where a shell runs a command (bash lines) ...
COMMAND_PREFIX = re.compile(r"(?:^|\$\(|&&|\|\||;|\||\(|\{|\bthen\b|\bdo\b|!|`)\s*" + ENV_ASSIGNMENTS + "$")
# ... and in a python string that is handed to a shell (no prose punctuation)
COMMAND_PREFIX_PY = re.compile(r"(?:^|\$\(|&&|\|\||;|\|)\s*" + ENV_ASSIGNMENTS + "$")
# calls whose string argument is run, not printed
EXEC_CALLS = {"run_cmd", "system", "run", "call", "check_call", "check_output", "Popen", "_run_as", "run_as"}
TERMINATORS = re.compile(r"&&|\|\||;|\||\)|&>|2>|>|<|\n")
PLACEHOLDER = "\x01"


class Finding(object):
    def __init__(self, kind, path, func, line, text):
        self.kind, self.path, self.func, self.line, self.text = kind, path, func, line, text

    def __str__(self):
        return "%s: %s %s() line %s: %s" % (self.kind, self.path, self.func, self.line, self.text.strip()[:140])


# ------------------------------------------------------------------------------
# shared occurrence parsing
# ------------------------------------------------------------------------------
def in_string(text, idx):
    """True when position idx of a shell line sits inside quotes.

    A $( ... ) inside double quotes opens an unquoted command context again,
    so `x="$(cd d && docker compose ps)"` is a command, not text.
    """
    stack = [None]
    i = 0
    while i < idx:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        q = stack[-1]
        if q is None:
            if c in "\"'":
                stack[-1] = c
            elif c == "$" and text[i + 1:i + 2] == "(":
                stack.append(None)
                i += 2
                continue
            elif c == ")" and len(stack) > 1:
                stack.pop()
        elif q == '"':
            if c == '"':
                stack[-1] = None
            elif c == "$" and text[i + 1:i + 2] == "(":
                stack.append(None)
                i += 2
                continue
        else:
            if c == "'":
                stack[-1] = None
        i += 1
    return stack[-1] is not None


def parse_verb(rest):
    """First non-option argument after `docker compose` in `rest` ('' if none)."""
    cut = TERMINATORS.search(rest)
    if cut:
        rest = rest[:cut.start()]
    try:
        words = shlex.split(rest, posix=True)
    except ValueError:
        words = rest.split()
    i = 0
    while i < len(words):
        w = words[i]
        if w in VALUE_OPTIONS:
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w
    return ""


def verb_is_variable(verb):
    return verb == "" or "$" in verb or PLACEHOLDER in verb or "`" in verb


def occurrences(text, python_exec=None):
    """(index, executed, verb) for every compose mention in one line or string.

    Bash (python_exec is None): executed means a command position outside
    quotes. Python: the string must be handed to a run call (python_exec) and
    the mention must sit where a shell starts a command.
    """
    out = []
    for m in OCCURRENCE.finditer(text):
        before = text[:m.start()]
        if python_exec is None:
            executed = bool(COMMAND_PREFIX.search(before)) and not in_string(text, m.start())
        else:
            executed = python_exec and bool(COMMAND_PREFIX_PY.search(before))
        out.append((m.start(), executed, parse_verb(text[m.end():])))
    return out


def judge(kind_prefix, path, func, lineno, text, idx, executed, verb, allow_exec, allow_message):
    """Findings for one occurrence."""
    base = os.path.basename(path)

    def allowed(table):
        return (base, func) in table or (base, "*") in table

    found = []
    if executed:
        if verb in READ_VERBS:
            return found
        if verb == "exec":
            if not allowed(ALLOW_COMPOSE_EXEC) and not allowed(allow_exec):
                found.append(Finding("exec-outside-allow-list", path, func, lineno, text))
            return found
        if allowed(allow_exec):
            return found
        if verb_is_variable(verb):
            found.append(Finding("variable-verb", path, func, lineno, text))
        else:
            found.append(Finding("state-change-outside-allow-list", path, func, lineno, text))
        return found
    # a message: only the literal state changing verbs matter
    if verb in MESSAGE_VERBS and not allowed(allow_message) and not allowed(allow_exec):
        if "COMPOSE_ENV_FILES=" not in text[:idx]:
            found.append(Finding("message-without-cli-command", path, func, lineno, text))
    return found


# ------------------------------------------------------------------------------
# bash
# ------------------------------------------------------------------------------
FUNC_START = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")


def strip_inline_comment(line):
    """Cut a trailing ` # comment` that is outside quotes."""
    for m in re.finditer(r"(?:^|\s)#", line):
        if not in_string(line, m.start()):
            return line[:m.start()]
    return line


def scan_bash(path, text):
    findings = []
    executed_compose = False
    func = "<top>"
    for lineno, raw in enumerate(text.split("\n"), 1):
        m = FUNC_START.match(raw)
        if m:
            func = m.group(1)
        if raw.startswith("}"):
            end_func = True
        else:
            end_func = False
        stripped = raw.strip()
        if stripped.startswith("#"):
            if end_func:
                func = "<top>"
            continue
        line = strip_inline_comment(raw)
        for idx, executed, verb in occurrences(line):
            if executed:
                executed_compose = True
            findings.extend(judge("bash", path, func, lineno, line, idx, executed, verb,
                                  ALLOW_EXEC, ALLOW_MESSAGE))
        if end_func:
            func = "<top>"
    return findings, executed_compose


def needs_env_setup(path, text, executed_compose):
    """Scripts that run docker compose must call compose_env_setup at entry."""
    base = os.path.basename(path)
    if not executed_compose:
        return []
    if base in ("common.sh", "komodo-setup.sh"):
        return []  # library, and the komodo project (its own environment)
    if re.search(r"^\s*(?:if !\s+)?compose_env_setup\b", text, re.M):
        return []
    return [Finding("no-compose_env_setup", path, "<file>", 0, "runs docker compose without compose_env_setup")]


# ------------------------------------------------------------------------------
# python
# ------------------------------------------------------------------------------
def fstring_text(node):
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        else:
            parts.append(PLACEHOLDER)
    return "".join(parts)


def list_calls(elts):
    """Find ["docker", "compose", ...] / ["docker-compose", ...] in list elements.

    Returns (verb, judgeable) tuples: verb is the first non-option element.
    """
    out = []
    n = len(elts)
    for i, e in enumerate(elts):
        if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
            continue
        if e.value == "docker-compose":
            start = i + 1
        elif e.value == "docker" and i + 1 < n and isinstance(elts[i + 1], ast.Constant) and elts[i + 1].value == "compose":
            start = i + 2
        else:
            continue
        j = start
        verb = ""
        while j < n:
            el = elts[j]
            if isinstance(el, ast.Starred):
                verb = PLACEHOLDER
                break
            if not (isinstance(el, ast.Constant) and isinstance(el.value, str)):
                verb = PLACEHOLDER
                break
            if el.value in VALUE_OPTIONS:
                j += 2
                continue
            if el.value.startswith("-"):
                j += 1
                continue
            verb = el.value
            break
        out.append((getattr(e, "lineno", 0), verb))
    return out


def scan_python(path, text):
    tree = ast.parse(text)
    findings = []
    stack = []

    def current():
        return stack[-1] if stack else "<module>"

    calls = []  # callee names of the enclosing Call nodes

    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                doc_nodes.add(id(body[0].value))

    def visit(node):
        pushed = False
        call_pushed = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node.name)
            pushed = True
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            calls.append(name)
            call_pushed = True
        python_exec = any(c in EXEC_CALLS for c in calls)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_nodes:
            for idx, executed, verb in occurrences(node.value, python_exec):
                findings.extend(judge("python", path, current(), node.lineno, node.value, idx, executed, verb,
                                      ALLOW_EXEC, ALLOW_MESSAGE))
        elif isinstance(node, ast.JoinedStr):
            joined = fstring_text(node)
            for idx, executed, verb in occurrences(joined, python_exec):
                findings.extend(judge("python", path, current(), node.lineno, joined, idx, executed, verb,
                                      ALLOW_EXEC, ALLOW_MESSAGE))
        if isinstance(node, (ast.List, ast.Tuple)):
            for lineno, verb in list_calls(node.elts):
                text_line = "[docker compose %s ...]" % (verb.replace(PLACEHOLDER, "<var>") or "<none>")
                if verb in READ_VERBS:
                    continue
                if (os.path.basename(path), current()) in ALLOW_EXEC:
                    continue
                if verb == "exec":
                    if (os.path.basename(path), current()) not in ALLOW_COMPOSE_EXEC:
                        findings.append(Finding("exec-outside-allow-list", path, current(), lineno, text_line))
                    continue
                kind = "variable-verb" if verb_is_variable(verb) else "state-change-outside-allow-list"
                findings.append(Finding(kind, path, current(), lineno, text_line))
        if not isinstance(node, ast.JoinedStr):
            for child in ast.iter_child_nodes(node):
                visit(child)
        if call_pushed:
            calls.pop()
        if pushed:
            stack.pop()

    visit(tree)
    return findings


# ------------------------------------------------------------------------------
# whole tree
# ------------------------------------------------------------------------------
def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def input_files(root):
    scripts = sorted(glob.glob(os.path.join(root, SCRIPT_GLOB)))
    cli = os.path.join(root, CLI_FILE)
    wrapper = os.path.join(root, WRAPPER_FILE)
    return scripts, cli, wrapper


def scan_tree(root):
    """(findings, scanned_files, mention_count). Missing inputs are findings."""
    findings = []
    scripts, cli, wrapper = input_files(root)
    mentions = 0
    scanned = 0
    if not scripts:
        findings.append(Finding("input-missing", root, "<tree>", 0, "no scripts/*.sh found"))
    for path in scripts:
        text = read(path)
        scanned += 1
        mentions += len(OCCURRENCE.findall(text))
        f, executed = scan_bash(path, text)
        findings.extend(f)
        findings.extend(needs_env_setup(path, text, executed))
    for path, scanner in ((cli, scan_python), (wrapper, scan_bash)):
        if not os.path.isfile(path):
            findings.append(Finding("input-missing", path, "<file>", 0, "file not found"))
            continue
        text = read(path)
        scanned += 1
        mentions += len(OCCURRENCE.findall(text))
        out = scanner(path, text)
        findings.extend(out[0] if isinstance(out, tuple) else out)
    if scanned and mentions == 0:
        findings.append(Finding("input-empty", root, "<tree>", 0, "no compose mention found in any input"))
    return findings, scanned, mentions


# ------------------------------------------------------------------------------
# the test run
# ------------------------------------------------------------------------------
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  %s  %s" % ("PASS" if cond else "FAIL", name) + (("  (%s)" % (detail,)) if detail and not cond else ""))


def kinds(findings):
    return sorted(set(f.kind for f in findings))


def snippet_bash(text, name="x.sh"):
    return scan_bash(name, text)[0]


def snippet_python(text, name="voipbin-cli.py"):
    return scan_python(name, text)


def main(argv):
    root = DEFAULT_ROOT
    list_all = False
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            root = os.path.abspath(argv[i + 1])
            i += 2
        elif argv[i] == "--list":
            list_all = True
            i += 1
        else:
            print("unknown argument: %s" % argv[i])
            return 2

    print("== G1 scanner behaviour on small inputs")
    check("G1 read verbs pass", snippet_bash("docker compose ps -q\ndocker compose logs web\n") == [])
    check("G1 every read verb passes", all(snippet_bash("docker compose %s\n" % v) == [] for v in sorted(READ_VERBS)))
    check("G1 options are skipped before the verb",
          snippet_bash("docker compose -f a.yml -p x --env-file e ps\n") == [])
    check("G1 a state change is found behind options",
          kinds(snippet_bash("docker compose -f a.yml -p x up -d\n")) == ["state-change-outside-allow-list"])
    check("G1 every state changing verb is found",
          all(kinds(snippet_bash("docker compose %s x\n" % v)) == ["state-change-outside-allow-list"]
              for v in ("up", "rm", "down", "restart", "stop", "start", "run", "create", "pull",
                        "kill", "pause", "unpause", "build", "scale", "cp", "watch")))
    check("G1 a variable verb fails closed", kinds(snippet_bash('docker compose "$verb" "$@"\n')) == ["variable-verb"])
    check("G1 a placeholder verb in a python string fails closed",
          kinds(snippet_python('x = run_cmd(f"docker compose {verb} 2>&1")\n')) == ["variable-verb"])
    check("G1 a python list with a variable verb fails closed",
          kinds(snippet_python('subprocess.run(["docker", "compose", verb, "x"])\n')) == ["variable-verb"])
    check("G1 a python list with a starred element fails closed",
          kinds(snippet_python('subprocess.run(["docker", "compose", *args])\n')) == ["variable-verb"])
    check("G1 a python list with a literal state change is found",
          kinds(snippet_python('subprocess.run(["docker", "compose", "up", "-d", s])\n')) == ["state-change-outside-allow-list"])
    check("G1 docker-compose (v1 spelling) is found too",
          kinds(snippet_bash("docker-compose up -d\n")) == ["state-change-outside-allow-list"])
    check("G1 docker-compose.yml is not a command", snippet_bash('echo "see docker-compose.yml"\n') == [])
    check("G1 exec is not a read verb", kinds(snippet_bash("docker compose exec -T db ls\n")) == ["exec-outside-allow-list"])
    check("G1 exec passes in an allowed function",
          snippet_bash("check_rabbitmq_plugin() {\n    docker compose exec -T rabbitmq x\n}\n", "doctor.sh") == [])
    check("G1 comments are ignored", snippet_bash("# docker compose up -d\n    # docker compose down\n") == [])
    check("G1 a trailing comment is ignored", snippet_bash("echo ok  # docker compose up -d\n") == [])
    check("G1 a state change after && is found", kinds(snippet_bash("cd x && docker compose up -d y\n")) == ["state-change-outside-allow-list"])
    check("G1 an environment prefix does not hide a command",
          kinds(snippet_bash('COMPOSE_PROFILES="$p" docker compose up -d\n')) == ["state-change-outside-allow-list"])
    check("G1 a call inside $(...) is found", kinds(snippet_bash('x=$(docker compose down)\n')) == ["state-change-outside-allow-list"])
    check("G1 an allowed function may run the state change",
          snippet_bash("compose_exec() {\n    docker compose \"$verb\" \"$@\"\n}\n", "common.sh") == [])
    check("G1 the allow list is per function, not per file",
          kinds(snippet_bash("other() {\n    docker compose up -d\n}\n", "common.sh")) == ["state-change-outside-allow-list"])
    check("G1 a message with a state change is found",
          kinds(snippet_bash('echo "Run docker compose up -d first"\n')) == ["message-without-cli-command"])
    check("G1 a message with an environment prefix passes",
          snippet_bash('echo "Run COMPOSE_ENV_FILES=.env,.env.komodo docker compose up -d first"\n') == [])
    check("G1 a message that only reads passes", snippet_bash('echo "see docker compose logs web"\n') == [])
    check("G1 a python print with a state change is found",
          kinds(snippet_python('print("run: docker compose up -d")\n')) == ["message-without-cli-command"])
    check("G1 a python docstring is not a message",
          snippet_python('def f():\n    """docker compose up -d"""\n    return 1\n') == [])
    check("G1 a python call in an allowed function passes",
          snippet_python('def _compose_up():\n    return run_cmd("docker compose up -d 2>&1")\n') == [])
    check("G1 the same call in another function is found",
          kinds(snippet_python('def other():\n    return run_cmd("docker compose up -d 2>&1")\n')) == ["state-change-outside-allow-list"])
    check("G1 a script that runs compose without compose_env_setup is found",
          kinds(needs_env_setup("x.sh", "docker compose ps\n", True)) == ["no-compose_env_setup"])
    check("G1 compose_env_setup at entry satisfies it",
          needs_env_setup("x.sh", "main() {\n    compose_env_setup || exit 21\n}\n", True) == [])

    print("== G2 the tree")
    findings, scanned, mentions = scan_tree(root)
    check("G2 inputs found and scanned (scripts, CLI, wrapper)", scanned >= 3 and not [f for f in findings if f.kind.startswith("input-")],
          "scanned=%d" % scanned)
    check("G2 compose mentions were found at all", mentions > 0, "mentions=%d" % mentions)
    scripts_only = [f for f in findings if f.path.endswith(".sh") or f.path.endswith("voipbin")]
    cli_only = [f for f in findings if f.path.endswith(".py")]
    for label, group in (("scripts and wrapper", scripts_only), ("voipbin-cli.py", cli_only)):
        for kind in ("state-change-outside-allow-list", "variable-verb", "exec-outside-allow-list",
                     "message-without-cli-command", "no-compose_env_setup"):
            offenders = [f for f in group if f.kind == kind]
            check("G2 %s: no %s" % (label, kind), not offenders,
                  "%d: %s" % (len(offenders), "; ".join(str(o) for o in offenders[:3])))
    print("  INFO  findings: total=%d scripts+wrapper=%d cli=%d" % (len(findings), len(scripts_only), len(cli_only)))
    if list_all:
        for f in findings:
            print("  FINDING  %s" % f)

    print("== G3 mutations: one violating line is reported")
    scripts, cli, wrapper = input_files(root)
    real = {p: read(p) for p in scripts if os.path.basename(p) in ("start.sh", "stop.sh", "install-certs.sh", "doctor.sh", "setup-dns.sh")}
    for p, text in sorted(real.items()):
        before = len(scan_bash(p, text)[0])
        after = len(scan_bash(p, text + "\nmutant() {\n    docker compose up -d x\n}\n")[0])
        check("G3 %s: an injected docker compose up is reported" % os.path.basename(p), after == before + 1)
        after = len(scan_bash(p, text + '\nmutant() {\n    docker compose "$v" x\n}\n')[0])
        check("G3 %s: an injected variable verb is reported" % os.path.basename(p), after == before + 1)
        after = len(scan_bash(p, text + '\nmutant() {\n    docker compose exec x y\n}\n')[0])
        check("G3 %s: an injected docker compose exec is reported" % os.path.basename(p), after == before + 1)
        after = len(scan_bash(p, text + '\nmutant() {\n    echo "run docker compose restart x"\n}\n')[0])
        check("G3 %s: an injected message with a state change is reported" % os.path.basename(p), after == before + 1)
    if os.path.isfile(cli):
        text = read(cli)
        before = len(scan_python(cli, text))
        after = len(scan_python(cli, text + '\ndef mutant():\n    run_cmd("docker compose up -d")\n'))
        check("G3 voipbin-cli.py: an injected docker compose up is reported", after == before + 1)
        after = len(scan_python(cli, text + '\ndef mutant(v):\n    run_cmd(f"docker compose {v} x")\n'))
        check("G3 voipbin-cli.py: an injected f-string verb is reported", after == before + 1)
        after = len(scan_python(cli, text + '\ndef mutant(v):\n    subprocess.run(["docker", "compose", v])\n'))
        check("G3 voipbin-cli.py: an injected list with a variable verb is reported", after == before + 1)
    if scripts:
        start = [p for p in scripts if p.endswith("start.sh")]
        if start:
            text = read(start[0])
            stripped = text.replace("compose_env_setup", "compose_env_setup_removed")
            check("G3 start.sh: without compose_env_setup the entry check reports it",
                  kinds(needs_env_setup(start[0], stripped, scan_bash(start[0], stripped)[1])) == ["no-compose_env_setup"])
        common = [p for p in scripts if p.endswith("common.sh")]
        if common:
            text = read(common[0]).replace("compose_exec() {", "compose_exec_renamed() {")
            check("G3 common.sh: renaming the bridge function drops its allowance",
                  any(f.kind in ("variable-verb", "state-change-outside-allow-list") for f in scan_bash(common[0], text)[0]))

    print("== G4 the retry interval override does not change the production default")
    certs = os.path.join(root, "scripts", "install-certs.sh")
    if os.path.isfile(certs):
        text = read(certs)
        check("G4 the default is the constant 15", re.search(r"^KOMODO_PRECHECK_INTERVAL_DEFAULT=15$", text, re.M) is not None)
        check("G4 the retry count is the constant 4", re.search(r"^KOMODO_PRECHECK_RETRIES=4$", text, re.M) is not None)
        reads = [l for l in text.split("\n") if "VOIPBIN_KOMODO_RETRY_INTERVAL" in l and not l.lstrip().startswith("#")]
        check("G4 the override is read once, with the constant as its default",
              len(reads) == 1 and reads[0].strip() == 'KOMODO_PRECHECK_INTERVAL="${VOIPBIN_KOMODO_RETRY_INTERVAL:-$KOMODO_PRECHECK_INTERVAL_DEFAULT}"',
              str(reads))
        check("G4 a bad override value falls back to the default",
              "KOMODO_PRECHECK_INTERVAL_DEFAULT\"" in text and "=~ ^[0-9]+" in text)
    else:
        check("G4 install-certs.sh exists", False, certs)
    others = []
    for p in glob.glob(os.path.join(root, "scripts", "*")) + [os.path.join(root, WRAPPER_FILE)]:
        if os.path.isfile(p) and not p.endswith("install-certs.sh"):
            try:
                if "VOIPBIN_KOMODO_RETRY_INTERVAL" in read(p):
                    others.append(os.path.basename(p))
            except UnicodeDecodeError:
                pass
    check("G4 no other production file reads the override", others == [], str(others))

    print()
    failed = [r for r in results if not r[1]]
    print("%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
