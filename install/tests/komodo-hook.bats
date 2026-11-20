#!/usr/bin/env bats
# Tests for scripts/komodo-hook.sh (Komodo pre_deploy gate).
#
# The hook runs with the install directory as its working directory, so every
# test runs it from a temp directory holding the lock and token files.

load 'test_helper'

setup() {
    setup_test_env
    HOOK_DIR_UNDER_TEST="$TEST_TEMP_DIR/install"
    mkdir -p "$HOOK_DIR_UNDER_TEST"
    LOCK="$HOOK_DIR_UNDER_TEST/.voipbin-op.lock"
    TOKEN="$HOOK_DIR_UNDER_TEST/.voipbin-hook-token"
    # A process that is alive for the whole test, and one that is already gone
    sleep 300 &
    LIVE_PID=$!
    ( : ) &
    DEAD_PID=$!
    wait "$DEAD_PID" 2>/dev/null || true
}

teardown() {
    kill "$LIVE_PID" 2>/dev/null || true
    teardown_test_env
}

run_hook() {
    run bash -c "cd '$HOOK_DIR_UNDER_TEST' && bash '$SCRIPTS_DIR/komodo-hook.sh'"
}

write_lock() {
    printf 'update pid=%s 2026-01-01T00:00:00Z\n' "$1" > "$LOCK"
}

write_token() {
    printf '%s %s\n' "$1" "$2" > "$TOKEN"
}

now() { date +%s; }

@test "no lock: pass" {
    run_hook
    [ "$status" -eq 0 ]
}

@test "live lock with a valid token: pass and the token is consumed" {
    write_lock "$LIVE_PID"
    write_token "$LIVE_PID" "$(now)"
    run_hook
    [ "$status" -eq 0 ]
    [ ! -e "$TOKEN" ]
}

@test "live lock without a token: refused with exit 7 and the standard message" {
    write_lock "$LIVE_PID"
    run_hook
    [ "$status" -eq 7 ]
    [[ "$output" == *"voipbin operation in progress"* ]]
}

@test "stale lock (holder not running): pass" {
    write_lock "$DEAD_PID"
    run_hook
    [ "$status" -eq 0 ]
    [[ "$output" == *"stale lock"* ]]
}

@test "stale lock: a leftover token is not needed and not touched" {
    write_lock "$DEAD_PID"
    write_token "$DEAD_PID" "$(now)"
    run_hook
    [ "$status" -eq 0 ]
    [ -e "$TOKEN" ]
}

@test "live lock with a token from a dead lock holder: refused" {
    write_lock "$LIVE_PID"
    write_token "$DEAD_PID" "$(now)"
    run_hook
    [ "$status" -eq 7 ]
    [ -e "$TOKEN" ]
}

@test "token older than 60 seconds: refused" {
    write_lock "$LIVE_PID"
    write_token "$LIVE_PID" "$(( $(now) - 61 ))"
    run_hook
    [ "$status" -eq 7 ]
    [[ "$output" == *"voipbin operation in progress"* ]]
}

@test "token at 30 seconds of age is still valid" {
    write_lock "$LIVE_PID"
    write_token "$LIVE_PID" "$(( $(now) - 30 ))"
    run_hook
    [ "$status" -eq 0 ]
}

@test "token issued in the future: refused" {
    write_lock "$LIVE_PID"
    write_token "$LIVE_PID" "$(( $(now) + 3600 ))"
    run_hook
    [ "$status" -eq 7 ]
}

@test "token pid does not match the lock pid: refused" {
    write_lock "$LIVE_PID"
    write_token "$(( LIVE_PID + 1 ))" "$(now)"
    run_hook
    [ "$status" -eq 7 ]
    [ -e "$TOKEN" ]
}

@test "token reuse: the second deploy is refused" {
    write_lock "$LIVE_PID"
    write_token "$LIVE_PID" "$(now)"
    run_hook
    [ "$status" -eq 0 ]
    run_hook
    [ "$status" -eq 7 ]
}

@test "malformed token content: refused" {
    write_lock "$LIVE_PID"
    printf 'not a token\n' > "$TOKEN"
    run_hook
    [ "$status" -eq 7 ]
}

@test "symlinked token: refused even when its target is valid" {
    write_lock "$LIVE_PID"
    printf '%s %s\n' "$LIVE_PID" "$(now)" > "$TEST_TEMP_DIR/real-token"
    ln -s "$TEST_TEMP_DIR/real-token" "$TOKEN"
    run_hook
    [ "$status" -eq 7 ]
    [ -e "$TEST_TEMP_DIR/real-token" ]
}

@test "symlinked lock: refused" {
    printf 'update pid=%s now\n' "$DEAD_PID" > "$TEST_TEMP_DIR/real-lock"
    ln -s "$TEST_TEMP_DIR/real-lock" "$LOCK"
    run_hook
    [ "$status" -eq 7 ]
}

@test "lock without a readable pid: refused (fail closed)" {
    printf 'update no-pid-here\n' > "$LOCK"
    run_hook
    [ "$status" -eq 7 ]
}

@test "lock holder owned by another user (EPERM on kill -0) counts as alive" {
    # pid 1 exists and a non-root user gets EPERM when signalling it
    if [[ "$(id -u)" -eq 0 ]]; then skip "needs a non-root user"; fi
    write_lock 1
    run_hook
    [ "$status" -eq 7 ]
}

@test "lock or token owned by neither root nor the directory owner: refused" {
    if [[ "$(id -u)" -ne 0 ]]; then skip "needs root to create a foreign owner"; fi
    write_lock "$LIVE_PID"
    write_token "$LIVE_PID" "$(now)"
    chown 12345:12345 "$TOKEN"
    run_hook
    [ "$status" -eq 7 ]
}

@test "hook calls no repository script and carries a format line after the shebang" {
    [ "$(sed -n 1p "$SCRIPTS_DIR/komodo-hook.sh")" = "#!/bin/bash" ]
    sed -n 2p "$SCRIPTS_DIR/komodo-hook.sh" | grep -Eq '^# komodo-hook-format: [0-9]+$'
    if grep -v '^[[:space:]]*#' "$SCRIPTS_DIR/komodo-hook.sh" | grep -Eq '(\./|scripts/|\.sh|source |^\. )'; then false; fi
}
