#!/usr/bin/env bats
# Tests for the shared container health classification (common.sh
# container_issue_kind) and its use in check-install.sh and doctor.sh:
# - "unhealthy" only because the image has no healthcheck binary is a
#   known non-fault,
# - tts-manager restarting because no TTS credentials are configured is an
#   optional feature that is not configured,
# - everything else stays a failure.
#
# Isolation rule: docker/dig/curl are stubbed via MOCK_BIN_DIR.

load 'test_helper'

setup() {
    setup_test_env
    RESOLV_FIXTURE="$TEST_TEMP_DIR/resolv.conf"
    export RESOLV_FIXTURE
}

teardown() {
    teardown_test_env
}

# docker stub for container_issue_kind: `inspect` prints the given last
# health-probe output, `logs` prints the given log text. (Stub bodies are
# written through an unquoted heredoc: keep $, quotes and backslashes out
# of the fixture strings.)
stub_docker_probe() {
    local probe="$1" logs="${2:-}"
    mock_command_script "docker" '
if [[ "$1" == "inspect" ]]; then printf "%s\n" "'"$probe"'"; exit 0; fi
if [[ "$1" == "logs" ]]; then printf "%s\n" "'"$logs"'"; exit 0; fi
exit 1'
}

load_common() {
    source "$SCRIPTS_DIR/common.sh"
}

# =============================================================================
# container_issue_kind
# =============================================================================

@test "container_issue_kind: running and healthy is ok" {
    load_common
    stub_docker_probe ''
    run container_issue_kind voipbin-api-mgr "Up 5 minutes (healthy)"
    [[ "$output" == "ok" ]]
}

@test "container_issue_kind: unhealthy because the healthcheck binary is missing is known" {
    load_common
    stub_docker_probe 'OCI runtime exec failed: exec: wget: executable file not found in PATH'
    run container_issue_kind voipbin-call-mgr "Up 5 minutes (unhealthy)"
    [[ "$output" == "known-healthcheck" ]]
}

@test "container_issue_kind: unhealthy for another reason fails" {
    load_common
    stub_docker_probe 'connection refused'
    run container_issue_kind voipbin-call-mgr "Up 5 minutes (unhealthy)"
    [[ "$output" == "fail" ]]
}

@test "container_issue_kind: only the LAST probe decides" {
    load_common
    mock_command_script "docker" '
if [[ "$1" == "inspect" ]]; then printf "%s\n%s\n" "executable file not found" "connection refused"; exit 0; fi
exit 1'
    run container_issue_kind voipbin-call-mgr "Up 5 minutes (unhealthy)"
    [[ "$output" == "fail" ]]
}

@test "container_issue_kind: tts-manager restarting without TTS credentials is optional" {
    load_common
    stub_docker_probe '' '{"message":"Could not create the tts handler. Check TTS provider credentials (e.g. GOOGLE_APPLICATION_CREDENTIALS)."}'
    run container_issue_kind voipbin-tts-mgr "Restarting (1) 5 seconds ago"
    [[ "$output" == "optional-tts" ]]
}

@test "container_issue_kind: tts-manager restarting for another reason fails" {
    load_common
    stub_docker_probe '' 'panic: something else'
    run container_issue_kind voipbin-tts-mgr "Restarting (1) 5 seconds ago"
    [[ "$output" == "fail" ]]
}

@test "container_issue_kind: another service restarting with the TTS log line still fails" {
    load_common
    stub_docker_probe '' 'Check TTS provider credentials'
    run container_issue_kind voipbin-call-mgr "Restarting (1) 5 seconds ago"
    [[ "$output" == "fail" ]]
}

@test "container_issue_kind: exited fails" {
    load_common
    stub_docker_probe ''
    run container_issue_kind voipbin-call-mgr "Exited (1) 2 minutes ago"
    [[ "$output" == "fail" ]]
}

# =============================================================================
# check-install.sh services check
# =============================================================================

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

# $1: compose ps rows (Name<TAB>Status, \n separated, printf format)
# $2: number of running containers; $3: last health probe; $4: logs
stub_docker_check() {
    local rows="$1" running="$2" probe="$3" logs="$4"
    mock_command_script "docker" '
if [[ "$1" == "compose" && "$2" == "config" ]]; then printf "db\nredis\ncall-manager\ntts-manager\n"; exit 0; fi
if [[ "$1" == "compose" && "$2" == "ps" && "$*" == *"--status running"* ]]; then for i in $(seq 1 '"$running"'); do echo "id$i"; done; exit 0; fi
if [[ "$1" == "compose" && "$2" == "ps" ]]; then printf "'"$rows"'"; exit 0; fi
if [[ "$1" == "inspect" && "$*" == *"State.Health"* ]]; then printf "%s\n" "'"$probe"'"; exit 0; fi
if [[ "$1" == "inspect" ]]; then echo "DOMAIN_NAME_EXTENSION=registrar.voipbin.test"; exit 0; fi
if [[ "$1" == "logs" ]]; then printf "%s\n" "'"$logs"'"; exit 0; fi
exit 1'
    mock_command_script "curl" 'if [[ "$*" == *"-w"* ]]; then echo -n "200"; fi; exit 0'
    mock_command_script "dig" 'for a in "$@"; do last="$a"; done; case "$last" in api.*) echo 192.168.1.100 ;; sip.*) echo 192.168.1.108 ;; esac; exit 0'
    echo "nameserver 127.0.0.1" > "$RESOLV_FIXTURE"
}

run_check() {
    run env -u COMPOSE_PROFILES RESOLV_CONF="$RESOLV_FIXTURE" bash "$SCRIPTS_DIR/check-install.sh"
}

