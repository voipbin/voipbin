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
#
# `mock_command_script "ip" 'echo "master"; exit 0'` below: check_voip_interfaces
# now delegates to common.sh's voip_internal_interfaces_ok() (VOIP-1331),
# which also checks that the "-br" bridge-side veth peers report a `master`
# (bridge enslavement, not just presence) - echoing it unconditionally keeps
# these "interfaces present"-flavored tests exercising the DNS/prereqs logic
# they're actually about, not an orphaned-peer branch they aren't.

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
    mock_command_script "ip" 'echo "master"; exit 0'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 0 ]]
}

@test "check_host_prereqs fails when WEB_REVERSE_PROXY=true but the Caddyfile is missing (VOIP-1325, review round 1 MED-4)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES=web-proxy" "WEB_REVERSE_PROXY=true"
    mock_command_script "ip" 'echo "master"; exit 0'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"
    # No config/caddy/Caddyfile created — setup-host.sh was skipped/aborted.

    check_host_prereqs || true

    [[ "$HOST_PREREQS_MISSING" == *'config/caddy/Caddyfile is missing'* ]]
}

@test "check_host_prereqs passes when WEB_REVERSE_PROXY=true and the Caddyfile exists" {
    load_start_functions
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES=web-proxy" "WEB_REVERSE_PROXY=true"
    mock_command_script "ip" 'echo "master"; exit 0'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"
    mkdir -p "$TEST_TEMP_DIR/config/caddy"
    touch "$TEST_TEMP_DIR/config/caddy/Caddyfile"

    run check_host_prereqs

    [[ "$status" -eq 0 ]]
}

@test "check_host_prereqs passes with interfaces present and resolv.conf at 127.0.0.1 (internal)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'echo "master"; exit 0'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 127.0.0.1" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 0 ]]
}

@test "check_host_prereqs fails in internal mode when DNS is not configured" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'echo "master"; exit 0'
    mock_command_script "dig" 'exit 1'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 1 ]]
}

@test "check_host_prereqs does not accept a commented-out nameserver line (internal)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'echo "master"; exit 0'
    mock_command_script "dig" 'exit 1'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    printf '#nameserver 127.0.0.1\nnameserver 8.8.8.8\n' > "$RESOLV_CONF"

    run check_host_prereqs

    [[ "$status" -eq 1 ]]
}

