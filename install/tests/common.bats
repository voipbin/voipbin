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
# generate_caddy_config() tests (VOIP-1325)
# =============================================================================

@test "generate_caddy_config creates config directory if missing" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    [[ -d "$config_dir" ]]
}

@test "generate_caddy_config creates Caddyfile" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    [[ -f "$config_dir/Caddyfile" ]]
}

@test "generate_caddy_config removes Caddyfile if it's a directory" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"
    mkdir -p "$config_dir/Caddyfile"  # Docker mount issue, same as coredns

    generate_caddy_config "example.com" "$config_dir" "/certs"

    [[ -f "$config_dir/Caddyfile" ]]
    [[ ! -d "$config_dir/Caddyfile" ]]
}

@test "generate_caddy_config writes an api.<domain> site block" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'api.example.com {'
}

@test "generate_caddy_config writes an admin.<domain> site block" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'admin.example.com {'
}

@test "generate_caddy_config writes a meet.<domain> site block" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'meet.example.com {'
}

@test "generate_caddy_config writes a talk.<domain> site block" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'talk.example.com {'
}

@test "generate_caddy_config proxies api.<domain> to api-manager over HTTPS with pinned name and trust" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'reverse_proxy https://api-manager:443'
    assert_file_contains "$config_dir/Caddyfile" 'tls_server_name api.example.com'
    # tls_trust_pool (not the public root store): a self-signed or
    # internal-CA BYO cert is a fully supported install (install-certs.sh's
    # validate_cert() never requires a publicly-trusted chain) — round 2
    # review HIGH-A found tls_server_name alone 502s every request for that
    # class of cert, since it only pins the verification NAME, not trust.
    # (round 3 review LOW: tls_trusted_ca_certs is deprecated in the pinned
    # caddy:2-alpine image in favor of this field.)
    assert_file_contains "$config_dir/Caddyfile" 'tls_trust_pool file /certs/api/cert.pem'
    assert_file_not_contains "$config_dir/Caddyfile" 'tls_insecure_skip_verify'
}

@test "generate_caddy_config proxies admin.<domain> to square-admin over plain HTTP" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'reverse_proxy http://square-admin:80'
}

@test "generate_caddy_config proxies meet.<domain> to square-meet over plain HTTP" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'reverse_proxy http://square-meet:80'
}

@test "generate_caddy_config proxies talk.<domain> to square-talk over plain HTTP" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'reverse_proxy http://square-talk:80'
}

@test "generate_caddy_config uses the given certs_dir for the tls directive" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir" "/certs"

    assert_file_contains "$config_dir/Caddyfile" 'tls /certs/api/cert.pem /certs/api/privkey.pem'
}

