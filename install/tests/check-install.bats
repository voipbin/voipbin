#!/usr/bin/env bats
# Tests for scripts/check-install.sh (dual-mode DNS, VOIP-1275 Phase 5)
# Mode-aware check selection, CHECK-line and VOIPBIN_CHECK grammar, and the
# identical-environment rule for the derived service count.
#
# Isolation rule: docker / dig / curl / mkcert are ALL stubbed via
# MOCK_BIN_DIR and PROJECT_DIR is $TEST_TEMP_DIR — every check runs against
# fixtures, never against the real tree, daemon, resolver or network.

load 'test_helper'

setup() {
    setup_test_env
    RESOLV_FIXTURE="$TEST_TEMP_DIR/resolv.conf"
    RESOLV_BACKUP_FIXTURE="$TEST_TEMP_DIR/resolv.conf.voipbin-backup"
    RESOLV_UPSTREAMS_FIXTURE="$TEST_TEMP_DIR/resolv.conf.voipbin-upstreams"
}

teardown() {
    teardown_test_env
}

# run_check: invoke the script with the fixture resolv.conf paths and a clean
# COMPOSE_PROFILES (tests that want the conflict set it explicitly).
run_check() {
    run env -u COMPOSE_PROFILES \
        RESOLV_CONF="$RESOLV_FIXTURE" RESOLV_BACKUP="$RESOLV_BACKUP_FIXTURE" \
        RESOLV_UPSTREAMS="$RESOLV_UPSTREAMS_FIXTURE" \
        bash "$SCRIPTS_DIR/check-install.sh"
}

make_internal_env() {
    create_env_file \
        "DOMAIN_MODE=internal" \
        "COMPOSE_PROFILES=internal-dns" \
        "TLS_MODE=selfsigned" \
        "BASE_DOMAIN=voipbin.test" \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108" \
        "DOMAIN_NAME_EXTENSION=registrar.voipbin.test"
}

make_external_env() {
    create_env_file \
        "DOMAIN_MODE=external" \
        "COMPOSE_PROFILES=" \
        "TLS_MODE=byo" \
        "BASE_DOMAIN=example.com" \
        "HOST_EXTERNAL_IP=203.0.113.10" \
        "KAMAILIO_EXTERNAL_IP=203.0.113.11" \
        "DOMAIN_NAME_EXTENSION=registrar.example.com"
}

# Docker stub: 4 compose services all running/healthy, registrar-manager
# inspectable with the given DOMAIN_NAME_EXTENSION. Records the
# COMPOSE_PROFILES value each compose invocation ran with.
stub_docker_healthy() {
    local domain_ext="${1:-registrar.voipbin.test}"
    mock_command_script "docker" '
if [[ "$1" == "compose" ]]; then
    echo "COMPOSE_PROFILES=${COMPOSE_PROFILES-<unset>}" >> "'"$TEST_TEMP_DIR"'/compose_env.log"
    if [[ "$2" == "config" ]]; then
        printf "db\nredis\nrabbitmq\nregistrar-manager\n"
        exit 0
    fi
    if [[ "$2" == "ps" && "$*" == *"--status running"* ]]; then
        printf "id1\nid2\nid3\nid4\n"
        exit 0
    fi
    if [[ "$2" == "ps" ]]; then
        printf "voipbin-db\tUp 5 minutes (healthy)\nvoipbin-redis\tUp 5 minutes\nrabbitmq-1\tUp 5 minutes\nvoipbin-registrar-mgr\tUp 5 minutes (healthy)\n"
        exit 0
    fi
fi
if [[ "$1" == "inspect" ]]; then
    echo "DOMAIN_NAME_EXTENSION='"$domain_ext"'"
    echo "PATH=/usr/bin"
    exit 0
fi
if [[ "$1" == "exec" && "$*" == *"schedule-control"* ]]; then
    printf "%s" "[{\"name\":\"number-renew\",\"enabled\":true},{\"name\":\"execution-retention\",\"enabled\":true},{\"name\":\"database-backup\",\"enabled\":true}]"
    exit 0
fi
exit 1'
}

