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
