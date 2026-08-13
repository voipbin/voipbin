#!/usr/bin/env bats
# Configuration validation tests
# Validates docker-compose.yml.dist, .env.template, and generated configs.
#
# These tests run against a bare repo checkout (no init.sh run), so they
# target docker-compose.yml.dist — the committed file — not the live,
# untracked docker-compose.yml that only exists after init.sh runs. See
# init.bats for tests covering the .dist -> live copy behavior itself.

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# docker-compose.yml.dist - YAML Syntax Validation
# =============================================================================

@test "docker-compose.yml.dist exists" {
    [[ -f "$PROJECT_ROOT/docker-compose.yml.dist" ]]
}

@test "docker-compose.yml.dist is valid YAML" {
    # Use Python's yaml parser (more portable than requiring Docker)
    # Falls back to docker compose if python3/pyyaml not available
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
        run python3 -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/docker-compose.yml.dist'))"
    elif command -v docker &>/dev/null; then
        run docker compose -f "$PROJECT_ROOT/docker-compose.yml.dist" config --quiet 2>&1
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
# docker-compose.yml.dist - Required Services
# =============================================================================

@test "docker-compose.yml.dist defines db service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  db:"
}

@test "docker-compose.yml.dist defines redis service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  redis:"
}

@test "docker-compose.yml.dist defines rabbitmq service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  rabbitmq:"
}

@test "docker-compose.yml.dist defines coredns service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  coredns:"
}

@test "docker-compose.yml.dist defines kamailio service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  kamailio:"
}

@test "docker-compose.yml.dist defines api-manager service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  api-manager:"
}

@test "docker-compose.yml.dist defines square-admin service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  square-admin:"
}

@test "docker-compose.yml.dist defines square-meet service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  square-meet:"
}

@test "docker-compose.yml.dist defines square-talk service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  square-talk:"
}

# =============================================================================
# docker-compose.yml.dist - web-proxy (Caddy reverse proxy, VOIP-1325)
# =============================================================================

@test "docker-compose.yml.dist defines web-proxy service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  web-proxy:"
}

@test "docker-compose.yml.dist gates web-proxy behind the web-proxy compose profile" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'profiles: ["web-proxy"]'
}

@test "docker-compose.yml.dist publishes web-proxy on HOST_EXTERNAL_IP's 80 and 443, not a 0.0.0.0 wildcard" {
    # Kamailio binds host ports 80/443 on its own dedicated
    # KAMAILIO_EXTERNAL_IP (network_mode: host, e.g. for WSS) — a wildcard
    # 0.0.0.0 publish for Caddy collides with that at the kernel bind layer
    # even though the IPs differ (confirmed live on bm-nyc-01: "address
    # already in use" starting web-proxy). HOST_EXTERNAL_IP is also the
    # address every admin/meet/talk/api DNS record targets.
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"${HOST_EXTERNAL_IP:-0.0.0.0}:80:80"'
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"${HOST_EXTERNAL_IP:-0.0.0.0}:443:443"'
}

@test "docker-compose.yml.dist mounts the generated Caddyfile and certs into web-proxy" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./certs:/certs:ro"
}

@test "docker-compose.yml.dist has a healthcheck for web-proxy" {
    local block
    block=$(sed -n '/^  web-proxy:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"healthcheck:"* ]]
}

@test "docker-compose.yml.dist web-proxy depends on api-manager and the three square-* frontends" {
    local block
    block=$(sed -n '/^  web-proxy:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"- api-manager"* ]]
    [[ "$block" == *"- square-admin"* ]]
    [[ "$block" == *"- square-meet"* ]]
    [[ "$block" == *"- square-talk"* ]]
}

# =============================================================================
# docker-compose.yml.dist - Web Service Port Mappings
# =============================================================================

@test "docker-compose.yml.dist maps admin to port 3003" {
    # SQUARE_BIND_ADDR (VOIP-1325): host address is templated (defaults to
    # 0.0.0.0) so init.sh can bind it to 127.0.0.1 instead with
    # --web-reverse-proxy, since Caddy becomes the sole external path then.
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'SQUARE_BIND_ADDR:-0.0.0.0}:3003:80'
}

@test "docker-compose.yml.dist maps meet to port 3004" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'SQUARE_BIND_ADDR:-0.0.0.0}:3004:80'
}

@test "docker-compose.yml.dist maps talk to port 3005" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'SQUARE_BIND_ADDR:-0.0.0.0}:3005:80'
}

@test "docker-compose.yml.dist maps api-manager to port 8443" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "0.0.0.0:8443:443"
}

