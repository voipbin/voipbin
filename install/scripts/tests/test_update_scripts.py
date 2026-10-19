#!/usr/bin/env python3
"""Behavioral tests for `_update_scripts` (git-based script update).

Real git repositories are built in a temp dir (an origin plus a clone laid out
like the README: the checkout root is the PARENT of install/). Ownership
demotion is tested with mocks because an unprivileged runner cannot setuid.

Run: python3 scripts/tests/test_update_scripts.py <install-dir>
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

INSTALL = sys.argv[1] if len(sys.argv) > 1 else "."
CLI_PATH = os.path.join(INSTALL, "scripts", "voipbin-cli.py")
START_CWD = os.getcwd()

sys.argv = ["voipbin", "__noop__"]
spec = importlib.util.spec_from_file_location("vbcli_us", CLI_PATH)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test",
    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.test",
    GIT_TERMINAL_PROMPT="0",
)
# the code under test also runs git: keep it away from the developer's config
os.environ.update({k: GIT_ENV[k] for k in GIT_ENV if k.startswith("GIT_")})


def sh(cwd, *argv):
    r = subprocess.run(argv, cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    assert r.returncode == 0, f"{argv}: {r.stderr}"
    return r.stdout.strip()


def commit_file(repo, rel, text, msg="c"):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", msg)


class Env:
    """origin (bare) + work (clone, README layout) + a second clone to push from."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="vbus-")
        self.origin = os.path.join(self.root, "origin.git")
        self.work = os.path.join(self.root, "work")
        self.other = os.path.join(self.root, "other")
        sh(self.root, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh(self.root, "git", "clone", "-q", self.origin, self.work)
        sh(self.work, "git", "checkout", "-q", "-b", "main")
        commit_file(self.work, "install/versions.lock.dist", "{}\n")
        commit_file(self.work, "install/scripts/a.sh", "one\n")
        commit_file(self.work, "README.md", "root\n")
        sh(self.work, "git", "push", "-q", "origin", "main")
        sh(self.work, "git", "branch", "-q", "-u", "origin/main")
        sh(self.root, "git", "clone", "-q", self.origin, self.other)
        self.install = os.path.join(self.work, "install")

    def push_upstream(self, rel="install/scripts/a.sh", text="two\n"):
        sh(self.other, "git", "pull", "-q", "origin", "main")
        commit_file(self.other, rel, text, "upstream")
        sh(self.other, "git", "push", "-q", "origin", "main")

    def head(self):
        return sh(self.work, "git", "rev-parse", "HEAD")

    def close(self):
        os.chdir(START_CWD)
        shutil.rmtree(self.root, ignore_errors=True)


def mkcli(project_dir):
    cli = m.VoIPBinCLI.__new__(m.VoIPBinCLI)
    cli.config = {"project_dir": project_dir}
    return cli


def rcu(cli):
    return getattr(cli, "_update_scripts_rc", None)


def run_us(env_or_dir, check_only=False, cli=None):
    pd = env_or_dir.install if isinstance(env_or_dir, Env) else env_or_dir
    cli = cli or mkcli(pd)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = cli._update_scripts(pd, check_only)
    return ok, cli, buf.getvalue()


# ---- repository discovery ----
e = Env()
ok, cli, out = run_us(e)
check("U1 README layout (.git in the parent of install/) is found; up to date -> True",
      ok is True and "Already up to date" in out, out[-200:])
e.push_upstream()
ok, cli, out = run_us(e, check_only=True)
check("U2 --check reports changes and leaves HEAD alone", ok is True and e.head() != sh(e.origin, "git", "rev-parse", "main"))
ok, cli, out = run_us(e)
check("U3 behind by one commit -> pulled, True",
      ok is True and e.head() == sh(e.origin, "git", "rev-parse", "main"), out[-200:])
check("U3b the file really changed", open(os.path.join(e.install, "scripts", "a.sh")).read() == "two\n")
e.close()

# ---- pull succeeds when origin is an ancestor of HEAD (local commits ahead are allowed) ----
e = Env()
commit_file(e.work, "install/scripts/local.sh", "local\n", "local only")
local_head = e.head()
ok, cli, out = run_us(e)
check("U4 local commits AHEAD of origin -> accepted (origin is an ancestor of HEAD)",
      ok is True and e.head() == local_head, out[-200:])
# ahead, and origin also has something new through a mocked fast-forward pull:
e.push_upstream()
sh(e.work, "git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")   # now merged: ahead again
ok, cli, out = run_us(e)
check("U4b ahead after merging upstream -> True", ok is True, out[-200:])
e.close()

e = Env()
commit_file(e.work, "install/scripts/local.sh", "local\n", "local only")
e.push_upstream()
head_before = e.head()
ok, cli, out = run_us(e)
check("U5 DIVERGED (local ahead + origin ahead) -> False, code 11, HEAD untouched",
      ok is False and rcu(cli) == 11 and e.head() == head_before, out[-300:])
e.close()

# Same shape, but git is configured to merge on pull: only --ff-only keeps the
# diverged repository untouched, so this fails if the flag is dropped.
e = Env()
sh(e.work, "git", "config", "pull.rebase", "false")
commit_file(e.work, "install/scripts/local.sh", "local\n", "local only")
e.push_upstream()
head_before = e.head()
_prev_autoedit = os.environ.get("GIT_MERGE_AUTOEDIT")
os.environ["GIT_MERGE_AUTOEDIT"] = "no"
ok, cli, out = run_us(e)
if _prev_autoedit is None:
    os.environ.pop("GIT_MERGE_AUTOEDIT", None)
else:
    os.environ["GIT_MERGE_AUTOEDIT"] = _prev_autoedit
check("U5b DIVERGED with pull.rebase=false -> still 11 and no merge commit (needs --ff-only)",
      ok is False and rcu(cli) == 11 and e.head() == head_before, out[-300:])
e.close()

# behind after a pull that reported success: origin is NOT an ancestor of HEAD
e = Env()
e.push_upstream()
real_git = m._git


def git_pull_noop(cwd, *args, owner=None):
    if args and args[0] == "pull":
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return real_git(cwd, *args, owner=owner)


with mock.patch.object(m, "_git", git_pull_noop):
    ok, cli, out = run_us(e)
check("U6 pull exits 0 but HEAD is BEHIND origin -> False, code 11",
      ok is False and rcu(cli) == 11 and "not an ancestor" in out, out[-300:])
e.close()

# ---- failure modes decided by exit codes ----
e = Env()
sh(e.work, "git", "checkout", "-q", "--detach")
ok, cli, out = run_us(e)
check("U7 detached HEAD -> False, 11", ok is False and rcu(cli) == 11 and "detached" in out, out[-200:])
e.close()

e = Env()
sh(e.work, "git", "checkout", "-q", "-b", "feature")
ok, cli, out = run_us(e)
check("U8 branch without an origin counterpart -> False, 11 (never 'up to date')",
      ok is False and rcu(cli) == 11 and "does not exist" in out and "Already up to date" not in out, out[-200:])
e.close()

e = Env()
sh(e.work, "git", "remote", "set-url", "origin", os.path.join(e.root, "missing.git"))
ok, cli, out = run_us(e)
check("U9 fetch failure -> False, 11", ok is False and rcu(cli) == 11, out[-200:])
e.close()

e = Env()
e.push_upstream()
with mock.patch.object(m, "_git", lambda cwd, *a, owner=None: (
        types.SimpleNamespace(returncode=1, stdout="", stderr="boom") if a and a[0] == "pull"
        else real_git(cwd, *a, owner=owner))):
    ok, cli, out = run_us(e)
check("U10 pull failure decided by exit code -> False, 11", ok is False and rcu(cli) == 11, out[-200:])
e.close()

# pull failure while the stash cannot be restored: the operator is told the stash is kept
e = Env()
e.push_upstream()
with open(os.path.join(e.install, "scripts", "a.sh"), "a") as f:
    f.write("local edit\n")
def _fail_pull_and_pop(cwd, *a, owner=None):
    if a and a[0] == "pull":
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    if a[:2] == ("stash", "pop"):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="pop failed")
    return real_git(cwd, *a, owner=owner)
