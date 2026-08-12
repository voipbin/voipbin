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

@test "refuses when an EXPECTED file is renamed away (rename-aware parsing, the actual discriminating case)" {
    # Regression test for the real bug: `git status --porcelain` (newline
    # form) renders a rename as "R  old -> new" on one line. Naive
    # `awk '{print $2}'` parsing extracts "old" - which for THIS scenario
    # (renaming install/versions.lock itself away) is exactly the expected
    # path, so the old parsing would WRONGLY treat this as "expected file
    # modified, all good" even though the actual file that would need
    # committing no longer exists under that name.
    #
    # IMPORTANT: this must be a PURE rename with NO content change - git's
    # rename detection is similarity-based, and a rename bundled with a
    # large-enough content edit (e.g. applying the digest bump first) gets
    # reported as separate Add+Delete entries instead of a single "R" line,
    # which does NOT exercise the buggy code path at all (verified: a
    # rename-after-content-change version of this test passes even against
    # the unfixed `awk '{print $2}'` parsing, making it a vacuous test - a
    # pure rename is the only form that produces the "R  old -> new" line
    # this fix targets).
    setup_fake_repo
    (
        cd "$REPO_DIR" || exit 1
        git mv install/versions.lock install/versions-renamed-away.lock
    )
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"unexpected dirty file"* ]]
    [[ "$output" == *"versions-renamed-away.lock"* ]]
    run git -C "$REPO_DIR" branch --list "NOJIRA-Bump-*"
    [[ -z "$output" ]]
}

@test "refuses when an EXPECTED file is renamed away WITH a small edit (still under git's rename-similarity threshold)" {
    # Defense-in-depth companion to the zero-delta pure-rename test above:
    # a real bump-image-digest.sh run always changes SOME content, so this
    # confirms the guard still catches a renamed-away expected file when
    # git's similarity detection still classifies it as a rename (a small
    # edit, well under the default 50% dissimilarity threshold, so it
    # stays a single "R" status line rather than degrading to Add+Delete).
    setup_fake_repo
    (
        cd "$REPO_DIR" || exit 1
        # One-line tweak, tiny relative to the whole file - stays "R".
        sed -i 's/1111111111111111111111111111111111111111111111111111111111aa/1111111111111111111111111111111111111111111111111111111111ab/' \
            install/versions.lock
        git mv install/versions.lock install/versions-renamed-away.lock
    )
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"unexpected dirty file"* ]]
    [[ "$output" == *"versions-renamed-away.lock"* ]]
    run git -C "$REPO_DIR" branch --list "NOJIRA-Bump-*"
    [[ -z "$output" ]]
}

@test "refuses a source-repo-url outside github.com/voipbin/" {
    setup_fake_repo
    apply_fake_bump "sha256:$(printf 'c%.0s' {1..64})"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc" \
        "https://evil.example.com/phishing"

    [[ "$status" -ne 0 ]]
    [[ "$output" == *"source-repo-url must start with"* ]]
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

@test "branch name has no spurious trailing hyphen" {
    # Regression test: `echo "$BRANCH" | tr -c '...' '-'` used to translate
    # echo's trailing newline into a literal trailing '-' that command
    # substitution doesn't strip, so every branch name/PR title ended in a
    # stray hyphen. Assert the EXACT expected name (not a wildcard glob,
    # which is what every other test uses and is why this went unnoticed
    # for 5 review rounds).
    setup_fake_repo
    install_fake_curl
    apply_fake_bump "sha256:$(printf 'c%.0s' {1..64})"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Branch: NOJIRA-Bump-bin-agent-manager-cccccccccccc"* ]]
    [[ "$output" != *"NOJIRA-Bump-bin-agent-manager-cccccccccccc-"* ]]

    local exact_ref
    exact_ref="$(git -C "$REPO_DIR" ls-remote origin | awk '{print $2}' | grep 'NOJIRA-Bump')"
    [[ "$exact_ref" == "refs/heads/NOJIRA-Bump-bin-agent-manager-cccccccccccc" ]]
}

@test "does not falsely warn 'not modified' for either expected file when both are actually modified" {
    # Regression test: the "is not modified - proceeding anyway" warning
    # used to compare a newline-delimited $DIRTY_FILES against a
    # space-padded substring pattern, which never matched anything -
    # meaning it fired for BOTH files on every single normal invocation,
    # including this exact both-files-modified case. Assert it's silent
    # here so a regression to that broken comparison is caught.
    setup_fake_repo
    install_fake_curl
    apply_fake_bump "sha256:$(printf 'c%.0s' {1..64})"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -eq 0 ]]
    [[ "$output" != *"is not modified"* ]]
}

@test "does warn 'not modified' for the one expected file that genuinely wasn't touched" {
    # The positive-case counterpart: confirm the warning still fires
    # correctly when it should (only one of the two expected files
    # changed), not just that the fix silenced it unconditionally.
    setup_fake_repo
    install_fake_curl
    # Only touch docker-compose.yml.dist directly - versions.lock stays
    # byte-identical to the committed baseline.
    sed -i 's/1111111111111111111111111111111111111111111111111111111111aa/2222222222222222222222222222222222222222222222222222222222bb/' \
        "$REPO_DIR/install/docker-compose.yml.dist"
    cd "$REPO_DIR" || exit 1
    GH_TOKEN="fake-token" run run_script "voipbin/bin-agent-manager" "cccccccccccccccccccccccccccccccccccccccc"

    [[ "$status" -eq 0 ]]
    [[ "$output" == *"install/versions.lock is not modified"* ]]
    [[ "$output" != *"install/docker-compose.yml.dist is not modified"* ]]
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
