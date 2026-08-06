#!/bin/bash
# Setup VoIP network interfaces for host-mode services
# This script:
# 1. Creates macvlan interfaces on the default bridge for internal communication
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
    fi
}

# Setup external IP on physical interface
setup_external_ip() {
    local ext_ip="$1"
    local iface="$2"

    if [[ -z "$ext_ip" || -z "$iface" ]]; then
        return 0
    fi

    # Get the subnet mask from existing IP
    local existing_cidr=$(ip addr show "$iface" | grep -oP 'inet \K[\d./]+' | head -1)
    local netmask=$(echo "$existing_cidr" | cut -d'/' -f2)
    netmask="${netmask:-24}"

    local ext_cidr="${ext_ip}/${netmask}"

    echo ""
    log_info "Configuring external VoIP IP on $iface..."

    # Check if IP already exists on the interface
    if ip addr show "$iface" | grep -q "inet ${ext_ip}/"; then
        log_info "  External IP $ext_ip already configured on $iface"
        return 0
    fi

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

# Create each internal interface
for INTERFACE_NAME in "${!INTERNAL_INTERFACES[@]}"; do
    IP_ADDR="${INTERNAL_INTERFACES[$INTERFACE_NAME]}"
    IP_CIDR="${IP_ADDR}/16"

    echo ""
    log_info "Configuring $INTERFACE_NAME ($IP_ADDR)..."

    # Check if interface already exists
    if ip link show "$INTERFACE_NAME" &>/dev/null; then
        # Check if it has the correct IP
        CURRENT_IP=$(ip addr show "$INTERFACE_NAME" | grep -oP 'inet \K[\d.]+' || echo "")
        if [[ "$CURRENT_IP" == "$IP_ADDR" ]]; then
            log_info "  Interface already configured with correct IP"
            continue
        else
            log_warn "  Removing existing interface to reconfigure..."
            ip link delete "$INTERFACE_NAME"
        fi
    fi

    # Create macvlan interface
    log_info "  Creating macvlan interface on '$BRIDGE_IF'..."
    ip link add "$INTERFACE_NAME" link "$BRIDGE_IF" type macvlan mode bridge

    # Assign IP address
    log_info "  Assigning IP address $IP_CIDR..."
    ip addr add "$IP_CIDR" dev "$INTERFACE_NAME"

    # Bring interface up
    log_info "  Bringing interface up..."
    ip link set "$INTERFACE_NAME" up

    log_info "  Done: $INTERFACE_NAME = $IP_ADDR"
done

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
