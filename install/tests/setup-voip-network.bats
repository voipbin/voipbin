#!/usr/bin/env bats
# Tests for scripts/setup-voip-network.sh

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# handle_ip_change() tests (VOIP-1285)
# =============================================================================
# handle_ip_change() was extracted from top-level inline code into a
# function specifically so it's reachable here via load_network_functions
# (which only extracts up to the `parse_args "$@"` line).

setup_ip_change_test_paths() {
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    RESOLV_UPSTREAMS="$TEST_TEMP_DIR/resolv.conf.voipbin-upstreams"
    SYSTEMD_RESOLVE_CONF="$TEST_TEMP_DIR/systemd-resolve.conf"
    export RESOLV_CONF RESOLV_UPSTREAMS SYSTEMD_RESOLVE_CONF
}

@test "handle_ip_change refreshes DNS fallback upstreams when IP changed (internal mode)" {
    setup_ip_change_test_paths
    create_env_file \
        "DOMAIN_MODE=internal" \
        "TLS_MODE=byo" \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108"
    mock_ip_route "192.168.1.200"  # differs from configured HOST_EXTERNAL_IP
    load_network_functions

    handle_ip_change

    assert_equal "$IP_CHANGED" "true"
    assert_file_contains "$RESOLV_CONF" "nameserver 127.0.0.1"
    [[ -f "$RESOLV_UPSTREAMS" ]]
    assert_file_contains "$PROJECT_DIR/config/coredns/Corefile" "192.168.1.200"
}

@test "handle_ip_change does not touch DNS in external mode even when IP changed" {
    setup_ip_change_test_paths
    create_env_file \
        "DOMAIN_MODE=external" \
        "TLS_MODE=byo" \
        "HOST_EXTERNAL_IP=192.168.1.100"
    mock_ip_route "192.168.1.200"
    load_network_functions

    handle_ip_change

    assert_equal "$IP_CHANGED" "true"
    [[ ! -f "$RESOLV_CONF" ]]
    [[ ! -f "$RESOLV_UPSTREAMS" ]]
}

@test "handle_ip_change is a no-op when the host IP is unchanged" {
    setup_ip_change_test_paths
    create_env_file \
        "DOMAIN_MODE=internal" \
        "TLS_MODE=byo" \
        "HOST_EXTERNAL_IP=192.168.1.100" \
        "KAMAILIO_EXTERNAL_IP=192.168.1.108"
    mock_ip_route "192.168.1.100"  # matches configured HOST_EXTERNAL_IP
    load_network_functions

    handle_ip_change

    assert_equal "$IP_CHANGED" "false"
    [[ ! -f "$RESOLV_CONF" ]]
    [[ ! -f "$RESOLV_UPSTREAMS" ]]
}

# =============================================================================
# detect_physical_interface() tests
# =============================================================================

@test "detect_physical_interface returns interface from ip route" {
    mock_command_script "ip" '
if [[ "$1" == "route" && "$2" == "get" ]]; then
    echo "8.8.8.8 via 192.168.1.1 dev eth0 src 192.168.1.100 uid 1000"
fi
'
    load_network_functions

    result=$(detect_physical_interface)

    assert_equal "$result" "eth0"
}

@test "detect_physical_interface returns interface with different name" {
    mock_command_script "ip" '
if [[ "$1" == "route" && "$2" == "get" ]]; then
    echo "8.8.8.8 via 10.0.0.1 dev enp0s3 src 10.0.0.50 uid 1000"
fi
'
    load_network_functions

    result=$(detect_physical_interface)

    assert_equal "$result" "enp0s3"
}

@test "detect_physical_interface returns empty when ip route fails" {
    mock_command "ip" "" 1
    load_network_functions

    result=$(detect_physical_interface)

    [[ -z "$result" ]]
}

# =============================================================================
# get_interface_ip() tests
# =============================================================================

@test "get_interface_ip returns IP address of given interface" {
    mock_command_script "ip" '
if [[ "$1" == "addr" && "$2" == "show" && "$3" == "eth0" ]]; then
    echo "2: eth0: <BROADCAST,MULTICAST,UP> mtu 1500"
    echo "    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0"
fi
'
    load_network_functions

    result=$(get_interface_ip "eth0")

    assert_equal "$result" "192.168.1.100"
}

