#!/usr/bin/env python3
"""T7/T8: live backup/restore round-trip against the voipbin-test project.

Uses the real _do_backup / cmd_restore code paths (imported module, real
docker), with config pointed at the test containers. No root needed for
these paths (they only shell out to docker).

T7: seed a marker row -> backup -> verify artifacts + manifest + gunzip -t
T8: mutate DB (drop the marker) -> restore -> marker row is back, redis flushed
"""
import importlib.util, os, sys, subprocess, json, glob

WORKTREE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
CLI = os.path.join(WORKTREE, "scripts", "voipbin-cli.py")
DB = "voipbin-test-db-1"
REDIS = "voipbin-test-redis-1"

# cmd_restore's stopped-services guard runs `docker compose ps`; point compose
# at the SAME project the test containers belong to.
os.environ["COMPOSE_PROJECT_NAME"] = "voipbin-test"
os.environ["COMPOSE_FILE"] = (
    os.path.join(WORKTREE, "docker-compose.yml")
    + os.pathsep
    + os.path.join(WORKTREE, "docker-compose.test.yml")
)
os.chdir(WORKTREE)

sys.argv = ["voipbin", "__noop__"]
spec = importlib.util.spec_from_file_location("vbcli", CLI)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

cli = m.VoIPBinCLI.__new__(m.VoIPBinCLI)
cli.config = {
    "project_dir": WORKTREE,
    "db_container": DB,
    "redis_container": REDIS,
    "db_password": os.environ.get("MYSQL_ROOT_PASSWORD", "root_password"),
}

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" ({detail})" if detail and not cond else ""))

def mysql(q):
    # Password expands INSIDE the container from its own MYSQL_ROOT_PASSWORD
    # env var (injected via docker-compose.yml), never on this process's own
    # docker exec argv (visible to any local user on the host via `ps aux`) -
    # matches the idiom voipbin-cli.py's _do_backup/cmd_restore use (see
    # commit 42b9d60). $0 is a throwaway arg name; the query itself is passed
    # positionally as $1 rather than interpolated into the shell string.
    return subprocess.run(
        ["docker", "exec", DB, "sh", "-c",
         'exec mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-root_password}" -N -e "$1"', "_", q],
        capture_output=True, text=True).stdout.strip()

# make the test project's compose ps see only db/redis running
# (cmd_restore checks 'docker compose ps' in project_dir — worktree compose
# project has no running containers, so the stopped-services gate passes)

# --- seed marker ---
mysql("CREATE TABLE IF NOT EXISTS bin_manager.t7_marker (id INT PRIMARY KEY, note VARCHAR(64));"
      "REPLACE INTO bin_manager.t7_marker VALUES (1,'pre-backup-state');")
subprocess.run(["docker", "exec", REDIS, "redis-cli", "SET", "t8stale", "should-be-flushed"],
               capture_output=True)

# --- T7 backup ---
ts = cli._do_backup(WORKTREE)
check("T7 backup returned ts", bool(ts), str(ts))
bdir = os.path.join(WORKTREE, "backups", ts or "")
check("T7 mysql.sql.gz exists+nonempty",
      ts and os.path.getsize(os.path.join(bdir, "mysql.sql.gz")) > 10000)
rc = subprocess.run(["gunzip", "-t", os.path.join(bdir, "mysql.sql.gz")]).returncode
check("T7 gunzip -t passes", rc == 0)
man = json.load(open(os.path.join(bdir, "manifest.json")))
_lock = json.load(open(os.path.join(WORKTREE, "versions.lock")))
_man_commit = man.get("versions_lock_target_commit") or man.get("target_commit")
check("T7 manifest target_commit MATCHES versions.lock",
      bool(_man_commit) and _man_commit == _lock.get("target_commit"),
      f"manifest={_man_commit!r} lock={_lock.get('target_commit')!r}")
check("T7 dir mode 700", oct(os.stat(bdir).st_mode & 0o777) == "0o700", oct(os.stat(bdir).st_mode & 0o777))
gz = os.path.join(bdir, "mysql.sql.gz")
check("T7 file mode 600", oct(os.stat(gz).st_mode & 0o777) == "0o600", oct(os.stat(gz).st_mode & 0o777))
check("T7 config/.env or versions.lock copied",
      os.path.exists(os.path.join(bdir, "config", "versions.lock")))

# --- mutate: simulate post-backup damage ---
mysql("UPDATE bin_manager.t7_marker SET note='DAMAGED' WHERE id=1;")
assert mysql("SELECT note FROM bin_manager.t7_marker WHERE id=1;") == "DAMAGED"

# --- T8a: the stopped-services guard FIRES while rabbitmq is still up ---
pre = mysql("SELECT note FROM bin_manager.t7_marker WHERE id=1;")
cli.cmd_restore([ts, "--force"])  # rabbitmq running -> guard must refuse
post = mysql("SELECT note FROM bin_manager.t7_marker WHERE id=1;")
check("T8a guard refuses restore while rabbitmq is running (no import happened)",
      pre == post == "DAMAGED", f"pre={pre} post={post}")

# --- T8 restore (needs --force; stop rabbitmq so only db/redis remain) ---
subprocess.run(["docker", "stop", "voipbin-test-rabbitmq-1"], capture_output=True)
cli.cmd_restore([ts, "--force"])
subprocess.run(["docker", "start", "voipbin-test-rabbitmq-1"], capture_output=True)
note = mysql("SELECT note FROM bin_manager.t7_marker WHERE id=1;")
check("T8 marker row restored to pre-backup state", note == "pre-backup-state", note)
stale = subprocess.run(["docker", "exec", REDIS, "redis-cli", "GET", "t8stale"],
                       capture_output=True, text=True).stdout.strip()
check("T8 redis FLUSHALL removed stale key", stale == "", repr(stale))

print()
failed = [r for r in results if not r[1]]
print(f"{len(results)-len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