@test "check_host_prereqs accepts an answering CoreDNS when resolv.conf is untouched (internal)" {
    load_start_functions
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    mock_command_script "ip" 'echo "master"; exit 0'
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
    mock_command_script "ip" 'echo "master"; exit 0'
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
# mistaken for "done" — bin_manager alone having tables is not sufficient:
# the asterisk stream needs its alembic revision and a service must have
# created its version table.
# =============================================================================

@test "check_database_initialized fails when only bin_manager has tables (asterisk never migrated)" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$*" == *"asterisk.alembic_version"* ]]; then
    exit 1
elif [[ "$*" == *"information_schema.TABLES"* ]]; then
    echo "42"
fi
'

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

@test "check_database_initialized passes when the asterisk stream has a version and services have version tables" {
    load_start_functions
    mock_command_script "docker" '
if [[ "$*" == *"asterisk.alembic_version"* ]]; then
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
# main()'s Step 6.5 (VOIP-1328): provision_asterisk_db_user() must run
# unconditionally, NOT gated on check_database_initialized. Round 1 of the
# VOIP-1328 review found the original version only ran provisioning inside
# migrate.sh, which main() skips entirely once check_database_initialized
# returns true — so an operator adding DATABASE_ASTERISK_USERNAME/PASSWORD
# to an EXISTING install's .env and re-running `start` got a silently
# unprovisioned DB user even though docker-compose.yml had already switched
# the credential. Extracted verbatim from start.sh the same way
# load_step12_gate is (see below), so this fails if Step 6.5 is ever
# accidentally moved inside the `if check_database_initialized` branch.
# =============================================================================

# Extracts the literal "# Step 6.5" block from start.sh (up to but excluding
# "# Mode gate for Steps 7-8") and defines it as a function named
# run_step6_5_gate, so tests can invoke the exact production control flow
# without mocking the rest of main() (docker compose up, wait_for_api, etc.).
load_step6_5_gate() {
    local extracted="$TEST_TEMP_DIR/step6_5_gate.sh"
    sed -n '/# Step 6.5 (VOIP-1328)/,/# Mode gate for Steps 7-8/p' "$SCRIPTS_DIR/start.sh" \
        | sed '$d' \
        > "$extracted"

    {
        echo 'run_step6_5_gate() {'
        cat "$extracted"
        echo '}'
    } > "$extracted.fn"

    # Symmetry with load_init_database_functions/_main_functions: if either
    # marker comment is ever reworded, the sed range would silently extract
    # an empty or wrong block instead of failing loudly.
    if ! grep -q 'provision_asterisk_db_user' "$extracted.fn"; then
        echo "load_step6_5_gate: failed to extract the Step 6.5 block from start.sh (marker comments changed?)" >&2
        return 1
    fi

    source "$extracted.fn"
}

@test "Step 6.5 gate provisions the dedicated DB user even when check_database_initialized is true" {
    load_start_functions
    load_step6_5_gate
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    local log_file="$TEST_TEMP_DIR/docker.log"
    mock_command_script "docker" "
echo \"ARGS: \$*\" >> '$log_file'
if [[ \"\$1\" == \"exec\" && \"\$2\" == \"-i\" ]]; then
    cat >> '$log_file'
    echo '' >> '$log_file'
fi
exit 0
"

    run run_step6_5_gate

    [[ "$status" -eq 0 ]]
    grep -q "CREATE USER IF NOT EXISTS 'asterisk_rt'@'%'" "$log_file"
}

@test "Step 6.5 gate is a cheap no-op when DATABASE_ASTERISK_USERNAME is unset (pre-VOIP-1328 install)" {
    load_start_functions
    load_step6_5_gate
    create_env_file "OTHER_VAR=x"
    local log_file="$TEST_TEMP_DIR/docker.log"
    mock_command_script "docker" "
echo \"ARGS: \$*\" >> '$log_file'
exit 0
"

    run run_step6_5_gate

    [[ "$status" -eq 0 ]]
    [[ ! -f "$log_file" ]]
}

@test "Step 6.5 gate fails main() with a clear error when provisioning fails" {
    load_start_functions
    load_step6_5_gate
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    mock_command_script "docker" '
if [[ "$1" == "exec" && "$2" == "-i" ]]; then
    cat > /dev/null
    exit 1
fi
exit 0
'

    run run_step6_5_gate

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"Failed to provision the dedicated asterisk-registrar DB user"* ]]
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

# =============================================================================
# check_database_initialized: service-owned schema. bin_manager counts as
# migrated when at least one service version table (*schema_migrations)
# exists; asterisk always needs its alembic row.
# =============================================================================

# Docker stub answering each probe from files in $TEST_TEMP_DIR; a missing
# file means "no output" (what mysql prints for a missing table/row).
stub_docker_init_probe() {
    mock_command_script "docker" '
body="$*"
read_or_empty() { [[ -f "'"$TEST_TEMP_DIR"'/$1" ]] && cat "'"$TEST_TEMP_DIR"'/$1"; return 0; }
case "$body" in
    *"asterisk.alembic_version"*)    read_or_empty ast_alembic ;;
    *"LIKE"*"schema"*)               read_or_empty svc_tables ;;
    *"information_schema.TABLES"*)   read_or_empty bin_tables ;;
esac
exit 0
'
    rm -f "$TEST_TEMP_DIR"/{ast_alembic,svc_tables,bin_tables}
}

@test "check_database_initialized passes when the asterisk row and a service version table exist" {
    load_start_functions
    stub_docker_init_probe
    echo 40 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo 12 > "$TEST_TEMP_DIR/svc_tables"

    run check_database_initialized

    [[ "$status" -eq 0 ]]
}

@test "check_database_initialized fails when no service version table exists yet" {
    load_start_functions
    stub_docker_init_probe
    echo 40 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo 0 > "$TEST_TEMP_DIR/svc_tables"

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

@test "check_database_initialized: a bin_manager.alembic_version row does not count as initialized" {
    load_start_functions
    mock_command_script "docker" '
body="$*"
case "$body" in
    *"bin_manager.alembic_version"*) echo a5e6f559299c ;;
    *"asterisk.alembic_version"*)    echo c07b40884361 ;;
    *"LIKE"*"schema"*)               echo 0 ;;
    *"information_schema.TABLES"*)   echo 40 ;;
