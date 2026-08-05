#!/bin/bash
# VoIPBin Sandbox - .env.template Drift Checker
# Compares the variables the init scripts write into .env against the variables
# documented in .env.template, and reports any difference in either direction.
#
# Usage: ./scripts/check-env-template-sync.sh
#
# Exit code: 0 if in sync, 1 if drift was found.
#
# Run this manually after touching init.sh or .env.template. CI wiring is
# deliberately out of scope for now.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source common functions (log_info / log_warn / log_error / log_step)
source "$SCRIPT_DIR/common.sh"

ENV_TEMPLATE="$PROJECT_DIR/.env.template"

# Every script that generates a .env. init_no_sudo.sh does not exist in this
# repo today but is listed so the check keeps working if it is ever added;
# missing files are skipped silently.
INIT_SCRIPTS=(
    "$SCRIPT_DIR/init.sh"
    "$SCRIPT_DIR/init_no_sudo.sh"
)

# =============================================================================
# Intentionally template-only variables (NOT drift)
# =============================================================================
# These are documented in .env.template for operator visibility but are
# deliberately never written by init.sh. See
# docs/plans/2026-07-30-sandbox-install-reliability-design.md §2.5.
#
# 1. compose-internal-default — docker-compose.yml reads these as
#    ${VAR:-default} with a default matching the template, so the sandbox works
#    without them being set. Only relevant when splitting the state layer across
#    hosts (Track A externalization,
#    docs/plans/2026-07-05-production-grade-horizontal-scale-design.md).
# 2. dead — docker-compose.yml sets RTPENGINE_INTERFACE unconditionally from
#    RTPENGINE_EXTERNAL_IP, so setting it in .env has no effect. Documented so a
#    curious operator understands why, not because it is overridable.
#
# Category 3 ("aspirational") used to hold CLICKHOUSE_ADDRESS, on the grounds
# that a real consumer existed (bin-timeline-manager) but no local service to
# point it at. docker-compose.yml now runs a `clickhouse` service and init.sh
# writes CLICKHOUSE_ADDRESS=clickhouse:9000, so that reasoning no longer holds
# and the entry has been removed rather than left inert. The category is empty
# today; re-add it with a fresh rationale if a genuinely aspirational variable
# shows up again.
TEMPLATE_ONLY_VARS=(
    # compose-internal-default
    DB_HOST
    DB_PORT
    REDIS_HOST
    REDIS_PORT
    RABBITMQ_HOST
    RABBITMQ_PORT
    KAMAILIO_DB_HOST
    KAMAILIO_REDIS_HOST
    RTPENGINE_PORT_MIN
    RTPENGINE_PORT_MAX
    RTPENGINE_LISTEN_HTTP
    SCHEDULE_BACKUP_HOST_DIR
    # dead
    RTPENGINE_INTERFACE
)

is_excluded() {
    local candidate="$1"
    local excluded
    for excluded in "${TEMPLATE_ONLY_VARS[@]}"; do
        [[ "$candidate" == "$excluded" ]] && return 0
    done
    return 1
}

# =============================================================================
# Variable extraction
# =============================================================================
# .env.template documents variables as bare `VAR=value` lines.
extract_template_vars() {
    grep -oE '^[A-Z0-9_]+=' "$ENV_TEMPLATE" | tr -d '=' | sort -u
}

# init.sh writes .env with a single `cat > "$ENV_FILE" << EOF` heredoc, so the
# variable names appear as bare `VAR=...` lines inside that heredoc. Only the
# heredoc body is scanned — matching bare `VAR=` anywhere in the script would
# also pick up the script's own shell locals (SCRIPT_DIR, ENV_FILE, ...).
# The `echo "VAR=..." >> $ENV_FILE` append form is matched too, in case a future
# script writes .env that way instead.
extract_init_vars() {
    local script
    for script in "${INIT_SCRIPTS[@]}"; do
        [[ -f "$script" ]] || continue
        awk '
            # Start of a heredoc redirected into the .env file. Capture the
            # delimiter so nested/other heredocs are not mistaken for the end.
            !in_heredoc && /(cat|tee)[^|;&]*>>?[[:space:]]*"?\$(ENV_FILE|env_file)"?[^|;&]*<<-?/ {
                delim = $0
                sub(/.*<<-?[[:space:]]*/, "", delim)
                gsub(/["'"'"']/, "", delim)
                in_heredoc = 1
                next
            }
            in_heredoc && $0 == delim { in_heredoc = 0; next }
            in_heredoc && /^[A-Z0-9_]+=/ { sub(/=.*/, "", $0); print }
            # echo "VAR=value" >> "$ENV_FILE"
            /echo[[:space:]]+"[A-Z0-9_]+=/ && />>?[[:space:]]*"?\$(ENV_FILE|env_file)"?/ {
                line = $0
                sub(/.*echo[[:space:]]+"/, "", line)
                sub(/=.*/, "", line)
                print line
            }
        ' "$script"
    done | sort -u
}

# =============================================================================
# Main
# =============================================================================
main() {
    if [[ ! -f "$ENV_TEMPLATE" ]]; then
        log_error ".env.template not found at $ENV_TEMPLATE"
        exit 1
    fi

    local found_any=0
    for script in "${INIT_SCRIPTS[@]}"; do
        [[ -f "$script" ]] && found_any=1
    done
    if [[ $found_any -eq 0 ]]; then
        log_error "No init script found; expected at least $SCRIPT_DIR/init.sh"
        exit 1
    fi

    log_step "Checking .env.template sync"

    local template_vars init_vars
    template_vars=$(extract_template_vars)
    init_vars=$(extract_init_vars)

    local drift=0 var

    # Written by an init script but undocumented in the template.
    while IFS= read -r var; do
        [[ -n "$var" ]] || continue
        if ! grep -qxF "$var" <<< "$template_vars"; then
            log_error "  $var is written to .env by an init script but is missing from .env.template"
            drift=1
        fi
    done <<< "$init_vars"

    # Documented in the template but never written, and not on the
    # intentionally-template-only list above.
    while IFS= read -r var; do
        [[ -n "$var" ]] || continue
        if ! grep -qxF "$var" <<< "$init_vars"; then
            if is_excluded "$var"; then
                continue
            fi
            log_error "  $var is documented in .env.template but no init script writes it"
            drift=1
        fi
    done <<< "$template_vars"

    if [[ $drift -ne 0 ]]; then
        echo ""
        log_error ".env.template is out of sync"
        log_error "Fix by documenting the variable in .env.template, generating it in"
        log_error "init.sh, or adding it to TEMPLATE_ONLY_VARS with a reason."
        exit 1
    fi

    log_info "  .env.template is in sync with the init scripts"
}

main "$@"
