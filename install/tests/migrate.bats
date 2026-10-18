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

bats_require_minimum_version 1.5.0

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
    mkdir -p "$DBSCHEME_DIR/asterisk_config"

    write_alembic_ini "asterisk_config" "asterisk" "config"

    local ini="$DBSCHEME_DIR/asterisk_config/alembic.ini"
    [[ -f "$ini" ]]
    local mode
    mode="$(stat -c '%a' "$ini")"
    [[ "$mode" == "600" ]]
    grep -q "sqlalchemy.url = mysql+pymysql://$DB_USER:$DB_PASSWORD@db:3306/asterisk" "$ini"
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

# =============================================================================
# Schema ownership: pin source, stream selection (asterisk only), cache
# ownership. Function-level tests use the extracted helpers; the end-to-end
# tests copy migrate.sh next to a throwaway project directory and run it with
# stubbed docker/sleep/chown so nothing touches the network or a real database.
# =============================================================================

# --- adopt_tmp_owner (root run against a project owned by someone else) ---

stub_root_with_chown_log() {
    CHOWN_LOG="$TEST_TEMP_DIR/chown.log"
    : > "$CHOWN_LOG"
    mock_command_script "id" '
if [[ "$1" == "-u" ]]; then echo "'"${1:-0}"'"; else exec /usr/bin/id "$@"; fi
'
    mock_command_script "chown" 'echo "chown $*" >> "'"$CHOWN_LOG"'"; exit '"${2:-0}"
}

@test "adopt_tmp_owner: does nothing when not running as root" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner"
    load_migrate_functions
    stub_root_with_chown_log 1000
    mkdir -p "$DBSCHEME_DIR"

    run adopt_tmp_owner

    [[ "$status" -eq 0 ]]
    [[ ! -s "$CHOWN_LOG" ]]
}

@test "adopt_tmp_owner: root run re-owns a tmp/ it created and the whole cache" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    load_migrate_functions
    stub_root_with_chown_log 0
    TMP_PREEXISTED=0
    mkdir -p "$DBSCHEME_DIR"
    local uid gid
    uid="$(stat -c %u "$PROJECT_DIR")"; gid="$(stat -c %g "$PROJECT_DIR")"

    run adopt_tmp_owner

    [[ "$status" -eq 0 ]]
    grep -qF -- "chown -h $uid:$gid $PROJECT_DIR/tmp" "$CHOWN_LOG"
    grep -qF -- "chown -hR $uid:$gid $DBSCHEME_DIR" "$CHOWN_LOG"
}

@test "adopt_tmp_owner: leaves a pre-existing tmp/ alone but still re-owns the cache" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    load_migrate_functions
    stub_root_with_chown_log 0
    TMP_PREEXISTED=1
    mkdir -p "$DBSCHEME_DIR"
    local uid gid
    uid="$(stat -c %u "$PROJECT_DIR")"; gid="$(stat -c %g "$PROJECT_DIR")"

    run adopt_tmp_owner

    [[ "$status" -eq 0 ]]
    run ! grep -qF -- "chown -h $uid:$gid $PROJECT_DIR/tmp" "$CHOWN_LOG"
    grep -qF -- "chown -hR $uid:$gid $DBSCHEME_DIR" "$CHOWN_LOG"
}

@test "adopt_tmp_owner: heals a pre-existing tmp/ that an earlier root run left root-owned" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    load_migrate_functions
    stub_root_with_chown_log 0
    TMP_PREEXISTED=1
    mkdir -p "$DBSCHEME_DIR"
    local uid gid real_stat
    uid="$(stat -c %u "$PROJECT_DIR")"; gid="$(stat -c %g "$PROJECT_DIR")"
    real_stat="$(command -v stat)"
    # Report tmp/ as owned by uid 0; everything else is the real answer.
    mock_command_script "stat" '
if [[ "$1" == "-c" && "$2" == "%u" && "$3" == "'"$PROJECT_DIR"'/tmp" ]]; then echo 0; exit 0; fi
exec '"$real_stat"' "$@"
'

    run adopt_tmp_owner

    [[ "$status" -eq 0 ]]
    grep -qF -- "chown -h $uid:$gid $PROJECT_DIR/tmp" "$CHOWN_LOG"
}

