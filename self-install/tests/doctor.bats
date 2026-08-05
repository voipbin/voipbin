#!/usr/bin/env bats
# Tests for scripts/doctor.sh (sandbox install doctor, VOIP-1280)
# Stage-aware check selection, DOCTOR/FIX line and VOIPBIN_DOCTOR grammar,
# docker-availability gating, and the identical-environment rule for every
# docker compose invocation.
#
# Isolation rule: docker / dig / ss / ip / df / id / mkcert are ALL stubbed via
# MOCK_BIN_DIR and PROJECT_DIR is $TEST_TEMP_DIR — every check runs against
# fixtures, never against the real tree, daemon, resolver or network. openssl
# is real (cert fixtures), stubbed only where a capability is missing.

load 'test_helper'

setup() {
    setup_test_env
    RESOLV_FIXTURE="$TEST_TEMP_DIR/resolv.conf"
}

teardown() {
    teardown_test_env
}

# run_doctor: invoke the script with the fixture resolv.conf path and a clean
# COMPOSE_PROFILES (tests that want the conflict set it explicitly).
run_doctor() {
    run env -u COMPOSE_PROFILES \
        RESOLV_CONF="$RESOLV_FIXTURE" \
        bash "$SCRIPTS_DIR/doctor.sh"
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

# =============================================================================
# Fixture builders (real openssl for cert fixtures)
# =============================================================================

# Append the four SSL base64 vars matching certs/api to .env (certs-env-sync).
sync_cert_env() {
    local cert_b64 key_b64
    cert_b64=$(base64 -w0 "$CERTS_DIR/api/cert.pem")
    key_b64=$(base64 -w0 "$CERTS_DIR/api/privkey.pem")
    {
        echo "API_SSL_CERT_BASE64=$cert_b64"
        echo "API_SSL_PRIVKEY_BASE64=$key_b64"
        echo "HOOK_SSL_CERT_BASE64=$cert_b64"
        echo "HOOK_SSL_PRIVKEY_BASE64=$key_b64"
    } >> "$PROJECT_DIR/.env"
}

# Self-signed CN-only cert (the init.sh selfsigned fallback shape) + env sync.
seed_selfsigned_cert() {
    local days="${1:-90}" cn="${2:-voipbin.test}"
    mkdir -p "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days "$days" \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=$cn" 2>/dev/null
    sync_cert_env
}

# CA-signed BYO cert with wildcard SAN (issuer != subject) + env sync.
seed_byo_cert() {
    local d="${1:-example.com}"
    local ca_dir="$TEST_TEMP_DIR/byoca"
    mkdir -p "$ca_dir" "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$ca_dir/ca.key" -out "$ca_dir/ca.pem" \
        -subj "/O=Test CA/CN=Test Root CA" 2>/dev/null
    openssl req -new -nodes -newkey rsa:2048 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$ca_dir/cert.csr" \
        -subj "/CN=$d" 2>/dev/null
    printf 'subjectAltName=DNS:%s,DNS:*.%s\n' "$d" "$d" > "$ca_dir/ext.cnf"
    openssl x509 -req -in "$ca_dir/cert.csr" \
        -CA "$ca_dir/ca.pem" -CAkey "$ca_dir/ca.key" -CAcreateserial \
        -out "$CERTS_DIR/api/cert.pem" -days 90 -extfile "$ca_dir/ext.cnf" 2>/dev/null
    sync_cert_env
}

# mkcert-shaped fixture: CAROOT-style CA + leaf with the mkcert SAN set
# (`voipbin.test` + `*.voipbin.test`, never a literal api.voipbin.test) so the
# SAN clause must be wildcard-aware. mkcert stub answers -CAROOT.
seed_mkcert_cert() {
    local caroot="$TEST_TEMP_DIR/caroot"
    mkdir -p "$caroot" "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$caroot/rootCA.key" -out "$caroot/rootCA.pem" \
        -subj "/O=mkcert development CA/CN=mkcert test CA" 2>/dev/null
    openssl req -new -nodes -newkey rsa:2048 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$caroot/cert.csr" \
        -subj "/CN=voipbin.test" 2>/dev/null
    printf 'subjectAltName=DNS:voipbin.test,DNS:*.voipbin.test\n' > "$caroot/ext.cnf"
    openssl x509 -req -in "$caroot/cert.csr" \
        -CA "$caroot/rootCA.pem" -CAkey "$caroot/rootCA.key" -CAcreateserial \
        -out "$CERTS_DIR/api/cert.pem" -days 90 -extfile "$caroot/ext.cnf" 2>/dev/null
    sync_cert_env
    mock_command_script "mkcert" '
if [[ "$1" == "-CAROOT" ]]; then echo "'"$TEST_TEMP_DIR"'/caroot"; exit 0; fi
exit 0'
}

# =============================================================================
# Command stubs
# =============================================================================

# stub_ss <rows>: emits the given LISTEN rows (ss -tuln[p] shape) for any ss
# invocation. Rows go through a fixture file (they contain quotes).
stub_ss() {
    printf '%s\n' "$1" > "$TEST_TEMP_DIR/ss_rows.txt"
    mock_command_script "ss" '
echo "Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
cat "'"$TEST_TEMP_DIR"'/ss_rows.txt"
exit 0'
}

# Baseline: only the whitelisted systemd-resolved stub listener (no conflicts).
stub_ss_quiet() {
    stub_ss 'udp   UNCONN 0      0      127.0.0.53%lo:53   0.0.0.0:*  users:(("systemd-resolve",pid=1,fd=1))'
}

# Healthy running internal stack: coredns on 127.0.0.1 + host IP, compose
# published ports on 0.0.0.0, kamailio on its dedicated IP.
stub_ss_running_internal() {
    stub_ss 'udp   UNCONN 0      0      127.0.0.53%lo:53   0.0.0.0:*  users:(("systemd-resolve",pid=1,fd=1))
udp   UNCONN 0      0      127.0.0.1:53       0.0.0.0:*  users:(("docker-proxy",pid=100,fd=4))
udp   UNCONN 0      0      192.168.1.100:53   0.0.0.0:*  users:(("docker-proxy",pid=101,fd=4))
tcp   LISTEN 0      511    0.0.0.0:8443       0.0.0.0:*  users:(("docker-proxy",pid=102,fd=4))
tcp   LISTEN 0      511    0.0.0.0:3003       0.0.0.0:*
tcp   LISTEN 0      511    0.0.0.0:3004       0.0.0.0:*
tcp   LISTEN 0      511    0.0.0.0:3005       0.0.0.0:*
udp   UNCONN 0      0      192.168.1.108:5060 0.0.0.0:*  users:(("kamailio",pid=103,fd=9))'
}

# stub_ip <host_ip> <yes|no>: 'ip route get' answers host_ip; 'ip link show'
# reports the VoIP macvlan interfaces present (yes) or missing (no).
stub_ip() {
    local host_ip="$1" ifaces="${2:-yes}"
    mock_command_script "ip" '
if [[ "$1" == "route" && "$2" == "get" ]]; then
    echo "8.8.8.8 via 192.168.1.1 dev eth0 src '"$host_ip"' uid 1000"
    exit 0
fi
if [[ "$1" == "link" && "$2" == "show" ]]; then
    if [[ "'"$ifaces"'" == "yes" ]]; then
        echo "5: $3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500"
        exit 0
    fi
    echo "Device \"$3\" does not exist." >&2
    exit 1
fi
exit 0'
}

# stub_df <free_kb>: df -P output with the given Available column.
stub_df() {
    mock_command_script "df" '
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/sda1 976754616 100000000 '"$1"' 20% /"
exit 0'
}

# stub_id <groups>: id -nG answers the given group list.
stub_id() {
    mock_command_script "id" '
if [[ "$1" == "-nG" ]]; then echo "'"$1"'"; exit 0; fi
exit 0'
}

# dig stub for doctor: api/admin/meet/talk -> host IP, everything else -> the
# kamailio IP; scenario "broken" seeds two wrong records and one NXDOMAIN.
stub_dig_doctor() {
    local host_ip="$1" kam_ip="$2" scenario="${3:-ok}"
    mock_command_script "dig" '
echo "DIG:$*" >> "'"$TEST_TEMP_DIR"'/dig.log"
fqdn=""
for a in "$@"; do
    case "$a" in @*|+*|-*) ;; *) fqdn="$a" ;; esac
