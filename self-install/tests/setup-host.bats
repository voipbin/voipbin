#!/usr/bin/env bats
# Tests for scripts/setup-host.sh (dual-mode DNS, VOIP-1275 Phase 3)
# Mode-aware step selection, CAROOT two-pass handoff shape, idempotent skip
# logging, and the .env precondition.
#
# Isolation rule: sub-scripts (setup-dns.sh, setup-voip-network.sh), mkcert,
# sudo and ip are ALL stubbed — nothing executes real root operations.

load 'test_helper'

setup() {
    setup_test_env
    # Deterministic: never inherit a SUDO_USER or a compose project override
    # from the invoking shell
    unset SUDO_USER
    unset COMPOSE_PROJECT_NAME

    # Stub sub-scripts: run_host_setup invokes "$SCRIPT_DIR/<script>", so tests
    # point SCRIPT_DIR at this stub directory after loading.
    STUB_SCRIPTS="$TEST_TEMP_DIR/stub_scripts"
    mkdir -p "$STUB_SCRIPTS"
    printf '#!/bin/bash\necho "STUB_SETUP_DNS $*"\n' > "$STUB_SCRIPTS/setup-dns.sh"
    printf '#!/bin/bash\necho "STUB_SETUP_NETWORK"\n' > "$STUB_SCRIPTS/setup-voip-network.sh"
    chmod +x "$STUB_SCRIPTS/setup-dns.sh" "$STUB_SCRIPTS/setup-voip-network.sh"
}

teardown() {
    teardown_test_env
}

# Common stub set: mkcert present with CA already trusted, interfaces absent,
# resolv.conf not pointing at CoreDNS, docker network absent (fresh host).
stub_default_host_state() {
    mock_command_script "mkcert" '
if [[ "$1" == "-check" ]]; then exit 0; fi
if [[ "$1" == "-CAROOT" ]]; then echo "/stub/caroot"; exit 0; fi
echo "MKCERT-INSTALL CAROOT=$CAROOT"
exit 0'
    mock_command_script "ip" 'exit 1'
    mock_command_script "docker" '
if [[ "$1" == "network" && "$2" == "inspect" ]]; then exit 1; fi
if [[ "$1" == "network" && "$2" == "create" ]]; then
    shift 2
    echo "DOCKER-NETWORK-CREATE $*" >> "'"$TEST_TEMP_DIR"'/docker.log"
    echo "stub-network-id"
    exit 0
fi
exit 1'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 8.8.8.8" > "$RESOLV_CONF"
}

# =============================================================================
# Mode step selection (design §2.5 step table)
# =============================================================================

@test "run_host_setup internal runs mkcert, ca-trust, dns and network steps" {
    load_setup_host_functions
    create_env_file "DOMAIN_MODE=internal" "HOST_EXTERNAL_IP=192.168.1.100" "KAMAILIO_EXTERNAL_IP=192.168.1.108"
    stub_default_host_state
    SCRIPT_DIR="$STUB_SCRIPTS"

    SETUP_HOST_STEPS=""
    run_host_setup internal > "$TEST_TEMP_DIR/out.log"

    assert_equal "$SETUP_HOST_STEPS" "mkcert:skipped,ca-trust:skipped,dns:done,docker-network:done,voip-network:done"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "STUB_SETUP_DNS -y"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "STUB_SETUP_NETWORK"
    # Corefile generation lives here (plan Phase 3 traceability note)
    [[ -f "$TEST_TEMP_DIR/config/coredns/Corefile" ]]
    assert_file_contains "$TEST_TEMP_DIR/config/coredns/Corefile" "192.168.1.108"
}

