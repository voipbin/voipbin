#!/usr/bin/env bats
# Tests for scripts/common.sh

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# detect_host_ip() tests
# =============================================================================

@test "detect_host_ip returns IP from .env when HOST_EXTERNAL_IP is set" {
    create_env_file "HOST_EXTERNAL_IP=10.0.0.50"
    load_common

    result=$(detect_host_ip)

    assert_equal "$result" "10.0.0.50"
}

@test "detect_host_ip uses ip route when .env has no HOST_EXTERNAL_IP" {
    create_env_file "OTHER_VAR=something"
    mock_ip_route "192.168.1.100"
    load_common

    result=$(detect_host_ip)

    assert_equal "$result" "192.168.1.100"
}

@test "detect_host_ip uses ip route when .env is missing" {
    # No .env file created
    mock_ip_route "172.16.0.50"
    load_common

    result=$(detect_host_ip)

    assert_equal "$result" "172.16.0.50"
}

@test "detect_host_ip uses hostname -I when ip route fails" {
    create_env_file ""
    # Mock ip to fail
    mock_command "ip" "" 1
    mock_hostname "10.20.30.40"
    load_common

    result=$(detect_host_ip)

    assert_equal "$result" "10.20.30.40"
}

@test "detect_host_ip returns 127.0.0.1 as final fallback" {
    create_env_file ""
    # Mock all commands to fail
    mock_command "ip" "" 1
    mock_command "hostname" "" 1
    mock_command "ipconfig" "" 1
    load_common

    result=$(detect_host_ip)

    assert_equal "$result" "127.0.0.1"
}

@test "detect_host_ip returns valid IP format" {
    mock_ip_route "192.168.5.25"
    load_common

    result=$(detect_host_ip)

    assert_valid_ip "$result"
}

# =============================================================================
# generate_coredns_config() tests
# =============================================================================

@test "generate_coredns_config creates config directory if missing" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir"

    [[ -d "$config_dir" ]]
}

@test "generate_coredns_config creates Corefile" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir"

    [[ -f "$config_dir/Corefile" ]]
}

@test "generate_coredns_config removes Corefile if it's a directory" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"
    mkdir -p "$config_dir/Corefile"  # Create as directory (Docker mount issue)

    generate_coredns_config "192.168.1.100" "$config_dir"

    [[ -f "$config_dir/Corefile" ]]  # Should now be a file
    [[ ! -d "$config_dir/Corefile" ]]
}

@test "generate_coredns_config writes api.voipbin.test with host_ip" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "10.0.0.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" 'api.voipbin.test 60 IN A 10.0.0.100'
}

@test "generate_coredns_config writes admin.voipbin.test with host_ip" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "10.0.0.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" 'admin.voipbin.test 60 IN A 10.0.0.100'
}

@test "generate_coredns_config writes meet.voipbin.test with host_ip" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "10.0.0.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" 'meet.voipbin.test 60 IN A 10.0.0.100'
}

@test "generate_coredns_config writes talk.voipbin.test with host_ip" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "10.0.0.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" 'talk.voipbin.test 60 IN A 10.0.0.100'
}

@test "generate_coredns_config writes voipbin.test catch-all with kamailio_ip" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "10.0.0.100" "$config_dir" "10.0.0.200"

    # The catch-all uses template syntax with kamailio_ip
    assert_file_contains "$config_dir/Corefile" '60 IN A 10.0.0.200'
}

@test "generate_coredns_config uses host_ip as kamailio_ip when not specified" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "10.0.0.100" "$config_dir"

    # When kamailio_ip not specified, it defaults to host_ip
    # The voipbin.test block should have the host_ip
    grep -A5 '^voipbin.test {' "$config_dir/Corefile" | grep -q '10.0.0.100'
}

@test "generate_coredns_config includes forward zone with 8.8.8.8" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" 'forward . 8.8.8.8 8.8.4.4'
}

@test "generate_coredns_config includes cache directive" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" 'cache 30'
}

# =============================================================================
# get_env_var() tests
# =============================================================================

@test "get_env_var returns empty for absent variable" {
    create_env_file "OTHER_VAR=value"
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MISSING_VAR")

    assert_equal "$result" ""
}

@test "get_env_var returns empty for missing file" {
    load_common

    result=$(get_env_var "$PROJECT_DIR/does-not-exist" "ANY_VAR")

    assert_equal "$result" ""
}

@test "get_env_var last occurrence wins" {
    create_env_file "MY_VAR=first" "OTHER=x" "MY_VAR=second"
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MY_VAR")

    assert_equal "$result" "second"
}