esac
exit 0
'

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

@test "check_database_initialized fails when service tables exist but the asterisk stream never ran" {
    load_start_functions
    stub_docker_init_probe
    echo 40 > "$TEST_TEMP_DIR/bin_tables"
    echo 12 > "$TEST_TEMP_DIR/svc_tables"

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

@test "check_database_initialized fails when bin_manager has no tables at all, whatever else exists" {
    load_start_functions
    stub_docker_init_probe
    echo 0 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo 12 > "$TEST_TEMP_DIR/svc_tables"

    run check_database_initialized

    [[ "$status" -eq 1 ]]
}

@test "check_database_initialized fails (does not abort) when docker is unreachable, under set -e" {
    load_start_functions
    mock_command "docker" "" 1

    run bash -c 'set -e; source "'"$SCRIPTS_DIR"'/common.sh"; source "'"$TEST_TEMP_DIR"'/start_functions.sh"; if check_database_initialized; then echo yes; else echo no; fi'

    [[ "$output" == *"no"* ]]
}

# =============================================================================
# wait_for_service_schema (schema completion gate). The gate's collaborators
# are replaced by functions driven from variables, and the clock is fake, so
# the tests are instant and deterministic.
# =============================================================================

# Fake clock + sleep: each sleep advances the clock; SLEEPS counts them.
gate_fake_env() {
    FAKE_NOW=1000
    SLEEPS=0
    : > "$TEST_TEMP_DIR/polls"
    schema_gate_now() { echo "$FAKE_NOW"; }
    schema_gate_sleep() { SLEEPS=$((SLEEPS + 1)); FAKE_NOW=$((FAKE_NOW + $1)); }
    GATE_BACKUP=1
    GATE_STATUS_FN=gate_status_all_applied
    # Counted through a file: the status function runs inside $(...).
    schema_table_status() { echo poll >> "$TEST_TEMP_DIR/polls"; "$GATE_STATUS_FN"; }
    schema_gate_backup_listed() { [ "$GATE_BACKUP" = "1" ]; }
    schema_gate_service_missing_key() { [[ ",${GATE_KEYLESS:-}," == *",$1,"* ]]; }
}

# `run` executes in a subshell, so SLEEPS/GATE_POLLS would be lost; gate_run
# runs the gate in the current shell and sets $status / $output like `run`.
gate_run() {
    wait_for_service_schema "$@" > "$TEST_TEMP_DIR/gate.out"
    status=$?
    output="$(cat "$TEST_TEMP_DIR/gate.out")"
}

gate_polls() { wc -l < "$TEST_TEMP_DIR/polls" | tr -d ' '; }

gate_status_all_applied() {
    local e
    for e in "${SERVICE_SCHEMA_TABLES[@]}"; do printf '%s\t1\t0\n' "${e%%:*}"; done
}

# All applied except the services named in GATE_MISSING (comma list).
gate_status_except_missing() {
    local e
    for e in "${SERVICE_SCHEMA_TABLES[@]}"; do
        if [[ ",${GATE_MISSING:-}," == *",${e#*:},"* ]]; then continue; fi
        printf '%s\t1\t0\n' "${e%%:*}"
    done
}

@test "gate: service-owned, all 26 applied and the backup schedule listed => complete after two polls" {
    load_start_functions
    gate_fake_env

    wait_for_service_schema voipbin-db > "$TEST_TEMP_DIR/out.txt"

    grep -q '^SCHEMA_GATE waiting done=26/26 missing=$' "$TEST_TEMP_DIR/out.txt"
    grep -q '^SCHEMA_GATE complete elapsed=10$' "$TEST_TEMP_DIR/out.txt"
    [[ "$SLEEPS" -eq 1 ]]
    [[ "$(gate_polls)" -eq 2 ]]
}

@test "gate: a single all-applied poll is not enough (needs two consecutive)" {
    load_start_functions
    gate_fake_env
    # Poll 1: tag-manager missing. Poll 2: everything applied. Poll 3: still.
    gate_status_flip() {
        if [ "$(gate_polls)" -le 1 ]; then GATE_MISSING="tag-manager" gate_status_except_missing; else gate_status_all_applied; fi
    }
    GATE_STATUS_FN=gate_status_flip

    wait_for_service_schema voipbin-db > "$TEST_TEMP_DIR/out.txt"

    grep -q '^SCHEMA_GATE waiting done=25/26 missing=tag-manager$' "$TEST_TEMP_DIR/out.txt"
    grep -q '^SCHEMA_GATE waiting done=26/26 missing=$' "$TEST_TEMP_DIR/out.txt"
    grep -q '^SCHEMA_GATE complete elapsed=20$' "$TEST_TEMP_DIR/out.txt"
    [[ "$(gate_polls)" -eq 3 ]]
}

@test "gate: times out with a warning naming the missing services, returns 0, and prints elapsed" {
    load_start_functions
    gate_fake_env
    GATE_STATUS_FN=gate_status_except_missing
    GATE_MISSING="tag-manager,billing-manager"
    SCHEMA_GATE_TIMEOUT_SECONDS=30

    gate_run voipbin-db

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"SCHEMA_GATE waiting done=24/26 missing=billing-manager,tag-manager"* ]]
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=30 services=billing-manager,tag-manager"* ]]
}

