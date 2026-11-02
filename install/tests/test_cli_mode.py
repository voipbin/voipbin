#!/usr/bin/env python3
"""Tests for the CLI/SIP-tooling mode awareness, the guided
`install` command, exit-code propagation, clean confirmation and ownership.

Plain asserts, no pytest dependency; run directly:

    python3 tests/test_cli_mode.py

voipbin-cli.py is imported as a module via importlib (import does not call
check_root) and pointed at a fixture tree via the VOIPBIN_PROJECT_DIR env
override (Config.get honors VOIPBIN_<KEY>; Config.set is never used because
it writes the developer's real ~/.voipbin-cli.conf).

Isolation: docker/dig/curl/getent/openssl/mkcert are stubbed on PATH so no
handler can reach the real daemon, resolver or network; every stub logs its
invocation so "never invoked" is assertable.
"""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import stat
import sys
import tempfile

# voipbin-cli.py imports yaml at module level; without it there is nothing to
# test (the CLI itself cannot run either).
try:
    import yaml  # noqa: F401
except ImportError:
    print("SKIP: PyYAML not installed (voipbin-cli.py imports yaml at module level)")
    sys.exit(0)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")

STUB_COMMANDS = ["docker", "dig", "curl", "getent", "openssl", "mkcert"]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_env(project_dir, lines):
    with open(os.path.join(project_dir, ".env"), "w") as f:
        f.write("\n".join(lines) + "\n")


def make_stub_bin(tmp_dir):
    """PATH-prepended stubs that log every invocation to stub.log."""
    stub_bin = os.path.join(tmp_dir, "stub_bin")
    os.makedirs(stub_bin, exist_ok=True)
    log_path = os.path.join(tmp_dir, "stub.log")
    for cmd in STUB_COMMANDS:
        path = os.path.join(stub_bin, cmd)
        with open(path, "w") as f:
            f.write(
                "#!/bin/bash\n"
                f'echo "{cmd.upper()}:$*" >> "{log_path}"\n'
                "exit 0\n"
            )
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_bin, log_path


def stub_log(log_path):
    try:
        with open(log_path) as f:
            return f.read()
    except OSError:
        return ""


def capture(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


INTERNAL_ENV = [
    "DOMAIN_MODE=internal",
    "COMPOSE_PROFILES=internal-dns",
    "TLS_MODE=mkcert",
    "BASE_DOMAIN=voipbin.test",
    "HOST_EXTERNAL_IP=192.168.1.100",
    "KAMAILIO_EXTERNAL_IP=192.168.1.108",
    "DOMAIN_NAME_EXTENSION=registrar.voipbin.test",
]

EXTERNAL_ENV = [
    "DOMAIN_MODE=external",
    "COMPOSE_PROFILES=",
    "TLS_MODE=byo",
    "BASE_DOMAIN=example.com",
    "HOST_EXTERNAL_IP=203.0.113.10",
    "KAMAILIO_EXTERNAL_IP=203.0.113.11",
    "DOMAIN_NAME_EXTENSION=registrar.example.com",
]


def make_cli(tmp_dir, env_lines, cli_module):
    project_dir = os.path.join(tmp_dir, "project")
    os.makedirs(project_dir, exist_ok=True)
    write_env(project_dir, env_lines)
    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir
    return cli_module.VoIPBinCLI(), project_dir


def test_dns_external_gate(cli_module, tmp_dir, log_path):
    cli, _ = make_cli(tmp_dir, EXTERNAL_ENV, cli_module)

    for subcmd in ["status", "list", "setup", "test", "regenerate"]:
        if os.path.exists(log_path):
            os.remove(log_path)
        out = capture(cli.cmd_dns, [subcmd])
        assert "DNS is operator-managed in external mode" in out, (subcmd, out)
        # The design §2.9 record table with the real domain and IPs
        assert "api.example.com" in out, out
        assert "203.0.113.10" in out, out
        assert "sip.example.com" in out, out
        assert "203.0.113.11" in out, out
        assert "registrar.example.com" in out, out
        assert "*.registrar.example.com" in out, out
        # Apex registrar row exists independently of the wildcard row
        assert "not covered by the wildcard" in out, out
        # No probe or mutation ran: docker/dig/setup scripts never invoked
        assert stub_log(log_path) == "", (subcmd, stub_log(log_path))
    print("ok test_dns_external_gate")


def test_dns_internal_not_gated(cli_module, tmp_dir, log_path):
    cli, _ = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    if os.path.exists(log_path):
        os.remove(log_path)

    out = capture(cli.cmd_dns, ["status"])
    assert "DNS is operator-managed in external mode" not in out, out
    # Internal status probes CoreDNS/resolution as before (stubs invoked)
    assert "DIG:" in stub_log(log_path) or "DOCKER:" in stub_log(log_path)
    print("ok test_dns_internal_not_gated")


def test_certs_external_report(cli_module, tmp_dir, log_path):
    cli, project_dir = make_cli(tmp_dir, EXTERNAL_ENV, cli_module)

    for subcmd in ["status", "trust"]:
        out = capture(cli.cmd_certs, [subcmd])
        assert "external mode" in out.lower(), out
        assert "install-certs.sh" in out, out
        # Destructive advice must never appear for a BYO install
        assert "rm -rf" not in out, out
        assert "mkcert -install" not in out, out
    print("ok test_certs_external_report")


def test_certs_external_with_cert_reports_expiry_and_sans(cli_module, tmp_dir, log_path):
    cli, project_dir = make_cli(tmp_dir, EXTERNAL_ENV, cli_module)
    cert_dir = os.path.join(project_dir, "certs", "api")
    os.makedirs(cert_dir, exist_ok=True)
    with open(os.path.join(cert_dir, "cert.pem"), "w") as f:
        f.write("dummy-pem\n")
    if os.path.exists(log_path):
        os.remove(log_path)

    out = capture(cli.cmd_certs, ["status"])
    assert "Expires:" in out, out
    # Expiry/SAN inspection goes through openssl (stub records the calls)
    assert "OPENSSL:" in stub_log(log_path), stub_log(log_path)
    assert "install-certs.sh" in out, out
    assert "rm -rf" not in out, out
    print("ok test_certs_external_with_cert_reports_expiry_and_sans")


def test_certs_internal_not_gated(cli_module, tmp_dir, log_path):
    cli, _ = make_cli(tmp_dir, INTERNAL_ENV, cli_module)

    out = capture(cli.cmd_certs, ["status"])
    assert "external mode" not in out.lower(), out
    print("ok test_certs_internal_not_gated")


def test_status_urls_from_base_domain(cli_module, tmp_dir, log_path):
    cli, project_dir = make_cli(tmp_dir, EXTERNAL_ENV, cli_module)
    # cmd_status needs a non-empty compose ps and reads .env relative to cwd
    stub_bin = os.path.join(tmp_dir, "stub_bin")
    with open(os.path.join(stub_bin, "docker"), "w") as f:
        f.write(
            "#!/bin/bash\n"
            'if [[ "$1" == "compose" && "$2" == "ps" ]]; then\n'
            '    printf "voipbin-db\\tUp 5 minutes\\n"\n'
            "    exit 0\n"
            "fi\n"
            "exit 0\n"
        )
    prev_cwd = os.getcwd()
    os.chdir(project_dir)
    try:
        out = capture(cli.cmd_status, [])
    finally:
        os.chdir(prev_cwd)

    assert "https://api.example.com:8443" in out, out
    assert "http://admin.example.com:3003" in out, out
    assert "*.registrar.example.com" in out, out
    assert "api.voipbin.test" not in out, out
    print("ok test_status_urls_from_base_domain")


def test_test_call_defaults_from_env(tmp_dir):
    project_dir = os.path.join(tmp_dir, "project")
    write_env(project_dir, EXTERNAL_ENV)
    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir
    os.environ.pop("VOIPBIN_SIP_SERVER", None)
    os.environ.pop("VOIPBIN_DOMAIN_NAME_EXTENSION", None)

    mod = load_module("test_call_ext", os.path.join(SCRIPTS_DIR, "test_call.py"))
    assert mod.SIP_SERVER == "sip.example.com", mod.SIP_SERVER
    assert mod.DOMAIN.endswith(".registrar.example.com"), mod.DOMAIN

    # Explicit environment variables still win over .env derivation
    os.environ["VOIPBIN_SIP_SERVER"] = "10.0.0.5"
    os.environ["VOIPBIN_DOMAIN_NAME_EXTENSION"] = "realm.override.test"
    mod = load_module("test_call_override", os.path.join(SCRIPTS_DIR, "test_call.py"))
    assert mod.SIP_SERVER == "10.0.0.5", mod.SIP_SERVER
    assert mod.DOMAIN.endswith(".realm.override.test"), mod.DOMAIN
    os.environ.pop("VOIPBIN_SIP_SERVER", None)
    os.environ.pop("VOIPBIN_DOMAIN_NAME_EXTENSION", None)
    print("ok test_test_call_defaults_from_env")


def test_softphone_defaults_from_env(tmp_dir):
    project_dir = os.path.join(tmp_dir, "project")
    write_env(project_dir, EXTERNAL_ENV)
    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir

    mod = load_module("softphone_ext", os.path.join(SCRIPTS_DIR, "softphone.py"))
    assert mod.default_server() == "sip.example.com", mod.default_server()
    assert mod.default_domain_ext() == "registrar.example.com", mod.default_domain_ext()

    # No .env at all falls back to the historic internal defaults
    os.environ["VOIPBIN_PROJECT_DIR"] = os.path.join(tmp_dir, "empty")
    os.makedirs(os.environ["VOIPBIN_PROJECT_DIR"], exist_ok=True)
    assert mod.default_server() == "sip.voipbin.test", mod.default_server()
    assert mod.default_domain_ext() == "registrar.voipbin.test", mod.default_domain_ext()
    print("ok test_softphone_defaults_from_env")


def test_crlf_env_values_are_trimmed(cli_module, tmp_dir):
    """CRLF-saved .env: no trailing \\r may leak into derived values."""
    project_dir = os.path.join(tmp_dir, "crlf-project")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, ".env"), "w", newline="") as f:
        f.write("\r\n".join(EXTERNAL_ENV) + "\r\n")

    assert cli_module.read_env_file_var(project_dir, "DOMAIN_MODE") == "external"
    assert cli_module.read_env_file_var(project_dir, "BASE_DOMAIN") == "example.com"

    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir
    os.environ.pop("VOIPBIN_SIP_SERVER", None)
    os.environ.pop("VOIPBIN_DOMAIN_NAME_EXTENSION", None)

    sp = load_module("softphone_crlf", os.path.join(SCRIPTS_DIR, "softphone.py"))
    assert sp.default_server() == "sip.example.com", sp.default_server()
    assert sp.default_domain_ext() == "registrar.example.com", sp.default_domain_ext()

    tc = load_module("test_call_crlf", os.path.join(SCRIPTS_DIR, "test_call.py"))
    assert tc.SIP_SERVER == "sip.example.com", tc.SIP_SERVER
    assert tc.DOMAIN.endswith(".registrar.example.com"), tc.DOMAIN
    print("ok test_crlf_env_values_are_trimmed")


