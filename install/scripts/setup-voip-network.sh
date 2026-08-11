#!/bin/bash
# Setup VoIP network interfaces for host-mode services
# This script:
# 1. Creates veth pairs on the default bridge for internal communication
#    (VOIP-1331 - was macvlan, see create_internal_interfaces() below for why)
# 2. Optionally adds a secondary IP to the host's physical interface for external SIP
#
# External IP can be configured via:
#   - KAMAILIO_EXTERNAL_IP environment variable
#   - Or passed as argument: ./setup-voip-network.sh --external-ip 192.168.45.160

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on — deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

# Derived the same way docker compose derives it (shared, common.sh) so a
# renamed checkout directory doesn't hardcode "sandbox".
COMPOSE_PROJECT="$(derive_compose_project_name)" || {
    derive_rc=$?
    if [[ "$derive_rc" -eq 2 ]]; then
        log_error "invalid COMPOSE_PROJECT_NAME \"${COMPOSE_PROJECT_NAME:-}\": project names must consist only of lowercase alphanumeric characters, hyphens, and underscores as well as start with a letter or number"
    else
        log_error "could not derive a compose project name from $PROJECT_DIR (set COMPOSE_PROJECT_NAME)"
    fi
    exit 1
}
NETWORK_NAME="${COMPOSE_PROJECT}_default"

# Interface configurations for internal network
declare -A INTERNAL_INTERFACES=(
    ["kamailio-int"]="10.100.0.200"
    ["rtpengine-int"]="10.100.0.201"
)

# External IP configuration (read from .env or argument)
EXTERNAL_IP=""
EXTERNAL_INTERFACE=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Detect host's physical network interface
detect_physical_interface() {
    # Get the interface used for default route
    local iface=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'dev \K\S+' | head -1)
    echo "$iface"
}

# Get current IP of an interface
get_interface_ip() {
    local iface="$1"
    ip addr show "$iface" 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1
}

