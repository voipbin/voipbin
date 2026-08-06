#!/usr/bin/env bats
# Tests for scripts/common.sh's DNS fallback/upstream capture (VOIP-1285)

load 'test_helper'

setup() {
    setup_test_env
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    RESOLV_UPSTREAMS="$TEST_TEMP_DIR/resolv.conf.voipbin-upstreams"
    SYSTEMD_RESOLVE_CONF="$TEST_TEMP_DIR/systemd-resolve.conf"
    export RESOLV_CONF RESOLV_UPSTREAMS SYSTEMD_RESOLVE_CONF
}

teardown() {
    teardown_test_env
}

# =============================================================================
# capture_dns_upstreams: source priority + filtering
# =============================================================================

@test "capture_dns_upstreams initial reads from systemd-resolved live file when present" {
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 192.168.1.1
nameserver 192.168.1.2
EOF
    load_common

    capture_dns_upstreams initial

    assert_file_contains "$RESOLV_UPSTREAMS" "192.168.1.1"
    assert_file_contains "$RESOLV_UPSTREAMS" "192.168.1.2"
}

@test "capture_dns_upstreams initial falls back to plain resolv.conf when no systemd-resolved file" {
    cat > "$RESOLV_CONF" << 'EOF'
nameserver 10.0.0.1
EOF
    load_common

    capture_dns_upstreams initial

    assert_file_contains "$RESOLV_UPSTREAMS" "10.0.0.1"
}

@test "capture_dns_upstreams initial excludes loopback/link-local/IPv6-loopback entries" {
    cat > "$RESOLV_CONF" << 'EOF'
nameserver 127.0.0.53
nameserver 169.254.1.1
nameserver ::1
nameserver 10.0.0.1
EOF
    load_common

    capture_dns_upstreams initial

    assert_file_not_contains "$RESOLV_UPSTREAMS" "127.0.0.53"
    assert_file_not_contains "$RESOLV_UPSTREAMS" "169.254.1.1"
    assert_file_not_contains "$RESOLV_UPSTREAMS" "::1"
    assert_file_contains "$RESOLV_UPSTREAMS" "10.0.0.1"
}

@test "capture_dns_upstreams initial excludes IPv6 link-local addresses across the fe80::/10 block" {
    cat > "$RESOLV_CONF" << 'EOF'
nameserver fe80::1
nameserver FE80::2
nameserver 2001:db8::1
EOF
    load_common

    capture_dns_upstreams initial

    assert_file_not_contains "$RESOLV_UPSTREAMS" "fe80::1"
    assert_file_not_contains "$RESOLV_UPSTREAMS" "FE80::2"
    assert_file_contains "$RESOLV_UPSTREAMS" "2001:db8::1"
}

@test "capture_dns_upstreams initial falls back to hardcoded 8.8.8.8/8.8.4.4 when nothing usable" {
    # No systemd-resolved file, no plain resolv.conf either.
    load_common

    capture_dns_upstreams initial

    assert_file_contains "$RESOLV_UPSTREAMS" "8.8.8.8"
    assert_file_contains "$RESOLV_UPSTREAMS" "8.8.4.4"
}

@test "capture_dns_upstreams refresh never reads plain resolv.conf (source 2 skipped)" {
    # Our own previously-written resolv.conf: if source 2 were consulted,
    # this would be recovered as a "real" upstream, which is wrong at
    # refresh time.
    cat > "$RESOLV_CONF" << 'EOF'
nameserver 127.0.0.1
nameserver 8.8.8.8
EOF
    load_common

    capture_dns_upstreams refresh

    # No systemd-resolved file either, so refresh must fall through to the
    # hardcoded pair, not the plain resolv.conf content above.
    assert_file_contains "$RESOLV_UPSTREAMS" "8.8.8.8"
    assert_file_contains "$RESOLV_UPSTREAMS" "8.8.4.4"
    assert_file_not_contains "$RESOLV_UPSTREAMS" "127.0.0.1"
}

@test "capture_dns_upstreams refresh still reads systemd-resolved live file when present" {
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 192.168.50.1
EOF
    load_common

    capture_dns_upstreams refresh

    assert_file_contains "$RESOLV_UPSTREAMS" "192.168.50.1"
}

