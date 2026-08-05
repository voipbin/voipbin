#!/bin/bash
# VoIPBin Sandbox - BYO Certificate Installer (design §2.6, VOIP-1275)
# Validates and installs an operator-provided certificate (e.g. Let's Encrypt)
# into the layout kamailio and the sandbox scripts consume, rewrites the four
# base64 values in .env, and recreates the running services that consume them.
#
# Unprivileged and idempotent; usable as a certbot --deploy-hook. The deploy
# hook runs as root, so the owner/group/mode of .env and everything written
# under certs/ is preserved (captured before, restored after) and newly
# created cert directories inherit the owner of certs/ itself — the next
# unprivileged start.sh must not hit root-owned files.
#
# Usage: ./scripts/install-certs.sh [--check-only] [--domain <base-domain>] <fullchain.pem> <privkey.pem>
#
#   --check-only   Run validation only; install nothing, touch nothing.
#   --domain <d>   Validate against <d> instead of BASE_DOMAIN from .env
#                  (used by init.sh's pre-flight before .env exists).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on — deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
CERTS_DIR="${CERTS_DIR:-$PROJECT_DIR/certs}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

# The six first-level names the certificate is installed for (design §2.6):
# a non-wildcard cert missing any of these is a hard fail — kamailio would
# otherwise fail TLS silently at runtime.
REQUIRED_NAMES=(api sip sip-service conference trunk registrar)

# =============================================================================
# Result line + exit helpers (design §2.2 pattern, shared with init.sh)
# Every exit path — including set -e aborts — must end with a
# VOIPBIN_CERTS: status=ok|error line on stdout.
# Exit codes: 0 success; 1 validation/user error; 2 environment error.
# =============================================================================

# emit_result <status> [detail...]
emit_result() {
    local status="$1"
    shift
    if [[ $# -gt 0 ]]; then
        echo "VOIPBIN_CERTS: status=$status $*"
    else
        echo "VOIPBIN_CERTS: status=$status"
    fi
    CERTS_RESULT_EMITTED="true"
}

# die <exit-code> <reason>
die() {
    local code="$1"
    shift
    log_error "$*"
    emit_result error "reason=\"$*\""
    exit "$code"
}

# EXIT trap: registered inside main() (NOT at top level — the bats helper
# sources this file minus 'main "$@"', so a top-level trap would fire at
# every test-shell exit). The already-emitted flag prevents double printing.
certs_exit_trap() {
    local code=$?
    if [[ "${CERTS_RESULT_EMITTED:-false}" != "true" ]]; then
        if [[ $code -eq 0 ]]; then
            echo "VOIPBIN_CERTS: status=ok"
        else
            echo "VOIPBIN_CERTS: status=error reason=\"certificate install aborted (exit $code)\""
        fi
    fi
}

# =============================================================================
# CLI parsing
# =============================================================================

show_usage() {
    cat << 'USAGE'
Usage: ./scripts/install-certs.sh [OPTIONS] <fullchain.pem> <privkey.pem>

Options:
  --check-only        Validate only; install nothing
  --domain <d>        Base domain to validate against (default: BASE_DOMAIN
                      from .env)
  -h, --help          Show this help
USAGE
}

parse_args() {
    CERTS_CHECK_ONLY="false"
    CERTS_DOMAIN=""
    CERT_FILE=""
    KEY_FILE=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check-only)
                CERTS_CHECK_ONLY="true"
                shift
                ;;
            --domain)
                [[ $# -ge 2 ]] || die 1 "--domain requires a value"
                CERTS_DOMAIN="$2"
                shift 2
                ;;
            --help|-h)
                show_usage
                exit 0
                ;;
            -*)
                die 1 "unknown flag: $1"
                ;;
            *)
                if [[ -z "$CERT_FILE" ]]; then
                    CERT_FILE="$1"
                elif [[ -z "$KEY_FILE" ]]; then
                    KEY_FILE="$1"
                else
                    die 1 "unexpected argument: $1"
                fi
                shift
                ;;
        esac
    done

    if [[ -z "$CERT_FILE" || -z "$KEY_FILE" ]]; then
        show_usage
        die 1 "both <fullchain.pem> and <privkey.pem> are required"
    fi
    [[ -r "$CERT_FILE" ]] || die 1 "certificate file not readable: $CERT_FILE"
    [[ -r "$KEY_FILE" ]] || die 1 "private key file not readable: $KEY_FILE"

    return 0
}

# --domain wins; otherwise BASE_DOMAIN from .env (design §2.6).
resolve_domain() {
    if [[ -n "$CERTS_DOMAIN" ]]; then
        TARGET_DOMAIN="$CERTS_DOMAIN"
        return 0
    fi
    TARGET_DOMAIN=$(get_env_var "$ENV_FILE" BASE_DOMAIN)
    if [[ -z "$TARGET_DOMAIN" ]]; then
        die 1 "no --domain given and no BASE_DOMAIN in $ENV_FILE — pass --domain <base-domain>"
    fi
}