@test "adopt_tmp_owner: does nothing when the project itself is owned by root" {
    load_migrate_functions
    stub_root_with_chown_log 0
    mkdir -p "$DBSCHEME_DIR"
    local real_stat
    real_stat="$(command -v stat)"
    mock_command_script "stat" '
if [[ "$1" == "-c" && ( "$2" == "%u" || "$2" == "%g" ) && "$3" == "'"$PROJECT_DIR"'" ]]; then echo 0; exit 0; fi
exec '"$real_stat"' "$@"
'

    run adopt_tmp_owner

    [[ "$status" -eq 0 ]]
    [[ ! -s "$CHOWN_LOG" ]]
}

@test "adopt_tmp_owner: a failing chown never fails the function" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    load_migrate_functions
    stub_root_with_chown_log 0 1
    mkdir -p "$DBSCHEME_DIR"

    run adopt_tmp_owner

    [[ "$status" -eq 0 ]]
    [[ -s "$CHOWN_LOG" ]]
}

@test "fetch_dbscheme: a cache the caller cannot write fails with the chown recovery command" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner"
    load_migrate_functions
    mkdir -p "$DBSCHEME_DIR"
    echo "$DBSCHEME_COMMIT" > "$DBSCHEME_DIR/.pin"
    chmod 500 "$DBSCHEME_DIR"

    run fetch_dbscheme

    chmod 700 "$DBSCHEME_DIR"
    [[ "$status" -eq 1 ]]
    [[ "$output" == *"sudo chown -R"* ]]
    [[ "$output" == *"$PROJECT_DIR/tmp"* ]]
}

# --- end-to-end: the real migrate.sh against a throwaway project dir ---

# Builds $MIG_PROJECT with a copy of migrate.sh + common.sh, a cached dbscheme
# tree at pin "pinDIST" and stubs for docker, sleep and chown.
setup_migrate_project() {
    MIG_PROJECT="$TEST_TEMP_DIR/inst"
    mkdir -p "$MIG_PROJECT/scripts"
    cp "$SCRIPTS_DIR/migrate.sh" "$SCRIPTS_DIR/common.sh" "$MIG_PROJECT/scripts/"
    : > "$MIG_PROJECT/.env"
    DOCKER_LOG="$TEST_TEMP_DIR/docker.log"
    CHOWN_LOG="$TEST_TEMP_DIR/chown.log"
    : > "$DOCKER_LOG"; : > "$CHOWN_LOG"
    rm -f "$TEST_TEMP_DIR/mysql_down" "$TEST_TEMP_DIR/create_fail"

    mock_command_script "sleep" 'exit 0'
    mock_command_script "chown" 'echo "chown $*" >> "'"$CHOWN_LOG"'"; exit 0'
    mock_command_script "docker" '
echo "$*" >> "'"$DOCKER_LOG"'"
case "$1" in
    inspect)
        if [[ "$*" == *"-f"* ]]; then echo "stubnet"; fi
        exit 0 ;;
    exec)
        [[ -f "'"$TEST_TEMP_DIR"'/mysql_down" ]] && exit 1
        case "$*" in
            *"CREATE DATABASE"*)
                if [[ -f "'"$TEST_TEMP_DIR"'/create_fail" ]]; then echo "boom"; exit 1; fi
                exit 0 ;;
        esac
        exit 0 ;;
    run) exit 0 ;;
esac
exit 0
'
}

write_lock() {
    # $1 = file name under MIG_PROJECT, $2 = commit value
    printf '{"dbscheme_monorepo_commit": "%s", "images": {}}\n' "$2" > "$MIG_PROJECT/$1"
}

cache_tree() {
    # $1 = pin
    DBSCHEME_DIR="$MIG_PROJECT/tmp/bin-dbscheme-manager"
    mkdir -p "$DBSCHEME_DIR/asterisk_config" "$DBSCHEME_DIR/bin-manager"
    echo "$1" > "$DBSCHEME_DIR/.pin"
}

run_migrate() {
    run bash "$MIG_PROJECT/scripts/migrate.sh"
}