@test "generate_caddy_config defaults certs_dir to /certs (Caddy container mount point)" {
    load_common
    local config_dir="$TEST_TEMP_DIR/config/caddy"

    generate_caddy_config "example.com" "$config_dir"

    assert_file_contains "$config_dir/Caddyfile" 'tls /certs/api/cert.pem /certs/api/privkey.pem'
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

# VOIP-1328 review finding H1: `docker compose`'s own .env parsing strips a
# single matched pair of surrounding quotes; get_env_var previously did not,
# so a quoted DATABASE_ASTERISK_PASSWORD in .env would disagree with what
# the asterisk-registrar container actually received - silently recreating
# the exact realtime-auth failure the dedicated DB user was meant to fix.
@test "get_env_var strips a matched pair of surrounding double quotes" {
    create_env_file 'MY_VAR="quoted value"'
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MY_VAR")

    assert_equal "$result" "quoted value"
}

@test "get_env_var strips a matched pair of surrounding single quotes" {
    create_env_file "MY_VAR='quoted value'"
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MY_VAR")

    assert_equal "$result" "quoted value"
}

@test "get_env_var leaves a single unmatched quote character alone" {
    create_env_file 'MY_VAR=has"onequote'
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MY_VAR")

    assert_equal "$result" 'has"onequote'
}

@test "get_env_var leaves mismatched quote pairs (double...single) alone" {
    printf 'MY_VAR="mismatched%s\n' "'" > "$PROJECT_DIR/.env"
    load_common

    result=$(get_env_var "$PROJECT_DIR/.env" "MY_VAR")

    assert_equal "$result" "\"mismatched'"
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

@test "derive_domain_env with web_reverse_proxy=true drops the :8443 suffix from api URLs (VOIP-1325)" {
    load_common

    derive_domain_env "example.com" "true"

    assert_equal "$DERIVED_API_URL" "https://api.example.com/"
    assert_equal "$DERIVED_WEBSOCKET_URL" "wss://api.example.com/v1.0/ws"
    assert_equal "$DERIVED_EMAIL_VERIFY_BASE_URL" "https://api.example.com"
    # Unaffected: SIP/conference URLs never went through :8443 in the first place
    assert_equal "$DERIVED_REGISTRAR_URL" "wss://sip.example.com:5066"
    assert_equal "$DERIVED_CONFERENCE_URL" "wss://conference.example.com"
}

@test "derive_domain_env with web_reverse_proxy omitted defaults to keeping :8443 (backward compatible)" {
    load_common

    derive_domain_env "example.com"

    assert_equal "$DERIVED_API_URL" "https://api.example.com:8443/"
}

@test "derive_domain_env with web_reverse_proxy=false explicitly keeps :8443" {
    load_common

    derive_domain_env "example.com" "false"

    assert_equal "$DERIVED_API_URL" "https://api.example.com:8443/"
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

@test "update_env_ips leaves pinned Kamailio/RTPEngine IPs untouched, still updates host IP" {
    create_env_file \
        "DOMAIN_MODE=external" \
        "BASE_DOMAIN=example.com" \
        "HOST_EXTERNAL_IP=104.243.38.39" \
        "KAMAILIO_EXTERNAL_IP=199.127.61.42" \
        "RTPENGINE_EXTERNAL_IP=199.127.61.134" \
        "EXTERNAL_IP_PINNED=true" \
        "API_URL=https://api.example.com:8443/" \
        "WEBSOCKET_URL=wss://api.example.com:8443/v1.0/ws"
    load_common

    run update_env_ips "104.243.38.99"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'unchanged (EXTERNAL_IP_PINNED=true)'* ]]
    assert_file_contains "$PROJECT_DIR/.env" "HOST_EXTERNAL_IP=104.243.38.99"
    # provider-registered IPs must survive the host+8 offset recompute
    assert_file_contains "$PROJECT_DIR/.env" "KAMAILIO_EXTERNAL_IP=199.127.61.42"
    assert_file_contains "$PROJECT_DIR/.env" "RTPENGINE_EXTERNAL_IP=199.127.61.134"
}

@test "update_env_ips returns the pinned Kamailio IP as its actual value (not empty)" {
    # Regression test: the pinned branch previously never assigned
    # new_kamailio_ip, so the function's `echo "$new_kamailio_ip"` return
    # value (consumed by regenerate_ip_config() for CoreDNS regeneration in
    # internal mode) was empty instead of the real, unchanged IP.
    create_env_file \
        "DOMAIN_MODE=internal" \
        "BASE_DOMAIN=voipbin.test" \
        "HOST_EXTERNAL_IP=104.243.38.39" \
        "KAMAILIO_EXTERNAL_IP=199.127.61.42" \
        "RTPENGINE_EXTERNAL_IP=199.127.61.134" \
        "EXTERNAL_IP_PINNED=true"
    load_common

    result="$(update_env_ips "104.243.38.99")"
    last_line="$(echo "$result" | tail -1)"

    [[ "$last_line" == "199.127.61.42" ]]
}

@test "update_env_ips's captured return value is the bare IP with no log noise (real caller pattern)" {
    # regenerate_ip_config() (common.sh) calls this exactly as:
    #   local new_kamailio_ip=$(update_env_ips "$current_ip")
    # then feeds $new_kamailio_ip straight into generate_coredns_config() as
    # a DNS answer value. Command substitution captures ALL of stdout, so
    # if update_env_ips's log_info calls aren't routed to stderr, this
    # variable ends up holding the entire multi-line log transcript instead
    # of an IP — which generate_coredns_config would then write verbatim
    # into the Corefile, producing invalid CoreDNS zone syntax. Assert on
    # the exact caller pattern, not a post-hoc `tail -1` of the raw output,
    # so a regression here is caught even if some other line happens to be
    # a bare IP too.
    create_env_file \
        "DOMAIN_MODE=internal" \
        "BASE_DOMAIN=voipbin.test" \
        "HOST_EXTERNAL_IP=104.243.38.39" \
        "KAMAILIO_EXTERNAL_IP=199.127.61.42" \
        "RTPENGINE_EXTERNAL_IP=199.127.61.134" \
        "EXTERNAL_IP_PINNED=true"
    load_common

    local new_kamailio_ip
    new_kamailio_ip=$(update_env_ips "104.243.38.99")

    assert_equal "$new_kamailio_ip" "199.127.61.42"
}

@test "update_env_ips treats EXTERNAL_IP_PINNED=TRUE (uppercase) as pinned, not as unset/false" {
    # Safety guard must fail closed on unexpected casing: init.sh itself
    # only ever writes lowercase true/false, so this only matters for a
    # hand-edited .env, but the wrong default here would silently replace
    # a provider-registered IP with an auto-generated one nobody owns.
    create_env_file \
        "DOMAIN_MODE=external" \
        "BASE_DOMAIN=example.com" \
        "HOST_EXTERNAL_IP=104.243.38.39" \
        "KAMAILIO_EXTERNAL_IP=199.127.61.42" \
        "RTPENGINE_EXTERNAL_IP=199.127.61.134" \
        "EXTERNAL_IP_PINNED=TRUE"
    load_common

    run update_env_ips "104.243.38.99"

    [[ "$output" == *'unchanged (EXTERNAL_IP_PINNED=true)'* ]]
    assert_file_contains "$PROJECT_DIR/.env" "KAMAILIO_EXTERNAL_IP=199.127.61.42"
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

# =============================================================================
# provision_asterisk_db_user() tests (VOIP-1328)
#
# docker is stubbed via mock_command_script so these run with zero real
# containers. The mock records every stdin payload it receives to a file
# so tests can assert on the exact SQL sent, and on whether the password
# ever appears as a literal docker-exec ARGUMENT (which would leak through
# `ps aux` — the whole reason it's piped via stdin instead of on argv).
# =============================================================================

stub_docker_capturing_run() {
    local log_file="$1"
    mock_command_script "docker" "
echo \"ARGS: \$*\" >> '$log_file'
if [[ \"\$1\" == \"exec\" ]]; then
    cat >> '$log_file'
    echo '' >> '$log_file'
fi
exit 0
"
}

@test "provision_asterisk_db_user is a no-op when .env has no DATABASE_ASTERISK_USERNAME (backward compat)" {
    # No .env file at all - matches a pre-VOIP-1328 install.
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    [[ ! -f "$log_file" ]]
}

@test "provision_asterisk_db_user is a no-op when DATABASE_ASTERISK_USERNAME=root" {
    create_env_file "DATABASE_ASTERISK_USERNAME=root" "DATABASE_ASTERISK_PASSWORD=whatever"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    [[ ! -f "$log_file" ]]
}

@test "provision_asterisk_db_user creates/alters/grants the dedicated user via stdin, never on argv" {
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    [[ -f "$log_file" ]]
    # The password must appear in the captured STDIN payload...
    grep -q "deadbeefcafef00d" "$log_file"
    # ...but never on the "ARGS: ..." line (the docker exec argv itself).
    ! grep "^ARGS:" "$log_file" | grep -q "deadbeefcafef00d"
    grep -q "CREATE USER IF NOT EXISTS 'asterisk_rt'@'%'" "$log_file"
    grep -q "ALTER USER 'asterisk_rt'@'%'" "$log_file"
    grep -q "GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk\.\* TO 'asterisk_rt'@'%'" "$log_file"
    # docker exec targeted the container name passed as $1, not a hardcoded one.
    grep -q "^ARGS: exec -i voipbin-db" "$log_file"
}

@test "provision_asterisk_db_user strips a quoted password from .env before using it (H1 regression)" {
    create_env_file \
        'DATABASE_ASTERISK_USERNAME=asterisk_rt' \
        'DATABASE_ASTERISK_PASSWORD="deadbeefcafef00d"'
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    # The SQL must carry the UNQUOTED password (matching what docker-compose
    # would inject into the container) - not the literal value with quotes.
    grep -q "IDENTIFIED WITH mysql_native_password BY 'deadbeefcafef00d'" "$log_file"
    ! grep -q '\\"deadbeefcafef00d\\"' "$log_file"
}

@test "provision_asterisk_db_user truncates a password over 49 chars to match what Asterisk actually sends (VOIP-1332)" {
    # A 64-char value, as generate_random_key() used to produce for
    # DATABASE_ASTERISK_PASSWORD before VOIP-1332. Asterisk's
    # res_config_mysql.c stores the password in `char pass[50]` and copies
    # into it with ast_copy_string(), which silently truncates at 49 usable
    # chars - so provision_asterisk_db_user() must grant MySQL the SAME
    # truncated value, not the full 64 chars, or the DB and what Asterisk
    # actually sends over the wire permanently disagree.
    local full_pw="67d91bde6a21cd110fdcbb312fe838d632c31191784ce89cdc3c6db2e6028807"
    local truncated_pw="${full_pw:0:49}"
    [[ "${#full_pw}" -eq 64 ]]
    [[ "${#truncated_pw}" -eq 49 ]]

    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=$full_pw"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    grep -q "IDENTIFIED WITH mysql_native_password BY '${truncated_pw}'" "$log_file"
    # The full untruncated value must never appear - a stray occurrence
    # would mean the truncation only happened to LOOK right by coincidence.
    ! grep -q "$full_pw" "$log_file"
}

@test "provision_asterisk_db_user leaves a password well under 49 chars untouched" {
    # generate_random_key_short()'s 32-char output (and anything else safely
    # under 49 chars) must pass through byte-for-byte.
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00ddeadbeefcafef00d"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    grep -q "IDENTIFIED WITH mysql_native_password BY 'deadbeefcafef00ddeadbeefcafef00d'" "$log_file"
}

@test "provision_asterisk_db_user leaves a password of exactly 49 chars untouched (boundary)" {
    local pw_49
    pw_49="$(printf 'a%.0s' $(seq 1 49))"
    [[ "${#pw_49}" -eq 49 ]]
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=$pw_49"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    grep -q "IDENTIFIED WITH mysql_native_password BY '${pw_49}'" "$log_file"
}

@test "provision_asterisk_db_user truncates a password of exactly 50 chars by exactly one char (boundary)" {
    local pw_50 pw_49_expected
    pw_50="$(printf 'a%.0s' $(seq 1 50))"
    pw_49_expected="${pw_50:0:49}"
    [[ "${#pw_50}" -eq 50 ]]
    [[ "${#pw_49_expected}" -eq 49 ]]
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=$pw_50"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    grep -q "IDENTIFIED WITH mysql_native_password BY '${pw_49_expected}'" "$log_file"
    ! grep -q "IDENTIFIED WITH mysql_native_password BY '${pw_50}'" "$log_file"
}

@test "provision_asterisk_db_user is idempotent: re-running twice both succeed with identical SQL" {
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"
    [[ "$status" -eq 0 ]]
    local first_run
    first_run="$(cat "$log_file")"
    rm -f "$log_file"

    run provision_asterisk_db_user "voipbin-db"
    [[ "$status" -eq 0 ]]
    local second_run
    second_run="$(cat "$log_file")"

    # CREATE USER IF NOT EXISTS + ALTER USER means a second run is
    # side-effect-identical to the first (no error, no divergent SQL).
    [[ "$first_run" == "$second_run" ]]
    grep -q "CREATE USER IF NOT EXISTS 'asterisk_rt'@'%'" <<< "$second_run"
}

@test "provision_asterisk_db_user falls back to .env's MYSQL_ROOT_PASSWORD when DATABASE_ASTERISK_PASSWORD is unset" {
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "MYSQL_ROOT_PASSWORD=root-fallback-pw"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -eq 0 ]]
    grep -q "root-fallback-pw" "$log_file"
}

@test "provision_asterisk_db_user rejects a username with characters outside [A-Za-z0-9_]" {
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt'; DROP TABLE asterisk.ps_aors; --" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"DATABASE_ASTERISK_USERNAME"* ]]
    [[ ! -f "$log_file" ]]
}

@test "provision_asterisk_db_user rejects a password containing a single quote" {
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=abc'; DROP TABLE asterisk.ps_aors; --"
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"DATABASE_ASTERISK_PASSWORD"* ]]
    [[ ! -f "$log_file" ]]
}

