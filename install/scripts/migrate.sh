#!/bin/bash
# VoIPBin - Containerized database migration (design §3.3)
#
# Runs alembic 'upgrade head' for BOTH schema streams (bin-manager, asterisk)
# inside a python:3.11-slim container attached to the compose network.
# Replaces the host-pip/alembic dependency entirely.
#
# - Reads the dbscheme monorepo commit from versions.lock (single source of
#   truth; no hardcoded pin).
# - Non-interactive by design: no re-run prompt. Callers (update all /
#   init_database.sh / start.sh) rely on the exit code.
# - Sequential streams, abort on first failure (partial-migration recovery is
#   documented in the design doc failure modes: stop -> voipbin restore).
#
# Exit codes: 0 ok, non-zero failure (callers MUST abort on non-zero).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

MONOREPO_URL="https://github.com/voipbin/monorepo.git"
DBSCHEME_DIR="$PROJECT_DIR/tmp/bin-dbscheme-manager"

DB_CONTAINER="${VOIPBIN_DB_CONTAINER:-voipbin-db}"
DB_USER="root"
DB_PASSWORD="${MYSQL_ROOT_PASSWORD:-root_password}"
# All mysql invocations read the password from the CONTAINER's env
# (MYSQL_ROOT_PASSWORD) instead of putting it on the host argv, where it
# would be visible to every user via 'ps aux'.
MYSQL_IN_DB='exec mysql -u"$0" -p"${MYSQL_ROOT_PASSWORD:-root_password}"'

# Exact pins (design §3.3; version floats rejected in review).
# The FULL dependency tree is pinned and installed with --no-deps so the
# resolver can never float a transitive dep (Mako/MarkupSafe/greenlet/cffi/
# pycparser/typing-extensions are alembic/SQLAlchemy/cryptography deps).
# cryptography is REQUIRED by PyMySQL for MySQL 8's caching_sha2_password
# default auth plugin (found in live T6 testing, not optional).
PIP_PINS="alembic==1.11.3 SQLAlchemy==1.4.52 PyMySQL==1.1.0 cryptography==42.0.8 Mako==1.3.5 MarkupSafe==2.1.5 typing-extensions==4.12.2 greenlet==3.0.3 cffi==1.16.0 pycparser==2.22"
# Digest-pinned (multi-arch index digest) to match the versions.lock philosophy:
# a mutable tag here would let the migration environment drift between runs.
MIGRATE_IMAGE="python:3.11-slim@sha256:b27df5841f3355e9473f9a516d38a6783b6c8dfeacaf2d14a240f443b368ddb6"

log()  { echo -e "\033[0;32m[migrate]\033[0m $1"; }
err()  { echo -e "\033[0;31m[migrate:ERROR]\033[0m $1" >&2; }

# Any temp fetch dir is cleaned up on ANY exit (incl. SIGINT / set -e aborts).
WORKDIR=""
trap '[ -n "$WORKDIR" ] && rm -rf "$WORKDIR"' EXIT

# --- 1. Resolve the dbscheme pin from versions.lock (single source of truth) ---
LOCK_FILE="$PROJECT_DIR/versions.lock"
if [ ! -f "$LOCK_FILE" ]; then
    err "versions.lock not found at $LOCK_FILE — cannot resolve dbscheme pin."
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is required to parse versions.lock (not found in PATH)."
    exit 1
fi

DBSCHEME_COMMIT="$(VB_LOCK_FILE="$LOCK_FILE" python3 -c "import json,os;print(json.load(open(os.environ['VB_LOCK_FILE'])).get('dbscheme_monorepo_commit',''))")" || {
    err "Failed to parse $LOCK_FILE (invalid JSON?)."
    exit 1
}
if [ -z "$DBSCHEME_COMMIT" ]; then
    err "dbscheme_monorepo_commit missing/empty in versions.lock."
    exit 1
fi
log "dbscheme pin (from versions.lock): $DBSCHEME_COMMIT"