@test "migrate.sh reads the pin from versions.lock.dist when it exists, even if the live lock differs" {
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    write_lock versions.lock pinLIVE000000
    cache_tree pinDIST000000

    run_migrate

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"versions.lock.dist"* ]]
    [[ "$output" == *"pinDIST000000"* ]]
    # The cache was written for the dist pin; reuse proves that pin was used.
    [[ "$output" == *"already at pinned commit (cached)"* ]]
    [[ "$output" == *"pin=pinDIST00000 pin_source=dist"* ]]
}

@test "migrate.sh falls back to the live versions.lock when there is no versions.lock.dist" {
    setup_migrate_project
    write_lock versions.lock pinLIVE000000
    cache_tree pinLIVE000000

    run_migrate

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"pin_source=live"* ]]
    [[ "$output" == *"pinLIVE000000"* ]]
}

@test "migrate.sh does NOT fall back to the live lock when versions.lock.dist has an empty pin" {
    setup_migrate_project
    write_lock versions.lock.dist ""
    write_lock versions.lock pinLIVE000000
    cache_tree pinLIVE000000

    run_migrate

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"missing/empty"* ]]
    ! grep -q "^run " "$DOCKER_LOG"
}

@test "migrate.sh does NOT fall back to the live lock when versions.lock.dist is broken JSON" {
    setup_migrate_project
    echo '{not json' > "$MIG_PROJECT/versions.lock.dist"
    write_lock versions.lock pinLIVE000000
    cache_tree pinLIVE000000

    run_migrate

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"Failed to parse"* ]]
}

@test "migrate.sh exits 1 when neither lock file exists" {
    setup_migrate_project

    run_migrate

    [[ "$status" -eq 1 ]]
    [[ "$output" == *"versions.lock.dist"* ]]
}

@test "migrate.sh runs only the asterisk stream and never touches bin_manager with alembic" {
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000

    run_migrate

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"DBSCHEME_PIN pin=pinDIST00000 pin_source=dist"* ]]
    [[ "$output" != *"MIGRATE_MODE"* ]]
    [[ "$(grep -c '^run ' "$DOCKER_LOG")" == "1" ]]
    grep -q -- "-w /dbscheme/asterisk_config" "$DOCKER_LOG"
    run ! grep -q -- "-w /dbscheme/bin-manager" "$DOCKER_LOG"
    # alembic.ini exists only for the stream that ran.
    [[ -f "$DBSCHEME_DIR/asterisk_config/alembic.ini" ]]
    [[ ! -e "$DBSCHEME_DIR/bin-manager/alembic.ini" ]]
}

@test "migrate.sh: a bin-manager stream in the pinned tree is ignored, whatever the database holds" {
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000
    : > "$DBSCHEME_DIR/bin-manager/RETIRED.md"

    run_migrate

    [[ "$status" -eq 0 ]]
    [[ "$(grep -c '^run ' "$DOCKER_LOG")" == "1" ]]
    run ! grep -q "alembic_version" "$DOCKER_LOG"
    run ! grep -q -- "-w /dbscheme/bin-manager" "$DOCKER_LOG"
}

@test "migrate.sh: a tree without asterisk_config fails the asterisk stream and exits non-zero" {
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000
    rm -rf "$DBSCHEME_DIR/asterisk_config"

    run_migrate

    [[ "$status" -ne 0 ]]
}

@test "migrate.sh keeps the utf8 / utf8_general_ci database creation statement" {
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000

    run_migrate

    [[ "$status" -eq 0 ]]
    grep -q "CREATE DATABASE IF NOT EXISTS bin_manager CHARACTER SET utf8 COLLATE utf8_general_ci" "$DOCKER_LOG"
    grep -q "CREATE DATABASE IF NOT EXISTS asterisk CHARACTER SET utf8 COLLATE utf8_general_ci" "$DOCKER_LOG"
}

@test "migrate.sh (root run): hands tmp/ and the cache to the project owner after a normal run" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000
    mock_command_script "id" 'if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi'

    run_migrate

    [[ "$status" -eq 0 ]]
    grep -qF -- "chown -hR $(stat -c %u "$MIG_PROJECT"):$(stat -c %g "$MIG_PROJECT") $DBSCHEME_DIR" "$CHOWN_LOG"
    # The re-own happens after alembic.ini is written and before the stream runs.
    [[ "$(grep -c "chown -hR" "$CHOWN_LOG")" -ge 1 ]]
}