with mock.patch.object(m, "_git", _fail_pull_and_pop):
    ok, cli, out = run_us(e)
check("U10b pull failure + failed stash restore -> False, 11, stash-kept notice",
      ok is False and rcu(cli) == 11 and "stash is still kept" in out and "git stash list" in out, out[-300:])
e.close()

# after a successful pull, a HEAD that does not contain the remote branch + failed restore
e = Env()
e.push_upstream()
with open(os.path.join(e.install, "scripts", "a.sh"), "a") as f:
    f.write("local edit\n")
def _fail_ancestor_and_pop(cwd, *a, owner=None):
    if a[:2] == ("merge-base", "--is-ancestor"):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    if a[:2] == ("stash", "pop"):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="pop failed")
    return real_git(cwd, *a, owner=owner)
with mock.patch.object(m, "_git", _fail_ancestor_and_pop):
    ok, cli, out = run_us(e)
check("U10c ancestor failure + failed stash restore -> False, 11, stash-kept notice",
      ok is False and rcu(cli) == 11 and "stash is still kept" in out, out[-300:])
e.close()

d = tempfile.mkdtemp(prefix="vbus-nogit-")
ok, cli, out = run_us(d)
check("U11 no repository anywhere above -> False, 11", ok is False and rcu(cli) == 11 and "Not a git repository" in out, out[-200:])
os.chdir(START_CWD)
shutil.rmtree(d, ignore_errors=True)

