#!/bin/bash
# VoIPBin Sandbox - Database Initialization Script
# Creates databases and runs alembic migrations from the monorepo

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DBSCHEME_DIR="$PROJECT_DIR/tmp/bin-dbscheme-manager"

# Database configuration (matches docker-compose.yml)
DB_HOST="127.0.0.1"
DB_PORT="3306"
DB_ROOT_USER="root"
DB_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root_password}"
# The docker-exec'd mysql invocations below do NOT use $DB_ROOT_PASSWORD
# directly on the host's `docker exec` argv (visible to any local user via
# `ps aux`). Instead, IN_DB_MYSQL expands ${MYSQL_ROOT_PASSWORD} INSIDE the
# container via `sh -c`, matching migrate.sh's MYSQL_IN_DB idiom. This also
# means this script does not need MYSQL_ROOT_PASSWORD set in the host shell
# (it isn't, since this script never sources .env) — the container already
# has the var injected via docker-compose.yml. DB_ROOT_PASSWORD above is
# retained only for the legacy configure_alembic()/run_migrations() fallback
# path below (unused by main(), which delegates to migrate.sh), which writes
# a DSN into an alembic.ini config file rather than a process argv.
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

# Check if alembic migrations have been applied
check_migrations_applied() {
    local bin_manager_version
    local asterisk_version

    # Check bin_manager
    bin_manager_version=$(docker exec voipbin-db sh -c "$IN_DB_MYSQL -N -e 'SELECT version_num FROM bin_manager.alembic_version LIMIT 1;'" root 2>/dev/null || echo "")

    # Check asterisk
    asterisk_version=$(docker exec voipbin-db sh -c "$IN_DB_MYSQL -N -e 'SELECT version_num FROM asterisk.alembic_version LIMIT 1;'" root 2>/dev/null || echo "")

    if [ -n "$bin_manager_version" ] && [ -n "$asterisk_version" ]; then
        log_info "Migrations already applied:"
        log_info "  bin_manager: $bin_manager_version"
        log_info "  asterisk: $asterisk_version"
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

    # configure_alembic() (below) writes alembic.ini files carrying the real
    # MySQL root DSN into this directory - keep it out of reach of other
    # local users.
    chmod 700 "$DBSCHEME_DIR"

    log_info "  Database schema files ready at: $DBSCHEME_DIR"
}

# Configure alembic.ini files
configure_alembic() {
    log_step "Configuring alembic..."

    local db_url="mysql://${DB_ROOT_USER}:${DB_ROOT_PASSWORD}@${DB_HOST}:${DB_PORT}"

    # Configure bin-manager alembic
    cat > "$DBSCHEME_DIR/bin-manager/alembic.ini" << EOF
[alembic]
script_location = main
sqlalchemy.url = ${db_url}/bin_manager

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
EOF
    # alembic.ini embeds the real MySQL root DSN - keep it out of reach of
    # other local users (default umask leaves it world-readable).
    chmod 600 "$DBSCHEME_DIR/bin-manager/alembic.ini"
    log_info "  Configured: bin-manager/alembic.ini"

    # Configure asterisk_config alembic
    cat > "$DBSCHEME_DIR/asterisk_config/alembic.ini" << EOF
[alembic]
script_location = config
sqlalchemy.url = ${db_url}/asterisk

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
EOF
    chmod 600 "$DBSCHEME_DIR/asterisk_config/alembic.ini"
    log_info "  Configured: asterisk_config/alembic.ini"
}