@test "gate: a version table that is empty, dirty, or has two rows counts as not applied" {
    load_start_functions
    gate_fake_env
    gate_status_bad_states() {
        local e
        for e in "${SERVICE_SCHEMA_TABLES[@]}"; do
            case "${e%%:*}" in
                agent_schema_migrations) ;;                                  # missing table: no row at all
                ai_schema_migrations)       printf '%s\t0\t0\n' "${e%%:*}" ;; # empty
                billing_schema_migrations)  printf '%s\t1\t1\n' "${e%%:*}" ;; # dirty
                call_schema_migrations)     printf '%s\t2\t0\n' "${e%%:*}" ;; # two rows
                *) printf '%s\t1\t0\n' "${e%%:*}" ;;
            esac
        done
    }
    GATE_STATUS_FN=gate_status_bad_states
    SCHEMA_GATE_TIMEOUT_SECONDS=10

    gate_run voipbin-db

    [[ "$output" == *"done=22/26 missing=agent-manager,ai-manager,billing-manager,call-manager"* ]]
}

@test "gate: all applied but the database-backup schedule not listed keeps waiting and says so" {
    load_start_functions
    gate_fake_env
    GATE_BACKUP=0
    SCHEMA_GATE_TIMEOUT_SECONDS=20

    gate_run voipbin-db

    [[ "$output" == *"SCHEMA_GATE waiting done=26/26 missing= schedule=pending"* ]]
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=20"* ]]
}

@test "gate: all services applied but the backup schedule never listed -> incomplete line carries schedule=pending" {
    load_start_functions
    gate_fake_env
    GATE_STATUS_FN=gate_status_all_applied
    GATE_BACKUP=0
    SCHEMA_GATE_TIMEOUT_SECONDS=30

    wait_for_service_schema voipbin-db > "$TEST_TEMP_DIR/out.txt"

    grep -q '^SCHEMA_GATE incomplete elapsed=30 services= schedule=pending$' "$TEST_TEMP_DIR/out.txt"
}

@test "gate: the default limit is 300 seconds" {
    load_start_functions
    gate_fake_env
    GATE_STATUS_FN=gate_status_except_missing
    GATE_MISSING="tag-manager"
    unset SCHEMA_GATE_TIMEOUT_SECONDS

    wait_for_service_schema voipbin-db > "$TEST_TEMP_DIR/out.txt"

    grep -q '^SCHEMA_GATE incomplete elapsed=300 services=tag-manager$' "$TEST_TEMP_DIR/out.txt"
    [[ "$SLEEPS" -eq 30 ]]
}