done
if [[ "'"$scenario"'" == "multi-ok" && "$fqdn" == api.* ]]; then
    printf "198.51.100.7\n203.0.113.10\n"
    exit 0
fi
if [[ "'"$scenario"'" == "multi-wrong" && "$fqdn" == api.* ]]; then
    printf "198.51.100.1\n198.51.100.2\n"
    exit 0
fi
if [[ "'"$scenario"'" == "broken" ]]; then
    case "$fqdn" in
        api.*) echo "198.51.100.99"; exit 0 ;;
        sip.example.com) echo "198.51.100.98"; exit 0 ;;
        conference.*) exit 0 ;;
    esac
fi
case "$fqdn" in
    api.*|admin.*|meet.*|talk.*) echo "'"$host_ip"'" ;;
    *) echo "'"$kam_ip"'" ;;
esac
exit 0'
}

# Parameterized docker stub covering every probe doctor.sh issues. Scenario
# branches diverge only where the scenario needs them; everything else answers
# healthy. Logs COMPOSE_PROFILES on every compose invocation
# (identical-environment assertion, design §8 item 10).
stub_docker_doctor() {
    local scenario="${1:-healthy}"
    local domain_ext="${2:-registrar.voipbin.test}"
    mock_command_script "docker" '
scenario="'"$scenario"'"

if [[ "$1" == "info" ]]; then
    [[ "$scenario" == "daemon-down" ]] && exit 1
    if [[ "$2" == "-f" ]]; then echo "/var/lib/docker"; fi
    exit 0
fi

if [[ "$1" == "logs" ]]; then
    echo "2026-08-02 ERROR: plugin download failed"
    exit 0
fi

if [[ "$1" == "network" && "$2" == "inspect" ]]; then
    [[ "$scenario" == "no-network" ]] && exit 1
    echo "sandboxproject"
    exit 0
fi

if [[ "$1" == "inspect" ]]; then
    args="$*"
    last=""
    for a in "$@"; do last="$a"; done
    if [[ "$args" == *"voipbin-registrar-mgr"* ]]; then
        if [[ "$scenario" == "realm-mismatch" ]]; then
            echo "DOMAIN_NAME_EXTENSION=voipbin.test"
        else
            echo "DOMAIN_NAME_EXTENSION='"$domain_ext"'"
        fi
        echo "PATH=/usr/bin"
        exit 0
    fi
    if [[ "$args" == *"HostConfig.NetworkMode"* ]]; then
        case "$last" in
            proxy_call) echo "container:owner_call" ;;
            proxy_conference) echo "container:owner_conference" ;;
            proxy_registrar) echo "container:owner_registrar" ;;
            *) echo "container:$last" ;;
        esac
        exit 0
    fi
    if [[ "$args" == *"StartedAt"* ]]; then
        case "$last" in
            owner_call)
                if [[ "$scenario" == "netns-owner-restarted" ]]; then
                    echo "2026-08-02T11:00:00.123456789Z"
                else
                    echo "2026-08-02T09:00:00.123456789Z"
                fi ;;
            owner_*) echo "2026-08-02T09:00:00.123456789Z" ;;
            proxy_*) echo "2026-08-02T10:00:00.123456789Z" ;;
            *) echo "2026-08-02T09:00:00.000000000Z" ;;
        esac
        exit 0
    fi
    exit 0