@test "run_host_setup external runs only the network steps (docker network + interfaces)" {
    load_setup_host_functions
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    stub_default_host_state
    SCRIPT_DIR="$STUB_SCRIPTS"
    # Differently-named project dir: the network name must be DERIVED from it
    # (compose normalization), not hardcoded to sandbox_default.
    PROJECT_DIR="$TEST_TEMP_DIR/My_Sandbox.42"
    mkdir -p "$PROJECT_DIR"

    SETUP_HOST_STEPS=""
    run_host_setup external > "$TEST_TEMP_DIR/out.log"

    assert_equal "$SETUP_HOST_STEPS" "docker-network:done,voip-network:done"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "STUB_SETUP_NETWORK"
    assert_file_not_contains "$TEST_TEMP_DIR/out.log" "STUB_SETUP_DNS"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "operator-managed"
    # No Corefile is ever generated in external mode
    [[ ! -f "$TEST_TEMP_DIR/config/coredns/Corefile" ]]
    # Fresh-host fix: the compose default network is created BEFORE the
    # interfaces step (setup-voip-network.sh needs its bridge interface).
    # Name derived from the project dir basename (My_Sandbox.42 → my_sandbox42).
    assert_file_contains "$TEST_TEMP_DIR/docker.log" "DOCKER-NETWORK-CREATE my_sandbox42_default"
}

# =============================================================================
# CAROOT two-pass handoff (design §2.5)
# =============================================================================

@test "step_install_ca_trust with SUDO_USER resolves user CAROOT and installs in two passes" {
    load_setup_host_functions
    # sudo stub: answers the CAROOT resolution with a clean path, echoes
    # everything else so the pass-2 command shape is assertable.
    mock_command_script "sudo" '
if [[ "$*" == *"mkcert -CAROOT"* ]]; then
    echo "/home/testuser/.local/share/mkcert"
else
    echo "SUDO:$*"
fi'
    mock_command_script "mkcert" '
if [[ "$1" == "-check" ]]; then exit 1; fi
echo "MKCERT-INSTALL CAROOT=$CAROOT"
exit 0'
    export SUDO_USER="testuser"

    run step_install_ca_trust

    [[ "$status" -eq 0 ]]
    # Pass 1: root install against the USER's CAROOT (not root's default)
    [[ "$output" == *'MKCERT-INSTALL CAROOT=/home/testuser/.local/share/mkcert'* ]]
    # Pass 2: user NSS store via sudo -u <user> -H env CAROOT=...
    [[ "$output" == *'SUDO:-u testuser -H env CAROOT=/home/testuser/.local/share/mkcert mkcert -install'* ]]
}

@test "step_install_ca_trust without SUDO_USER uses plain mkcert -install (never sudo -u '')" {
    load_setup_host_functions
    mock_command_script "sudo" 'echo "SUDO:$*"'
    mock_command_script "mkcert" '
if [[ "$1" == "-check" ]]; then exit 1; fi
echo "MKCERT-INSTALL CAROOT=$CAROOT"
exit 0'
    unset SUDO_USER

    run step_install_ca_trust

    [[ "$status" -eq 0 ]]
    # Plain install with root's default CAROOT (no CAROOT override)
    [[ "$output" == *'MKCERT-INSTALL CAROOT='* ]]
    [[ "$output" != *'CAROOT=/'* ]]
    # The user pass and the user-CAROOT resolution are both skipped entirely
    [[ "$output" != *'SUDO:'* ]]
}

# =============================================================================
# Idempotence: every step probes current state and logs a skip
# =============================================================================

@test "run_host_setup internal skips every step when host state is already configured" {
    load_setup_host_functions
    create_env_file "DOMAIN_MODE=internal" "HOST_EXTERNAL_IP=192.168.1.100" "KAMAILIO_EXTERNAL_IP=192.168.1.108"
    mock_command_script "mkcert" '
if [[ "$1" == "-check" ]]; then exit 0; fi
echo "MKCERT-INSTALL CAROOT=$CAROOT"
exit 0'
    mock_command_script "ip" 'exit 0'
    mock_command_script "docker" '
if [[ "$1" == "network" && "$2" == "inspect" ]]; then exit 0; fi
exit 1'
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 127.0.0.1" > "$RESOLV_CONF"
    SCRIPT_DIR="$STUB_SCRIPTS"
    # Derivation fixture: skip log must name the project-derived network
    PROJECT_DIR="$TEST_TEMP_DIR/My_Sandbox.42"
    mkdir -p "$PROJECT_DIR"

    SETUP_HOST_STEPS=""
    run_host_setup internal > "$TEST_TEMP_DIR/out.log"

    assert_equal "$SETUP_HOST_STEPS" "mkcert:skipped,ca-trust:skipped,dns:skipped,docker-network:skipped,voip-network:skipped"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "mkcert already installed, skipping"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "mkcert CA already installed, skipping"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "resolv.conf already points at CoreDNS (127.0.0.1), skipping"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "Docker network my_sandbox42_default already exists, skipping"
    assert_file_contains "$TEST_TEMP_DIR/out.log" "VoIP network interfaces already configured, skipping"
    assert_file_not_contains "$TEST_TEMP_DIR/out.log" "STUB_SETUP_DNS"
    assert_file_not_contains "$TEST_TEMP_DIR/out.log" "STUB_SETUP_NETWORK"
}