# =============================================================================
# Validation (design §2.6 step 1)
# =============================================================================

# Print the certificate's SAN DNS entries, one per line.
get_cert_sans() {
    local cert="$1"
    openssl x509 -in "$cert" -noout -text 2>/dev/null \
        | grep -A1 'Subject Alternative Name' \
        | tail -1 \
        | tr ',' '\n' \
        | sed -n 's/.*DNS:\([^ ,]*\).*/\1/p'
}

# san_covers_name <name> <san-list> — exact match, or a wildcard on the
# name's immediate parent domain (*.example.com covers api.example.com but
# NOT x.registrar.example.com).
san_covers_name() {
    local name="$1"
    local sans="$2"
    local parent="${name#*.}"

    if echo "$sans" | grep -qxF "$name"; then
        return 0
    fi
    if echo "$sans" | grep -qxF "*.${parent}"; then
        return 0
    fi
    return 1
}

validate_cert() {
    local d="$TARGET_DOMAIN"

    log_step "Validating certificate for base domain: $d"

    # Key matches cert (compare public keys)
    local cert_pub key_pub
    cert_pub=$(openssl x509 -in "$CERT_FILE" -noout -pubkey 2>/dev/null)
    key_pub=$(openssl pkey -in "$KEY_FILE" -pubout 2>/dev/null)
    if [[ -z "$cert_pub" || -z "$key_pub" || "$cert_pub" != "$key_pub" ]]; then
        die 1 "private key does not match certificate (or files are not valid PEM)"
    fi
    log_info "  Private key matches certificate"

    # SAN coverage of all six installed names — hard fail on any miss
    local sans
    sans=$(get_cert_sans "$CERT_FILE")
    if [[ -z "$sans" ]]; then
        die 1 "certificate has no subjectAltName DNS entries"
    fi

    local missing=()
    local name
    for name in "${REQUIRED_NAMES[@]}"; do
        if ! san_covers_name "${name}.${d}" "$sans"; then
            missing+=("${name}.${d}")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Certificate SAN does not cover: ${missing[*]}"
        log_error "All six names are required (kamailio would fail TLS silently at runtime)."
        log_error "Use a wildcard certificate for *.$d (see the external-mode runbook)."
        die 1 "certificate does not cover required names: ${missing[*]}"
    fi
    log_info "  SAN covers all required names (api/sip/sip-service/conference/trunk/registrar).$d"

    # Expiry: warn under 30 days (an already-expired cert also lands here)
    CERT_EXPIRES=$(openssl x509 -in "$CERT_FILE" -noout -enddate 2>/dev/null | cut -d'=' -f2)
    CERT_EXPIRES_DATE=$(date -d "$CERT_EXPIRES" '+%Y-%m-%d' 2>/dev/null || echo "unknown")
    if ! openssl x509 -in "$CERT_FILE" -noout -checkend $((30 * 24 * 3600)) &>/dev/null; then
        log_warn "  Certificate expires within 30 days (or is already expired): $CERT_EXPIRES"
        log_warn "  Renew it soon; the certbot --deploy-hook recipe automates reinstallation."
    else
        log_info "  Certificate valid until: $CERT_EXPIRES"
    fi

    # *.registrar.<d> is needed only by SIP devices that resolve the realm
    # domain directly instead of using the outbound proxy sip.<d>.
    if ! echo "$sans" | grep -qxF "*.registrar.${d}"; then
        log_warn "  Certificate does not cover *.registrar.$d"
        log_warn "  SIP devices resolving {customer_id}.registrar.$d directly will fail TLS;"
        log_warn "  devices using the outbound proxy sip.$d are unaffected."
    fi
}

# =============================================================================
# Install (design §2.6 steps 2-4)
# =============================================================================

# Layout kamailio and the scripts already consume: certs/api/{cert,privkey}.pem
# plus five certs/<name>.<d>/{fullchain,privkey}.pem copies of the same cert.
install_layout() {
    local d="$TARGET_DOMAIN"

    log_step "Installing certificates to $CERTS_DIR"

    # New paths inherit the owner/group of certs/ itself (design §2.6).
    mkdir -p "$CERTS_DIR"
    local certs_owner
    certs_owner=$(stat -c '%u:%g' "$CERTS_DIR" 2>/dev/null || stat -f '%u:%g' "$CERTS_DIR" 2>/dev/null || echo "")

    mkdir -p "$CERTS_DIR/api"
    cp "$CERT_FILE" "$CERTS_DIR/api/cert.pem"
    cp "$KEY_FILE" "$CERTS_DIR/api/privkey.pem"
    # Private keys: owner-only (cp inherits the source mode, which may be wider)
    chmod 600 "$CERTS_DIR/api/privkey.pem"
    log_info "  Installed certs/api/{cert,privkey}.pem"

    local name dir
    for name in registrar conference sip sip-service trunk; do
        dir="$CERTS_DIR/${name}.${d}"
        mkdir -p "$dir"
        cp "$CERT_FILE" "$dir/fullchain.pem"
        cp "$KEY_FILE" "$dir/privkey.pem"
        chmod 600 "$dir/privkey.pem"
    done
    log_info "  Installed certs/{registrar,conference,sip,sip-service,trunk}.$d/"

    # Ownership: a no-op when run unprivileged (files are already ours and
    # unprivileged chown fails silently); restores user ownership when run as
    # root, e.g. from a certbot --deploy-hook.
    if [[ -n "$certs_owner" ]]; then
        chown -R "$certs_owner" "$CERTS_DIR/api" 2>/dev/null || true
        for name in registrar conference sip sip-service trunk; do
            chown -R "$certs_owner" "$CERTS_DIR/${name}.${d}" 2>/dev/null || true
        done
    fi
}

