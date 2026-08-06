#!/usr/bin/env python3
"""Pins voipbin-cli.py's _compose_project_name() to the same rule as
scripts/common.sh's derive_compose_project_name() (bash), so the two
implementations can't silently drift apart again (Task 8, project-name
derivation centralization).

Rule (both implementations): COMPOSE_PROJECT_NAME env var wins when set
(validated as ^[a-z0-9][a-z0-9_-]*$, used as-is — never sanitized, matching
real `docker compose`'s own behavior); otherwise the project directory's
basename, lowercased, filtered to [a-z0-9_-], with any leading '-'/'_'
stripped.

Run: python3 scripts/tests/test_compose_project_name.py <worktree>
"""
import importlib.util
import os
import sys

WORKTREE = sys.argv[1] if len(sys.argv) > 1 else "."
CLI_PATH = os.path.join(WORKTREE, "scripts", "voipbin-cli.py")

sys.argv = ["voipbin", "__noop__"]
spec = importlib.util.spec_from_file_location("vbcli_project_name", CLI_PATH)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def mkcli():
    # __new__ (not VoIPBinCLI()) — skips __init__'s Config() file I/O, which
    # would otherwise read/write the developer's real ~/.voipbin-cli.conf.
    return m.VoIPBinCLI.__new__(m.VoIPBinCLI)


cli = mkcli()
os.environ.pop("COMPOSE_PROJECT_NAME", None)

# --- basename-derivation cases (mirrors tests/compose-project-name.bats) ---
check(
    "plain lowercase basename passes through unchanged",
    cli._compose_project_name("/tmp/sandbox") == "sandbox",
    cli._compose_project_name("/tmp/sandbox"),
)
check(
    "renamed checkout directory derives its own basename",
    cli._compose_project_name("/tmp/self-install") == "self-install",
    cli._compose_project_name("/tmp/self-install"),
)
check(
    "uppercase basename is lowercased",
    cli._compose_project_name("/tmp/Self-Install") == "self-install",
    cli._compose_project_name("/tmp/Self-Install"),
)
check(
    "leading dash is stripped after lowercasing",
    cli._compose_project_name("/tmp/-Self-Install") == "self-install",
    cli._compose_project_name("/tmp/-Self-Install"),
)
check(
    "leading underscore is stripped",
    cli._compose_project_name("/tmp/_self_install") == "self_install",
    cli._compose_project_name("/tmp/_self_install"),
)
check(
    "mixed-case with disallowed characters is filtered and stripped",
    cli._compose_project_name("/tmp/--My_Weird.Dir!!") == "my_weirddir",
    cli._compose_project_name("/tmp/--My_Weird.Dir!!"),
)

# --- COMPOSE_PROJECT_NAME env var precedence ---
os.environ["COMPOSE_PROJECT_NAME"] = "sandbox"
check(
    "COMPOSE_PROJECT_NAME env var takes precedence over basename",
    cli._compose_project_name("/tmp/some-other-dir") == "sandbox",
    cli._compose_project_name("/tmp/some-other-dir"),
)
del os.environ["COMPOSE_PROJECT_NAME"]

os.environ["COMPOSE_PROJECT_NAME"] = "Invalid Name!"
try:
    cli._compose_project_name("/tmp/whatever")
    check("invalid COMPOSE_PROJECT_NAME raises ValueError", False, "no exception raised")
except ValueError:
    check("invalid COMPOSE_PROJECT_NAME raises ValueError", True)
del os.environ["COMPOSE_PROJECT_NAME"]

# --- basename that filters down to empty (mirrors the bats case) ---
# Must raise, not silently fall back to a fixed name like "voipbin" — that
# fallback used to mask exactly this kind of degenerate input.
os.environ.pop("COMPOSE_PROJECT_NAME", None)
try:
    result = cli._compose_project_name("/tmp/---")
    check(
        "basename filtering down to empty raises ValueError",
        False,
        f"no exception raised, returned {result!r}",
    )
except ValueError:
    check("basename filtering down to empty raises ValueError", True)

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
