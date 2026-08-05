#!/usr/bin/env bats
# Tests for scripts/setup-dns.sh

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# Helper to load setup-dns.sh functions
# =============================================================================

load_dns_functions() {
    # Source common.sh first (setup-dns.sh depends on it)
    source "$SCRIPTS_DIR/common.sh"

    # Extract functions from setup-dns.sh (up to main "$@")
    local temp_dns="$TEST_TEMP_DIR/dns_functions.sh"

    sed -n '1,/^main "\$@"/p' "$SCRIPTS_DIR/setup-dns.sh" | \
        sed -e '$d' \
            -e 's/^set -e$/# set -e  # disabled for testing/' \
            -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
            -e 's|^PROJECT_DIR=.*|# PROJECT_DIR overridden by test|' > "$temp_dns"

    source "$temp_dns"

    # Override paths to use test environment (after sourcing to ensure they stick)
    SCRIPT_DIR="$SCRIPTS_DIR"
    PROJECT_DIR="$TEST_TEMP_DIR"
}

# =============================================================================
# check_coredns() tests
# =============================================================================

@test "check_coredns returns 0 when voipbin-dns container is running" {
    mock_command_script "docker" '
if [[ "$1" == "ps" ]]; then
    echo "voipbin-dns"
    echo "voipbin-db"
fi
'
    load_dns_functions

    run check_coredns

    [[ "$status" -eq 0 ]]
}

@test "check_coredns returns 1 when voipbin-dns container is not running" {
    mock_command_script "docker" '
if [[ "$1" == "ps" ]]; then
    echo "voipbin-db"
    echo "voipbin-redis"
fi
'
    load_dns_functions

    run check_coredns

    [[ "$status" -eq 1 ]]
}

@test "check_coredns returns 1 when docker fails" {
    mock_command "docker" "" 1
    load_dns_functions

    run check_coredns

    [[ "$status" -eq 1 ]]
}

# =============================================================================
# regenerate_corefile() tests
# =============================================================================

@test "regenerate_corefile creates CoreDNS config directory" {
    mock_command_script "docker" 'exit 1'  # CoreDNS not running
    load_dns_functions

    local config_dir="$PROJECT_DIR/config/coredns"

    regenerate_corefile "192.168.1.100" 2>/dev/null || true

    [[ -d "$config_dir" ]]
}

@test "regenerate_corefile reads KAMAILIO_EXTERNAL_IP from .env" {
    mock_command_script "docker" 'exit 1'  # CoreDNS not running
    create_env_file "KAMAILIO_EXTERNAL_IP=10.0.0.200"
    load_dns_functions

    regenerate_corefile "192.168.1.100" 2>/dev/null || true

    # Should use the kamailio IP from .env for SIP services
    assert_file_contains "$PROJECT_DIR/config/coredns/Corefile" "10.0.0.200"
}

@test "regenerate_corefile uses host_ip when KAMAILIO_EXTERNAL_IP not in .env" {
    mock_command_script "docker" 'exit 1'  # CoreDNS not running
    create_env_file "OTHER_VAR=value"
    load_dns_functions

    regenerate_corefile "192.168.1.100" 2>/dev/null || true

    # Should use host_ip as fallback for kamailio_ip
    assert_file_contains "$PROJECT_DIR/config/coredns/Corefile" "192.168.1.100"
}

@test "regenerate_corefile refreshes DNS fallback upstreams when the host IP actually changed" {
    setup_dns_test_paths
    mock_command_script "docker" 'exit 1'  # CoreDNS not running
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 10.5.5.5
EOF
    # .env's configured IP differs from the host_ip passed in below, so
    # regenerate_corefile's inner "IP actually changed" branch fires.
    create_env_file "HOST_EXTERNAL_IP=192.168.1.50"
    load_dns_functions

    regenerate_corefile "192.168.1.100" "true" 2>/dev/null || true

    assert_file_contains "$RESOLV_UPSTREAMS" "10.5.5.5"
    assert_file_contains "$RESOLV_CONF" "nameserver 127.0.0.1"
    assert_file_contains "$RESOLV_CONF" "nameserver 10.5.5.5"
}

@test "regenerate_corefile does not touch DNS fallback state when force_update is set but the IP is unchanged" {
    setup_dns_test_paths
    mock_command_script "docker" 'exit 1'
    create_env_file "HOST_EXTERNAL_IP=192.168.1.100"
    load_dns_functions

    # force_update=true, but host_ip matches .env's configured IP, so the
    # inner "actually changed" branch (and the DNS refresh inside it) must
    # not fire.
    regenerate_corefile "192.168.1.100" "true" 2>/dev/null || true

    [[ ! -f "$RESOLV_UPSTREAMS" ]]
    [[ ! -f "$RESOLV_CONF" ]]
}

# =============================================================================
# Script structure tests
# =============================================================================

@test "setup-dns.sh defines setup_linux function" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "setup_linux()"
}