@test "provision_asterisk_db_user rejects rather than silently defaulting when neither DATABASE_ASTERISK_PASSWORD nor MYSQL_ROOT_PASSWORD are set (H1/LOW-3)" {
    create_env_file "DATABASE_ASTERISK_USERNAME=asterisk_rt"
    # No DATABASE_ASTERISK_PASSWORD and no MYSQL_ROOT_PASSWORD in .env.
    # docker-compose.yml's own
    # ${DATABASE_ASTERISK_PASSWORD:-${MYSQL_ROOT_PASSWORD}} resolves to an
    # EMPTY string in this exact case - a hardcoded "root_password" literal
    # fallback here would silently diverge from what the container actually
    # authenticates with (VOIP-1328 review round 2, LOW-3), so this must be
    # rejected rather than provisioned with a guessed value.
    load_common
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"DATABASE_ASTERISK_PASSWORD"* ]]
    [[ ! -f "$log_file" ]]
}

@test "provision_asterisk_db_user returns non-zero and prints MySQL's error output when the docker command fails" {
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    load_common
    mock_command_script "docker" '
if [[ "$1" == "exec" ]]; then
    cat > /dev/null
fi
echo "ERROR 1045 (28000): Access denied" >&2
exit 1
'

    run provision_asterisk_db_user "voipbin-db"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"Failed to provision dedicated DB user"* ]]
    [[ "$output" == *"ERROR 1045"* ]]
}

