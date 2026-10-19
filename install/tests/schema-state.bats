#!/usr/bin/env bats
# Tests for the service-owned schema helpers in common.sh
# (schema_table_status, schema_table_applied, schema_*_services) and for
# scripts/check-schema-table-list.sh.
#
# Most tests stub docker. The "real MySQL" tests are opt-in: set
# SCHEMA_TEST_DB_CONTAINER to a running mysql:8.0 container whose root
# password is in its MYSQL_ROOT_PASSWORD environment variable; they create and
# drop their own tables in a bin_manager database and are skipped otherwise.
# The monorepo test needs SCHEMA_TEST_MONOREPO (a checkout directory) and is
# skipped otherwise.

bats_require_minimum_version 1.5.0

load 'test_helper'

setup() {
    setup_test_env
    load_common
}

teardown() {
    teardown_test_env
}

# One status line per listed service; $1 = space separated services to skip
# (no table), $2 = services whose row is dirty, $3 = services with an empty table.
status_text() {
    local skip=" $1 " dirty=" $2 " empty=" $3 " e svc
    for e in "${SERVICE_SCHEMA_TABLES[@]}"; do
        svc="${e#*:}"
        if [[ "$skip" == *" $svc "* ]]; then continue; fi
        if [[ "$dirty" == *" $svc "* ]]; then printf '%s\t1\t1\n' "${e%%:*}"; continue; fi
        if [[ "$empty" == *" $svc "* ]]; then printf '%s\t0\t0\n' "${e%%:*}"; continue; fi
        printf '%s\t1\t0\n' "${e%%:*}"
    done
}

