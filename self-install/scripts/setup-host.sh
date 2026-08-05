#!/bin/bash
# VoIPBin Sandbox - Host Setup Script (design §2.5, VOIP-1275)
# The single sudo command in the AI-driven install flow. Owns every host
# mutation: mkcert package install, mkcert CA trust-store install (two-pass
# CAROOT handoff), DNS hijack (Corefile + setup-dns.sh), the compose default
# docker network (fresh hosts, before any compose up), and the VoIP macvlan
# interfaces (setup-voip-network.sh).
#
# Usage: sudo ./scripts/setup-host.sh
#
# Mode-aware (reads DOMAIN_MODE from .env, which must exist — run init first):
#   internal: mkcert package, CA trust, DNS, VoIP interfaces
#   external: VoIP interfaces only (TLS and DNS are operator-managed)
#
# Idempotent: each step probes current state and logs a skip when already done.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on — deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
# resolv.conf path (overridable so tests can stub host probes)
RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

# Compose default network subnet (must mirror the docker-compose.yml
# networks.default definition).
NETWORK_SUBNET="10.100.0.0/16"

# Compose project name: derive_compose_project_name() (shared, common.sh).
# The default network is then <project>_default with a
# com.docker.compose.project=<project> label, exactly what a later
# `docker compose up` expects to adopt.

# =============================================================================
# Result line + exit helpers (design §2.2 pattern, shared with init.sh)
# Every exit path — including set -e aborts — must end with a
# VOIPBIN_SETUP_HOST: status=ok|error line on stdout.
# Exit codes: 0 success; 1 validation/user error; 2 environment error.
# =============================================================================