# =============================================================================
# voip_internal_interfaces_ok() tests (VOIP-1331)
#
# Shared by doctor.sh/setup-host.sh/start.sh so their "already configured,
# skip" gates agree. An existence-only check would treat a host still
# running the legacy pre-VOIP-1331 macvlan interfaces as "done" and never
# route back through setup-voip-network.sh's migration logic.
# =============================================================================

stub_ip_for_voip_interfaces_check() {
    local kamailio_present="$1"    # "yes"/"no"
    local kamailio_type="$2"       # "macvlan"/"veth"/anything else = not macvlan
    local rtpengine_present="$3"
    local rtpengine_type="$4"
    local kamailio_enslaved="${5:-yes}"    # "yes"/"no" - "no" simulates an orphaned veth peer (VOIP-1331 review round 3)
    local rtpengine_enslaved="${6:-yes}"
    mock_command_script "ip" "
present() {
    case \"\$1\" in
        kamailio-int) [[ '$kamailio_present' == 'yes' ]] ;;
        rtpengine-int) [[ '$rtpengine_present' == 'yes' ]] ;;
        *) return 1 ;;
    esac
}
is_macvlan() {
    case \"\$1\" in
        kamailio-int) [[ '$kamailio_type' == 'macvlan' ]] ;;
        rtpengine-int) [[ '$rtpengine_type' == 'macvlan' ]] ;;
        *) return 1 ;;
    esac
}
is_enslaved() {
    case \"\$1\" in
        kamailio-br) [[ '$kamailio_enslaved' == 'yes' ]] ;;
        rtpengine-br) [[ '$rtpengine_enslaved' == 'yes' ]] ;;
        *) return 1 ;;
    esac
}
if [[ \"\$1\" == \"link\" && \"\$2\" == \"show\" ]]; then
    case \"\$3\" in
        kamailio-br|rtpengine-br)
            if is_enslaved \"\$3\"; then
                echo \"5: \$3@br-abc123: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue master br-abc123 state UP\"
            else
                echo \"5: \$3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\"
            fi
            exit 0
            ;;
        *)
            present \"\$3\" && exit 0 || exit 1
            ;;
    esac