@test "get_env_var preserves values containing equals signs" {
    create_env_file "MY_VAR=a=b=c"
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MY_VAR")

    assert_equal "$result" "a=b=c"
}

@test "get_env_var never executes command substitution content" {
    create_env_file 'EVIL_VAR=$(touch '"$TEST_TEMP_DIR"'/pwned)'
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "EVIL_VAR")

    # Value returned literally, side effect never executed
    assert_equal "$result" '$(touch '"$TEST_TEMP_DIR"'/pwned)'
    [[ ! -f "$TEST_TEMP_DIR/pwned" ]]
}

@test "get_env_var strips CRLF line endings and surrounding whitespace" {
    # CRLF-saved .env (e.g. edited on Windows): the trailing \r must never
    # leak into strict-equality mode/TLS comparisons.
    printf 'DOMAIN_MODE=external\r\nBASE_DOMAIN=example.com\r\nPADDED_VAR=  padded value  \r\n' \
        > "$PROJECT_DIR/.env"
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "BASE_DOMAIN")
    assert_equal "$result" "example.com"

    result=$(get_env_var "$PROJECT_DIR/.env" "PADDED_VAR")
    assert_equal "$result" "padded value"

    result=$(get_domain_mode "$PROJECT_DIR/.env")
    assert_equal "$result" "external"
}

# =============================================================================
# get_domain_mode() tests
# =============================================================================

@test "get_domain_mode returns internal when DOMAIN_MODE absent (legacy .env)" {
    create_env_file "BASE_DOMAIN=voipbin.test"
    load_common

    result=$(get_domain_mode "$PROJECT_DIR/.env")

    assert_equal "$result" "internal"
}

@test "get_domain_mode returns internal for missing .env" {
    load_common

    result=$(get_domain_mode "$PROJECT_DIR/does-not-exist")

    assert_equal "$result" "internal"
}

@test "get_domain_mode returns external when DOMAIN_MODE=external" {
    create_env_file "DOMAIN_MODE=external"
    load_common

    result=$(get_domain_mode "$PROJECT_DIR/.env")

    assert_equal "$result" "external"
}

# =============================================================================
# derive_domain_env() tests (mode-1 no-regression invariant, design §2.1)
# =============================================================================

@test "derive_domain_env voipbin.test is byte-identical to the historic literals" {
    load_common

    derive_domain_env "voipbin.test"

    assert_equal "$DERIVED_API_URL" "https://api.voipbin.test:8443/"
    assert_equal "$DERIVED_WEBSOCKET_URL" "wss://api.voipbin.test:8443/v1.0/ws"
    assert_equal "$DERIVED_REGISTRAR_URL" "wss://sip.voipbin.test:5066"
    assert_equal "$DERIVED_REGISTRAR_DOMAIN" "registrar.voipbin.test"
    assert_equal "$DERIVED_CONFERENCE_URL" "wss://conference.voipbin.test"
    assert_equal "$DERIVED_CONFERENCE_DOMAIN" "conference.voipbin.test"
    assert_equal "$DERIVED_DOMAIN_NAME_EXTENSION" "registrar.voipbin.test"
    assert_equal "$DERIVED_DOMAIN_NAME_TRUNK" "trunk.voipbin.test"
    assert_equal "$DERIVED_EMAIL_VERIFY_BASE_URL" "https://api.voipbin.test:8443"
    assert_equal "$DERIVED_BASE_DOMAIN" "voipbin.test"
    assert_equal "$DERIVED_BASE_HOSTNAME" "voipbin.test"
}

@test "derive_domain_env example.com spot-checks" {
    load_common

    derive_domain_env "example.com"

    assert_equal "$DERIVED_API_URL" "https://api.example.com:8443/"
    assert_equal "$DERIVED_WEBSOCKET_URL" "wss://api.example.com:8443/v1.0/ws"
    assert_equal "$DERIVED_REGISTRAR_DOMAIN" "registrar.example.com"
    assert_equal "$DERIVED_DOMAIN_NAME_EXTENSION" "registrar.example.com"
    assert_equal "$DERIVED_EMAIL_VERIFY_BASE_URL" "https://api.example.com:8443"
    assert_equal "$DERIVED_BASE_DOMAIN" "example.com"
}

# =============================================================================
# update_env_ips() mode gating (design §2.4)
# =============================================================================

