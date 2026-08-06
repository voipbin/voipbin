#!/usr/bin/env python3
"""Behavioral test for the billing-manager/ai-manager replica>1 startup guard
added in Phase 1 (address externalization) of the horizontal-scale-architecture
design (docs/plans/2026-07-05-production-grade-horizontal-scale-design.md §1.4).

Contracts:
  G1: replica count 1 for billing-manager/ai-manager -> no warning printed
  G2: replica count >1 for billing-manager -> warning printed, names the service
  G3: replica count >1 for ai-manager -> warning printed, names the service
  G4: other services with replica count >1 (e.g. call-manager) -> NOT warned
      (this guard is scoped to the two named services only, not a blanket
      "warn on any N>1" — per the design's explicit non-goal)
  G5: empty/garbage `docker compose ps` output -> guard returns cleanly,
      no exception (defensive: must not crash cmd_start)

Run: /tmp/vbcli-venv/bin/python scripts/tests/test_ticker_replica_guard.py <worktree>
"""
import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stdout

WORKTREE = sys.argv[1] if len(sys.argv) > 1 else "."
CLI_PATH = os.path.join(WORKTREE, "scripts", "voipbin-cli.py")

sys.argv = ["voipbin", "__noop__"]
spec = importlib.util.spec_from_file_location("vbcli_guard", CLI_PATH)
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
    cli = m.VoIPBinCLI.__new__(m.VoIPBinCLI)
    cli.config = {"project_dir": "."}
    return cli


def compose_ps_json(service_counts):
    """Build a fake `docker compose ps --format json` NDJSON output."""
    lines = []
    for svc, n in service_counts.items():
        for i in range(n):
            lines.append(json.dumps({"Service": svc, "Name": f"{svc}-{i}", "Health": "", "State": "running"}))
    return "\n".join(lines)


def run_guard_with_fake_ps(cli, ps_output):
    captured = io.StringIO()
    orig_run_cmd = m.run_cmd
    m.run_cmd = lambda cmd, **kw: ps_output
    try:
        with redirect_stdout(captured):
            cli._check_ticker_replica_guard()
    finally:
        m.run_cmd = orig_run_cmd
    return captured.getvalue()


# G1: N=1 for both risky services -> no warning
cli = mkcli()
out = run_guard_with_fake_ps(cli, compose_ps_json({"billing-manager": 1, "ai-manager": 1, "call-manager": 3}))
check("G1: replica=1 for billing/ai-manager prints no warning", "WARNING" not in out, out)

# G2: billing-manager N=3 -> warning naming billing-manager
cli = mkcli()
out = run_guard_with_fake_ps(cli, compose_ps_json({"billing-manager": 3}))
check("G2: billing-manager N=3 triggers warning", "WARNING" in out and "billing-manager" in out, out)

# G3: ai-manager N=2 -> warning naming ai-manager
cli = mkcli()
out = run_guard_with_fake_ps(cli, compose_ps_json({"ai-manager": 2}))
check("G3: ai-manager N=2 triggers warning", "WARNING" in out and "ai-manager" in out, out)

# G4: call-manager N=5 (not a risky service) -> no warning
cli = mkcli()
out = run_guard_with_fake_ps(cli, compose_ps_json({"call-manager": 5}))
check("G4: call-manager N=5 does NOT trigger warning (scoped to 2 named services)", "WARNING" not in out, out)

# G5: garbage/empty ps output -> no crash
cli = mkcli()
try:
    out = run_guard_with_fake_ps(cli, "not json at all\n{{{broken")
    check("G5: garbage ps output does not raise", True)
except Exception as e:
    check("G5: garbage ps output does not raise", False, str(e))

cli = mkcli()
try:
    out = run_guard_with_fake_ps(cli, "")
    check("G5b: empty ps output does not raise", True)
except Exception as e:
    check("G5b: empty ps output does not raise", False, str(e))

# --- summary ---
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
