#!/bin/bash
# VoIPBin Install - Komodo setup (core + mongo + host periphery + hook)
#
# Usage: sudo ./scripts/komodo-setup.sh
#
# Brings up Komodo next to the main stack and registers the voipbin Stack in it.
# It never calls the main stack's compose project: a failure here leaves the
# main stack exactly as it was. Safe to re-run (every step probes its state).
#
# Steps:
#   1. guard       komodo_lib.py guard (refuses hosts with an existing install)
#   2. preflight   local checks only: x86_64, systemd, docker, port 9120.
#                  Release asset and registry reachability are checked only when
#                  a download or an image pull is actually needed.
#   3. secrets     komodo/.env and komodo/admin-password, created once (600)
#   4. core+mongo  docker compose -p komodo up -d, wait for core
#   5. periphery   pinned binary (sha256 verified), systemd unit, hardened config
#   6. hook        /usr/local/lib/voipbin/komodo-hook.sh
#   7. provision   komodo_lib.py provision (import, render, verify, strip, marker)
#
# Pinned versions come from komodo/versions.env.
#
# Exit codes: 0 success; 1 failure; 2 refused or environment problem (nothing
# changed on the main stack); 21 Komodo unavailable (from komodo_lib.py).
# The last output line is always:
#   VOIPBIN_KOMODO: status=ok|error ...
#
# Test-only environment variables: VOIPBIN_PROJECT_DIR (install directory),
# VOIPBIN_KOMODO_LIB (komodo_lib.py path), VOIPBIN_KOMODO_ROOT (prefix for the
# system paths written under /usr, /etc, /run), VOIPBIN_KOMODO_WAIT_INTERVAL
# (seconds between readiness probes).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${VOIPBIN_PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
KOMODO_LIB="${VOIPBIN_KOMODO_LIB:-$SCRIPT_DIR/komodo_lib.py}"
KOMODO_DIR="$PROJECT_DIR/komodo"
ROOT_PREFIX="${VOIPBIN_KOMODO_ROOT:-}"

source "$SCRIPT_DIR/common.sh"

# =============================================================================
# Constants
# =============================================================================

COMPOSE_PROJECT="komodo"
CORE_CONTAINER="voipbin-komodo-core"
MONGO_VOLUME="komodo_mongo-data"
CORE_PORT=9120
CORE_URL="http://127.0.0.1:$CORE_PORT"
SERVER_NAME="Host"

PERIPHERY_BIN="/usr/local/bin/periphery"
PERIPHERY_CONFIG_DIR="/etc/komodo"
PERIPHERY_CONFIG="$PERIPHERY_CONFIG_DIR/periphery.config.toml"
PERIPHERY_UNIT="/etc/systemd/system/periphery.service"
PERIPHERY_UNIT_NAME="periphery"
PERIPHERY_RELEASE_URL_BASE="https://github.com/moghtech/komodo/releases/download"

HOOK_SRC="$SCRIPT_DIR/komodo-hook.sh"
HOOK_DIR="/usr/local/lib/voipbin"
HOOK_DEST="$HOOK_DIR/komodo-hook.sh"

WAIT_ATTEMPTS=60
WAIT_INTERVAL="${VOIPBIN_KOMODO_WAIT_INTERVAL:-2}"

# =============================================================================
# Result line + exit helpers
# Every exit path ends with a VOIPBIN_KOMODO: status=ok|error line on stdout.
# =============================================================================

KOMODO_RESULT_EMITTED="false"
KOMODO_STEP="start"
KOMODO_DETAIL=""
TMP_FILES=()