# --- 2. Fetch bin-dbscheme-manager at the pinned commit ---
fetch_dbscheme() {
    mkdir -p "$PROJECT_DIR/tmp"

    # Already at the right commit?
    if [ -f "$DBSCHEME_DIR/.pin" ]; then
        if [ "$(cat "$DBSCHEME_DIR/.pin" 2>/dev/null)" = "$DBSCHEME_COMMIT" ]; then
            log "dbscheme already at pinned commit (cached)."
            # alembic.ini (written below) carries the real MySQL root password -
            # keep the cached path's permissions as tight as a fresh fetch's.
            chmod 700 "$DBSCHEME_DIR"
            return 0
        fi
    fi

    rm -rf "$DBSCHEME_DIR"
    WORKDIR="$(mktemp -d "$PROJECT_DIR/tmp/dbscheme-fetch.XXXXXX")"
    local workdir="$WORKDIR"
    local mono_dir="${VOIPBIN_MONOREPO_DIR:-$HOME/gitvoipbin/monorepo}"

    log "Fetching monorepo@${DBSCHEME_COMMIT:0:12} (sparse: bin-dbscheme-manager)..."
    local clone_ok=0
    if git clone --filter=blob:none --sparse --no-checkout "$MONOREPO_URL" "$workdir/repo" 2>"$workdir/clone.err"; then
        if (
            cd "$workdir/repo"
            git sparse-checkout set bin-dbscheme-manager
            git fetch -q origin "$DBSCHEME_COMMIT" 2>/dev/null || true
            git checkout -q "$DBSCHEME_COMMIT"
        ); then
            clone_ok=1
            mv "$workdir/repo/bin-dbscheme-manager" "$DBSCHEME_DIR"
        else
            err "checkout of pinned commit ${DBSCHEME_COMMIT:0:12} failed (unreachable on origin?)."
            log "Trying the local monorepo fallback..."
        fi
    fi
    if [ "$clone_ok" = "0" ] && [ -d "$mono_dir/bin-dbscheme-manager" ]; then
        # Offline fallback: local monorepo checkout (override via VOIPBIN_MONOREPO_DIR).
        log "Falling back to local monorepo copy at $mono_dir."
        (
            cd "$mono_dir"
            git archive "$DBSCHEME_COMMIT" bin-dbscheme-manager | tar -x -C "$workdir"
        ) || {
            err "local git archive at pinned commit failed."
            err "Hint: the local monorepo may be stale - run 'git fetch' in $mono_dir."
            return 1
        }
        mv "$workdir/bin-dbscheme-manager" "$DBSCHEME_DIR"
    elif [ "$clone_ok" = "0" ]; then
        err "Could not fetch monorepo (network) and no local fallback exists."
        if [ -s "$workdir/clone.err" ]; then
            err "git clone said:"
            tail -3 "$workdir/clone.err" >&2
        fi
        return 1
    fi
    rm -rf "$workdir"
    WORKDIR=""
    echo "$DBSCHEME_COMMIT" > "$DBSCHEME_DIR/.pin"
    # alembic.ini (written below) will carry the real MySQL root password.
    chmod 700 "$DBSCHEME_DIR"
    log "dbscheme ready at $DBSCHEME_DIR"
}
fetch_dbscheme

# --- 3. Ensure DB is up and resolve the compose network ---
if ! docker inspect "$DB_CONTAINER" >/dev/null 2>&1; then
    err "$DB_CONTAINER container not found. Start the db first: docker compose up -d db"
    exit 1
fi

NETWORK="$(docker inspect "$DB_CONTAINER" -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | head -1)"
if [ -z "$NETWORK" ]; then
    err "Could not resolve the compose network from $DB_CONTAINER."
    exit 1
fi
log "compose network: $NETWORK"

log "Waiting for MySQL to accept connections..."
for i in $(seq 1 30); do
    if docker exec "$DB_CONTAINER" sh -c "$MYSQL_IN_DB -e 'SELECT 1'" "$DB_USER" >/dev/null 2>&1; then
        break
    fi
    [ "$i" = "30" ] && { err "MySQL not ready after 60s."; exit 1; }
    sleep 2
done

# --- 4. Create databases (idempotent; charset MUST match init_database.sh:
#        utf8/utf8_general_ci. The MySQL 8 default utf8mb4 breaks several
#        migrations with Error 1071 'Specified key was too long' because
#        4-byte chars push long VARCHAR indexes past the 3072-byte limit.) ---
CREATE_OUT="$(docker exec "$DB_CONTAINER" sh -c "$MYSQL_IN_DB -e 'CREATE DATABASE IF NOT EXISTS bin_manager CHARACTER SET utf8 COLLATE utf8_general_ci; CREATE DATABASE IF NOT EXISTS asterisk CHARACTER SET utf8 COLLATE utf8_general_ci;'" "$DB_USER" 2>&1)" || {
    err "CREATE DATABASE failed:"
    echo "$CREATE_OUT" | grep -v "Using a password" >&2 || true
    exit 1
}

# --- 5. Write alembic.ini for both streams (PyMySQL DSN, db service hostname) ---
write_alembic_ini() {
    local stream_dir="$1" dbname="$2" script_location="$3"
    cat > "$DBSCHEME_DIR/$stream_dir/alembic.ini" <<EOF
[alembic]
script_location = $script_location
sqlalchemy.url = mysql+pymysql://$DB_USER:$DB_PASSWORD@db:3306/$dbname

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
    # alembic.ini now embeds the real MySQL root password - keep it out of
    # reach of other local users (default umask leaves it world-readable).
    chmod 600 "$DBSCHEME_DIR/$stream_dir/alembic.ini"
}
write_alembic_ini "bin-manager" "bin_manager" "main"
write_alembic_ini "asterisk_config" "asterisk" "config"

# --- 6. Run migrations sequentially in a container (abort on first failure) ---
run_stream() {
    local stream_dir="$1" label="$2"
    log "alembic upgrade head: $label ..."
    docker run --rm \
        --network "$NETWORK" \
        -v "$DBSCHEME_DIR:/dbscheme:ro" \
        -w "/dbscheme/$stream_dir" \
        "$MIGRATE_IMAGE" \
        /bin/sh -c "pip install -q --no-deps $PIP_PINS && alembic -c alembic.ini upgrade head"
    log "$label: OK"
}

run_stream "bin-manager" "bin_manager (voipbin core)"
run_stream "asterisk_config" "asterisk (PJSIP config)"

log "All migrations applied successfully."