# =============================================================================
# docker-compose.yml.dist - Network Configuration
# =============================================================================

@test "docker-compose.yml.dist defines default network" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "networks:"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  default:"
}

@test "docker-compose.yml.dist default network uses 10.100.0.0/16 subnet" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "10.100.0.0/16"
}

# =============================================================================
# docker-compose.yml.dist - Container Fixed IPs
# =============================================================================
# NOTE: admin/meet/talk/api-manager static ipv4_address pins were removed in
# Phase 1 of the horizontal-scale-architecture design (docs/plans/2026-07-05):
# these services are only reached via published host ports + DNS, never
# referenced by another service's hardcoded IP, so service-name DNS is safe
# and sufficient. Asterisk-call/registrar/conference static IPs remain (they
# ARE referenced by Kamailio's hardcoded routing) until Phase 3 replaces them
# with dispatcher-list generation.

@test "docker-compose.yml.dist does not assign a fixed IP to admin (removed Phase 1)" {
    # Capture the FULL square-admin service block (from its header to the next
    # top-level service key), not just a fixed line count — a fixed -A2/-A3
    # window can silently stop short of the networks: block if unrelated
    # lines are inserted above it, turning this into a false-pass guard.
    run sed -n '/^  square-admin:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml.dist"
    [[ "$output" != *"ipv4_address: 10.100.0.101"* ]]
}

@test "docker-compose.yml.dist does not assign a fixed IP to meet (removed Phase 1)" {
    run sed -n '/^  square-meet:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml.dist"
    [[ "$output" != *"ipv4_address: 10.100.0.102"* ]]
}

@test "docker-compose.yml.dist does not assign a fixed IP to talk (removed Phase 1)" {
    run sed -n '/^  square-talk:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml.dist"
    [[ "$output" != *"ipv4_address: 10.100.0.103"* ]]
}

@test "docker-compose.yml.dist does not assign a fixed IP to api-manager (removed Phase 1)" {
    run sed -n '/^  api-manager:/,/^  [a-zA-Z_-]\+:/p' "$PROJECT_ROOT/docker-compose.yml.dist"
    [[ "$output" != *"ipv4_address: 10.100.0.100"* ]]
}

# =============================================================================
# docker-compose.yml.dist - State-layer address externalization (Phase 1)
# =============================================================================
# All bin-* app services must use ${DB_HOST:-db}/${REDIS_HOST:-redis}/
# ${RABBITMQ_HOST:-rabbitmq} instead of hardcoded compose service names, so
# an operator splitting the state layer onto a separate host only needs to
# change .env (no compose edits). Verifies zero hardcoded stragglers remain.

@test "docker-compose.yml.dist uses \${MYSQL_ROOT_PASSWORD} in DATABASE_DSN (not hardcoded root_password)" {
    run grep -c 'DATABASE_DSN=root:${MYSQL_ROOT_PASSWORD}@tcp' "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -ge 1 ]
    run grep -c "DATABASE_DSN=root:root_password@" "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml.dist uses \${MYSQL_ROOT_PASSWORD} in DATABASE_DSN_BIN/DATABASE_DSN_ASTERISK (registrar-manager, not hardcoded root_password)" {
    run grep -cE 'DATABASE_DSN_(BIN|ASTERISK)=root:\$\{MYSQL_ROOT_PASSWORD\}@tcp' "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -ge 1 ]
    run grep -cE "DATABASE_DSN_(BIN|ASTERISK)=root:root_password@" "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml.dist has no hardcoded RABBITMQ_ADDRESS (all externalized via RABBITMQ_HOST)" {
    run grep -c "RABBITMQ_ADDRESS=amqp://guest:guest@rabbitmq:5672" "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml.dist has no hardcoded REDIS_ADDRESS (all externalized via REDIS_HOST)" {
    run grep -c "REDIS_ADDRESS=redis:6379" "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -eq 0 ]
}

@test "docker-compose.yml.dist has no hardcoded DATABASE_ASTERISK_HOST (externalized via DB_HOST)" {
    run grep -cE "DATABASE_ASTERISK_HOST=db$" "$PROJECT_ROOT/docker-compose.yml.dist"
    [ "$output" -eq 0 ]
}

@test "docker compose config renders identical DB/Redis/RabbitMQ addresses at defaults (no behavior change)" {
    # Explicit -f: this runs against a bare checkout, where only
    # docker-compose.yml.dist exists (docker-compose.yml is the untracked
    # live copy init.sh creates, so implicit cwd discovery would fail here).
    run bash -c "cd '$PROJECT_ROOT' && docker compose -f docker-compose.yml.dist config 2>/dev/null | grep -m1 'DATABASE_DSN:'"
    [[ "$output" == *"tcp(db:3306)/bin_manager"* ]]
}

@test ".env.template documents DB_HOST/REDIS_HOST/RABBITMQ_HOST externalization vars" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "DB_HOST=db"
    assert_file_contains "$PROJECT_ROOT/.env.template" "REDIS_HOST=redis"
    assert_file_contains "$PROJECT_ROOT/.env.template" "RABBITMQ_HOST=rabbitmq"
}

# =============================================================================
# docker-compose.yml.dist - Port Conflict Detection
# =============================================================================

@test "docker-compose.yml.dist has no duplicate host port mappings" {
    # Extract all host ports from port mappings (format: "host:container" or "0.0.0.0:host:container")
    local ports=$(grep -oE '^\s*-\s*"[0-9.]*:?[0-9]+:[0-9]+"' "$PROJECT_ROOT/docker-compose.yml.dist" | \
                  grep -oE '[0-9]+:[0-9]+"' | \
                  cut -d: -f1 | \
                  sort)

    # Validate we found ports
    if [[ -z "$ports" ]]; then
        echo "No port mappings found in docker-compose.yml.dist" >&2
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

@test "docker-compose.yml.dist kamailio uses expected internal IP" {
    # Kamailio should reference the internal IP for the internal network
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "KAMAILIO_INTERNAL_ADDR=10.100.0.200"
}

# =============================================================================
# docker-compose.yml.dist - Monitoring Stack (Prometheus/Grafana, VOIP-1336)
# =============================================================================

@test "docker-compose.yml.dist defines prometheus service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  prometheus:"
}

@test "docker-compose.yml.dist defines grafana service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  grafana:"
}

@test "docker-compose.yml.dist defines node-exporter service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  node-exporter:"
}