@test "SERVICE_SCHEMA_TABLES has 26 unique entries shaped <name>_schema_migrations:<name>-manager" {
    [[ "${#SERVICE_SCHEMA_TABLES[@]}" -eq 26 ]]
    local e
    for e in "${SERVICE_SCHEMA_TABLES[@]}"; do
        [[ "$e" =~ ^([a-z]+)_schema_migrations:([a-z]+)-manager$ ]]
        [[ "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]]
    done
    [[ "$(printf '%s\n' "${SERVICE_SCHEMA_TABLES[@]}" | sort -u | wc -l)" -eq 26 ]]
}

@test "schema_service_table_in_list quotes every table for an IN (...) list" {
    run schema_service_table_in_list

    [[ "$output" == "'agent_schema_migrations','ai_schema_migrations'"* ]]
    [[ "$output" == *"'webchat_schema_migrations'" ]]
    [[ "$(grep -o "'" <<< "$output" | wc -l)" -eq 52 ]]
}

@test "schema_table_status lists only tables that have a dirty column" {
    mock_command_script "docker" 'echo "$*" >> "'"$TEST_TEMP_DIR"'/args"'

    run schema_table_status voipbin-db

    grep -q "information_schema.COLUMNS" "$TEST_TEMP_DIR/args"
    grep -q "COLUMN_NAME='dirty'" "$TEST_TEMP_DIR/args"
}

@test "real mysql: a *schema_migrations table without a dirty column does not break the status" {
    real_db_setup
    real_db "CREATE TABLE bin_manager.agent_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);
             INSERT INTO bin_manager.agent_schema_migrations VALUES (1, 0);
             CREATE TABLE bin_manager.fooXschema_migrations (a int);" >/dev/null

    run schema_table_status "$SCHEMA_TEST_DB_CONTAINER"
    real_db "DROP DATABASE bin_manager;" >/dev/null

    [[ "$status" -eq 0 ]]
    [[ "$output" == *$'agent_schema_migrations\t1\t0'* ]]
}

@test "README recovery command and the doctor FIX line use the same /app/bin/<name>-control form and README section" {
    local readme="$BATS_TEST_DIRNAME/../README.md" doctor="$BATS_TEST_DIRNAME/../scripts/doctor.sh"
    grep -q '/app/bin/<name>-control migrate force' "$readme"
    grep -q '/app/bin/<name>-control migrate' "$doctor"
    ! grep -q '<service>-control' "$readme"
    grep -q 'Dirty migration state' "$readme"
    grep -q 'Troubleshooting an upgrade' "$readme"
    grep -q 'Troubleshooting an upgrade, Dirty migration state' "$doctor"
}

@test "README pre-merge copies are gitignored and protected from the script update" {
    local f
    for f in .env.pre-merge docker-compose.yml.pre-merge; do
        git -C "$BATS_TEST_DIRNAME/.." check-ignore -q "$f"
        grep -q "\"$f\"" "$BATS_TEST_DIRNAME/../scripts/voipbin-cli.py"
    done
}

@test "schema_table_applied: exactly one row and dirty=0 is applied; absent, empty, dirty, two rows are not" {
    local status_txt
    status_txt="$(status_text "billing-manager" "call-manager" "ai-manager")"

    schema_table_applied agent_schema_migrations "$status_txt"
    run ! schema_table_applied billing_schema_migrations "$status_txt"   # table absent
    run ! schema_table_applied ai_schema_migrations "$status_txt"        # empty table
    run ! schema_table_applied call_schema_migrations "$status_txt"      # dirty
    run ! schema_table_applied campaign_schema_migrations $'campaign_schema_migrations\t2\t0' # two rows
    run ! schema_table_applied agent_schema_migrations ""                # no status at all
}

@test "schema_unapplied_services lists compose service names, comma separated, in list order" {
    local status
    status="$(status_text "billing-manager" "call-manager" "ai-manager")"

    run schema_unapplied_services "$status"

    [[ "$output" == "ai-manager,billing-manager,call-manager" ]]
}

@test "schema_unapplied_services is empty when everything is applied" {
    run schema_unapplied_services "$(status_text "" "" "")"

    [[ "$output" == "" ]]
}

@test "schema_dirty_services names listed services and unlisted dirty tables" {
    local status
    status="$(status_text "" "tag-manager" "")"
    status+=$'\n'"rag_schema_migrations"$'\t'"1"$'\t'"1"

    run schema_dirty_services "$status"

    [[ "$output" == "tag-manager,rag_schema_migrations" ]]
}

@test "schema_dirty_services ignores non-numeric noise" {
    run schema_dirty_services $'42\nfoo\tbar\tbaz'

    [[ "$output" == "" ]]
}

@test "schema_table_status lists existing tables first and SELECTs only those (no failing session on a missing table)" {
    mock_command_script "docker" '
echo "$*" >> "'"$TEST_TEMP_DIR"'/args"
case "$*" in
    *"UNION ALL"*) printf "agent_schema_migrations\t1\t0\ntag_schema_migrations\t1\t1\n" ;;
    *"TABLE_NAME IN"*) printf "agent_schema_migrations\ntag_schema_migrations\n" ;;
esac
exit 0
'

    run schema_table_status voipbin-db

    [[ "$status" -eq 0 ]]
    [[ "$output" == *$'agent_schema_migrations\t1\t0'* ]]
    local select_call
    select_call="$(grep "UNION ALL" "$TEST_TEMP_DIR/args")"
    [[ "$select_call" == *"bin_manager.\`agent_schema_migrations\`"* ]]
    [[ "$select_call" == *"bin_manager.\`tag_schema_migrations\`"* ]]
    [[ "$select_call" != *"billing_schema_migrations"* ]]
    # The listing query carries both the exact-name list and the LIKE filter.
    grep -q "TABLE_NAME IN ('agent_schema_migrations'" "$TEST_TEMP_DIR/args"
    grep -qF "LIKE '%schema\\_migrations'" "$TEST_TEMP_DIR/args"
}

@test "schema_table_status ignores listing rows that are not plain identifiers" {
    mock_command_script "docker" '
echo "$*" >> "'"$TEST_TEMP_DIR"'/args"
case "$*" in
    *"TABLE_NAME IN"*) printf "agent_schema_migrations\nx; DROP TABLE y\n" ;;
esac
exit 0
'

    run schema_table_status voipbin-db

    run ! grep -q "DROP TABLE" <(grep "UNION ALL\|AS t" "$TEST_TEMP_DIR/args")
}

@test "schema_table_status prints nothing and succeeds when no version table exists yet" {
    mock_command_script "docker" 'exit 0'

    run schema_table_status voipbin-db

    [[ "$status" -eq 0 ]]
    [[ -z "$output" ]]
}

@test "schema_table_status fails when either query fails" {
    mock_command "docker" "" 1

    run schema_table_status voipbin-db

    [[ "$status" -eq 1 ]]
}

@test "schema_mysql passes the password through the container env, never the host argv" {
    mock_command_script "docker" 'echo "$*" >> "'"$TEST_TEMP_DIR"'/args"; echo ok'
    export MYSQL_ROOT_PASSWORD="hostvalue"

    run schema_mysql voipbin-db "SELECT 1"

    [[ "$output" == "ok" ]]
    run ! grep -q "hostvalue" "$TEST_TEMP_DIR/args"
    grep -q 'MYSQL_ROOT_PASSWORD:-root_password' "$TEST_TEMP_DIR/args"
}

# --- real mysql:8.0 (opt-in) ---

real_db() { schema_mysql "$SCHEMA_TEST_DB_CONTAINER" "$1"; }

real_db_setup() {
    [[ -n "${SCHEMA_TEST_DB_CONTAINER:-}" ]] || skip "set SCHEMA_TEST_DB_CONTAINER to run against a real mysql:8.0"
    real_db "DROP DATABASE IF EXISTS bin_manager; CREATE DATABASE bin_manager CHARACTER SET utf8 COLLATE utf8_general_ci;" >/dev/null
}

@test "real mysql: LIKE/IN escaping selects exactly the version tables (backslash survives sh -c and docker exec)" {
    real_db_setup
    real_db "CREATE TABLE bin_manager.agent_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);
             CREATE TABLE bin_manager.xschemaXmigrations (id int);
             CREATE TABLE bin_manager.other_table (id int);
             CREATE TABLE bin_manager.zz_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);" >/dev/null

    run real_db "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='bin_manager' AND TABLE_NAME LIKE '%schema\\_migrations' ORDER BY TABLE_NAME;"

    [[ "$output" == $'agent_schema_migrations\nzz_schema_migrations' ]]
    real_db "DROP DATABASE bin_manager;" >/dev/null
}