# dig stub answering api./sip. of any domain with the given IPs; logs args.
stub_dig() {
    local api_ip="$1"
    local sip_ip="$2"
    mock_command_script "dig" '
echo "DIG:$*" >> "'"$TEST_TEMP_DIR"'/dig.log"
for a in "$@"; do last="$a"; done
case "$last" in
    api.*) echo "'"$api_ip"'" ;;
    sip.*) echo "'"$sip_ip"'" ;;
esac
exit 0'
}

# curl stub: succeeds, answers 200 for -w invocations; logs args.
stub_curl_ok() {
    mock_command_script "curl" '
echo "CURL:$*" >> "'"$TEST_TEMP_DIR"'/curl.log"
if [[ "$*" == *"-w"* ]]; then echo -n "200"; fi
exit 0'
}

# =============================================================================
# All-pass runs + result grammar
# =============================================================================

@test "internal all-pass: every CHECK line passes, final status=pass, exit 0" {
    make_internal_env
    stub_docker_healthy "registrar.voipbin.test"
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    [[ "$status" -eq 0 ]]
    for name in compose-profiles services dns tls api-ping realm scheduler resolv-conf; do
        [[ "$output" == *"CHECK $name: pass"* ]]
    done
    # TLS_MODE=selfsigned: cert-trust is a skip, not a fail
    [[ "$output" == *'CHECK cert-trust: skip'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_CHECK:\ status=pass\ passed=[0-9]+\ failed=0\ mode=internal$ ]]
}

@test "external all-pass: mode-appropriate checks, final status=pass, exit 0" {
    make_external_env
    stub_docker_healthy "registrar.example.com"
    stub_dig "203.0.113.10" "203.0.113.11"
    stub_curl_ok
    echo "nameserver 8.8.8.8" > "$RESOLV_FIXTURE"
    # BYO cert valid far beyond 14 days
    mkdir -p "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=example.com" 2>/dev/null

    run_check

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'CHECK cert-trust: pass'* ]]
    [[ "$output" == *'CHECK dns: pass'* ]]
    [[ "$output" == *'CHECK tls: pass'* ]]
    [[ "$output" == *'CHECK scheduler: pass'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_CHECK:\ status=pass\ passed=[0-9]+\ failed=0\ mode=external$ ]]
    # external DNS goes through the system resolver, never @127.0.0.1
    ! grep -q '@127.0.0.1' "$TEST_TEMP_DIR/dig.log"
    # external TLS check never uses -k (the -k invocation is api-ping only)
    ! grep -E '^CURL:.*-fsS.*-k' "$TEST_TEMP_DIR/curl.log" | grep -qv 'cacert'
}

@test "internal DNS check queries CoreDNS directly (@127.0.0.1)" {
    make_internal_env
    stub_docker_healthy
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    grep -q '@127.0.0.1' "$TEST_TEMP_DIR/dig.log"
}

@test "service count runs docker compose with the .env COMPOSE_PROFILES (identical env)" {
    make_internal_env
    stub_docker_healthy
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    # Every compose invocation (config + both ps calls) saw the same
    # .env-sourced profile set.
    [[ -f "$TEST_TEMP_DIR/compose_env.log" ]]
    [[ "$(sort -u "$TEST_TEMP_DIR/compose_env.log")" == "COMPOSE_PROFILES=internal-dns" ]]
}

# =============================================================================
# Failure detection
# =============================================================================

@test "service count mismatch fails the services check and the run" {
    make_internal_env
    mock_command_script "docker" '
if [[ "$1" == "compose" && "$2" == "config" ]]; then printf "db\nredis\nrabbitmq\nregistrar-manager\n"; exit 0; fi
if [[ "$1" == "compose" && "$2" == "ps" && "$*" == *"--status running"* ]]; then printf "id1\nid2\n"; exit 0; fi
if [[ "$1" == "compose" && "$2" == "ps" ]]; then printf "voipbin-db\tUp 5 minutes\nvoipbin-redis\tUp 5 minutes\n"; exit 0; fi
if [[ "$1" == "inspect" ]]; then echo "DOMAIN_NAME_EXTENSION=registrar.voipbin.test"; exit 0; fi
exit 1'
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK services: fail running=2 expected=4'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_CHECK:\ status=fail\ passed=[0-9]+\ failed=[1-9][0-9]*\ mode=internal$ ]]
}