@test "step_setup_dns repairs coredns config ownership even when skipping" {
    load_setup_host_functions
    create_env_file "DOMAIN_MODE=internal"
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    echo "nameserver 127.0.0.1" > "$RESOLV_CONF"
    PROJECT_DIR="$TEST_TEMP_DIR"
    mkdir -p "$PROJECT_DIR/config/coredns"
    mock_command_script "chown" 'echo "STUB_CHOWN $*"'
    SUDO_USER="testuser"

    SETUP_HOST_STEPS=""
    run step_setup_dns

    [[ "$output" == *'already points at CoreDNS'* ]]
    [[ "$output" == *"STUB_CHOWN -R testuser $TEST_TEMP_DIR/config/coredns"* ]]
}

@test "step_setup_dns does not skip on a commented-out nameserver line" {
    load_setup_host_functions
    create_env_file "DOMAIN_MODE=internal" "HOST_EXTERNAL_IP=192.168.1.100" "KAMAILIO_EXTERNAL_IP=192.168.1.108"
    RESOLV_CONF="$TEST_TEMP_DIR/resolv.conf"
    printf '#nameserver 127.0.0.1\nnameserver 8.8.8.8\n' > "$RESOLV_CONF"
    SCRIPT_DIR="$STUB_SCRIPTS"
    PROJECT_DIR="$TEST_TEMP_DIR"

    SETUP_HOST_STEPS=""
    run step_setup_dns

    [[ "$output" != *'already points at CoreDNS'* ]]
    [[ "$output" == *'STUB_SETUP_DNS'* ]]
}

# =============================================================================
# Compose default network ensure (fresh-host fix, VOIP-1275)
# =============================================================================

@test "step_ensure_docker_network derives name/label from the project dir (compose normalization)" {
    load_setup_host_functions
    mock_command_script "docker" '
if [[ "$1" == "network" && "$2" == "inspect" ]]; then exit 1; fi
if [[ "$1" == "network" && "$2" == "create" ]]; then
    shift 2
    echo "DOCKER-NETWORK-CREATE $*" >> "'"$TEST_TEMP_DIR"'/docker.log"
    echo "stub-network-id"
    exit 0
fi
exit 1'
    # Compose-normalized derivation: lowercase, drop chars outside [a-z0-9_-]
    PROJECT_DIR="$TEST_TEMP_DIR/My_Sandbox.42"
    mkdir -p "$PROJECT_DIR"

    SETUP_HOST_STEPS=""
    run step_ensure_docker_network

    [[ "$status" -eq 0 ]]
    # Exactly what compose would create: derived name, bridge driver, the
    # compose file subnet, and the two labels compose validates on adoption.
    local created
    created=$(cat "$TEST_TEMP_DIR/docker.log")
    [[ "$created" == *'my_sandbox42_default'* ]]
    [[ "$created" == *'--driver bridge'* ]]
    [[ "$created" == *'--subnet 10.100.0.0/16'* ]]
    [[ "$created" == *'--label com.docker.compose.network=default'* ]]
    [[ "$created" == *'--label com.docker.compose.project=my_sandbox42'* ]]
}

