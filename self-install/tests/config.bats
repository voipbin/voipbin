#!/usr/bin/env bats
# Configuration validation tests
# Validates docker-compose.yml, .env.template, and generated configs

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# docker-compose.yml - YAML Syntax Validation
# =============================================================================

@test "docker-compose.yml exists" {
    [[ -f "$PROJECT_ROOT/docker-compose.yml" ]]
}

@test "docker-compose.yml is valid YAML" {
    # Use Python's yaml parser (more portable than requiring Docker)
    # Falls back to docker compose if python3/pyyaml not available
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
        run python3 -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/docker-compose.yml'))"
    elif command -v docker &>/dev/null; then
        run docker compose -f "$PROJECT_ROOT/docker-compose.yml" config --quiet 2>&1
    else
        skip "Neither python3+pyyaml nor docker available for YAML validation"
    fi

    if [[ "$status" -ne 0 ]]; then
        echo "YAML validation failed:" >&2
        echo "$output" >&2
    fi
    [[ "$status" -eq 0 ]]
}

# =============================================================================
# docker-compose.yml - Required Services
# =============================================================================

@test "docker-compose.yml defines db service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  db:"
}

@test "docker-compose.yml defines redis service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  redis:"
}

@test "docker-compose.yml defines rabbitmq service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  rabbitmq:"
}

@test "docker-compose.yml defines coredns service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  coredns:"
}

@test "docker-compose.yml defines kamailio service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  kamailio:"
}

@test "docker-compose.yml defines api-manager service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  api-manager:"
}

@test "docker-compose.yml defines square-admin service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  square-admin:"
}

@test "docker-compose.yml defines square-meet service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  square-meet:"
}

@test "docker-compose.yml defines square-talk service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  square-talk:"
}

# =============================================================================
# docker-compose.yml - Web Service Port Mappings
# =============================================================================

@test "docker-compose.yml maps admin to port 3003" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "0.0.0.0:3003:80"
}

@test "docker-compose.yml maps meet to port 3004" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "0.0.0.0:3004:80"
}

@test "docker-compose.yml maps talk to port 3005" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "0.0.0.0:3005:80"
}

@test "docker-compose.yml maps api-manager to port 8443" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "0.0.0.0:8443:443"
}

# =============================================================================
# docker-compose.yml - Network Configuration
# =============================================================================

@test "docker-compose.yml defines default network" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "networks:"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "  default:"
}

@test "docker-compose.yml default network uses 10.100.0.0/16 subnet" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "10.100.0.0/16"
}

# =============================================================================
# docker-compose.yml - Container Fixed IPs
# =============================================================================
# NOTE: admin/meet/talk/api-manager static ipv4_address pins were removed in
# Phase 1 of the horizontal-scale-architecture design (docs/plans/2026-07-05):
# these services are only reached via published host ports + DNS, never
# referenced by another service's hardcoded IP, so service-name DNS is safe
# and sufficient. Asterisk-call/registrar/conference static IPs remain (they
# ARE referenced by Kamailio's hardcoded routing) until Phase 3 replaces them
# with dispatcher-list generation.

@test "docker-compose.yml does not assign a fixed IP to admin (removed Phase 1)" {
    # Capture the FULL square-admin service block (from its header to the next
    # top-level service key), not just a fixed line count — a fixed -A2/-A3
    # window can silently stop short of the networks: block if unrelated
    # lines are inserted above it, turning this into a false-pass guard.
    run sed -n '/^  square-admin:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml"
    [[ "$output" != *"ipv4_address: 10.100.0.101"* ]]
}

@test "docker-compose.yml does not assign a fixed IP to meet (removed Phase 1)" {
    run sed -n '/^  square-meet:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml"
    [[ "$output" != *"ipv4_address: 10.100.0.102"* ]]
}

@test "docker-compose.yml does not assign a fixed IP to talk (removed Phase 1)" {
    run sed -n '/^  square-talk:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml"
    [[ "$output" != *"ipv4_address: 10.100.0.103"* ]]
}

