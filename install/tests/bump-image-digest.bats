#!/usr/bin/env bats
# Tests for scripts/bump-image-digest.sh

load 'test_helper'

# Builds a minimal, self-contained install/ tree under $PROJECT_DIR: real
# scripts/ (so bump-image-digest.sh finds common.sh/sync-compose-images.sh
# via its own SCRIPT_DIR resolution) plus a small, fully fabricated
# versions.lock.dist + docker-compose.yml.dist pair (never the real repo
# files - this script mutates them in place, and its default LOCK_FILE
# target is versions.lock.dist, matching the fixture).
setup_fixture() {
    mkdir -p "$PROJECT_DIR/scripts"
    cp "$SCRIPTS_DIR/common.sh" "$PROJECT_DIR/scripts/"
    cp "$SCRIPTS_DIR/sync-compose-images.sh" "$PROJECT_DIR/scripts/"
    cp "$SCRIPTS_DIR/bump-image-digest.sh" "$PROJECT_DIR/scripts/"

    cat > "$PROJECT_DIR/versions.lock.dist" <<'EOF'
{
  "_comment": "test fixture",
  "pin_strategy": "ancestry-based single commit",
  "target_commit": "0000000000000000000000000000000000000000",
  "target_commit_desc": "fixture",
  "dbscheme_monorepo_commit": "0000000000000000000000000000000000000000",
  "generated": "2026-01-01T00:00:00.000000Z",
  "generated_by": "fixture",
  "images": {
    "voipbin/bin-agent-manager": "sha256:1111111111111111111111111111111111111111111111111111111111aa",
    "voipbin/bin-call-manager": "sha256:2222222222222222222222222222222222222222222222222222222222bb"
  },
  "image_source_tags": {
    "voipbin/bin-agent-manager": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "voipbin/bin-call-manager": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "note_separate_repos": []
}
EOF

    cat > "$PROJECT_DIR/docker-compose.yml.dist" <<'EOF'
services:
  agent-manager:
    image: voipbin/bin-agent-manager@sha256:1111111111111111111111111111111111111111111111111111111111aa
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

@test "bump-image-digest.sh updates only the target image's entries" {
    local new_digest="sha256:$(printf 'c%.0s' {1..64})"
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-agent-manager" "$new_digest" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -eq 0 ]]

    run python3 -c "
import json
d = json.load(open('$PROJECT_DIR/versions.lock.dist'))
print(d['images']['voipbin/bin-agent-manager'])
print(d['image_source_tags']['voipbin/bin-agent-manager'])
print(d['images']['voipbin/bin-call-manager'])
"
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"$new_digest"* ]]
    [[ "$output" == *"cccccccccccccccccccccccccccccccccccccccc"* ]]
    # untouched entry unchanged
    [[ "$output" == *"sha256:2222222222222222222222222222222222222222222222222222222222bb"* ]]
}

@test "bump-image-digest.sh propagates the change into docker-compose.yml.dist" {
    local new_digest="sha256:$(printf 'd%.0s' {1..64})"
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-agent-manager" "$new_digest" "dddddddddddddddddddddddddddddddddddddddd"

    [[ "$status" -eq 0 ]]
    assert_file_contains "$PROJECT_DIR/docker-compose.yml.dist" "voipbin/bin-agent-manager@$new_digest"
    # untouched service's line unchanged
    assert_file_contains "$PROJECT_DIR/docker-compose.yml.dist" \
        "voipbin/bin-call-manager@sha256:2222222222222222222222222222222222222222222222222222222222bb"
}

@test "bump-image-digest.sh accepts a bare sha256 digest without resolving it" {
    # No registry access needed/attempted for this form - CI already knows
    # the digest of the image it just pushed.
    local new_digest="sha256:$(printf 'e%.0s' {1..64})"
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-agent-manager" "$new_digest" "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    [[ "$status" -eq 0 ]]
    [[ "$output" != *"Resolving digest"* ]]
}

@test "bump-image-digest.sh refuses an image not already in versions.lock" {
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-does-not-exist" "sha256:$(printf 'f%.0s' {1..64})" "ffffffffffffffffffffffffffffffffffffffff"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"no established entry"* ]]
    # untouched
    run python3 -c "import json; print(json.load(open('$PROJECT_DIR/versions.lock.dist'))['images']['voipbin/bin-agent-manager'])"
    [[ "$output" == "sha256:1111111111111111111111111111111111111111111111111111111111aa" ]]
}

@test "bump-image-digest.sh rejects an untracked image BEFORE attempting registry resolution" {
    # Uses a real tag:ref form (not a bare sha256:) precisely so a bug that
    # checks tracking AFTER resolution would try to hit the network here
    # (no docker/registry access in this test sandbox - it would hang or
    # fail with a network/auth error, not the clean "no established entry"
    # message this asserts).
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-does-not-exist" "voipbin/bin-does-not-exist:deadbeef" "ffffffffffffffffffffffffffffffffffffffff"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"no established entry"* ]]
    [[ "$output" != *"Resolving digest"* ]]
}

@test "bump-image-digest.sh refuses a source-commit that isn't a full 40-char SHA" {
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-agent-manager" "sha256:$(printf 'a%.0s' {1..64})" "not-a-sha"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"40-char git SHA"* ]]
}

@test "bump-image-digest.sh refuses an image-repo not prefixed voipbin/" {
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "not-voipbin/bin-agent-manager" "sha256:$(printf 'a%.0s' {1..64})" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"must start with 'voipbin/'"* ]]
}

@test "bump-image-digest.sh dies cleanly when versions.lock.dist is missing" {
    rm -f "$PROJECT_DIR/versions.lock.dist"
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-agent-manager" "sha256:$(printf 'a%.0s' {1..64})" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"versions.lock not found"* ]]
}

@test "bump-image-digest.sh does not touch target_commit/target_commit_desc/dbscheme_monorepo_commit" {
    run bash "$PROJECT_DIR/scripts/bump-image-digest.sh" \
        "voipbin/bin-agent-manager" "sha256:$(printf 'a%.0s' {1..64})" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    [[ "$status" -eq 0 ]]
    run python3 -c "
import json
d = json.load(open('$PROJECT_DIR/versions.lock.dist'))
print(d['target_commit'])
print(d['target_commit_desc'])
print(d['dbscheme_monorepo_commit'])
"
    [[ "$output" == *"0000000000000000000000000000000000000000"* ]]
    [[ "$output" == *"fixture"* ]]
}