@test "step_ensure_docker_network uses a valid COMPOSE_PROJECT_NAME as-is over the project dir" {
    load_setup_host_functions
    mock_command_script "docker" '
if [[ "$1" == "network" && "$2" == "inspect" ]]; then exit 1; fi
if [[ "$1" == "network" && "$2" == "create" ]]; then
    shift 2
    echo "DOCKER-NETWORK-CREATE $*" >> "'"$TEST_TEMP_DIR"'/docker.log"
    echo "stub-network-id"
    exit 0
fi
exit 1'
    PROJECT_DIR="$TEST_TEMP_DIR/My_Sandbox.42"
    mkdir -p "$PROJECT_DIR"
    # Compose honors COMPOSE_PROJECT_NAME over the directory name; so must we.
    # A valid override passes through unmodified (compose never normalizes it).
    export COMPOSE_PROJECT_NAME="custom-proj_1"

    SETUP_HOST_STEPS=""
    run step_ensure_docker_network

    [[ "$status" -eq 0 ]]
    local created
    created=$(cat "$TEST_TEMP_DIR/docker.log")
    [[ "$created" == *'custom-proj_1_default'* ]]
    [[ "$created" == *'--label com.docker.compose.project=custom-proj_1'* ]]
    [[ "$created" != *'my_sandbox42'* ]]
}

@test "step_ensure_docker_network dies on an invalid COMPOSE_PROJECT_NAME (compose does not sanitize)" {
    load_setup_host_functions
    mock_command_script "docker" '
if [[ "$1" == "network" && "$2" == "create" ]]; then
    echo "DOCKER-NETWORK-CREATE $*" >> "'"$TEST_TEMP_DIR"'/docker.log"
fi
exit 0'
    PROJECT_DIR="$TEST_TEMP_DIR/My_Sandbox.42"
    mkdir -p "$PROJECT_DIR"
    # Real compose (v2.40.3) hard-errors on this instead of sanitizing it;
    # normalizing here would create a network compose will never adopt.
    export COMPOSE_PROJECT_NAME="-Custom.Proj"

    SETUP_HOST_STEPS=""
    run step_ensure_docker_network

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'invalid COMPOSE_PROJECT_NAME'* ]]
    [[ "$output" == *'must consist only of lowercase alphanumeric characters, hyphens, and underscores as well as start with a letter or number'* ]]
    [[ "$output" == *'VOIPBIN_SETUP_HOST: status=error'* ]]
    # No network was created under the normalized (never-adopted) name
    [[ ! -f "$TEST_TEMP_DIR/docker.log" ]]
}

@test "step_ensure_docker_network skips when the network already exists" {
    load_setup_host_functions
    mock_command_script "docker" '
if [[ "$1" == "network" && "$2" == "inspect" ]]; then exit 0; fi
if [[ "$1" == "network" && "$2" == "create" ]]; then
    echo "DOCKER-NETWORK-CREATE $*" >> "'"$TEST_TEMP_DIR"'/docker.log"
    exit 0
fi
exit 1'

    SETUP_HOST_STEPS=""
    run step_ensure_docker_network

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'already exists, skipping'* ]]
    [[ ! -f "$TEST_TEMP_DIR/docker.log" ]]
}

# =============================================================================
# Preconditions + result-line grammar
# =============================================================================

@test "check_env_exists refuses without .env, pointing at init" {
    load_setup_host_functions
    rm -f "$ENV_FILE"

    run check_env_exists

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'run ./scripts/init.sh first'* ]]
    [[ "$output" == *'VOIPBIN_SETUP_HOST: status=error'* ]]
}

@test "setup-host.sh unprivileged refuses via check_root and still emits a result line" {
    if [[ $EUID -eq 0 ]]; then
        skip "test requires an unprivileged user"
    fi
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"

    run bash "$SCRIPTS_DIR/setup-host.sh"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *'must be run with sudo'* ]]
    local last_line
    last_line=$(echo "$output" | tail -1)
    [[ "$last_line" =~ ^VOIPBIN_SETUP_HOST:\ status=error ]]
}
