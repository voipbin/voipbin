#!/bin/bash
# VoIPBin Sandbox - Common Functions
# Shared utilities used by all sandbox scripts

# =============================================================================
# Script Path Setup
# =============================================================================
# These should be set by the sourcing script before sourcing this file:
# SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# =============================================================================
# Colors
# =============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# Logging Functions
# =============================================================================
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
    echo -e "\n${BLUE}==>${NC} $1"
}

# =============================================================================
# Root Check
# =============================================================================
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo ""
        log_error "This script must be run with sudo"
        echo ""
        echo "  sudo $0"
        echo ""
        exit 1
    fi
}

# =============================================================================
# .env Access + Domain Mode (dual-mode DNS, VOIP-1275)
# =============================================================================

# get_env_var <env-file> <var-name>
# Prints the value of VAR= from the file (last occurrence wins). Pure text
# processing — the file is never sourced, so untrusted content (e.g. $(...))
# is returned literally, never executed. Empty output if the file or the
# variable is absent. Trailing \r (CRLF-saved .env) and surrounding
# whitespace are stripped so the strict-equality mode/TLS gates never
# silently mismatch on an invisible character.
get_env_var() {
    local env_file="$1"
    local var_name="$2"
    local value

    [[ -n "$env_file" && -f "$env_file" ]] || return 0
    value=$(grep "^${var_name}=" "$env_file" 2>/dev/null | tail -1 | cut -d'=' -f2-)
    value="${value%$'\r'}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s\n' "$value"
}

# get_domain_mode <env-file>
# Prints the install mode: internal | external. A missing file or missing
# DOMAIN_MODE key maps to "internal" (legacy .env rule, design §2.5) — all
# mode gates must call this, never get_env_var DOMAIN_MODE directly.
get_domain_mode() {
    local env_file="$1"
    local mode
    mode=$(get_env_var "$env_file" DOMAIN_MODE)

    if [[ -z "$mode" ]]; then
        echo "internal"
    else
        echo "$mode"
    fi
}

# derive_domain_env <base_domain>
# Sets the DERIVED_* shell variables for the 11 domain-dependent .env values
# (design §2.1). This is the ONLY place domain values are composed. For
# <base_domain> = voipbin.test the results are byte-identical to the historic
# literals (mode-1 no-regression invariant; asserted by tests/common.bats).
derive_domain_env() {
    local d="$1"

    DERIVED_API_URL="https://api.${d}:8443/"
    DERIVED_WEBSOCKET_URL="wss://api.${d}:8443/v1.0/ws"
    DERIVED_REGISTRAR_URL="wss://sip.${d}:5066"
    DERIVED_REGISTRAR_DOMAIN="registrar.${d}"
    DERIVED_CONFERENCE_URL="wss://conference.${d}"
    DERIVED_CONFERENCE_DOMAIN="conference.${d}"
    DERIVED_DOMAIN_NAME_EXTENSION="registrar.${d}"
    DERIVED_DOMAIN_NAME_TRUNK="trunk.${d}"
    DERIVED_EMAIL_VERIFY_BASE_URL="https://api.${d}:8443"
    DERIVED_BASE_DOMAIN="${d}"
    DERIVED_BASE_HOSTNAME="${d}"
}