@test "gate: a zero, negative or non-numeric limit falls back to 300 (it is not an off switch)" {
    load_start_functions
    local bad
    for bad in 0 -5 abc "" 1.5; do
        gate_fake_env
        GATE_STATUS_FN=gate_status_except_missing
        GATE_MISSING="tag-manager"
        SCHEMA_GATE_TIMEOUT_SECONDS="$bad"
        wait_for_service_schema voipbin-db > "$TEST_TEMP_DIR/out.txt"
        grep -q '^SCHEMA_GATE incomplete elapsed=300 services=tag-manager$' "$TEST_TEMP_DIR/out.txt"
    done
}

@test "gate: a status query that fails once and then recovers is re-evaluated on the next poll" {
    load_start_functions
    gate_fake_env
    schema_table_status() { if [ "$SLEEPS" -lt 1 ]; then return 1; fi; gate_status_all_applied; }

    gate_run voipbin-db

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"SCHEMA_GATE waiting state=unknown"* ]]
    [[ "$output" == *"SCHEMA_GATE complete"* ]]
}

@test "gate: an unreadable poll between two readable ones breaks the consecutive-poll requirement" {
    load_start_functions
    gate_fake_env
    # polls 1,3,4 readable and complete; poll 2 unreadable
    schema_table_status() {
        echo poll >> "$TEST_TEMP_DIR/polls"
        if [ "$(gate_polls)" -eq 2 ]; then return 1; fi
        gate_status_all_applied
    }

    gate_run voipbin-db

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"SCHEMA_GATE waiting state=unknown"* ]]
    [[ "$output" == *"SCHEMA_GATE complete elapsed=30"* ]]
}

@test "gate: a failing status query is treated like an unknown state, not as 'all missing'" {
    load_start_functions
    gate_fake_env
    schema_table_status() { return 1; }
    SCHEMA_GATE_TIMEOUT_SECONDS=20

    gate_run voipbin-db

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=20 services=unknown"* ]]
}

@test "gate: ends early when every missing service is crash-looping on a missing key and nothing changed" {
    load_start_functions
    gate_fake_env
    GATE_STATUS_FN=gate_status_except_missing
    GATE_MISSING="tts-manager,rag-manager"
    GATE_MISSING="tts-manager"
    GATE_KEYLESS="tts-manager"

    gate_run voipbin-db

    [[ "$status" -eq 0 ]]
    # First poll cannot be early (no previous poll to compare); the second ends it.
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=10 services=tts-manager"* ]]
    [[ "$SLEEPS" -eq 1 ]]
}

@test "gate: does NOT end early when one missing service has no missing-key evidence" {
    load_start_functions
    gate_fake_env
    GATE_STATUS_FN=gate_status_except_missing
    GATE_MISSING="tts-manager,tag-manager"
    GATE_KEYLESS="tts-manager"
    SCHEMA_GATE_TIMEOUT_SECONDS=60

    gate_run voipbin-db

    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=60 services=tag-manager,tts-manager"* ]]
    [[ "$SLEEPS" -eq 6 ]]
}

@test "gate: does NOT end early while the applied set is still changing between polls" {
    load_start_functions
    gate_fake_env
    GATE_KEYLESS="tts-manager"
    # tag-manager lands at poll 2; tts-manager never applies.
    gate_status_progress() {
        if [ "$(gate_polls)" -le 1 ]; then GATE_MISSING="tts-manager,tag-manager" gate_status_except_missing
        else GATE_MISSING="tts-manager" gate_status_except_missing; fi
    }
    GATE_STATUS_FN=gate_status_progress

    gate_run voipbin-db

    # Poll 2 changed the set (so no early exit there); poll 3 is identical to poll 2.
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=20 services=tts-manager"* ]]
    [[ "$SLEEPS" -eq 2 ]]
}

@test "gate: never ends early when a missing service has no keyless evidence at all" {
    load_start_functions
    gate_fake_env
    GATE_STATUS_FN=gate_status_except_missing
    GATE_MISSING="tag-manager"
    GATE_KEYLESS=""
    SCHEMA_GATE_TIMEOUT_SECONDS=100

    gate_run voipbin-db

    [[ "$SLEEPS" -eq 10 ]]
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=100 services=tag-manager"* ]]
}

