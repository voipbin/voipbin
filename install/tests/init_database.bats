#!/usr/bin/env bats
# Tests for scripts/init_database.sh's create_databases() and main() (VOIP-1328
# dedicated asterisk-registrar realtime DB user provisioning).
#
# docker is stubbed via mock_command_script so these run with zero real
# containers. The mock records every stdin payload it receives to a file
# so tests can assert on the exact SQL sent, and on whether the password
# ever appears as a literal docker-exec ARGUMENT (which would leak through
# `ps aux` — the whole reason it's piped via stdin instead).
#
# provision_asterisk_db_user() itself (the shared implementation in
# common.sh: no-op cases, quote-stripping, validation, idempotency) is
# tested exhaustively once, directly against common.sh, in common.bats.
# These tests only verify that init_database.sh WIRES IT UP correctly -
# in particular that main() calls it BEFORE the "already initialized,
# re-run? (y/N)" gate (review finding H2, round 2: it previously only ran
# inside create_databases(), which the "N" branch skips entirely, so an
# operator adding the two vars to an EXISTING install's .env and declining
# to re-run migrations got a silently unprovisioned DB user).

bats_require_minimum_version 1.5.0

load 'test_helper'

setup() {
    setup_test_env
    IN_DB_MYSQL='exec mysql -u"$0" -p"${MYSQL_ROOT_PASSWORD:-root_password}"'
}

teardown() {
    teardown_test_env
}

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

@test "create_databases creates bin_manager and asterisk databases" {
    load_init_database_functions
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run create_databases

    [[ "$status" -eq 0 ]]
    grep -q "CREATE DATABASE IF NOT EXISTS bin_manager" "$log_file"
    grep -q "CREATE DATABASE IF NOT EXISTS asterisk" "$log_file"
}

@test "create_databases does not itself provision the DB user (that's main()'s job, see below)" {
    load_init_database_functions
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_capturing_run "$log_file"

    run create_databases

    [[ "$status" -eq 0 ]]
    ! grep -q "CREATE USER" "$log_file"
}

# --- main() (VOIP-1328 review finding H2, round 2) ---
#
# Dispatches on the SQL text embedded in each `docker exec ... sh -c "..."`
# invocation so a single mock can drive main() through wait_for_mysql,
# check_databases_exist, check_migrations_applied, create_databases, and
# provision_asterisk_db_user without a real MySQL. SCRIPT_DIR points at a
# stub migrate.sh so main()'s final delegation step also succeeds.

stub_docker_for_main() {
    local log_file="$1"
    local db_state="$2"  # "fresh" (no databases/migrations yet) or "initialized"

    mkdir -p "$TEST_TEMP_DIR/bin"
    cat > "$TEST_TEMP_DIR/bin/migrate.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
    chmod +x "$TEST_TEMP_DIR/bin/migrate.sh"
    SCRIPT_DIR="$TEST_TEMP_DIR/bin"

    mock_command_script "docker" "
echo \"ARGS: \$*\" >> '$log_file'
if [[ \"\$1\" == \"ps\" ]]; then
    echo 'voipbin-db'
    exit 0
fi
if [[ \"\$1\" == \"exec\" && \"\$2\" == \"-i\" ]]; then
    # Only provision_asterisk_db_user uses -i (stdin-piped SQL, no -e on argv).
    input=\$(cat)
    echo \"\$input\" >> '$log_file'
    echo '' >> '$log_file'
    exit 0
fi
if [[ \"\$1\" == \"exec\" ]]; then
    # Every other call embeds its SQL in argv via -e '...' — already
    # captured by the ARGS line above, no stdin involved.
    case \"\$*\" in
        *'COUNT(*)'*'SCHEMATA'*)
            if [[ '$db_state' == 'initialized' ]]; then echo 2; else echo 0; fi
            exit 0
            ;;
        *'bin_manager.alembic_version'*)
            if [[ '$db_state' == 'initialized' ]]; then echo 'rev-bin-1'; else echo ''; fi
            exit 0
            ;;
        *'COUNT(*)'*'information_schema.TABLES'*)
            if [[ '$db_state' == 'initialized' ]]; then echo 42; else echo 0; fi
            exit 0
            ;;
        *'asterisk.alembic_version'*)
            if [[ '$db_state' == 'initialized' ]]; then echo 'rev-ast-1'; else echo ''; fi
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi
exit 0
"
}

@test "main() provisions the DB user BEFORE the already-initialized gate, even when the operator declines to re-run (H2 round 2 regression)" {
    load_init_database_main_functions
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_for_main "$log_file" "initialized"

    # Simulate the operator answering "N" at the "re-run migrations anyway?
    # (y/N)" prompt: redirect stdin for the whole `run main` command so the
    # already-sourced main() (a shell function, not a subprocess) reads it.
    echo "n" > "$TEST_TEMP_DIR/answer.txt"
    run main < "$TEST_TEMP_DIR/answer.txt"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Skipping migration. Database is ready."* ]]
    # The critical assertion: CREATE USER was issued despite the "N" answer.
    grep -q "CREATE USER IF NOT EXISTS 'asterisk_rt'@'%'" "$log_file"
}

@test "main() provisions the DB user on a fresh install too" {
    load_init_database_main_functions
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=deadbeefcafef00d"
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_for_main "$log_file" "fresh"

    run main

    [[ "$status" -eq 0 ]]
    grep -q "CREATE USER IF NOT EXISTS 'asterisk_rt'@'%'" "$log_file"
    grep -q "CREATE DATABASE IF NOT EXISTS bin_manager" "$log_file"
}

@test "main() exits non-zero and never reaches create_databases when provisioning fails" {
    load_init_database_main_functions
    create_env_file \
        "DATABASE_ASTERISK_USERNAME=asterisk_rt" \
        "DATABASE_ASTERISK_PASSWORD=abc'; DROP TABLE asterisk.ps_aors; --"
    local log_file="$TEST_TEMP_DIR/docker.log"
    stub_docker_for_main "$log_file" "fresh"

    run main

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"DATABASE_ASTERISK_PASSWORD"* ]]
    ! grep -q "CREATE DATABASE" "$log_file"
}

