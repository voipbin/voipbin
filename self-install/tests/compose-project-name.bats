#!/usr/bin/env bats
# Tests for scripts/common.sh: derive_compose_project_name()

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

@test "derive_compose_project_name: uses COMPOSE_PROJECT_NAME env var when set" {
    load_common
    COMPOSE_PROJECT_NAME="sandbox"
    result=$(derive_compose_project_name)
    [ "$result" = "sandbox" ]
}

@test "derive_compose_project_name: derives from PROJECT_DIR basename when unset" {
    load_common
    unset COMPOSE_PROJECT_NAME
    PROJECT_DIR="/tmp/self-install"
    result=$(derive_compose_project_name)
    [ "$result" = "self-install" ]
}

@test "derive_compose_project_name: normalizes uppercase and strips leading dash" {
    load_common
    unset COMPOSE_PROJECT_NAME
    PROJECT_DIR="/tmp/-Self-Install"
    result=$(derive_compose_project_name)
    [ "$result" = "self-install" ]
}

@test "derive_compose_project_name: strips leading underscore" {
    load_common
    unset COMPOSE_PROJECT_NAME
    PROJECT_DIR="/tmp/_self_install"
    result=$(derive_compose_project_name)
    [ "$result" = "self_install" ]
}

@test "derive_compose_project_name: fails on invalid COMPOSE_PROJECT_NAME" {
    load_common
    COMPOSE_PROJECT_NAME="Invalid Name!"
    run derive_compose_project_name
    [ "$status" -ne 0 ]
}

@test "derive_compose_project_name: fails on a basename that filters down to empty" {
    load_common
    unset COMPOSE_PROJECT_NAME
    PROJECT_DIR="/tmp/---"
    run derive_compose_project_name
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}
