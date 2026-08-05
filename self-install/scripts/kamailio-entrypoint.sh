#!/bin/bash
# Wrapper entrypoint for Kamailio in sandbox environment
# Creates symlinks for files that Kamailio expects in default locations
# Modifies config for local development (removes conflicting port listeners)

set -e

CONFIG_DIR="/config"
KAMAILIO_ETC="/usr/local/etc/kamailio"

echo "Sandbox Kamailio entrypoint starting..."

# Create symlinks for files expected in default locations
if [ -f "${CONFIG_DIR}/dispatcher.list" ]; then
    mkdir -p "${KAMAILIO_ETC}"
    ln -sf "${CONFIG_DIR}/dispatcher.list" "${KAMAILIO_ETC}/dispatcher.list"
    echo "Created symlink for dispatcher.list"
fi

if [ -f "${CONFIG_DIR}/pstn_whitelist.txt" ]; then
    ln -sf "${CONFIG_DIR}/pstn_whitelist.txt" "${KAMAILIO_ETC}/pstn_whitelist.txt"
    echo "Created symlink for pstn_whitelist.txt"
fi

# For local development: copy config to writable location and modify
# Remove port 80 listener which conflicts with host services
LOCAL_CONFIG="/tmp/kamailio_local"
mkdir -p "${LOCAL_CONFIG}"
cp -r ${CONFIG_DIR}/* "${LOCAL_CONFIG}/"
chmod -R 755 "${LOCAL_CONFIG}"

# Modify the config to use port 8080 instead of 80 for HTTP
# and add log_stderror for debugging
if [ -f "${LOCAL_CONFIG}/kamailio.cfg" ]; then
    # Replace port 80 with 8080 for HTTP/WebSocket listeners
    sed -i 's/:80/:8080/g' "${LOCAL_CONFIG}/kamailio.cfg"
    # Also update the advertise addresses
    sed -i 's/advertise.*:80$/advertise EXTERNAL_LB_ADDR:8080/g' "${LOCAL_CONFIG}/kamailio.cfg"
    # Enable stderr logging for debugging
    sed -i 's/log_stderror=no/log_stderror=yes/' "${LOCAL_CONFIG}/kamailio.cfg"

    # Fix loop detection for local development (same-host softphone testing)
    # Only detect loops when source port is a Kamailio listening port
    sed -i 's/if (dst_ip == myself && src_ip == myself) {/if (dst_ip == myself \&\& src_ip == myself \&\& ($sp == 5060 || $sp == 5061 || $sp == 443 || $sp == 8080)) {/' "${LOCAL_CONFIG}/kamailio.cfg"

    # Domain validation - keep default .voipbin.net for call-manager compatibility
    # The call-manager has hardcoded .voipbin.net domain checks

    echo "Modified kamailio.cfg for local development (port 80 -> 8080, relaxed loop detection, .localhost domain)"
fi

# Update health check endpoint
echo "Kamailio config prepared in ${LOCAL_CONFIG}"

# Call the original entrypoint with modified config path
exec /usr/local/bin/entrypoint.sh kamailio -DD -E -f "${LOCAL_CONFIG}/kamailio.cfg"