@test "get_interface_ip returns empty for non-existent interface" {
    mock_command "ip" "" 1
    load_network_functions

    result=$(get_interface_ip "nonexistent0")

    [[ -z "$result" ]]
}

# =============================================================================
# load_external_ips() tests
# =============================================================================

@test "load_external_ips sets EXTERNAL_IP from KAMAILIO_EXTERNAL_IP in .env" {
    create_env_file "KAMAILIO_EXTERNAL_IP=10.0.0.100"
    load_network_functions
    EXTERNAL_IP=""

    load_external_ips

    assert_equal "$EXTERNAL_IP" "10.0.0.100"
}

@test "load_external_ips sets RTPENGINE_EXTERNAL_IP from .env" {
    create_env_file "RTPENGINE_EXTERNAL_IP=10.0.0.101"
    load_network_functions
    RTPENGINE_EXTERNAL_IP=""

    load_external_ips

    assert_equal "$RTPENGINE_EXTERNAL_IP" "10.0.0.101"
}

@test "load_external_ips loads both IPs from .env" {
    create_env_file "KAMAILIO_EXTERNAL_IP=192.168.1.200" "RTPENGINE_EXTERNAL_IP=192.168.1.201"
    load_network_functions
    EXTERNAL_IP=""
    RTPENGINE_EXTERNAL_IP=""

    load_external_ips

    assert_equal "$EXTERNAL_IP" "192.168.1.200"
    assert_equal "$RTPENGINE_EXTERNAL_IP" "192.168.1.201"
}

@test "load_external_ips does not override EXTERNAL_IP if already set" {
    create_env_file "KAMAILIO_EXTERNAL_IP=10.0.0.100"
    load_network_functions
    EXTERNAL_IP="already.set.ip"

    load_external_ips

    assert_equal "$EXTERNAL_IP" "already.set.ip"
}

@test "load_external_ips does not override RTPENGINE_EXTERNAL_IP if already set" {
    create_env_file "RTPENGINE_EXTERNAL_IP=10.0.0.101"
    load_network_functions
    RTPENGINE_EXTERNAL_IP="already.set.rtp"

    load_external_ips

    assert_equal "$RTPENGINE_EXTERNAL_IP" "already.set.rtp"
}

@test "load_external_ips handles missing .env file gracefully" {
    # Don't create .env file
    load_network_functions
    EXTERNAL_IP=""
    RTPENGINE_EXTERNAL_IP=""

    # Should not error
    run load_external_ips

    [[ "$status" -eq 0 ]]
}

@test "load_external_ips handles .env with other variables" {
    create_env_file "SOME_OTHER_VAR=value" "KAMAILIO_EXTERNAL_IP=172.16.0.50" "ANOTHER_VAR=123"
    load_network_functions
    EXTERNAL_IP=""

    load_external_ips

    assert_equal "$EXTERNAL_IP" "172.16.0.50"
}

# =============================================================================
# setup_external_ip() pinned-IP gating (VOIP-1322)
# =============================================================================
# Pinned IPs (EXTERNAL_IP_PINNED=true) are hosting-provider-registered routed
# addresses wired by the operator (or init.sh's --kamailio-ip/--rtpengine-ip
# path) outside this script's knowledge. setup_external_ip() must not attempt
# `ip addr add` for them — that duplicated the address across two interfaces
# when a macvlan device already owned it (found running against a real
# ReliableSite dedicated server, 2026-08-11).

@test "load_external_ips loads EXTERNAL_IP_PINNED from .env" {
    create_env_file "KAMAILIO_EXTERNAL_IP=199.127.61.42" "RTPENGINE_EXTERNAL_IP=199.127.61.134" "EXTERNAL_IP_PINNED=true"
    load_network_functions
    EXTERNAL_IP=""; RTPENGINE_EXTERNAL_IP=""; EXTERNAL_IP_PINNED=""

    load_external_ips

    assert_equal "$EXTERNAL_IP_PINNED" "true"
}