@test "gate: the last sleep is shortened so the wait never runs past the limit" {
    load_start_functions
    gate_fake_env
    GATE_MISSING="tag-manager"
    GATE_KEYLESS=""
    GATE_STATUS_FN=gate_status_except_missing
    SCHEMA_GATE_TIMEOUT_SECONDS=15
    local naps=""
    schema_gate_sleep() { naps+="$1 "; SLEEPS=$((SLEEPS + 1)); FAKE_NOW=$((FAKE_NOW + $1)); }

    gate_run voipbin-db

    [[ "$SLEEPS" -eq 2 ]]
    [[ "$naps" == "10 5 " ]]
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=15 services=tag-manager"* ]]
}

@test "gate: callable from a set -e script without aborting it (incomplete and complete)" {
    load_start_functions
    run timeout 20 bash -c '
        set -e
        source "'"$SCRIPTS_DIR"'/common.sh"
        clock="'"$TEST_TEMP_DIR"'/clock"; echo 0 > "$clock"
        schema_gate_now() { cat "$clock"; }
        schema_gate_sleep() { echo $(( $(cat "$clock") + $1 )) > "$clock"; }
        schema_table_status() { return 1; }
        SCHEMA_GATE_TIMEOUT_SECONDS=30
        wait_for_service_schema voipbin-db || echo "gate returned $?"
        schema_table_status() { for e in "${SERVICE_SCHEMA_TABLES[@]}"; do printf "%s\t1\t0\n" "${e%%:*}"; done; }
        schema_gate_backup_listed() { return 0; }
        wait_for_service_schema voipbin-db || echo "gate returned $?"
        echo after-gate
    '

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"SCHEMA_GATE incomplete elapsed=30 services=unknown"* ]]
    [[ "$output" == *"SCHEMA_GATE complete"* ]]
    [[ "$output" == *"after-gate"* ]]
    [[ "$output" != *"gate returned"* ]]
}

# --- early-exit evidence (log classification) ---

@test "log classifier: text format key-missing line with a plain 'migrations applied' line is accepted" {
    load_start_functions
    local logs
    logs=$'time=2026-01-01T00:00:00Z level=info msg="migrations applied" version=3\ntime=2026-01-01T00:00:01Z level=fatal msg="Check TTS provider credentials: api key missing"'

    run schema_gate_log_shows_missing_key "$logs"

    [[ "$status" -eq 0 ]]
}

@test "log classifier: JSON format key-missing line with a 'no pending migrations' line is accepted" {
    load_start_functions
    local logs
    logs=$'{"severity":"INFO","message":"no pending migrations"}\n{"severity":"ERROR","message":"openai token is not set"}'

    run schema_gate_log_shows_missing_key "$logs"

    [[ "$status" -eq 0 ]]
}

@test "log classifier: any migration or connection error line in the window rejects the key-missing evidence (text and JSON)" {
    load_start_functions
    local bad logs
    for bad in \
        'level=error msg="migration failed: dirty database version 4"' \
        '{"severity":"ERROR","message":"could not run migrations: boom"}' \
        'level=error msg="Error 1040: Too many connections"' \
        'level=error msg="dial tcp 127.0.0.1:3306: connection refused"' \
        'level=error msg="could not apply migration 000002"'; do
        logs=$'level=fatal msg="api key missing"\n'"$bad"
        run schema_gate_log_shows_missing_key "$logs"
        [[ "$status" -ne 0 ]]
    done
}

@test "log classifier: logs without a missing-key line are not evidence" {
    load_start_functions

    run schema_gate_log_shows_missing_key $'level=info msg="started"\nlevel=error msg="panic: nil pointer"'

    [[ "$status" -ne 0 ]]
}

@test "missing-key evidence needs a container, 3+ restarts, and a clean key-missing log" {
    load_start_functions
    mock_command_script "docker" '
case "$1" in
    compose) cat "'"$TEST_TEMP_DIR"'/ps_out" 2>/dev/null; exit 0 ;;
    inspect) cat "'"$TEST_TEMP_DIR"'/restarts" 2>/dev/null; exit 0 ;;
    logs)    cat "'"$TEST_TEMP_DIR"'/logs" 2>/dev/null; exit 0 ;;
