#!/usr/bin/env bats
# Tests for scripts/start.sh (dual-mode DNS, VOIP-1275 Phase 3)
# check_host_prereqs branch matrix and the unprivileged fail-fast/proceed
# behavior that replaces check_root.
#
# Isolation rule: ip/dig/docker are stubbed via MOCK_BIN_DIR and
# PROJECT_DIR=$TEST_TEMP_DIR — nothing may touch the real host or daemon.
# The root×missing cell of the matrix (setup-host.sh subprocess invocation)
# cannot be exercised without real root operations; it is covered by the
# live internal-mode verification (plan DoD item 5).

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# check_host_prereqs matrix (design §2.5)
# =============================================================================

@test "check_host_prereqs passes with interfaces present in external mode (no DNS requirement)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    mock_command_script "ip" 'exit 0'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 0 ]]
}

@test "check_host_prereqs passes with interfaces present and resolv.conf at 127.0.0.1 (internal)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'exit 0'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 127.0.0.1" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 0 ]]
}

@test "check_host_prereqs fails in internal mode when DNS is not configured" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'exit 0'
    mock_command_script "dig" 'exit 1'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 1 ]]
}

@test "check_host_prereqs does not accept a commented-out nameserver line (internal)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'exit 0'
    mock_command_script "dig" 'exit 1'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    printf '#nameserver 127.0.0.1\nnameserver 8.8.8.8\n' > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 1 ]]
}

@test "check_host_prereqs accepts an answering CoreDNS when resolv.conf is untouched (internal)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'exit 0'
    mock_command_script "dig" 'echo "192.168.1.100"'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 0 ]]
}

@test "check_host_prereqs fails when both VoIP interfaces are missing" {
    load_start_functions
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    mock_command_script "ip" 'exit 1'

    run check_host_prereqs

    [[ "$status" -eq 1 ]]
}

@test "check_host_prereqs fails when only one VoIP interface exists (both are required)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    mock_command_script "ip" '
if [[ "$1" == "link" && "$2" == "show" && "$3" == "kamailio-int" ]]; then exit 0; fi
exit 1'

    run check_host_prereqs

    [[ "$status" -eq 1 ]]
}

@test "check_host_prereqs sets HOST_PREREQS_MISSING with the missing prerequisite" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'exit 1'

    check_host_prereqs || true

    [[ "$HOST_PREREQS_MISSING" == *'kamailio-int/rtpengine-int'* ]]
}

# =============================================================================
# ensure_scheduled_backup_enabled (VOIP-1281): database-backup ships disabled
# upstream; start.sh turns it on idempotently.
# =============================================================================

@test "ensure_scheduled_backup_enabled is a no-op when already enabled" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$*" == *"schedule list"* ]]; then
    printf "%s" "[{\"name\":\"database-backup\",\"enabled\":true}]"
    exit 0
fi
if [[ "$1" == "exec" && "$*" == *"schedule enable"* ]]; then
    echo "SHOULD NOT BE CALLED" >> "'"$TEST_TEMP_DIR"'/enable_calls.log"
    exit 0
fi
exit 1'

    run ensure_scheduled_backup_enabled

    [[ ! -f "$TEST_TEMP_DIR/enable_calls.log" ]]
}

@test "ensure_scheduled_backup_enabled enables a disabled schedule" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$*" == *"schedule list"* ]]; then
    printf "%s" "[{\"name\":\"database-backup\",\"enabled\":false}]"
    exit 0
fi
if [[ "$1" == "exec" && "$*" == *"schedule enable database-backup"* ]]; then
    exit 0
fi
exit 1'

    run ensure_scheduled_backup_enabled

    [[ "$output" == *'Enabled scheduled DB backup'* ]]
}

@test "ensure_scheduled_backup_enabled warns when schedule-manager is unreachable" {
    load_start_functions
    mock_command_script "docker" 'exit 1'

    run ensure_scheduled_backup_enabled

    [[ "$output" == *'Could not reach schedule-manager'* ]]
    [[ "$output" == *'schedule-control schedule enable database-backup'* ]]
}

@test "ensure_scheduled_backup_enabled warns when the enable command itself fails" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$*" == *"schedule list"* ]]; then
    printf "%s" "[{\"name\":\"database-backup\",\"enabled\":false}]"
    exit 0
