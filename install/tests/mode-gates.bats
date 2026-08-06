#!/usr/bin/env bats
# Mode-gate tests (dual-mode DNS, VOIP-1275 Phase 2)
# External-mode no-ops in the entry-point scripts and the COMPOSE_PROFILES
# conflict/stale-.env guard.
#
# Isolation rule: every test that runs a script able to reach docker /
# docker compose uses the suite's MOCK_BIN_DIR docker stub AND
# PROJECT_DIR=$TEST_TEMP_DIR — nothing may touch the real tree or daemon.

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

# =============================================================================
# setup-dns.sh external-mode no-op (design §2.3)
# =============================================================================

@test "setup-dns.sh exits 0 with operator-managed message in external mode" {
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    # Sudo-free: the gate exits before any root check. Docker stubbed anyway.
    mock_command "docker" ""

    run bash "$SCRIPTS_DIR/setup-dns.sh"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'external mode: DNS is operator-managed, skipping'* ]]
}

@test "setup-dns.sh --test also gated in external mode" {
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    mock_command "docker" ""

    run bash "$SCRIPTS_DIR/setup-dns.sh" --test

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'external mode: DNS is operator-managed, skipping'* ]]
}

@test "setup-dns.sh --uninstall is exempt from the external-mode gate" {
    # Leftover resolv.conf hijack state must remain removable in external
    # mode. Unprivileged, --uninstall proceeds past the gate and dies on the
    # root check instead of printing the skip message.
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="
    mock_command "docker" ""
    if [[ $EUID -eq 0 ]]; then
        skip "test requires an unprivileged user"
    fi

    run bash "$SCRIPTS_DIR/setup-dns.sh" --uninstall

    [[ "$status" -ne 0 ]]
    [[ "$output" != *'external mode: DNS is operator-managed, skipping'* ]]
    [[ "$output" == *'must be run with sudo'* ]]
}

@test "setup-dns.sh is not gated in internal mode (reaches root check)" {
    create_env_file "DOMAIN_MODE=internal" "BASE_DOMAIN=voipbin.test" "COMPOSE_PROFILES=internal-dns"
    mock_command "docker" ""
    if [[ $EUID -eq 0 ]]; then
        skip "test requires an unprivileged user"
    fi

    run bash "$SCRIPTS_DIR/setup-dns.sh"

    [[ "$output" != *'external mode: DNS is operator-managed, skipping'* ]]
    [[ "$output" == *'must be run with sudo'* ]]
}

# =============================================================================
# clean.sh --volumes removes the test-data marker (design §2.7)
# =============================================================================

@test "clean.sh --volumes removes .test_data_initialized" {
    # MANDATORY isolation: docker stub + PROJECT_DIR=$TEST_TEMP_DIR — without
    # both, this test would wipe the developer's live volumes.
    mock_command "docker" ""
    touch "$PROJECT_DIR/.test_data_initialized"

    run bash "$SCRIPTS_DIR/clean.sh" --volumes

    [[ "$status" -eq 0 ]]
    [[ ! -f "$PROJECT_DIR/.test_data_initialized" ]]
    [[ "$output" == *'Removing test data marker'* ]]
}

@test "clean.sh --purge alone keeps DB-state semantics (marker not its concern, .env removed)" {
    mock_command "docker" ""
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    touch "$PROJECT_DIR/.test_data_initialized"

    run bash "$SCRIPTS_DIR/clean.sh" --purge

    [[ "$status" -eq 0 ]]
    # marker follows the volumes, not the purge (moved out of --purge)
    [[ -f "$PROJECT_DIR/.test_data_initialized" ]]
    [[ ! -f "$PROJECT_DIR/.env" ]]
}

@test "clean.sh --dns is a no-op with message in external mode" {
    mock_command "docker" ""
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com" "COMPOSE_PROFILES="

    run bash "$SCRIPTS_DIR/clean.sh" --dns

    [[ "$status" -eq 0 ]]
    [[ "$output" == *'External mode: DNS is operator-managed, skipping'* ]]
}

# =============================================================================
# check_compose_profiles_conflict matrix (design §2.5/§6)
# =============================================================================

@test "check_compose_profiles_conflict fails on shell-exported COMPOSE_PROFILES contradicting .env" {
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    load_common
    COMPOSE_PROFILES="something-else"

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'contradicts .env'* ]]
    [[ "$output" == *'unset COMPOSE_PROFILES'* ]]
}

@test "check_compose_profiles_conflict fails on stale internal .env (no COMPOSE_PROFILES key)" {
    create_env_file "BASE_DOMAIN=voipbin.test"
    load_common
    unset COMPOSE_PROFILES

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'stale .env, re-run ./scripts/init.sh --yes'* ]]
}

@test "check_compose_profiles_conflict passes on clean internal .env" {
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    load_common
    unset COMPOSE_PROFILES

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 0 ]]
}

@test "check_compose_profiles_conflict passes on external .env with empty COMPOSE_PROFILES" {
    create_env_file "DOMAIN_MODE=external" "COMPOSE_PROFILES="
    load_common
    unset COMPOSE_PROFILES

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 0 ]]
}

@test "check_compose_profiles_conflict passes when shell value matches .env" {
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns"
    load_common
    COMPOSE_PROFILES="internal-dns"

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 0 ]]
}

@test "check_compose_profiles_conflict passes when .env is missing" {
    load_common
    unset COMPOSE_PROFILES

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 0 ]]
}

@test "check_compose_profiles_conflict external .env without COMPOSE_PROFILES key passes (stale rule is internal-only)" {
    create_env_file "DOMAIN_MODE=external" "BASE_DOMAIN=example.com"
    load_common
    unset COMPOSE_PROFILES

    run check_compose_profiles_conflict "$PROJECT_DIR/.env"

    [[ "$status" -eq 0 ]]
}

# =============================================================================
# start.sh guard wiring (task 0): stale .env fails fast with VOIPBIN_START line
# =============================================================================

@test "start.sh fails fast with VOIPBIN_START error on stale internal .env" {
    # Docker stub so check_dependencies can never reach the real daemon (the
    # guard fires before it anyway).
    mock_command "docker" ""
    create_env_file "BASE_DOMAIN=voipbin.test" "HOST_EXTERNAL_IP=10.0.0.1"

    run env -u COMPOSE_PROFILES bash "$SCRIPTS_DIR/start.sh"

    [[ "$status" -eq 1 ]]
    [[ "$output" == *'VOIPBIN_START: status=error reason="stale .env, re-run ./scripts/init.sh --yes"'* ]]
}

@test "start.sh missing .env keeps the run-init-first message (guard skipped)" {
    mock_command "docker" ""
    rm -f "$PROJECT_DIR/.env"
    if [[ $EUID -eq 0 ]]; then
        skip "test requires an unprivileged user"
    fi

    run bash "$SCRIPTS_DIR/start.sh"

    [[ "$status" -ne 0 ]]
    [[ "$output" != *'VOIPBIN_START: status=error reason="stale .env'* ]]
}