@test "docker-compose.yml.dist defines cadvisor service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  cadvisor:"
}

@test "docker-compose.yml.dist defines redis-exporter service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  redis-exporter:"
}

@test "docker-compose.yml.dist defines mysqld-exporter service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  mysqld-exporter:"
}

@test "docker-compose.yml.dist binds prometheus to 127.0.0.1 only, not 0.0.0.0" {
    # Same posture as redis/rabbitmq/mysql being dropped externally at the
    # nftables layer on production hosts (README "Monitoring") — prometheus
    # must never publish on a wildcard address.
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"127.0.0.1:9090:9090"'
}

@test "docker-compose.yml.dist binds grafana to 127.0.0.1 only, not 0.0.0.0" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"127.0.0.1:3000:3000"'
}

@test "docker-compose.yml.dist does not publish any host port for the exporters" {
    # node-exporter/cadvisor/redis-exporter/mysqld-exporter are scraped by
    # prometheus over the compose network only — they must have no `ports:`
    # block of their own (unlike prometheus/grafana above).
    for svc in node-exporter cadvisor redis-exporter mysqld-exporter; do
        local block
        block=$(sed -n "/^  ${svc}:/,/^  [a-z]/p" "$PROJECT_ROOT/docker-compose.yml.dist")
        [[ "$block" != *$'\n    ports:'* ]]
    done
}

@test "docker-compose.yml.dist grafana admin password comes from GRAFANA_ADMIN_PASSWORD" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}'
}

@test "docker-compose.yml.dist mysqld-exporter DSN uses MYSQL_ROOT_PASSWORD" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'root:${MYSQL_ROOT_PASSWORD}@(db:3306)/'
}

@test "docker-compose.yml.dist redis-exporter points at the redis service with no auth" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" 'REDIS_ADDR=redis://redis:6379'
}