fi
exit 1'

    run ensure_scheduled_backup_enabled

    [[ "$output" == *'Could not enable database-backup'* ]]
}

# =============================================================================
# Script-level behavior: the gate that replaced check_root (design §2.5)
# =============================================================================

@test "start.sh unprivileged with missing prereqs fails fast with next=setup-host" {
    if [[ $EUID -eq 0 ]]; then
        skip "test requires an unprivileged user"
    fi
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test"
    mock_command "docker" ""
    mock_command_script "ip" 'exit 1'
    mock_command_script "dig" 'exit 1'
    export RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"

    run env -u COMPOSE_PROFILES bash "$SCRIPTS_DIR/start.sh"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'VOIPBIN_START: status=error reason="host setup missing" next="sudo ./scripts/setup-host.sh"'* ]]
}

@test "start.sh unprivileged with satisfied prereqs proceeds past the gate" {
    if [[ $EUID -eq 0 ]]; then
        skip "test requires an unprivileged user"
    fi
    # byo mode with the cert missing: the run must get PAST the host-prereq
    # gate and die later at setup_mkcert's byo fail-fast — proving the
    # unprivileged path proceeds when prerequisites are satisfied.
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" \
        "BASE_DOMAIN=voipbin.test" "TLS_MODE=byo" "HOST_EXTERNAL_IP=192.168.1.100"
    mock_command "docker" ""
    mock_command_script "ip" 'exit 0'
    export RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 127.0.0.1" > "$RESOLV_CONF"

    run env -u COMPOSE_PROFILES bash "$SCRIPTS_DIR/start.sh"

    [[ "$status" -ne 0 ]]
    [[ "$output" != *'host setup missing'* ]]
    [[ "$output" == *'TLS_MODE=byo but'* ]]
    [[ "$output" == *'install-certs.sh'* ]]
    # Result-line guarantee (§2.2): even a set -e abort closes with a line
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_START:\ status=error ]]
}

# =============================================================================
# check_database_initialized (VOIP-1289): a partial migration must not be
# mistaken for "done" — bin_manager alone having tables is not sufficient,
# both schemas must be at some alembic revision.
# =============================================================================

@test "check_database_initialized fails when only bin_manager has tables (asterisk never migrated)" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$*" == *"bin_manager.alembic_version"* ]]; then
    echo "a5e6f559299c"
elif [[ "$*" == *"asterisk.alembic_version"* ]]; then
    exit 1
elif [[ "$*" == *"information_schema.TABLES"* ]]; then
    echo "42"
fi
'

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

@test "check_database_initialized passes when both schemas have an alembic version" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$*" == *"bin_manager.alembic_version"* ]]; then
    echo "a5e6f559299c"
elif [[ "$*" == *"asterisk.alembic_version"* ]]; then
    echo "c07b40884361"
elif [[ "$*" == *"information_schema.TABLES"* ]]; then
    echo "42"
fi
'

    run check_database_initialized

    [[ "$status" -eq 0 ]]
}

@test "check_database_initialized fails on a completely fresh (empty) database" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$*" == *"information_schema.TABLES"* ]]; then
    echo "0"
fi
'

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

# =============================================================================
# setup_test_customer marker gating (VOIP-1289): the .test_data_initialized
# marker must only be written when all 3 extensions actually got created,
# so a broken run doesn't defeat the "just re-run start.sh" recovery path.
# =============================================================================

# Shared docker stub covering every `docker exec` call setup_test_customer
# makes, other than extension creation (that's over curl, not docker).
stub_docker_for_test_customer() {
    mock_command_script "docker" '
case "$*" in
    *"customer-control customer create"*) exit 0 ;;
    *"customer-control customer list"*) echo "[{\"email\":\"admin@localhost\",\"id\":\"cust-1\"}]" ;;
    *"agent-control agent list"*) echo "[{\"id\":\"agent-1\"}]" ;;
    *"agent-control agent update-password"*) exit 0 ;;
    *"customer-control accesskey create"*) echo "token: fake-accesskey" ;;
    *"billing-control account update-plan-type"*) exit 0 ;;
    *"billing-control account add-balance"*) exit 0 ;;
    *) exit 0 ;;
esac
'
}