fi

if [[ "$1" == "exec" ]]; then
    body="$*"
    if [[ "$body" == *"mysqladmin"* ]]; then
        [[ "$scenario" == "db-noping" ]] && exit 1
        echo "mysqld is alive"
        exit 0
    fi
    if [[ "$body" == *"information_schema.TABLES"* ]]; then
        if [[ "$scenario" == "db-empty" || "$scenario" == "marker-stale" ]]; then
            echo "0"
        else
            echo "42"
        fi
        exit 0
    fi
    if [[ "$body" == *"alembic_version"* ]]; then
        [[ "$scenario" == "db-empty" ]] && exit 1
        echo "head123"
        exit 0
    fi
    exit 0
fi

if [[ "$1" == "compose" ]]; then
    echo "COMPOSE_PROFILES=${COMPOSE_PROFILES-<unset>}" >> "'"$TEST_TEMP_DIR"'/compose_env.log"
    shift
    if [[ "$1" == "version" ]]; then
        if [[ "$scenario" == "old-compose" ]]; then echo "2.20.0"; else echo "2.39.1"; fi
        exit 0
    fi
    if [[ "$1" == "exec" ]]; then
        body="$*"
        if [[ "$body" == *"rabbitmq-plugins list -e"* ]]; then
            if [[ "$scenario" == "plugin-disabled" ]]; then
                printf "rabbitmq_management\nrabbitmq_prometheus\n"
            else
                printf "rabbitmq_delayed_message_exchange\nrabbitmq_management\n"
            fi
            exit 0
        fi
        if [[ "$body" == *"rabbitmqctl list_queues"* ]]; then
            if [[ "$scenario" == "queue-zero" ]]; then
                printf "asterisk.call.request\t0\nasterisk.conference.request\t1\nasterisk.registrar.request\t1\n"
            else
                printf "asterisk.call.request\t1\nasterisk.conference.request\t1\nasterisk.registrar.request\t1\n"
            fi
            exit 0
        fi
        exit 0
    fi
    if [[ "$1" == "ps" ]]; then
        shift
        args="$*"
        stack_down="false"
        [[ "$scenario" == "stack-down" ]] && stack_down="true"
        if [[ "$args" == "--status running -q" ]]; then
            [[ "$stack_down" == "true" ]] && exit 0
            printf "cid1\ncid2\ncid3\ncid4\n"
            exit 0
        fi
        if [[ "$args" == "--status running -q "* ]]; then
            svc="${args##* }"
            [[ "$stack_down" == "true" ]] && exit 0
            if [[ "$svc" == "coredns" && "$scenario" == "coredns-down" ]]; then exit 0; fi
            echo "cid_$svc"
            exit 0
        fi
        if [[ "$args" == *"{{.Service}}"* ]]; then
            [[ "$stack_down" == "true" ]] && exit 0
            printf "db\nredis\nrabbitmq\ncoredns\nkamailio\nrtpengine\napi-manager\nsquare-admin\nsquare-meet\nsquare-talk\nregistrar-manager\nasterisk-call\nasterisk-call-proxy\nasterisk-conference\nasterisk-conference-proxy\nasterisk-registrar\nasterisk-registrar-proxy\n"
            exit 0
        fi
        if [[ "$args" == "-a "* ]]; then
            if [[ "$scenario" == "containers-bad" ]]; then
                printf "voipbin-db\trunning\tUp 5 minutes (healthy)\nrabbitmq-1\texited\tExited (1) 2 minutes ago\nvoipbin-kamailio\trunning\tUp 5 minutes\n"
            else
                printf "voipbin-db\trunning\tUp 5 minutes (healthy)\nrabbitmq-1\trunning\tUp 5 minutes\nvoipbin-kamailio\trunning\tUp 5 minutes\nvoipbin-registrar-mgr\trunning\tUp 5 minutes\n"
            fi
            exit 0
        fi
        if [[ "$args" == "-q "* ]]; then
            svc="${args#-q }"
            case "$svc" in
                db) [[ "$scenario" == "db-missing" ]] && exit 0; echo "dbcid" ;;
                asterisk-call)
                    if [[ "$scenario" == "netns-owner-recreated" ]]; then
                        echo "owner_call_new"
                    else
                        echo "owner_call"
                    fi ;;
                asterisk-conference) echo "owner_conference" ;;
                asterisk-registrar) echo "owner_registrar" ;;
                asterisk-call-proxy) echo "proxy_call" ;;
                asterisk-conference-proxy) echo "proxy_conference" ;;
                asterisk-registrar-proxy) echo "proxy_registrar" ;;
                *) echo "cid_$svc" ;;
            esac
            exit 0
        fi
        exit 0
    fi
    exit 0