# emit_result <status> [detail...]
emit_result() {
    local status="$1"
    shift
    if [[ $# -gt 0 ]]; then
        echo "VOIPBIN_SETUP_HOST: status=$status $*"
    else
        echo "VOIPBIN_SETUP_HOST: status=$status"
    fi
    SETUP_HOST_RESULT_EMITTED="true"
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
setup_host_exit_trap() {
    local code=$?
    if [[ "${SETUP_HOST_RESULT_EMITTED:-false}" != "true" ]]; then
        if [[ $code -eq 0 ]]; then
            echo "VOIPBIN_SETUP_HOST: status=ok"
        else
            echo "VOIPBIN_SETUP_HOST: status=error reason=\"host setup aborted (exit $code)\""
        fi
    fi
}

# =============================================================================
# Step bookkeeping
# =============================================================================

# record_step <name>:<done|skipped> — appended to the result line's steps= list
record_step() {
    if [[ -z "$SETUP_HOST_STEPS" ]]; then
        SETUP_HOST_STEPS="$1"
    else
        SETUP_HOST_STEPS="$SETUP_HOST_STEPS,$1"
    fi
}

# =============================================================================
# Preconditions
# =============================================================================

# .env must exist: mode selection lives there (design §2.5 — init first).
check_env_exists() {
    if [[ ! -f "$ENV_FILE" ]]; then
        die 1 "no .env at $ENV_FILE — run ./scripts/init.sh first"
    fi
}

# =============================================================================
# Steps (internal mode: all four; external mode: voip-network only)
# =============================================================================

# Step: install the mkcert package if missing (logic moved out of
# start.sh:setup_mkcert(), which now only checks presence — design §2.5).
step_install_mkcert() {
    if command -v mkcert &> /dev/null; then
        log_info "  mkcert already installed, skipping"
        record_step "mkcert:skipped"
        return 0
    fi

    log_info "  Installing mkcert..."
    if command -v apt &> /dev/null; then
        apt update && apt install -y mkcert libnss3-tools
    elif command -v brew &> /dev/null; then
        brew install mkcert
    else
        die 2 "could not install mkcert automatically — install it manually (https://github.com/FiloSottile/mkcert) and re-run"
    fi

    if ! command -v mkcert &> /dev/null; then
        die 2 "mkcert installation failed"
    fi
    record_step "mkcert:done"
}

# Step: install the mkcert CA into the trust stores.
#
# CAROOT two-pass handoff (design §2.5): under sudo, a plain 'mkcert -install'
# would resolve CAROOT to root's directory and trust a DIFFERENT CA than the
# one the unprivileged init.sh used to sign the certs — trust would silently
# never attach. So the invoking user's CAROOT is resolved explicitly and
# installed in two passes: once as root for the system store, once as the
# invoking user for the user's NSS store (Chrome/Firefox) — a root-only pass
# would write root's ~/.pki/nssdb and leave the user's browsers untrusting.
step_install_ca_trust() {
    if [[ -n "${SUDO_USER:-}" ]]; then
        local user_caroot
        user_caroot="$(sudo -u "$SUDO_USER" -H mkcert -CAROOT)"

        if CAROOT="$user_caroot" mkcert -check &> /dev/null; then
            log_info "  mkcert CA already installed, skipping"
            record_step "ca-trust:skipped"
            return 0
        fi

        log_info "  Installing mkcert CA (CAROOT=$user_caroot)..."
        # Pass 1: system trust store, as root, against the user's CAROOT
        CAROOT="$user_caroot" mkcert -install
        # Pass 2: the invoking user's NSS store
        sudo -u "$SUDO_USER" -H env CAROOT="$user_caroot" mkcert -install
    else
        # Direct root login: no invoking user to resolve — skip the user pass
        # AND the user-CAROOT resolution entirely (never execute sudo -u "").
        # Plain mkcert -install uses root's default CAROOT.
        if mkcert -check &> /dev/null; then
            log_info "  mkcert CA already installed, skipping"
            record_step "ca-trust:skipped"
            return 0
        fi

        log_info "  Installing mkcert CA (root default CAROOT)..."
        mkcert -install
    fi
    record_step "ca-trust:done"
}

# Restore invoking-user ownership of project files this root script wrote.
# Without this, unprivileged start.sh cannot regenerate the Corefile (its
# Step 7 rewrites it each internal-mode start). Idempotent; runs even when
# the DNS step skips, so a rerun repairs ownership left by an older run.
fix_coredns_ownership() {
    [[ -z "${SUDO_USER:-}" ]] && return 0
    [[ -d "$PROJECT_DIR/config/coredns" ]] || return 0
    chown -R "$SUDO_USER" "$PROJECT_DIR/config/coredns" 2>/dev/null || true
}

# Step: generate the Corefile and configure the OS resolver (setup-dns.sh -y).
# start.sh Step 7 regenerates the Corefile each internal-mode start anyway.
step_setup_dns() {
    if grep -qE "^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.1" "$RESOLV_CONF" 2>/dev/null; then
        log_info "  resolv.conf already points at CoreDNS (127.0.0.1), skipping DNS setup"
        fix_coredns_ownership
        record_step "dns:skipped"
        return 0
    fi

    local host_ip kamailio_ip
    host_ip=$(get_env_var "$ENV_FILE" HOST_EXTERNAL_IP)
    kamailio_ip=$(get_env_var "$ENV_FILE" KAMAILIO_EXTERNAL_IP)
    [[ -z "$host_ip" ]] && host_ip="127.0.0.1"
    [[ -z "$kamailio_ip" ]] && kamailio_ip="$host_ip"

    generate_coredns_config "$host_ip" "$PROJECT_DIR/config/coredns" "$kamailio_ip"
    log_info "  Generated CoreDNS Corefile (web → $host_ip, SIP → $kamailio_ip)"

    "$SCRIPT_DIR/setup-dns.sh" -y
    fix_coredns_ownership
    record_step "dns:done"
}

# Step: ensure the compose default network exists (both modes, probe-and-skip).
# setup-voip-network.sh derives its bridge interface from the compose default
# network, which on a fresh host does not exist until the first
# `docker compose up`. Internal mode usually creates it via the DNS step
# (setup-dns.sh runs `docker compose up -d coredns`), but external mode
# deploys no coredns and would reach the interfaces step with no network.
# The network is created exactly as compose would (derived project name,
# bridge driver, the compose file's subnet, and the
# com.docker.compose.network/project labels), which a later
# `docker compose up -d` adopts cleanly — verified: without these labels
# compose hard-errors ("was found but has incorrect label"), with them it
# adopts silently and even owns the network on `compose down`.
step_ensure_docker_network() {
    local project network_name derive_rc
    project="$(derive_compose_project_name)"
    derive_rc=$?
    if [[ "$derive_rc" -ne 0 ]]; then
        if [[ "$derive_rc" -eq 2 ]]; then
            die 1 "invalid COMPOSE_PROJECT_NAME \"${COMPOSE_PROJECT_NAME:-}\": project names must consist only of lowercase alphanumeric characters, hyphens, and underscores as well as start with a letter or number"
        else
            die 1 "could not derive a compose project name from $PROJECT_DIR (set COMPOSE_PROJECT_NAME)"
        fi
    fi
    network_name="${project}_default"

    if docker network inspect "$network_name" &> /dev/null; then
        log_info "  Docker network $network_name already exists, skipping"
        record_step "docker-network:skipped"
        return 0
    fi

    log_info "  Creating docker network $network_name (compose-compatible)..."
    if ! docker network create "$network_name" \
        --driver bridge \
        --subnet "$NETWORK_SUBNET" \
        --label "com.docker.compose.network=default" \
        --label "com.docker.compose.project=$project" > /dev/null; then
        die 2 "failed to create docker network $network_name"
    fi
    record_step "docker-network:done"
}

# Step: VoIP macvlan interfaces (both modes).
step_setup_network() {
    if ip link show kamailio-int &> /dev/null && ip link show rtpengine-int &> /dev/null; then
        log_info "  VoIP network interfaces already configured, skipping"
        record_step "voip-network:skipped"
        return 0
    fi

    "$SCRIPT_DIR/setup-voip-network.sh"
    record_step "voip-network:done"
}

# =============================================================================
# Mode-aware step table (design §2.5)
# =============================================================================

run_host_setup() {
    local mode="$1"

    if [[ "$mode" == "internal" ]]; then
        log_step "Checking mkcert package..."
        step_install_mkcert

        log_step "Checking mkcert CA trust..."
        step_install_ca_trust

        log_step "Checking DNS configuration..."
        step_setup_dns
    else
        log_info "External mode: TLS and DNS are operator-managed — skipping mkcert and DNS steps"
    fi

    log_step "Checking docker network..."
    step_ensure_docker_network

    log_step "Checking VoIP network interfaces..."
    step_setup_network
}

# =============================================================================
# Main
# =============================================================================

main() {
    # Result-line guarantee: registered here, not at top level (§2.2 pattern).
    trap setup_host_exit_trap EXIT

    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - Host Setup"
    echo "=============================================="
    echo ""

    check_root
    check_env_exists

    SETUP_HOST_STEPS=""
    local mode
    mode=$(get_domain_mode "$ENV_FILE")
    log_info "Install mode: $mode"

    run_host_setup "$mode"

    echo ""
    log_info "Host setup complete. Next: ./scripts/start.sh"
    emit_result ok "mode=$mode steps=$SETUP_HOST_STEPS"
}

main "$@"
