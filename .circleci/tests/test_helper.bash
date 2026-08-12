#!/bin/bash
# Minimal, self-contained bats helper for .circleci/scripts/ tests.
# Deliberately independent of install/tests/test_helper.bash - CI-internal
# tooling and the self-hosting product's test harness are separate concerns.

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CIRCLECI_DIR="$(dirname "$TESTS_DIR")"
SCRIPTS_DIR="$CIRCLECI_DIR/scripts"

TEST_TEMP_DIR=""
ORIGINAL_PATH="$PATH"

setup_test_env() {
    TEST_TEMP_DIR="$(mktemp -d)"
    export MOCK_BIN_DIR="$TEST_TEMP_DIR/mock_bin"
    mkdir -p "$MOCK_BIN_DIR"
    export PATH="$MOCK_BIN_DIR:$ORIGINAL_PATH"
}

teardown_test_env() {
    if [[ -n "$TEST_TEMP_DIR" && -d "$TEST_TEMP_DIR" ]]; then
        rm -rf "$TEST_TEMP_DIR"
    fi
    export PATH="$ORIGINAL_PATH"
}

# Builds a fake voipbin/voipbin checkout: a bare "origin" repo plus a real
# working clone, with install/versions.lock + install/docker-compose.yml.dist
# committed at a known baseline. Returns the working clone's path via the
# REPO_DIR global.
setup_fake_repo() {
    local bare_dir="$TEST_TEMP_DIR/origin.git"
    REPO_DIR="$TEST_TEMP_DIR/repo"

    git init -q --bare "$bare_dir"

    git init -q "$REPO_DIR"
    (
        cd "$REPO_DIR" || exit 1
        git config user.email "test@example.com"
        git config user.name "Test"
        mkdir -p install
        cat > install/versions.lock <<'EOF'
{
  "images": {
    "voipbin/bin-agent-manager": "sha256:1111111111111111111111111111111111111111111111111111111111aa"
  },
  "image_source_tags": {
    "voipbin/bin-agent-manager": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
EOF
        cat > install/docker-compose.yml.dist <<'EOF'
services:
  agent-manager:
    image: voipbin/bin-agent-manager@sha256:1111111111111111111111111111111111111111111111111111111111aa
EOF
        git add -A
        git commit -q -m "baseline"
        git branch -M main
        git remote add origin "$bare_dir"
        git push -q origin main
    )
}

# Fakes a bump-image-digest.sh run: mutates the two tracked files in place
# without committing (matching the real script's contract).
apply_fake_bump() {
    local new_digest="$1"
    python3 -c "
import json
p = '$REPO_DIR/install/versions.lock'
d = json.load(open(p))
d['images']['voipbin/bin-agent-manager'] = '$new_digest'
d['image_source_tags']['voipbin/bin-agent-manager'] = 'cccccccccccccccccccccccccccccccccccccccc'
json.dump(d, open(p, 'w'), indent=2)
"
    sed -i "s|sha256:1111111111111111111111111111111111111111111111111111111111aa|$new_digest|" \
        "$REPO_DIR/install/docker-compose.yml.dist"
}

# A curl stub that fakes the GitHub PR-creation API response and logs every
# invocation (incl. headers) to $CURL_LOG, so tests can assert on what was
# actually sent (e.g. the Authorization header) without a real network call.
install_fake_curl() {
    CURL_LOG="$TEST_TEMP_DIR/curl.log"
    : > "$CURL_LOG"
    cat > "$MOCK_BIN_DIR/curl" <<EOF
#!/bin/bash
echo "CURL_CALL: \$*" >> "$CURL_LOG"
echo '{"html_url": "https://github.com/voipbin/voipbin/pull/999"}'
EOF
    chmod +x "$MOCK_BIN_DIR/curl"
}

install_failing_fake_curl() {
    cat > "$MOCK_BIN_DIR/curl" <<'EOF'
#!/bin/bash
exit 22
EOF
    chmod +x "$MOCK_BIN_DIR/curl"
}