@test "setup_test_customer writes the marker when all 3 extensions succeed" {
    load_start_functions
    stub_docker_for_test_customer
    mock_command_script "curl" '
case "$*" in
    *"/auth/login"*) echo "{\"token\":\"fake-jwt\"}" ;;
    *"/v1.0/extensions"*)
        if [[ "$*" == *"-w"* ]]; then printf "{}\n201"; else echo "{}"; fi
        ;;
    *"/v1.0/customer"*) echo "{\"billing_account_id\":\"bill-1\"}" ;;
    *) echo "{}" ;;
esac
'

    run setup_test_customer

    [[ "$status" -eq 0 ]]
    [[ -f "$PROJECT_DIR/.test_data_initialized" ]]
    [[ "$output" == *"Test customer created successfully!"* ]]
}

@test "setup_test_customer does not write the marker when an extension creation fails" {
    load_start_functions
    stub_docker_for_test_customer
    mock_command_script "curl" '
case "$*" in
    *"/auth/login"*) echo "{\"token\":\"fake-jwt\"}" ;;
    *"/v1.0/extensions"*)
        if [[ "$*" == *"\"extension\": \"2000\""* ]]; then
            if [[ "$*" == *"-w"* ]]; then printf "{\"error\":\"boom\"}\n500"; else echo "{\"error\":\"boom\"}"; fi
        else
            if [[ "$*" == *"-w"* ]]; then printf "{}\n201"; else echo "{}"; fi
        fi
        ;;
    *"/v1.0/customer"*) echo "{\"billing_account_id\":\"bill-1\"}" ;;
    *) echo "{}" ;;
esac
'

    run setup_test_customer

    [[ ! -f "$PROJECT_DIR/.test_data_initialized" ]]
    [[ "$output" == *"Could not create extension 2000"* ]]
    [[ "$output" == *"only 2/3 extensions succeeded"* ]]
}

# =============================================================================
# main()'s dev-seed gating (Step 12): VOIPBIN_SANDBOX_DEV_SEED defaults to
# false, so a fresh install must NOT call setup_test_customer unless the
# operator opted in. main() itself pulls in the full startup sequence
# (docker compose, wait_for_api, etc.), so instead of mocking all of that we
# extract main()'s actual Step 12 if/elif/else block verbatim from start.sh
# and eval it against the real check_test_data_initialized/dev_seed_enabled
# functions — this fails if the extracted block ever drifts from the real
# gating logic, same as testing main() directly would.
# =============================================================================

# Extracts the literal "# Step 12: Setup test data if needed" block from
# start.sh (up to but excluding "# Step 13: Show status") and defines it as
# a function named run_step12_gate, so tests can invoke the exact production
# control flow without mocking the rest of main().
load_step12_gate() {
    local extracted="$TEST_TEMP_DIR/step12_gate.sh"
    sed -n '/# Step 12: Setup test data if needed/,/# Step 13: Show status/p' "$SCRIPTS_DIR/start.sh" \
        | sed '$d' \
        > "$extracted"

    {
        echo 'run_step12_gate() {'
        cat "$extracted"
        echo '}'
    } > "$extracted.fn"

    source "$extracted.fn"
}

@test "Step 12 gate skips setup_test_customer when VOIPBIN_SANDBOX_DEV_SEED is unset (default)" {
    load_start_functions
    load_step12_gate
    unset VOIPBIN_SANDBOX_DEV_SEED
    rm -f "$PROJECT_DIR/.test_data_initialized"
    setup_test_customer() { echo "SHOULD NOT BE CALLED" >> "$TEST_TEMP_DIR/setup_test_customer_calls.log"; }

    run run_step12_gate

    [[ ! -f "$TEST_TEMP_DIR/setup_test_customer_calls.log" ]]
    [[ "$output" == *"Skipping dev seed data"* ]]
}

@test "Step 12 gate skips setup_test_customer when VOIPBIN_SANDBOX_DEV_SEED=false" {
    load_start_functions
    load_step12_gate
    export VOIPBIN_SANDBOX_DEV_SEED=false
    rm -f "$PROJECT_DIR/.test_data_initialized"
    setup_test_customer() { echo "SHOULD NOT BE CALLED" >> "$TEST_TEMP_DIR/setup_test_customer_calls.log"; }

    run run_step12_gate

    [[ ! -f "$TEST_TEMP_DIR/setup_test_customer_calls.log" ]]
    [[ "$output" == *"Skipping dev seed data"* ]]
}