@test "migrate.sh (root run): MySQL wait failure still hands the cache back and keeps exit code 1" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000
    touch "$TEST_TEMP_DIR/mysql_down"
    mock_command_script "id" 'if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi'

    run_migrate

    [[ "$status" -eq 1 ]]
    [[ "$output" == *"MySQL not ready"* ]]
    grep -qF -- "chown -hR" "$CHOWN_LOG"
}

@test "migrate.sh (root run): a tmp/ created by this run is handed back even when the fetch step fails" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    # No tmp/ yet: fetch_dbscheme creates it, then the fetch fails (git
    # stubbed to fail, no local monorepo fallback). This is an early abort
    # that must still hand the new tmp/ back to the project owner.
    mock_command_script "git" 'exit 1'
    mock_command_script "id" 'if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi'
    export VOIPBIN_MONOREPO_DIR="$TEST_TEMP_DIR/no-such-monorepo"

    run_migrate

    [[ "$status" -ne 0 ]]
    grep -qF -- "chown -h $(stat -c %u "$MIG_PROJECT"):$(stat -c %g "$MIG_PROJECT") $MIG_PROJECT/tmp" "$CHOWN_LOG"
}

@test "migrate.sh (root run): a tmp/ that existed before the run is not re-owned when the fetch step fails" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    mkdir -p "$MIG_PROJECT/tmp"
    mock_command_script "git" 'exit 1'
    mock_command_script "id" 'if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi'
    export VOIPBIN_MONOREPO_DIR="$TEST_TEMP_DIR/no-such-monorepo"

    run_migrate

    [[ "$status" -ne 0 ]]
    ! grep -qF -- "chown -h $(stat -c %u "$MIG_PROJECT"):$(stat -c %g "$MIG_PROJECT") $MIG_PROJECT/tmp" "$CHOWN_LOG"
}

@test "migrate.sh (root run): the exit code of a CREATE DATABASE failure is preserved by the exit handler" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000
    touch "$TEST_TEMP_DIR/create_fail"
    mock_command_script "id" 'if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi'

    run_migrate

    [[ "$status" -eq 1 ]]
    [[ "$output" == *"CREATE DATABASE failed"* ]]
    grep -qF -- "chown -hR" "$CHOWN_LOG"
}

@test "migrate.sh (root run): exit code is 0 on success even when chown fails" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner (project owner must not be root)"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000
    mock_command_script "id" 'if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi'
    mock_command_script "chown" 'exit 1'

    run_migrate

    [[ "$status" -eq 0 ]]
}

@test "migrate.sh (unprivileged run): never calls chown" {
    [[ "$(id -u)" != "0" ]] || skip "needs an unprivileged runner"
    setup_migrate_project
    write_lock versions.lock.dist pinDIST000000
    cache_tree pinDIST000000

    run_migrate

    [[ "$status" -eq 0 ]]
    [[ ! -s "$CHOWN_LOG" ]]
}

@test "resolve_db_password: environment value wins over .env" {
    load_migrate_functions
    printf 'MYSQL_ROOT_PASSWORD=dotenvvalue\n' > "$PROJECT_DIR/.env"
    MYSQL_ROOT_PASSWORD="envvalue" run resolve_db_password
    [[ "$status" -eq 0 ]]
    [[ "$output" == "envvalue" ]]
}

@test "resolve_db_password: reads .env when the environment has no value" {
    load_migrate_functions
    printf 'MYSQL_ROOT_PASSWORD=dotenvvalue\n' > "$PROJECT_DIR/.env"
    unset MYSQL_ROOT_PASSWORD
    run resolve_db_password
    [[ "$status" -eq 0 ]]
    [[ "$output" == "dotenvvalue" ]]
}

@test "resolve_db_password: keeps the historical default when neither is set" {
    load_migrate_functions
    rm -f "$PROJECT_DIR/.env"
    unset MYSQL_ROOT_PASSWORD
    run resolve_db_password
    [[ "$status" -eq 0 ]]
    [[ "$output" == "root_password" ]]
}
