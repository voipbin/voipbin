#!/bin/bash
# VoIPBin Install Initialization Script
# Generates .env file with auto-detected values and creates necessary certificates
#
# Usage: sudo ./voipbin init
#
# This script requires sudo for DNS forwarding configuration

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects where .env/certs are written — deliberate, documented.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
ENV_TEMPLATE="${ENV_TEMPLATE:-$PROJECT_DIR/.env.template}"
CERTS_DIR="${CERTS_DIR:-$PROJECT_DIR/certs}"
# resolv.conf hijack backup marker (overridable so tests can stub host probes)
RESOLV_BACKUP="${RESOLV_BACKUP:-/etc/resolv.conf.voipbin-backup}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

# =============================================================================
# Result line + exit helpers (design §2.2)
# Every exit path — including set -e aborts — must end with a
# VOIPBIN_INIT: status=ok|error line on stdout.
# Exit codes: 0 success; 1 validation/user error; 2 environment error.
# =============================================================================

# emit_result <status> [detail...]
emit_result() {
    local status="$1"
    shift
    if [[ $# -gt 0 ]]; then
        echo "VOIPBIN_INIT: status=$status $*"
    else
        echo "VOIPBIN_INIT: status=$status"
    fi
    INIT_RESULT_EMITTED="true"
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
init_exit_trap() {
    local code=$?
    if [[ "${INIT_RESULT_EMITTED:-false}" != "true" ]]; then
        if [[ $code -eq 0 ]]; then
            echo "VOIPBIN_INIT: status=ok"
        else
            echo "VOIPBIN_INIT: status=error reason=\"init aborted (exit $code)\""
        fi
    fi
}

# =============================================================================
# CLI parsing + validation (design §2.2)
# =============================================================================

show_usage() {
    cat << 'USAGE'
Usage: ./scripts/init.sh [OPTIONS]

Options:
  --mode internal|external    Install mode (default: internal)
  --domain <base-domain>      Base domain for external mode (e.g. example.com)
  --tls mkcert|selfsigned|byo TLS mode. Internal default: auto (mkcert if
                              available, else selfsigned). External mode
                              requires --tls byo --cert ... --key ...
  --cert <fullchain.pem>      Certificate file (required with --tls byo)
  --key <privkey.pem>         Private key file (required with --tls byo)
  --kamailio-ip <ip>          Explicit KAMAILIO_EXTERNAL_IP (skips the
                              host-relative auto-generation). Use this when
                              the host's additional IPs are individually
                              routed/registered by the provider (each with
                              its own gateway) rather than free addresses in
                              the host's own subnet — the auto-generated
                              host+8 offset is not guaranteed to be an
                              address you actually own in that case.
  --rtpengine-ip <ip>         Explicit RTPENGINE_EXTERNAL_IP (see
                              --kamailio-ip). Both flags must be given
                              together, or neither.
  --yes, -y                   Answer yes to all prompts (non-interactive)
  --force-reinit              Rewrite .env/certs/Corefile for a new mode or
                              domain (never touches the database; prints the
                              live-state follow-up steps on completion)
  --web-reverse-proxy         Enable the Caddy reverse proxy so api/admin/
                              meet/talk are reachable without host port
                              suffixes (external mode only, requires
                              --tls byo; see README.md)
  -h, --help                  Show this help
USAGE
}

# Domain validation: lowercase RFC-1123 labels, at least two labels,
# no scheme, no port, no trailing dot.
validate_domain() {
    local d="$1"

    [[ -n "$d" ]] || return 1
    [[ "$d" == *.* ]] || return 1
    [[ "$d" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || return 1
    return 0
}

# Loose dotted-quad syntax check for --kamailio-ip/--rtpengine-ip. Not a
# full RFC validator (doesn't reject e.g. 999.1.1.1) — good enough to catch
# typos/flag-value swaps before they land silently in .env.
validate_ipv4() {
    local ip="$1"
    [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    local octet
    for octet in ${ip//./ }; do
        # Force base-10: bash's arithmetic comparison treats a leading-zero
        # operand as octal, so e.g. "099" throws "value too great for base"
        # instead of failing this validator cleanly (leading-zero octets are
        # plausible copy-pasted from a hosting provider's control panel).
        [[ "$((10#$octet))" -le 255 ]] || return 1
    done
    return 0
}

parse_args() {
    INIT_MODE="internal"
    INIT_DOMAIN=""
    INIT_TLS_MODE=""
    INIT_CERT_FILE=""
    INIT_KEY_FILE=""
    INIT_YES="false"
    INIT_FORCE_REINIT="false"
    INIT_MODE_EXPLICIT="false"
    INIT_KAMAILIO_IP=""
    INIT_RTPENGINE_IP=""
    INIT_WEB_REVERSE_PROXY="false"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --mode)
                [[ $# -ge 2 ]] || die 1 "--mode requires a value"
                INIT_MODE="$2"
                INIT_MODE_EXPLICIT="true"
                shift 2
                ;;
            --domain)
                [[ $# -ge 2 ]] || die 1 "--domain requires a value"
                INIT_DOMAIN="$2"
                shift 2
                ;;
            --kamailio-ip)
                [[ $# -ge 2 ]] || die 1 "--kamailio-ip requires a value"
                INIT_KAMAILIO_IP="$2"
                shift 2
                ;;
            --rtpengine-ip)
                [[ $# -ge 2 ]] || die 1 "--rtpengine-ip requires a value"
                INIT_RTPENGINE_IP="$2"
                shift 2
                ;;
            --tls)
                [[ $# -ge 2 ]] || die 1 "--tls requires a value"
                INIT_TLS_MODE="$2"
                shift 2
                ;;
            --cert)
                [[ $# -ge 2 ]] || die 1 "--cert requires a value"
                INIT_CERT_FILE="$2"
                shift 2
                ;;
            --key)
                [[ $# -ge 2 ]] || die 1 "--key requires a value"
                INIT_KEY_FILE="$2"
                shift 2
                ;;
            --yes|-y)
                INIT_YES="true"
                shift
                ;;
            --force-reinit)
                INIT_FORCE_REINIT="true"
                shift
                ;;
            --web-reverse-proxy)
                INIT_WEB_REVERSE_PROXY="true"
                shift
                ;;
            --help|-h)
                show_usage
                exit 0
                ;;
            *)
                die 1 "unknown flag: $1"
                ;;
        esac
    done

    case "$INIT_MODE" in
        internal|external) ;;
        *) die 1 "invalid --mode: $INIT_MODE (expected internal|external)" ;;
    esac

    if [[ -n "$INIT_TLS_MODE" ]]; then
        case "$INIT_TLS_MODE" in
            mkcert|selfsigned|byo) ;;
            *) die 1 "invalid --tls: $INIT_TLS_MODE (expected mkcert|selfsigned|byo)" ;;
        esac
    fi

    if [[ "$INIT_MODE" == "external" ]]; then
        [[ -n "$INIT_DOMAIN" ]] || die 1 "--mode external requires --domain"
        # The internal-mode auto-default (mkcert/selfsigned) must NOT fall
        # through here: external mode with no --tls is rejected outright.
        if [[ -z "$INIT_TLS_MODE" ]]; then
            die 1 "external mode requires --tls byo --cert ... --key ..."
        fi
        if [[ "$INIT_TLS_MODE" != "byo" ]]; then
            die 1 "--tls $INIT_TLS_MODE is not supported in external mode; external mode requires --tls byo --cert ... --key ... (see the external-mode runbook)"
        fi
    else
        if [[ -n "$INIT_DOMAIN" ]]; then
            die 1 "--domain is only valid with --mode external (internal mode is always voipbin.test)"
        fi
    fi

    if [[ "$INIT_TLS_MODE" == "byo" ]]; then
        if [[ -z "$INIT_CERT_FILE" || -z "$INIT_KEY_FILE" ]]; then
            die 1 "--tls byo requires both --cert and --key"
        fi
    fi

    if [[ -n "$INIT_DOMAIN" ]] && ! validate_domain "$INIT_DOMAIN"; then
        die 1 "invalid domain: $INIT_DOMAIN (expected lowercase RFC-1123 labels, at least two labels, no scheme/port/trailing dot)"
    fi

    # --web-reverse-proxy needs a real BYO certificate to terminate TLS for
    # api/admin/meet/talk on the shared host IP (port 80/443, no per-service
    # port suffix) - internal mode's mkcert/selfsigned certs only ever cover
    # the individual :3003/:3004/:3005/:8443 access pattern this flag exists
    # to replace, so requiring external+byo here avoids a confusing runtime
    # failure instead of a clear one at parse time.
    if [[ "$INIT_WEB_REVERSE_PROXY" == "true" && "$INIT_MODE" != "external" ]]; then
        die 1 "--web-reverse-proxy requires --mode external --tls byo"
    fi

    # --kamailio-ip/--rtpengine-ip: both-or-neither, since a single explicit
    # IP without its pair would leave the other auto-generated relative to
    # the *explicit* one's host+8 offset — almost never what's wanted once
    # an operator cares enough to pin one of them by hand.
    if [[ -n "$INIT_KAMAILIO_IP" || -n "$INIT_RTPENGINE_IP" ]]; then
        [[ -n "$INIT_KAMAILIO_IP" && -n "$INIT_RTPENGINE_IP" ]] || \
            die 1 "--kamailio-ip and --rtpengine-ip must be given together"
        validate_ipv4 "$INIT_KAMAILIO_IP" || die 1 "invalid --kamailio-ip: $INIT_KAMAILIO_IP"
        validate_ipv4 "$INIT_RTPENGINE_IP" || die 1 "invalid --rtpengine-ip: $INIT_RTPENGINE_IP"
        [[ "$INIT_KAMAILIO_IP" != "$INIT_RTPENGINE_IP" ]] || \
            die 1 "--kamailio-ip and --rtpengine-ip must be different addresses"
    fi

    TARGET_DOMAIN="voipbin.test"
    [[ "$INIT_MODE" == "external" ]] && TARGET_DOMAIN="$INIT_DOMAIN"

    INIT_COMPOSE_PROFILES="internal-dns"
    [[ "$INIT_MODE" == "external" ]] && INIT_COMPOSE_PROFILES=""
    [[ "$INIT_WEB_REVERSE_PROXY" == "true" ]] && INIT_COMPOSE_PROFILES="web-proxy"

    # SQUARE_BIND_ADDR (VOIP-1325): with the reverse proxy fronting
    # admin/meet/talk on 443, publishing their plaintext-HTTP :3003-3005
    # ports to 0.0.0.0 too would leave the exact cleartext-credential
    # exposure the proxy exists to close. Bind those three to loopback
    # only; Caddy's own published 0.0.0.0:80/443 (docker-compose.yml) is
    # still the sole externally-reachable path to them.
    INIT_SQUARE_BIND_ADDR="0.0.0.0"
    [[ "$INIT_WEB_REVERSE_PROXY" == "true" ]] && INIT_SQUARE_BIND_ADDR="127.0.0.1"

    return 0
}

# =============================================================================
# Existing-.env compatibility (design §2.7)
# =============================================================================

# Refuse an internal->external --force-reinit while internal-mode host state
# remains: the coredns container would be orphaned (holding port 53) and
# resolv.conf would stay hijacked.
check_force_reinit_preconditions() {
    local leftover=()

    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^voipbin-dns$'; then
        leftover+=("coredns container (voipbin-dns) still exists")
    fi
    if [[ -f "$RESOLV_BACKUP" ]]; then
        leftover+=("resolv.conf hijack backup ($RESOLV_BACKUP) still present")
    fi

    if [[ ${#leftover[@]} -gt 0 ]]; then
        log_error "Cannot --force-reinit from internal to external mode while internal-mode host state remains:"
        local item
        for item in "${leftover[@]}"; do
            log_error "  - $item"
        done
        echo ""
        echo "Tear down the internal-mode host state first:"
        echo "  docker compose down"
        echo "  sudo ./scripts/setup-dns.sh --uninstall"
        echo ""
        die 1 "internal-mode host state must be torn down before --force-reinit to external"
    fi

    return 0
}

# Printed on --force-reinit completion: the database is never touched, so the
# live state carrying the old domain needs explicit operator follow-up.
print_force_reinit_followup() {
    echo ""
    echo "=============================================="
    echo "  --force-reinit: live-state follow-up"
    echo "=============================================="
    echo ""
    log_warn "The database was NOT touched. Extension SIP realms still embed the old domain."
    echo ""
    echo "To finish switching the live state to the new domain:"
    echo "  1. Delete the old extensions and recreate them via the API"
    echo "     (for the test customer: ./scripts/setup_test_customer.sh)"
    echo "  2. Recreate the services that cache domain configuration:"
    echo "     docker compose rm -fsv registrar-manager api-manager hook-manager customer-manager square-admin square-meet square-talk"
    echo "     docker compose up -d registrar-manager api-manager hook-manager customer-manager square-admin square-meet square-talk"
    echo ""
}

# check_web_reverse_proxy_restated <existing_web_reverse_proxy> <remedy>
# Footgun guard, same class/fail-closed case-insensitivity as the
# EXTERNAL_IP_PINNED guard in check_existing_env_compat: rewriting .env
# without restating --web-reverse-proxy would silently drop
# WEB_REVERSE_PROXY to false and COMPOSE_PROFILES to empty, dropping the
# Caddy container on the next start.sh — every customer-facing port-less
# URL goes dark with no error at any step. Called only from the points that
# are actually about to rewrite .env (not merely refuse or prompt), so a
# no-op "keep existing" or a mode/domain-switch refusal is never masked by
# this message instead of its own more specific one.
check_web_reverse_proxy_restated() {
    local existing_web_reverse_proxy="$1"
    local remedy="$2"

    if [[ "${existing_web_reverse_proxy,,}" == "true" && "$INIT_WEB_REVERSE_PROXY" != "true" ]]; then
        die 1 "existing install has WEB_REVERSE_PROXY=true; $remedy, or it will be silently disabled (COMPOSE_PROFILES loses web-proxy and the Caddy container stops on the next start.sh)"
    fi
}

# Compare the requested (mode, domain) against an existing .env. Same
# mode+domain keeps today's overwrite-prompt semantics (--yes auto-confirms);
# a switch is refused (SIP realms embed the domain in the database, §2.7)
# unless --force-reinit was given.
check_existing_env_compat() {
    [[ -f "$ENV_FILE" ]] || return 0

    local existing_mode existing_domain existing_web_reverse_proxy
    existing_mode=$(get_domain_mode "$ENV_FILE")
    existing_domain=$(get_env_var "$ENV_FILE" BASE_DOMAIN)
    [[ -z "$existing_domain" ]] && existing_domain="voipbin.test"
    existing_web_reverse_proxy="$(get_env_var "$ENV_FILE" WEB_REVERSE_PROXY)"

    if [[ "$INIT_FORCE_REINIT" == "true" ]]; then
        # Footgun guard: without an explicit --mode, the internal/voipbin.test
        # defaults would silently overwrite an existing external install's
        # .env/certs with self-signed config. Require the operator to state
        # the target when it differs from what is installed.
        if [[ "$INIT_MODE_EXPLICIT" != "true" ]] \
            && [[ "$existing_mode" != "$INIT_MODE" || "$existing_domain" != "$TARGET_DOMAIN" ]]; then
            log_error "Refusing --force-reinit with implicit target (defaults: mode=$INIT_MODE domain=$TARGET_DOMAIN)."
            log_error "  existing:  mode=$existing_mode domain=$existing_domain"
            die 1 "existing install is $existing_mode ($existing_domain); --force-reinit requires explicit --mode (and --domain for external) to confirm the target"
        fi
        if [[ "$existing_mode" == "internal" && "$INIT_MODE" == "external" ]]; then
            check_force_reinit_preconditions
        fi

        # Footgun guard: an existing pinned install (--kamailio-ip/
        # --rtpengine-ip at some earlier init) has KAMAILIO_EXTERNAL_IP/
        # RTPENGINE_EXTERNAL_IP that came from the hosting provider's own
        # allocation, not from HOST_EXTERNAL_IP. --force-reinit without
        # re-passing both flags would silently fall through to the
        # host+8-offset auto-generation and overwrite them with an address
        # nobody owns — exactly what pinning exists to prevent. Require the
        # operator to restate the pin (or explicitly accept losing it) on
        # every --force-reinit, the same way the mode/domain guard above
        # requires explicit --mode.
        # Case-insensitive: this is a safety guard, so an unexpected value
        # (e.g. a hand-edited "EXTERNAL_IP_PINNED=TRUE") must fail closed
        # (still guard) rather than silently not matching and letting the
        # pinned IP get overwritten — the opposite of what a strict
        # lowercase-only comparison would do here.
        local existing_ip_pinned
        existing_ip_pinned="$(get_env_var "$ENV_FILE" EXTERNAL_IP_PINNED)"
        if [[ "${existing_ip_pinned,,}" == "true" && -z "$INIT_KAMAILIO_IP" ]]; then
            die 1 "existing install has pinned external IPs (EXTERNAL_IP_PINNED=true); --force-reinit requires re-passing --kamailio-ip/--rtpengine-ip explicitly, or the auto-generated host+8 offset will silently replace them with an address you don't own"
        fi

        check_web_reverse_proxy_restated "$existing_web_reverse_proxy" \
            "--force-reinit requires re-passing --web-reverse-proxy explicitly"

        log_warn "--force-reinit: rewriting .env/certs/Corefile for mode=$INIT_MODE domain=$TARGET_DOMAIN"
        return 0
    fi

    if [[ "$existing_mode" == "$INIT_MODE" && "$existing_domain" == "$TARGET_DOMAIN" ]]; then
        log_warn ".env file already exists at $ENV_FILE"
        if [[ "$INIT_YES" == "true" ]]; then
            check_web_reverse_proxy_restated "$existing_web_reverse_proxy" \
                "re-run with --web-reverse-proxy explicitly"
            log_info "Overwriting existing .env (--yes)"
            return 0
        fi
        read -p "Do you want to overwrite it? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_info "Keeping existing .env file"
            echo ""
            emit_result ok "mode=$existing_mode domain=$existing_domain action=kept-existing"
            exit 0
        fi
        # Interactive confirm (y): same guard, now that the operator has
        # actually agreed to the overwrite.
        check_web_reverse_proxy_restated "$existing_web_reverse_proxy" \
            "re-run with --web-reverse-proxy explicitly"
        return 0
    fi

    log_error "Refusing to switch mode/domain on an existing install."
    log_error "  existing:  mode=$existing_mode domain=$existing_domain"
    log_error "  requested: mode=$INIT_MODE domain=$TARGET_DOMAIN"
    echo ""
    echo "Extension SIP realms embed the domain in the database; switching"
    echo "requires wiping or regenerating that state. Two supported paths:"
    echo ""
    echo "  1. Full reset (demo installs):"
    echo "       ./scripts/clean.sh --volumes --purge"
    echo "     then re-run init for the new mode/domain."
    echo ""
    echo "  2. Keep the database, rewrite config only:"
    echo "       ./scripts/init.sh --force-reinit ..."
    echo "     (prints the required live-state follow-up; never touches the DB)"
    echo ""
    die 1 "mode/domain switch requires clean.sh --volumes --purge or --force-reinit"
}

# Setup DNS by calling the setup-dns.sh script
setup_dns() {
    local host_ip="$1"
    local kamailio_ip="$2"
    local api_ip="$3"
    local admin_ip="$4"
    local meet_ip="$5"
    local talk_ip="$6"

    # External-mode gate (design §2.3/§2.4): external init never generates a
    # Corefile and never calls setup-dns.sh — effective on both the root and
    # unprivileged paths because it lives inside the function itself.
    if [[ "${INIT_MODE:-internal}" != "internal" ]]; then
        log_info "External mode: DNS is operator-managed, skipping Corefile generation and setup-dns.sh"
        return 0
    fi

    log_step "Setting up DNS..."

    # Create CoreDNS configuration using common function
    generate_coredns_config "$host_ip" "$PROJECT_DIR/config/coredns" "$kamailio_ip"
    log_info "  Created CoreDNS configuration"
    log_info "    API services   → $api_ip"
    log_info "    Admin console  → $admin_ip"
    log_info "    Meet           → $meet_ip"
    log_info "    Talk           → $talk_ip"
    log_info "    SIP services   → $kamailio_ip"

    # Call the setup-dns.sh script (handles OS-specific DNS config + starts CoreDNS)
    if [[ -f "$SCRIPT_DIR/setup-dns.sh" ]]; then
        "$SCRIPT_DIR/setup-dns.sh" -y
    else
        log_warn "  setup-dns.sh not found, skipping DNS configuration"
    fi
}

# Check if mkcert is installed and CA is set up
check_mkcert() {
    if command -v mkcert &> /dev/null; then
        return 0
    fi
    return 1
}

# Install mkcert local CA (one-time setup)
setup_mkcert_ca() {
    if mkcert -check &> /dev/null 2>&1; then
        log_info "  mkcert CA already installed"
        return 0
    fi

    log_info "  Installing mkcert local CA (may require sudo)..."
    mkcert -install
}

# Generate certificate (uses mkcert if available, otherwise OpenSSL)
generate_cert() {
    local domain=$1
    local cert_dir="$CERTS_DIR/$domain"

    if [[ -f "$cert_dir/fullchain.pem" && -f "$cert_dir/privkey.pem" ]]; then
        log_info "  Certificate for $domain already exists, skipping"
        return 0
    fi

    mkdir -p "$cert_dir"

    if [[ "$USE_MKCERT" == "true" ]]; then
        # Use mkcert for locally-trusted certificates
        mkcert -cert-file "$cert_dir/fullchain.pem" -key-file "$cert_dir/privkey.pem" \
            "$domain" "*.$domain" localhost 127.0.0.1 ::1 2>/dev/null
    else
        # Fallback to self-signed OpenSSL certificate
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "$cert_dir/privkey.pem" \
            -out "$cert_dir/fullchain.pem" \
            -subj "/CN=*.$domain" 2>/dev/null
    fi

    log_info "  Created certificate for $domain"
}

# Generate base64-encoded certificate for API/Hook managers
generate_api_cert() {
    local cert_dir="$CERTS_DIR/api"
    local host_ip="$1"

    if [[ -f "$cert_dir/cert.pem" && -f "$cert_dir/privkey.pem" ]]; then
        log_info "  API certificate already exists"
    else
        mkdir -p "$cert_dir"

        if [[ "$USE_MKCERT" == "true" ]]; then
            # Use mkcert for locally-trusted certificates
            mkcert -cert-file "$cert_dir/cert.pem" -key-file "$cert_dir/privkey.pem" \
                voipbin.test "*.voipbin.test" localhost 127.0.0.1 ::1 "$host_ip" 2>/dev/null
            log_info "  Created API certificate (mkcert - browser trusted)"
        else
            # Fallback to self-signed OpenSSL certificate
            openssl req -x509 -nodes -days 365 -newkey rsa:4096 \
                -keyout "$cert_dir/privkey.pem" \
                -out "$cert_dir/cert.pem" \
                -subj "/C=US/ST=Dev/L=Local/O=VoIPBin/OU=Dev/CN=voipbin.test" 2>/dev/null
            log_info "  Created API certificate (self-signed)"
        fi
    fi

    # Return base64-encoded values
    API_SSL_CERT_BASE64=$(cat "$cert_dir/cert.pem" | base64 -w0)
    API_SSL_PRIVKEY_BASE64=$(cat "$cert_dir/privkey.pem" | base64 -w0)
}

# Generate random string for JWT key
generate_random_key() {
    openssl rand -hex 32
}

# generate_random_key_short: for values a downstream C consumer stores in a
# small fixed-size buffer (see DATABASE_ASTERISK_PASSWORD below) where a
# full generate_random_key() would be silently truncated.
generate_random_key_short() {
    openssl rand -hex 16
}

# Generate sequential external IPs for services
# Uses a fixed offset from host IP to ensure unique, predictable IPs
generate_service_ips() {
    local host_ip="$1"

    # Extract the network prefix (first 3 octets) and last octet
    local prefix=$(echo "$host_ip" | cut -d'.' -f1-3)
    local host_last=$(echo "$host_ip" | cut -d'.' -f4)

    # Calculate base for service IPs (host + 8, wrapped if needed)
    local base=$((host_last + 8))
    if [[ $base -gt 250 ]]; then
        base=$((host_last - 50))
    fi
    if [[ $base -lt 2 ]]; then
        base=160
    fi

    # Assign sequential IPs (only for VoIP services that need dedicated IPs)
    KAMAILIO_EXTERNAL_IP="${prefix}.$((base))"
    RTPENGINE_EXTERNAL_IP="${prefix}.$((base + 1))"
}

# Main initialization
main() {
    # Result-line guarantee: registered here, not at top level (§2.2).
    trap init_exit_trap EXIT

    parse_args "$@"

    echo ""
    echo "=============================================="
    echo "  VoIPBin Install Initialization"
    echo "=============================================="
    echo ""

    # No root check (design §2.5): unprivileged init performs every
    # unprivileged step (.env, certs, config) and defers host mutation to
    # sudo ./scripts/setup-host.sh; the root convenience path keeps today's
    # inline behavior (mkcert CA install + DNS setup at the end).

    # Environment prerequisites
    if ! command -v openssl &> /dev/null; then
        die 2 "openssl is required but not installed"
    fi

    # Check template exists
    if [[ ! -f "$ENV_TEMPLATE" ]]; then
        die 2 ".env.template not found at $ENV_TEMPLATE"
    fi

    # Existing-.env compatibility: refuse mode/domain switches (§2.7)
    check_existing_env_compat

    # Step 1: Resolve TLS mode / check certificate tools
    log_step "Checking certificate tools..."
    USE_MKCERT="false"
    case "$INIT_TLS_MODE" in
        byo)
            log_info "  TLS mode: byo (bring-your-own certificates)"
            # Pre-flight (§2.6): validate the certificate against --domain
            # BEFORE .env is written, so a bad cert aborts init cleanly with
            # no half-written .env. Full install runs after .env exists.
            if [[ "$INIT_MODE" == "external" ]]; then
                log_info "  Pre-flight certificate validation..."
                local preflight_certs_args=(--check-only --domain "$INIT_DOMAIN")
                [[ "$INIT_WEB_REVERSE_PROXY" == "true" ]] && preflight_certs_args+=(--web-reverse-proxy)
                if ! "$SCRIPT_DIR/install-certs.sh" "${preflight_certs_args[@]}" "$INIT_CERT_FILE" "$INIT_KEY_FILE"; then
                    die 1 "certificate pre-flight failed for $INIT_CERT_FILE (see install-certs.sh output above)"
                fi
            fi
            ;;
        selfsigned)
            log_info "  TLS mode: selfsigned (browser will show warnings)"
            ;;
        mkcert)
            if ! check_mkcert; then
                die 2 "--tls mkcert requested but mkcert is not installed (sudo apt install mkcert)"
            fi
            # CA trust install is a host mutation: root path only (§2.5).
            # Unprivileged, mkcert creates the CA implicitly on first cert
            # generation; trust is installed later by setup-host.sh.
            if [[ $EUID -eq 0 ]]; then
                setup_mkcert_ca
            else
                log_info "  Unprivileged run: CA trust will be installed by sudo ./scripts/setup-host.sh"
            fi
            USE_MKCERT="true"
            ;;
        *)
            # Auto-default (internal mode only): mkcert if available, else
            # selfsigned — current behavior.
            if check_mkcert; then
                log_info "  mkcert found - will generate browser-trusted certificates"
                if [[ $EUID -eq 0 ]]; then
                    setup_mkcert_ca
                else
                    log_info "  Unprivileged run: CA trust will be installed by sudo ./scripts/setup-host.sh"
                fi
                USE_MKCERT="true"
                INIT_TLS_MODE="mkcert"
            else
                log_warn "  mkcert not found - will use self-signed certificates"
                log_warn "  Install mkcert for browser-trusted certs: sudo apt install mkcert && mkcert -install"
                INIT_TLS_MODE="selfsigned"
            fi
            ;;
    esac
    echo ""

    # Step 2: Detect host IP and find external IPs for services
    log_step "Detecting network configuration..."
    HOST_IP=$(detect_host_ip)
    log_info "  Host IP: $HOST_IP"

    if [[ -n "$INIT_KAMAILIO_IP" ]]; then
        KAMAILIO_EXTERNAL_IP="$INIT_KAMAILIO_IP"
        RTPENGINE_EXTERNAL_IP="$INIT_RTPENGINE_IP"
        EXTERNAL_IP_PINNED="true"
        log_info "  Kamailio External IP:  $KAMAILIO_EXTERNAL_IP (explicit, --kamailio-ip)"
        log_info "  RTPEngine External IP: $RTPENGINE_EXTERNAL_IP (explicit, --rtpengine-ip)"
    else
        log_info "  Generating external IPs for VoIP services..."
        generate_service_ips "$HOST_IP"
        EXTERNAL_IP_PINNED="false"
        log_info "  Kamailio External IP:  $KAMAILIO_EXTERNAL_IP"
        log_info "  RTPEngine External IP: $RTPENGINE_EXTERNAL_IP"
        log_info "  (VoIP services need different IPs than host to avoid SIP loop detection)"
        log_info "  Not the addresses you actually own? Pass --kamailio-ip/--rtpengine-ip"
        log_info "  explicitly — see 'Hosting-provider routed IPs' in README.md."
    fi
    echo ""

    # Steps 3-4 are internal-mode only (§2.6): in external mode the BYO
    # certificate is installed by install-certs.sh after .env exists, so no
    # stale certs/*.voipbin.test/ directories are created and the BYO base64
    # values are never overwritten by a self-signed cert.
    if [[ "$INIT_MODE" == "external" ]]; then
        log_step "Skipping certificate generation (external mode: BYO certificates)"
        API_SSL_CERT_BASE64=""
        API_SSL_PRIVKEY_BASE64=""
        echo ""
    else
        # Step 3: Generate SSL certificates for SIP/TLS
        log_step "Generating SIP TLS certificates..."
        for domain in registrar.voipbin.test conference.voipbin.test sip.voipbin.test sip-service.voipbin.test trunk.voipbin.test; do
            generate_cert "$domain"
        done
        echo ""

        # Step 4: Generate API certificates
        log_step "Generating API SSL certificates..."
        generate_api_cert "$HOST_IP"
        echo ""
    fi

    # Step 5: Generate random keys
    log_step "Generating security keys..."
    JWT_KEY=$(generate_random_key)
    log_info "  Generated JWT_KEY"

    MYSQL_ROOT_PASSWORD=$(generate_random_key)
    log_info "  Generated MYSQL_ROOT_PASSWORD"

    RABBITMQ_DEFAULT_USER="voipbin"
    RABBITMQ_DEFAULT_PASS=$(generate_random_key)
    log_info "  Generated RABBITMQ_DEFAULT_PASS"

    # VOIP-1329: the voip-asterisk-call/-conference/-registrar images ship
    # manager.conf (AMI) and ari.conf (ARI) with a static baked-in
    # [asterisk]/secret=asterisk account - nothing in their k8s_start.sh
    # entrypoint or the k8s deployment manifests wires AMI_USERNAME/
    # AMI_PASSWORD into those files. A randomly generated AMI_USERNAME/
    # PASSWORD here would just never match what the account is actually
    # named/secreted as, breaking the AMI/ARI-to-RabbitMQ proxy sidecars'
    # authentication (call origination/control) while SIP registration keeps
    # working fine (that path is pjsip + realtime MySQL, not AMI/ARI). Match
    # the images' existing account instead of trying to override it.
    #
    # Security note: this is NOT primarily a network-exposure mitigation -
    # AMI (port 5038) IS bound to 127.0.0.1 inside each asterisk container's
    # own namespace, but the SAME account is also the ARI credential, and
    # ARI (port 8088, HTTP+websocket) listens on 0.0.0.0 in these images and
    # is reachable from any other container on the internal 10.100.0.0/16
    # compose network (and from the host). That exposure is baked into the
    # images regardless of what .env says - randomizing this value here
    # cannot mitigate it, and only re-breaks authentication. The trust
    # boundary that actually matters is the compose network/host, not this
    # credential.
    AMI_USERNAME="asterisk"
    AMI_PASSWORD="asterisk"
    log_info "  Using fixed AMI/ARI credentials (VOIP-1329)"

    POSTGRES_PASSWORD=$(generate_random_key)
    log_info "  Generated POSTGRES_PASSWORD"

    # VOIP-1328: asterisk-registrar's realtime MySQL client previously reused
    # the root account (DATABASE_ASTERISK_USERNAME=root in docker-compose.yml).
    # A dedicated least-privilege user is better practice regardless.
    # docker-compose.yml still defaults to root when these two vars are
    # absent from .env, so existing installs are unaffected until they
    # re-run init.sh (or apply the migration manually).
    #
    # VOIP-1332: the auth failures VOIP-1328 originally saw (misleadingly
    # reported by Asterisk as "err 1045 Access Denied") are actually caused
    # by generate_random_key()'s 64-char password being silently truncated
    # to 49 chars by res_config_mysql.c's `char pass[50]` buffer
    # (ast_copy_string() truncates, doesn't error) - the DB then holds a
    # password Asterisk never actually sends. Confirmed by directly reading
    # Asterisk's addons/res_config_mysql.c and reproducing on bm-nyc-01: the
    # connection succeeds the moment the DB account's password is set to
    # (or generated no longer than) 49 chars. Use the short generator here.
    DATABASE_ASTERISK_USERNAME="asterisk_rt"
    DATABASE_ASTERISK_PASSWORD=$(generate_random_key_short)
    log_info "  Generated DATABASE_ASTERISK_PASSWORD"
    echo ""

    # Step 6: Create dummy GCP credentials file
    log_step "Creating config files..."
    mkdir -p "$PROJECT_DIR/config"
    # Remove if it's accidentally a directory
    if [ -d "$PROJECT_DIR/config/dummy-gcp-credentials.json" ]; then
        rm -rf "$PROJECT_DIR/config/dummy-gcp-credentials.json"
    fi
    cat > "$PROJECT_DIR/config/dummy-gcp-credentials.json" << 'GCPEOF'
{
  "type": "service_account",
  "project_id": "dummy-project",
  "private_key_id": "dummy",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBALRiMLAHudeSA2ai+XPJL6ym1QoXJdKbXj0AKQN2EvPcpNN/wSuP\nXtEFdOFVzFO+Y++2ABFAYBIelO4DMHXg0hECAwEAAQJAYPdAPokyZ0UPpP+pu3rq\nwNroFnEMGCKjLPq2F87h3H8cIg+X8MpNJfWfIEKlFyjJfG4P8lNz+uN+Qk7lJI/p\noQIhAOEAx+qYBvP9Tdr3MJk0CJ9JQYUcEUhz5BNLaOrfa+VtAiEAzB0bvEG1Lx8k\nUlxNoqB+IY2M6FXDWHI6fNZ7R3HSqkkCIHdq6s1bLjH0gGsR4LzJPmB8zPfY4u4v\n8i6x6xIPAd+tAiEAj8vQrNPr/5L7VELVah+D6bKDL4Qo3eiH5xcxP+J/f3ECIENY\nrh3PQJN1sObumL6LglGu+l0u+KYoPql4EbdWzaEv\n-----END RSA PRIVATE KEY-----\n",
  "client_email": "dummy@dummy-project.iam.gserviceaccount.com",
  "client_id": "123456789",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token"
}
GCPEOF
    log_info "  Created dummy GCP credentials (replace with real credentials for full functionality)"
    echo ""

    # Step 7: Create .env file
    log_step "Creating .env file..."

    # Single source of truth for the 11 domain-dependent values (§2.1):
    # for voipbin.test the derived values are byte-identical to the historic
    # literals, which is how internal mode stays unchanged.
    derive_domain_env "$TARGET_DOMAIN" "$INIT_WEB_REVERSE_PROXY"

    cat > "$ENV_FILE" << EOF
# ==============================================================================
# VoIPBin Install - Environment Configuration
# ==============================================================================
# Generated by init.sh on $(date)
# ==============================================================================

# ==============================================================================
# Google Cloud Platform (Optional - for TTS, storage, and AI features)
# Set to your service account JSON path for full functionality
# Default uses a dummy file that allows services to start without GCP
#
# The project/region/bucket placeholders below are deliberately non-empty:
# rag-manager's Validate() hard-fails at boot on an empty GCP_PROJECT_ID or
# GCP_REGION, before any GCP client is constructed. These values satisfy that
# validation without granting any GCP access — replace them with your real
# project to actually use RAG ingestion/query, TTS, or storage.
# ==============================================================================
GOOGLE_APPLICATION_CREDENTIALS=./config/dummy-gcp-credentials.json
GCP_PROJECT_ID=sandbox-placeholder
GCP_REGION=us-central1
GCP_BUCKET_NAME_TMP=
GCP_BUCKET_NAME_MEDIA=sandbox-placeholder-media

# ==============================================================================
# API SSL Certificates (Auto-generated)
# ==============================================================================
API_SSL_CERT_BASE64=$API_SSL_CERT_BASE64
API_SSL_PRIVKEY_BASE64=$API_SSL_PRIVKEY_BASE64

HOOK_SSL_CERT_BASE64=$API_SSL_CERT_BASE64
HOOK_SSL_PRIVKEY_BASE64=$API_SSL_PRIVKEY_BASE64

# ==============================================================================
# Install Mode (written by init.sh - re-run init to change; see docs)
# ==============================================================================
# DOMAIN_MODE: internal (CoreDNS serves *.voipbin.test) | external (operator DNS)
DOMAIN_MODE=$INIT_MODE
# COMPOSE_PROFILES: internal-dns enables the coredns service; web-proxy
# enables the Caddy reverse proxy (VOIP-1325); empty in plain external mode
COMPOSE_PROFILES=$INIT_COMPOSE_PROFILES
# TLS_MODE: mkcert | selfsigned | byo
TLS_MODE=$INIT_TLS_MODE
# WEB_REVERSE_PROXY: true enables the Caddy reverse proxy so api/admin/meet/talk
# are reachable without host port suffixes (external mode only, VOIP-1325)
WEB_REVERSE_PROXY=$INIT_WEB_REVERSE_PROXY
# SQUARE_BIND_ADDR: host bind address for admin/meet/talk's published ports.
# 127.0.0.1 when WEB_REVERSE_PROXY=true (Caddy is the only external path to
# these plaintext-HTTP ports); 0.0.0.0 otherwise. See docker-compose.yml.
SQUARE_BIND_ADDR=$INIT_SQUARE_BIND_ADDR

# ==============================================================================
# SIP/VoIP Network Configuration
# ==============================================================================
# Base domain for SIP routing
BASE_DOMAIN=$DERIVED_BASE_DOMAIN

# Host's external IP (your machine's LAN IP)
HOST_EXTERNAL_IP=$HOST_IP

# Kamailio's dedicated external IP (auto-detected available IP in your subnet)
# This MUST be different from HOST_EXTERNAL_IP to avoid SIP loop detection
KAMAILIO_EXTERNAL_IP=$KAMAILIO_EXTERNAL_IP

# RTPEngine's dedicated external IP (for RTP media traffic)
# Auto-detected available IP in your subnet
RTPENGINE_EXTERNAL_IP=$RTPENGINE_EXTERNAL_IP

# When true, KAMAILIO_EXTERNAL_IP/RTPENGINE_EXTERNAL_IP were set explicitly
# via --kamailio-ip/--rtpengine-ip (e.g. hosting-provider-registered routed
# IPs, each with its own gateway, outside the host's own subnet) and must
# NOT be recalculated from HOST_EXTERNAL_IP by the host-IP-change handling
# in scripts/common.sh's update_env_ips(). false means the classic
# host+8-offset auto-generation is in effect and safe to recompute.
EXTERNAL_IP_PINNED=$EXTERNAL_IP_PINNED

# ==============================================================================
# Frontend Configuration
# ==============================================================================
# Base hostname (used as fallback if per-service URLs not set)
BASE_HOSTNAME=$DERIVED_BASE_HOSTNAME

# Admin Console (square-admin) and Talk (square-talk) URLs
API_URL=$DERIVED_API_URL
WEBSOCKET_URL=$DERIVED_WEBSOCKET_URL
REGISTRAR_URL=$DERIVED_REGISTRAR_URL
REGISTRAR_DOMAIN=$DERIVED_REGISTRAR_DOMAIN

# Meet (square-meet) URLs
CONFERENCE_URL=$DERIVED_CONFERENCE_URL
CONFERENCE_DOMAIN=$DERIVED_CONFERENCE_DOMAIN

# Domain names for extension and trunk registration
DOMAIN_NAME_EXTENSION=$DERIVED_DOMAIN_NAME_EXTENSION
DOMAIN_NAME_TRUNK=$DERIVED_DOMAIN_NAME_TRUNK

# SIP TLS certificates path
CERTS_PATH=./certs

# ==============================================================================
# Telephony Providers (OPTIONAL - configure if needed)
# ==============================================================================
TWILIO_SID=
TWILIO_API_KEY=

TELNYX_API_KEY=
TELNYX_CONNECTION_ID=
TELNYX_PROFILE_ID=

MESSAGEBIRD_API_KEY=

# ==============================================================================
# Email Providers (OPTIONAL)
# ==============================================================================
SENDGRID_API_KEY=
MAILGUN_API_KEY=

# ==============================================================================
# AI/ML Services (OPTIONAL - for AI assistant features)
# ==============================================================================
OPENAI_API_KEY=
CARTESIA_API_KEY=
ELEVENLABS_API_KEY=
DEEPGRAM_API_KEY=
XAI_API_KEY=

# ==============================================================================
# AWS (OPTIONAL - for transcription)
# ==============================================================================
AWS_ACCESS_KEY=
AWS_SECRET_KEY=

# ==============================================================================
# Security & Storage
# ==============================================================================
JWT_KEY=$JWT_KEY
MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD
DATABASE_ASTERISK_USERNAME=$DATABASE_ASTERISK_USERNAME
DATABASE_ASTERISK_PASSWORD=$DATABASE_ASTERISK_PASSWORD
RABBITMQ_DEFAULT_USER=$RABBITMQ_DEFAULT_USER
RABBITMQ_DEFAULT_PASS=$RABBITMQ_DEFAULT_PASS
AMI_USERNAME=$AMI_USERNAME
AMI_PASSWORD=$AMI_PASSWORD
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
EMAIL_VERIFY_BASE_URL=$DERIVED_EMAIL_VERIFY_BASE_URL

# ==============================================================================
# Analytics & Monitoring (OPTIONAL)
# ==============================================================================
CLICKHOUSE_ADDRESS=clickhouse:9000
CLICKHOUSE_DATABASE=default
HOMER_URI=
HOMER_API_ADDRESS=
HOMER_AUTH_TOKEN=
HOMER_WHITELIST=

# PSTN whitelist - defaults to HOST_EXTERNAL_IP in docker-compose.yml
PSTN_WHITELIST_IPS=

# ==============================================================================
# RAG (OPTIONAL - for knowledge base features)
# ==============================================================================
# rag-manager now uses Vertex AI embeddings + PostgreSQL/pgvector. It reads its
# project/region/bucket from the GCP block above and its DSN from
# docker-compose.yml; RAG_TOP_K is the only rag-specific knob left here.
RAG_TOP_K=5
EOF

    # .env now holds real, randomly generated credentials (MySQL/RabbitMQ/AMI/
    # Postgres/JWT) — restrict it to owner-only, matching the treatment
    # already applied to alembic.ini in migrate.sh's write_alembic_ini().
    chmod 600 "$ENV_FILE"

    log_info "  Created $ENV_FILE"
    echo ""

    # Step 7.5 (external mode only, §2.6): full BYO certificate install now
    # that .env exists — layout under certs/ plus the .env base64 rewrite.
    if [[ "$INIT_MODE" == "external" ]]; then
        log_step "Installing BYO certificates..."
        local install_certs_args=(--domain "$INIT_DOMAIN")
        [[ "$INIT_WEB_REVERSE_PROXY" == "true" ]] && install_certs_args+=(--web-reverse-proxy)
        if ! "$SCRIPT_DIR/install-certs.sh" "${install_certs_args[@]}" "$INIT_CERT_FILE" "$INIT_KEY_FILE"; then
            die 1 "certificate installation failed for $INIT_CERT_FILE (see install-certs.sh output above)"
        fi
        echo ""
    fi

    # Step 8: Setup DNS for SIP domains (root path only, §2.5 — the
    # unprivileged path defers every host mutation to setup-host.sh)
    if [[ $EUID -eq 0 ]]; then
        setup_dns "$HOST_IP" "$KAMAILIO_EXTERNAL_IP" "$API_EXTERNAL_IP" "$ADMIN_EXTERNAL_IP" "$MEET_EXTERNAL_IP" "$TALK_EXTERNAL_IP"
    else
        log_info "Unprivileged run: skipping DNS setup (handled by sudo ./scripts/setup-host.sh)"
    fi
    echo ""

    # Ownership (§2.5): on the root convenience path (sudo ./scripts/init.sh),
    # hand the generated files back to the invoking user so a later
    # unprivileged start.sh/install-certs.sh doesn't hit root-owned files.
    if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then
        local sudo_group
        sudo_group="$(id -gn "$SUDO_USER" 2>/dev/null || echo "$SUDO_USER")"
        chown "$SUDO_USER:$sudo_group" "$ENV_FILE" 2>/dev/null || true
        chown -R "$SUDO_USER:$sudo_group" "$CERTS_DIR" "$PROJECT_DIR/config" 2>/dev/null || true
        log_info "Ownership of .env, certs/, config/ set to $SUDO_USER"
    fi

    # Summary
    echo "=============================================="
    echo "  Initialization Complete!"
    echo "=============================================="
    echo ""
    log_info "Configuration:"
    log_info "  Host IP:            $HOST_IP (web services)"
    log_info "  Kamailio IP:        $KAMAILIO_EXTERNAL_IP (SIP signaling)"
    log_info "  RTPEngine IP:       $RTPENGINE_EXTERNAL_IP (RTP media)"
    log_info "  Base Domain:        $TARGET_DOMAIN"
    log_info "  Certificates:       $CERTS_DIR"
    log_info "  Environment:        $ENV_FILE"
    echo ""
    # Summary URLs/domains derive from the selected base domain (same approach
    # as start.sh's summary) — internal mode stays voipbin.test byte-for-byte,
    # external mode shows the operator's real domain.
    if [[ "$INIT_WEB_REVERSE_PROXY" == "true" ]]; then
        # VOIP-1325: with the Caddy proxy, :3003-3005 are bound to
        # 127.0.0.1 (SQUARE_BIND_ADDR) and unreachable from anywhere but
        # this host — printing them here would send the operator to a URL
        # that doesn't work for anyone but themselves logged into the box.
        log_info "Web Services (via Caddy reverse proxy, no port suffix):"
        log_info "  https://admin.${TARGET_DOMAIN}"
        log_info "  https://meet.${TARGET_DOMAIN}"
        log_info "  https://talk.${TARGET_DOMAIN}"
        log_info "  https://api.${TARGET_DOMAIN}"
    else
        log_info "Web Services (Docker port mapping):"
        log_info "  http://admin.${TARGET_DOMAIN}:3003"
        log_info "  http://meet.${TARGET_DOMAIN}:3004"
        log_info "  http://talk.${TARGET_DOMAIN}:3005"
        log_info "  https://api.${TARGET_DOMAIN}:8443"
    fi
    echo ""
    log_info "SIP Services:"
    log_info "  sip.${TARGET_DOMAIN}    → $KAMAILIO_EXTERNAL_IP"
    # Branch on the TLS mode first: byo certs are operator-provided and must
    # never get the self-signed warning or the destructive rm -rf advice.
    # Internal-mode output (mkcert/selfsigned) stays byte-identical.
    if [[ "$INIT_TLS_MODE" == "byo" ]]; then
        log_info "  SSL Certs:      operator-provided (BYO), managed via scripts/install-certs.sh"
    elif [[ "$USE_MKCERT" == "true" ]]; then
        log_info "  SSL Certs:      mkcert (browser-trusted)"
    else
        log_warn "  SSL Certs:      self-signed (browser will show warnings)"
        echo ""
        log_warn "To avoid browser certificate warnings, install mkcert:"
        echo "    sudo apt install mkcert    # or: brew install mkcert"
        echo "    mkcert -install"
        echo "    rm -rf $CERTS_DIR"
        echo "    voipbin> init"
    fi
    echo ""
    log_info "Next steps:"
    echo "  1. (Optional) Edit .env to add your API keys (GCP, OpenAI, etc.)"
    echo "  2. Run: voipbin> start"
    echo ""

    if [[ "$INIT_FORCE_REINIT" == "true" ]]; then
        print_force_reinit_followup
    fi

    # Machine-parseable result line (§2.2). next= depends on privilege:
    # unprivileged init hands off to setup-host.sh (Phase 3 activates the path;
    # the line format lands here).
    local next_cmd="./scripts/start.sh"
    [[ $EUID -ne 0 ]] && next_cmd="sudo ./scripts/setup-host.sh"
    emit_result ok "mode=$INIT_MODE domain=$TARGET_DOMAIN tls=$INIT_TLS_MODE next=\"$next_cmd\""
}

main "$@"