@test "Step 12 gate calls setup_test_customer when VOIPBIN_SANDBOX_DEV_SEED=true and no marker exists" {
    load_start_functions
    load_step12_gate
    export VOIPBIN_SANDBOX_DEV_SEED=true
    rm -f "$PROJECT_DIR/.test_data_initialized"
    setup_test_customer() { echo "CALLED" >> "$TEST_TEMP_DIR/setup_test_customer_calls.log"; }

    run run_step12_gate

    [[ -f "$TEST_TEMP_DIR/setup_test_customer_calls.log" ]]
    [[ "$output" == *"Creating test customer and extensions..."* ]]
}

# =============================================================================
# main()'s startup-summary dev-seed gating (Step 13): the "Default SIP
# Extensions" block (and the $CUSTOMER_ID-conditional SIP Domain/Server lines
# nested inside it) must not print when VOIPBIN_SANDBOX_DEV_SEED is off, since
# no extensions were created in that case — same bug class as the already-
# gated "Default Admin Account" block just above it. Extracted verbatim from
# start.sh the same way load_step12_gate is, so this fails if the extracted
# block ever drifts from the real gating logic.
# =============================================================================

# Extracts the literal "Default SIP Extensions" if-block from start.sh's
# Step 13 summary (from its "if dev_seed_enabled; then" line down to the
# matching outer "fi") and defines it as a function named
# run_sip_extensions_summary_gate. The "Default SIP Extensions" echo is two
# lines after the relevant "if dev_seed_enabled; then" (the other occurrence
# guards "Default Admin Account" instead), so locating it by line number and
# stepping back two lines pins the correct block even though the guard text
# itself is not unique in the file.
load_sip_extensions_summary_gate() {
    local extracted="$TEST_TEMP_DIR/sip_extensions_summary_gate.sh"
    local marker_line start_line
    marker_line=$(grep -n 'Default SIP Extensions (created on first run)' "$SCRIPTS_DIR/start.sh" | head -1 | cut -d: -f1)
    start_line=$((marker_line - 2))

    sed -n "${start_line},/^    fi\$/p" "$SCRIPTS_DIR/start.sh" > "$extracted"

    {
        echo 'run_sip_extensions_summary_gate() {'
        echo 'local ext_domain="${DOMAIN_NAME_EXTENSION:-registrar.voipbin.test}"'
        cat "$extracted"
        echo '}'
    } > "$extracted.fn"

    source "$extracted.fn"
}

@test "Step 13 summary skips Default SIP Extensions block when VOIPBIN_SANDBOX_DEV_SEED is unset (default)" {
    load_start_functions
    load_sip_extensions_summary_gate
    unset VOIPBIN_SANDBOX_DEV_SEED
    CUSTOMER_ID=""

    run run_sip_extensions_summary_gate

    [[ "$output" != *"Default SIP Extensions"* ]]
    [[ "$output" != *"pass1000"* ]]
    [[ "$output" != *"SIP Domain:"* ]]
}

@test "Step 13 summary skips Default SIP Extensions block when VOIPBIN_SANDBOX_DEV_SEED=false" {
    load_start_functions
    load_sip_extensions_summary_gate
    export VOIPBIN_SANDBOX_DEV_SEED=false
    CUSTOMER_ID="some-leftover-id"

    run run_sip_extensions_summary_gate

    [[ "$output" != *"Default SIP Extensions"* ]]
    [[ "$output" != *"pass1000"* ]]
    [[ "$output" != *"SIP Domain:"* ]]
}

@test "Step 13 summary shows Default SIP Extensions block (with SIP Domain) when VOIPBIN_SANDBOX_DEV_SEED=true" {
    load_start_functions
    load_sip_extensions_summary_gate
    export VOIPBIN_SANDBOX_DEV_SEED=true
    CUSTOMER_ID="cust-123"

    run run_sip_extensions_summary_gate

    [[ "$output" == *"Default SIP Extensions"* ]]
    [[ "$output" == *"1000 / pass1000"* ]]
    [[ "$output" == *"SIP Domain:    cust-123.registrar.voipbin.test"* ]]
}