@test "setup_external_ip skips ip addr add when pinned and IP already present on host" {
    load_network_functions
    mock_ip_addr_show_only "199.127.61.42"
    EXTERNAL_IP_PINNED="true"

    run setup_external_ip "199.127.61.42" "enp7s0"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"already present on the host"* ]]
    [[ "$output" != *"Adding secondary IP"* ]]
}

@test "setup_external_ip warns but does not fail when pinned IP is missing from host" {
    load_network_functions
    mock_ip_addr_show_only  # nothing present
    EXTERNAL_IP_PINNED="true"

    run setup_external_ip "199.127.61.42" "enp7s0"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"not found on any interface"* ]]
    [[ "$output" != *"Adding secondary IP"* ]]
}

@test "setup_external_ip still auto-configures when not pinned" {
    load_network_functions
    mock_ip_addr_show_only  # nothing present anywhere -> falls through to ip addr add
    EXTERNAL_IP_PINNED="false"

    run setup_external_ip "192.168.1.108" "enp7s0"

    # mock_ip_addr_show_only's "ip addr add ..." branch exits 1 (unmocked),
    # but setup_external_ip swallows that failure with a warning rather than
    # propagating a nonzero exit — assert on the log line, not $status.
    [[ "$output" == *"Adding secondary IP 192.168.1.108/24 to enp7s0"* ]]
}

# =============================================================================
# parse_args() tests
# =============================================================================

@test "parse_args sets EXTERNAL_IP from --external-ip argument" {
    load_network_functions
    EXTERNAL_IP=""

    parse_args --external-ip 192.168.5.100

    assert_equal "$EXTERNAL_IP" "192.168.5.100"
}

@test "parse_args sets EXTERNAL_INTERFACE from --interface argument" {
    load_network_functions
    EXTERNAL_INTERFACE=""

    parse_args --interface eth1

    assert_equal "$EXTERNAL_INTERFACE" "eth1"
}

@test "parse_args handles both arguments together" {
    load_network_functions
    EXTERNAL_IP=""
    EXTERNAL_INTERFACE=""

    parse_args --external-ip 10.0.0.50 --interface enp0s8

    assert_equal "$EXTERNAL_IP" "10.0.0.50"
    assert_equal "$EXTERNAL_INTERFACE" "enp0s8"
}

@test "parse_args handles arguments in reverse order" {
    load_network_functions
    EXTERNAL_IP=""
    EXTERNAL_INTERFACE=""

    parse_args --interface wlan0 --external-ip 172.20.0.1

    assert_equal "$EXTERNAL_IP" "172.20.0.1"
    assert_equal "$EXTERNAL_INTERFACE" "wlan0"
}

@test "parse_args ignores unknown arguments" {
    load_network_functions
    EXTERNAL_IP=""

    # Should not error with unknown args
    run parse_args --unknown-arg value --external-ip 1.2.3.4 --another-unknown

    [[ "$status" -eq 0 ]]
}

# =============================================================================
# INTERNAL_INTERFACES configuration tests
# =============================================================================
# Note: Bash associative arrays don't persist when sourced in test contexts.
# Instead, we verify the source file contains the expected configuration.

@test "INTERNAL_INTERFACES defines kamailio-int with 10.100.0.200" {
    # Verify the source file contains the expected configuration
    assert_file_contains "$SCRIPTS_DIR/setup-voip-network.sh" '["kamailio-int"]="10.100.0.200"'
}

@test "INTERNAL_INTERFACES defines rtpengine-int with 10.100.0.201" {
    assert_file_contains "$SCRIPTS_DIR/setup-voip-network.sh" '["rtpengine-int"]="10.100.0.201"'
}

@test "INTERNAL_INTERFACES declaration exists in script" {
    assert_file_contains "$SCRIPTS_DIR/setup-voip-network.sh" 'declare -A INTERNAL_INTERFACES'
}

# =============================================================================
# create_internal_interfaces() tests (VOIP-1331)
#
# kamailio-int/rtpengine-int used to be macvlan interfaces with the compose
# bridge as their parent - that has a kernel-level asymmetry where NEW
# inbound TCP connections from other bridge ports (containers) to the
# macvlan child are silently dropped, confirmed via tcpdump against a real
# bare-metal deployment. Fixed by switching to veth pairs, one end enslaved
# directly to the bridge (exactly how Docker attaches every container's own
# veth). These tests assert the actual `ip link` commands issued, not just
# the end-state file presence, since a veth pair with the peer end NOT
# enslaved to the bridge would look identical to `ip link show` but
# reproduce the exact same bug this fix exists to close.
# =============================================================================

