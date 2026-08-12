#!/usr/bin/env python3
"""Tests for the CLI/SIP-tooling mode awareness (VOIP-1275 Phase 6).

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


def main():
    tmp_dir = tempfile.mkdtemp(prefix="voipbin-cli-mode-test.")
    prev_path = os.environ.get("PATH", "")
    prev_project_dir = os.environ.get("VOIPBIN_PROJECT_DIR")
    try:
        stub_bin, log_path = make_stub_bin(tmp_dir)
        os.environ["PATH"] = stub_bin + os.pathsep + prev_path

        cli_module = load_module("voipbin_cli", os.path.join(SCRIPTS_DIR, "voipbin-cli.py"))

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
