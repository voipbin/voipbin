#!/bin/bash
# VoIPBin Install - Database Initialization Script
# Creates databases and delegates schema migration to scripts/migrate.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DBSCHEME_DIR="$PROJECT_DIR/tmp/bin-dbscheme-manager"

# common.sh provides get_env_var() (quote/CRLF/whitespace-stripping parity
# with `docker compose`'s own .env parsing - VOIP-1328 review finding H1)
# and provision_asterisk_db_user() (the single shared implementation also
# used by migrate.sh and start.sh, called from main() below - VOIP-1328
# review findings H1/H2/H3/M2/M4). This script's own log_info/log_warn/
# log_error/log_step definitions further below are unaffected: bash keeps
# the LAST definition of a function, so they simply override common.sh's.
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

# The docker-exec'd mysql invocations below do NOT use the root password
# directly on the host's `docker exec` argv (visible to any local user via
# `ps aux`). Instead, IN_DB_MYSQL expands ${MYSQL_ROOT_PASSWORD} INSIDE the
# container via `sh -c`, matching migrate.sh's MYSQL_IN_DB idiom. This also
# means this script does not need MYSQL_ROOT_PASSWORD set in the host shell
# (it isn't, since this script never sources .env) — the container already
# has the var injected via docker-compose.yml.
IN_DB_MYSQL='exec mysql -u"$0" -p"${MYSQL_ROOT_PASSWORD:-root_password}"'

# Monorepo configuration
MONOREPO_URL="https://github.com/voipbin/monorepo.git"
MONOREPO_BRANCH="main"
# Single source of truth for the dbscheme pin is versions.lock (design §3.3).
# The old hardcoded MONOREPO_PIN here caused three-way manual sync drift
# (versions.lock vs compose vs this script).
MONOREPO_PIN="$(python3 -c "import json;print(json.load(open('$PROJECT_DIR/versions.lock')).get('dbscheme_monorepo_commit',''))" 2>/dev/null || true)"
if [ -z "$MONOREPO_PIN" ]; then
    echo "[ERROR] Could not read dbscheme_monorepo_commit from versions.lock" >&2
    exit 1
fi
DBSCHEME_PATH="bin-dbscheme-manager"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
# NO_COLOR (https://no-color.org): any non-empty value disables color.
if [ -n "${NO_COLOR:-}" ]; then RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''; fi

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# Wait for MySQL to be ready (with actual connection test, not just ping)
wait_for_mysql() {
    local max_attempts=30
    local attempt=1

    log_info "Waiting for MySQL to be ready..."
    while [ $attempt -le $max_attempts ]; do
        # Use actual SELECT query to verify root authentication works
        if docker exec voipbin-db sh -c "$IN_DB_MYSQL -e 'SELECT 1'" root &>/dev/null; then
            log_info "MySQL is ready!"
            return 0
        fi
        echo -n "."
        sleep 2
        attempt=$((attempt + 1))
    done

    echo ""
    log_error "MySQL did not become ready in time"
    return 1
}

# Check if databases already exist
check_databases_exist() {
    local result
    result=$(docker exec voipbin-db sh -c "$IN_DB_MYSQL -N -e \"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ('bin_manager', 'asterisk');\"" root 2>/dev/null)

    if [ "$result" == "2" ]; then
        return 0  # Both databases exist
    fi
    return 1
}

# Check if the schema has been migrated. The rule is shared with start.sh
# (db_schema_initialized in common.sh): bin_manager has tables, the asterisk
# alembic stream has a version, and at least one service has created its
# *schema_migrations table.
check_migrations_applied() {
    if db_schema_initialized voipbin-db; then
        log_info "Migrations already applied (bin_manager and asterisk schemas present)"
        return 0
    fi
    return 1
}

# Create databases
create_databases() {
    log_step "Creating databases..."

    docker exec voipbin-db sh -c "$IN_DB_MYSQL -e 'CREATE DATABASE IF NOT EXISTS bin_manager CHARACTER SET utf8 COLLATE utf8_general_ci;'" root
    log_info "  Created database: bin_manager"

    docker exec voipbin-db sh -c "$IN_DB_MYSQL -e 'CREATE DATABASE IF NOT EXISTS asterisk CHARACTER SET utf8 COLLATE utf8_general_ci;'" root
    log_info "  Created database: asterisk"

    # VOIP-1328: the dedicated realtime DB user is NOT provisioned here.
    # main() already calls provision_asterisk_db_user() unconditionally
    # before the "already initialized" gate (review finding H2, round 2) -
    # duplicating the call here would just double the provisioning log
    # noise on every ordinary run without adding any new code path.
}

