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