# check_compose_profiles_conflict <env-file>
# Guards against (a) a shell-exported COMPOSE_PROFILES contradicting the
# project .env (shell wins in Compose precedence, silently changing the
# service set) and (b) a stale pre-dual-mode .env (internal mode, no
# COMPOSE_PROFILES key) that would silently drop coredns while resolv.conf
# still points at 127.0.0.1. Defined here (not in start.sh) because both
# start.sh and check-install.sh consume it.
# Returns 0 when consistent; on conflict sets COMPOSE_PROFILES_CONFLICT_REASON,
# logs the problem, and returns 1 (callers emit their own result line).
check_compose_profiles_conflict() {
    local env_file="$1"

    COMPOSE_PROFILES_CONFLICT_REASON=""
    [[ -n "$env_file" && -f "$env_file" ]] || return 0

    local env_profiles
    env_profiles=$(get_env_var "$env_file" COMPOSE_PROFILES)

    # (a) shell-exported COMPOSE_PROFILES contradicting .env
    if [[ -n "${COMPOSE_PROFILES+x}" && "$COMPOSE_PROFILES" != "$env_profiles" ]]; then
        COMPOSE_PROFILES_CONFLICT_REASON="COMPOSE_PROFILES is set in your shell ('${COMPOSE_PROFILES}') and contradicts .env ('${env_profiles}'); run: unset COMPOSE_PROFILES"
        log_error "$COMPOSE_PROFILES_CONFLICT_REASON"
        return 1
    fi

    # (b) internal mode with no COMPOSE_PROFILES key at all -> stale .env
    if ! grep -q '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null; then
        if [[ "$(get_domain_mode "$env_file")" == "internal" ]]; then
            COMPOSE_PROFILES_CONFLICT_REASON="stale .env, re-run ./scripts/init.sh --yes"
            log_error "$COMPOSE_PROFILES_CONFLICT_REASON"
            return 1
        fi
    fi

    return 0
}

