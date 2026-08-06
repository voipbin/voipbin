#!/usr/bin/env bats
# Tests for scripts/install-certs.sh (dual-mode DNS, VOIP-1275 Phase 4)
# Validation (SAN/key/expiry), layout install, .env base64 rewrite with
# identity preservation, and the result-line grammar — all against real
# openssl fixture certificates in $TEST_TEMP_DIR.
#
# Isolation rule: docker is stubbed via MOCK_BIN_DIR and PROJECT_DIR is
# $TEST_TEMP_DIR — no service is ever recreated, no real tree is touched.

load 'test_helper'

setup() {
    setup_test_env
    FIXTURES="$TEST_TEMP_DIR/fixtures"
    mkdir -p "$FIXTURES"
}

teardown() {
    teardown_test_env
}

# make_cert <basename> <san-list> [extra req args...]
make_cert() {
    local base="$1"
    local san="$2"
    shift 2
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "$FIXTURES/$base.key" \
        -out "$FIXTURES/$base.pem" \
        -days 90 \
        -subj "/CN=example.com" \
        -addext "subjectAltName=$san" \
        "$@" 2>/dev/null
}

# Wildcard fixture covering every required name incl. *.registrar
make_wildcard_cert() {
    make_cert wildcard "DNS:example.com,DNS:*.example.com,DNS:*.registrar.example.com"
}

make_env_fixture() {
    create_env_file \
        "DOMAIN_MODE=external" \
        "COMPOSE_PROFILES=" \
        "BASE_DOMAIN=example.com" \
        "API_SSL_CERT_BASE64=old-cert" \
        "API_SSL_PRIVKEY_BASE64=old-key" \
        "HOOK_SSL_CERT_BASE64=old-cert" \
        "HOOK_SSL_PRIVKEY_BASE64=old-key"
}

# =============================================================================
# Validation (design §2.6 step 1)
# =============================================================================

@test "check-only passes a wildcard cert covering all names (no registrar warning)" {
    make_wildcard_cert

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com \
        "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'SAN covers all required names'* ]]
    [[ "$output" != *'does not cover *.registrar.example.com'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_CERTS:\ status=ok\ domain=example\.com ]]
    [[ "$last_line" == *'action=check-only'* ]]
}

@test "non-wildcard cert missing one required name hard-fails listing it" {
    # sip-service.example.com deliberately missing
    make_cert partial "DNS:api.example.com,DNS:sip.example.com,DNS:conference.example.com,DNS:trunk.example.com,DNS:registrar.example.com"

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com \
        "$FIXTURES/partial.pem" "$FIXTURES/partial.key"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'does not cover'* ]]
    [[ "$output" == *'sip-service.example.com'* ]]
    [[ "$output" == *'VOIPBIN_CERTS: status=error'* ]]
}

@test "expired cert warns but does not fail validation" {
    make_cert expired "DNS:example.com,DNS:*.example.com,DNS:*.registrar.example.com" \
        -not_before 20250101000000Z -not_after 20250201000000Z

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com \
        "$FIXTURES/expired.pem" "$FIXTURES/expired.key"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'expires within 30 days (or is already expired)'* ]]
    [[ "$output" == *'VOIPBIN_CERTS: status=ok'* ]]
}

@test "cert covering the six names but not *.registrar warns about direct-realm devices" {
    make_cert noreg "DNS:api.example.com,DNS:sip.example.com,DNS:sip-service.example.com,DNS:conference.example.com,DNS:trunk.example.com,DNS:registrar.example.com"

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com \
        "$FIXTURES/noreg.pem" "$FIXTURES/noreg.key"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'does not cover *.registrar.example.com'* ]]
    [[ "$output" == *'outbound proxy sip.example.com are unaffected'* ]]
}

@test "mismatched private key hard-fails" {
    make_wildcard_cert
    make_cert other "DNS:example.com,DNS:*.example.com"

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com \
        "$FIXTURES/wildcard.pem" "$FIXTURES/other.key"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'private key does not match certificate'* ]]
    [[ "$output" == *'VOIPBIN_CERTS: status=error'* ]]
}

@test "missing --domain with no BASE_DOMAIN in .env fails with guidance" {
    make_wildcard_cert
    rm -f "$PROJECT_DIR/.env"

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only \
        "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'pass --domain'* ]]
}

# =============================================================================
# Install: layout + .env rewrite (design §2.6 steps 2-3)
# =============================================================================