stub_ip_capturing_calls() {
    local log_file="$1"
    local existing_iface="${2:-}"   # interface name that should appear as "already exists"
    local existing_ip="${3:-}"      # IP that existing_iface should report (for skip/reconfigure branch tests)
    local existing_type="${4:-}"    # "macvlan" to simulate a legacy pre-VOIP-1331 interface, else veth-like/unspecified
    mock_command_script "ip" "
echo \"ARGS: \$*\" >> '$log_file'
if [[ \"\$1\" == \"link\" && \"\$2\" == \"show\" ]]; then
    if [[ \"\$3\" == \"$existing_iface\" && -n \"$existing_iface\" ]]; then
        exit 0
    fi
    exit 1
fi
if [[ \"\$1\" == \"-d\" && \"\$2\" == \"link\" && \"\$3\" == \"show\" ]]; then
    if [[ \"\$4\" == \"$existing_iface\" && -n \"$existing_iface\" && \"$existing_type\" == \"macvlan\" ]]; then
        echo \"110: $existing_iface@br-abc123: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\"
        echo \"    macvlan mode bridge bcqueuelen 1000\"
        exit 0
    fi
    exit 0
fi
if [[ \"\$1\" == \"addr\" && \"\$2\" == \"show\" && \"\$3\" == \"$existing_iface\" && -n \"$existing_iface\" ]]; then
    echo \"    inet $existing_ip/16 scope global $existing_iface\"
    exit 0
fi
exit 0
"
}

@test "create_internal_interfaces creates a veth pair with the -br peer enslaved to the bridge (not macvlan)" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file"

    create_internal_interfaces "br-abc123"

    # Both interfaces created as veth pairs, never as macvlan.
    grep -q "ARGS: link add kamailio-int type veth peer name kamailio-br" "$log_file"
    grep -q "ARGS: link add rtpengine-int type veth peer name rtpengine-br" "$log_file"
    ! grep -q "macvlan" "$log_file"
}

@test "create_internal_interfaces enslaves the -br peer to the given bridge and brings it up" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file"

    create_internal_interfaces "br-abc123"

    grep -q "ARGS: link set kamailio-br master br-abc123" "$log_file"
    grep -q "ARGS: link set kamailio-br up" "$log_file"
    grep -q "ARGS: link set rtpengine-br master br-abc123" "$log_file"
    grep -q "ARGS: link set rtpengine-br up" "$log_file"
}

@test "create_internal_interfaces assigns the static IP and brings the host-side end up" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file"

    create_internal_interfaces "br-abc123"

    grep -q "ARGS: addr add 10.100.0.200/16 dev kamailio-int" "$log_file"
    grep -q "ARGS: link set kamailio-int up" "$log_file"
    grep -q "ARGS: addr add 10.100.0.201/16 dev rtpengine-int" "$log_file"
    grep -q "ARGS: link set rtpengine-int up" "$log_file"
}

@test "create_internal_interfaces skips creation for an interface already configured with the correct IP" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file" "kamailio-int" "10.100.0.200"

    run create_internal_interfaces "br-abc123"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"already configured with correct IP"* ]]
    ! grep -q "ARGS: link add kamailio-int " "$log_file"
    ! grep -q "ARGS: link delete kamailio-int" "$log_file"
    # The other interface (not pre-existing) is still created normally.
    grep -q "ARGS: link add rtpengine-int type veth peer name rtpengine-br" "$log_file"
}

@test "create_internal_interfaces re-asserts bridge enslavement on the skip path (VOIP-1331 review round 3, HIGH: orphaned veth peer repair)" {
    # Unlike macvlan (killed by the kernel when its parent bridge
    # disappears), a veth pair survives `docker compose down`/`voipbin>
    # clean` orphaned, with the bridge-side "-br" peer's `master` cleared -
    # reachable via this repo's own documented clean-then-start cycle. An
    # unconditional re-assertion on the "already configured" skip path
    # repairs that in place; this test locks it in regardless of whether
    # the peer actually is orphaned (the call is idempotent when it isn't).
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file" "kamailio-int" "10.100.0.200"

    run create_internal_interfaces "br-abc123"

    [[ "$status" -eq 0 ]]
    grep -q "ARGS: link set kamailio-br master br-abc123" "$log_file"
    grep -q "ARGS: link set kamailio-br up" "$log_file"
}

@test "create_internal_interfaces deletes and recreates an interface configured with the wrong IP" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file" "kamailio-int" "10.99.0.99"

    run create_internal_interfaces "br-abc123"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Removing existing interface to reconfigure"* ]]
    grep -q "ARGS: link delete kamailio-int" "$log_file"
    grep -q "ARGS: link add kamailio-int type veth peer name kamailio-br" "$log_file"
}

# --- Legacy macvlan migration (VOIP-1331 review finding HIGH) ---
#
# A host that already ran the pre-VOIP-1331 script has kamailio-int/
# rtpengine-int as macvlan interfaces already holding the correct IP. An
# IP-only "already configured" check would skip re-creating them forever,
# silently leaving the macvlan-on-bridge bug in place despite this fix
# being "applied". These tests force the migration even when the IP
# already matches.

@test "create_internal_interfaces force-migrates a legacy macvlan interface even when its IP already matches" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file" "kamailio-int" "10.100.0.200" "macvlan"

    run create_internal_interfaces "br-abc123"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"legacy macvlan interface"* ]]
    # Must NOT take the "already configured, skip" shortcut.
    [[ "$output" != *"already configured with correct IP"* ]]
    grep -q "ARGS: link delete kamailio-int" "$log_file"
    grep -q "ARGS: link add kamailio-int type veth peer name kamailio-br" "$log_file"
}