@test "docker-compose.yml.dist pins redis-exporter to the -alpine tag (has a shell for its CMD-SHELL healthcheck)" {
    # The bare v1.66.0 tag is a scratch image with no /bin/sh - a CMD-SHELL
    # healthcheck against it can never execute and reports permanently
    # unhealthy, failing check-install.sh/doctor.sh's generic
    # no-unhealthy-container gate for every fresh install. Regression test
    # for that bug (caught in code review before merge).
    local block
    block=$(sed -n '/^  redis-exporter:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"redis_exporter:v1.66.0-alpine@sha256:"* ]]
}

@test "docker-compose.yml.dist health-gates redis-exporter/mysqld-exporter/prometheus/grafana depends_on (service_healthy, not bare start-order)" {
    # Matches the convention used by every other cross-service dependency on
    # db/redis in this file (e.g. agent-manager's depends_on block) - a bare
    # (non-conditional) depends_on only orders container creation, not
    # readiness, causing avoidable restart churn on cold boot.
    local block
    block=$(sed -n '/^  redis-exporter:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"condition: service_healthy"* ]]
    block=$(sed -n '/^  mysqld-exporter:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"condition: service_healthy"* ]]
    block=$(sed -n '/^  prometheus:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"condition: service_healthy"* ]]
    block=$(sed -n '/^  grafana:/,/^  [a-z]/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"condition: service_healthy"* ]]
}

@test "docker-compose.yml.dist enables the rabbitmq_prometheus plugin" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "rabbitmq-plugins enable --offline rabbitmq_prometheus"
}

@test "docker-compose.yml.dist has a healthcheck for every monitoring service" {
    for svc in prometheus grafana node-exporter cadvisor redis-exporter mysqld-exporter; do
        local block
        block=$(sed -n "/^  ${svc}:/,/^  [a-z]/p" "$PROJECT_ROOT/docker-compose.yml.dist")
        [[ "$block" == *"healthcheck:"* ]]
    done
}

@test "docker-compose.yml.dist images for the monitoring stack are pinned by digest" {
    for svc in prometheus grafana node-exporter cadvisor redis-exporter mysqld-exporter; do
        local image_line
        image_line=$(sed -n "/^  ${svc}:/,/^  [a-z]/p" "$PROJECT_ROOT/docker-compose.yml.dist" | grep -m1 "^    image:")
        [[ "$image_line" == *"@sha256:"* ]]
    done
}

@test "docker-compose.yml.dist defines prometheus_data and grafana_data volumes" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  prometheus_data:"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  grafana_data:"
}

@test "docker-compose.yml.dist mounts prometheus scrape config and grafana provisioning" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./config/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./config/grafana/provisioning:/etc/grafana/provisioning:ro"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./config/grafana/dashboards:/var/lib/grafana/dashboards:ro"
}

# =============================================================================
# Monitoring Stack - Supporting Config Files
# =============================================================================

@test "config/prometheus/prometheus.yml exists" {
    [[ -f "$PROJECT_ROOT/config/prometheus/prometheus.yml" ]]
}

@test "config/prometheus/prometheus.yml is valid YAML" {
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
        run python3 -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/config/prometheus/prometheus.yml'))"
        [[ "$status" -eq 0 ]]
    else
        skip "python3+pyyaml not available for YAML validation"
    fi
}

@test "config/prometheus/prometheus.yml scrapes node, cadvisor, rabbitmq, redis and mysql" {
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "node-exporter:9100"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "cadvisor:8080"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "rabbitmq:15692"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "redis-exporter:9121"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "mysqld-exporter:9104"
}

@test "config/prometheus/prometheus.yml scrapes every bin-*-manager service's native :2112/metrics" {
    # Every manager service in docker-compose.yml.dist already exposes
    # Prometheus metrics on :2112/metrics natively (CLAUDE.md "Manager
    # Services") - assert the scrape job exists and covers the full set,
    # not just a couple of spot-checked names, so a manager silently
    # dropped from the target list is caught.
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "job_name: voipbin-managers"
    local svc
    for svc in agent-manager api-manager call-manager customer-manager \
               flow-manager registrar-manager billing-manager \
               webchat-manager webhook-manager schedule-manager; do
        assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "${svc}:2112"
    done
}

@test "config/prometheus/prometheus.yml's voipbin-managers target list matches docker-compose.yml.dist's :2112/metrics services exactly" {
    if ! command -v python3 &>/dev/null || ! python3 -c "import yaml" 2>/dev/null; then
        skip "python3+pyyaml not available"
    fi
    run python3 - "$PROJECT_ROOT/docker-compose.yml.dist" "$PROJECT_ROOT/config/prometheus/prometheus.yml" <<'PYEOF'
import re
import sys
import yaml

compose_path, prom_path = sys.argv[1], sys.argv[2]

lines = open(compose_path).read().split("\n")
svc_line = next(i for i, l in enumerate(lines) if l.rstrip() == "services:")
vol_line = next(i for i, l in enumerate(lines) if l.rstrip() == "volumes:" and not l.startswith(" "))
services = []
for i in range(svc_line + 1, vol_line):
    m = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", lines[i])
    if m:
        services.append((m.group(1), i))

def block(idx):
    end = vol_line
    for name, s in services:
        if s > idx:
            end = s
            break
    return "\n".join(lines[idx:end])

expected = {name for name, idx in services if "2112/metrics" in block(idx)}

doc = yaml.safe_load(open(prom_path))
job = next(j for j in doc["scrape_configs"] if j["job_name"] == "voipbin-managers")
targets = {t.rsplit(":", 1)[0] for sc in job["static_configs"] for t in sc["targets"]}

missing = expected - targets
extra = targets - expected
assert not missing, f"missing from prometheus.yml: {sorted(missing)}"
assert not extra, f"stale/unknown targets in prometheus.yml: {sorted(extra)}"
print("OK", len(expected), "services matched")
PYEOF
    [[ "$status" -eq 0 ]]
}

@test "config/grafana/provisioning/datasources/prometheus.yml exists and points at prometheus:9090" {
    local f="$PROJECT_ROOT/config/grafana/provisioning/datasources/prometheus.yml"
    [[ -f "$f" ]]
    assert_file_contains "$f" "http://prometheus:9090"
}

@test "config/grafana/provisioning/dashboards/dashboards.yml exists" {
    [[ -f "$PROJECT_ROOT/config/grafana/provisioning/dashboards/dashboards.yml" ]]
}

@test "config/grafana/dashboards ships docker-system, rabbitmq, redis and manager-services dashboards" {
    [[ -f "$PROJECT_ROOT/config/grafana/dashboards/docker-system-overview.json" ]]
    [[ -f "$PROJECT_ROOT/config/grafana/dashboards/rabbitmq-overview.json" ]]
    [[ -f "$PROJECT_ROOT/config/grafana/dashboards/redis-overview.json" ]]
    [[ -f "$PROJECT_ROOT/config/grafana/dashboards/voipbin-manager-services.json" ]]
}

@test "config/grafana/dashboards JSON files are valid and reference the Prometheus datasource" {
    if ! command -v python3 &>/dev/null || ! python3 -c "import json" 2>/dev/null; then
        skip "python3 not available for JSON validation"
    fi
    local f
    for f in "$PROJECT_ROOT"/config/grafana/dashboards/*.json; do
        run python3 -c "import json; json.load(open('$f'))"
        [[ "$status" -eq 0 ]]
        assert_file_contains "$f" '"type": "prometheus"'
    done
}

# =============================================================================
# .env.template / init.sh - GRAFANA_ADMIN_PASSWORD (VOIP-1336)
# =============================================================================

@test ".env.template contains GRAFANA_ADMIN_PASSWORD" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "GRAFANA_ADMIN_PASSWORD="
}

@test "init.sh generates GRAFANA_ADMIN_PASSWORD with generate_random_key" {
    assert_file_contains "$SCRIPTS_DIR/init.sh" "GRAFANA_ADMIN_PASSWORD=\$(generate_random_key)"
}

@test "init.sh writes GRAFANA_ADMIN_PASSWORD into the generated .env" {
    assert_file_contains "$SCRIPTS_DIR/init.sh" "GRAFANA_ADMIN_PASSWORD=\$GRAFANA_ADMIN_PASSWORD"
}

# =============================================================================
# docker-compose.yml.dist - db/redis/rabbitmq loopback-only binding (VOIP-1336)
# =============================================================================

@test "docker-compose.yml.dist binds db to 127.0.0.1 by default via DB_BIND_ADDRESS" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"${DB_BIND_ADDRESS:-127.0.0.1}:3306:3306"'
}

@test "docker-compose.yml.dist binds redis to 127.0.0.1 by default via REDIS_BIND_ADDRESS" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"${REDIS_BIND_ADDRESS:-127.0.0.1}:6379:6379"'
}

@test "docker-compose.yml.dist binds both rabbitmq ports to 127.0.0.1 by default via RABBITMQ_BIND_ADDRESS" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"${RABBITMQ_BIND_ADDRESS:-127.0.0.1}:5672:5672"'
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"${RABBITMQ_BIND_ADDRESS:-127.0.0.1}:15672:15672"'
}

@test "docker compose config renders db/redis/rabbitmq bound to 127.0.0.1 with no env overrides" {
    if ! command -v docker &>/dev/null; then
        skip "docker not available"
    fi
    run docker compose -f "$PROJECT_ROOT/docker-compose.yml.dist" config
    [[ "$status" -eq 0 ]]
    local db_block redis_block rabbitmq_block
    db_block=$(echo "$output" | sed -n '/^  db:/,/^  [a-z]/p')
    redis_block=$(echo "$output" | sed -n '/^  redis:/,/^  [a-z]/p')
    rabbitmq_block=$(echo "$output" | sed -n '/^  rabbitmq:/,/^  [a-z]/p')
    [[ "$db_block" == *"host_ip: 127.0.0.1"* ]]
    [[ "$redis_block" == *"host_ip: 127.0.0.1"* ]]
    [[ "$rabbitmq_block" == *"host_ip: 127.0.0.1"* ]]
}

@test "docker compose config honors DB_BIND_ADDRESS/REDIS_BIND_ADDRESS/RABBITMQ_BIND_ADDRESS overrides" {
    if ! command -v docker &>/dev/null; then
        skip "docker not available"
    fi
    # An explicit override must NOT be silently ignored - 127.0.0.1 must not
    # appear as db's bind address once DB_BIND_ADDRESS is overridden.
    run bash -c "DB_BIND_ADDRESS=0.0.0.0 REDIS_BIND_ADDRESS=0.0.0.0 RABBITMQ_BIND_ADDRESS=0.0.0.0 docker compose -f '$PROJECT_ROOT/docker-compose.yml.dist' config"
    [[ "$status" -eq 0 ]]
    local db_block
    db_block=$(echo "$output" | sed -n '/^  db:/,/^  [a-z]/p')
    [[ "$db_block" != *"127.0.0.1"* ]]
    [[ "$db_block" == *"host_ip: 0.0.0.0"* ]] || [[ "$db_block" != *"host_ip:"* ]]
}

@test ".env.template documents DB_BIND_ADDRESS/REDIS_BIND_ADDRESS/RABBITMQ_BIND_ADDRESS defaulting to 127.0.0.1" {
    assert_file_contains "$PROJECT_ROOT/.env.template" "DB_BIND_ADDRESS=127.0.0.1"
    assert_file_contains "$PROJECT_ROOT/.env.template" "REDIS_BIND_ADDRESS=127.0.0.1"
    assert_file_contains "$PROJECT_ROOT/.env.template" "RABBITMQ_BIND_ADDRESS=127.0.0.1"
}

@test "init.sh writes DB_BIND_ADDRESS/REDIS_BIND_ADDRESS/RABBITMQ_BIND_ADDRESS into the generated .env" {
    assert_file_contains "$SCRIPTS_DIR/init.sh" "DB_BIND_ADDRESS=127.0.0.1"
    assert_file_contains "$SCRIPTS_DIR/init.sh" "REDIS_BIND_ADDRESS=127.0.0.1"
    assert_file_contains "$SCRIPTS_DIR/init.sh" "RABBITMQ_BIND_ADDRESS=127.0.0.1"
}

@test "docs/follow-ups.md notes already-live servers keep 0.0.0.0 until deliberately merged" {
    assert_file_contains "$PROJECT_ROOT/docs/follow-ups.md" "bm-nyc-01"
    assert_file_contains "$PROJECT_ROOT/docs/follow-ups.md" "does not retroactively apply"
}

# =============================================================================
# kamailio-exporter / RTPEngine scrape / Alertmanager (VOIP-1338)
# =============================================================================

@test "docker-compose.yml.dist defines kamailio-exporter service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  kamailio-exporter:"
}

@test "docker-compose.yml.dist defines alertmanager service" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  alertmanager:"
}

@test "docker-compose.yml.dist kamailio-exporter and rtpengine do not share a port (9105 vs 9101)" {
    # Both run network_mode: host, so a port collision would mean two
    # processes trying to bind the same host port - the second one to start
    # would fail outright. See the kamailio-exporter service's own comment
    # block for the full explanation.
    local rtpengine_block kamailio_exporter_block
    rtpengine_block=$(sed -n '/^  rtpengine:/,/^  [a-z-]\+:$/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    kamailio_exporter_block=$(sed -n '/^  kamailio-exporter:/,/^  [a-z-]\+:$/p' "$PROJECT_ROOT/docker-compose.yml.dist")

    [[ "$rtpengine_block" == *"RTPENGINE_LISTEN_HTTP:-9101"* ]]
    [[ "$kamailio_exporter_block" == *"0.0.0.0:9105"* ]]
    [[ "$kamailio_exporter_block" != *":9101"* ]]
}

@test "docker-compose.yml.dist kamailio and kamailio-exporter share the kamailio-run volume" {
    local kamailio_block kamailio_exporter_block
    kamailio_block=$(sed -n '/^  kamailio:/,/^  [a-z-]\+:$/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    kamailio_exporter_block=$(sed -n '/^  kamailio-exporter:/,/^  [a-z-]\+:$/p' "$PROJECT_ROOT/docker-compose.yml.dist")

    [[ "$kamailio_block" == *"kamailio-run:/run/kamailio"* ]]
    [[ "$kamailio_exporter_block" == *"kamailio-run:/run/kamailio"* ]]
}

@test "docker-compose.yml.dist kamailio-exporter scrape-uri matches the shared volume mount path" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "unix:///run/kamailio/kamailio_ctl"
}

@test "docker-compose.yml.dist pins kamailio-exporter and alertmanager images by digest" {
    for svc in kamailio-exporter alertmanager; do
        local block
        block=$(sed -n "/^  ${svc}:/,/^  [a-z-]\+:$/p" "$PROJECT_ROOT/docker-compose.yml.dist")
        [[ "$block" == *"@sha256:"* ]]
    done
}

@test "docker-compose.yml.dist defines kamailio-run and alertmanager_data volumes" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  kamailio-run:"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "  alertmanager_data:"
}

@test "docker-compose.yml.dist binds alertmanager's published host port to 127.0.0.1 only, not 0.0.0.0" {
    # Same posture as prometheus/grafana: the container's own internal
    # --web.listen-address is legitimately 0.0.0.0 (binds every interface
    # INSIDE the container namespace), but the ports: host-publish mapping
    # must be loopback-only. Match the existing prometheus/grafana test
    # style (assert the published-port string), not a "block excludes
    # 0.0.0.0" check, which would also (incorrectly) flag the internal
    # listen-address.
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" '"127.0.0.1:9093:9093"'
}

@test "docker-compose.yml.dist prometheus has host.docker.internal extra_hosts for rtpengine/kamailio-exporter scraping" {
    local block
    block=$(sed -n '/^  prometheus:/,/^  [a-z-]\+:$/p' "$PROJECT_ROOT/docker-compose.yml.dist")
    [[ "$block" == *"host.docker.internal:host-gateway"* ]]
}

@test "docker-compose.yml.dist mounts alert-rules.yml and alertmanager.yml" {
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./config/prometheus/alert-rules.yml:/etc/prometheus/alert-rules.yml:ro"
    assert_file_contains "$PROJECT_ROOT/docker-compose.yml.dist" "./config/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro"
}

@test "config/prometheus/prometheus.yml is still valid YAML after VOIP-1338 edits" {
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
        run python3 -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/config/prometheus/prometheus.yml'))"
        [[ "$status" -eq 0 ]]
    else
        skip "python3+pyyaml not available"
    fi
}

@test "config/prometheus/prometheus.yml scrapes rtpengine and kamailio via host.docker.internal" {
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "host.docker.internal:9101"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "host.docker.internal:9105"
}

@test "config/prometheus/prometheus.yml wires alertmanager and rule_files" {
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "alertmanager:9093"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/prometheus.yml" "/etc/prometheus/alert-rules.yml"
}

@test "config/prometheus/alert-rules.yml exists and is valid YAML" {
    [[ -f "$PROJECT_ROOT/config/prometheus/alert-rules.yml" ]]
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
        run python3 -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/config/prometheus/alert-rules.yml'))"
        [[ "$status" -eq 0 ]]
    else
        skip "python3+pyyaml not available"
    fi
}

@test "config/prometheus/alert-rules.yml defines the expected alert groups" {
    for grp in instance-health node-resources rtpengine rabbitmq redis; do
        assert_file_contains "$PROJECT_ROOT/config/prometheus/alert-rules.yml" "name: $grp"
    done
}

@test "config/prometheus/alert-rules.yml does not port Kubernetes-only pod-health rules" {
    # Only check the actual `groups:` rule definitions, not this file's own
    # header comment - which deliberately documents (in prose) that the
    # pod-health group and its kube_pod_*/kube_deployment_* metrics were
    # NOT ported, and would otherwise self-trip this exact assertion.
    run bash -c "sed -n '/^groups:/,\$p' '$PROJECT_ROOT/config/prometheus/alert-rules.yml' | grep -c 'kube_pod\|kube_deployment'"
    [[ "$output" == "0" ]]
}

@test "config/prometheus/alert-rules.yml RabbitMQ alarm rules use the built-in rabbitmq_prometheus plugin's actual metric names" {
    # rabbitmq_node_mem_alarm/rabbitmq_node_disk_free_alarm belong to the
    # community rabbitmq_exporter, not the built-in plugin this stack
    # enables - see this file's own header comment for the empirical check.
    assert_file_contains "$PROJECT_ROOT/config/prometheus/alert-rules.yml" "rabbitmq_alarms_memory_used_watermark"
    assert_file_contains "$PROJECT_ROOT/config/prometheus/alert-rules.yml" "rabbitmq_alarms_free_disk_space_watermark"
    # Same reasoning as the pod-health test above: only check the `groups:`
    # rule definitions, not the header comment's prose explanation (which
    # quotes the old, wrong metric names on purpose).
    run bash -c "sed -n '/^groups:/,\$p' '$PROJECT_ROOT/config/prometheus/alert-rules.yml' | grep -c 'rabbitmq_node_mem_alarm\|rabbitmq_node_disk_free_alarm'"
    [[ "$output" == "0" ]]
}

@test "config/prometheus/alert-rules.yml RedisHighMemory guards against a zero maxmemory divide-by-zero" {
    assert_file_contains "$PROJECT_ROOT/config/prometheus/alert-rules.yml" "redis_memory_max_bytes > 0"
}

@test "config/alertmanager/alertmanager.yml exists and is valid YAML" {
    [[ -f "$PROJECT_ROOT/config/alertmanager/alertmanager.yml" ]]
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
        run python3 -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/config/alertmanager/alertmanager.yml'))"
        [[ "$status" -eq 0 ]]
    else
        skip "python3+pyyaml not available"
    fi
}

@test "config/grafana/dashboards ships kamailio and rtpengine dashboards (VOIP-1338)" {
    [[ -f "$PROJECT_ROOT/config/grafana/dashboards/kamailio-overview.json" ]]
    [[ -f "$PROJECT_ROOT/config/grafana/dashboards/rtpengine-overview.json" ]]
}

@test "config/grafana/dashboards kamailio/rtpengine JSON files are valid and reference the Prometheus datasource" {
    for f in "$PROJECT_ROOT/config/grafana/dashboards/kamailio-overview.json" "$PROJECT_ROOT/config/grafana/dashboards/rtpengine-overview.json"; do
        if command -v python3 &>/dev/null && python3 -c "import json" 2>/dev/null; then
            run python3 -c "import json; json.load(open('$f'))"
            [[ "$status" -eq 0 ]]
        fi
        assert_file_contains "$f" '"type": "prometheus"'
    done
}

@test "docker compose config renders successfully with the VOIP-1338 monitoring additions" {
    if ! command -v docker &>/dev/null; then
        skip "docker not available"
    fi
    run docker compose -f "$PROJECT_ROOT/docker-compose.yml.dist" --env-file "$PROJECT_ROOT/.env.template" config --quiet
    [[ "$status" -eq 0 ]]
}

@test "asterisk-overview dashboard was deliberately NOT ported (no exporter exists, VOIP-1338)" {
    [[ ! -f "$PROJECT_ROOT/config/grafana/dashboards/asterisk-overview.json" ]]
}

@test "docs/follow-ups.md documents the deferred alertmanager notification channel (VOIP-1338)" {
    assert_file_contains "$PROJECT_ROOT/docs/follow-ups.md" "VOIP-1338"
}

@test "config/grafana/dashboards/rtpengine-overview.json only references verified rtpengine_* Prometheus metric names" {
    # Regression guard for a real bug caught in review: an earlier draft of
    # this dashboard referenced rtpengine_calls/_packet_loss/_jitter/_mos,
    # none of which sipwise/rtpengine's daemon/statistics.c PROM() macro
    # ever emits - the dashboard would have shown "No data" on 4 of 9
    # panels forever. This allowlist was built by extracting every
    # PROM("name", ...) call from rtpengine's actual source.
    local allowed="sessions|sessions_total|closed_sessions_total|transcoded_media|mediastreams|uptime_seconds|bytes_total|packets_total|packets_lost|ports|ports_free|ports_used|one_way_sessions_total|zero_packet_streams_total|mos_total|mos2_total|mos_samples_total|jitter_total|jitter2_total|jitter_samples_total|packetloss_total|packetloss2_total|packetloss_samples_total|rtt_e2e_total|rtt_dsct_total|call_duration_total|call_duration2_total|call_duration_avg|requests_total|request_seconds_total|errors_total|rtp_duplicates|rtp_reordered|rtp_seq_resets|rtp_skips|transcode_bytes_total|transcode_packets_total|transcode_samples_total|transcoders|packet_errors_total"

    run python3 - "$PROJECT_ROOT/config/grafana/dashboards/rtpengine-overview.json" "$allowed" <<'PYEOF'
import json, re, sys
path, allowed = sys.argv[1], sys.argv[2].split("|")
d = json.load(open(path))
found = set()
for p in d.get("panels", []):
    for t in p.get("targets", []):
        expr = t.get("expr", "")
        for m in re.finditer(r"rtpengine_([a-zA-Z0-9_]+)", expr):
            found.add(m.group(1))
unknown = sorted(n for n in found if n not in allowed)
if unknown:
    print("Unknown/unverified rtpengine_* metric names:", unknown)
    sys.exit(1)
print("all", len(found), "referenced metric names are in the verified allowlist")
PYEOF
    [[ "$status" -eq 0 ]]
}