@test "real mysql: applied means one row and dirty=0; empty, dirty and absent tables are not applied" {
    real_db_setup
    real_db "CREATE TABLE bin_manager.agent_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);
             INSERT INTO bin_manager.agent_schema_migrations VALUES (1, 0);
             CREATE TABLE bin_manager.ai_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);
             CREATE TABLE bin_manager.billing_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);
             INSERT INTO bin_manager.billing_schema_migrations VALUES (1, 1);
             CREATE TABLE bin_manager.rag_schema_migrations (version bigint NOT NULL, dirty tinyint(1) NOT NULL);
             INSERT INTO bin_manager.rag_schema_migrations VALUES (3, 1);" >/dev/null

    local st
    st="$(schema_table_status "$SCHEMA_TEST_DB_CONTAINER")"

    schema_table_applied agent_schema_migrations "$st"
    run ! schema_table_applied ai_schema_migrations "$st"
    run ! schema_table_applied billing_schema_migrations "$st"
    run ! schema_table_applied call_schema_migrations "$st"
    [[ "$(schema_dirty_services "$st")" == "billing-manager,rag_schema_migrations" ]]
    real_db "DROP DATABASE bin_manager;" >/dev/null
}

# --- check-schema-table-list.sh ---

make_fake_monorepo() {
    FAKE_MONO="$TEST_TEMP_DIR/mono"
    local e svc
    for e in "${SERVICE_SCHEMA_TABLES[@]}"; do
        svc="${e#*:}"
        mkdir -p "$FAKE_MONO/bin-$svc/migrations"
        printf 'package migrations\n\nconst VersionTable = "%s"\n' "${e%%:*}" > "$FAKE_MONO/bin-$svc/migrations/embed.go"
    done
}

@test "check-schema-table-list.sh passes when the monorepo agrees with the list" {
    make_fake_monorepo

    run "$SCRIPTS_DIR/check-schema-table-list.sh" "$FAKE_MONO"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"matches the monorepo (26 services)"* ]]
}

@test "check-schema-table-list.sh fails on a renamed version table" {
    make_fake_monorepo
    sed -i 's/tag_schema_migrations/tag_migrations_v2/' "$FAKE_MONO/bin-tag-manager/migrations/embed.go"

    run "$SCRIPTS_DIR/check-schema-table-list.sh" "$FAKE_MONO"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *"tag-manager: list has tag_schema_migrations but VersionTable is tag_migrations_v2"* ]]
}

@test "check-schema-table-list.sh fails when a listed service has no embed.go" {
    make_fake_monorepo
    rm "$FAKE_MONO/bin-tag-manager/migrations/embed.go"

    run "$SCRIPTS_DIR/check-schema-table-list.sh" "$FAKE_MONO"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *"tag-manager: listed with table tag_schema_migrations"* ]]
}

@test "check-schema-table-list.sh fails when the monorepo has a migrating service the list lacks" {
    make_fake_monorepo
    mkdir -p "$FAKE_MONO/bin-newthing-manager/migrations"
    printf 'const VersionTable = "newthing_schema_migrations"\n' > "$FAKE_MONO/bin-newthing-manager/migrations/embed.go"

    run "$SCRIPTS_DIR/check-schema-table-list.sh" "$FAKE_MONO"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *"newthing-manager"* ]]
    [[ "$output" == *"missing from SERVICE_SCHEMA_TABLES"* ]]
}

@test "check-schema-table-list.sh needs exactly one directory argument" {
    run "$SCRIPTS_DIR/check-schema-table-list.sh"
    [[ "$status" -eq 2 ]]

    run "$SCRIPTS_DIR/check-schema-table-list.sh" "$TEST_TEMP_DIR/does-not-exist"
    [[ "$status" -eq 2 ]]
}

@test "check-schema-table-list.sh agrees with a real monorepo checkout (opt-in)" {
    [[ -n "${SCHEMA_TEST_MONOREPO:-}" ]] || skip "set SCHEMA_TEST_MONOREPO to a monorepo checkout to run this"

    run "$SCRIPTS_DIR/check-schema-table-list.sh" "$SCHEMA_TEST_MONOREPO"

    [[ "$status" -eq 0 ]]
}