@test "setup-dns.sh defines setup_macos function" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "setup_macos()"
}

@test "setup-dns.sh defines uninstall_linux function" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "uninstall_linux()"
}

@test "setup-dns.sh defines uninstall_macos function" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "uninstall_macos()"
}

@test "setup-dns.sh defines test_dns function" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "test_dns()"
}

@test "setup-dns.sh sources common.sh" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" 'source "$SCRIPT_DIR/common.sh"'
}

# =============================================================================
# Linux DNS configuration tests
# =============================================================================

@test "setup-dns.sh configures resolv.conf to use 127.0.0.1 on Linux" {
    # Verify the script writes nameserver 127.0.0.1 for CoreDNS
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "nameserver 127.0.0.1"
}

@test "setup-dns.sh backs up resolv.conf before modifying" {
    # Verify backup mechanism exists
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "RESOLV_BACKUP"
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "/etc/resolv.conf.voipbin-backup"
}

@test "setup-dns.sh restores resolv.conf on uninstall" {
    # Verify restore mechanism exists
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" 'cp "$RESOLV_BACKUP"'
}

# =============================================================================
# setup_linux() / uninstall_linux() behavioral tests (VOIP-1285)
# =============================================================================
# Known, deliberate gap: setup_linux()'s cleanup-before-write reorder (the
# systemd-resolved/NetworkManager drop-in cleanup blocks moving before the
# resolv.conf write) is not covered by a call-order-recording bats test
# here. Those cleanup blocks are gated on hardcoded, non-overridable real
# paths (/etc/systemd/resolved.conf.d/voipbin-sandbox.conf,
# /etc/NetworkManager/...) — staging them would require root-owned /etc
# writes, defeating the point of unprivileged bats coverage. The reorder
# itself was verified by direct code inspection (multiple design/code
# review rounds traced the exact line numbers); see
# docs/plans/2026-08-02-dns-hijack-spof-design.md §2 and
# docs/plans/2026-08-02-dns-hijack-spof-implementation-plan.md Step 2.

# Points RESOLV_CONF/RESOLV_BACKUP/RESOLV_UPSTREAMS/SYSTEMD_RESOLVE_CONF at
# TEST_TEMP_DIR so setup_linux()/uninstall_linux() can be exercised for
# real without touching the host's actual /etc.
setup_dns_test_paths() {
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    RESOLV_BACKUP="$TEST_TEMP_DIR/resolv.conf.voipbin-backup"
    RESOLV_UPSTREAMS="$TEST_TEMP_DIR/resolv.conf.voipbin-upstreams"
    SYSTEMD_RESOLVE_CONF="$TEST_TEMP_DIR/systemd-resolve.conf"
    export RESOLV_CONF RESOLV_BACKUP RESOLV_UPSTREAMS SYSTEMD_RESOLVE_CONF
}

@test "setup_linux fresh install produces non-empty fallback lines" {
    setup_dns_test_paths
    mock_command "systemctl" "" 0
    load_dns_functions

    setup_linux "192.168.1.100"

    assert_file_contains "$RESOLV_CONF" "nameserver 127.0.0.1"
    # $RESOLV_UPSTREAMS stores bare IPs, one per line (the "nameserver "
    # prefix is added later by write_resolv_conf_with_fallback).
    result_count=$(command wc -l < "$RESOLV_UPSTREAMS")
    [[ "$result_count" -gt 0 ]]
}

@test "setup_linux with a valid pre-existing state file and unchanged IP genuinely skips recapture" {
    # Distinguishing test: pre-seed $RESOLV_UPSTREAMS with a "canary" value
    # that no real capture path (source 1: absent here; source 2: reads
    # $RESOLV_CONF, which doesn't exist yet in this test; source 3: the
    # hardcoded 8.8.8.8/8.8.4.4 pair) could ever reproduce. If the "is
    # there a valid state file already" guard is broken (e.g. checking for
    # a "nameserver " prefix that $RESOLV_UPSTREAMS never contains), the
    # canary gets silently overwritten by whatever `capture_dns_upstreams
    # initial` finds instead of being left alone — this test catches that
    # even when every capture path would happen to reach the same output
    # a passing test could otherwise mistake for "correctly skipped".
    setup_dns_test_paths
    mock_command "systemctl" "" 0
    create_env_file "HOST_EXTERNAL_IP=192.168.1.100"
    mock_ip_route "192.168.1.100"  # unchanged vs .env -> check_ip_changed is false
    printf '203.0.113.9\n' > "$RESOLV_UPSTREAMS"
    load_dns_functions

    setup_linux "192.168.1.100"

    local upstreams_content
    upstreams_content=$(cat "$RESOLV_UPSTREAMS")
    assert_equal "$upstreams_content" "203.0.113.9"
    assert_file_not_contains "$RESOLV_UPSTREAMS" "8.8.8.8"
}