@test "docker-compose.yml does not assign a fixed IP to api-manager (removed Phase 1)" {
    run sed -n '/^  api-manager:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml"
    [[ "$output" != *"ipv4_address: 10.100.0.100"* ]]
}

# =============================================================================
# docker-compose.yml - State-layer address externalization (Phase 1)
# =============================================================================
# All bin-* app services must use ${DB_HOST:-db}/${REDIS_HOST:-redis}/
# ${RABBITMQ_HOST:-rabbitmq} instead of hardcoded compose service names, so
# an operator splitting the state layer onto a separate host only needs to
# change .env (no compose edits). Verifies zero hardcoded stragglers remain.

@test "docker-compose.yml uses \${MYSQL_ROOT_PASSWORD} in DATABASE_DSN (not hardcoded root_password)" {
    run grep -c 'DATABASE_DSN=root:${MYSQL_ROOT_PASSWORD}@tcp' "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -ge 1 ]
    run grep -c "DATABASE_DSN=root:root_password@" "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml uses \${MYSQL_ROOT_PASSWORD} in DATABASE_DSN_BIN/DATABASE_DSN_ASTERISK (registrar-manager, not hardcoded root_password)" {
    run grep -cE 'DATABASE_DSN_(BIN|ASTERISK)=root:\$\{MYSQL_ROOT_PASSWORD\}@tcp' "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -ge 1 ]
    run grep -cE "DATABASE_DSN_(BIN|ASTERISK)=root:root_password@" "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml has no hardcoded RABBITMQ_ADDRESS (all externalized via RABBITMQ_HOST)" {
    run grep -c "RABBITMQ_ADDRESS=amqp://guest:guest@rabbitmq:5672" "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml has no hardcoded REDIS_ADDRESS (all externalized via REDIS_HOST)" {
    run grep -c "REDIS_ADDRESS=redis:6379" "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml has no hardcoded DATABASE_ASTERISK_HOST (externalized via DB_HOST)" {
    run grep -cE "DATABASE_ASTERISK_HOST=db$" "$PROJECT_ROOT/docker-compose.yml"
    [ "$output" -eq 0 ]
}

@test "docker compose config renders identical DB/Redis/RabbitMQ addresses at defaults (no behavior change)" {
    run bash -c "cd '$PROJECT_ROOT' && docker compose config 2>/dev/null | grep -m1 'DATABASE_DSN:'"
    [[ "$output" == *"tcp(db:3306)/bin_manager"* ]]
}

@test ".env.template documents DB_HOST/REDIS_HOST/RABBITMQ_HOST externalization vars" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "DB_HOST=db"
    assert_file_contains "$PROJECT_ROOT/.env.template" "REDIS_HOST=redis"
    assert_file_contains "$PROJECT_ROOT/.env.template" "RABBITMQ_HOST=rabbitmq"
}

# =============================================================================
# docker-compose.yml - Port Conflict Detection
# =============================================================================

@test "docker-compose.yml has no duplicate host port mappings" {
    # Extract all host ports from port mappings (format: "host:container" or "0.0.0.0:host:container")
    local ports=$(grep -oE '^\s*-\s*"[0-9.]*:?[0-9]+:[0-9]+"' "$PROJECT_ROOT/docker-compose.yml" | \
                  grep -oE '[0-9]+:[0-9]+"' | \
                  cut -d: -f1 | \
                  sort)

    # Validate we found ports
    if [[ -z "$ports" ]]; then
        echo "No port mappings found in docker-compose.yml" >&2
        return 1
    fi

    local unique_ports=$(echo "$ports" | sort -u)
    local port_count=$(echo "$ports" | wc -l)
    local unique_count=$(echo "$unique_ports" | wc -l)

    if [[ "$port_count" -ne "$unique_count" ]]; then
        echo "Duplicate ports found:" >&2
        echo "$ports" | uniq -d >&2
        return 1
    fi
}

# =============================================================================
# .env.template Validation
# =============================================================================

@test ".env.template exists" {
    [[ -f "$PROJECT_ROOT/.env.template" ]]
}

@test ".env.template has no duplicate keys" {
    # Extract all KEY= patterns (ignoring comments and empty lines)
    local keys=$(grep -E '^[A-Z_]+=' "$PROJECT_ROOT/.env.template" | cut -d= -f1 | sort)
    local unique_keys=$(echo "$keys" | sort -u)
    local key_count=$(echo "$keys" | wc -l)
    local unique_count=$(echo "$unique_keys" | wc -l)

    if [[ "$key_count" -ne "$unique_count" ]]; then
        echo "Duplicate keys found:" >&2
        echo "$keys" | uniq -d >&2
        return 1
    fi
}

@test ".env.template contains HOST_EXTERNAL_IP" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "HOST_EXTERNAL_IP="
}

@test ".env.template contains KAMAILIO_EXTERNAL_IP" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "KAMAILIO_EXTERNAL_IP="
}

@test ".env.template contains RTPENGINE_EXTERNAL_IP" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "RTPENGINE_EXTERNAL_IP="
}