# Load external IPs from .env if not set
load_external_ips() {
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        if [[ -z "$EXTERNAL_IP" ]]; then
            EXTERNAL_IP=$(grep '^KAMAILIO_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
        fi
        if [[ -z "$RTPENGINE_EXTERNAL_IP" ]]; then
            RTPENGINE_EXTERNAL_IP=$(grep '^RTPENGINE_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
        fi
        if [[ -z "$EXTERNAL_IP_PINNED" ]]; then
            EXTERNAL_IP_PINNED=$(grep '^EXTERNAL_IP_PINNED=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
        fi
    fi
}

# Setup external IP on physical interface
setup_external_ip() {
    local ext_ip="$1"
    local iface="$2"

    if [[ -z "$ext_ip" || -z "$iface" ]]; then
        return 0
    fi

    echo ""
    log_info "Configuring external VoIP IP on $iface..."

    # Pinned IPs (--kamailio-ip/--rtpengine-ip at init time) are typically
    # hosting-provider-registered routed addresses that need provider-specific
    # wiring this script cannot know (per-IP gateway/netmask, MAC binding via
    # the provider's control panel, a dedicated macvlan device, etc. — see
    # README.md "Hosting-provider routed IPs"). Blindly `ip addr add`-ing them
    # onto $iface here duplicates whatever the operator already configured
    # instead of skipping cleanly, which is worse than doing nothing.
    # Case-insensitive: same fail-closed rationale as init.sh's
    # --force-reinit guard and common.sh's update_env_ips().
    if [[ "${EXTERNAL_IP_PINNED,,}" == "true" ]]; then
        if ip addr show 2>/dev/null | grep -q "inet ${ext_ip}/"; then
            log_info "  $ext_ip is pinned (EXTERNAL_IP_PINNED=true) and already present on the host — leaving as-is"
        else
            log_warn "  $ext_ip is pinned (EXTERNAL_IP_PINNED=true) but not found on any interface"
            log_warn "  This script does not auto-configure pinned IPs; see README.md 'Hosting-provider routed IPs'"
        fi
        return 0
    fi

    # Check if IP already exists anywhere on the host (not just $iface) —
    # e.g. a previous run, or an operator-managed interface — to avoid
    # binding the same address to two interfaces at once.
    if ip addr show 2>/dev/null | grep -q "inet ${ext_ip}/"; then
        log_info "  External IP $ext_ip already configured on the host"
        return 0
    fi

    # Get the subnet mask from existing IP
    local existing_cidr=$(ip addr show "$iface" | grep -oP 'inet \K[\d./]+' | head -1)
    local netmask=$(echo "$existing_cidr" | cut -d'/' -f2)
    netmask="${netmask:-24}"

    local ext_cidr="${ext_ip}/${netmask}"

    # Add the secondary IP
    log_info "  Adding secondary IP $ext_cidr to $iface..."
    ip addr add "$ext_cidr" dev "$iface" 2>/dev/null || {
        log_warn "  Failed to add IP (may already exist)"
    }

    log_info "  External VoIP IP configured: $ext_ip on $iface"
}

# Check if host IP has changed and update .env / DNS / certs if needed.
# Wrapped in a function (rather than left as top-level inline code) so it's
# independently callable from bats via tests/test_helper.bash's
# load_network_functions (VOIP-1285) — it extracts this script only up to
# the `parse_args "$@"` call below, so anything meant to be testable must
# be defined as a function above that line.
handle_ip_change() {
    IP_CHANGED=false
    if check_ip_changed; then
        local current_ip=$(detect_current_host_ip)
        local configured_ip=$(get_configured_host_ip)
        log_warn "Host IP changed: $configured_ip -> $current_ip"
        log_info "Updating .env with new IPs..."
        update_env_ips "$current_ip"

        # Regenerate SSL certificate
        regenerate_ssl_certs "$current_ip"

        # Regenerate CoreDNS config + DNS fallback upstreams (internal
        # mode only — this script runs in both modes under
        # setup-host.sh, and external mode deploys no coredns, §2.4)
        if [[ "$(get_domain_mode "$PROJECT_DIR/.env")" == "internal" ]]; then
            local kamailio_ip=$(grep '^KAMAILIO_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
            generate_coredns_config "$current_ip" "$PROJECT_DIR/config/coredns" "$kamailio_ip"
            log_info "CoreDNS configuration regenerated"

            # Host IP changed -> refresh DNS fallback upstreams too (a
            # new gateway likely means a new upstream DNS server).
            capture_dns_upstreams refresh
            write_resolv_conf_with_fallback
        else
            log_info "External mode: operator DNS may need updating (HOST_EXTERNAL_IP changed)"
        fi

        IP_CHANGED=true
    fi
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --external-ip)
                EXTERNAL_IP="$2"
                shift 2
                ;;
            --interface)
                EXTERNAL_INTERFACE="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
}

# Parse arguments
parse_args "$@"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

# Check if host IP has changed and update .env / DNS / certs if needed
handle_ip_change

# Load external IPs from .env (may have just been updated)
load_external_ips

# Check if Docker network exists
if ! docker network inspect "$NETWORK_NAME" &>/dev/null; then
    log_error "Docker network '$NETWORK_NAME' does not exist."
    log_info "Run 'docker compose up -d' first to create the network."
    exit 1
fi

# Get the bridge interface name
NETWORK_ID=$(docker network inspect "$NETWORK_NAME" -f '{{.Id}}' | cut -c1-12)
BRIDGE_IF="br-${NETWORK_ID}"

if ! ip link show "$BRIDGE_IF" &>/dev/null; then
    log_error "Bridge interface '$BRIDGE_IF' not found"
    exit 1
fi

log_info "Found bridge interface: $BRIDGE_IF"

# create_internal_interfaces <bridge-if>
# Creates/reconfigures each entry in INTERNAL_INTERFACES as a veth pair
# (VOIP-1331): one end enslaved directly to the compose bridge (exactly how
# Docker attaches every container's own veth), the other kept in the host
# namespace with the static IP. This used to be a macvlan interface with the
# bridge as its parent - that setup has a kernel-level asymmetry where NEW
# inbound TCP connections from OTHER bridge ports (i.e. from containers) to
# the macvlan child are silently dropped (confirmed via tcpdump: the frame
# reaches the bridge but never arrives at the macvlan child), while
# macvlan-initiated traffic works fine. That broke asterisk-call's outbound
# INVITE to Kamailio (call-manager could create the channel, but the SIP
# packet never left the host). A veth pair has no such asymmetry: from the
# bridge's perspective this interface is indistinguishable from any other
# container's own network attachment. Extracted into its own function (was
# inline top-level code) specifically so it's unit-testable.
create_internal_interfaces() {
    local bridge_if="$1"

    for INTERFACE_NAME in "${!INTERNAL_INTERFACES[@]}"; do
        local ip_addr="${INTERNAL_INTERFACES[$INTERFACE_NAME]}"
        local ip_cidr="${ip_addr}/16"

        echo ""
        log_info "Configuring $INTERFACE_NAME ($ip_addr)..."

        # "-br" suffix, not "-int-br": Linux interface names are capped at
        # IFNAMSIZ-1 = 15 chars. "${INTERFACE_NAME}-br" would make
        # "rtpengine-int-br" (16 chars), which `ip link add ... peer name`
        # rejects outright ("wrong: name not a valid ifname") - and since
        # this whole function runs under `set -e`, that failure would abort
        # BOTH interfaces, not just the long one. Stripping the "-int"
        # suffix keeps both current names comfortably under the limit
        # (kamailio-br=11, rtpengine-br=12); the explicit guard below turns
        # any future interface name that doesn't fit into a loud, immediate
        # error instead of a confusing kernel-level failure. Computed and
        # checked BEFORE any existence probe/delete below (VOIP-1331 review
        # round 3, LOW) so a future over-long name fails loudly up front
        # instead of after this interface has already been torn down.
        local br_peer_name="${INTERFACE_NAME%-int}-br"
        if (( ${#br_peer_name} > 15 )); then
            log_error "peer name '$br_peer_name' (derived from '$INTERFACE_NAME') exceeds the 15-char Linux interface name limit (IFNAMSIZ) - rename the interface or shorten the '-br' suffix scheme"
            exit 1
        fi

        # Check if interface already exists
        if ip link show "$INTERFACE_NAME" &>/dev/null; then
            # VOIP-1331: force-migrate a pre-existing LEGACY MACVLAN interface
            # (the old, broken design - see the comment above this function)
            # even when its IP already matches. An IP-only check would
            # otherwise skip re-creation forever on any host that already
            # ran the old macvlan-based script, silently leaving the bug in
            # place despite this fix being "applied".
            if ip -d link show "$INTERFACE_NAME" 2>/dev/null | grep -q 'macvlan'; then
                log_warn "  Found a legacy macvlan interface (VOIP-1331) - removing to recreate as a veth pair..."
                ip link delete "$INTERFACE_NAME"
            else
                local current_ip
                current_ip=$(ip addr show "$INTERFACE_NAME" | grep -oP 'inet \K[\d.]+' || echo "")
                if [[ "$current_ip" == "$ip_addr" ]]; then
                    # VOIP-1331 review round 3, HIGH: unlike the old macvlan
                    # design (killed automatically by the kernel when its
                    # parent bridge disappeared), a veth pair survives
                    # `docker compose down`/`voipbin> clean` orphaned, with
                    # its bridge-side peer's `master` cleared - reachable via
                    # this repo's own documented clean → start cycle (a new
                    # compose network gets a new bridge name on recreate).
                    # An IP-only "already configured" check would then skip
                    # re-enslaving it forever, leaving Kamailio/RTPEngine
                    # silently cut off from every container while every gate
                    # in this repo (doctor.sh, check-install.sh, this
                    # script) reports success. Re-assert enslavement
                    # unconditionally on this path - `ip link set ... master`
                    # is idempotent when already correct, and repairs the
                    # orphan in place otherwise.
                    log_info "  Interface already configured with correct IP - re-asserting bridge enslavement..."
                    ip link set "$br_peer_name" master "$bridge_if"
                    ip link set "$br_peer_name" up
                    continue
                else
                    log_warn "  Removing existing interface to reconfigure..."
                    ip link delete "$INTERFACE_NAME"
                fi
            fi
        fi

        log_info "  Creating veth pair on '$bridge_if'..."
        ip link add "$INTERFACE_NAME" type veth peer name "$br_peer_name"
        ip link set "$br_peer_name" master "$bridge_if"
        ip link set "$br_peer_name" up

        # Assign IP address
        log_info "  Assigning IP address $ip_cidr..."
        ip addr add "$ip_cidr" dev "$INTERFACE_NAME"

        # Bring interface up
        log_info "  Bringing interface up..."
        ip link set "$INTERFACE_NAME" up

        log_info "  Done: $INTERFACE_NAME = $ip_addr"
    done
}
create_internal_interfaces "$BRIDGE_IF"

# Verify all internal interfaces
echo ""
log_info "Verifying internal interfaces..."
echo "----------------------------------------"
for INTERFACE_NAME in "${!INTERNAL_INTERFACES[@]}"; do
    ip addr show "$INTERFACE_NAME" 2>/dev/null | head -4
    echo ""
done
echo "----------------------------------------"

# Detect physical interface if not specified
if [[ -z "$EXTERNAL_INTERFACE" ]]; then
    EXTERNAL_INTERFACE=$(detect_physical_interface)
fi

# Setup Kamailio external IP if configured
if [[ -n "$EXTERNAL_IP" && -n "$EXTERNAL_INTERFACE" ]]; then
    setup_external_ip "$EXTERNAL_IP" "$EXTERNAL_INTERFACE"
fi

# Setup RTPEngine external IP if configured
if [[ -n "$RTPENGINE_EXTERNAL_IP" && -n "$EXTERNAL_INTERFACE" ]]; then
    setup_external_ip "$RTPENGINE_EXTERNAL_IP" "$EXTERNAL_INTERFACE"
fi

echo ""
log_info "VoIP network interfaces configured successfully!"
echo ""
log_info "Internal interfaces (default bridge: $BRIDGE_IF):"
log_info "  kamailio-int:  10.100.0.200 (Kamailio internal)"
log_info "  rtpengine-int: 10.100.0.201 (RTPEngine internal)"

if [[ -n "$EXTERNAL_INTERFACE" ]]; then
    echo ""
    log_info "External interface ($EXTERNAL_INTERFACE):"
    if [[ -n "$EXTERNAL_IP" ]]; then
        log_info "  Kamailio IP:  $EXTERNAL_IP (SIP signaling)"
    fi
    if [[ -n "$RTPENGINE_EXTERNAL_IP" ]]; then
        log_info "  RTPEngine IP: $RTPENGINE_EXTERNAL_IP (RTP media)"
    fi
fi

echo ""
log_info "Restart services to use new interfaces:"
log_info "  docker compose restart kamailio rtpengine"

# Restart api-manager if IP changed to pick up new certificate
if [[ "$IP_CHANGED" == "true" ]]; then
    if docker ps --format '{{.Names}}' | grep -q "voipbin-api-mgr"; then
        echo ""
        log_info "Restarting api-manager to apply new certificate..."
        cd "$PROJECT_DIR" && docker compose rm -sf api-manager && docker compose up -d api-manager
    fi
fi
