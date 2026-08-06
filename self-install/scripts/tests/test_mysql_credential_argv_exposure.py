#!/usr/bin/env python3
"""Regression test for 42b9d60: MySQL root password host-argv exposure.

f155290 introduced `docker exec voipbin-db mysql ... -p"${MYSQL_ROOT_PASSWORD:-root_password}"`
in scripts/start.sh and scripts/init_database.sh. That naive substitution
expands `${MYSQL_ROOT_PASSWORD}` in the HOST shell *before* `docker exec`
execs, so the real password lands in the `docker exec` process's argv --
visible to any local user on the box via `ps aux` / `ps -ef` / `/proc/<pid>/cmdline`
for as long as the command runs.

42b9d60 fixed this by wrapping the mysql invocation in `sh -c '...'`
(single-quoted at the point START_MYSQL_IN_DB is *defined*, so the
`${MYSQL_ROOT_PASSWORD:-root_password}` text stays literal on the host and
is only expanded by `sh` *inside the container*, against the container's
own env).

This test does NOT hand-copy that invocation -- it extracts the actual
`wait_for_database()` docker-exec line and the actual `START_MYSQL_IN_DB`
value straight out of scripts/start.sh at run time, so it tracks the real
fixed code path and would also faithfully reproduce a regression if
someone reverted the fix. It:

  1. Starts a disposable, uniquely-named MySQL container (never the live
     sandbox's `voipbin-db`) with a freshly generated, non-literal test
     password.
  2. Runs the extracted invocation with MYSQL_ROOT_PASSWORD exported in
     the host env (as it realistically would be after `source .env`),
     substituting `SELECT 1` for a `SELECT SLEEP(N)` so there's a window
     to observe the running process.
  3. While that command is in flight, repeatedly scans every host
     process's /proc/<pid>/cmdline (falls back to `ps -eo args` if /proc
     is unavailable) and asserts the test password string never appears.
  4. Asserts the query actually completed successfully (return code 0),
     proving the fix doesn't "fix" the leak by quietly breaking auth.
"""
import glob
import os
import re
import secrets
import subprocess
import sys
import time

WORKTREE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
START_SH = os.path.join(WORKTREE, "scripts", "start.sh")
CONTAINER = "voipbin-credtest-db"
IMAGE = "mysql:8.0"
SLEEP_SECONDS = 4

# Distinctive + generated (not a fixed literal), so this doesn't itself read
# as a hardcoded credential and can't collide with any real password.
TEST_PASSWORD = "credleaktest_" + secrets.token_hex(20)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" ({detail})" if detail and not cond else ""))


def extract_wait_for_database_invocation(src):
    """Pull the real docker-exec line out of start.sh's wait_for_database()."""
    idx = src.index("wait_for_database()")
    body = src[idx: idx + 2000]
    m = re.search(r"docker exec voipbin-db (.*?) &>/dev/null", body)
    if not m:
        raise AssertionError(
            "could not find the docker exec invocation inside wait_for_database() "
            "in scripts/start.sh -- has the function been restructured?"
        )
    return m.group(1)


def extract_start_mysql_in_db(src):
    m = re.search(r"^START_MYSQL_IN_DB='([^\n]*)'\s*$", src, re.M)
    return m.group(1) if m else None


def scan_host_processes_for(needle):
    """Return the first offending cmdline containing `needle`, or None."""
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if needle in text:
            return f"{cmdline_path}: {text.strip()}"
    # Fallback for environments without a readable /proc (unlikely on Linux CI).
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        if needle in out:
            return "ps -eo args: <redacted match>"
    except FileNotFoundError:
        pass
    return None


def cleanup():
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


cleanup()  # in case a previous run left the container behind

try:
    # --- start a disposable MySQL container with the generated test password ---
    run = subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", f"MYSQL_ROOT_PASSWORD={TEST_PASSWORD}",
            IMAGE,
        ],
        capture_output=True, text=True,
    )
    check("test MySQL container started", run.returncode == 0, run.stderr.strip())

    # --- wait for it to accept connections ---
    # `mysqladmin ping` can succeed against the entrypoint's *temporary*
    # bootstrap server before the real mysqld is up, giving a false-positive
    # "ready" a moment before the final server (re)starts and briefly refuses
    # auth. Poll with an actual authenticated query instead.
    ready = False
    for _ in range(90):
        p = subprocess.run(
            ["docker", "exec", CONTAINER, "mysql", "-uroot", f"-p{TEST_PASSWORD}", "-e", "SELECT 1"],
            capture_output=True, text=True,
        )
        if p.returncode == 0:
            ready = True
            break
        time.sleep(1)
    check("test MySQL container became ready", ready)

    # --- extract the real fixed-code invocation from start.sh ---
    src = open(START_SH).read()
    invocation = extract_wait_for_database_invocation(src)
    start_mysql_in_db = extract_start_mysql_in_db(src)

    check(
        "wait_for_database() invocation does not embed a raw ${MYSQL_ROOT_PASSWORD} "
        "expansion directly in the docker-exec argv (i.e. it delegates to sh -c)",
        "sh -c" in invocation,
        invocation,
    )

    query_invocation = invocation.replace("SELECT 1", f"SELECT SLEEP({SLEEP_SECONDS})")
    full_cmd = f"docker exec {CONTAINER} {query_invocation}"

    env = os.environ.copy()
    env["MYSQL_ROOT_PASSWORD"] = TEST_PASSWORD  # realistic: exported host-side after `source .env`
    if start_mysql_in_db is not None:
        env["START_MYSQL_IN_DB"] = start_mysql_in_db

    proc = subprocess.Popen(
        ["bash", "-c", full_cmd],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    leak = None
    deadline = time.time() + SLEEP_SECONDS + 5
    while proc.poll() is None and time.time() < deadline:
        found = scan_host_processes_for(TEST_PASSWORD)
        if found:
            leak = found
            break
        time.sleep(0.1)

    stdout, stderr = proc.communicate(timeout=15)

    check(
        "test password never appears in any host process argv while the "
        "mysql invocation is running",
        leak is None,
        leak or "",
    )
    check(
        "the mysql query still succeeds (fix doesn't just hide the leak by "
        "breaking auth)",
        proc.returncode == 0,
        f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}",
    )

finally:
    cleanup()

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
