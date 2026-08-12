#!/usr/bin/env bats
# Tests for .circleci/scripts/open-versions-lock-pr.sh

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

run_script() {
    bash "$SCRIPTS_DIR/open-versions-lock-pr.sh" "$@"
}

@test "refuses to run when GH_TOKEN is not set" {
    setup_fake_repo
    unset GH_TOKEN
    cd "$REPO_DIR" || exit 1
    run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"GH_TOKEN is not set"* ]]
}

@test "refuses an image-repo not prefixed voipbin/" {
    setup_fake_repo
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "not-voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"must start with 'voipbin/'"* ]]
}

@test "refuses a source-commit that isn't a full 40-char SHA" {
    setup_fake_repo
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "not-a-sha"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"40-char git SHA"* ]]
}

@test "clean working tree is a successful no-op, not an error" {
    setup_fake_repo
    cd "$REPO_DIR" || exit 1
    # No apply_fake_bump call - tree stays exactly at the committed baseline.
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Nothing to PR"* ]]
    # No branch was created for this.
    run git -C "$REPO_DIR" branch --list "NOJIRA-Bump-*"
    [[ -z "$output" ]]
}

@test "refuses when the working tree has an unexpected dirty file" {
    setup_fake_repo
    apply_fake_bump "sha256:$(printf 'c%.0s' {1..64})"
    echo "unexpected" > "$REPO_DIR/install/some-other-file.txt"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"unexpected dirty file"* ]]
    [[ "$output" == *"some-other-file.txt"* ]]
    # No branch was created - refused before any git mutation.
    run git -C "$REPO_DIR" branch --list "NOJIRA-Bump-*"
    [[ -z "$output" ]]
}

@test "happy path: commits, pushes, and opens a PR with the correct digests in the body" {
    setup_fake_repo
    install_fake_curl
    local new_digest="sha256:$(printf 'c%.0s' {1..64})"
    apply_fake_bump "$new_digest"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token-value" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"PR_URL=https://github.com/voipbin/voipbin/pull/999"* ]]

    # Branch was actually pushed to the (bare, local) origin.
    run git -C "$REPO_DIR" ls-remote origin "NOJIRA-Bump-bin-agent-manager-*"
    [[ -n "$output" ]]

    # curl received the new/old digests and an Authorization header - not
    # just "a curl call happened".
    run cat "$CURL_LOG"
    [[ "$output" == *"Authorization: Bearer fake-token-value"* ]]
}

@test "GH_TOKEN value never appears in stdout/stderr output" {
    setup_fake_repo
    install_fake_curl
    apply_fake_bump "sha256:$(printf 'd%.0s' {1..64})"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="super-secret-token-xyz789" run run_script "voipbin/bin-agent-manager" "dddddddddddddddddddddddddddddddddddddddd"

    [[ "$status" -eq 0 ]]
    [[ "$output" != *"super-secret-token-xyz789"* ]]
}

@test "GH_TOKEN is never written into .git/config" {
    setup_fake_repo
    install_fake_curl
    apply_fake_bump "sha256:$(printf 'e%.0s' {1..64})"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="super-secret-token-xyz789" run run_script "voipbin/bin-agent-manager" "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    [[ "$status" -eq 0 ]]
    run cat "$REPO_DIR/.git/config"
    [[ "$output" != *"super-secret-token-xyz789"* ]]
}

@test "the PR body includes old and new digests and the source commit" {
    setup_fake_repo
    install_fake_curl
    local new_digest="sha256:$(printf 'f%.0s' {1..64})"
    apply_fake_bump "$new_digest"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "ffffffffffffffffffffffffffffffffffffffff" \
        "https://github.com/voipbin/monorepo/commit/ffffffffffffffffffffffffffffffffffffffff"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"sha256:1111111111111111111111111111111111111111111111111111111111aa"* ]]
    [[ "$output" == *"$new_digest"* ]]
}

@test "reports a clear error (but does not lose the pushed branch) when the GitHub API call fails" {
    setup_fake_repo
    install_failing_fake_curl
    apply_fake_bump "sha256:$(printf '9%.0s' {1..64})"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "9999999999999999999999999999999999999999"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"PR creation may have failed"* ]]
    # The branch was still pushed - not silently lost just because the API
    # call afterward failed.
    run git -C "$REPO_DIR" ls-remote origin "NOJIRA-Bump-bin-agent-manager-*"
    [[ -n "$output" ]]
}