# Run alembic migrations
run_migrations() {
    log_step "Running alembic migrations (parallel)..."

    # Check if alembic is installed with a compatible SQLAlchemy version.
    # The monorepo dbscheme migrations require SQLAlchemy 1.x (they use the
    # 1.x-only MetaData(bind=...) API). SQLAlchemy 2.x breaks them, so we must
    # pin "sqlalchemy<2.0" — matching the dbscheme Dockerfile. A bare
    # `command -v alembic` check is NOT enough: alembic may already be present
    # alongside SQLAlchemy 2.x, which passes the check but fails at upgrade head.
    local need_install=false
    if ! command -v alembic &> /dev/null; then
        need_install=true
    elif ! python3 -c 'import sqlalchemy,sys; sys.exit(0 if sqlalchemy.__version__.split(".")[0]=="1" else 1)' 2>/dev/null; then
        log_warn "Incompatible SQLAlchemy detected (need 1.x). Reinstalling pinned versions..."
        need_install=true
    fi

    if [ "$need_install" = true ]; then
        log_warn "Installing alembic with pinned SQLAlchemy<2.0..."
        pip3 install "sqlalchemy<2.0" alembic PyMySQL 2>/dev/null || {
            log_error "Failed to install alembic. Please install it manually:"
            log_error "  pip3 install 'sqlalchemy<2.0' alembic PyMySQL"
            return 1
        }
    fi

    # Create temp files for capturing output and status
    local bin_manager_log=$(mktemp)
    local asterisk_log=$(mktemp)
    local bin_manager_status_file=$(mktemp)
    local asterisk_status_file=$(mktemp)

    # Spinner characters
    local spinner='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    # Run both migrations in parallel
    (cd "$DBSCHEME_DIR/bin-manager" && alembic -c alembic.ini upgrade head > "$bin_manager_log" 2>&1; echo $? > "$bin_manager_status_file") &
    local bin_manager_pid=$!

    (cd "$DBSCHEME_DIR/asterisk_config" && alembic -c alembic.ini upgrade head > "$asterisk_log" 2>&1; echo $? > "$asterisk_status_file") &
    local asterisk_pid=$!

    # Show progress with spinner
    local i=0
    local bin_done=false
    local ast_done=false

    while true; do
        # Check if processes are done
        if ! kill -0 $bin_manager_pid 2>/dev/null; then
            bin_done=true
        fi
        if ! kill -0 $asterisk_pid 2>/dev/null; then
            ast_done=true
        fi

        # Build status line
        local spin_char="${spinner:$((i % ${#spinner})):1}"
        local bin_status="$spin_char running"
        local ast_status="$spin_char running"

        if $bin_done; then
            if [ "$(cat "$bin_manager_status_file" 2>/dev/null)" = "0" ]; then
                bin_status="${GREEN}✓ done${NC}"
            else
                bin_status="${RED}✗ failed${NC}"
            fi
        fi

        if $ast_done; then
            if [ "$(cat "$asterisk_status_file" 2>/dev/null)" = "0" ]; then
                ast_status="${GREEN}✓ done${NC}"
            else
                ast_status="${RED}✗ failed${NC}"
            fi
        fi

        # Print status line (overwrite previous)
        printf "\r  bin-manager: %-12b | asterisk_config: %-12b" "$bin_status" "$ast_status"

        # Exit if both done
        if $bin_done && $ast_done; then
            echo ""
            break
        fi

        sleep 0.1
        i=$((i + 1))
    done

    # Wait for processes (should already be done)
    wait $bin_manager_pid 2>/dev/null
    wait $asterisk_pid 2>/dev/null

    # Get final status
    local bin_manager_status=$(cat "$bin_manager_status_file" 2>/dev/null || echo "1")
    local asterisk_status=$(cat "$asterisk_status_file" 2>/dev/null || echo "1")

    # Show error details if failed
    if [ "$bin_manager_status" != "0" ]; then
        log_error "  bin-manager migration error:"
        cat "$bin_manager_log"
    fi

    if [ "$asterisk_status" != "0" ]; then
        log_error "  asterisk_config migration error:"
        cat "$asterisk_log"
    fi

    # Cleanup temp files
    rm -f "$bin_manager_log" "$asterisk_log" "$bin_manager_status_file" "$asterisk_status_file"

    # Return error if either failed
    if [ "$bin_manager_status" != "0" ] || [ "$asterisk_status" != "0" ]; then
        return 1
    fi

    log_info "  Migrations completed successfully!"
}

# Main function
main() {
    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - Database Initialization"
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

    # Delegate schema migration to the containerized migrate.sh (design §3.3):
    # runs alembic inside python:3.11-slim on the compose network - no host
    # pip/alembic required. The legacy host-alembic functions
    # (setup_dbscheme/configure_alembic/run_migrations) remain above for
    # reference/fallback but are no longer on the default path.
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
