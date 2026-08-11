#!/usr/bin/env bats
# Tests for scripts/migrate.sh's dbscheme directory / alembic.ini permission
# hardening.
#
# alembic.ini (written by write_alembic_ini) embeds the real MySQL root
# password, so its containing directory (DBSCHEME_DIR) must be chmod 700
# and the file itself chmod 600 - on BOTH the fresh-fetch path and the
# cached ("already at pinned commit") path of fetch_dbscheme(). A prior
# review round found the cached path skipped the chmod 700 entirely; these
# tests are structured to catch that regression directly.
#
# Isolation rule: git is stubbed via MOCK_BIN_DIR and PROJECT_DIR/DBSCHEME_DIR
# point into TEST_TEMP_DIR — nothing touches the real repo, network, or a
# real monorepo checkout.

load 'test_helper'

setup() {
    setup_test_env
    DBSCHEME_DIR="$PROJECT_DIR/tmp/bin-dbscheme-manager"
    DB_USER="root"
    DB_PASSWORD="test-root-password"
    DBSCHEME_COMMIT="abc123def456"
    WORKDIR=""
}

teardown() {
    teardown_test_env
}

# Stubs `git` well enough to drive fetch_dbscheme()'s fresh-fetch (sparse
# clone) branch without any real network access: `clone` creates the
# destination directory, `checkout` (run after cd'ing into it) materializes
# the bin-dbscheme-manager subdirectory that the real script then `mv`s into
# place.
stub_git_for_fresh_fetch() {
    mock_command_script "git" '
case "$1" in
    clone)
        dest="${@: -1}"
        mkdir -p "$dest"
        exit 0
        ;;
    sparse-checkout|fetch)
        exit 0
        ;;
    checkout)
        mkdir -p "bin-dbscheme-manager"
        echo "placeholder" > "bin-dbscheme-manager/env.py"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
'
}

@test "fetch_dbscheme (fresh fetch) creates DBSCHEME_DIR at mode 700" {
    load_migrate_functions
    stub_git_for_fresh_fetch
    mkdir -p "$PROJECT_DIR/tmp"

    run fetch_dbscheme

    [[ "$status" -eq 0 ]]
    [[ -d "$DBSCHEME_DIR" ]]
    local mode
    mode="$(stat -c '%a' "$DBSCHEME_DIR")"
    [[ "$mode" == "700" ]]
    [[ "$(cat "$DBSCHEME_DIR/.pin")" == "$DBSCHEME_COMMIT" ]]
}

@test "fetch_dbscheme (cached path) still tightens DBSCHEME_DIR to mode 700" {
    load_migrate_functions
    # Simulate a directory left over from BEFORE the chmod 700 hardening
    # landed (e.g. an install that pre-dates this PR, or any world-readable
    # leftover) - the .pin file already matches, so this hits the early
    # "cached" return.
    mkdir -p "$DBSCHEME_DIR"
    echo "$DBSCHEME_COMMIT" > "$DBSCHEME_DIR/.pin"
    chmod 755 "$DBSCHEME_DIR"

    run fetch_dbscheme

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"cached"* ]]
    local mode
    mode="$(stat -c '%a' "$DBSCHEME_DIR")"
    [[ "$mode" == "700" ]]
}

@test "write_alembic_ini writes alembic.ini at mode 600 (fresh-fetch path)" {
    load_migrate_functions
    stub_git_for_fresh_fetch
    mkdir -p "$PROJECT_DIR/tmp"
    fetch_dbscheme
    mkdir -p "$DBSCHEME_DIR/bin-manager"

    write_alembic_ini "bin-manager" "bin_manager" "main"

    local ini="$DBSCHEME_DIR/bin-manager/alembic.ini"
    [[ -f "$ini" ]]
    local mode
    mode="$(stat -c '%a' "$ini")"
    [[ "$mode" == "600" ]]
    grep -q "sqlalchemy.url = mysql+pymysql://$DB_USER:$DB_PASSWORD@db:3306/bin_manager" "$ini"
}

@test "write_alembic_ini writes alembic.ini at mode 600 (cached-fetch path)" {
    load_migrate_functions
    mkdir -p "$DBSCHEME_DIR/asterisk_config"
    echo "$DBSCHEME_COMMIT" > "$DBSCHEME_DIR/.pin"
    chmod 755 "$DBSCHEME_DIR"
    fetch_dbscheme

    write_alembic_ini "asterisk_config" "asterisk" "config"

    local ini="$DBSCHEME_DIR/asterisk_config/alembic.ini"
    [[ -f "$ini" ]]
    local mode
    mode="$(stat -c '%a' "$ini")"
    [[ "$mode" == "600" ]]
    # The cached fetch_dbscheme path must ALSO leave DBSCHEME_DIR at 700 -
    # this is the exact gap the round-2 review found.
    local dir_mode
    dir_mode="$(stat -c '%a' "$DBSCHEME_DIR")"
    [[ "$dir_mode" == "700" ]]
}

# --- VOIP-1328 wire-up: migrate.sh calls the shared
# provision_asterisk_db_user() from common.sh. The function's own behavior
# (no-op cases, .env parsing/quote-stripping, validation, idempotency,
# stdin-vs-argv secret handling) is tested exhaustively once, directly
# against common.sh, in common.bats — not re-tested here to avoid drift
# between two copies of the same assertions. ---

@test "migrate.sh calls provision_asterisk_db_user with DB_CONTAINER after CREATE DATABASE" {
    run grep -n 'provision_asterisk_db_user "\$DB_CONTAINER"' "$SCRIPTS_DIR/migrate.sh"

    [[ "$status" -eq 0 ]]
    local call_line create_db_line
    call_line="$(echo "$output" | head -1 | cut -d: -f1)"
    create_db_line="$(grep -n "CREATE DATABASE IF NOT EXISTS bin_manager" "$SCRIPTS_DIR/migrate.sh" | head -1 | cut -d: -f1)"
    [[ "$call_line" -gt "$create_db_line" ]]
}

@test "migrate.sh sources common.sh (where provision_asterisk_db_user/get_env_var live)" {
    run grep -q 'source "\$SCRIPT_DIR/common.sh"' "$SCRIPTS_DIR/migrate.sh"

    [[ "$status" -eq 0 ]]
}