# --- check_migrations_applied: shared rule with start.sh (db_schema_initialized) ---
#
# bin_manager counts as migrated with an alembic row OR a service version
# table; asterisk always needs its alembic row; bin_manager must have tables.

stub_docker_schema_probe() {
    rm -f "$TEST_TEMP_DIR"/{bin_alembic,ast_alembic,svc_tables,bin_tables}
    mock_command_script "docker" '
body="$*"
read_or_empty() { [[ -f "'"$TEST_TEMP_DIR"'/$1" ]] && cat "'"$TEST_TEMP_DIR"'/$1"; return 0; }
case "$body" in
    *"bin_manager.alembic_version"*) read_or_empty bin_alembic ;;
    *"asterisk.alembic_version"*)    read_or_empty ast_alembic ;;
    *"LIKE"*"schema"*)               read_or_empty svc_tables ;;
    *"information_schema.TABLES"*)   read_or_empty bin_tables ;;
esac
exit 0
'
}

@test "check_migrations_applied: service-owned bin_manager (schema tables, no alembic row) counts as applied" {
    load_init_database_main_functions
    stub_docker_schema_probe
    echo 30 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo 5 > "$TEST_TEMP_DIR/svc_tables"

    run check_migrations_applied

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Migrations already applied"* ]]
}

@test "check_migrations_applied: alembic bin_manager plus asterisk counts as applied" {
    load_init_database_main_functions
    stub_docker_schema_probe
    echo 30 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo a5e6f559299c > "$TEST_TEMP_DIR/bin_alembic"

    run check_migrations_applied

    [[ "$status" -eq 0 ]]
}

@test "check_migrations_applied: bin_manager with no alembic row and no service version table is not applied" {
    load_init_database_main_functions
    stub_docker_schema_probe
    echo 30 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo 0 > "$TEST_TEMP_DIR/svc_tables"

    run check_migrations_applied

    [[ "$status" -eq 1 ]]
}

@test "check_migrations_applied: service tables without the asterisk alembic row are not applied" {
    load_init_database_main_functions
    stub_docker_schema_probe
    echo 30 > "$TEST_TEMP_DIR/bin_tables"
    echo 5 > "$TEST_TEMP_DIR/svc_tables"

    run check_migrations_applied

    [[ "$status" -eq 1 ]]
}

@test "check_migrations_applied: an empty bin_manager is not applied even if version rows exist" {
    load_init_database_main_functions
    stub_docker_schema_probe
    echo 0 > "$TEST_TEMP_DIR/bin_tables"
    echo c07b40884361 > "$TEST_TEMP_DIR/ast_alembic"
    echo a5e6f559299c > "$TEST_TEMP_DIR/bin_alembic"

    run check_migrations_applied

    [[ "$status" -eq 1 ]]
}

@test "check_migrations_applied uses the shared db_schema_initialized rule (no duplicate copy of the query)" {
    run grep -c "alembic_version" "$SCRIPTS_DIR/init_database.sh"
    local direct="$output"

    grep -q "db_schema_initialized voipbin-db" "$SCRIPTS_DIR/init_database.sh"
    # start.sh and init_database.sh both delegate to the same common.sh function.
    grep -q "db_schema_initialized voipbin-db" "$SCRIPTS_DIR/start.sh"
    run ! grep -q "SELECT version_num FROM bin_manager.alembic_version" "$SCRIPTS_DIR/init_database.sh"
    run ! grep -q "SELECT version_num FROM bin_manager.alembic_version" "$SCRIPTS_DIR/start.sh"
    [[ -n "$direct" ]]
}
