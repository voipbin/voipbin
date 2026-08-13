#!/usr/bin/env bats
# Tests for scripts/sync-compose-images.sh

load 'test_helper'

# Same fixture shape as bump-image-digest.bats: real scripts/ (so
# sync-compose-images.sh finds common.sh via its own SCRIPT_DIR
# resolution) plus a small, fully fabricated versions.lock.dist +
# docker-compose.yml.dist pair.
setup_fixture() {
    mkdir -p "$PROJECT_DIR/scripts"
    cp "$SCRIPTS_DIR/common.sh" "$PROJECT_DIR/scripts/"
    cp "$SCRIPTS_DIR/sync-compose-images.sh" "$PROJECT_DIR/scripts/"

    cat > "$PROJECT_DIR/versions.lock.dist" <<'EOF'
{
  "_comment": "test fixture",
  "images": {
    "voipbin/bin-agent-manager": "sha256:1111111111111111111111111111111111111111111111111111111111aa",
    "voipbin/bin-call-manager": "sha256:2222222222222222222222222222222222222222222222222222222222bb"
  }
}
EOF

    cat > "$PROJECT_DIR/docker-compose.yml.dist" <<'EOF'
services:
  agent-manager:
    image: voipbin/bin-agent-manager@sha256:0000000000000000000000000000000000000000000000000000000000aa
  call-manager:
    image: voipbin/bin-call-manager@sha256:2222222222222222222222222222222222222222222222222222222222bb
EOF
}

setup() {
    setup_test_env
    setup_fixture
}

teardown() {
    teardown_test_env
}

@test "sync-compose-images.sh rewrites a stale image line to match versions.lock" {
    run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"

    [[ "$status" -eq 0 ]]
    assert_file_contains "$PROJECT_DIR/docker-compose.yml.dist" \
        "voipbin/bin-agent-manager@sha256:1111111111111111111111111111111111111111111111111111111111aa"
}

@test "sync-compose-images.sh is a no-op the second time (idempotent)" {
    run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"
    [[ "$status" -eq 0 ]]

    run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"0 image line(s) rewritten"* ]]
}

@test "sync-compose-images.sh fails on drift: compose references an image the lock does not pin" {
    cat >> "$PROJECT_DIR/docker-compose.yml.dist" <<'EOF'
  extra-manager:
    image: voipbin/bin-extra-manager@sha256:3333333333333333333333333333333333333333333333333333333333cc
EOF
    run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"DRIFT"* ]]
    [[ "$output" == *"bin-extra-manager"* ]]
}

# =============================================================================
# VOIP-1334: concurrent-write protection, same mechanism/rationale as
# bump-image-digest.bats's equivalent tests - see that file's header
# comment above its own VOIP-1334 section for the full explanation of why
# these exercise the real acquire_file_lock() primitive against an
# external holder rather than racing two real script invocations.
# =============================================================================

@test "sync-compose-images.sh waits for a concurrent holder to release the lock, then succeeds" {
    (
        exec 9>"$PROJECT_DIR/docker-compose.yml.dist.flock"
        flock -x 9
        sleep 2
    ) &
    local holder_pid=$!
    sleep 0.3

    local start_ts elapsed
    start_ts=$(date +%s)
    LOCK_TIMEOUT_SECONDS=10 run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"
    elapsed=$(( $(date +%s) - start_ts ))

    wait "$holder_pid" 2>/dev/null || true

    [[ "$status" -eq 0 ]]
    [[ "$elapsed" -ge 1 ]]
    assert_file_contains "$PROJECT_DIR/docker-compose.yml.dist" \
        "voipbin/bin-agent-manager@sha256:1111111111111111111111111111111111111111111111111111111111aa"
}

@test "sync-compose-images.sh fails clearly (and leaves docker-compose.yml.dist untouched) when the lock is held longer than LOCK_TIMEOUT_SECONDS" {
    (
        exec 9>"$PROJECT_DIR/docker-compose.yml.dist.flock"
        flock -x 9
        sleep 5
    ) &
    local holder_pid=$!
    sleep 0.3

    local before_hash
    before_hash=$(md5sum "$PROJECT_DIR/docker-compose.yml.dist" | cut -d' ' -f1)

    LOCK_TIMEOUT_SECONDS=1 run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"Timed out after 1s waiting for a lock"* ]]
    local after_hash
    after_hash=$(md5sum "$PROJECT_DIR/docker-compose.yml.dist" | cut -d' ' -f1)
    [[ "$before_hash" == "$after_hash" ]]

    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
}

@test "sync-compose-images.sh locks a SEPARATE .flock file scoped to COMPOSE_FILE, never the file itself or LOCK_FILE" {
    run bash "$PROJECT_DIR/scripts/sync-compose-images.sh"

    [[ "$status" -eq 0 ]]
    [[ -f "$PROJECT_DIR/docker-compose.yml.dist.flock" ]]
    [[ ! -f "$PROJECT_DIR/versions.lock.dist.flock" ]]
}