@test "setup_linux idempotent re-run with unchanged IP does not recapture upstreams" {
    setup_dns_test_paths
    mock_command "systemctl" "" 0
    create_env_file "HOST_EXTERNAL_IP=192.168.1.100"
    mock_ip_route "192.168.1.100"
    load_dns_functions

    setup_linux "192.168.1.100"
    local first_capture
    first_capture=$(cat "$RESOLV_UPSTREAMS")

    setup_linux "192.168.1.100"
    local second_capture
    second_capture=$(cat "$RESOLV_UPSTREAMS")

    assert_equal "$second_capture" "$first_capture"
    assert_file_contains "$RESOLV_CONF" "nameserver 127.0.0.1"
}

@test "setup_linux re-run with changed IP refreshes upstreams" {
    setup_dns_test_paths
    mock_command "systemctl" "" 0
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 10.1.1.1
EOF
    create_env_file "HOST_EXTERNAL_IP=192.168.1.100"
    mock_ip_route "192.168.1.100"
    load_dns_functions

    setup_linux "192.168.1.100"
    assert_file_contains "$RESOLV_UPSTREAMS" "10.1.1.1"

    # Simulate a network change: new systemd-resolved upstream, current IP
    # now differs from the .env-configured one.
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 10.2.2.2
EOF
    mock_ip_route "192.168.1.200"

    setup_linux "192.168.1.200"

    assert_file_contains "$RESOLV_UPSTREAMS" "10.2.2.2"
    assert_file_not_contains "$RESOLV_UPSTREAMS" "10.1.1.1"
}

@test "setup_linux treats an empty upstreams state file as absent and recaptures" {
    setup_dns_test_paths
    mock_command "systemctl" "" 0
    create_env_file "HOST_EXTERNAL_IP=192.168.1.100"
    mock_ip_route "192.168.1.100"
    : > "$RESOLV_UPSTREAMS"  # existing but empty
    load_dns_functions

    setup_linux "192.168.1.100"

    # $RESOLV_UPSTREAMS stores bare IPs, one per line (the "nameserver "
    # prefix is added later by write_resolv_conf_with_fallback).
    result_count=$(command wc -l < "$RESOLV_UPSTREAMS")
    [[ "$result_count" -gt 0 ]]
}

@test "setup_linux logs a disclosure warning when systemd-resolved is active" {
    setup_dns_test_paths
    mock_command "systemctl" "" 0  # is-active exits 0 -> "active"
    load_dns_functions

    run setup_linux "192.168.1.100"

    if [[ "$output" != *"no longer manages"* ]]; then
        echo "disclosure warning did not fire though systemd-resolved is active" >&2
        return 1
    fi
}

@test "setup_linux does not log the disclosure warning when systemd-resolved is inactive" {
    setup_dns_test_paths
    mock_command "systemctl" "" 1  # is-active exits nonzero -> "inactive"
    load_dns_functions

    run setup_linux "192.168.1.100"

    if [[ "$output" == *"no longer manages"* ]]; then
        echo "disclosure warning fired even though systemd-resolved is inactive" >&2
        return 1
    fi
}

@test "uninstall_linux removes the upstreams state file alongside the backup" {
    setup_dns_test_paths
    mock_command "systemctl" "" 0
    load_dns_functions

    setup_linux "192.168.1.100"
    [[ -f "$RESOLV_UPSTREAMS" ]]

    uninstall_linux

    [[ ! -f "$RESOLV_UPSTREAMS" ]]
    [[ ! -f "$RESOLV_BACKUP" ]]
}

# =============================================================================
# macOS DNS configuration tests
# =============================================================================

@test "setup-dns.sh creates /etc/resolver directory on macOS" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "mkdir -p /etc/resolver"
}

@test "setup-dns.sh creates resolver file for voipbin.test on macOS" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "/etc/resolver/voipbin.test"
}

@test "setup-dns.sh flushes DNS cache on macOS" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "dscacheutil -flushcache"
}

# =============================================================================
# Command line argument tests
# =============================================================================

@test "setup-dns.sh supports --uninstall flag" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "--uninstall"
}

@test "setup-dns.sh supports --test flag" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "--test"
}

@test "setup-dns.sh supports --regenerate flag" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "--regenerate"
}

@test "setup-dns.sh supports -y flag for non-interactive mode" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "|-y)"
}

# =============================================================================
# test_dns() function tests
# =============================================================================

@test "test_dns tests expected domains" {
    # Verify test_dns checks all required domains
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "api.voipbin.test"
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "admin.voipbin.test"
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "meet.voipbin.test"
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "talk.voipbin.test"
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "sip.voipbin.test"
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" "registrar.voipbin.test"
}

@test "test_dns uses dig or nslookup for resolution" {
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" 'command -v dig'
    assert_file_contains "$SCRIPTS_DIR/setup-dns.sh" 'command -v nslookup'
}