@test ".env.template contains API_SSL_CERT_BASE64" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "API_SSL_CERT_BASE64="
}

@test ".env.template contains DOMAIN_NAME_EXTENSION" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "DOMAIN_NAME_EXTENSION="
}

@test ".env.template contains BASE_DOMAIN" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "BASE_DOMAIN="
}

# =============================================================================
# CoreDNS Corefile Validation (when generated)
# =============================================================================

@test "generate_coredns_config creates valid Corefile structure" {
    load_common
    local config_dir="$TEST_TEMP_DIR/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir" "192.168.1.200"

    # Check required blocks exist
    assert_file_contains "$config_dir/Corefile" "api.voipbin.test {"
    assert_file_contains "$config_dir/Corefile" "admin.voipbin.test {"
    assert_file_contains "$config_dir/Corefile" "meet.voipbin.test {"
    assert_file_contains "$config_dir/Corefile" "talk.voipbin.test {"
    assert_file_contains "$config_dir/Corefile" "voipbin.test {"
    assert_file_contains "$config_dir/Corefile" ". {"
}

@test "generate_coredns_config includes template directive for dynamic DNS" {
    load_common
    local config_dir="$TEST_TEMP_DIR/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir"

    assert_file_contains "$config_dir/Corefile" "template IN A"
}

@test "generate_coredns_config includes AAAA handling" {
    load_common
    local config_dir="$TEST_TEMP_DIR/coredns"

    generate_coredns_config "192.168.1.100" "$config_dir"

    # Should handle AAAA queries (return NOERROR to prevent IPv6 failures)
    assert_file_contains "$config_dir/Corefile" "template IN AAAA"
    assert_file_contains "$config_dir/Corefile" "rcode NOERROR"
}

# =============================================================================
# Script Consistency Checks
# =============================================================================

@test "common.sh no longer defines removed static IP variables for admin/meet/talk/api" {
    # Phase 1 removed ADMIN_IP/MEET_IP/TALK_IP/API_MANAGER_IP entirely (dead vars
    # referencing addresses that no longer exist in docker-compose.yml) — assert
    # they're gone rather than leaving a vacuous/false-passing consistency check.
    run grep -E '^(API_MANAGER_IP|ADMIN_IP|MEET_IP|TALK_IP)=' "$SCRIPTS_DIR/common.sh"
    [ "$status" -ne 0 ]
}

@test "setup-voip-network.sh defines same internal IPs as expected" {
    # The internal network IPs should be consistent
    assert_file_contains "$SCRIPTS_DIR/setup-voip-network.sh" '["kamailio-int"]="10.100.0.200"'
    assert_file_contains "$SCRIPTS_DIR/setup-voip-network.sh" '["rtpengine-int"]="10.100.0.201"'
}

@test "docker-compose.yml kamailio uses expected internal IP" {
    # Kamailio should reference the internal IP for the internal network
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml" "KAMAILIO_INTERNAL_ADDR=10.100.0.200"
}