# an unrelated repository in a parent directory must not be adopted
stray = tempfile.mkdtemp(prefix="vbus-stray-")
sh(stray, "git", "init", "-q", "-b", "main")
commit_file(stray, "dotfile", "x\n")
inner = os.path.join(stray, "opt", "voipbin", "install")
os.makedirs(inner)
ok, cli, out = run_us(inner)
check("U12 enclosing repository that does not track this install dir -> False, 11",
      ok is False and rcu(cli) == 11 and "does not track" in out, out[-200:])
os.chdir(START_CWD)
shutil.rmtree(stray, ignore_errors=True)

# ---- local changes: stash handling ----
e = Env()
e.push_upstream("install/scripts/b.sh", "b\n")
with open(os.path.join(e.install, "scripts", "a.sh"), "a") as f:
    f.write("local edit\n")
ok, cli, out = run_us(e)
check("U13 tracked local edit is stashed, pulled and restored",
      ok is True and "local edit" in open(os.path.join(e.install, "scripts", "a.sh")).read()
      and os.path.exists(os.path.join(e.install, "scripts", "b.sh")), out[-300:])
check("U13b stash list is empty afterwards", sh(e.work, "git", "stash", "list") == "")
backups = os.path.join(e.install, ".backup")
check("U13c backup is taken relative to the repository root",
      os.path.isdir(backups) and any(
          os.path.exists(os.path.join(backups, d, "install", "scripts", "a.sh")) for d in os.listdir(backups)))
e.close()

# untracked-only change: stash rc 0 without creating a stash -> an old stash must survive
e = Env()
with open(os.path.join(e.install, "scripts", "a.sh"), "a") as f:
    f.write("older\n")
sh(e.work, "git", "stash", "push", "-q", "-m", "operator stash")
e.push_upstream("install/scripts/b.sh", "b\n")
with open(os.path.join(e.install, "scripts", "new.sh"), "w") as f:
    f.write("untracked\n")
ok, cli, out = run_us(e)
check("U14 untracked-only change: the operator's earlier stash is not popped",
      ok is True and "operator stash" in sh(e.work, "git", "stash", "list"), out[-300:])
e.close()

# conflict on restore -> 11 with its own wording
e = Env()
e.push_upstream("install/scripts/a.sh", "upstream\n")
with open(os.path.join(e.install, "scripts", "a.sh"), "w") as f:
    f.write("local\n")
ok, cli, out = run_us(e)
check("U15 stash pop conflict -> False, 11, 'scripts were updated' wording",
      ok is False and rcu(cli) == 11 and "Scripts were updated" in out
      and "old pin is intact" not in out, out[-300:])
e.close()

# protected paths do not count as local changes (boundary match)
e = Env()
e.push_upstream("install/scripts/b.sh", "b\n")
for name in (".env", "docker-compose.yml.tmp"):
    with open(os.path.join(e.install, name), "w") as f:
        f.write("x\n")
ok, cli, out = run_us(e)
check("U16 untracked .env / docker-compose.yml.tmp are protected: no stash attempted",
      ok is True and "Local changes detected" not in out, out[-300:])
e.close()

check("U17 '.env' does not protect '.env.template'", m._is_protected_path(".env") and not m._is_protected_path(".env.template"))
check("U17b 'docker-compose.yml.tmp' protected, '.dist' is not",
      m._is_protected_path("docker-compose.yml.tmp") and not m._is_protected_path("docker-compose.yml.dist"))
check("U17c directories match on a path boundary",
      m._is_protected_path("certs/x.pem") and m._is_protected_path("certs/") and m._is_protected_path("backups/2026/x")
      and not m._is_protected_path("certsX/a"))
check("U18 porcelain -z: rename keeps the destination, spaces survive",
      m._parse_porcelain_z("R  new name.sh\0old.sh\0?? a b\0 M x\0") ==
      [("R ", "new name.sh"), ("??", "a b"), (" M", "x")])

