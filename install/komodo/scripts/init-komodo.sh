#!/usr/bin/env bash
# init-komodo.sh - generates install/komodo/.env and install/komodo/docker-compose.yml.
#
# Idempotent: safe to re-run. An existing .env is never overwritten wholesale
# - only missing keys are appended (this lets an operator add Komodo to a
# host that's been running the main stack for a while, and lets this script
# gain new keys in the future without clobbering what's already there). An
# existing docker-compose.yml is never overwritten either, matching the
# copy-once dist/live split ../../scripts/init.sh already uses for the main
# stack (see ../../CLAUDE.md "docker-compose.yml.dist vs docker-compose.yml").
#
# There is deliberately no --force-reinit here: regenerating
# KOMODO_DATABASE_PASSWORD without also wiping the komodo_mongo_data volume
# leaves MongoDB's existing root account out of sync with the new .env value
# (MONGO_INITDB_ROOT_* only takes effect on a volume's first init) and Komodo
# core falls into an auth-failure loop - the same class of problem this repo
# already hit with DATABASE_ASTERISK_PASSWORD (VOIP-1328/VOIP-1332). If you
# need to rotate these secrets, wipe komodo_mongo_data too (this is a
# deliberately manual, not automated, procedure - see README.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."   # install/komodo/

# Deliberately duplicated (not sourced) from ../scripts/init.sh's
# generate_random_key() - that function lives in init.sh itself, not in
# common.sh, and this script avoids touching the main install/ path at all.
generate_random_key() {
    openssl rand -hex 32
}

ENV_FILE="./.env"
ENV_TEMPLATE="./.env.template"
COMPOSE_DIST="./docker-compose.yml.dist"
COMPOSE_LIVE="./docker-compose.yml"

if [[ ! -f "$ENV_TEMPLATE" ]]; then
    echo "[init-komodo] .env.template not found at $ENV_TEMPLATE" >&2
    exit 2
fi

# --- .env: create from template, or append only missing keys -------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "[init-komodo] Creating $ENV_FILE from .env.template..."
    cp "$ENV_TEMPLATE" "$ENV_FILE"
fi

set_if_missing() {
    local key="$1" value="$2"
    if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        echo "${key}=${value}" >>"$ENV_FILE"
        echo "[init-komodo]   generated ${key}"
    fi
}

set_if_missing KOMODO_BIND_ADDRESS "127.0.0.1"
set_if_missing KOMODO_HOST "http://localhost:9120"
set_if_missing KOMODO_TITLE "Komodo"
set_if_missing KOMODO_SERVER_NAME "bm-nyc-01"
set_if_missing KOMODO_ADMIN_USERNAME "admin"
set_if_missing KOMODO_ADMIN_PASSWORD "$(generate_random_key)"
set_if_missing KOMODO_DATABASE_USERNAME "komodo"
set_if_missing KOMODO_DATABASE_PASSWORD "$(generate_random_key)"
set_if_missing KOMODO_JWT_SECRET "$(generate_random_key)"
set_if_missing KOMODO_WEBHOOK_SECRET "$(generate_random_key)"

chmod 600 "$ENV_FILE"

# --- docker-compose.yml: copy-once from .dist, same pattern as main stack -
if [[ ! -f "$COMPOSE_LIVE" ]]; then
    if [[ ! -f "$COMPOSE_DIST" ]]; then
        echo "[init-komodo] docker-compose.yml.dist not found at $COMPOSE_DIST" >&2
        exit 2
    fi
    cp "$COMPOSE_DIST" "$COMPOSE_LIVE"
    echo "[init-komodo] Created $COMPOSE_LIVE (from docker-compose.yml.dist)"
fi

echo "[init-komodo] Done. Next: ./scripts/komodo.sh up"
