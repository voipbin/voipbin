#!/usr/bin/env bats
# Tests for the Komodo part of scripts/doctor.sh: the komodo-* items fed by one
# `komodo_lib.py health` line, the certificate state files, the adoption note
# of a pre-existing install, and the FIX text that follows the install mode.
#
# Isolation: komodo_lib.py is a fake that prints a prepared KOMODO_HEALTH line
# (VOIPBIN_KOMODO_LIB), systemctl and docker are stubs, PROJECT_DIR is a temp
# directory. Nothing touches a real Komodo, systemd, docker daemon or network.

bats_require_minimum_version 1.5.0

load 'test_helper'

setup() {
    setup_test_env
    export T="$TEST_TEMP_DIR"
    export NO_COLOR=1
    unset COMPOSE_ENV_FILES COMPOSE_PROFILES
    export VOIPBIN_KOMODO_LIB="$T/fake_lib.py"
    cat > "$T/fake_lib.py" <<'PY'
import os, sys
t = os.environ["T"]
with open(t + "/lib.log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "health" and os.path.exists(t + "/health.out"):
    sys.stdout.write(open(t + "/health.out").read())
PY
    mock_command_script systemctl 'if [[ -f "$T/restart_usec" ]]; then cat "$T/restart_usec"; else echo 5s; fi'
    mock_command_script docker 'echo "${COMPOSE_ENV_FILES-unset}|$*" >> "$T/docker.env.log"
if [[ "$1" == "info" ]]; then exit 0; fi
exit 0'
    load_doctor_functions
    DOCTOR_STAGE="prestart"
    DOCTOR_MODE="internal"
    ENV_FILE="$PROJECT_DIR/.env"
}

teardown() {
    chmod -R u+rwx "$T" 2>/dev/null || true
    teardown_test_env
}

mark_adopted() {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/.adopted"
}

install_env() {
    printf '%s\n' "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test" "$@" > "$PROJECT_DIR/.env"
}

# health <fields...>: the health line of the fake, healthy unless overridden
set_health() {
    local core=ok db=ok auth=ok unit=active server=Ok stack=present envfile=ok kv
    for kv in "$@"; do
        case "$kv" in
            core=*) core="${kv#*=}" ;;
            db=*) db="${kv#*=}" ;;
            auth=*) auth="${kv#*=}" ;;
            periphery_unit=*) unit="${kv#*=}" ;;
            server=*) server="${kv#*=}" ;;
            stack=*) stack="${kv#*=}" ;;
            envfile=*) envfile="${kv#*=}" ;;
        esac
    done
    echo "KOMODO_HEALTH: adopted=yes core=$core db=$db auth=$auth periphery_unit=$unit server=$server stack=$stack envfile=$envfile" > "$T/health.out"
}

lib_calls() { [[ -f "$T/lib.log" ]] && cat "$T/lib.log" || true; }

# -----------------------------------------------------------------------------
# stages and modes
# -----------------------------------------------------------------------------

@test "check_komodo before install (no .env): one skip, counters untouched, no Komodo call" {
    DOCTOR_STAGE="preinstall"
    run check_komodo
    [ "$output" = "DOCTOR komodo: skip no .env" ]
    [ ! -e "$T/lib.log" ]
}

@test "check_komodo none: exactly one warn without advice to set Komodo up, no Komodo call" {
    install_env
    check_komodo > "$T/out.txt"
    [ "$(cat "$T/out.txt")" = "DOCTOR komodo-adoption: warn existing installs keep running docker compose directly on the host" ]
    [ "$CHECKS_WARNED" -eq 1 ]
    [ "$CHECKS_FAILED" -eq 0 ]
    [ ! -e "$T/lib.log" ]
    [[ "$(cat "$T/out.txt")" != *'komodo setup'* ]]
    [[ "$(cat "$T/out.txt")" != *FIX* ]]
}

@test "check_komodo broken: a fail with the setup command, the health probe is not run" {
    install_env
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-state: fail' "$T/out.txt"
    grep -q '^FIX komodo-state: sudo ./voipbin komodo setup$' "$T/out.txt"
    [ ! -e "$T/lib.log" ]
    # the certificate state files are still looked at
    grep -q '^DOCTOR certs-pending: pass' "$T/out.txt"
}

# -----------------------------------------------------------------------------
# healthy and failing Komodo
# -----------------------------------------------------------------------------

