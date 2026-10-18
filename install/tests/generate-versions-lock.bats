#!/usr/bin/env bats
# Tests for scripts/generate-versions-lock.sh: the service-owned schema check.
#
# Every image with embedded migrations must be pinned to a build that already
# contains the migration runner and carries the target's migration files, and
# a service that would "keep its current pin" fails the run. The check always
# runs.
#
# Isolation: a throwaway git repository stands in for the monorepo and a
# stubbed docker answers the registry lookups ($TEST_TEMP_DIR/published lists
# the image:tag pairs that "exist"). Nothing touches the network.

load 'test_helper'

setup() {
    setup_test_env
    MONO="$TEST_TEMP_DIR/mono"
    LOCK_IN="$TEST_TEMP_DIR/in.lock"
    OUT="$TEST_TEMP_DIR/out.lock"
    : > "$TEST_TEMP_DIR/published"
    make_monorepo
    write_lock
    stub_registry
}

teardown() {
    teardown_test_env
}

git_mono() { git -C "$MONO" -c user.name=t -c user.email=t@example.test "$@"; }

commit_all() {
    git_mono add -A
    git_mono commit -q -m "$1"
    git_mono rev-parse HEAD
}

# History (first-parent line):
#   C0  tag-manager embeds migrations (no runner call yet) and api-manager exists
#   C1  tag-manager's cmd calls migrationhandler.Run (the introduction commit)
#   C2  changes tag-manager's migrations after C1 (newer files)
make_monorepo() {
    mkdir -p "$MONO"
    git -C "$MONO" init -q -b main
    mkdir -p "$MONO/bin-tag-manager/migrations" "$MONO/bin-tag-manager/cmd/tag-manager" \
             "$MONO/bin-api-manager/cmd"
    printf 'package migrations\n' > "$MONO/bin-tag-manager/migrations/embed.go"
    printf -- '-- baseline\n' > "$MONO/bin-tag-manager/migrations/000001_baseline.up.sql"
    printf 'package main\n' > "$MONO/bin-tag-manager/cmd/tag-manager/main.go"
    printf 'package main\n' > "$MONO/bin-api-manager/cmd/main.go"
    C0="$(commit_all c0)"

    printf 'package main\n\nfunc main() { migrationhandler.Run() }\n' > "$MONO/bin-tag-manager/cmd/tag-manager/main.go"
    C1="$(commit_all c1)"

    printf -- '-- second\n' > "$MONO/bin-tag-manager/migrations/000002_second.up.sql"
    C2="$(commit_all c2)"
}

write_lock() {
    cat > "$LOCK_IN" <<EOF
{
  "_comment": "test",
  "pin_strategy": "ancestry-based single commit",
  "target_commit": "old",
  "target_commit_desc": "old",
  "dbscheme_monorepo_commit": "old",
  "generated": "2026-01-01T00:00:00Z",
  "generated_by": "test",
  "images": {
    "voipbin/bin-api-manager": "sha256:$(printf 'a%.0s' {1..64})",
    "voipbin/bin-tag-manager": "sha256:$(printf 'b%.0s' {1..64})"
  },
  "image_source_tags": {
    "voipbin/bin-api-manager": "old",
    "voipbin/bin-tag-manager": "old"
  }
}
EOF
}

# docker stub: `info` works; `manifest inspect -v <ref>` succeeds for refs
# pinned by digest (the reachability probe) and for image:tag pairs listed in
# $TEST_TEMP_DIR/published, answering a digest derived from the tag.
stub_registry() {
    mock_command_script "docker" '
if [[ "$1" == "info" ]]; then exit 0; fi
if [[ "$1" == "manifest" && "$2" == "inspect" ]]; then
    ref="${@: -1}"
    if [[ "$ref" == *"@sha256:"* ]]; then
        echo "{\"Descriptor\": {\"digest\": \"${ref##*@}\"}}"
        exit 0
    fi
    if grep -qxF -- "$ref" "'"$TEST_TEMP_DIR"'/published"; then
        tag="${ref##*:}"
        echo "{\"Descriptor\": {\"digest\": \"sha256:${tag:0:40}000000000000000000000000\"}}"
        exit 0
    fi
    exit 1
fi
exit 1
'
}

publish() { echo "voipbin/$1:$2" >> "$TEST_TEMP_DIR/published"; }

run_generate() {
    run env MONOREPO_PATH="$MONO" LOCK_FILE="$LOCK_IN" OUTPUT_FILE="$OUT" \
        bash "$SCRIPTS_DIR/generate-versions-lock.sh" "$1"
}

@test "tag-manager pinned at the runner introduction commit passes and writes the lock" {
    publish bin-tag-manager "$C1"
    publish bin-api-manager "$C0"

    run_generate "$C1"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Service-owned schema check passed (1 services with embedded migrations)"* ]]
    [[ -f "$OUT" ]]
    grep -q "\"dbscheme_monorepo_commit\": \"$C1\"" "$OUT"
}

@test "a tag older than the migration runner fails and writes nothing" {
    publish bin-tag-manager "$C0"
    publish bin-api-manager "$C0"

    run_generate "$C1"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"voipbin/bin-tag-manager: pinned tag ${C0:0:12} predates the migration runner (introduced in ${C1:0:12})"* ]]
    [[ ! -e "$OUT" ]]
}

@test "a service with no registry tag (would keep its current pin) fails and writes nothing" {
    publish bin-api-manager "$C0"

    run_generate "$C1"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"keeping current pin"* ]]
    [[ "$output" == *"voipbin/bin-tag-manager: no registry tag resolved"* ]]
    [[ ! -e "$OUT" ]]
}

@test "an image whose service directory is gone at the target is not a schema-owning service" {
    # Remove the directory at C4: the image has no
    # embed.go at the target, so the check skips it and the run keeps the
    # old pin for it, as it always did.
    git_mono rm -rq bin-tag-manager
    local c4
    c4="$(commit_all c4)"
    publish bin-api-manager "$C0"

    run_generate "$c4"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Service-owned schema check passed (0 services"* ]]
}

@test "migrations that differ between the pinned build and the target fail" {
    publish bin-tag-manager "$C1"
    publish bin-api-manager "$C0"

    run_generate "$C2"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"has different bin-tag-manager/migrations files than the target"* ]]
    [[ ! -e "$OUT" ]]
}

@test "a target older than the migration runner fails: the check has no off switch" {
    publish bin-tag-manager "$C0"
    publish bin-api-manager "$C0"

    run_generate "$C0"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"cannot find the commit that first called migrationhandler.Run in bin-tag-manager/cmd"* ]]
    [[ ! -e "$OUT" ]]
}

@test "the check only reads MONOREPO_PATH: the repository is unchanged afterwards" {
    publish bin-tag-manager "$C1"
    publish bin-api-manager "$C0"
    local before after
    before="$(git_mono for-each-ref; git_mono status --porcelain)"

    run_generate "$C1"

    after="$(git_mono for-each-ref; git_mono status --porcelain)"
    [[ "$status" -eq 0 ]]
    [[ "$before" == "$after" ]]
}

@test "branches outside the target history do not affect the introduction commit lookup" {
    # A second branch that does not contain the introduction must not matter.
    git_mono branch side "$C0"
    publish bin-tag-manager "$C1"
    publish bin-api-manager "$C0"

    run_generate "$C1"

    [[ "$status" -eq 0 ]]
}