esac
exit 0
'
    rm -f "$TEST_TEMP_DIR/ps_out" "$TEST_TEMP_DIR/restarts" "$TEST_TEMP_DIR/logs"

    # No container at all (never created).
    run schema_gate_service_missing_key tts-manager
    [[ "$status" -ne 0 ]]

    echo abc123 > "$TEST_TEMP_DIR/ps_out"
    echo 2 > "$TEST_TEMP_DIR/restarts"
    echo 'level=fatal msg="api key missing"' > "$TEST_TEMP_DIR/logs"
    run schema_gate_service_missing_key tts-manager
    [[ "$status" -ne 0 ]]   # only 2 restarts

    echo 3 > "$TEST_TEMP_DIR/restarts"
    run schema_gate_service_missing_key tts-manager
    [[ "$status" -eq 0 ]]

    echo 'level=error msg="Error 1040: Too many connections"' >> "$TEST_TEMP_DIR/logs"
    run schema_gate_service_missing_key tts-manager
    [[ "$status" -ne 0 ]]   # connection-limit crash is not a key problem

    echo 'level=info msg="started"' > "$TEST_TEMP_DIR/logs"
    run schema_gate_service_missing_key tts-manager
    [[ "$status" -ne 0 ]]   # cause unknown
}

@test "container lookup is by compose service (compose ps -a -q), not by container name" {
    load_start_functions
    mock_command_script "docker" 'echo "$*" >> "'"$TEST_TEMP_DIR"'/docker.args"; echo cid1; echo cid2'

    run schema_gate_container_id contact-manager

    [[ "$status" -eq 0 ]]
    [[ "$output" == "cid1" ]]
    grep -qF "compose ps -a -q contact-manager" "$TEST_TEMP_DIR/docker.args"
}

# --- wiring and list integrity ---

@test "start.sh calls the schema gate after 'Waiting for services to stabilize' and before the backup step, never failing the start" {
    local sleep_line gate_line backup_line
    sleep_line="$(grep -n 'Waiting for services to stabilize' "$SCRIPTS_DIR/start.sh" | head -1 | cut -d: -f1)"
    gate_line="$(grep -n 'wait_for_service_schema' "$SCRIPTS_DIR/start.sh" | head -1 | cut -d: -f1)"
    backup_line="$(grep -n '^    ensure_scheduled_backup_enabled' "$SCRIPTS_DIR/start.sh" | head -1 | cut -d: -f1)"

    [[ -n "$sleep_line" && -n "$gate_line" && -n "$backup_line" ]]
    [[ "$gate_line" -gt "$sleep_line" ]]
    [[ "$gate_line" -lt "$backup_line" ]]
    grep -q 'wait_for_service_schema "voipbin-db" || true' "$SCRIPTS_DIR/start.sh"
}

@test "SERVICE_SCHEMA_TABLES matches the compose services that take a database DSN, minus those without migrations" {
    load_common
    run python3 - "$PROJECT_ROOT/docker-compose.yml.dist" "${SERVICE_SCHEMA_TABLES[@]}" <<'PYEOF'
import re, sys
compose, entries = sys.argv[1], sys.argv[2:]
text = open(compose).read()
body = text[re.search(r'^services:\n', text, re.M).end():]
blocks = re.split(r'\n(?=  [a-z][a-z0-9-]*:\n)', body)
dsn_services = set()
for b in blocks:
    name = b.split(':')[0].strip()
    if 'DATABASE_DSN' in b:
        dsn_services.add(name)
# Services that connect to the database but ship no migrations.
no_migrations = {'api-manager', 'hook-manager', 'transfer-manager', 'webhook-manager'}
expected = dsn_services - no_migrations
listed = {e.split(':', 1)[1] for e in entries}
tables = [e.split(':', 1)[0] for e in entries]
errors = []
if listed != expected:
    errors.append(f"service mismatch: only in compose={sorted(expected-listed)} only in list={sorted(listed-expected)}")
if len(entries) != 26 or len(set(tables)) != 26:
    errors.append(f"expected 26 unique tables, got {len(entries)}/{len(set(tables))}")
for e in entries:
    t, s = e.split(':', 1)
    if t != s[:-len('-manager')] + '_schema_migrations':
        errors.append(f"unexpected table name for {s}: {t}")
print('\n'.join(errors))
sys.exit(1 if errors else 0)
PYEOF

    [[ "$status" -eq 0 ]]
}