emit_result() {
    local status="$1"
    shift
    if [[ $# -gt 0 ]]; then
        echo "VOIPBIN_KOMODO: status=$status $*"
    else
        echo "VOIPBIN_KOMODO: status=$status"
    fi
    KOMODO_RESULT_EMITTED="true"
}

# die <exit-code> <reason>
die() {
    local code="$1"
    shift
    log_error "$*"
    emit_result error "step=$KOMODO_STEP reason=\"$*\""
    exit "$code"
}

cleanup_tmp() {
    local f
    for f in "${TMP_FILES[@]:-}"; do
        [[ -n "$f" ]] && rm -f -- "$f" 2>/dev/null || true
    done
}

# Registered inside main() so sourcing this file in a test never installs it.
komodo_exit_trap() {
    local code=$?
    cleanup_tmp
    if [[ "$KOMODO_RESULT_EMITTED" != "true" ]]; then
        if [[ $code -eq 0 ]]; then
            echo "VOIPBIN_KOMODO: status=ok"
        else
            echo "VOIPBIN_KOMODO: status=error step=$KOMODO_STEP reason=\"komodo setup aborted (exit $code)\""
        fi
    fi
}

# new_tmp <dir> <template>: private temp file (mode 600), removed on exit.
# The path is returned in NEW_TMP (not via a subshell, so it stays registered).
new_tmp() {
    NEW_TMP=$(umask 077 && mktemp "$1/$2") || die 1 "cannot create a temporary file in $1"
    TMP_FILES+=("$NEW_TMP")
}

# =============================================================================
# Helpers
# =============================================================================

# sys_path <absolute system path> -> path under the test prefix (empty in production)
sys_path() {
    printf '%s%s' "$ROOT_PREFIX" "$1"
}

# komodo_compose <args...>: the komodo project only, with a clean compose
# environment so nothing from the caller redirects files or the project.
komodo_compose() {
    env -u COMPOSE_ENV_FILES -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
        docker compose -p "$COMPOSE_PROJECT" \
        -f "$KOMODO_DIR/docker-compose.yml" \
        --env-file "$KOMODO_DIR/versions.env" \
        --env-file "$KOMODO_DIR/.env" \
        "$@"
}

# load_versions: read komodo/versions.env as data (never sourced) and validate.
load_versions() {
    local file="$KOMODO_DIR/versions.env" line key val
    [[ -f "$file" ]] || die 2 "missing $file"
    KOMODO_VERSION="" KOMODO_PERIPHERY_SHA256="" KOMODO_CORE_IMAGE="" KOMODO_MONGO_IMAGE=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$line" =~ ^(KOMODO_[A-Z0-9_]+)=([^[:space:]]+)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            case "$key" in
                KOMODO_VERSION|KOMODO_PERIPHERY_SHA256|KOMODO_CORE_IMAGE|KOMODO_MONGO_IMAGE)
                    printf -v "$key" '%s' "$val" ;;
            esac
        else
            die 2 "malformed line in $file"
        fi
    done < "$file"
    [[ "$KOMODO_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 2 "KOMODO_VERSION invalid in versions.env"
    [[ "$KOMODO_PERIPHERY_SHA256" =~ ^[0-9a-f]{64}$ ]] || die 2 "KOMODO_PERIPHERY_SHA256 invalid in versions.env"
    [[ "$KOMODO_CORE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || die 2 "KOMODO_CORE_IMAGE is not digest pinned"
    [[ "$KOMODO_MONGO_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || die 2 "KOMODO_MONGO_IMAGE is not digest pinned"
}

file_sha256() {
    sha256sum -- "$1" | cut -d' ' -f1
}

# core_container_publishes_port: the komodo core container is running and publishes
# the port (a re-run finds its own listener, which must pass).
core_container_publishes_port() {
    local names
    names=$(docker ps --filter "name=^${CORE_CONTAINER}\$" --filter "publish=$CORE_PORT" \
        --format '{{.Names}}' 2>/dev/null || true)
    [[ "$names" == *"$CORE_CONTAINER"* ]]
}

port_in_use() {
    local out
    out=$(ss -Hltn "( sport = :$CORE_PORT )" 2>/dev/null || true)
    [[ -n "$out" ]]
}

periphery_connected() {
    local out
    out=$(ss -Htnp state established "( dport = :$CORE_PORT )" 2>/dev/null || true)
    [[ "$out" == *'"periphery"'* ]]
}

wait_for() {
    local what="$1"
    shift
    local i
    for ((i = 0; i < WAIT_ATTEMPTS; i++)); do
        if "$@"; then
            return 0
        fi
        sleep "$WAIT_INTERVAL"
    done
    die 1 "$what did not become ready in time"
}

core_responds() {
    curl -fsS -m 3 -o /dev/null -X POST -H 'content-type: application/json' \
        -d '{"type":"GetLoginOptions","params":{}}' "$CORE_URL/auth" 2>/dev/null
}

# =============================================================================
# Steps
# =============================================================================

step_root() {
    KOMODO_STEP="root"
    if [[ "$(id -u)" -ne 0 ]]; then
        die 1 "must be run as root (sudo ./voipbin komodo setup)"
    fi
}

step_guard() {
    KOMODO_STEP="guard"
    local rc=0
    python3 "$KOMODO_LIB" guard || rc=$?
    if [[ $rc -ne 0 ]]; then
        # The guard prints its own explanation; keep its exit code.
        emit_result error "step=guard reason=\"guard refused (exit $rc)\""
        exit "$rc"
    fi
}

step_preflight() {
    KOMODO_STEP="preflight"
    log_step "Checking this host"

    local arch
    arch=$(uname -m)
    [[ "$arch" == "x86_64" ]] || die 2 "unsupported architecture '$arch': komodo setup supports x86_64 only"

    if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d "$(sys_path /run/systemd/system)" ]]; then
        die 2 "systemd is required to run the periphery service"
    fi
    docker compose version >/dev/null 2>&1 || die 2 "docker compose is not available"
    command -v ss >/dev/null 2>&1 || die 2 "ss (iproute2) is required for the port check"

    if port_in_use && ! core_container_publishes_port; then
        die 2 "port $CORE_PORT is in use by another process (komodo core needs 127.0.0.1:$CORE_PORT)"
    fi
    log_info "local checks passed"
}

# Network reachability is checked only for what is actually missing.
step_reachability() {
    KOMODO_STEP="preflight"
    local image
    for image in "$KOMODO_CORE_IMAGE" "$KOMODO_MONGO_IMAGE"; do
        if ! docker image inspect "$image" >/dev/null 2>&1; then
            log_info "image not present locally, checking registry: ${image%%@*}"
            timeout 60 docker manifest inspect "$image" >/dev/null 2>&1 \
                || die 2 "cannot reach the image registry for ${image%%@*}"
        fi
    done
}

# Refuse a symlinked komodo directory, create it 700 owned by the install owner.
step_directory() {
    KOMODO_STEP="secrets"
    OWNER_SPEC=$(stat -c '%u:%g' -- "$PROJECT_DIR")
    [[ ! -L "$KOMODO_DIR" ]] || die 2 "$KOMODO_DIR is a symlink"
    [[ -f "$KOMODO_DIR/docker-compose.yml" ]] || die 2 "missing $KOMODO_DIR/docker-compose.yml"
    chmod 700 -- "$KOMODO_DIR"
    chown "$OWNER_SPEC" -- "$KOMODO_DIR"
}

# rand_hex <bytes>: random hex string in RAND_VALUE (no subshell, so a failure
# stops the script instead of producing an empty secret).
rand_hex() {
    RAND_VALUE=$(openssl rand -hex "$1") || die 2 "openssl rand failed"
    [[ -n "$RAND_VALUE" ]] || die 2 "openssl rand returned nothing"
}

# Secrets are generated only when absent; a file and the mongo volume must
# exist together (mongo applies its root password on first initialization only).
step_secrets() {
    KOMODO_STEP="secrets"
    local env_file="$KOMODO_DIR/.env"
    local pw_file="$KOMODO_DIR/admin-password"
    local have_file="no" have_volume="no"
    [[ -e "$env_file" || -L "$env_file" ]] && have_file="yes"
    docker volume inspect "$MONGO_VOLUME" >/dev/null 2>&1 && have_volume="yes"

    if [[ "$have_file" == "no" && "$have_volume" == "yes" ]]; then
        die 2 "docker volume $MONGO_VOLUME exists but komodo/.env does not. The stored mongo password cannot be recovered: remove the volume (docker volume rm $MONGO_VOLUME) and re-run, or restore komodo/.env from a backup"
    fi
    if [[ "$have_file" == "yes" && "$have_volume" == "no" ]]; then
        die 2 "komodo/.env exists but docker volume $MONGO_VOLUME does not. Remove komodo/.env and re-run so both are created together"
    fi

    if [[ "$have_file" == "yes" ]]; then
        [[ -f "$env_file" && ! -L "$env_file" ]] || die 2 "$env_file is not a regular file"
        chmod 600 -- "$env_file"
        chown "$OWNER_SPEC" -- "$env_file"
        log_info "komodo/.env present, keeping it"
    else
        local admin_pw db_pw jwt webhook tmp
        if [[ -f "$pw_file" && ! -L "$pw_file" ]]; then
            admin_pw=$(head -n1 -- "$pw_file")
        else
            rand_hex 16
            admin_pw="$RAND_VALUE"
        fi
        [[ -n "$admin_pw" ]] || die 2 "$pw_file is empty"
        rand_hex 24
        db_pw="$RAND_VALUE"
        rand_hex 32
        jwt="$RAND_VALUE"
        rand_hex 32
        webhook="$RAND_VALUE"
        new_tmp "$KOMODO_DIR" ".env.XXXXXX"
        tmp="$NEW_TMP"
        {
            printf 'KOMODO_DATABASE_USERNAME=komodo\n'
            printf 'KOMODO_DATABASE_PASSWORD=%s\n' "$db_pw"
            printf 'KOMODO_INIT_ADMIN_USERNAME=admin\n'
            printf 'KOMODO_INIT_ADMIN_PASSWORD=%s\n' "$admin_pw"
            printf 'KOMODO_JWT_SECRET=%s\n' "$jwt"
            printf 'KOMODO_WEBHOOK_SECRET=%s\n' "$webhook"
        } > "$tmp"
        chmod 600 -- "$tmp"
        chown "$OWNER_SPEC" -- "$tmp"
        mv -f -- "$tmp" "$env_file"
        if [[ ! -e "$pw_file" ]]; then
            new_tmp "$KOMODO_DIR" ".admin-password.XXXXXX"
            tmp="$NEW_TMP"
            printf '%s\n' "$admin_pw" > "$tmp"
            chmod 600 -- "$tmp"
            chown "$OWNER_SPEC" -- "$tmp"
            mv -f -- "$tmp" "$pw_file"
            ADMIN_PASSWORD_FILE="$pw_file"
        fi
        log_info "generated komodo/.env (mode 600)"
    fi

    if [[ -f "$pw_file" && ! -L "$pw_file" ]]; then
        chmod 600 -- "$pw_file"
        chown "$OWNER_SPEC" -- "$pw_file"
    fi
}

step_core_mongo() {
    KOMODO_STEP="core"
    log_step "Starting Komodo core and mongo"
    komodo_compose up -d >/dev/null || die 1 "docker compose up failed for the komodo project"
    wait_for "komodo core" core_responds
    log_info "komodo core is answering on 127.0.0.1:$CORE_PORT"
}

# periphery_config_hardened: an existing config already has everything the
# connected steady state needs (no onboarding key, pinned core key, terminals
# off, mode 600).
periphery_config_hardened() {
    local cfg
    cfg=$(sys_path "$PERIPHERY_CONFIG")
    [[ -f "$cfg" && ! -L "$cfg" ]] || return 1
    [[ "$(stat -c %a -- "$cfg")" == "600" ]] || return 1
    grep -qE '^onboarding_key[[:space:]]*=' "$cfg" && return 1
    grep -qE '^core_public_keys[[:space:]]*=' "$cfg" || return 1
    grep -qE '^disable_terminals[[:space:]]*=[[:space:]]*true' "$cfg" || return 1
    grep -qE '^disable_container_terminals[[:space:]]*=[[:space:]]*true' "$cfg" || return 1
    return 0
}

# write_periphery_config <core public key> [onboarding key]
# Keys are written with builtins only (never on a command line).
write_periphery_config() {
    local pubkey="$1" onboarding="${2:-}"
    local dir cfg tmp
    dir=$(sys_path "$PERIPHERY_CONFIG_DIR")
    cfg=$(sys_path "$PERIPHERY_CONFIG")
    mkdir -p -- "$dir"
    new_tmp "$dir" ".periphery.config.XXXXXX"
    tmp="$NEW_TMP"
    {
        printf 'root_directory = "%s"\n' "$PERIPHERY_CONFIG_DIR"
        printf 'disable_terminals = true\n'
        printf 'disable_container_terminals = true\n'
        printf 'core_address = "%s"\n' "$CORE_URL"
        printf 'connect_as = "%s"\n' "$SERVER_NAME"
        printf 'core_public_keys = "%s"\n' "$pubkey"
        if [[ -n "$onboarding" ]]; then
            printf 'onboarding_key = "%s"\n' "$onboarding"
        fi
    } > "$tmp"
    chmod 600 -- "$tmp"
    chown root:root -- "$tmp"
    mv -f -- "$tmp" "$cfg"
}

# install_periphery_binary: download to a temp file, verify sha256 against the
# pinned value, and only then replace the installed binary. Sets
# PERIPHERY_BINARY_CHANGED=yes when it was replaced.
install_periphery_binary() {
    local bin dir url tmp
    bin=$(sys_path "$PERIPHERY_BIN")
    dir=$(dirname "$bin")
    PERIPHERY_BINARY_CHANGED="no"

    if [[ -f "$bin" && ! -L "$bin" && "$(file_sha256 "$bin")" == "$KOMODO_PERIPHERY_SHA256" ]]; then
        log_info "periphery binary already at the pinned version, skipping download"
        return 0
    fi

    url="$PERIPHERY_RELEASE_URL_BASE/$KOMODO_VERSION/periphery-x86_64"
    curl -fsSIL -m 20 -o /dev/null -- "$url" 2>/dev/null \
        || die 2 "cannot reach the Komodo release assets ($PERIPHERY_RELEASE_URL_BASE)"

    mkdir -p -- "$dir"
    new_tmp "$dir" ".periphery.XXXXXX"
    tmp="$NEW_TMP"
    log_info "downloading periphery $KOMODO_VERSION"
    curl -fsSL --proto '=https' -o "$tmp" -- "$url" || die 2 "periphery download failed"
    if [[ "$(file_sha256 "$tmp")" != "$KOMODO_PERIPHERY_SHA256" ]]; then
        die 2 "periphery download sha256 does not match the pinned value; installation stopped, existing binary left in place"
    fi
    chmod 755 -- "$tmp"
    chown root:root -- "$tmp"
    mv -f -- "$tmp" "$bin"
    PERIPHERY_BINARY_CHANGED="yes"
}

# render_unit: systemd unit with a restart delay (without it a crash loop trips
# the start limit and the unit stays failed).
render_unit() {
    cat <<EOF
[Unit]
Description=Agent to connect with Komodo Core

[Service]
Environment="HOME=/root"
ExecStart=/bin/sh -lc "$PERIPHERY_BIN --config-path $PERIPHERY_CONFIG"
Restart=on-failure
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=default.target
EOF
}

# install_periphery_unit: rewrite when different. Sets PERIPHERY_UNIT_CHANGED.
install_periphery_unit() {
    local unit dir tmp
    unit=$(sys_path "$PERIPHERY_UNIT")
    dir=$(dirname "$unit")
    PERIPHERY_UNIT_CHANGED="no"
    mkdir -p -- "$dir"
    new_tmp "$dir" ".periphery.service.XXXXXX"
    tmp="$NEW_TMP"
    render_unit > "$tmp"
    chmod 644 -- "$tmp"
    chown root:root -- "$tmp"
    if [[ -f "$unit" ]] && cmp -s -- "$tmp" "$unit"; then
        return 0
    fi
    mv -f -- "$tmp" "$unit"
    systemctl daemon-reload
    PERIPHERY_UNIT_CHANGED="yes"
}

step_periphery() {
    KOMODO_STEP="periphery"
    log_step "Installing periphery $KOMODO_VERSION (systemd)"

    install_periphery_binary
    install_periphery_unit

    # Already connected and hardened: nothing to register again.
    if [[ "$PERIPHERY_BINARY_CHANGED" == "no" ]] && periphery_config_hardened \
        && systemctl is-active --quiet "$PERIPHERY_UNIT_NAME" && periphery_connected; then
        systemctl enable "$PERIPHERY_UNIT_NAME" >/dev/null 2>&1 || true
        log_info "periphery is connected and hardened, keeping its configuration"
        PERIPHERY_STATE="unchanged"
        return 0
    fi

    local key_file pub_file rc=0 onboarding pubkey
    new_tmp "$KOMODO_DIR" ".onboard-key.XXXXXX"
    key_file="$NEW_TMP"
    new_tmp "$KOMODO_DIR" ".core-pubkey.XXXXXX"
    pub_file="$NEW_TMP"
    python3 "$KOMODO_LIB" onboard --out-key "$key_file" --out-pubkey "$pub_file" || rc=$?
    if [[ $rc -ne 0 ]]; then
        local code=1
        [[ $rc -eq 2 || $rc -eq 21 ]] && code=$rc
        die "$code" "could not create the periphery onboarding key (exit $rc)"
    fi
    onboarding=$(<"$key_file")
    pubkey=$(<"$pub_file")
    [[ -n "$onboarding" && -n "$pubkey" ]] || die 1 "onboarding key or core public key came back empty"

    write_periphery_config "$pubkey" "$onboarding"
    systemctl enable "$PERIPHERY_UNIT_NAME" >/dev/null
    systemctl restart "$PERIPHERY_UNIT_NAME"
    wait_for "periphery connection to komodo core" periphery_connected

    # Registered: drop the one-time key and reconnect with the pinned core key only.
    write_periphery_config "$pubkey"
    systemctl restart "$PERIPHERY_UNIT_NAME"
    wait_for "periphery reconnection to komodo core" periphery_connected
    rm -f -- "$key_file" "$pub_file"
    PERIPHERY_STATE="installed"
    log_info "periphery registered as '$SERVER_NAME'"
}

step_hook() {
    KOMODO_STEP="hook"
    log_step "Installing the deploy hook"
    [[ -f "$HOOK_SRC" ]] || die 2 "missing $HOOK_SRC"
    local dir dest tmp
    dir=$(sys_path "$HOOK_DIR")
    dest=$(sys_path "$HOOK_DEST")
    mkdir -p -- "$dir"
    chmod 755 -- "$dir"
    chown root:root -- "$dir"
    if [[ -f "$dest" && ! -L "$dest" ]] && cmp -s -- "$HOOK_SRC" "$dest" \
        && [[ "$(stat -c '%a' -- "$dest")" == "755" ]]; then
        HOOK_STATE="unchanged"
        log_info "hook already installed"
        return 0
    fi
    new_tmp "$dir" ".komodo-hook.XXXXXX"
    tmp="$NEW_TMP"
    cat -- "$HOOK_SRC" > "$tmp"
    chmod 755 -- "$tmp"
    chown root:root -- "$tmp"
    mv -f -- "$tmp" "$dest"
    HOOK_STATE="installed"
    log_info "hook installed at $HOOK_DEST"
}

step_provision() {
    KOMODO_STEP="provision"
    log_step "Registering the voipbin Stack"
    local rc=0
    python3 "$KOMODO_LIB" provision || rc=$?
    if [[ $rc -ne 0 ]]; then
        local code=1
        [[ $rc -eq 2 || $rc -eq 21 ]] && code=$rc
        die "$code" "komodo provision failed (exit $rc)"
    fi
}

# =============================================================================
# Main
# =============================================================================

main() {
    trap komodo_exit_trap EXIT
    ADMIN_PASSWORD_FILE=""
    PERIPHERY_STATE="unchanged"
    HOOK_STATE="unchanged"
    PERIPHERY_BINARY_CHANGED="no"
    PERIPHERY_UNIT_CHANGED="no"

    step_root
    step_guard
    step_preflight
    load_versions
    step_reachability
    step_directory
    step_secrets
    step_core_mongo
    step_periphery
    step_hook
    step_provision

    if [[ -n "$ADMIN_PASSWORD_FILE" ]]; then
        log_info "initial admin password stored in $ADMIN_PASSWORD_FILE (mode 600): change it after the first login and delete the file"
    fi
    emit_result ok "periphery=$PERIPHERY_STATE hook=$HOOK_STATE core=127.0.0.1:$CORE_PORT${ADMIN_PASSWORD_FILE:+ admin_password_file=$ADMIN_PASSWORD_FILE}"
}

main "$@"