# Download or update dbscheme from monorepo
setup_dbscheme() {
    log_step "Setting up database schema files..."

    mkdir -p "$PROJECT_DIR/tmp"

    if [ -d "$DBSCHEME_DIR" ]; then
        log_info "  Updating existing dbscheme directory..."
        cd "$DBSCHEME_DIR"
        git fetch origin "$MONOREPO_PIN" 2>/dev/null && git checkout -q "$MONOREPO_PIN" 2>/dev/null || git checkout -q "$MONOREPO_PIN" 2>/dev/null || true
    else
        log_info "  Cloning dbscheme from monorepo..."
        # Use sparse checkout to only get the dbscheme-manager directory
        cd "$PROJECT_DIR/tmp"
        git clone --filter=blob:none --sparse "$MONOREPO_URL" bin-dbscheme-manager-repo 2>/dev/null && (cd bin-dbscheme-manager-repo && git checkout -q "$MONOREPO_PIN" 2>/dev/null || true) || {
            # Fallback: clone from local monorepo if available
            if [ -d "$HOME/gitvoipbin/monorepo/bin-dbscheme-manager" ]; then
                log_info "  Using local monorepo..."
                cp -r "$HOME/gitvoipbin/monorepo/bin-dbscheme-manager" "$DBSCHEME_DIR"
            else
                log_error "Could not clone monorepo and local copy not found"
                return 1
            fi
        }

        if [ -d "bin-dbscheme-manager-repo" ]; then
            cd bin-dbscheme-manager-repo
            git sparse-checkout set "$DBSCHEME_PATH"
            mv "$DBSCHEME_PATH" "$DBSCHEME_DIR"
            cd ..
            rm -rf bin-dbscheme-manager-repo
        fi
    fi

    # Keep the schema directory private to the owner.
    chmod 700 "$DBSCHEME_DIR"

    log_info "  Database schema files ready at: $DBSCHEME_DIR"
}

# Main function
main() {
    echo ""
    echo "=============================================="
    echo "  VoIPBin Install - Database Initialization"
    echo "=============================================="
    echo ""

    # Check if db container is running
    if ! docker ps --format '{{.Names}}' | grep -q '^voipbin-db$'; then
        log_error "Database container 'voipbin-db' is not running."
        log_info "Start it with: docker compose up -d db"
        exit 1
    fi

    # Wait for MySQL
    wait_for_mysql || exit 1
    echo ""

    # VOIP-1328 review finding H2 (round 2): this MUST run before the
    # "already initialized" gate below, not only inside create_databases().
    # An operator adding DATABASE_ASTERISK_USERNAME/PASSWORD to an EXISTING
    # install's .env and re-running this script (the documented recovery
    # path in CLAUDE.md) answers "N" at the re-run-migrations prompt below
    # (they don't want migrations re-run, just the DB user provisioned) and
    # previously hit `exit 0` before create_databases() - and therefore
    # provision_asterisk_db_user() - ever ran, leaving asterisk-registrar
    # pointed at a MySQL account that was never created. Idempotent and
    # cheap, so running it unconditionally on every invocation is safe.
    provision_asterisk_db_user voipbin-db || exit 1
    echo ""

    # Check if already initialized
    if check_databases_exist && check_migrations_applied; then
        log_info "Database is already initialized!"
        echo ""
        read -p "Do you want to re-run migrations anyway? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_info "Skipping migration. Database is ready."
            exit 0
        fi
    fi

    # Create databases
    create_databases
    echo ""

    # Delegate schema migration to the containerized migrate.sh: it runs the
    # asterisk alembic stream inside python:3.11-slim on the compose network (no
    # host pip/alembic required). The services own the bin_manager schema.
    # setup_dbscheme() above is a legacy helper that main() never calls.
    if ! "$SCRIPT_DIR/migrate.sh"; then
        log_error "Containerized migration failed (scripts/migrate.sh)."
        exit 1
    fi
    echo ""

    echo "=============================================="
    echo "  Database Initialization Complete!"
    echo "=============================================="
    echo ""
    log_info "Databases created:"
    log_info "  - bin_manager (VoIPBin core)"
    log_info "  - asterisk (Asterisk configuration)"
    echo ""
    log_info "You can now start all services:"
    log_info "  docker compose up -d"
    echo ""
}

main "$@"