@test "full install writes the cert layout and base64 values into the fixture .env" {
    make_wildcard_cert
    make_env_fixture
    mock_command "docker" ""

    run bash "$SCRIPTS_DIR/install-certs.sh" "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"

    [[ "$status" -eq 0 ]]
    # Layout: certs/api + the five per-domain directories; every installed
    # private key is owner-only (600)
    [[ -f "$CERTS_DIR/api/cert.pem" ]]
    [[ -f "$CERTS_DIR/api/privkey.pem" ]]
    [[ "$(/usr/bin/stat -c '%a' "$CERTS_DIR/api/privkey.pem")" == "600" ]]
    for name in registrar conference sip sip-service trunk; do
        [[ -f "$CERTS_DIR/$name.example.com/fullchain.pem" ]]
        [[ -f "$CERTS_DIR/$name.example.com/privkey.pem" ]]
        [[ "$(/usr/bin/stat -c '%a' "$CERTS_DIR/$name.example.com/privkey.pem")" == "600" ]]
    done
    # .env base64 values are the actual fixture cert/key
    local cert_b64 key_b64
    cert_b64=$(base64 -w0 < "$FIXTURES/wildcard.pem")
    key_b64=$(base64 -w0 < "$FIXTURES/wildcard.key")
    assert_file_contains "$PROJECT_DIR/.env" "API_SSL_CERT_BASE64=$cert_b64"
    assert_file_contains "$PROJECT_DIR/.env" "API_SSL_PRIVKEY_BASE64=$key_b64"
    assert_file_contains "$PROJECT_DIR/.env" "HOOK_SSL_CERT_BASE64=$cert_b64"
    assert_file_contains "$PROJECT_DIR/.env" "HOOK_SSL_PRIVKEY_BASE64=$key_b64"
    assert_file_not_contains "$PROJECT_DIR/.env" "old-cert"
    # Atomic rewrite left no temp file behind
    [[ -z "$(ls "$PROJECT_DIR"/.env.install-certs.* 2>/dev/null)" ]]
    # Not running -> services skipped with a note
    [[ "$output" == *'api-manager not running'* ]]
    [[ "$output" == *'kamailio not running'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_CERTS:\ status=ok\ domain=example\.com\ expires= ]]
}

@test "--check-only installs nothing and leaves .env untouched" {
    make_wildcard_cert
    make_env_fixture
    mock_command "docker" ""

    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only \
        "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"

    [[ "$status" -eq 0 ]]
    [[ ! -f "$CERTS_DIR/api/cert.pem" ]]
    [[ ! -d "$CERTS_DIR/sip.example.com" ]]
    assert_file_contains "$PROJECT_DIR/.env" "API_SSL_CERT_BASE64=old-cert"
}

# =============================================================================
# Service recreation scoping (design §2.6 step 4)
# =============================================================================

@test "recreate_services scopes the running check to this project's compose" {
    load_install_certs_functions
    mock_command_script "docker" '
echo "DOCKER:$*" >> "'"$TEST_TEMP_DIR"'/docker.log"
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
    printf "api-manager\nkamailio\n"
fi
exit 0'

    run recreate_services

    [[ "$status" -eq 0 ]]
    local calls
    calls=$(cat "$TEST_TEMP_DIR/docker.log")
    # The check is compose-scoped (run from $PROJECT_DIR), never global docker ps
    [[ "$calls" == *'DOCKER:compose ps --services --status running'* ]]
    [[ "$calls" != *$'\n''DOCKER:ps'* ]]
    [[ "$calls" != DOCKER:ps* ]]
    # Both consumers were recreated/restarted via compose
    [[ "$calls" == *'DOCKER:compose rm -sf api-manager hook-manager'* ]]
    [[ "$calls" == *'DOCKER:compose up -d api-manager hook-manager'* ]]
    [[ "$calls" == *'DOCKER:compose restart kamailio'* ]]
}

@test "recreate_services ignores same-named containers from other compose projects" {
    load_install_certs_functions
    # Regression: a foreign stack's voipbin-api-mgr/voipbin-kamailio containers
    # are visible to global `docker ps` but NOT to this project's compose ps.
    mock_command_script "docker" '
if [[ "$1" == "compose" && "$2" == "ps" ]]; then exit 0; fi
if [[ "$1" == "ps" ]]; then printf "voipbin-api-mgr\nvoipbin-kamailio\n"; exit 0; fi
echo "UNEXPECTED-DOCKER:$*" >> "'"$TEST_TEMP_DIR"'/docker.log"
exit 0'

    run recreate_services

    [[ "$status" -eq 0 ]]
    # Skip-with-note path, no recreate/restart attempted
    [[ "$output" == *'api-manager not running'* ]]
    [[ "$output" == *'kamailio not running'* ]]
    [[ ! -f "$TEST_TEMP_DIR/docker.log" ]]
}

# =============================================================================
# Ownership/mode preservation across the .env inode replacement (design §2.6)
# =============================================================================

@test "update_env_certs restores mode and re-applies owner from stat (stubbed, root-less)" {
    load_install_certs_functions
    make_wildcard_cert
    make_env_fixture
    CERT_FILE="$FIXTURES/wildcard.pem"
    KEY_FILE="$FIXTURES/wildcard.key"
    # stat/chown stubs: root isn't available in tests, so the capture/restore
    # logic is asserted through stub invocations instead of real ownership.
    mock_command_script "stat" '
if [[ "$1" == "-c" && "$2" == "%a" ]]; then echo "600"; exit 0; fi
if [[ "$1" == "-c" && "$2" == "%u:%g" ]]; then echo "1234:5678"; exit 0; fi
exit 1'
    mock_command_script "chown" 'echo "CHOWN:$*"; exit 0'

    run update_env_certs

    [[ "$status" -eq 0 ]]
    # Owner captured before the rewrite is re-applied to the replacement file
    [[ "$output" == *'CHOWN:1234:5678'* ]]
    # Mode captured before the rewrite survives the inode replacement
    [[ "$(/usr/bin/stat -c '%a' "$ENV_FILE")" == "600" ]]
    # And the rewrite itself happened
    local cert_b64
    cert_b64=$(base64 -w0 < "$CERT_FILE")
    assert_file_contains "$ENV_FILE" "API_SSL_CERT_BASE64=$cert_b64"
}