fi
if [[ \"\$1\" == \"-d\" && \"\$2\" == \"link\" && \"\$3\" == \"show\" ]]; then
    if is_macvlan \"\$4\"; then
        echo \"macvlan mode bridge\"
    fi
    exit 0
fi
exit 1
"
}

@test "voip_internal_interfaces_ok returns true when both interfaces exist as non-macvlan" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "veth" "yes" "veth"

    run voip_internal_interfaces_ok

    [[ "$status" -eq 0 ]]
}

@test "voip_internal_interfaces_ok returns false when an interface is missing" {
    load_common
    stub_ip_for_voip_interfaces_check "no" "" "yes" "veth"

    run voip_internal_interfaces_ok

    [[ "$status" -ne 0 ]]
}

@test "voip_internal_interfaces_ok returns false when kamailio-int is still a legacy macvlan interface" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "macvlan" "yes" "veth"

    run voip_internal_interfaces_ok

    [[ "$status" -ne 0 ]]
}

@test "voip_internal_interfaces_ok returns false when rtpengine-int is still a legacy macvlan interface" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "veth" "yes" "macvlan"

    run voip_internal_interfaces_ok

    [[ "$status" -ne 0 ]]
}

@test "voip_internal_interfaces_ok returns false when both interfaces are still legacy macvlan" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "macvlan" "yes" "macvlan"

    run voip_internal_interfaces_ok

    [[ "$status" -ne 0 ]]
}