@test "unhealthy service fails the services check naming the container" {
    make_internal_env
    mock_command_script "docker" '
if [[ "$1" == "compose" && "$2" == "config" ]]; then printf "db\nredis\n"; exit 0; fi
if [[ "$1" == "compose" && "$2" == "ps" && "$*" == *"--status running"* ]]; then printf "id1\nid2\n"; exit 0; fi
if [[ "$1" == "compose" && "$2" == "ps" ]]; then printf "voipbin-db\tUp 5 minutes (unhealthy)\nvoipbin-redis\tUp 5 minutes\n"; exit 0; fi
if [[ "$1" == "inspect" ]]; then echo "DOMAIN_NAME_EXTENSION=registrar.voipbin.test"; exit 0; fi
exit 1'
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK services: fail'* ]]
    [[ "$output" == *'voipbin-db'* ]]
}

@test "external DNS mismatch fails listing the expected records" {
    make_external_env
    stub_docker_healthy "registrar.example.com"
    stub_dig "198.51.100.99" "198.51.100.98"
    stub_curl_ok
    echo "nameserver 8.8.8.8" > "$RESOLV_FIXTURE"
    mkdir -p "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=example.com" 2>/dev/null

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK dns: fail expected records: api.example.com A 203.0.113.10, sip.example.com A 203.0.113.11'* ]]
    [[ "$output" == *'DNS runbook'* ]]
}

@test "internal commented-out nameserver line is not treated as CoreDNS-configured" {
    make_internal_env
    stub_docker_healthy "registrar.voipbin.test"
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    printf '#nameserver 127.0.0.1\nnameserver 8.8.8.8\n' > "$RESOLV_FIXTURE"

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK resolv-conf: fail'* ]]
}

@test "internal resolv-conf pass detail reports fallback upstream count" {
    make_internal_env
    stub_docker_healthy "voipbin.test"
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    printf '8.8.8.8\n8.8.4.4\n' > "$RESOLV_UPSTREAMS_FIXTURE"

    run_check

    [[ "$output" == *'CHECK resolv-conf: pass'* ]]
    [[ "$output" == *'fallback: 2 upstream(s)'* ]]
}

@test "internal resolv-conf pass detail stays a single line when the upstreams state file exists but is empty" {
    make_internal_env
    stub_docker_healthy "voipbin.test"
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    : > "$RESOLV_UPSTREAMS_FIXTURE"  # exists, but empty (killed process, corruption, etc.)

    run_check

    # A `grep -c` zero-match/command-substitution bug would emit a
    # two-line "0\n0" count here, splitting the CHECK line in two and
    # breaking the "one CHECK line per check" output contract.
    local resolv_conf_lines
    resolv_conf_lines=$(printf '%s\n' "$output" | command grep -c '^CHECK resolv-conf:')
    assert_equal "$resolv_conf_lines" "1"
    [[ "$output" == *'CHECK resolv-conf: pass'* ]]
    [[ "$output" == *'fallback: 0 upstream(s)'* ]]
}

@test "internal resolv-conf pass detail reports zero fallback upstreams when state file is absent" {
    make_internal_env
    stub_docker_healthy "voipbin.test"
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    # No $RESOLV_UPSTREAMS_FIXTURE file created.

    run_check

    [[ "$output" == *'CHECK resolv-conf: pass'* ]]
    [[ "$output" == *'fallback: 0 upstream(s)'* ]]
}