# Rewrite the four base64 values in .env atomically (temp file + move) while
# preserving the file's owner/group/mode across the inode replacement.
update_env_certs() {
    if [[ ! -f "$ENV_FILE" ]]; then
        die 2 ".env not found at $ENV_FILE — run ./scripts/init.sh first"
    fi

    log_step "Updating base64 certificates in .env"

    local cert_b64 key_b64
    cert_b64=$(base64 -w0 < "$CERT_FILE")
    key_b64=$(base64 -w0 < "$KEY_FILE")

    # Capture identity before replacing the inode
    local env_mode env_owner
    env_mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || echo "")
    env_owner=$(stat -c '%u:%g' "$ENV_FILE" 2>/dev/null || stat -f '%u:%g' "$ENV_FILE" 2>/dev/null || echo "")

    local tmp_env
    tmp_env="$(dirname "$ENV_FILE")/.env.install-certs.$$"
    sed -e "s|^API_SSL_CERT_BASE64=.*|API_SSL_CERT_BASE64=$cert_b64|" \
        -e "s|^API_SSL_PRIVKEY_BASE64=.*|API_SSL_PRIVKEY_BASE64=$key_b64|" \
        -e "s|^HOOK_SSL_CERT_BASE64=.*|HOOK_SSL_CERT_BASE64=$cert_b64|" \
        -e "s|^HOOK_SSL_PRIVKEY_BASE64=.*|HOOK_SSL_PRIVKEY_BASE64=$key_b64|" \
        "$ENV_FILE" > "$tmp_env"

    # Restore identity: chmod always; chown is a silent no-op unprivileged
    # and restores the original owner when run as root (deploy hook).
    if [[ -n "$env_mode" ]]; then
        chmod "$env_mode" "$tmp_env"
    fi
    if [[ -n "$env_owner" ]]; then
        chown "$env_owner" "$tmp_env" 2>/dev/null || true
    fi

    mv -f "$tmp_env" "$ENV_FILE"
    log_info "  API_SSL_*/HOOK_SSL_* base64 values updated (owner/mode preserved)"
}

# Recreate/restart the running consumers only (design §2.6 step 4):
# api-manager and hook-manager bake the certs from env (recreate); kamailio
# mounts the cert files (restart). Not running -> skip with a note.
recreate_services() {
    log_step "Applying certificates to running services"

    # Scope the "is it running" check to THIS project's compose. A global
    # `docker ps` name match (voipbin-api-mgr) would match any compose project
    # on the host and trigger recreates against a stack this .env doesn't own.
    local running_services
    running_services="$(cd "$PROJECT_DIR" && docker compose ps --services --status running 2>/dev/null || true)"

    if grep -qx "api-manager" <<< "$running_services"; then
        log_info "  Recreating api-manager and hook-manager (env-baked certs)..."
        (cd "$PROJECT_DIR" && docker compose rm -sf api-manager hook-manager && docker compose up -d api-manager hook-manager)
    else
        log_info "  api-manager not running — skipping recreate (certs apply on next start)"
    fi

    if grep -qx "kamailio" <<< "$running_services"; then
        log_info "  Restarting kamailio (file-mounted certs)..."
        (cd "$PROJECT_DIR" && docker compose restart kamailio)
    else
        log_info "  kamailio not running — skipping restart (certs apply on next start)"
    fi
}

# =============================================================================
# Main
# =============================================================================

main() {
    # Result-line guarantee: registered here, not at top level (§2.2 pattern).
    trap certs_exit_trap EXIT

    parse_args "$@"

    if ! command -v openssl &> /dev/null; then
        die 2 "openssl is required but not installed"
    fi

    resolve_domain
    validate_cert

    if [[ "$CERTS_CHECK_ONLY" == "true" ]]; then
        log_info "Check-only mode: nothing installed"
        emit_result ok "domain=$TARGET_DOMAIN expires=$CERT_EXPIRES_DATE action=check-only"
        return 0
    fi

    install_layout
    update_env_certs
    recreate_services

    echo ""
    log_info "Certificate installation complete."
    emit_result ok "domain=$TARGET_DOMAIN expires=$CERT_EXPIRES_DATE"
}

main "$@"