@test "check-install: unhealthy only from a missing healthcheck binary passes services" {
    make_internal_env
    stub_docker_check 'voipbin-db\tUp 5 minutes (healthy)\nvoipbin-redis\tUp 5 minutes\nvoipbin-call-mgr\tUp 5 minutes (unhealthy)\nvoipbin-tts-mgr\tUp 5 minutes\n' 4 'exec: wget: executable file not found in PATH' ''
    run_check
    [[ "$output" == *'CHECK services: pass running=4/4, 1 with no healthcheck binary (known, not a fault)'* ]]
}

@test "check-install: tts-manager restarting without credentials passes and leaves the running count" {
    make_internal_env
    stub_docker_check 'voipbin-db\tUp 5 minutes (healthy)\nvoipbin-redis\tUp 5 minutes\nvoipbin-call-mgr\tUp 5 minutes\nvoipbin-tts-mgr\tRestarting (1) 3 seconds ago\n' 3 '' 'Check TTS provider credentials (e.g. GOOGLE_APPLICATION_CREDENTIALS).'
    run_check
    [[ "$output" == *'CHECK services: pass running=3/3'* ]]
    [[ "$output" == *'optional feature not configured: voipbin-tts-mgr'* ]]
}

@test "check-install: another restarting service still fails services" {
    make_internal_env
    stub_docker_check 'voipbin-db\tUp 5 minutes (healthy)\nvoipbin-redis\tUp 5 minutes\nvoipbin-call-mgr\tRestarting (1) 3 seconds ago\nvoipbin-tts-mgr\tUp 5 minutes\n' 3 '' 'panic: boom'
    run_check
    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK services: fail unhealthy/restarting/exited: voipbin-call-mgr'* ]]
}

@test "check-install: unhealthy for another reason still fails services" {
    make_internal_env
    stub_docker_check 'voipbin-db\tUp 5 minutes (healthy)\nvoipbin-redis\tUp 5 minutes\nvoipbin-call-mgr\tUp 5 minutes (unhealthy)\nvoipbin-tts-mgr\tUp 5 minutes\n' 4 'dial tcp: connection refused' ''
    run_check
    [[ "$status" -eq 1 ]]
    [[ "$output" == *'CHECK services: fail unhealthy/restarting/exited: voipbin-call-mgr'* ]]
}

# =============================================================================
# doctor.sh containers check (only the containers check is asserted; the
# other checks see an otherwise empty, stubbed host)
# =============================================================================

# $1: compose ps -a rows (Name<TAB>State<TAB>Status, printf format)
stub_docker_doctor_containers() {
    local rows="$1" probe="$2" logs="$3"
    mock_command_script "docker" '
if [[ "$1" == "info" ]]; then exit 0; fi
if [[ "$1" == "compose" && "$*" == *"ps -a --format"* ]]; then printf "'"$rows"'"; exit 0; fi
if [[ "$1" == "compose" && "$*" == *"--status running -q"* ]]; then echo cid1; exit 0; fi
if [[ "$1" == "compose" && "$2" == "version" ]]; then echo "2.29.0"; exit 0; fi
if [[ "$1" == "inspect" && "$*" == *"State.Health"* ]]; then printf "%s\n" "'"$probe"'"; exit 0; fi
if [[ "$1" == "logs" ]]; then printf "%s\n" "'"$logs"'"; exit 0; fi
exit 0'
}

run_doctor() {
    run env -u COMPOSE_PROFILES RESOLV_CONF="$RESOLV_FIXTURE" bash "$SCRIPTS_DIR/doctor.sh"
}

@test "doctor: missing healthcheck binary is reported as known, containers passes" {
    make_internal_env
    stub_docker_doctor_containers 'voipbin-db\trunning\tUp 5 minutes (healthy)\nvoipbin-call-mgr\trunning\tUp 5 minutes (unhealthy)\n' 'exec: wget: executable file not found in PATH' ''
    run_doctor
    [[ "$output" == *'DOCTOR containers: pass all 2 containers running; 1 with no healthcheck binary (known, not a fault)'* ]]
    [[ "$output" != *'FIX containers:'* ]]
}

@test "doctor: tts-manager without credentials warns without a FIX line" {
    make_internal_env
    stub_docker_doctor_containers 'voipbin-db\trunning\tUp 5 minutes (healthy)\nvoipbin-tts-mgr\trestarting\tRestarting (1) 2 seconds ago\n' '' 'Check TTS provider credentials'
    run_doctor
    [[ "$output" == *'DOCTOR containers: warn optional feature not configured: voipbin-tts-mgr'* ]]
    [[ "$output" != *'FIX containers:'* ]]
}

@test "doctor: a real failure wins over the optional tts warning" {
    make_internal_env
    stub_docker_doctor_containers 'voipbin-tts-mgr\trestarting\tRestarting (1) 2 seconds ago\nvoipbin-call-mgr\texited\tExited (1) 1 minute ago\n' '' 'Check TTS provider credentials'
    run_doctor
    [[ "$output" == *'DOCTOR containers: fail not running/unhealthy: voipbin-call-mgr(exited)'* ]]
    [[ "$output" == *'FIX containers:'* ]]
}

@test "doctor: unclassifiable unhealthy still fails containers" {
    make_internal_env
    stub_docker_doctor_containers 'voipbin-call-mgr\trunning\tUp 5 minutes (unhealthy)\n' 'connection refused' ''
    run_doctor
    [[ "$output" == *'DOCTOR containers: fail not running/unhealthy: voipbin-call-mgr(unhealthy)'* ]]
}