fi
exit 1'
}

# =============================================================================
# Healthy fixture bundles
# =============================================================================

setup_healthy_internal() {
    make_internal_env
    seed_selfsigned_cert 90 voipbin.test
    touch "$PROJECT_DIR/.test_data_initialized"
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    stub_docker_doctor healthy
    stub_dig_doctor "192.168.1.100" "192.168.1.108"
    stub_ss_running_internal
    stub_ip "192.168.1.100" yes
    stub_df 209715200
    stub_id "tester adm docker"
}

setup_healthy_external() {
    make_external_env
    seed_byo_cert example.com
    touch "$PROJECT_DIR/.test_data_initialized"
    echo "nameserver 8.8.8.8" > "$RESOLV_FIXTURE"
    stub_docker_doctor healthy "registrar.example.com"
    stub_dig_doctor "203.0.113.10" "203.0.113.11"
    stub_ss_quiet
    stub_ip "203.0.113.10" yes
    stub_df 209715200
    stub_id "tester adm docker"
}

# =============================================================================
# Stage awareness + early abort (design §4, §6; §8 item 8)
# =============================================================================

@test "pre-install stage: prerequisites run, env-file fails with init FIX, later checks skip, exit 1" {
    rm -f "$PROJECT_DIR/.env"
    stub_docker_doctor healthy
    stub_ss_quiet
    stub_ip "192.168.1.100" yes
    stub_df 209715200
    stub_id "tester adm docker"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR docker: pass'* ]]
    [[ "$output" == *'DOCTOR env-file: fail'* ]]
    [[ "$output" == *'FIX env-file: ./scripts/init.sh --yes'* ]]
    [[ "$output" == *'DOCTOR domain-mode: skip no .env'* ]]
    [[ "$output" == *'DOCTOR certs: skip no .env'* ]]
    [[ "$output" == *'DOCTOR containers: skip no .env'* ]]
    # the summary references the external-mode init variant
    [[ "$output" == *'--mode external'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_DOCTOR:\ status=fail\ passed=[0-9]+\ failed=[1-9][0-9]*\ warned=[0-9]+\ mode=unknown$ ]]
}

@test "missing common.sh aborts with status=error reason and exit 2" {
    cp "$SCRIPTS_DIR/doctor.sh" "$TEST_TEMP_DIR/doctor.sh"

    run bash "$TEST_TEMP_DIR/doctor.sh"

    [[ "$status" -eq 2 ]]
    [[ "$output" == *'VOIPBIN_DOCTOR: status=error reason="common.sh missing"'* ]]
}

@test "pre-start stage: install state runs, every runtime check skips stack not running" {
    make_internal_env
    seed_selfsigned_cert
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    stub_docker_doctor stack-down
    stub_dig_doctor "192.168.1.100" "192.168.1.108"
    stub_ss_quiet
    stub_ip "192.168.1.100" yes
    stub_df 209715200
    stub_id "tester adm docker"

    run_doctor

    [[ "$output" == *'DOCTOR env-file: pass'* ]]
    for name in containers rabbitmq-plugin queue-consumers proxy-netns database realm test-data; do
        [[ "$output" == *"DOCTOR $name: skip stack not running"* ]]
    done
    # coredns clause fails with the container-only remedy while resolv.conf is fine
    [[ "$output" == *'DOCTOR dns-internal: fail'* ]]
    [[ "$output" == *'FIX dns-internal: docker compose up -d coredns'* ]]
}

# =============================================================================
# All-pass runs + final-line grammar (design §8 items 1, 7b)
# =============================================================================

@test "healthy internal all-pass: every check passes, exit 0, warned=0 mode=internal" {
    setup_healthy_internal

    run_doctor

    [[ "$status" -eq 0 ]]
    for name in docker docker-group compose-version tools ports disk-space \
                env-file domain-mode compose-profiles dns-internal certs \
                certs-env-sync voip-interfaces compose-network containers \
                rabbitmq-plugin queue-consumers proxy-netns database realm test-data; do
        [[ "$output" == *"DOCTOR $name: pass"* ]]
    done
    # TLS_MODE=selfsigned: SAN/issuer clauses are skipped, noted in the detail
    [[ "$output" == *'selfsigned'* ]]
    [[ "$output" == *'DOCTOR dns-external: skip'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_DOCTOR:\ status=pass\ passed=[0-9]+\ failed=0\ warned=0\ mode=internal$ ]]
}

@test "healthy external all-pass: mode-appropriate checks, exit 0, mode=external" {
    setup_healthy_external

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR dns-external: pass'* ]]
    [[ "$output" == *'DOCTOR dns-internal: skip'* ]]
    [[ "$output" == *'DOCTOR certs: pass'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_DOCTOR:\ status=pass\ passed=[0-9]+\ failed=0\ warned=0\ mode=external$ ]]
}

