#!/bin/bash
# Custom entrypoint for asterisk-call in sandbox
# Updates entityid to match container's MAC address for asterisk-proxy compatibility
# Adds outbound proxy configuration for routing through Kamailio

set -e

echo "Sandbox Asterisk Call entrypoint starting..."

# Detect target interface and MAC address
TARGET_INTERFACE=$(ls /sys/class/net | grep -v "lo" | head -n 1)
MAC_FILE="/sys/class/net/$TARGET_INTERFACE/address"

if [ -f "$MAC_FILE" ]; then
    MAC_ADDRESS=$(cat "$MAC_FILE")
    echo "[INFO] MAC Address detected: $MAC_ADDRESS (Interface: $TARGET_INTERFACE)"
else
    echo "[CRITICAL ERROR] Failed to detect MAC address."
    exit 1
fi

HOSTNAME=$(hostname)
KAMAILIO_ADDR="${KAMAILIO_OUTBOUND_ADDR:-host.docker.internal:5060}"

echo "[INFO] Hostname: $HOSTNAME"
echo "[INFO] Kamailio outbound proxy: $KAMAILIO_ADDR"

# -----------------------------------------------------------
# Run the original config generation scripts (but don't start asterisk)
# -----------------------------------------------------------

echo "Generating Kamailio dynamic config..."
python3 /k8s_asterisk_kamailio.py "$HOSTNAME" || true

echo "Generating endpoints config..."
python3 /k8s_asterisk_endpoints.py --hostname "$HOSTNAME" || {
    echo "[ERROR] Failed to generate endpoints config."
    exit 1
}

# Configure Realtime DB if enabled
if [ -n "$ENABLE_REALTIME" ]; then
    echo "Configuring Asterisk Realtime Architecture (MySQL)..."
    python3 /k8s_asterisk_realtime.py \
        --db-host "$DATABASE_HOSTNAME" \
        --db-port "${DATABASE_PORT:-3306}" \
        --db-name "$DATABASE_NAME" \
        --db-user "$DATABASE_USERNAME" \
        --db-pass "$DATABASE_PASSWORD" || {
        echo "[CRITICAL ERROR] Failed to configure Realtime DB."
        exit 1
    }
fi

# Apply MAC address and hostname substitutions
sed -i "s/VOIPBIN_MAC_ADDRESS/$MAC_ADDRESS/g" /etc/asterisk/*
sed -i "s/VOIPBIN_HOSTNAME/$HOSTNAME/g" /etc/asterisk/*

# -----------------------------------------------------------
# Apply sandbox-specific customizations AFTER config generation
# -----------------------------------------------------------

# Update entityid in asterisk.conf to match container's MAC address
# This ensures asterisk-proxy queue name matches what call-manager expects
if grep -q "entityid" /etc/asterisk/asterisk.conf; then
    echo "[INFO] Updating entityid in asterisk.conf to: $MAC_ADDRESS"
    sed -i "s/entityid = .*/entityid = $MAC_ADDRESS/" /etc/asterisk/asterisk.conf
fi

# Add outbound_proxy to call-out endpoint for routing through Kamailio
# This ensures all outbound calls go through Kamailio SIP proxy
echo "[INFO] Adding outbound_proxy to call-out endpoint..."

# Check if outbound_proxy already exists
if ! grep -q "outbound_proxy" /etc/asterisk/pjsip_endpoints.conf; then
    # Add outbound_proxy after the last line of [call-out] section
    # Also switch to UDP transport since Kamailio on host network uses UDP
    sed -i '/^\[call-out\]/,/^$/{
        s/^transport=transport-tcp$/transport=transport-udp/
        /^rtp_timeout=/a outbound_proxy=sip:'"$KAMAILIO_ADDR"'\\;lr
    }' /etc/asterisk/pjsip_endpoints.conf
    echo "[INFO] Added outbound_proxy to call-out endpoint"
else
    echo "[INFO] outbound_proxy already configured"
fi

# Verify configuration
echo "[INFO] Current entityid:"
grep "entityid" /etc/asterisk/asterisk.conf || echo "(not found)"

echo "[INFO] call-out endpoint config:"
sed -n '/^\[call-out\]/,/^\[/p' /etc/asterisk/pjsip_endpoints.conf | head -15

# -----------------------------------------------------------
# Start services
# -----------------------------------------------------------

# Create recording directory
mkdir -p /var/spool/asterisk/recording

# Start asterisk-exporter
/asterisk-exporter -web_listen_address ":2112" &

# Start asterisk
echo "[INFO] Starting Asterisk..."
exec /usr/sbin/asterisk -fvvvvvvvg