def write_doctor_stub(project_dir, exit_code):
    """Write a scripts/doctor.sh stub with the given exit code."""
    scripts_dir = os.path.join(project_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    path = os.path.join(scripts_dir, "doctor.sh")
    with open(path, "w") as f:
        f.write(f"#!/bin/bash\necho DOCTOR-STUB\nexit {exit_code}\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_doctor_registered_and_repl_path_returns(cli_module, tmp_dir, log_path):
    """Doctor scenario 11 (VOIP-1280 design §8): registration in cli.commands
    (guards against the cmd_hook defined-but-unregistered gap) and the REPL
    path storing the REAL exit code without exiting the process."""
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)

    # Registered in all lookup surfaces (the anti-cmd_hook assertion)
    assert "doctor" in cli.commands, sorted(cli.commands)
    assert cli.commands["doctor"] == cli.cmd_doctor
    assert "doctor" in cli.help_text, sorted(cli.help_text)

    # REPL path: handler returns normally (no SystemExit) and stores the real
    # exit code, not a raw shell-out wait status (which would be 256).
    doctor_path = write_doctor_stub(project_dir, 1)
    capture(cli.cmd_doctor, [])
    assert cli.last_doctor_rc == 1, cli.last_doctor_rc

    write_doctor_stub(project_dir, 0)
    capture(cli.cmd_doctor, [])
    assert cli.last_doctor_rc == 0, cli.last_doctor_rc

    # Missing script: existence guard, no exception, environment-error code
    os.remove(doctor_path)
    out = capture(cli.cmd_doctor, [])
    assert "not found" in out.lower(), out
    assert cli.last_doctor_rc == 2, cli.last_doctor_rc
    print("ok test_doctor_registered_and_repl_path_returns")


def test_doctor_noninteractive_dispatch_propagates_exit_code(cli_module, tmp_dir, log_path):
    """The non-interactive `voipbin doctor` path must exit with doctor.sh's
    exit code (part of the machine contract); exit 0 stays a plain return."""
    project_dir = os.path.join(tmp_dir, "project")
    os.makedirs(project_dir, exist_ok=True)
    write_env(project_dir, INTERNAL_ENV)
    prev_project_dir = os.environ.get("VOIPBIN_PROJECT_DIR")
    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir

    orig_check_root = cli_module.check_root
    orig_argv = sys.argv
    prev_cwd = os.getcwd()
    cli_module.check_root = lambda: None
    try:
        write_doctor_stub(project_dir, 1)
        sys.argv = ["voipbin", "doctor"]
        raised = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli_module.main()
        except SystemExit as e:
            raised = e
        assert raised is not None, "main() did not propagate doctor's exit code"
        assert raised.code == 1, raised.code

        # exit 0: no SystemExit, main() returns normally
        os.chdir(prev_cwd)
        write_doctor_stub(project_dir, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            cli_module.main()
    finally:
        cli_module.check_root = orig_check_root
        sys.argv = orig_argv
        os.chdir(prev_cwd)
        if prev_project_dir is None:
            os.environ.pop("VOIPBIN_PROJECT_DIR", None)
        else:
            os.environ["VOIPBIN_PROJECT_DIR"] = prev_project_dir
    print("ok test_doctor_noninteractive_dispatch_propagates_exit_code")


def test_upgrade_pinned_syncs_live_compose_before_pull(cli_module, tmp_dir, log_path):
    """Regression test for the docker-compose.yml.dist split: after
    docker-compose.yml became untracked (install/.gitignore), the pinned
    `update all` flow's `git pull` (step 2) stopped touching the live
    docker-compose.yml the later `docker compose pull`/`up -d` steps
    actually read. Step 2b must run scripts/sync-compose-images.sh with
    COMPOSE_FILE pointed at the LIVE file (not .dist) before step 3, or the
    upgrade silently no-ops while still reporting success."""
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    sync_log = os.path.join(tmp_dir, "sync_calls.log")

    scripts_dir = os.path.join(project_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    sync_stub = os.path.join(scripts_dir, "sync-compose-images.sh")
    with open(sync_stub, "w") as f:
        f.write(
            "#!/bin/bash\n"
            f'echo "SYNC_CALLED COMPOSE_FILE=$COMPOSE_FILE" >> "{sync_log}"\n'
            "exit 0\n"
        )
    os.chmod(sync_stub, os.stat(sync_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # migrate.sh deliberately absent: step 4 aborts cleanly right after step
    # 3, so this test doesn't need to stub the rest of the upgrade too.

    if os.path.exists(log_path):
        os.remove(log_path)

    out = capture(cli._upgrade_pinned, project_dir, False, False, "pull", None)

    assert "SYNC_CALLED" in stub_log(sync_log), stub_log(sync_log)
    expected_compose_file = os.path.join(project_dir, "docker-compose.yml")
    assert f"COMPOSE_FILE={expected_compose_file}" in stub_log(sync_log), stub_log(sync_log)
    # Ordering: step 2b's sync ran, THEN docker compose pull (step 3)
    # succeeded (rc=0 from the "docker" PATH stub - reaching "Step 4/6" at
    # all proves step 3 didn't abort), THEN it stopped at the missing
    # migrate.sh (step 4) rather than silently skipping straight to verify.
    assert "Step 2b/6" in out, out
    assert "Step 3/6" in out, out
    assert "migrate.sh not found" in out, out
    assert out.index("Step 2b/6") < out.index("Step 3/6") < out.index("migrate.sh not found"), out
    print("ok test_upgrade_pinned_syncs_live_compose_before_pull")


def test_tracked_paths_uses_compose_dist_not_live_file(cli_module):
    """docker-compose.yml/versions.lock (no suffix) are gitignored post-split
    and would never show a diff; the .dist files are the committed copies
    that actually change upstream and must be the ones 'update scripts
    --check' reports on."""
    assert "docker-compose.yml.dist" in cli_module.TRACKED_PATHS
    assert "docker-compose.yml" not in cli_module.TRACKED_PATHS
    assert "versions.lock.dist" in cli_module.TRACKED_PATHS
    assert "versions.lock" not in cli_module.TRACKED_PATHS
    print("ok test_tracked_paths_uses_compose_dist_not_live_file")


def test_cmd_update_resume_from_pull_detects_unpin_via_dist_file(cli_module, tmp_dir, log_path):
    """versions.lock.dist split regression: cmd_update's "did the just-pulled
    release unpin this repo" check must read versions.lock.dist (the file
    git pull actually changes), not the live versions.lock (untracked,
    operator-owned, never touched by a pull either way - checking it here
    would never fire post-split)."""
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)

    # Live versions.lock present throughout (pinned repo, both cases below):
    # only versions.lock.dist's presence should decide this branch's outcome.
    with open(os.path.join(project_dir, "versions.lock"), "w") as f:
        f.write("{}")

    calls = []
    orig_upgrade_pinned = cli._upgrade_pinned
    cli._upgrade_pinned = lambda *a, **kw: calls.append(("_upgrade_pinned", a, kw))
    try:
        # Case 1: versions.lock.dist still present in the pulled commit ->
        # falls through to the normal pinned dispatch (reaches
        # _upgrade_pinned with resume_from="pull", does NOT print the
        # unpin warning).
        with open(os.path.join(project_dir, "versions.lock.dist"), "w") as f:
            f.write("{}")
        out = capture(cli.cmd_update, ["all", "--resume-from=pull"])
        assert "is GONE in the new" not in out, out
        assert len(calls) == 1, calls
        assert calls[0][0] == "_upgrade_pinned"
        assert calls[0][2].get("resume_from") == "pull", calls[0]  # resume_from kwarg
        calls.clear()

        # Case 2: versions.lock.dist removed by the pulled commit (release
        # unpinned this repo) -> prints the warning and returns WITHOUT
        # calling _upgrade_pinned.
        os.remove(os.path.join(project_dir, "versions.lock.dist"))
        out = capture(cli.cmd_update, ["all", "--resume-from=pull"])
        assert "versions.lock.dist is GONE in the new" in out, out
        assert "run 'update all' again to use the standard path" in out, out
        assert calls == [], calls
    finally:
        cli._upgrade_pinned = orig_upgrade_pinned
    print("ok test_cmd_update_resume_from_pull_detects_unpin_via_dist_file")


def test_upgrade_pinned_rollback_guidance_uses_scoped_checkout(cli_module, tmp_dir, log_path):
    """VOIP-1333 regression: the verify-failure rollback message used to say
    'git checkout the previous repo commit' with no pathspec, implying a
    scoped revert of install/'s tracked files. In reality install/ is a
    subdirectory of the real repo root, so a bare 'git checkout <commit>'
    reverts the ENTIRE repo tree and leaves the operator in detached HEAD -
    a real, reproduced footgun for a rollback instruction. The guidance
    must now use a pathspec-scoped 'git checkout <commit> -- <paths>',
    which restores just those files' content without touching HEAD."""
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)

    # Force the verify-FAILED branch of _upgrade_pinned's resumed process by
    # making docker compose pull itself fail, so we reach the rollback
    # message without needing to stub migrate.sh/verify_stack too.
    stub_bin = os.path.join(tmp_dir, "stub_bin")
    with open(os.path.join(stub_bin, "docker"), "w") as f:
        f.write("#!/bin/bash\nexit 1\n")

    sync_stub_dir = os.path.join(project_dir, "scripts")
    os.makedirs(sync_stub_dir, exist_ok=True)
    sync_stub = os.path.join(sync_stub_dir, "sync-compose-images.sh")
    with open(sync_stub, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(sync_stub, os.stat(sync_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    out = capture(cli._upgrade_pinned, project_dir, False, False, "pull", None)

    # docker compose pull failing aborts before the rollback message (that
    # message only prints on a verify FAILURE after a successful pull/up),
    # so this only proves Step 2b->3 ordering held; restore a passing
    # docker stub and force verify to fail instead to reach the real text.
    with open(os.path.join(stub_bin, "docker"), "w") as f:
        f.write(
            "#!/bin/bash\n"
            'if [[ "$1" == "compose" && "$2" == "ps" ]]; then exit 0; fi\n'
            "exit 0\n"
        )
    migrate_stub = os.path.join(sync_stub_dir, "migrate.sh")
    with open(migrate_stub, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(migrate_stub, os.stat(migrate_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    cli._verify_stack = lambda project_dir, timeout=120: False

    out = capture(cli._upgrade_pinned, project_dir, False, False, "pull", None)

    assert "Upgrade verification FAILED" in out, out
    assert "git checkout the previous repo commit" not in out, out
    assert "git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/" in out, out
    assert "pathspec-scoped on purpose" in out, out
    assert "detached HEAD" in out, out
    print("ok test_upgrade_pinned_rollback_guidance_uses_scoped_checkout")


def test_upgrade_pinned_migration_failure_guidance_uses_scoped_checkout(cli_module, tmp_dir, log_path):
    """VOIP-1333, third call site: Step 4's migration-failure recovery
    procedure had the same unscoped 'git checkout <previous commit>'."""
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    stub_bin = os.path.join(tmp_dir, "stub_bin")
    with open(os.path.join(stub_bin, "docker"), "w") as f:
        f.write("#!/bin/bash\nexit 0\n")  # compose pull succeeds

    scripts_dir = os.path.join(project_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    sync_stub = os.path.join(scripts_dir, "sync-compose-images.sh")
    with open(sync_stub, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(sync_stub, os.stat(sync_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    migrate_stub = os.path.join(scripts_dir, "migrate.sh")
    with open(migrate_stub, "w") as f:
        f.write("#!/bin/bash\nexit 1\n")  # migration fails
    os.chmod(migrate_stub, os.stat(migrate_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    out = capture(cli._upgrade_pinned, project_dir, False, False, "pull", None)

    assert "migration failed" in out, out
    assert "git checkout <previous commit>   (reverts" not in out, out
    assert "git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/" in out, out
    assert "pathspec-scoped" in out, out
    print("ok test_upgrade_pinned_migration_failure_guidance_uses_scoped_checkout")


def test_upgrade_pinned_compose_up_failure_guidance_uses_scoped_checkout(cli_module, tmp_dir, log_path):
    """VOIP-1333, fourth call site: Step 5's 'docker compose up -d failed'
    recovery line had the same unscoped 'git checkout the previous
    commit'."""
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    stub_bin = os.path.join(tmp_dir, "stub_bin")
    with open(os.path.join(stub_bin, "docker"), "w") as f:
        f.write(
            "#!/bin/bash\n"
            'if [[ "$1" == "compose" && "$2" == "up" ]]; then exit 1; fi\n'
            "exit 0\n"
        )

    scripts_dir = os.path.join(project_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    sync_stub = os.path.join(scripts_dir, "sync-compose-images.sh")
    with open(sync_stub, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(sync_stub, os.stat(sync_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    migrate_stub = os.path.join(scripts_dir, "migrate.sh")
    with open(migrate_stub, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(migrate_stub, os.stat(migrate_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    out = capture(cli._upgrade_pinned, project_dir, False, False, "pull", None)

    assert "docker compose up -d failed" in out, out
    assert "and git checkout the previous commit" not in out, out
    assert "git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/" in out, out
    assert "pathspec-scoped" in out, out
    print("ok test_upgrade_pinned_compose_up_failure_guidance_uses_scoped_checkout")


def test_rollback_pinned_guard_uses_scoped_checkout(cli_module, tmp_dir):
    """VOIP-1333 regression, second call site: cmd_rollback's pinned-repo
    guard had the identical unscoped 'git checkout the previous repo
    commit' footgun."""
    project_dir = os.path.join(tmp_dir, "rollback-project")
    os.makedirs(project_dir, exist_ok=True)
    write_env(project_dir, INTERNAL_ENV)
    with open(os.path.join(project_dir, "versions.lock"), "w") as f:
        f.write("{}")
    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir
    cli = cli_module.VoIPBinCLI()

    out = capture(cli.cmd_rollback, [])

    assert "rollback is disabled on a pinned repo" in out, out
    assert "git checkout the previous repo commit" not in out, out
    assert "git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/" in out, out
    assert "pathspec-scoped on purpose" in out, out
    assert "detached HEAD" in out, out
    print("ok test_rollback_pinned_guard_uses_scoped_checkout")



# =============================================================================
# Guided install, exit codes, clean confirmation, ownership, user-facing text
# =============================================================================

def write_script_stub(project_dir, name, body):
    """scripts/<name> stub (bash). body is the script text after the shebang."""
    scripts_dir = os.path.join(project_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    path = os.path.join(scripts_dir, name)
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def run_main(cli_module, argv, stdin_text=None):
    """Run cli main() with argv (check_root bypassed); returns (rc, stdout)."""
    orig_check_root = cli_module.check_root
    orig_argv, orig_stdin = sys.argv, sys.stdin
    prev_cwd = os.getcwd()
    cli_module.check_root = lambda: None
    sys.argv = ["voipbin"] + list(argv)
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    buf = io.StringIO()
    rc = 0
    try:
        with contextlib.redirect_stdout(buf):
            try:
                cli_module.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        cli_module.check_root = orig_check_root
        sys.argv, sys.stdin = orig_argv, orig_stdin
        os.chdir(prev_cwd)
    return rc, buf.getvalue()


def fresh_project(tmp_dir, name):
    project_dir = os.path.join(tmp_dir, name)
    shutil.rmtree(project_dir, ignore_errors=True)
    os.makedirs(os.path.join(project_dir, "scripts"))
    os.environ["VOIPBIN_PROJECT_DIR"] = project_dir
    return project_dir


def set_docker_stub(stub_bin, log_path, body):
    """Replace the docker stub with one that logs and runs body."""
    path = os.path.join(stub_bin, "docker")
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" f'echo "DOCKER:$*" >> "{log_path}"\n' + body + "\nexit 0\n")
    os.chmod(path, 0o755)


def reset_docker_stub(stub_bin, log_path):
    set_docker_stub(stub_bin, log_path, "")


def last_line(out):
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def test_init_passes_args_and_propagates_exit_code(cli_module, tmp_dir, log_path):
    """init used os.system without arguments and dropped the exit code."""
    project_dir = fresh_project(tmp_dir, "p_init")
    args_log = os.path.join(tmp_dir, "init_args.log")
    write_script_stub(project_dir, "init.sh", f'echo "$@" > "{args_log}"\nexit 2')
    rc, out = run_main(cli_module, ["init", "--yes", "--mode", "internal"])
    assert rc == 2, (rc, out)
    with open(args_log) as f:
        assert f.read().strip() == "--yes --mode internal"
    write_script_stub(project_dir, "init.sh", "exit 0")
    rc, out = run_main(cli_module, ["init"])
    assert rc == 0, (rc, out)
    print("ok test_init_passes_args_and_propagates_exit_code")


def test_start_propagates_exit_code_and_skips_pins_when_locked(cli_module, tmp_dir, log_path):
    project_dir = fresh_project(tmp_dir, "p_start")
    write_script_stub(project_dir, "start.sh", "exit 1")
    with open(os.path.join(project_dir, "versions.lock"), "w") as f:
        f.write("{}\n")
    called = []
    orig = cli_module.resolve_image_tags_parallel
    cli_module.resolve_image_tags_parallel = lambda *a, **k: called.append(a) or []
    try:
        rc, out = run_main(cli_module, ["start"])
    finally:
        cli_module.resolve_image_tags_parallel = orig
    assert rc == 1, (rc, out)
    assert not called, "version pinning ran on a versions.lock install"
    assert "pinning image versions" not in out, out
    print("ok test_start_propagates_exit_code_and_skips_pins_when_locked")


def test_clean_requires_confirmation(cli_module, tmp_dir, stub_bin, log_path):
    reset_docker_stub(stub_bin, log_path)
    project_dir = fresh_project(tmp_dir, "p_clean")
    write_env(project_dir, INTERNAL_ENV)
    for name in ("docker-compose.yml", "versions.lock", ".voipbin-installed"):
        with open(os.path.join(project_dir, name), "w") as f:
            f.write("x\n")
    open(log_path, "w").close()
    # non-interactive without --force: refused, nothing runs
    for opts in (["--volumes"], ["--purge"], ["--all"], ["--images"]):
        rc, out = run_main(cli_module, ["clean"] + opts, stdin_text="")
        assert rc == 1, (opts, rc, out)
        assert "--force" in out, out
    assert "DOCKER:" not in stub_log(log_path), stub_log(log_path)
    assert os.path.exists(os.path.join(project_dir, ".env"))
    # interactive answer other than 'yes': aborted
    cli = cli_module.VoIPBinCLI()
    orig_stdin = sys.stdin

    class TtyInput(io.StringIO):
        def isatty(self):
            return True
    sys.stdin = TtyInput("y\n")
    try:
        out = capture(cli.cmd_clean, ["--volumes"])
    finally:
        sys.stdin = orig_stdin
    assert cli.last_rc == 1 and "Aborted" in out, out
    assert "DOCKER:" not in stub_log(log_path)
    # --purge --force: the purge list matches clean.sh (compose, lock, marker)
    rc, out = run_main(cli_module, ["clean", "--purge", "--force"], stdin_text="")
    assert rc == 0, (rc, out)
    for name in (".env", "docker-compose.yml", "versions.lock", ".voipbin-installed"):
        assert not os.path.exists(os.path.join(project_dir, name)), name
    assert "WARNING: the database volume stays" in out, out
    # --volumes --force removes the install marker with the data
    with open(os.path.join(project_dir, ".voipbin-installed"), "w") as f:
        f.write("{}\n")
    rc, out = run_main(cli_module, ["clean", "--volumes", "--force"], stdin_text="")
    assert rc == 0, (rc, out)
    assert "DOCKER:compose down -v" in stub_log(log_path), (stub_log(log_path), out)
    # kept VoIP interfaces: the hint to remove them before installing again
    assert "clean --network --force" not in out or os.path.exists("/sys/class/net/kamailio-int"), out
    orig_ifaces = cli_module.INSTALL_VOIP_INTERFACES
    cli_module.INSTALL_VOIP_INTERFACES = ("lo",)  # present on every Linux host
    try:
        rc, out = run_main(cli_module, ["clean", "--volumes", "--force"], stdin_text="")
        assert rc == 0 and "sudo ./voipbin clean --network --force" in out, out
        rc, out = run_main(cli_module, ["clean", "--network", "--force"], stdin_text="")
        assert "VoIP network interfaces were kept" not in out, out
        rc, out = run_main(cli_module, ["clean", "--containers"], stdin_text="")
        assert "VoIP network interfaces were kept" not in out, "hint without removing the compose network"
    finally:
        cli_module.INSTALL_VOIP_INTERFACES = orig_ifaces
    assert not os.path.exists(os.path.join(project_dir, ".voipbin-installed"))
    # options that destroy no data still run without a question
    rc, out = run_main(cli_module, ["clean", "--containers"], stdin_text="")
    assert rc == 0, (rc, out)
    print("ok test_clean_requires_confirmation")


def install_docker_stub(stub_bin, log_path, volumes="", info_rc=0):
    set_docker_stub(stub_bin, log_path, f'''
if [[ "$1" == "info" ]]; then exit {info_rc}; fi
if [[ "$1 $2" == "volume ls" ]]; then printf "{volumes}"; exit 0; fi
if [[ "$1 $2 $3" == "compose config --services" ]]; then printf "db\\nredis\\napi-manager\\n"; exit 0; fi
''')


def write_install_stubs(project_dir, tmp_dir, start_rc=0, admin_rc=0):
    calls = os.path.join(tmp_dir, "install_calls.log")
    open(calls, "w").close()
    write_script_stub(project_dir, "doctor.sh",
                      f'echo doctor >> "{calls}"\necho "DOCTOR docker: pass ok"\necho "DOCTOR env-file: fail no .env"\nexit 1')
    write_script_stub(project_dir, "init.sh", f'''echo "init $*" >> "{calls}"
cat > .env <<EOF
DOMAIN_MODE=internal
BASE_DOMAIN=voipbin.test
HOST_EXTERNAL_IP=192.168.1.100
EOF
touch docker-compose.yml versions.lock
exit 0''')
    write_script_stub(project_dir, "setup-host.sh", f'echo setup-host >> "{calls}"\nexit 0')
    write_script_stub(project_dir, "start.sh", f'echo start >> "{calls}"\nexit {start_rc}')
    write_script_stub(project_dir, "create-admin.sh", f'''echo "create-admin $*" >> "{calls}"
[[ ${{#VOIPBIN_ADMIN_PASSWORD}} -ge 20 ]] && echo "password-env-ok" >> "{calls}"
echo "$VOIPBIN_ADMIN_PASSWORD" > "{tmp_dir}/admin_pw_seen"
[[ {admin_rc} -eq 3 ]] && {{ echo "VOIPBIN_ADMIN: status=exists customer_id=c1"; exit 3; }}
[[ {admin_rc} -ne 0 ]] && {{ echo 'VOIPBIN_ADMIN: status=error reason="boom"'; exit 1; }}
echo "VOIPBIN_ADMIN: status=ok customer_id=c1 agent_id=a1"''')
    return calls


def test_install_entry_guards(cli_module, tmp_dir, stub_bin, log_path):
    """Every guard exits before init.sh runs and ends with the result line."""
    project_dir = fresh_project(tmp_dir, "p_guard")
    install_docker_stub(stub_bin, log_path)
    calls = write_install_stubs(project_dir, tmp_dir)

    def run(argv, stdin_text=""):
        rc, out = run_main(cli_module, ["install"] + argv, stdin_text=stdin_text)
        assert last_line(out).startswith("VOIPBIN_INSTALL: "), out
        return rc, out, last_line(out)

    cases = [
        (["--bogus"], 1, "unknown argument"),
        (["--resume", "--mode", "external"], 1, "--resume does not accept"),
        ([], 1, "non-interactive run needs --yes"),
        (["--yes"], 1, "--admin-email is required"),
        (["--yes", "--admin-email", "not-an-email"], 1, "invalid --admin-email"),
        (["--resume", "--yes", "--admin-email", "a@example.com"], 1, "nothing to resume"),
    ]
    for argv, want_rc, reason in cases:
        rc, out, line = run(argv)
        assert rc == want_rc and "status=error" in line and reason in line, (argv, rc, line)

    existing = os.path.join(tmp_dir, "existing-pw")
    open(existing, "w").close()
    rc, out, line = run(["--yes", "--admin-email", "a@example.com", "--admin-password-file", existing])
    assert rc == 1 and "already exists" in line, line
    assert os.path.exists(existing), "a pre-existing --admin-password-file was removed"

    # (b) .env missing but the database volume exists
    project_name = os.path.basename(project_dir)
    install_docker_stub(stub_bin, log_path, volumes=f"other\\n{project_name}_db_data\\n")
    rc, out, line = run(["--yes", "--admin-email", "a@example.com"])
    assert rc == 1 and "database volume" in line, line
    install_docker_stub(stub_bin, log_path)

    # VoIP interfaces left by a removed install: step 1 stops a new install
    orig_ifaces = cli_module.INSTALL_VOIP_INTERFACES
    cli_module.INSTALL_VOIP_INTERFACES = ("lo",)  # present on every Linux host
    try:
        rc, out, line = run(["--yes", "--admin-email", "a@example.com"])
    finally:
        cli_module.INSTALL_VOIP_INTERFACES = orig_ifaces
    assert rc == 2 and "status=error step=1" in line and "VoIP network interfaces" in line, line
    assert "clean --network --force" in line and not os.path.exists(os.path.join(project_dir, ".env")), line

    # docker unreachable: environment error
    install_docker_stub(stub_bin, log_path, info_rc=1)
    rc, out, line = run(["--yes", "--admin-email", "a@example.com"])
    assert rc == 2 and "docker is not installed" in line, line
    install_docker_stub(stub_bin, log_path)

    # (c) .env present: never re-installs
    write_env(project_dir, INTERNAL_ENV)
    rc, out, line = run(["--yes", "--admin-email", "a@example.com"])
    assert rc == 1 and "existing .env without a completed install" in line and "--resume" in line, line
    cli_module.write_install_marker(os.path.join(project_dir, ".voipbin-installed"), "a@example.com")
    rc, out, line = run(["--yes", "--admin-email", "a@example.com"])
    assert rc == 0 and "state=already-installed" in line and "admin=a@example.com" in line, line
    rc, out, line = run(["--resume", "--yes", "--admin-email", "a@example.com"])
    assert rc == 1 and "already completed" in line, line

    with open(calls) as f:
        assert "init" not in f.read(), "a guard let init.sh run"
    reset_docker_stub(stub_bin, log_path)
    print("ok test_install_entry_guards")


def test_install_noninteractive_happy_path_and_failure(cli_module, tmp_dir, stub_bin, log_path):
    orig_urls = cli_module.INSTALL_REACHABILITY_URLS
    orig_verify = cli_module.VoIPBinCLI._install_wait_and_verify
    cli_module.INSTALL_REACHABILITY_URLS = ()
    cli_module.VoIPBinCLI._install_wait_and_verify = lambda self, d: (True, "3 containers", [], [])
    try:
        project_dir = fresh_project(tmp_dir, "p_happy")
        install_docker_stub(stub_bin, log_path)
        calls = write_install_stubs(project_dir, tmp_dir)
        pw_file = os.path.join(tmp_dir, "admin-password")
        if os.path.exists(pw_file):
            os.remove(pw_file)
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com",
                                        "--admin-password-file", pw_file, "--mode", "internal"], stdin_text="")
        assert rc == 0, (rc, out)
        assert last_line(out) == ("VOIPBIN_INSTALL: status=ok state=installed admin=admin@example.com "
                                  "url=http://admin.voipbin.test:3003"), last_line(out)
        with open(calls) as f:
            seq = [l.split()[0] for l in f.read().splitlines()]
        assert seq == ["doctor", "init", "setup-host", "start", "create-admin", "password-env-ok"], seq
        with open(calls) as f:
            assert "init --yes --mode internal" in f.read()
        st = os.stat(pw_file)
        assert stat.S_IMODE(st.st_mode) == 0o600, oct(st.st_mode)
        with open(pw_file) as f:
            password = f.read().strip()
        with open(os.path.join(tmp_dir, "admin_pw_seen")) as f:
            assert f.read().strip() == password
        assert len(password) >= 20
        assert password not in out, "password printed although --admin-password-file was given"
        marker = cli_module.read_install_marker(os.path.join(project_dir, ".voipbin-installed"))
        assert marker.get("admin_email") == "admin@example.com" and "password" not in json_dump(marker)
        # re-run: idempotent no-op
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com"], stdin_text="")
        assert rc == 0 and "state=already-installed" in last_line(out), out

        # failure injection: start.sh fails -> its code propagates, no marker,
        # the password file this run created is removed
        project_dir = fresh_project(tmp_dir, "p_fail")
        write_install_stubs(project_dir, tmp_dir, start_rc=2)
        pw_file2 = os.path.join(tmp_dir, "admin-password-2")
        if os.path.exists(pw_file2):
            os.remove(pw_file2)
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com",
                                        "--admin-password-file", pw_file2], stdin_text="")
        line = last_line(out)
        assert rc == 2 and line.startswith("VOIPBIN_INSTALL: status=error step=4"), (rc, line)
        # the next command keeps the --yes mode so it can be run as printed
        assert ('next="fix the error above, then sudo ./voipbin install --resume --yes '
                '--admin-email admin@example.com --admin-password-file <new path>"') in line, line
        assert "What failed:" in out and "Next:" in out
        assert not os.path.exists(pw_file2)
        assert not os.path.exists(os.path.join(project_dir, ".voipbin-installed"))

        # resume after the failure: init is not re-run
        write_install_stubs(project_dir, tmp_dir)
        os.makedirs(os.path.join(project_dir, "certs", "api"), exist_ok=True)
        for n in ("cert.pem", "privkey.pem"):
            open(os.path.join(project_dir, "certs", "api", n), "w").close()
        # VoIP interfaces made by the failed run itself do not stop --resume
        orig_ifaces = cli_module.INSTALL_VOIP_INTERFACES
        cli_module.INSTALL_VOIP_INTERFACES = ("lo",)
        try:
            rc, out = run_main(cli_module, ["install", "--resume", "--yes", "--admin-email", "admin@example.com"], stdin_text="")
        finally:
            cli_module.INSTALL_VOIP_INTERFACES = orig_ifaces
        assert rc == 0, (rc, out)
        with open(os.path.join(tmp_dir, "install_calls.log")) as f:
            seq = [l.split()[0] for l in f.read().splitlines()]
        assert "init" not in seq and seq[:3] == ["doctor", "setup-host", "start"], seq
        assert "Admin password" in out and "printed once" in out, "no-file run must show the password once with a warning"

        # a marker left behind without .env (removed install) is dropped, so a
        # later --resume is not refused as "already completed"
        project_dir = fresh_project(tmp_dir, "p_stale")
        write_install_stubs(project_dir, tmp_dir, start_rc=2)
        with open(os.path.join(project_dir, ".voipbin-installed"), "w") as f:
            f.write("{}")
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com"], stdin_text="")
        assert rc == 2 and not os.path.exists(os.path.join(project_dir, ".voipbin-installed")), (rc, out)

        # existing account without --reset-admin-password: exit 1, guidance
        project_dir = fresh_project(tmp_dir, "p_exists")
        write_install_stubs(project_dir, tmp_dir, admin_rc=3)
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com"], stdin_text="")
        assert rc == 1 and "already exists" in last_line(out), last_line(out)
        assert ('next="sudo ./voipbin install --resume --yes --admin-email admin@example.com '
                '--reset-admin-password"') in last_line(out), last_line(out)
        pw_file3 = os.path.join(tmp_dir, "admin-password-3")
        if os.path.exists(pw_file3):
            os.remove(pw_file3)
        project_dir = fresh_project(tmp_dir, "p_exists2")
        write_install_stubs(project_dir, tmp_dir, admin_rc=3)
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com",
                                        "--admin-password-file", pw_file3], stdin_text="")
        # the printed command keeps --admin-password-file, so the new password is not printed
        assert rc == 1 and "--admin-password-file <new path> --reset-admin-password" in last_line(out), last_line(out)
    finally:
        cli_module.INSTALL_REACHABILITY_URLS = orig_urls
        cli_module.VoIPBinCLI._install_wait_and_verify = orig_verify
        reset_docker_stub(stub_bin, log_path)
    print("ok test_install_noninteractive_happy_path_and_failure")


def test_install_user_caroot(cli_module, tmp_dir):
    """Under sudo, init.sh/start.sh get the invoking user's mkcert CA directory."""
    import pwd as pwd_mod
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout = rc, out

    caroot = os.path.join(tmp_dir, "user-caroot")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "-CAROOT":
            return R(0, caroot + "\n")
        return R(0)

    me = pwd_mod.getpwuid(os.getuid())
    orig_euid, orig_which = cli_module.os.geteuid, cli_module.shutil.which
    orig_sudo = os.environ.get("SUDO_USER")
    try:
        cli_module.os.geteuid = lambda: 0
        os.environ["SUDO_USER"] = me.pw_name
        cli_module.shutil.which = lambda n: "/usr/bin/" + n
        if me.pw_uid != 0:
            got = cli_module.install_user_caroot(run=fake_run)
            assert got == caroot, got
            assert calls[0] == ["sudo", "-u", me.pw_name, "-H", "mkcert", "-CAROOT"], calls
            assert calls[1] == ["sudo", "-u", me.pw_name, "-H", "mkdir", "-p", caroot], calls
            # mkcert not installed yet: the Linux default directory, still created as the user
            calls.clear()
            cli_module.shutil.which = lambda n: None
            got = cli_module.install_user_caroot(run=fake_run)
            default = os.path.join(me.pw_dir, ".local", "share", "mkcert")
            assert got == default and calls == [["sudo", "-u", me.pw_name, "-H", "mkdir", "-p", default]], (got, calls)
        # no SUDO_USER (direct root login) or not root: unchanged behavior
        del os.environ["SUDO_USER"]
        assert cli_module.install_user_caroot(run=fake_run) is None
        os.environ["SUDO_USER"] = me.pw_name
        cli_module.os.geteuid = lambda: 1000
        assert cli_module.install_user_caroot(run=fake_run) is None
    finally:
        cli_module.os.geteuid, cli_module.shutil.which = orig_euid, orig_which
        if orig_sudo is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = orig_sudo
    os.makedirs(caroot, exist_ok=True)
    for n in ("rootCA.pem", "rootCA-key.pem"):
        open(os.path.join(caroot, n), "w").close()
    seen = []
    cli_module.fix_caroot_ownership(caroot, (1234, 1234, "alice"), chown=lambda p, u, g: seen.append((os.path.basename(p), u)))
    assert sorted(seen) == [("rootCA-key.pem", 1234), ("rootCA.pem", 1234)], seen
    print("ok test_install_user_caroot")


def test_install_passes_caroot_to_init_and_start(cli_module, tmp_dir, stub_bin, log_path):
    orig = (cli_module.INSTALL_REACHABILITY_URLS, cli_module.VoIPBinCLI._install_wait_and_verify,
            cli_module.install_user_caroot)
    caroot = os.path.join(tmp_dir, "caroot-x")
    cli_module.INSTALL_REACHABILITY_URLS = ()
    cli_module.VoIPBinCLI._install_wait_and_verify = lambda self, d: (True, "3 containers", [], [])
    cli_module.install_user_caroot = lambda: caroot
    try:
        project_dir = fresh_project(tmp_dir, "p_caroot")
        install_docker_stub(stub_bin, log_path)
        write_install_stubs(project_dir, tmp_dir)
        for name in ("init.sh", "start.sh", "setup-host.sh"):
            path = os.path.join(project_dir, "scripts", name)
            with open(path) as f:
                body = f.read()
            with open(path, "w") as f:
                f.write(body.replace("#!/bin/bash\n",
                                     "#!/bin/bash\necho \"%s CAROOT=${CAROOT:-}\" >> %s\n"
                                     % (name, os.path.join(tmp_dir, "caroot_seen")), 1))
        seen_path = os.path.join(tmp_dir, "caroot_seen")
        if os.path.exists(seen_path):
            os.remove(seen_path)
        rc, out = run_main(cli_module, ["install", "--yes", "--admin-email", "admin@example.com"], stdin_text="")
        assert rc == 0, (rc, out)
        with open(seen_path) as f:
            seen = f.read().splitlines()
        assert "init.sh CAROOT=" + caroot in seen and "start.sh CAROOT=" + caroot in seen, seen
    finally:
        (cli_module.INSTALL_REACHABILITY_URLS, cli_module.VoIPBinCLI._install_wait_and_verify,
         cli_module.install_user_caroot) = orig
        reset_docker_stub(stub_bin, log_path)
    print("ok test_install_passes_caroot_to_init_and_start")


def json_dump(obj):
    import json
    return json.dumps(obj)


def test_classify_install_containers(cli_module):
    # the CLI and check-install.sh/doctor.sh (common.sh) must match the same tts log line
    with open(os.path.join(SCRIPTS_DIR, "common.sh")) as f:
        assert f'TTS_CREDENTIALS_MISSING_LOG="{cli_module.INSTALL_TTS_CREDENTIALS_MISSING_LOG}"' in f.read()
    def c(name, status="running", health="", probe="", restarts=0, restarting=False):
        return {"name": name, "status": status, "health": health, "last_probe": probe,
                "restart_count": restarts, "restarting": restarting}
    tts = lambda name: name == "voipbin-tts-mgr"
    snap = [
        c("voipbin-db", health="healthy"),
        c("voipbin-call-mgr", health="unhealthy", probe="exec: wget: executable file not found in $PATH"),
        c("voipbin-flow-mgr", health="unhealthy", probe="connection refused"),
        c("voipbin-tts-mgr", restarts=5, restarting=True),
        c("voipbin-ai-mgr", restarts=3),
        c("voipbin-redis", status="exited"),
        c("voipbin-api-mgr", health="starting"),
    ]
    baseline = {"voipbin-tts-mgr": 2, "voipbin-ai-mgr": 1}
    v = cli_module.classify_install_containers(snap, baseline, tts)
    assert v["known"] == ["voipbin-call-mgr"], v
    assert v["optional"] == ["voipbin-tts-mgr"], v
    assert v["starting"] == ["voipbin-api-mgr"], v
    assert sorted(n for n, _ in v["fail"]) == ["voipbin-ai-mgr", "voipbin-flow-mgr", "voipbin-redis"], v
    # tts restarting for another reason is a failure
    v = cli_module.classify_install_containers([c("voipbin-tts-mgr", restarting=True)], {}, lambda n: False)
    assert v["fail"] == [("voipbin-tts-mgr", "restarting")], v
    print("ok test_classify_install_containers")


def test_install_verify_timeouts_and_ping(cli_module, tmp_dir):
    cli, _ = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    now = [0.0]
    clock = lambda: now[0]

    def sleep(s):
        now[0] += s
    starting = [{"name": "voipbin-api-mgr", "status": "running", "health": "starting", "last_probe": "",
                 "restart_count": 0, "restarting": False}]
    cli._install_inspect_containers = lambda d: starting
    ok, summary, optional, failures = cli._install_wait_and_verify(".", clock=clock, sleep=sleep, ping=lambda: True)
    assert not ok and ("voipbin-api-mgr", "still starting after 180s") in failures, failures
    assert now[0] >= 180
    healthy = [dict(starting[0], health="healthy")]
    cli._install_inspect_containers = lambda d: healthy
    now[0] = 0.0
    ok, summary, optional, failures = cli._install_wait_and_verify(".", clock=clock, sleep=sleep, ping=lambda: False)
    assert not ok and failures[0][0] == "api-manager", failures
    assert 30 <= now[0] < 40, now[0]  # minimum observation window, then stops
    now[0] = 0.0
    ok, summary, optional, failures = cli._install_wait_and_verify(".", clock=clock, sleep=sleep, ping=lambda: True)
    assert ok and not failures, failures
    print("ok test_install_verify_timeouts_and_ping")


def test_install_ownership(cli_module, tmp_dir):
    me = pwd_name()
    prev = os.environ.get("SUDO_USER")
    try:
        os.environ["SUDO_USER"] = me
        owner = cli_module.resolve_install_owner(tmp_dir)
        assert owner and owner[0] == os.getuid() and owner[2] == me, owner
        os.environ.pop("SUDO_USER")
        owner = cli_module.resolve_install_owner(tmp_dir)
        assert owner and owner[0] == os.stat(tmp_dir).st_uid, owner
        os.environ["SUDO_USER"] = "root"
        orig_stat = cli_module.os.stat
        class RootStat:
            st_uid = 0
            st_gid = 0
        cli_module.os.stat = lambda p: RootStat()
        try:
            assert cli_module.resolve_install_owner(tmp_dir) is None
        finally:
            cli_module.os.stat = orig_stat
    finally:
        if prev is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = prev

    project_dir = os.path.join(tmp_dir, "p_owner")
    shutil.rmtree(project_dir, ignore_errors=True)
    os.makedirs(os.path.join(project_dir, "certs", "api"))
    for rel in (".env", "docker-compose.yml", "versions.lock", ".voipbin-installed",
                os.path.join("certs", "api", "cert.pem"), "README.md"):
        open(os.path.join(project_dir, rel), "w").close()
    outside = os.path.join(tmp_dir, "outside-target")
    open(outside, "w").close()
    os.symlink(outside, os.path.join(project_dir, "tmp"))
    seen = []
    n = cli_module.fix_install_ownership(project_dir, owner=(1234, 1234, "u"),
                                         chown=lambda p, u, g: seen.append(os.path.relpath(p, project_dir)))
    for rel in (".env", "docker-compose.yml", "versions.lock", ".voipbin-installed", "certs",
                os.path.join("certs", "api"), os.path.join("certs", "api", "cert.pem")):
        assert rel in seen, (rel, seen)
    assert "README.md" not in seen and "tmp" not in seen, seen
    assert n == len(seen)
    # owner cannot be resolved: no chown, one warning line
    orig = cli_module.resolve_install_owner
    cli_module.resolve_install_owner = lambda d: None
    try:
        seen.clear()
        out = capture(cli_module.fix_install_ownership, project_dir, None, lambda p, u, g: seen.append(p))
    finally:
        cli_module.resolve_install_owner = orig
    assert not seen and "ownership not changed" not in out, out  # warning goes to stderr
    # a symlinked parent directory is not followed
    real_cfg = os.path.join(tmp_dir, "elsewhere-config")
    os.makedirs(os.path.join(real_cfg, "coredns"), exist_ok=True)
    open(os.path.join(real_cfg, "coredns", "Corefile"), "w").close()
    os.symlink(real_cfg, os.path.join(project_dir, "config"))
    seen.clear()
    cli_module.fix_install_ownership(project_dir, owner=(1234, 1234, "u"),
                                     chown=lambda p, u, g: seen.append(os.path.relpath(p, project_dir)))
    assert not any(s.startswith("config") for s in seen), seen
    print("ok test_install_ownership")


def pwd_name():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def test_user_facing_text_has_no_sandbox_and_honors_no_color(cli_module, tmp_dir, log_path):
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    texts = [cli_module.render_help(), cli_module.render_api_help(),
             capture(cli_module.show_cli_usage), capture(cli.cmd_clean, []),
             cli_module.get_logo()]
    texts += [d + u for d, u in cli.help_text.values()]
    empty = os.path.join(tmp_dir, "p_dash")
    os.makedirs(empty, exist_ok=True)
    prev = os.getcwd()
    os.chdir(empty)
    try:
        texts.append(capture(cli_module.show_welcome_dashboard))
    finally:
        os.chdir(prev)
    for t in texts:
        assert "sandbox" not in t.lower(), t[:400]
    # the first screen no longer makes editing .env a required step
    assert "nano .env" not in texts[-1] and "install" in texts[-1], texts[-1]

    prev_no_color = os.environ.get("NO_COLOR")
    saved = {k: getattr(cli_module.Colors, k) for k in ("GREEN", "RED", "YELLOW", "BLUE", "GRAY", "WHITE", "BOLD", "DIM", "RESET")}
    class FakeTty:
        def isatty(self):
            return True
    os.environ.pop("NO_COLOR", None)
    assert cli_module.color_wanted(FakeTty()) is True
    os.environ["NO_COLOR"] = "1"
    try:
        assert cli_module.color_wanted(FakeTty()) is False, "NO_COLOR ignored on a terminal"
        assert cli_module.color_wanted() is False
        rc, out = run_main(cli_module, ["--help"])
        assert rc == 0 and "\x1b[" not in out, repr(out[:200])
        assert "install" in out and "help api" in out, out
    finally:
        for k, v in saved.items():
            setattr(cli_module.Colors, k, v)
        if prev_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = prev_no_color
    print("ok test_user_facing_text_has_no_sandbox_and_honors_no_color")


def test_help_and_version_need_no_root(cli_module, tmp_dir):
    make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    orig_check_root, orig_argv = cli_module.check_root, sys.argv
    prev_cwd = os.getcwd()

    def refuse():
        raise SystemExit(99)
    cli_module.check_root = refuse
    try:
        for argv in (["--help"], ["-h"], ["help"], ["help", "install"]):
            sys.argv = ["voipbin"] + argv
            with contextlib.redirect_stdout(io.StringIO()):
                cli_module.main()  # must not hit check_root
            os.chdir(prev_cwd)
        sys.argv = ["voipbin", "status"]
        raised = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli_module.main()
        except SystemExit as e:
            raised = e.code
        assert raised == 99, raised
    finally:
        cli_module.check_root, sys.argv = orig_check_root, orig_argv
        os.chdir(prev_cwd)
    print("ok test_help_and_version_need_no_root")


def test_status_never_prints_service_passwords(cli_module, tmp_dir, stub_bin, log_path):
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV + ["OPENAI_API_KEY=fake-openai-key"], cli_module)
    set_docker_stub(stub_bin, log_path, '''
if [[ "$1 $2" == "compose ps" ]]; then printf "voipbin-db\\tUp 5 minutes\\nvoipbin-mq\\tUp 5 minutes\\n"; exit 0; fi
''')
    prev_env = {k: os.environ.get(k) for k in ("RABBITMQ_DEFAULT_PASS", "MYSQL_ROOT_PASSWORD")}
    os.environ["RABBITMQ_DEFAULT_PASS"] = "fake-mq-secret"
    os.environ["MYSQL_ROOT_PASSWORD"] = "fake-db-secret"
    prev = os.getcwd()
    os.chdir(project_dir)
    try:
        out = capture(cli.cmd_status, [])
    finally:
        os.chdir(prev)
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_docker_stub(stub_bin, log_path)
    assert "fake-mq-secret" not in out and "fake-db-secret" not in out, out
    assert "root_password" not in out and "guest" not in out, out
    assert "credentials: see .env" in out, out
    assert "fake-openai-key" not in out, out
    assert re.search(r"OPENAI_API_KEY\s+\S*set", out), out
    assert "(TTS/storage disabled)" not in out and "(transcription)" not in out, out
    print("ok test_status_never_prints_service_passwords")


# ---------------------------------------------------------------------------
# Interactive TUI: pure render/parse functions, fallback, REPL loop
# ---------------------------------------------------------------------------

class FakeStream:
    def __init__(self, tty=True, encoding="utf-8"):
        self._tty, self.encoding = tty, encoding

    def isatty(self):
        return self._tty


def tui_info(**overrides):
    info = {"version": None, "state": "running", "running": 12, "total": 45,
            "dns": True, "net": True, "mode": "internal",
            "cwd": "/srv/voipbin-install/install"}
    info.update(overrides)
    return info


def test_tui_wanted(cli_module):
    tty = FakeStream()
    ok = {"TERM": "xterm"}
    assert cli_module.tui_wanted(tty, tty, ok, lines=24) is True
    assert cli_module.tui_wanted(FakeStream(False), tty, ok, lines=24) is False, "stdin not a tty"
    assert cli_module.tui_wanted(tty, FakeStream(False), ok, lines=24) is False, "stdout not a tty"
    assert cli_module.tui_wanted(tty, tty, {"TERM": "xterm", "NO_COLOR": "1"}, lines=24) is False
    assert cli_module.tui_wanted(tty, tty, {"TERM": "dumb"}, lines=24) is False
    assert cli_module.tui_wanted(tty, FakeStream(encoding="ANSI_X3.4-1968"), ok, lines=24) is False
    assert cli_module.tui_wanted(tty, FakeStream(encoding=None), ok, lines=24) is False
    assert cli_module.tui_wanted(tty, FakeStream(encoding="UTF-8"), ok, lines=24) is True
    assert cli_module.tui_wanted(tty, tty, ok, lines=7) is False, "too short for the frame"
    assert cli_module.tui_wanted(tty, tty, ok, lines=8) is True
    assert cli_module.tui_wanted(io.StringIO(""), io.StringIO(""), ok, lines=24) is False
    print("ok test_tui_wanted")


def test_tui_clip_and_widths(cli_module):
    vis, clip = cli_module.visible_len, cli_module.clip
    assert vis("\x1b[1mabc\x1b[0m") == 3 and vis("한글") == 4
    assert clip("abcdef", 10) == "abcdef" and clip("abcdef", 4) == "abc…"
    assert clip("abcdef", 0) == "" and vis(clip("한글한글", 5)) <= 5
    colored = clip("\x1b[1mabcdefgh\x1b[0m", 5)
    assert vis(colored) == 5 and colored.endswith("…\x1b[0m"), repr(colored)
    assert clip("/very/long/path/name", 8, keep="tail") == "…th/name"
    assert vis(clip("/very/long/path/name", 8, keep="tail")) <= 8
    print("ok test_tui_clip_and_widths")


def test_tui_render_header_notice_statusbar(cli_module):
    vis = cli_module.visible_len
    plain = lambda t: re.sub(r"\x1b\[[0-9;]*m", "", t)
    for width in (80, 60, 59, 40, 24, 20):
        for info in (tui_info(), tui_info(version="v1.2.3"), tui_info(state="stopped", running=None, total=None, dns=None, net=None),
                     tui_info(state="not_initialized", running=None, total=None, dns=None, net=None, mode=None),
                     tui_info(mode="external", dns="n/a"), tui_info(total=0),
                     tui_info(cwd="/" + "deep/" * 40 + "dir")):
            rows = (cli_module.render_header(info, width) + [cli_module.render_notice(info, width)]
                    + cli_module.render_frame(info, width))
            for row in rows:
                assert vis(row) <= width - 1, (width, vis(row), plain(row))
    header = [plain(r) for r in cli_module.render_header(tui_info(version="v1.2.3"), 100)]
    assert len(header) == 3 and "█" in header[0]
    icon = cli_module.TUI_ICON
    assert len(icon) == 3 and len({len(r) for r in icon}) == 1 and len(icon[0]) == 12, icon
    assert all(ch in " █▀▄" for r in icon for ch in r), icon
    assert icon == ("▄██▀▀▀▀▀▀██▄", "███▀    ▀███", "▀█▀      ▀█▀"), icon
    assert "VoIPBin Install CLI v1.2.3" in header[0] and "running 12/45" in header[1] and "mode internal" in header[1], header
    assert header[2].rstrip().endswith("/srv/voipbin-install/install"), header
    narrow = [plain(r) for r in cli_module.render_header(tui_info(cwd="/srv/projects/voipbin/checkout/current/install"), 40)]
    assert len(narrow) == 3 and "█" not in "".join(narrow), narrow
    assert "VoIPBin Install CLI" in narrow[0] and "v" not in narrow[0].split("CLI")[1], narrow
    assert "…" in narrow[2] and narrow[2].endswith("install"), narrow
    assert cli_module.render_notice(tui_info(), 80) == "Using .env (mode=internal) · /status"
    assert cli_module.render_notice(tui_info(state="not_initialized", mode=None), 80) == "No installation found · /install"
    full = [plain(r) for r in cli_module.render_statusbar(tui_info(), 100)]
    assert full[0] == "Env: running | Services: 12/45 | DNS: ● | Net: ● | Mode: internal", full
    assert full[1] == 'Try "/status" or "/logs <service>" · /help for commands · /exit to quit', full
    bar40 = [plain(r) for r in cli_module.render_statusbar(tui_info(), 40)]
    assert "12/45" in bar40[0] and "internal" in bar40[0] and "/help" in bar40[1] and "/exit" in bar40[1], bar40
    bar20 = [plain(r) for r in cli_module.render_statusbar(tui_info(), 20)]
    assert bar20[1] == "/help · /exit" and bar20[0].startswith("running 12/45"), bar20
    stopped = plain(cli_module.render_statusbar(tui_info(state="stopped", running=None, total=None, dns=None, net=None), 100)[0])
    assert stopped == "Env: stopped | Services: - | DNS: - | Net: - | Mode: internal", stopped
    fresh = [plain(r) for r in cli_module.render_statusbar(tui_info(state="not_initialized", running=None, total=None, dns=None, net=None, mode=None), 100)]
    assert fresh[0] == "Env: not installed | Services: - | DNS: - | Net: - | Mode: -", fresh
    assert fresh[1].startswith('Try "/install"'), fresh
    ext = plain(cli_module.render_statusbar(tui_info(mode="external", dns="n/a", net=False), 100)[0])
    assert "DNS: n/a" in ext and "Net: ○" in ext and "Mode: external" in ext, ext
    assert plain(cli_module.render_statusbar(tui_info(total=0), 100)[0]).count("Services: 12 ") == 1
    frame = cli_module.render_frame(tui_info(), 50)
    assert len(frame) == 5 and frame[1] == "" and frame[0] == frame[2] and "─" * 49 in frame[0]
    # colors off (config colors=false): no SGR sequences, frame unchanged
    saved = {k: getattr(cli_module.Colors, k) for k in ("GREEN", "RED", "YELLOW", "BLUE", "GRAY", "WHITE", "BOLD", "DIM", "RESET")}
    cli_module.Colors.disable()
    try:
        text = "".join(cli_module.render_header(tui_info(), 100) + cli_module.render_frame(tui_info(), 100))
        assert "\x1b[" not in text
    finally:
        for k, v in saved.items():
            setattr(cli_module.Colors, k, v)
    print("ok test_tui_render_header_notice_statusbar")


def test_tui_release_version(cli_module, tmp_dir):
    def version_of(text):
        path = os.path.join(tmp_dir, "CHANGELOG.test.md")
        with open(path, "w") as f:
            f.write(text)
        return cli_module.read_release_version(path)
    assert version_of("# Changelog\n\n## [Unreleased]\n\n### Added\n- x\n") is None
    assert version_of("## [Unreleased]\n\n## [v0.2.1] - 2026-11-02\n\n## [v0.2.0] - 2026-11-01\n") == "v0.2.1"
    assert version_of("") is None
    assert cli_module.read_release_version(os.path.join(tmp_dir, "missing.md")) is None
    shipped = cli_module.read_release_version()
    assert shipped is None or re.fullmatch(r"v\d+\.\d+\.\d+", shipped), shipped
    print("ok test_tui_release_version")


def test_tui_parse_slash(cli_module):
    parse = cli_module.parse_slash
    assert parse("status") == ("status", None) and parse("  install --yes ") == ("  install --yes ", None)
    assert parse("") == ("", None)
    for name in ("help", "install", "status", "start", "stop", "restart", "logs", "doctor", "update", "version", "clear", "exit"):
        assert parse("/" + name) == (name, None), name
    assert parse("/logs -f api-manager") == ("logs -f api-manager", None)
    assert parse("  /STATUS  ") == ("status", None)
    assert parse("/start   kamailio ") == ("start kamailio", None)
    line, msg = parse("/")
    assert line is None and "/install" in msg and "/exit" in msg and "/doctor" in msg, msg
    for bad in ("/quit", "/ status", "//x", "/ast", "/foo bar"):
        line, msg = parse(bad)
        assert line is None and msg.startswith("Unknown command: " + bad.split()[0]), (bad, msg)
    assert "ast" not in cli_module.TUI_SLASH_COMMANDS and len(cli_module.TUI_SLASH_COMMANDS) == 12
    print("ok test_tui_parse_slash")


def test_tui_completer_and_help(cli_module, tmp_dir):
    cli, project_dir = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    completer = cli_module.Completer(cli)
    assert completer.get_matches("/ast", "ast") == ["ast ", "asterisk "], "TUI off: classic completion unchanged"
    cli.tui_on = True
    assert sorted(completer.get_matches("/st", "st")) == ["start ", "status ", "stop "]
    assert sorted(completer.get_matches("/st", "/st")) == ["/start ", "/status ", "/stop "], "libedit keeps the slash"
    assert completer.get_matches("/", "") == [c + " " for c in cli_module.TUI_SLASH_COMMANDS]
    assert completer.get_matches("/ex", "ex") == ["exit "]
    assert completer.get_matches("/ast", "ast") == []
    assert completer.get_matches("/logs -", "-") == ["-f "]
    assert completer.get_matches("/update ", "") == ["scripts ", "all ", "--check "]
    assert completer.get_matches("sta", "sta")[0].startswith("sta"), "plain words still complete"
    assert "Slash commands" in capture(cli.cmd_help, [])
    assert "Slash commands" not in capture(cli.cmd_help, ["status"])
    cli.tui_on = False
    assert "Slash commands" not in capture(cli.cmd_help, [])
    # non-interactive help output carries no slash text
    rc, out = run_main(cli_module, ["help"])
    assert rc == 0 and "Slash commands" not in out and out == capture(cli_module.show_cli_usage), out[:200]
    rc, out = run_main(cli_module, ["--help"])
    assert rc == 0 and "Slash commands" not in out
    print("ok test_tui_completer_and_help")


def test_tui_collect_info_states_and_secrets(cli_module, tmp_dir, stub_bin, log_path):
    secret = "tui-secret-never-shown"
    env_lines = INTERNAL_ENV + ["OPENAI_API_KEY=" + secret, "MYSQL_ROOT_PASSWORD=" + secret]
    cli, project_dir = make_cli(tmp_dir, env_lines, cli_module)
    prev = os.getcwd()
    os.chdir(project_dir)
    calls = {"detect": 0, "quick": 0}
    orig_detect, orig_quick = cli_module.detect_dashboard_state, cli_module.get_quick_status

    def counting_detect():
        calls["detect"] += 1
        return orig_detect()

    def counting_quick():
        calls["quick"] += 1
        return orig_quick()
    cli_module.detect_dashboard_state, cli_module.get_quick_status = counting_detect, counting_quick
    try:
        set_docker_stub(stub_bin, log_path, '''
if [[ "$1 $2" == "compose ps" ]]; then printf "voipbin-db\\tUp 5 minutes\\nvoipbin-mq\\tUp 5 minutes\\nvoipbin-redis\\tExited (1)\\n"; exit 0; fi
if [[ "$1 $2" == "compose config" ]]; then printf "db\\nmq\\nredis\\napi-manager\\n"; exit 0; fi
''')
        info = cli_module.tui_collect_info(cli)
        assert (calls["detect"], calls["quick"]) == (1, 1), calls
        assert info["state"] == "running" and (info["running"], info["total"]) == (2, 4), info
        assert info["mode"] == "internal" and info["net"] is False and info["dns"] is False, info
        shown = " ".join(cli_module.render_header(info, 100) + [cli_module.render_notice(info, 100)]
                         + cli_module.render_frame(info, 100) + [str(info)])
        assert secret not in shown and "HOST_EXTERNAL_IP" not in shown, shown
        # control characters in the directory name and in DOMAIN_MODE never reach the screen
        evil_dir = os.path.join(tmp_dir, "evil\x1b[31mdir")
        os.makedirs(evil_dir, exist_ok=True)
        os.chdir(evil_dir)
        write_env(project_dir, INTERNAL_ENV[1:] + ["DOMAIN_MODE=ex\x1b[2Jternal"])
        info = cli_module.tui_collect_info(cli)
        assert "\x1b" not in info["cwd"] and info["cwd"].endswith("evil?[31mdir"), info["cwd"]
        os.chdir(project_dir)
        info = cli_module.tui_collect_info(cli)
        assert info["mode"] == "ex2Jternal" and "\x1b" not in str(info) and "[" not in info["mode"], info
        write_env(project_dir, env_lines)
        # external mode: DNS is not managed by the install
        write_env(project_dir, EXTERNAL_ENV)
        assert cli_module.tui_collect_info(cli)["dns"] == "n/a"
        write_env(project_dir, env_lines)
        # containers listed but none running: shown as stopped
        set_docker_stub(stub_bin, log_path, '''
if [[ "$1 $2" == "compose ps" ]]; then printf "voipbin-db\\tExited (0)\\n"; exit 0; fi
''')
        info = cli_module.tui_collect_info(cli)
        assert info["state"] == "stopped" and info["running"] is None, info
        assert cli_module._services_text(info) == "-"
        reset_docker_stub(stub_bin, log_path)
        info = cli_module.tui_collect_info(cli)
        assert info["state"] == "stopped" and info["mode"] == "internal", info
        os.remove(os.path.join(project_dir, ".env"))
        calls["detect"] = calls["quick"] = 0
        info = cli_module.tui_collect_info(cli)
        assert info["state"] == "not_initialized" and info["mode"] is None and calls == {"detect": 1, "quick": 0}, (info, calls)
    finally:
        cli_module.detect_dashboard_state, cli_module.get_quick_status = orig_detect, orig_quick
        reset_docker_stub(stub_bin, log_path)
        os.chdir(prev)
    print("ok test_tui_collect_info_states_and_secrets")


@contextlib.contextmanager
def fake_input(responder):
    import builtins
    orig = builtins.input
    builtins.input = responder
    try:
        yield
    finally:
        builtins.input = orig


@contextlib.contextmanager
def terminal_size(columns, lines):
    prev = {k: os.environ.get(k) for k in ("COLUMNS", "LINES")}
    os.environ["COLUMNS"], os.environ["LINES"] = str(columns), str(lines)
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_tui_read_line_sequences(cli_module, tmp_dir):
    import signal as signal_mod
    cli, _ = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    info = tui_info()
    marker_handler = lambda *args: None  # noqa: E731  distinguishable from the default handler
    handler_before = signal_mod.signal(signal_mod.SIGINT, marker_handler)
    prompts = []
    during = []

    def typed(text):
        def responder(prompt=""):
            prompts.append(prompt)
            during.append(signal_mod.getsignal(signal_mod.SIGINT))
            return text
        return responder

    def raising(exc):
        def responder(prompt=""):
            prompts.append(prompt)
            raise exc
        return responder
    with terminal_size(60, 24):
        rule = cli_module.render_frame(info, 60)[0]
        # normal line: frame drawn, cursor back on the input row, closing rule kept
        with fake_input(typed("/status")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert cli_module.tui_read_line(cli, info) == "/status"
        out = buf.getvalue()
        assert out.startswith("\r" + rule + "\n\n" + rule + "\n") and "\x1b[3A\r" in out, repr(out)
        assert out.endswith("\r\x1b[J" + rule + "\n"), repr(out)
        assert prompts[-1].startswith("\001") and "❯" in prompts[-1] and prompts[-1].endswith("\002 "), repr(prompts[-1])
        # blank line (and whitespace only): frame erased from the top rule, nothing left behind
        for blank in ("", "   "):
            buf = io.StringIO()
            with fake_input(typed(blank)), contextlib.redirect_stdout(buf):
                assert cli_module.tui_read_line(cli, info) == blank
            assert buf.getvalue().endswith("\x1b[2A\r\x1b[J"), repr(buf.getvalue())
        # Ctrl+C: typed text dropped, "" returned, frame erased
        buf = io.StringIO()
        with fake_input(raising(KeyboardInterrupt())), contextlib.redirect_stdout(buf):
            assert cli_module.tui_read_line(cli, info) == ""
        assert buf.getvalue().endswith("\x1b[1A\r\x1b[J"), repr(buf.getvalue())
        # Ctrl+D: closing rule kept, EOFError re-raised for the loop
        buf = io.StringIO()
        raised = False
        with fake_input(raising(EOFError())), contextlib.redirect_stdout(buf):
            try:
                cli_module.tui_read_line(cli, info)
            except EOFError:
                raised = True
        assert raised and buf.getvalue().endswith("\n\r\x1b[J" + rule + "\n"), repr(buf.getvalue())
        assert during and all(h is signal_mod.default_int_handler for h in during), during
        assert signal_mod.getsignal(signal_mod.SIGINT) is marker_handler, "SIGINT handler not restored"
        # the frame write fails: plain prompt, no exception
        class BrokenOut(io.StringIO):
            def write(self, text):
                raise OSError("closed")
        prompts.clear()
        orig_stdout = sys.stdout
        sys.stdout = BrokenOut()
        try:
            with fake_input(typed("status")):
                assert cli_module.tui_read_line(cli, info) == "status"
        finally:
            sys.stdout = orig_stdout
        assert prompts == [cli.get_prompt()], prompts
    signal_mod.signal(signal_mod.SIGINT, handler_before)
    # too short for a frame: plain prompt, nothing drawn
    with terminal_size(60, 5):
        prompts.clear()
        buf = io.StringIO()
        with fake_input(typed("status")), contextlib.redirect_stdout(buf):
            assert cli_module.tui_read_line(cli, info) == "status"
        assert buf.getvalue() == "" and prompts == [cli.get_prompt()]
    print("ok test_tui_read_line_sequences")


def test_tui_repl_loop(cli_module, tmp_dir):
    cli, _ = make_cli(tmp_dir, INTERNAL_ENV, cli_module)
    info = tui_info()
    calls = []
    for name in ("status", "start", "doctor", "clear", "logs", "update"):
        cli.commands[name] = (lambda n: lambda args: calls.append((n, list(args))))(name)

    def boom(args):
        calls.append(("restart", list(args)))
        raise RuntimeError("restart failed")
    cli.commands["restart"] = boom
    refreshed = []
    orig_collect, orig_intro = cli_module.tui_collect_info, cli_module.tui_print_intro
    cli_module.tui_collect_info = lambda c: refreshed.append(1) or {"state": "stopped"}
    intros = []
    cli_module.tui_print_intro = lambda i: intros.append(dict(i))
    lines = iter(["/status", "", "/doctor", "/logs -f api-manager", "start kamailio", "/clear", "clear",
                  "/bogus", "/restart", "bogus", "/exit"])
    orig_read = cli_module.tui_read_line
    cli_module.tui_read_line = lambda c, i: next(lines)
    try:
        out = capture(cli_module.repl_loop, cli, info)
    finally:
        cli_module.tui_collect_info, cli_module.tui_print_intro, cli_module.tui_read_line = orig_collect, orig_intro, orig_read
    assert calls == [("status", []), ("doctor", []), ("logs", ["-f", "api-manager"]), ("start", ["kamailio"]),
                     ("clear", []), ("clear", []), ("restart", [])], calls
    # refresh after status, start, restart (even though it raised); not after doctor/logs/clear
    assert len(refreshed) == 3, refreshed
    assert len(intros) == 2, "header redrawn after both /clear and clear"
    assert "Unknown command: /bogus. Type /help" in out and "Unknown command: bogus. Type 'help'" in out, out
    assert "Error: restart failed" in out and not cli.running
    # plain loop (no TUI snapshot): slash words are not commands, prompt is the classic one
    cli.running = True
    cli.tui_on = False
    seen = []
    answers = iter(["/status", "status", "exit"])
    def responder(prompt=""):
        seen.append(prompt)
        return next(answers)
    with fake_input(responder):
        out = capture(cli_module.repl_loop, cli, None)
    assert "Unknown command: /status. Type 'help'" in out and seen == [cli.get_prompt()] * 3, (out, seen)
    assert calls[-1] == ("status", []) and not cli.running
    # a context keeps the classic prompt and never goes through the frame
    cli.running, cli.context = True, "db"
    ran = []
    cli.run_in_context = lambda line: ran.append(line) or setattr(cli, "running", False)
    cli_module.tui_read_line = lambda c, i: (_ for _ in ()).throw(AssertionError("frame used in a context"))
    try:
        with fake_input(lambda prompt="": "select 1"):
            capture(cli_module.repl_loop, cli, tui_info())
    finally:
        cli_module.tui_read_line = orig_read
    assert ran == ["select 1"]
    print("ok test_tui_repl_loop")


TUI_CHILD = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('voipbin_cli_child', sys.argv[1])\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"
    "m.check_root = lambda: None\n"
    "sys.argv = ['voipbin']\n"
    "m.main()\n"
)


def tui_child_env(tmp_dir, project_dir, **extra):
    home = os.path.join(tmp_dir, "tui_home")
    os.makedirs(home, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "COLUMNS", "LINES", "TERM")}
    env.update({"HOME": home, "VOIPBIN_PROJECT_DIR": project_dir, "TERM": "xterm",
                "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8"})
    env.update(extra)
    return env


def test_tui_fallback_without_terminal(cli_module, tmp_dir, stub_bin, log_path):
    import subprocess
    project_dir = fresh_project(tmp_dir, "p_tui_fallback")
    write_env(project_dir, INTERNAL_ENV)
    cli_path = os.path.join(SCRIPTS_DIR, "voipbin-cli.py")
    for label, extra in (("pipe", {}), ("NO_COLOR", {"NO_COLOR": "1"}), ("TERM=dumb", {"TERM": "dumb"})):
        proc = subprocess.run([sys.executable, "-c", TUI_CHILD, cli_path], input="/status\nstatus\nexit\n",
                              capture_output=True, text=True, timeout=60, cwd=project_dir,
                              env=tui_child_env(tmp_dir, project_dir, **extra))
        out = proc.stdout
        assert proc.returncode == 0, (label, proc.returncode, proc.stderr[-300:])
        assert "\x1b[" not in out, (label, repr(out[:300]))
        assert "Unknown command: /status. Type 'help'" in out, (label, out[-400:])
        assert "voipbin\x01\x02> " in out and "❯" not in out and "Slash commands" not in out, (label, out[-400:])
        assert "Services stopped. Ready to start." in out and "Goodbye!" in out, (label, out[-400:])
    print("ok test_tui_fallback_without_terminal")


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def run_in_pty(tmp_dir, project_dir, keys, columns=100, rows=30, **extra):
    """Run the CLI REPL on a pseudo terminal, send keys, return (exit_status, output)."""
    import fcntl, pty, select, signal, struct, termios, time
    cli_path = os.path.join(SCRIPTS_DIR, "voipbin-cli.py")
    env = tui_child_env(tmp_dir, project_dir, **extra)
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(project_dir)
            os.execvpe(sys.executable, [sys.executable, "-c", TUI_CHILD, cli_path], env)
        finally:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    data = b""
    deadline = time.time() + 40

    def read_for(seconds):
        nonlocal data
        end = min(time.time() + seconds, deadline)
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                data += chunk
        return True
    status = None
    try:
        end = time.time() + 20
        while "❯".encode() not in data and time.time() < end:
            read_for(0.2)
        for key in keys:
            os.write(fd, key.encode())
            if not read_for(1.0):
                break
        read_for(1.0)
        finished = 0
        for _ in range(50):
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
            time.sleep(0.1)
        if not finished:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            status = None
    except ChildProcessError:
        pass
    finally:
        os.close(fd)
    return status, data.decode("utf-8", "replace")


def test_tui_pty_session(cli_module, tmp_dir, stub_bin, log_path):
    try:
        import pty  # noqa: F401  (skipped only where the stdlib has no pty module)
    except ImportError:
        print("skip test_tui_pty_session: no pty module on this platform")
        return
    project_dir = fresh_project(tmp_dir, "p_tui_pty")
    write_env(project_dir, INTERNAL_ENV + ["OPENAI_API_KEY=tui-pty-secret"])
    set_docker_stub(stub_bin, log_path, '''
if [[ "$1 $2" == "compose ps" ]]; then printf "voipbin-db\\tUp 5 minutes\\nvoipbin-mq\\tUp 5 minutes\\n"; exit 0; fi
if [[ "$1 $2" == "compose config" ]]; then printf "db\\nmq\\napi-manager\\n"; exit 0; fi
''')
    try:
        status, raw = run_in_pty(tmp_dir, project_dir, ["/help\r", "\r", "/status\r", "/bogus\r", "version\r", "/exit\r"])
        text = strip_ansi(raw)
        assert status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (status, text[-500:])
        assert "VoIPBin Install CLI" in text and "running 2/3 · mode internal" in text, text[:600]
        assert "Using .env (mode=internal) · /status" in text and "█" in text
        assert "Env: running | Services: 2/3 | DNS: ○ | Net: ○ | Mode: internal" in text, text[-900:]
        assert 'Try "/status" or "/logs <service>" · /help for commands · /exit to quit' in text
        assert "\x1b[?1049" not in raw, "alternate screen must not be used"
        assert "❯ /help" in text and "Slash commands:" in text and "VoIPBin Install CLI" in text
        assert "Unknown command: /bogus. Type /help for available commands." in text
        assert "No version pins found" in text or "Using :latest" in text, text[-600:]
        assert "Services" in text and "tui-pty-secret" not in text
        assert "Goodbye!" in text and "voipbin> " not in text
        # Ctrl+C clears the typed text, Ctrl+D leaves like exit
        status, raw = run_in_pty(tmp_dir, project_dir, ["status", "\x03", "\x04"])
        text = strip_ansi(raw)
        assert status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (status, text[-500:])
        assert "Goodbye!" in text and "Type 'exit' to quit" not in text, text[-400:]
        # narrow terminal keeps every line inside the width
        status, raw = run_in_pty(tmp_dir, project_dir, ["/exit\r"], columns=40, rows=24)
        for row in strip_ansi(raw).splitlines()[:12]:
            assert cli_module.visible_len(row.rstrip("\r")) <= 40, repr(row)
    finally:
        reset_docker_stub(stub_bin, log_path)
    print("ok test_tui_pty_session")


def main():
    tmp_dir = tempfile.mkdtemp(prefix="voipbin-cli-mode-test.")
    prev_path = os.environ.get("PATH", "")
    prev_project_dir = os.environ.get("VOIPBIN_PROJECT_DIR")
    try:
        stub_bin, log_path = make_stub_bin(tmp_dir)
        os.environ["PATH"] = stub_bin + os.pathsep + prev_path

        cli_module = load_module("voipbin_cli", os.path.join(SCRIPTS_DIR, "voipbin-cli.py"))
        # never depend on this host's VoIP interfaces; tests that need one set ("lo",)
        cli_module.INSTALL_VOIP_INTERFACES = ("voipbin-test-none",)

        test_dns_external_gate(cli_module, tmp_dir, log_path)
        test_dns_internal_not_gated(cli_module, tmp_dir, log_path)
        test_certs_external_report(cli_module, tmp_dir, log_path)
        test_certs_external_with_cert_reports_expiry_and_sans(cli_module, tmp_dir, log_path)
        test_certs_internal_not_gated(cli_module, tmp_dir, log_path)
        test_status_urls_from_base_domain(cli_module, tmp_dir, log_path)
        test_test_call_defaults_from_env(tmp_dir)
        test_softphone_defaults_from_env(tmp_dir)
        test_crlf_env_values_are_trimmed(cli_module, tmp_dir)
        test_doctor_registered_and_repl_path_returns(cli_module, tmp_dir, log_path)
        test_doctor_noninteractive_dispatch_propagates_exit_code(cli_module, tmp_dir, log_path)
        test_upgrade_pinned_syncs_live_compose_before_pull(cli_module, tmp_dir, log_path)
        test_tracked_paths_uses_compose_dist_not_live_file(cli_module)
        test_cmd_update_resume_from_pull_detects_unpin_via_dist_file(cli_module, tmp_dir, log_path)
        test_upgrade_pinned_rollback_guidance_uses_scoped_checkout(cli_module, tmp_dir, log_path)
        test_upgrade_pinned_migration_failure_guidance_uses_scoped_checkout(cli_module, tmp_dir, log_path)
        test_upgrade_pinned_compose_up_failure_guidance_uses_scoped_checkout(cli_module, tmp_dir, log_path)
        test_rollback_pinned_guard_uses_scoped_checkout(cli_module, tmp_dir)
        test_init_passes_args_and_propagates_exit_code(cli_module, tmp_dir, log_path)
        test_start_propagates_exit_code_and_skips_pins_when_locked(cli_module, tmp_dir, log_path)
        test_clean_requires_confirmation(cli_module, tmp_dir, stub_bin, log_path)
        test_install_entry_guards(cli_module, tmp_dir, stub_bin, log_path)
        test_install_noninteractive_happy_path_and_failure(cli_module, tmp_dir, stub_bin, log_path)
        test_classify_install_containers(cli_module)
        test_install_verify_timeouts_and_ping(cli_module, tmp_dir)
        test_install_ownership(cli_module, tmp_dir)
        test_install_user_caroot(cli_module, tmp_dir)
        test_install_passes_caroot_to_init_and_start(cli_module, tmp_dir, stub_bin, log_path)
        test_user_facing_text_has_no_sandbox_and_honors_no_color(cli_module, tmp_dir, log_path)
        test_help_and_version_need_no_root(cli_module, tmp_dir)
        test_status_never_prints_service_passwords(cli_module, tmp_dir, stub_bin, log_path)
        test_tui_wanted(cli_module)
        test_tui_clip_and_widths(cli_module)
        test_tui_render_header_notice_statusbar(cli_module)
        test_tui_release_version(cli_module, tmp_dir)
        test_tui_parse_slash(cli_module)
        test_tui_completer_and_help(cli_module, tmp_dir)
        test_tui_collect_info_states_and_secrets(cli_module, tmp_dir, stub_bin, log_path)
        test_tui_read_line_sequences(cli_module, tmp_dir)
        test_tui_repl_loop(cli_module, tmp_dir)
        test_tui_fallback_without_terminal(cli_module, tmp_dir, stub_bin, log_path)
        test_tui_pty_session(cli_module, tmp_dir, stub_bin, log_path)

        print("All test_cli_mode.py tests passed")
    finally:
        os.environ["PATH"] = prev_path
        if prev_project_dir is None:
            os.environ.pop("VOIPBIN_PROJECT_DIR", None)
        else:
            os.environ["VOIPBIN_PROJECT_DIR"] = prev_project_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