# =============================================================================
# Output grammar + counter semantics (design §3; §8 item 9)
# =============================================================================

@test "grammar: every DOCTOR/FIX line matches the contract and counters exclude skips" {
    setup_healthy_internal

    run_doctor

    [[ "$status" -eq 0 ]]
    local bad="" line
    while IFS= read -r line; do
        if [[ "$line" == DOCTOR\ * ]] && \
           ! [[ "$line" =~ ^DOCTOR\ [a-z-]+:\ (pass|fail|warn|skip)($|\ ) ]]; then
            bad+="$line"$'\n'
        fi
        if [[ "$line" == FIX\ * ]] && ! [[ "$line" =~ ^FIX\ [a-z-]+:\ .+$ ]]; then
            bad+="$line"$'\n'
        fi
    done <<< "$output"
    [[ -z "$bad" ]]

    # skip lines exist and increment no counter: the final-line counts must
    # equal the per-status DOCTOR line counts exactly
    local n_pass n_fail n_warn n_skip last_line
    n_pass=$(grep -c '^DOCTOR [a-z-]*: pass' <<< "$output" || true)
    n_fail=$(grep -c '^DOCTOR [a-z-]*: fail' <<< "$output" || true)
    n_warn=$(grep -c '^DOCTOR [a-z-]*: warn' <<< "$output" || true)
    n_skip=$(grep -c '^DOCTOR [a-z-]*: skip' <<< "$output" || true)
    [[ "$n_skip" -gt 0 ]]
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" == *"passed=$n_pass "* ]]
    [[ "$last_line" == *"failed=$n_fail "* ]]
    [[ "$last_line" == *"warned=$n_warn "* ]]
}

@test "every compose invocation runs with the .env COMPOSE_PROFILES (identical env)" {
    setup_healthy_internal

    run_doctor

    [[ -f "$TEST_TEMP_DIR/compose_env.log" ]]
    [[ "$(sort -u "$TEST_TEMP_DIR/compose_env.log")" == "COMPOSE_PROFILES=internal-dns" ]]
}

# =============================================================================
# Docker-availability gating (design §4; §8 item 2)
# =============================================================================

@test "stopped docker daemon: docker fails with FIX, dependents skip instead of cascade-failing" {
    make_internal_env
    seed_selfsigned_cert
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
    stub_docker_doctor daemon-down
    stub_dig_doctor "192.168.1.100" "192.168.1.108"
    stub_ss_quiet
    stub_ip "192.168.1.100" yes
    stub_df 209715200
    stub_id "tester adm docker"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR docker: fail'* ]]
    [[ "$output" == *'FIX docker: sudo systemctl start docker'* ]]
    [[ "$output" == *'DOCTOR compose-version: skip docker unavailable'* ]]
    [[ "$output" == *'DOCTOR compose-network: skip docker unavailable'* ]]
    for name in containers rabbitmq-plugin queue-consumers proxy-netns database realm test-data; do
        [[ "$output" == *"DOCTOR $name: skip docker unavailable"* ]]
    done
    # FIX lines appear exactly once; the summary re-list is indented, non-FIX
    [[ "$(grep -c '^FIX docker: ' <<< "$output")" -eq 1 ]]
    [[ "$output" == *'  [docker] sudo systemctl start docker'* ]]
}

# =============================================================================
# macOS gating (design §4): Linux-only probes skip, everything else runs
# =============================================================================

@test "macos: ports, voip-interfaces and proxy-netns skip as unsupported, rest runs" {
    setup_healthy_internal
    mock_uname "Darwin"

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR ports: skip unsupported on macos'* ]]
    [[ "$output" == *'DOCTOR voip-interfaces: skip unsupported on macos'* ]]
    [[ "$output" == *'DOCTOR proxy-netns: skip unsupported on macos'* ]]
    [[ "$output" == *'DOCTOR dns-internal: pass'* ]]
    [[ "$output" == *'DOCTOR containers: pass'* ]]
}

# =============================================================================
# Category 1 - prerequisites (design §5)
# =============================================================================

@test "compose version below 2.24.4 fails with the upgrade prescription" {
    setup_healthy_internal
    stub_docker_doctor old-compose

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR compose-version: fail'* ]]
    [[ "$output" == *'FIX compose-version: upgrade docker compose to >= 2.24.4'* ]]
}

@test "pre-install foreign wildcard listener on port 53 fails ports with the lsof prescription" {
    rm -f "$PROJECT_DIR/.env"
    stub_docker_doctor healthy
    stub_ss 'udp   UNCONN 0      0      127.0.0.53%lo:53   0.0.0.0:*  users:(("systemd-resolve",pid=1,fd=1))
udp   UNCONN 0      0      0.0.0.0:53         0.0.0.0:*  users:(("dnsmasq",pid=5,fd=4))'
    stub_ip "192.168.1.100" yes
    stub_df 209715200
    stub_id "tester adm docker"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR ports: fail'* ]]
    [[ "$output" == *'dnsmasq'* ]]
    [[ "$output" == *'FIX ports: sudo lsof -i :53'* ]]
}