@test "create_internal_interfaces does not touch an interface that is already a veth pair with the correct IP" {
    load_create_internal_interfaces
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file" "kamailio-int" "10.100.0.200"   # existing_type unset = not macvlan

    run create_internal_interfaces "br-abc123"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"already configured with correct IP"* ]]
    ! grep -q "ARGS: link delete kamailio-int" "$log_file"
}

# --- IFNAMSIZ guard (VOIP-1331 review finding CRITICAL) ---
#
# "${INTERFACE_NAME}-br" for "rtpengine-int" would be "rtpengine-int-br"
# (16 chars), exceeding the kernel's 15-char interface name limit -
# `ip link add ... peer name` rejects it outright, and since the whole
# function runs under `set -e` in the real script, that failure aborts
# BOTH interfaces, not just the long one. The fix strips the "-int" suffix
# before appending "-br"; this test locks in the explicit guard that turns
# any FUTURE interface name that doesn't fit into a loud, immediate error
# instead of a confusing kernel-level failure two lines down.

@test "create_internal_interfaces refuses a peer name that would exceed the 15-char IFNAMSIZ limit" {
    load_create_internal_interfaces
    declare -gA INTERNAL_INTERFACES=(["some-really-long-interface-name"]="10.100.0.202")
    local log_file="$TEST_TEMP_DIR/ip.log"
    stub_ip_capturing_calls "$log_file"

    run create_internal_interfaces "br-abc123"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"exceeds the 15-char"* ]]
    ! grep -q "ARGS: link add" "$log_file"
}

@test "create_internal_interfaces still uses the %-int stripped peer-name scheme (not \${INTERFACE_NAME}-br)" {
    # VOIP-1331 review round 2, LOW: an earlier version of this test
    # recomputed "${name%-int}-br" independently in bash rather than
    # reading the script's own expression, so it stayed green even when
    # the script's fix (the whole reason the CRITICAL finding got closed)
    # was reverted back to the unsafe "${INTERFACE_NAME}-br" form. Assert
    # against the actual source line instead, so a regression here fails
    # this test directly rather than relying solely on the mocked-ip tests
    # above to catch it.
    assert_file_contains "$SCRIPTS_DIR/setup-voip-network.sh" 'local br_peer_name="${INTERFACE_NAME%-int}-br"'
}