# ---- ownership: owner decision and the demoted git invocation ----
st_user = types.SimpleNamespace(st_uid=1000, st_gid=1001)
st_root = types.SimpleNamespace(st_uid=0, st_gid=0)
os_env_backup = dict(os.environ)
for k in ("SUDO_UID", "SUDO_GID", "SUDO_USER"):
    os.environ.pop(k, None)
with mock.patch.object(m.os, "geteuid", lambda: 0):
    check("O1 root + user-owned .git -> demote to the .git uid/gid (no SUDO_UID needed)",
          m._git_owner(st_user) == (1000, 1001))
    check("O1b root + root-owned .git -> no demotion", m._git_owner(st_root) is None)
with mock.patch.object(m.os, "geteuid", lambda: 1000):
    check("O1c non-root process never demotes", m._git_owner(st_user) is None)

captured = {}


def fake_run(argv, **kw):
    captured["argv"], captured["kw"] = argv, kw
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


order = []
owner_entry = types.SimpleNamespace(pw_dir="/srv/owner-home", pw_name="owner")
with mock.patch.object(m.subprocess, "run", fake_run), \
        mock.patch("pwd.getpwuid", lambda uid: owner_entry), \
        mock.patch.object(m.os, "getgrouplist", lambda name, gid: [gid, 50]), \
        mock.patch.object(m.os, "setgroups", lambda g: order.append(("groups", list(g)))), \
        mock.patch.object(m.os, "setgid", lambda g: order.append(("gid", g))), \
        mock.patch.object(m.os, "setuid", lambda u: order.append(("uid", u))):
    m._git("/repo/install", "status", "--porcelain", owner=(1000, 1001))
    kw = captured["kw"]
    kw["preexec_fn"]()
check("O2 git is an argument list, never a shell string", captured["argv"] == ["git", "status", "--porcelain"] and not kw.get("shell"))
check("O2b HOME comes from the owner's passwd entry; prompts disabled",
      kw["env"]["HOME"] == "/srv/owner-home" and kw["env"]["GIT_TERMINAL_PROMPT"] == "0")
check("O2c demotion order: supplementary groups, gid (the .git gid), then uid",
      order == [("groups", [1001, 50]), ("gid", 1001), ("uid", 1000)], str(order))
with mock.patch.object(m.subprocess, "run", fake_run):
    m._git("/repo/install", "status", owner=None)
check("O2d no owner -> no preexec_fn", "preexec_fn" not in captured["kw"])


def no_entry(uid):
    raise KeyError(uid)


with mock.patch("pwd.getpwuid", no_entry):
    try:
        m._git("/x", "status", owner=(4242, 4242))
        raised = False
    except m.GitOwnerError:
        raised = True
check("O3 owner without a passwd entry -> GitOwnerError", raised)

e = Env()
real_run_as = m._run_as
seen_owners = []


def run_as_spy(owner, argv, cwd):
    seen_owners.append((owner, argv[:2]))
    return real_run_as(None, argv, cwd)       # cannot setuid in CI: run unprivileged


with mock.patch.object(m, "_git_owner", lambda st: (4242, 4343)), \
        mock.patch.object(m, "_ownership_offenders", lambda *a, **k: []), \
        mock.patch.object(m, "_run_as", run_as_spy):
    e.push_upstream()
    ok, cli, out = run_us(e)
git_calls = [x for x in seen_owners if x[1][0] == "git"]
check("O4 every git call of the script update carries the repository owner",
      ok is True and git_calls and all(o == (4242, 4343) for o, _ in git_calls), str(git_calls[:3]))
e.close()

e = Env()
with mock.patch.object(m, "_git_owner", lambda st: (4242, 4343)), \
        mock.patch("pwd.getpwuid", no_entry):
    ok, cli, out = run_us(e)
check("O5 missing passwd entry -> False, 11, no crash", ok is False and rcu(cli) == 11 and "passwd" in out, out[-200:])
e.close()

# preflight: root-owned leftovers are detected even if os.access says yes
e = Env()
e.push_upstream()
calls_fetch = []


def spy_git(cwd, *a, owner=None):
    calls_fetch.append(a[0] if a else "")
    return real_git(cwd, *a, owner=None)


with mock.patch.object(m, "_git_owner", lambda st: (4242, 4343)), \
        mock.patch.object(m, "_git", spy_git), \
        mock.patch.object(m, "_ownership_offenders", lambda *a, **k: ["/repo/.git/objects/ab/cd"]):
    ok, cli, out = run_us(e)
check("O6 offenders found -> False, 11, chown hint, nothing fetched or pulled",
      ok is False and rcu(cli) == 11 and "chown -R" in out
      and "fetch" not in calls_fetch and "pull" not in calls_fetch, f"{calls_fetch} {out[-200:]}")