@test "check_komodo adopted healthy: every item passes" {
    install_env
    mark_adopted
    set_health
    check_komodo > "$T/out.txt"
    local name
    for name in komodo-core komodo-db komodo-auth komodo-periphery komodo-stack komodo-envfile certs-pending certs-sync-pending; do
        grep -q "^DOCTOR $name: pass" "$T/out.txt"
    done
    [ "$CHECKS_FAILED" -eq 0 ]
    [ "$CHECKS_WARNED" -eq 0 ]
    [ "$(lib_calls)" = "health" ]
}

@test "check_komodo: core down fails komodo-core and skips db, auth and stack with the reason" {
    install_env
    mark_adopted
    set_health core=down db=unknown auth=unknown server=unknown stack=unknown
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-core: fail' "$T/out.txt"
    grep -q '^FIX komodo-core: ' "$T/out.txt"
    grep -q '^DOCTOR komodo-db: skip komodo-core is down' "$T/out.txt"
    grep -q '^DOCTOR komodo-auth: skip komodo-core is down' "$T/out.txt"
    grep -q '^DOCTOR komodo-stack: skip' "$T/out.txt"
    # the unit state does not depend on core: it is still judged
    grep -q '^DOCTOR komodo-periphery: pass' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: a database that does not answer fails komodo-db, auth and stack skip" {
    install_env
    mark_adopted
    set_health db=timeout auth=unknown server=unknown stack=present
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-core: pass' "$T/out.txt"
    grep -q '^DOCTOR komodo-db: fail' "$T/out.txt"
    grep -q '^FIX komodo-db: ' "$T/out.txt"
    grep -q '^DOCTOR komodo-auth: skip' "$T/out.txt"
    grep -q '^DOCTOR komodo-stack: skip' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: a rejected API key fails komodo-auth only, the stack is skipped" {
    install_env
    mark_adopted
    set_health auth=rejected server=unknown stack=present
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-db: pass' "$T/out.txt"
    grep -q '^DOCTOR komodo-auth: fail' "$T/out.txt"
    grep -q '^FIX komodo-auth: ' "$T/out.txt"
    grep -q '^DOCTOR komodo-stack: skip' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: a stopped periphery unit fails komodo-periphery with the systemctl advice" {
    install_env
    mark_adopted
    set_health periphery_unit=inactive
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-periphery: fail periphery service is inactive' "$T/out.txt"
    grep -q '^FIX komodo-periphery: sudo systemctl start periphery' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: an active unit that Komodo reports as not ok fails komodo-periphery" {
    install_env
    mark_adopted
    set_health server=NotOk
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-periphery: fail' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: a restart delay under one second warns, one second or more passes" {
    install_env
    mark_adopted
    set_health
    echo "100ms" > "$T/restart_usec"
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-periphery: warn' "$T/out.txt"
    grep -q '^FIX komodo-periphery: sudo ./voipbin komodo setup' "$T/out.txt"
    [ "$CHECKS_WARNED" -eq 1 ]
    [ "$CHECKS_FAILED" -eq 0 ]
    echo "5s" > "$T/restart_usec"
    CHECKS_WARNED=0
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-periphery: pass' "$T/out.txt"
    [ "$CHECKS_WARNED" -eq 0 ]
}

@test "check_komodo: a missing stack, or one that does not read .env.komodo, fails komodo-stack" {
    install_env
    mark_adopted
    set_health stack=missing
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-stack: fail' "$T/out.txt"
    grep -q '^FIX komodo-stack: sudo ./voipbin komodo setup' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: every envfile state but ok fails komodo-envfile" {
    install_env
    mark_adopted
    local state
    for state in missing unreadable bad-owner-or-mode; do
        CHECKS_FAILED=0
        set_health envfile="$state"
        check_komodo > "$T/out.txt"
        grep -q "^DOCTOR komodo-envfile: fail .env.komodo is $state" "$T/out.txt"
        [ "$CHECKS_FAILED" -eq 1 ]
    done
}

@test "check_komodo: a probe that prints nothing is a skip, not a fail" {
    install_env
    mark_adopted
    rm -f "$T/health.out"
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-core: skip' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 0 ]
}