@test "disk space below the warn threshold warns without failing the run" {
    setup_healthy_internal
    stub_df 10485760

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR disk-space: warn'* ]]
    [[ "$output" == *'FIX disk-space:'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" == *'status=pass'* ]]
    [[ "$last_line" == *'warned=1'* ]]
}

@test "disk space below the minimum fails with the prune prescription" {
    setup_healthy_internal
    stub_df 2097152

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR disk-space: fail'* ]]
    [[ "$output" == *'docker system prune'* ]]
}

# =============================================================================
# Category 2 - install state (design §5; §8 items 3, 4, 7)
# =============================================================================

@test "commented-out resolv.conf nameserver line fails dns-internal with the setup-host FIX" {
    setup_healthy_internal
    printf '#nameserver 127.0.0.1\nnameserver 8.8.8.8\n' > "$RESOLV_FIXTURE"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR dns-internal: fail'* ]]
    [[ "$output" == *'FIX dns-internal: sudo ./scripts/setup-host.sh'* ]]
}

@test "coredns container down with healthy resolv.conf prescribes only the container restart" {
    setup_healthy_internal
    stub_docker_doctor coredns-down

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR dns-internal: fail'* ]]
    [[ "$output" == *'FIX dns-internal: docker compose up -d coredns'* ]]
}

@test "expired certificate fails the certs check" {
    setup_healthy_internal
    rm -f "$CERTS_DIR/api/cert.pem" "$CERTS_DIR/api/privkey.pem"
    # Capability-probed expired-cert fixture (design §8 item 4): -not_after
    # (OpenSSL 3.4+, never version-gate it) or a PATH-stubbed openssl that
    # fails -checkend for this one test.
    if openssl req -help 2>&1 | grep -q not_after; then
        openssl req -x509 -nodes -newkey rsa:2048 \
            -not_before 20200101000000Z -not_after 20200201000000Z \
            -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
            -subj "/CN=voipbin.test" 2>/dev/null
    else
        openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
            -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
            -subj "/CN=voipbin.test" 2>/dev/null
        local real_openssl
        real_openssl=$(command -v openssl)
        mock_command_script "openssl" '
if [[ "$*" == *"-checkend"* ]]; then exit 1; fi
exec "'"$real_openssl"'" "$@"'
    fi
    # re-sync the four env vars to the new fixture files
    sed -i '/_SSL_.*_BASE64=/d' "$PROJECT_DIR/.env"
    sync_cert_env

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR certs: fail'* ]]
    [[ "$output" == *'expired'* ]]
    [[ "$output" == *'FIX certs:'* ]]
}

@test "near-expiry certificate warns with a FIX and does not affect the exit code" {
    setup_healthy_internal
    rm -f "$CERTS_DIR/api/cert.pem" "$CERTS_DIR/api/privkey.pem"
    seed_selfsigned_cert 5 voipbin.test
    sed -i '/_SSL_.*_BASE64=/d' "$PROJECT_DIR/.env"
    sync_cert_env

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR certs: warn'* ]]
    [[ "$output" == *'FIX certs:'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" == *'status=pass'* ]]
}

@test "mkcert wildcard SAN satisfies api.<domain> and the CAROOT issuer matches" {
    setup_healthy_internal
    rm -f "$CERTS_DIR/api/cert.pem" "$CERTS_DIR/api/privkey.pem"
    sed -i -e 's/^TLS_MODE=.*/TLS_MODE=mkcert/' -e '/_SSL_.*_BASE64=/d' "$PROJECT_DIR/.env"
    seed_mkcert_cert

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR certs: pass'* ]]
}

@test "certs-env-sync fails when the .env base64 vars drift from certs/api" {
    setup_healthy_internal
    sed -i 's/^API_SSL_CERT_BASE64=.*/API_SSL_CERT_BASE64=c3RhbGU=/' "$PROJECT_DIR/.env"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR certs-env-sync: fail'* ]]
    [[ "$output" == *'API_SSL_CERT_BASE64'* ]]
    [[ "$output" == *'FIX certs-env-sync:'* ]]
}

@test "stale internal .env without COMPOSE_PROFILES key fails domain-mode and compose-profiles" {
    setup_healthy_internal
    sed -i '/^COMPOSE_PROFILES=/d' "$PROJECT_DIR/.env"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR domain-mode: fail'* ]]
    [[ "$output" == *'DOCTOR compose-profiles: fail stale .env, re-run ./scripts/init.sh --yes'* ]]
}

@test "invalid DOMAIN_MODE clamps the final line to mode=unknown and both dns checks skip truthfully" {
    setup_healthy_internal
    sed -i 's/^DOMAIN_MODE=.*/DOMAIN_MODE=garbage/' "$PROJECT_DIR/.env"

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR domain-mode: fail'* ]]
    # neither mode-specific skip may claim the OTHER mode is active
    [[ "$output" == *'DOCTOR dns-internal: skip domain mode invalid'* ]]
    [[ "$output" == *'DOCTOR dns-external: skip domain mode invalid'* ]]
    # pinned grammar: mode=<internal|external|unknown>, never the garbage value
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" == *'mode=unknown'* ]]
    [[ "$last_line" != *'mode=garbage'* ]]
}

@test "empty TLS_MODE skips the SAN/issuer clauses instead of false-failing a legacy install" {
    setup_healthy_internal
    sed -i '/^TLS_MODE=/d' "$PROJECT_DIR/.env"

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR certs: pass'* ]]
    [[ "$output" == *'TLS_MODE unset'* ]]
    [[ "$output" != *'certificate is self-signed'* ]]
}

@test "missing voip macvlan interfaces fail with the setup-voip-network prescription" {
    setup_healthy_internal
    stub_ip "192.168.1.100" no

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR voip-interfaces: fail'* ]]
    [[ "$output" == *'FIX voip-interfaces: sudo ./scripts/setup-voip-network.sh'* ]]
}

@test "external DNS diff lists every missing or mismatched record" {
    setup_healthy_external
    stub_dig_doctor "203.0.113.10" "203.0.113.11" broken

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR dns-external: fail'* ]]
    [[ "$output" == *'api.example.com expected 203.0.113.10 got 198.51.100.99'* ]]
    [[ "$output" == *'sip.example.com expected 203.0.113.11 got 198.51.100.98'* ]]
    [[ "$output" == *'conference.example.com expected 203.0.113.11 got NXDOMAIN'* ]]
    [[ "$output" == *'DNS runbook'* ]]
    [[ "$output" == *'sudo ./voipbin dns'* ]]
    # diff lines attach to their check: they print AFTER the DOCTOR/FIX pair
    local doctor_ln first_diff_ln
    doctor_ln=$(grep -n '^DOCTOR dns-external: fail' <<< "$output" | head -1 | cut -d: -f1)
    first_diff_ln=$(grep -n 'api.example.com expected' <<< "$output" | head -1 | cut -d: -f1)
    [[ "$first_diff_ln" -gt "$doctor_ln" ]]
}

@test "external DNS record set containing the expected IP among extras counts as matched" {
    setup_healthy_external
    # expected IP is deliberately NOT the first answer (round-robin ordering)
    stub_dig_doctor "203.0.113.10" "203.0.113.11" multi-ok

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR dns-external: pass'* ]]
}

@test "external DNS mismatch with multiple wrong A records lists the full answer set" {
    setup_healthy_external
    stub_dig_doctor "203.0.113.10" "203.0.113.11" multi-wrong

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR dns-external: fail'* ]]
    [[ "$output" == *'api.example.com expected 203.0.113.10 got 198.51.100.1,198.51.100.2'* ]]
}

# =============================================================================
# Category 3 - runtime pathology (design §5; §8 items 5, 6)
# =============================================================================

@test "disabled rabbitmq delayed_message plugin fails with the restart prescription" {
    setup_healthy_internal
    stub_docker_doctor plugin-disabled

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR rabbitmq-plugin: fail'* ]]
    [[ "$output" == *'FIX rabbitmq-plugin: docker compose restart rabbitmq'* ]]
    [[ "$output" == *'docker compose logs rabbitmq'* ]]
}

@test "zero-consumer queue with Up proxy fails with the exact proxy-restart prescription" {
    setup_healthy_internal
    stub_docker_doctor queue-zero

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR queue-consumers: fail'* ]]
    [[ "$output" == *'asterisk.call.request'* ]]
    [[ "$output" == *'FIX queue-consumers: docker compose restart asterisk-call-proxy asterisk-conference-proxy asterisk-registrar-proxy'* ]]
}

@test "exited container fails the containers check naming it and surfacing the last error line" {
    setup_healthy_internal
    stub_docker_doctor containers-bad

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR containers: fail'* ]]
    [[ "$output" == *'rabbitmq-1'* ]]
    [[ "$output" == *'plugin download failed'* ]]
    [[ "$output" == *'FIX containers: docker compose logs --tail 50'* ]]
    # last-error lines attach to their check: they print AFTER the DOCTOR/FIX pair
    local doctor_ln err_ln
    doctor_ln=$(grep -n '^DOCTOR containers: fail' <<< "$output" | head -1 | cut -d: -f1)
    err_ln=$(grep -n 'plugin download failed' <<< "$output" | head -1 | cut -d: -f1)
    [[ "$err_ln" -gt "$doctor_ln" ]]
}

@test "proxy-netns fails when the owner was recreated under the proxy" {
    setup_healthy_internal
    stub_docker_doctor netns-owner-recreated

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR proxy-netns: fail'* ]]
    [[ "$output" == *'recreated'* ]]
    [[ "$output" == *'FIX proxy-netns: sudo ./voipbin restart asterisk-call'* ]]
}

@test "proxy-netns fails when the owner restarted after the proxy (epoch comparison)" {
    setup_healthy_internal
    stub_docker_doctor netns-owner-restarted

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR proxy-netns: fail'* ]]
    [[ "$output" == *'restarted after'* ]]
    [[ "$output" == *'FIX proxy-netns: sudo ./voipbin restart asterisk-call'* ]]
}

@test "empty schema fails the database check with the migrate prescription" {
    setup_healthy_internal
    stub_docker_doctor db-empty

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR database: fail'* ]]
    [[ "$output" == *'FIX database: ./scripts/migrate.sh'* ]]
}

@test "realm mismatch fails with the recreate remedy" {
    setup_healthy_internal
    stub_docker_doctor realm-mismatch

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR realm: fail'* ]]
    [[ "$output" == *'docker compose rm -fsv registrar-manager && docker compose up -d registrar-manager'* ]]
}

@test "stale test-data marker with an empty schema fails with the marker-removal prescription" {
    setup_healthy_internal
    stub_docker_doctor marker-stale

    run_doctor

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'DOCTOR test-data: fail'* ]]
    [[ "$output" == *'FIX test-data: rm .test_data_initialized'* ]]
}

# =============================================================================
# Function-level tests via load_doctor_functions (design §6 helpers)
# =============================================================================

@test "load_doctor_functions: cert_san_covers matches wildcard SANs for single-label names only" {
    load_doctor_functions
    mkdir -p "$CERTS_DIR/api"
    openssl req -x509 -nodes -newkey rsa:2048 -days 90 \
        -keyout "$CERTS_DIR/api/privkey.pem" -out "$CERTS_DIR/api/cert.pem" \
        -subj "/CN=voipbin.test" \
        -addext 'subjectAltName=DNS:voipbin.test,DNS:*.voipbin.test' 2>/dev/null

    # wildcard *.voipbin.test satisfies the single-label api.voipbin.test
    cert_san_covers "$CERTS_DIR/api/cert.pem" "api.voipbin.test"
    # literal base name matches too
    cert_san_covers "$CERTS_DIR/api/cert.pem" "voipbin.test"
    # a two-label name under the wildcard is NOT satisfied
    run cert_san_covers "$CERTS_DIR/api/cert.pem" "a.b.voipbin.test"
    [[ "$status" -ne 0 ]]
}

@test "load_doctor_functions: doctor_result counters ignore skip, doctor_fix collects prescriptions" {
    load_doctor_functions
    CHECKS_PASSED=0; CHECKS_FAILED=0; CHECKS_WARNED=0; PRESCRIPTIONS=()

    run doctor_result demo skip "nothing to see"
    [[ "$output" == "DOCTOR demo: skip nothing to see" ]]

    doctor_result demo skip "nothing to see" > /dev/null
    doctor_result demo pass "fine" > /dev/null
    doctor_result demo warn "meh" > /dev/null
    doctor_result demo fail "broken" > /dev/null
    [[ "$CHECKS_PASSED" -eq 1 ]]
    [[ "$CHECKS_FAILED" -eq 1 ]]
    [[ "$CHECKS_WARNED" -eq 1 ]]

    run doctor_fix demo "run: something"
    [[ "$output" == "FIX demo: run: something" ]]
    doctor_fix demo "run: something" > /dev/null
    [[ "${#PRESCRIPTIONS[@]}" -eq 1 ]]
    [[ "${PRESCRIPTIONS[0]}" == "[demo] run: something" ]]
}

@test "load_doctor_functions: certs-env-sync skips when encoding fails instead of false-passing" {
    load_doctor_functions
    # .env has no *_SSL_*_BASE64 vars (all empty); if encoding also came back
    # empty, empty == empty would false-pass without the guard
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mkdir -p "$CERTS_DIR/api"
    printf 'dummy\n' > "$CERTS_DIR/api/cert.pem"
    printf 'dummy\n' > "$CERTS_DIR/api/privkey.pem"
    # openssl stub: base64 fails, everything else passes through (fall-through)
    local real_openssl
    real_openssl=$(command -v openssl)
    mock_command_script "openssl" '
if [[ "$1" == "base64" ]]; then exit 1; fi
exec "'"$real_openssl"'" "$@"'
    DOCTOR_STAGE="prestart"
    DOCTOR_MODE="internal"

    run check_certs_env_sync

    [[ "$output" == *'DOCTOR certs-env-sync: skip'* ]]
    [[ "$output" != *'DOCTOR certs-env-sync: pass'* ]]
}

@test "load_doctor_functions: certs-env-sync skips with an actionable detail when the key is unreadable" {
    # the root-owned 0600 privkey case on sudo-created installs; chmod 000
    # only blocks reads for non-root users
    [[ "$EUID" -eq 0 ]] && skip "requires a non-root test runner"
    load_doctor_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mkdir -p "$CERTS_DIR/api"
    printf 'dummy\n' > "$CERTS_DIR/api/cert.pem"
    printf 'dummy\n' > "$CERTS_DIR/api/privkey.pem"
    chmod 000 "$CERTS_DIR/api/privkey.pem"
    DOCTOR_STAGE="prestart"
    DOCTOR_MODE="internal"

    run check_certs_env_sync

    [[ "$output" == *'DOCTOR certs-env-sync: skip'* ]]
    [[ "$output" == *'privkey.pem not readable unprivileged'* ]]
    [[ "$output" == *'sudo ./voipbin doctor'* ]]
    [[ "$output" != *'openssl base64 failed'* ]]
}

@test "load_doctor_functions: detect_stage distinguishes preinstall, prestart and running" {
    load_doctor_functions
    stub_docker_doctor healthy
    DOCTOR_DOCKER_OK=true
    DOCTOR_PROFILES="internal-dns"

    rm -f "$ENV_FILE"
    [[ "$(detect_stage)" == "preinstall" ]]

    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    [[ "$(detect_stage)" == "running" ]]

    stub_docker_doctor stack-down
    [[ "$(detect_stage)" == "prestart" ]]

    # docker unreachable: stage degrades to prestart, never errors
    DOCTOR_DOCKER_OK=false
    [[ "$(detect_stage)" == "prestart" ]]
}

@test "schema present without the test-data marker warns without a FIX" {
    setup_healthy_internal
    rm -f "$PROJECT_DIR/.test_data_initialized"

    run_doctor

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'DOCTOR test-data: warn'* ]]
    ! grep -q '^FIX test-data:' <<< "$output"
}
