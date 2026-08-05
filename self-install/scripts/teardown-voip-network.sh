#!/bin/bash
# Teardown VoIP internal network interfaces
# Removes the macvlan interfaces created by setup-voip-network.sh

set -e

# Interface names to remove
INTERFACES=("kamailio-int" "rtpengine-int")

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

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

# Remove each interface
for INTERFACE_NAME in "${INTERFACES[@]}"; do
    if ! ip link show "$INTERFACE_NAME" &>/dev/null; then
        log_warn "Interface '$INTERFACE_NAME' does not exist. Skipping."
        continue
    fi

    log_info "Removing interface '$INTERFACE_NAME'..."
    ip link delete "$INTERFACE_NAME"
    log_info "  Done."
done

echo ""
log_info "VoIP internal network interfaces removed successfully!"