@test "check_komodo: a marker that vanished while checking is a fail" {
    install_env
    mark_adopted
    echo "KOMODO_HEALTH: adopted=no" > "$T/health.out"
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR komodo-state: fail' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

# -----------------------------------------------------------------------------
# certificate state files
# -----------------------------------------------------------------------------

@test "check_komodo: .certs-pending fails with the retry command built from the recorded paths" {
    install_env
    mark_adopted
    set_health
    printf '%s\n%s\n%s\n' "/etc/letsencrypt/live/x/fullchain.pem" "/etc/letsencrypt/live/x/privkey.pem" "2026-01-01T00:00:00Z" > "$PROJECT_DIR/.certs-pending"
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR certs-pending: fail' "$T/out.txt"
    grep -q '^FIX certs-pending: sudo ./scripts/install-certs.sh /etc/letsencrypt/live/x/fullchain.pem /etc/letsencrypt/live/x/privkey.pem$' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

@test "check_komodo: .certs-sync-pending fails with the start command and reads no certificate file" {
    install_env
    mark_adopted
    set_health
    date > "$PROJECT_DIR/.certs-sync-pending"
    check_komodo > "$T/out.txt"
    grep -q '^DOCTOR certs-sync-pending: fail' "$T/out.txt"
    grep -q '^FIX certs-sync-pending: sudo ./voipbin start$' "$T/out.txt"
    [ "$CHECKS_FAILED" -eq 1 ]
}

# -----------------------------------------------------------------------------
# output grammar
# -----------------------------------------------------------------------------

@test "check_komodo output keeps the DOCTOR and FIX line grammar, one FIX per fail or warn" {
    install_env
    mark_adopted
    set_health core=down db=unknown auth=unknown server=unknown stack=unknown periphery_unit=failed envfile=missing
    : > "$PROJECT_DIR/.certs-sync-pending"
    : > "$PROJECT_DIR/.certs-pending"
    check_komodo > "$T/out.txt"
    local line fails=0 fixes=0
    while IFS= read -r line; do
        [[ "$line" =~ ^(DOCTOR\ [a-z-]+:\ (pass|fail|warn|skip)(\ .*)?|FIX\ [a-z-]+:\ .+)$ ]] || { echo "bad line: $line"; false; }
        [[ "$line" == "DOCTOR "*": fail"* ]] && fails=$((fails + 1))
        [[ "$line" == "FIX "* ]] && fixes=$((fixes + 1))
    done < "$T/out.txt"
    [ "$fails" -ge 4 ]
    [ "$fails" -eq "$fixes" ]
}

# -----------------------------------------------------------------------------
# FIX text follows the mode
# -----------------------------------------------------------------------------

certs_fixture() {
    mkdir -p "$CERTS_DIR/api"
    printf 'CERT\n' > "$CERTS_DIR/api/cert.pem"
    printf 'KEY\n' > "$CERTS_DIR/api/privkey.pem"
    printf 'API_SSL_CERT_BASE64=stale\n' > "$PROJECT_DIR/.env.komodo"
}

@test "check_certs_env_sync with Komodo: the internal fix is sudo ./voipbin start" {
    install_env
    mark_adopted
    certs_fixture
    DOCTOR_MODE="internal"
    check_certs_env_sync > "$T/out.txt"
    grep -q '^DOCTOR certs-env-sync: fail' "$T/out.txt"
    grep -q '^FIX certs-env-sync: sudo ./voipbin start' "$T/out.txt"
    [[ "$(cat "$T/out.txt")" != *'init.sh'* ]]
}

@test "check_certs_env_sync with Komodo: the external fix is install-certs.sh then start" {
    install_env
    mark_adopted
    certs_fixture
    DOCTOR_MODE="external"
    check_certs_env_sync > "$T/out.txt"
    grep -q '^FIX certs-env-sync: ./scripts/install-certs.sh <fullchain.pem> <privkey.pem>, then sudo ./voipbin start' "$T/out.txt"
    [[ "$(cat "$T/out.txt")" != *'init.sh'* ]]
}

@test "check_certs_env_sync none: the fix text is unchanged" {
    install_env
    certs_fixture
    rm -f "$PROJECT_DIR/.env.komodo"
    DOCTOR_MODE="internal"
    check_certs_env_sync > "$T/out.txt"
    grep -q '^FIX certs-env-sync: ./scripts/init.sh (rewrites the four SSL vars)$' "$T/out.txt"
}

@test "check_certs_env_sync with Komodo: a copy that matches the files passes (write-through copy is read)" {
    install_env
    mark_adopted
    mkdir -p "$CERTS_DIR/api"
    printf 'CERT\n' > "$CERTS_DIR/api/cert.pem"
    printf 'KEY\n' > "$CERTS_DIR/api/privkey.pem"
    local c k
    c=$(openssl base64 -A -in "$CERTS_DIR/api/cert.pem"); k=$(openssl base64 -A -in "$CERTS_DIR/api/privkey.pem")
    printf '%s\n' "API_SSL_CERT_BASE64=$c" "API_SSL_PRIVKEY_BASE64=$k" "HOOK_SSL_CERT_BASE64=$c" "HOOK_SSL_PRIVKEY_BASE64=$k" > "$PROJECT_DIR/.env.komodo"
    check_certs_env_sync > "$T/out.txt"
    grep -q '^DOCTOR certs-env-sync: pass' "$T/out.txt"
}

@test "container fix text: docker compose without Komodo, the CLI command with Komodo" {
    install_env
    DOCTOR_DOCKER_OK=true
    DOCTOR_STAGE="running"
    DOCTOR_DOMAIN_EXT="registrar.voipbin.test"
    check_realm > "$T/out.txt"
    grep -q '^FIX realm: docker compose up -d registrar-manager$' "$T/out.txt"
    mark_adopted
    check_realm > "$T/out.txt"
    grep -q '^FIX realm: voipbin start registrar-manager$' "$T/out.txt"
}

# -----------------------------------------------------------------------------
# doctor.sh as a script
# -----------------------------------------------------------------------------

@test "doctor.sh adopted: the compose input names both files, a missing copy does not abort the run" {
    install_env
    mark_adopted
    set_health envfile=missing
    run env -u COMPOSE_PROFILES RESOLV_CONF="$T/resolv.conf" bash "$SCRIPTS_DIR/doctor.sh"
    [[ "$output" == *'DOCTOR komodo-envfile: fail'* ]]
    [[ "$output" == *'VOIPBIN_DOCTOR: status=fail'* ]]
    # no copy: nothing is exported, doctor reports instead of stopping
    [ ! -e "$T/docker.env.log" ] || ! grep -q '^\.env,\.env\.komodo|' "$T/docker.env.log"
}

@test "doctor.sh adopted: docker compose runs with both environment files when the copy exists" {
    install_env
    mark_adopted
    printf 'A=1\n' > "$PROJECT_DIR/.env.komodo"
    set_health
    run env -u COMPOSE_PROFILES RESOLV_CONF="$T/resolv.conf" bash "$SCRIPTS_DIR/doctor.sh"
    grep -q '^\.env,\.env\.komodo|compose ' "$T/docker.env.log"
    [[ "$output" == *'DOCTOR komodo-core: pass'* ]]
}

@test "doctor.sh none: the compose calls carry no environment file and no Komodo line but the adoption note" {
    install_env
    run env -u COMPOSE_PROFILES RESOLV_CONF="$T/resolv.conf" bash "$SCRIPTS_DIR/doctor.sh"
    [[ "$output" == *'DOCTOR komodo-adoption: warn'* ]]
    [[ "$output" != *'DOCTOR komodo-core'* ]]
    [ ! -e "$T/lib.log" ]
    if [[ -f "$T/docker.env.log" ]]; then
        if grep -q '^\.env,\.env\.komodo|' "$T/docker.env.log"; then false; fi
    fi
}

@test "doctor.sh before install: the komodo items are one skip" {
    run env -u COMPOSE_PROFILES RESOLV_CONF="$T/resolv.conf" bash "$SCRIPTS_DIR/doctor.sh"
    [[ "$output" == *'DOCTOR komodo: skip no .env'* ]]
    [ ! -e "$T/lib.log" ]
}

@test "doctor.sh source: the doctor never calls a state changing compose command" {
    if grep -nE '^[[:space:]]*[^#[:space:]].*docker compose (up|rm|down|restart|stop|start|run|create|pull)\b' "$SCRIPTS_DIR/doctor.sh" | grep -v '"[^"]*docker compose [a-z]' ; then
        false
    fi
    if grep -nE 'compose_exec|host_env_write|sync_cert_vars' "$SCRIPTS_DIR/doctor.sh"; then
        false
    fi
}