@test "external leftover upstreams-state file alone fails resolv-conf (backup and hijack both absent)" {
    make_external_env
    stub_docker_healthy "registrar.example.com"
    stub_dig "203.0.113.10" "203.0.113.11"
    stub_curl_ok
    echo "nameserver 8.8.8.8" > "$RESOLV_FIXTURE"
    printf '8.8.8.8\n' > "$RESOLV_UPSTREAMS_FIXTURE"
    mkdir -p "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=example.com" 2>/dev/null

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK resolv-conf: fail'* ]]
    [[ "$output" == *'upstreams=true'* ]]
    [[ "$output" == *'hijacked=false'* ]]
    [[ "$output" == *'backup=false'* ]]
    [[ "$output" == *'setup-dns.sh --uninstall'* ]]
}

@test "external leftover resolv.conf hijack fails naming setup-dns.sh --uninstall" {
    make_external_env
    stub_docker_healthy "registrar.example.com"
    stub_dig "203.0.113.10" "203.0.113.11"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    mkdir -p "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=example.com" 2>/dev/null

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK resolv-conf: fail'* ]]
    [[ "$output" == *'setup-dns.sh --uninstall'* ]]
}

@test "external near-expiry BYO cert fails cert-trust naming install-certs.sh" {
    make_external_env
    stub_docker_healthy "registrar.example.com"
    stub_dig "203.0.113.10" "203.0.113.11"
    stub_curl_ok
    echo "nameserver 8.8.8.8" > "$RESOLV_FIXTURE"
    mkdir -p "$CERTS_DIR/api"
    # Expires in 5 days (< the 14-day threshold)
    openssl req -x509 -nodes -newkey rsa:2048 -days 5 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=example.com" 2>/dev/null

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK cert-trust: fail'* ]]
    [[ "$output" == *'install-certs.sh'* ]]
}

@test "realm mismatch fails with the recreate remedy" {
    make_internal_env
    stub_docker_healthy "voipbin.test"
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK realm: fail running=voipbin.test .env=registrar.voipbin.test'* ]]
    [[ "$output" == *'docker compose rm -fsv registrar-manager'* ]]
}

# =============================================================================
# Scheduler present and firing (VOIP-1281)
# =============================================================================

@test "scheduler passes when all three seeded schedules are enabled" {
    load_check_install_functions
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$*" == *"schedule-control"* ]]; then
    printf "%s" "[{\"name\":\"number-renew\",\"enabled\":true},{\"name\":\"execution-retention\",\"enabled\":true},{\"name\":\"database-backup\",\"enabled\":true}]"
    exit 0
fi
exit 1'

    run check_scheduler

    [[ "$output" == *'CHECK scheduler: pass'* ]]
    [[ "$output" == *'3 schedule(s) enabled'* ]]
}

@test "scheduler fails naming a disabled seeded schedule" {
    load_check_install_functions
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$*" == *"schedule-control"* ]]; then
    printf "%s" "[{\"name\":\"number-renew\",\"enabled\":false},{\"name\":\"execution-retention\",\"enabled\":true},{\"name\":\"database-backup\",\"enabled\":true}]"
    exit 0
fi
exit 1'

    run check_scheduler

    [[ "$output" == *'CHECK scheduler: fail'* ]]
    [[ "$output" == *'number-renew'* ]]
}

@test "scheduler fails naming a missing seeded schedule" {
    load_check_install_functions
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$*" == *"schedule-control"* ]]; then
    printf "%s" "[{\"name\":\"execution-retention\",\"enabled\":true},{\"name\":\"database-backup\",\"enabled\":true}]"
    exit 0
fi
exit 1'

    run check_scheduler

    [[ "$output" == *'CHECK scheduler: fail'* ]]
    [[ "$output" == *'number-renew'* ]]
}

@test "scheduler fails when schedule-manager is not running or not inspectable" {
    load_check_install_functions
    mock_command_script "docker" 'exit 1'

    run check_scheduler

    [[ "$output" == *'CHECK scheduler: fail schedule-manager not running'* ]]
}

# =============================================================================
# Cert trust chain (internal, mkcert CAROOT match — design §2.5/§2.8)
# =============================================================================