e.close()

access_calls = []
root_owned_file = "/repo/install/scripts/a.sh"


def lstat_fake(path):
    return types.SimpleNamespace(st_uid=0 if path == root_owned_file else 4242)


def child_reports(owner, argv, cwd):
    # the demoted child says which of the passed paths it cannot write
    return types.SimpleNamespace(returncode=0, stdout="\n".join(p for p in argv[4:] if p == root_owned_file), stderr="")


with mock.patch.object(m.os, "access", lambda *a, **k: (access_calls.append(a), True)[1]), \
        mock.patch.object(m, "_run_as", child_reports):
    bad = m._ownership_offenders((4242, 4343), "/nonexistent/.git", "/repo", ["install/scripts/a.sh"], lstat_fn=lstat_fake)
check("O7 a root-owned tracked file is detected although os.access() would say writable",
      bad == [root_owned_file] and not access_calls, f"{bad} {access_calls}")

with mock.patch.object(m, "_run_as", lambda o, a, c: types.SimpleNamespace(returncode=0, stdout="", stderr="")):
    bad = m._ownership_offenders((4242, 4343), "/nonexistent/.git", "/repo", ["install/scripts/a.sh"], lstat_fn=lstat_fake)
check("O7b foreign-owned but group-writable (child says writable) -> not an offender", bad == [], str(bad))

# install/ sits below the repository root: feed the real `git ls-files` output
# produced from the install directory, exactly as the update command does.
e = Env()
listing = sh(e.install, "git", "ls-files", "-z", "--full-name")
tracked_real = [t for t in listing.split("\0") if t]
target = os.path.join(e.work, "install", "scripts", "a.sh")


def lstat_real(path):
    return types.SimpleNamespace(st_uid=0 if path == target else 4242)


def child_real(owner, argv, cwd):
    return types.SimpleNamespace(returncode=0, stdout="\n".join(p for p in argv[4:] if p == target), stderr="")


with mock.patch.object(m, "_run_as", child_real):
    bad = m._ownership_offenders((4242, 4343), os.path.join(e.work, ".git"), e.work, tracked_real, lstat_fn=lstat_real)
check("O8 root-owned file below a repository prefix is found from real ls-files output",
      "install/scripts/a.sh" in tracked_real and bad == [target], f"{tracked_real} {bad}")
e.close()

batches = []


def count_batches(owner, argv, cwd):
    batches.append(len(argv) - 4)
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


many = [f"/repo/dir/file-{i:06d}" for i in range(5000)]
with mock.patch.object(m, "_run_as", count_batches):
    m._not_writable_by((4242, 4343), many, "/repo")
check("O9 a large path list is split into several bounded child calls",
      len(batches) > 1 and sum(batches) == len(many) and max(batches) <= 2000, str(batches))

e = Env()
ls_args = []


def spy_ls(cwd, *a, owner=None):
    if a and a[0] == "ls-files":
        ls_args.append(a)
    return real_git(cwd, *a, owner=None)


with mock.patch.object(m, "_git_owner", lambda st: (4242, 4343)), \
        mock.patch.object(m, "_git", spy_ls), \
        mock.patch.object(m, "_ownership_offenders", lambda *a, **k: ["x"]):
    run_us(e)
check("O10 the tracked-file listing asks git for repository-root-relative paths",
      any("--full-name" in a and "-z" in a for a in ls_args), str(ls_args))
e.close()

# A tracked directory the user deleted locally must not be reported.
e = Env()
commit_file(e.work, "install/sub/x.sh", "x\n", "sub")
shutil.rmtree(os.path.join(e.work, "install", "sub"))
listing = sh(e.install, "git", "ls-files", "-z", "--full-name")
tracked_del = [t for t in listing.split("\0") if t]


def child_missing(owner, argv, cwd):
    # a real child cannot write a path that does not exist
    return types.SimpleNamespace(returncode=0, stdout="\n".join(p for p in argv[4:] if not os.path.exists(p)), stderr="")


with mock.patch.object(m, "_run_as", child_missing):
    bad = m._ownership_offenders((os.getuid(), os.getgid()), os.path.join(e.work, ".git"), e.work, tracked_del)
check("O11 a tracked directory deleted locally is not an ownership offender",
      "install/sub/x.sh" in tracked_del and not any(b.startswith(os.path.join(e.work, "install", "sub")) for b in bad), str(bad))
e.close()

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
os.chdir(START_CWD)
sys.exit(1 if failed else 0)