@test "capture_dns_upstreams caps at 2 upstream entries" {
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 10.0.0.1
nameserver 10.0.0.2
nameserver 10.0.0.3
EOF
    load_common

    capture_dns_upstreams initial

    result_count=$(command wc -l < "$RESOLV_UPSTREAMS")
    assert_equal "$result_count" "2"
}

@test "capture_dns_upstreams dedupes repeated nameserver lines" {
    cat > "$SYSTEMD_RESOLVE_CONF" << 'EOF'
nameserver 10.0.0.1
nameserver 10.0.0.1
nameserver 10.0.0.2
EOF
    load_common

    capture_dns_upstreams initial

    result_count=$(command wc -l < "$RESOLV_UPSTREAMS")
    assert_equal "$result_count" "2"
    assert_file_contains "$RESOLV_UPSTREAMS" "10.0.0.1"
    assert_file_contains "$RESOLV_UPSTREAMS" "10.0.0.2"
}

# =============================================================================
# write_resolv_conf_with_fallback
# =============================================================================

@test "write_resolv_conf_with_fallback writes 127.0.0.1 first, then captured upstreams" {
    load_common
    printf '10.0.0.1\n10.0.0.2\n' > "$RESOLV_UPSTREAMS"

    write_resolv_conf_with_fallback

    assert_file_contains "$RESOLV_CONF" "nameserver 127.0.0.1"
    assert_file_contains "$RESOLV_CONF" "nameserver 10.0.0.1"
    assert_file_contains "$RESOLV_CONF" "nameserver 10.0.0.2"
    assert_file_contains "$RESOLV_CONF" "options timeout:1 attempts:2"

    # 127.0.0.1 must come before the upstream fallbacks (resolver tries
    # nameservers in listed order).
    local ns_line upstream_line
    ns_line=$(command grep -n "^nameserver 127.0.0.1$" "$RESOLV_CONF" | cut -d: -f1)
    upstream_line=$(command grep -n "^nameserver 10.0.0.1$" "$RESOLV_CONF" | cut -d: -f1)
    [[ "$ns_line" -lt "$upstream_line" ]]
}

@test "write_resolv_conf_with_fallback caps at 2 upstream nameserver lines even if the state file has more" {
    load_common
    # Simulate a $RESOLV_UPSTREAMS that ended up with more than 2 lines
    # (manual edit, future capture-path bug, etc.) — the write step must
    # still cap at 2 (127.0.0.1 + 2 = MAXNS=3), not trust the file as-is.
    printf '10.0.0.1\n10.0.0.2\n10.0.0.3\n10.0.0.4\n' > "$RESOLV_UPSTREAMS"

    write_resolv_conf_with_fallback

    local nameserver_lines
    nameserver_lines=$(command grep -c '^nameserver ' "$RESOLV_CONF")
    assert_equal "$nameserver_lines" "3"  # 127.0.0.1 + 2 upstreams
    assert_file_contains "$RESOLV_CONF" "nameserver 10.0.0.1"
    assert_file_contains "$RESOLV_CONF" "nameserver 10.0.0.2"
    assert_file_not_contains "$RESOLV_CONF" "nameserver 10.0.0.3"
    assert_file_not_contains "$RESOLV_CONF" "nameserver 10.0.0.4"
}

@test "write_resolv_conf_with_fallback unlinks a symlinked resolv.conf instead of writing through it" {
    load_common
    printf '8.8.8.8\n' > "$RESOLV_UPSTREAMS"
    local fake_target="$TEST_TEMP_DIR/stub-resolv.conf"
    echo "nameserver 127.0.0.53" > "$fake_target"
    ln -s "$fake_target" "$RESOLV_CONF"

    write_resolv_conf_with_fallback

    # RESOLV_CONF must now be a regular file, not the old symlink, and the
    # symlink target must be untouched.
    [[ ! -L "$RESOLV_CONF" ]]
    assert_file_contains "$RESOLV_CONF" "nameserver 127.0.0.1"
    assert_file_contains "$fake_target" "nameserver 127.0.0.53"
    assert_file_not_contains "$fake_target" "127.0.0.1"
}