# =============================================================================
# Host IP Detection
# =============================================================================
detect_host_ip() {
    local ip=""

    # Try to get from .env first
    if [[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.env" ]]; then
        ip=$(grep '^HOST_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
    fi

    # Fallback: Try to get the IP of the default route interface
    if [[ -z "$ip" ]]; then
        if command -v ip &> /dev/null; then
            ip=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'src \K\S+' | head -1)
        fi
    fi

    # Fallback: get first non-localhost IP
    if [[ -z "$ip" ]]; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi

    # macOS fallback
    if [[ -z "$ip" ]]; then
        ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
    fi

    # Final fallback
    if [[ -z "$ip" ]]; then
        ip="127.0.0.1"
    fi

    echo "$ip"
}

# Detect current host IP (fresh detection, ignores .env)
detect_current_host_ip() {
    local ip=""

    # Try to get the IP of the default route interface
    if command -v ip &> /dev/null; then
        ip=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'src \K\S+' | head -1)
    fi

    # Fallback: get first non-localhost IP
    if [[ -z "$ip" ]]; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi

    # macOS fallback
    if [[ -z "$ip" ]]; then
        ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
    fi

    # Final fallback
    if [[ -z "$ip" ]]; then
        ip="127.0.0.1"
    fi

    echo "$ip"
}

# Get configured host IP from .env
get_configured_host_ip() {
    if [[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.env" ]]; then
        grep '^HOST_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1
    fi
}

# Check if host IP has changed from .env configuration
# Returns 0 if changed, 1 if same
check_ip_changed() {
    local current_ip=$(detect_current_host_ip)
    local configured_ip=$(get_configured_host_ip)

    if [[ -z "$configured_ip" ]]; then
        # No .env or no configured IP
        return 0
    fi

    if [[ "$current_ip" != "$configured_ip" ]]; then
        return 0  # Changed
    fi

    return 1  # Same
}

# Generate secondary IPs (Kamailio, RTPEngine) based on host IP
# Ensures they are in the same subnet but different from host
generate_secondary_ips() {
    local host_ip="$1"
    local prefix=$(echo "$host_ip" | cut -d'.' -f1-3)
    local host_last=$(echo "$host_ip" | cut -d'.' -f4)

    # Calculate base offset: host_last + 8 (with wrap-around)
    local base=$((host_last + 8))
    if [[ $base -gt 250 ]]; then
        base=$((host_last - 50))
        if [[ $base -lt 2 ]]; then
            base=160
        fi
    fi

    echo "KAMAILIO_EXTERNAL_IP=${prefix}.${base}"
    echo "RTPENGINE_EXTERNAL_IP=${prefix}.$((base + 1))"
}

# Update .env with new IPs
update_env_ips() {
    local new_host_ip="$1"
    local env_file="${PROJECT_DIR}/.env"

    if [[ ! -f "$env_file" ]]; then
        log_error ".env file not found"
        return 1
    fi

    # Generate new secondary IPs
    local secondary_ips=$(generate_secondary_ips "$new_host_ip")
    local new_kamailio_ip=$(echo "$secondary_ips" | grep KAMAILIO | cut -d'=' -f2)
    local new_rtpengine_ip=$(echo "$secondary_ips" | grep RTPENGINE | cut -d'=' -f2)

    log_info "Updating .env with new IPs:"
    log_info "  HOST_EXTERNAL_IP: $new_host_ip"
    log_info "  KAMAILIO_EXTERNAL_IP: $new_kamailio_ip"
    log_info "  RTPENGINE_EXTERNAL_IP: $new_rtpengine_ip"

    # Update .env file (IP vars update in both modes)
    sed -i "s|^HOST_EXTERNAL_IP=.*|HOST_EXTERNAL_IP=$new_host_ip|" "$env_file"
    sed -i "s|^KAMAILIO_EXTERNAL_IP=.*|KAMAILIO_EXTERNAL_IP=$new_kamailio_ip|" "$env_file"
    sed -i "s|^RTPENGINE_EXTERNAL_IP=.*|RTPENGINE_EXTERNAL_IP=$new_rtpengine_ip|" "$env_file"

    # Also update frontend URLs that use the host IP — composed from BASE_DOMAIN
    # (never a hardcoded literal), and skipped entirely in external mode:
    # external URLs never embed IPs, so an IP change must not clobber them.
    if [[ "$(get_domain_mode "$env_file")" == "external" ]]; then
        log_info "External mode: skipping API_URL/WEBSOCKET_URL rewrite (operator-managed domain)" >&2
    else
        local base_domain
        base_domain=$(get_env_var "$env_file" BASE_DOMAIN)
        [[ -z "$base_domain" ]] && base_domain="voipbin.test"
        sed -i "s|^API_URL=.*|API_URL=https://api.${base_domain}:8443/|" "$env_file"
        sed -i "s|^WEBSOCKET_URL=.*|WEBSOCKET_URL=wss://api.${base_domain}:8443/v1.0/ws|" "$env_file"
    fi

    echo "$new_kamailio_ip"
}

# Regenerate SSL certificates with new IP and update .env
regenerate_ssl_certs() {
    local new_host_ip="$1"
    local env_file="${PROJECT_DIR}/.env"
    local cert_dir="${PROJECT_DIR}/certs/api"

    # BYO certificates (TLS_MODE=byo) must never be overwritten by mkcert
    # regeneration. return 0, NOT 1: all call sites invoke this bare under
    # set -e, so a non-zero return would abort the caller mid-run on every
    # external-mode IP change. Same rule for every gate at a shared choke point.
    if [[ "$(get_env_var "$env_file" TLS_MODE)" == "byo" ]]; then
        log_info "TLS_MODE=byo: skipping certificate regeneration (manage certs with install-certs.sh)"
        return 0
    fi

    # Check if mkcert is available
    if ! command -v mkcert &> /dev/null; then
        log_warn "mkcert not installed, skipping certificate regeneration"
        log_warn "Install mkcert for automatic certificate updates: sudo apt install mkcert && mkcert -install"
        return 1
    fi

    # Create cert directory if needed
    mkdir -p "$cert_dir"

    log_info "Regenerating SSL certificate for IP: $new_host_ip"

    # Generate new certificate
    mkcert -cert-file "$cert_dir/cert.pem" -key-file "$cert_dir/privkey.pem" \
        "voipbin.test" "*.voipbin.test" "localhost" "127.0.0.1" "::1" "$new_host_ip" 2>/dev/null

    if [[ $? -ne 0 ]]; then
        log_error "Failed to generate certificate"
        return 1
    fi

    # Update base64-encoded certificates in .env
    if [[ -f "$env_file" ]]; then
        local new_cert_b64=$(base64 -w0 "$cert_dir/cert.pem")
        local new_key_b64=$(base64 -w0 "$cert_dir/privkey.pem")

        sed -i "s|^API_SSL_CERT_BASE64=.*|API_SSL_CERT_BASE64=$new_cert_b64|" "$env_file"
        sed -i "s|^API_SSL_PRIVKEY_BASE64=.*|API_SSL_PRIVKEY_BASE64=$new_key_b64|" "$env_file"
        # Hook certs use the same as API certs
        sed -i "s|^HOOK_SSL_CERT_BASE64=.*|HOOK_SSL_CERT_BASE64=$new_cert_b64|" "$env_file"
        sed -i "s|^HOOK_SSL_PRIVKEY_BASE64=.*|HOOK_SSL_PRIVKEY_BASE64=$new_key_b64|" "$env_file"

        log_info "SSL certificate regenerated and .env updated"
    fi

    return 0
}

# Full IP regeneration: detect current IP, update .env, regenerate CoreDNS and certs
regenerate_ip_config() {
    local current_ip=$(detect_current_host_ip)
    local configured_ip=$(get_configured_host_ip)

    if [[ "$current_ip" == "$configured_ip" ]]; then
        log_info "Host IP unchanged: $current_ip"
        return 1
    fi

    log_warn "Host IP changed: $configured_ip -> $current_ip"

    # Update .env and get new Kamailio IP
    local new_kamailio_ip=$(update_env_ips "$current_ip")

    # Regenerate CoreDNS config (internal mode only — external mode deploys no
    # coredns, and writing .test zones onto an external tree is confusing residue)
    if [[ "$(get_domain_mode "$PROJECT_DIR/.env")" == "internal" ]]; then
        generate_coredns_config "$current_ip" "$PROJECT_DIR/config/coredns" "$new_kamailio_ip"
        log_info "CoreDNS configuration regenerated"
    else
        log_info "External mode: operator DNS may need updating (HOST_EXTERNAL_IP changed)"
    fi

    # Regenerate SSL certificates
    regenerate_ssl_certs "$current_ip"

    return 0
}

# =============================================================================
# CoreDNS Configuration
# =============================================================================
# NOTE: api-manager/square-admin/square-meet/square-talk no longer have fixed
# 10.100.0.x addresses (removed in Phase 1 of the horizontal-scale-architecture
# design, docs/plans/2026-07-05) — DNS below resolves web-facing domains to the
# HOST's external IP; Docker port publishing (0.0.0.0:PORT:80/443) handles the
# routing to whichever container is currently running that service.

generate_coredns_config() {
    local host_ip="$1"
    local config_dir="${2:-$PROJECT_DIR/config/coredns}"
    local kamailio_ip="${3:-$host_ip}"  # Kamailio needs dedicated IP for SIP
    local corefile="$config_dir/Corefile"

    # Create directory
    mkdir -p "$config_dir"

    # Remove if it's a directory (Docker creates dir if file doesn't exist at mount time)
    if [[ -d "$corefile" ]]; then
        rm -rf "$corefile"
    fi

    cat > "$corefile" << EOF
# CoreDNS configuration for VoIPBin Sandbox
# Auto-generated - do not edit directly
#
# Web Services (resolve to host IP, Docker port mapping handles routing):
#   http://admin.voipbin.test       -> $host_ip:80
#   https://api.voipbin.test        -> $host_ip:8443
#   http://meet.voipbin.test:3004   -> $host_ip:3004
#   http://talk.voipbin.test:3005   -> $host_ip:3005
#
# SIP Services (resolve to Kamailio's dedicated external IP):
#   sip.voipbin.test                -> $kamailio_ip
#   pstn.voipbin.test               -> $kamailio_ip
#   trunk.voipbin.test              -> $kamailio_ip
#   *.registrar.voipbin.test        -> $kamailio_ip

# Web services - resolve to host IP (Docker handles port mapping)
api.voipbin.test {
    template IN A {
        answer "api.voipbin.test 60 IN A $host_ip"
    }
    template IN AAAA {
        rcode NOERROR
    }
}

admin.voipbin.test {
    template IN A {
        answer "admin.voipbin.test 60 IN A $host_ip"
    }
    template IN AAAA {
        rcode NOERROR
    }
}

meet.voipbin.test {
    template IN A {
        answer "meet.voipbin.test 60 IN A $host_ip"
    }
    template IN AAAA {
        rcode NOERROR
    }
}

talk.voipbin.test {
    template IN A {
        answer "talk.voipbin.test 60 IN A $host_ip"
    }
    template IN AAAA {
        rcode NOERROR
    }
}

# SIP services and catch-all - resolve to Kamailio's dedicated IP
voipbin.test {
    template IN A {
        answer "{{ .Name }} 60 IN A $kamailio_ip"
    }
    template IN AAAA {
        rcode NOERROR
    }
}

. {
    forward . 8.8.8.8 8.8.4.4
    cache 30
}
EOF
}

# =============================================================================
# DNS Fallback / Upstream Capture (VOIP-1285)
# =============================================================================
# Overridable so bats can point these at a temp dir instead of real /etc
# and /run (both normally require root to write).
RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"
RESOLV_UPSTREAMS="${RESOLV_UPSTREAMS:-/etc/resolv.conf.voipbin-upstreams}"
SYSTEMD_RESOLVE_CONF="${SYSTEMD_RESOLVE_CONF:-/run/systemd/resolve/resolv.conf}"

# _filter_and_dedupe_nameservers: reads nameserver IPs on stdin (one per
# line), drops loopback/link-local/IPv6-loopback entries, dedupes, caps at 2.
_filter_and_dedupe_nameservers() {
    # fe[89ab] covers the full fe80::/10 link-local block (first 10 bits
    # fixed), not just the literal "fe80:" prefix.
    grep -viE '^(127\.|169\.254\.|::1$|fe[89ab])' | awk '!seen[$0]++' | head -n 2
}

# capture_dns_upstreams <initial|refresh>
# Captures upstream (non-CoreDNS) nameservers into $RESOLV_UPSTREAMS, for use
# as a fallback chain so a downed CoreDNS container doesn't take down all
# host DNS resolution.
#
# initial: systemd-resolved live file -> plain resolv.conf -> hardcoded
#          Corefile-forward targets. Only correct before this script has
#          ever rewritten $RESOLV_CONF.
# refresh: systemd-resolved live file -> hardcoded Corefile-forward targets.
#          Deliberately skips "read $RESOLV_CONF" — at refresh time that
#          file is our own previously-generated output, so re-reading it
#          would just recover the (possibly now-stale) previous fallback
#          instead of finding a new upstream.
capture_dns_upstreams() {
    local mode="$1"
    local upstreams=""

    if [[ -f "$SYSTEMD_RESOLVE_CONF" ]]; then
        upstreams=$(grep -E '^nameserver[[:space:]]+' "$SYSTEMD_RESOLVE_CONF" \
            | awk '{print $2}' | _filter_and_dedupe_nameservers)
    fi

    if [[ -z "$upstreams" && "$mode" == "initial" && -f "$RESOLV_CONF" ]]; then
        upstreams=$(grep -E '^nameserver[[:space:]]+' "$RESOLV_CONF" \
            | awk '{print $2}' | _filter_and_dedupe_nameservers)
    fi

    if [[ -z "$upstreams" ]]; then
        # Same trust boundary as generate_coredns_config's "forward" target —
        # a hardcoded literal, not a read-back of a generated Corefile.
        upstreams=$'8.8.8.8\n8.8.4.4'
    fi

    printf '%s\n' "$upstreams" > "$RESOLV_UPSTREAMS"
}

# write_resolv_conf_with_fallback: writes $RESOLV_CONF as
#   nameserver 127.0.0.1 (CoreDNS)
#   nameserver <upstream-1>      (from $RESOLV_UPSTREAMS, up to 2)
#   nameserver <upstream-2>
#   options timeout:1 attempts:2
# capture_dns_upstreams must have been called at least once before this
# (directly, or in an earlier run whose state file still exists).
write_resolv_conf_with_fallback() {
    local upstream_lines=""
    if [[ -f "$RESOLV_UPSTREAMS" ]]; then
        # Defense in depth: cap at 2 here too (not just at capture time in
        # _filter_and_dedupe_nameservers), so 127.0.0.1 + these never
        # exceeds glibc's MAXNS=3 even if $RESOLV_UPSTREAMS ever ends up
        # with extra lines (manual edit, future capture-path bug, etc.).
        upstream_lines=$(head -n 2 "$RESOLV_UPSTREAMS" | sed 's/^/nameserver /')
    fi

    # Cleanup/restart of systemd-resolved or NetworkManager (run by the
    # caller before this, see setup-dns.sh) can recreate $RESOLV_CONF as a
    # symlink (e.g. back to /run/systemd/resolve/stub-resolv.conf). Unlink
    # first so `cat >` replaces it instead of writing through the symlink.
    rm -f "$RESOLV_CONF"
    {
        echo "# VoIPBin Sandbox - DNS via CoreDNS"
        echo "# CoreDNS handles *.voipbin.test locally and forwards others upstream."
        echo "# Fallback nameservers below are used if CoreDNS is unreachable."
        echo "# To restore: sudo ./scripts/setup-dns.sh --uninstall"
        echo "nameserver 127.0.0.1"
        [[ -n "$upstream_lines" ]] && echo "$upstream_lines"
        echo "options timeout:1 attempts:2"
    } > "$RESOLV_CONF"
}

# =============================================================================
# Compose Project Name Derivation
# =============================================================================
# Shared by setup-host.sh, setup-voip-network.sh, start.sh, and doctor.sh so a
# renamed checkout directory (e.g. sandbox -> self-install) derives a
# consistent Compose project name everywhere, instead of some places
# hardcoding the literal "sandbox".
#
# Mirrors docker compose's own derivation: COMPOSE_PROJECT_NAME when set
# (compose honors it too), else the project directory's basename. Real
# compose (v2) does NOT sanitize an explicit COMPOSE_PROJECT_NAME — it
# hard-errors on invalid values — so an override is validated and used as-is.
# Only the directory-basename derivation is normalized to compose's rules
# (lowercase, keep only [a-z0-9_-], strip leading chars until it starts with
# [a-z0-9]).
#
# Exit codes distinguish the two failure modes so callers can print an
# accurate message instead of guessing:
#   2 - an explicit COMPOSE_PROJECT_NAME is set but invalid
#   1 - no override is set and the directory basename filtered down to
#       nothing (e.g. "---" or "!!!"); still fails loudly rather than
#       silently producing an empty project name
derive_compose_project_name() {
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        [[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || return 2
        printf '%s' "$COMPOSE_PROJECT_NAME"
        return 0
    fi
    local derived
    derived=$(basename "$PROJECT_DIR" \
        | tr '[:upper:]' '[:lower:]' \
        | tr -cd 'a-z0-9_-' \
        | sed 's/^[-_]*//')
    [[ -n "$derived" ]] || return 1
    printf '%s' "$derived"
}

# =============================================================================
# OS Detection
# =============================================================================
detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "macos" ;;
        *)       echo "unknown" ;;
    esac
}