# make_ca_signed_cert <ca-dir> <cert-dir>: real openssl CA + a cert signed by
# it, so the issuer-vs-CA-subject comparison exercises real openssl output.
make_ca_signed_cert() {
    local ca_dir="$1"
    local cert_dir="$2"
    mkdir -p "$ca_dir" "$cert_dir"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$ca_dir/rootCA.key" -out "$ca_dir/rootCA.pem" \
        -subj "/O=mkcert development CA/CN=mkcert test CA" 2>/dev/null
    openssl req -new -nodes -newkey rsa:2048 \
        -keyout "$cert_dir/privkey.pem" -out "$cert_dir/cert.csr" \
        -subj "/CN=voipbin.test" 2>/dev/null
    openssl x509 -req -in "$cert_dir/cert.csr" \
        -CA "$ca_dir/rootCA.pem" -CAkey "$ca_dir/rootCA.key" -CAcreateserial \
        -out "$cert_dir/cert.pem" -days 30 2>/dev/null
}

@test "internal cert-trust passes when the cert issuer is the mkcert CAROOT CA" {
    load_check_install_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "TLS_MODE=mkcert"
    make_ca_signed_cert "$TEST_TEMP_DIR/caroot" "$CERTS_DIR/api"
    mock_command_script "mkcert" '
if [[ "$1" == "-CAROOT" ]]; then echo "'"$TEST_TEMP_DIR"'/caroot"; exit 0; fi
exit 0'
    load_check_env

    run check_cert_trust

    [[ "$output" == *'CHECK cert-trust: pass'* ]]
}

@test "internal cert-trust fails on CAROOT mismatch (different CA signed the cert)" {
    load_check_install_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "TLS_MODE=mkcert"
    make_ca_signed_cert "$TEST_TEMP_DIR/caroot_a" "$CERTS_DIR/api"
    # The trust store's CA is a DIFFERENT one (the CAROOT-under-sudo failure)
    mkdir -p "$TEST_TEMP_DIR/caroot_b"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$TEST_TEMP_DIR/caroot_b/rootCA.key" -out "$TEST_TEMP_DIR/caroot_b/rootCA.pem" \
        -subj "/O=mkcert development CA/CN=other CA (root)" 2>/dev/null
    mock_command_script "mkcert" '
if [[ "$1" == "-CAROOT" ]]; then echo "'"$TEST_TEMP_DIR"'/caroot_b"; exit 0; fi
exit 0'
    load_check_env

    run check_cert_trust

    [[ "$output" == *'CHECK cert-trust: fail'* ]]
    [[ "$output" == *'CAROOT mismatch'* ]]
    [[ "$output" == *'setup-host.sh'* ]]
}

# =============================================================================
# COMPOSE_PROFILES guard + stale .env (§2.5/§6) and .env precondition
# =============================================================================

@test "stale internal .env (no COMPOSE_PROFILES key) fails the compose-profiles check" {
    create_env_file \
        "DOMAIN_MODE=internal" \
        "BASE_DOMAIN=voipbin.test" \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108" \
        "DOMAIN_NAME_EXTENSION=registrar.voipbin.test"
    stub_docker_healthy
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run_check

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK compose-profiles: fail stale .env, re-run ./scripts/init.sh --yes'* ]]
}

@test "shell-exported COMPOSE_PROFILES contradicting .env fails the compose-profiles check" {
    make_internal_env
    stub_docker_healthy
    stub_dig "192.168.1.100" "192.168.1.108"
    stub_curl_ok
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"

    run env COMPOSE_PROFILES="something-else" \
        RESOLV_CONF="$RESOLV_FIXTURE" RESOLV_BACKUP="$RESOLV_BACKUP_FIXTURE" \
        bash "$SCRIPTS_DIR/check-install.sh"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK compose-profiles: fail'* ]]
    [[ "$output" == *'unset COMPOSE_PROFILES'* ]]
}

@test "missing .env fails immediately with a mode=unknown result line" {
    rm -f "$PROJECT_DIR/.env"
    mock_command "docker" ""

    run_check

    [[ "$status" -eq 1 ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_CHECK:\ status=fail\ passed=0\ failed=1\ mode=unknown ]]
    [[ "$last_line" == *'run ./scripts/init.sh first'* ]]
}