@test "update_env_ips external mode: IPs updated, URLs untouched" {
    create_env_file \
        "DOMAIN_MODE=external" \
        "BASE_DOMAIN=example.com" \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108" \
        "RTPENGINE_EXTERNAL_IP=192.168.1.109" \
        "API_URL=https://api.example.com:8443/" \
        "WEBSOCKET_URL=wss://api.example.com:8443/v1.0/ws"
    load_common

    run update_env_ips "10.0.0.50"

    [[ "$status" -eq 0 ]]
    assert_file_contains "$PROJECT_DIR/.env" "HOST_EXTERNAL_IP=10.0.0.50"
    assert_file_contains "$PROJECT_DIR/.env" "KAMAILIO_EXTERNAL_IP=10.0.0.58"
    assert_file_contains "$PROJECT_DIR/.env" "RTPENGINE_EXTERNAL_IP=10.0.0.59"
    # URLs must NOT be rewritten in external mode
    assert_file_contains "$PROJECT_DIR/.env" "API_URL=https://api.example.com:8443/"
    assert_file_contains "$PROJECT_DIR/.env" "WEBSOCKET_URL=wss://api.example.com:8443/v1.0/ws"
    assert_file_not_contains "$PROJECT_DIR/.env" "voipbin.test"
}

@test "update_env_ips internal mode composes URLs from BASE_DOMAIN (non-default)" {
    create_env_file \
        "DOMAIN_MODE=internal" \
        "BASE_DOMAIN=lab.internal" \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108" \
        "RTPENGINE_EXTERNAL_IP=192.168.1.109" \
        "API_URL=https://api.stale.example:8443/" \
        "WEBSOCKET_URL=wss://api.stale.example:8443/v1.0/ws"
    load_common

    run update_env_ips "10.0.0.50"

    [[ "$status" -eq 0 ]]
    assert_file_contains "$PROJECT_DIR/.env" "API_URL=https://api.lab.internal:8443/"
    assert_file_contains "$PROJECT_DIR/.env" "WEBSOCKET_URL=wss://api.lab.internal:8443/v1.0/ws"
}

@test "update_env_ips legacy .env (no mode, no BASE_DOMAIN) falls back to voipbin.test" {
    create_env_file \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108" \
        "RTPENGINE_EXTERNAL_IP=192.168.1.109" \
        "API_URL=https://api.stale.example:8443/" \
        "WEBSOCKET_URL=wss://api.stale.example:8443/v1.0/ws"
    load_common

    run update_env_ips "10.0.0.50"

    [[ "$status" -eq 0 ]]
    assert_file_contains "$PROJECT_DIR/.env" "API_URL=https://api.voipbin.test:8443/"
    assert_file_contains "$PROJECT_DIR/.env" "WEBSOCKET_URL=wss://api.voipbin.test:8443/v1.0/ws"
}

# =============================================================================
# regenerate_ssl_certs() TLS gating (design §2.4)
# =============================================================================

@test "regenerate_ssl_certs skips with return 0 when TLS_MODE=byo" {
    create_env_file \
        "TLS_MODE=byo" \
        "API_SSL_CERT_BASE64=original-cert-b64" \
        "API_SSL_PRIVKEY_BASE64=original-key-b64"
    mkdir -p "$PROJECT_DIR/certs/api"
    echo "REAL CA CERT - DO NOT TOUCH" > "$PROJECT_DIR/certs/api/cert.pem"
    # A mkcert invocation would prove the gate failed
    mock_command_script "mkcert" "touch '$TEST_TEMP_DIR/mkcert-was-called'; exit 0"
    load_common

    run regenerate_ssl_certs "10.0.0.50"

    # return 0, NOT 1: callers invoke this bare under set -e
    [[ "$status" -eq 0 ]]
    [[ "$output" == *'TLS_MODE=byo'* ]]
    [[ ! -f "$TEST_TEMP_DIR/mkcert-was-called" ]]
    [[ "$(cat "$PROJECT_DIR/certs/api/cert.pem")" == "REAL CA CERT - DO NOT TOUCH" ]]
    assert_file_contains "$PROJECT_DIR/.env" "API_SSL_CERT_BASE64=original-cert-b64"
}

# =============================================================================
# detect_os() tests
# =============================================================================

@test "detect_os returns linux on Linux" {
    mock_uname "Linux"
    load_common

    result=$(detect_os)

    assert_equal "$result" "linux"
}

@test "detect_os returns macos on Darwin" {
    mock_uname "Darwin"
    load_common

    result=$(detect_os)

    assert_equal "$result" "macos"
}

@test "detect_os returns unknown for other systems" {
    mock_uname "FreeBSD"
    load_common

    result=$(detect_os)

    assert_equal "$result" "unknown"
}