# --- Orphaned veth peer (VOIP-1331 review round 3, HIGH) ---
#
# Unlike macvlan (destroyed by the kernel when its parent bridge
# disappears), a veth pair survives `docker compose down`/`voipbin> clean`
# orphaned, with the bridge-side "-br" peer's `master` cleared - reachable
# via this repo's own documented clean-then-start cycle (a recreated
# compose network gets a new bridge name). Presence + non-macvlan alone is
# not sufficient; the "-br" peer must still show a `master`.

@test "voip_internal_interfaces_ok returns false when kamailio-int's bridge peer is orphaned (no master)" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "veth" "yes" "veth" "no" "yes"

    run voip_internal_interfaces_ok

    [[ "$status" -ne 0 ]]
}

@test "voip_internal_interfaces_ok returns false when rtpengine-int's bridge peer is orphaned (no master)" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "veth" "yes" "veth" "yes" "no"

    run voip_internal_interfaces_ok

    [[ "$status" -ne 0 ]]
}

@test "voip_internal_interfaces_ok returns true when both bridge peers are correctly enslaved" {
    load_common
    stub_ip_for_voip_interfaces_check "yes" "veth" "yes" "veth" "yes" "yes"

    run voip_internal_interfaces_ok

    [[ "$status" -eq 0 ]]
}
