#!/bin/bash
# VoIPBin - open a PR bumping one image's pin in install/versions.lock
#
# CI-internal tooling, NOT part of the install/ self-hosting product surface
# (deliberately kept out of install/scripts/ - see .circleci/README.md).
#
# Preconditions (caller's responsibility):
#   - CWD is a git checkout of voipbin/voipbin, HEAD on the default branch,
#     clean except for install/versions.lock and install/docker-compose.yml.dist
#     already modified in the working tree (e.g. by
#     install/scripts/bump-image-digest.sh) - NOT yet committed.
#   - The 'origin' remote is a plain https://github.com/voipbin/voipbin.git
#     URL (no embedded credentials).
#
# Usage:
#   GH_TOKEN=<token> ./.circleci/scripts/open-versions-lock-pr.sh <image-repo> <source-commit> [<source-repo-url>]
#
#   <image-repo>        e.g. voipbin/bin-agent-manager - must match the
#                       image whose entry was just changed in versions.lock.
#   <source-commit>     full 40-char git SHA the new image was built from.
#   <source-repo-url>   optional https URL to the source commit, linked in
#                       the PR body for review convenience (e.g.
#                       https://github.com/voipbin/monorepo/commit/<sha>).
#
# Environment:
#   GH_TOKEN   GitHub token with contents:write + pull-requests:write on
#              voipbin/voipbin (required). NEVER echoed, NEVER embedded in a
#              git remote URL or written to .git/config: git push auth uses
#              a per-invocation `-c http.extraheader` Authorization header
#              (present only for that one command's process), and the
#              GitHub REST API call uses a plain `-H "Authorization: ..."`
#              curl header. Both approaches keep the token out of anything
#              that could later leak via `git remote -v`, `.git/config`, a
#              cached git credential store, or shell history replay.
#              Residual, accepted exposure: like any credential passed as a
#              command-line argument, it is briefly visible to other
#              processes on the same host via `ps`/`/proc/<pid>/cmdline`
#              while the `git`/`curl` child process runs - standard for a
#              single-tenant CI runner, not eliminated by the above.
#
# Safety: refuses to run (exits 1, no branch/commit/push attempted) if the
# working tree has ANY dirty file other than the two expected ones - this
# is a deliberate narrow scope guard, not a generic "commit whatever is
# dirty" script.
#
# No self-merge: this script only opens a PR. It never approves or merges
# anything - that stays a human decision, same as every other PR in this
# repo.

set -e

REPO_SLUG="voipbin/voipbin"
EXPECTED_FILES=("install/versions.lock" "install/docker-compose.yml.dist")

log_info()  { echo "[INFO] $*"; }
log_warn()  { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

usage() {
    sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -lt 2 || $# -gt 3 ]]; then
    usage
    exit 1
fi

IMAGE_REPO="$1"
SOURCE_COMMIT="$2"
SOURCE_REPO_URL="${3:-}"

if [[ -z "${GH_TOKEN:-}" ]]; then
    log_error "GH_TOKEN is not set"
    exit 1
fi

if [[ ! "$IMAGE_REPO" =~ ^voipbin/ ]]; then
    log_error "image-repo must start with 'voipbin/' (got: $IMAGE_REPO)"
    exit 1
fi

if ! [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    log_error "source-commit must be a full 40-char git SHA (got: $SOURCE_COMMIT)"
    exit 1
fi

# Rendered as a clickable link in the (public, human-reviewed) PR body -
# constrained to voipbin's own GitHub org so this can't become an arbitrary
# link-injection vector even though the design assumes CI-trusted callers.
if [[ -n "$SOURCE_REPO_URL" ]] && ! [[ "$SOURCE_REPO_URL" =~ ^https://github\.com/voipbin/ ]]; then
    log_error "source-repo-url must start with https://github.com/voipbin/ (got: $SOURCE_REPO_URL)"
    exit 1
fi

if ! git rev-parse --git-dir &> /dev/null; then
    log_error "not inside a git checkout"
    exit 1
fi

# ---- Narrow scope guard: refuse anything but the two expected dirty files ----
# Uses `git status --porcelain=v1 -z` (NUL-delimited) parsed in Python, NOT
# `awk '{print $2}'` on the newline form - the newline form renders a
# rename/copy as "XY old -> new" on ONE line, so naive whitespace-splitting
# silently extracts the OLD path and never sees the new one, which would
# let a renamed-away expected file (or a renamed-in unexpected file) slip
# past this guard undetected. The -z form instead emits the new path, then
# (only for R/C status codes) a second NUL-terminated field with the old
# path - parsed explicitly below so nothing is missed.
DIRTY_FILES="$(git status --porcelain=v1 -z | python3 -c "
import sys
data = sys.stdin.buffer.read()
parts = data.split(b'\x00')
paths = []
i = 0
while i < len(parts):
    entry = parts[i]
    i += 1
    if not entry:
        continue
    status, path = entry[:2], entry[3:]
    paths.append(path.decode('utf-8', 'surrogateescape'))
    if status[0:1] in (b'R', b'C') or status[1:2] in (b'R', b'C'):
        i += 1  # skip the paired orig-path field
print('\n'.join(paths))
")"
if [[ -z "$DIRTY_FILES" ]]; then
    log_info "Nothing to PR: working tree is clean (versions.lock/docker-compose.yml.dist unchanged)."
    log_info "This is not an error - it means the digest bump was a no-op (already at that pin)."
    exit 0
fi

UNEXPECTED=""
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    match="false"
    for expected in "${EXPECTED_FILES[@]}"; do
        [[ "$f" == "$expected" ]] && match="true"
    done
    [[ "$match" == "false" ]] && UNEXPECTED="$UNEXPECTED $f"
done <<< "$DIRTY_FILES"

if [[ -n "$UNEXPECTED" ]]; then
    log_error "Refusing to proceed: working tree has unexpected dirty file(s):$UNEXPECTED"
    log_error "This script only ever commits ${EXPECTED_FILES[*]} - investigate the extra"
    log_error "change(s) before retrying (they will NOT be committed by this script)."
    exit 1
fi

for expected in "${EXPECTED_FILES[@]}"; do
    found="false"
    while IFS= read -r f; do
        [[ "$f" == "$expected" ]] && found="true"
    done <<< "$DIRTY_FILES"
    if [[ "$found" == "false" ]]; then
        log_warn "$expected is not modified - proceeding anyway (the other file's change is enough to PR)"
    fi
done

# ---- Read the old/new digest for the PR body (informational only) ----
NEW_DIGEST="$(python3 -c "
import json, sys
print(json.load(open('install/versions.lock'))['images'].get(sys.argv[1], '?'))
" "$IMAGE_REPO")"
OLD_DIGEST="$(git show HEAD:install/versions.lock 2>/dev/null | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin)['images'].get(sys.argv[1], '?'))
except Exception:
    print('?')
" "$IMAGE_REPO" || echo "?")"

SHORT_NAME="${IMAGE_REPO#voipbin/}"
SHORT_COMMIT="${SOURCE_COMMIT:0:12}"
BRANCH="NOJIRA-Bump-${SHORT_NAME}-${SHORT_COMMIT}"
# Sanitize: branch names built from image names only contain [a-z0-9-],
# but guard defensively against anything unexpected sneaking through. Pure
# bash parameter expansion, not `echo ... | tr -c ...` - `echo` appends a
# trailing newline, and `tr -c` (complementing the allowed set) translates
# that newline into a literal trailing '-' that command substitution does
# NOT strip (it only strips trailing newlines), leaving every branch
# name/PR title with a spurious trailing hyphen.
BRANCH="${BRANCH//[^A-Za-z0-9._-]/-}"

log_info "Branch: $BRANCH"
log_info "$IMAGE_REPO: $OLD_DIGEST -> $NEW_DIGEST"

git checkout -b "$BRANCH"
git add -- "${EXPECTED_FILES[@]}"
git -c user.name="voipbin-ci" -c user.email="ci@voipbin.net" commit -m "Bump $SHORT_NAME image pin

- voipbin: update versions.lock + docker-compose.yml.dist for
  $IMAGE_REPO to the image built from commit $SOURCE_COMMIT"

# ---- Push using a per-invocation Authorization header, never a token in
# the remote URL or .git/config. -c is scoped to this one command only. ----
AUTH_HEADER="Authorization: Basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)"
git -c http.extraheader="$AUTH_HEADER" push origin "$BRANCH:$BRANCH"

# ---- Open the PR via the GitHub REST API ----
PR_TITLE="$BRANCH"
PR_BODY_FILE="$(mktemp)"
trap 'rm -f "$PR_BODY_FILE"' EXIT

{
    echo "Automated image pin bump, opened by CI after $IMAGE_REPO's release job pushed a new image."
    echo ""
    echo "- voipbin: $IMAGE_REPO: \`$OLD_DIGEST\` -> \`$NEW_DIGEST\`"
    if [[ -n "$SOURCE_REPO_URL" ]]; then
        echo "- Built from: $SOURCE_REPO_URL"
    else
        echo "- Built from commit: $SOURCE_COMMIT"
    fi
    echo ""
    echo "This PR still requires human review and merge like any other change to install/ -"
    echo "nothing self-merges. See install/CLAUDE.md's docker-compose.yml.dist vs"
    echo "docker-compose.yml section for how this propagates to a running server."
} > "$PR_BODY_FILE"

# Not `set -e`-gated: a curl/API failure here must fall through to the
# explicit error handling below (which explains the branch is still safely
# pushed), not abort the script mid-line with a bare curl error.
PR_RESPONSE="$(curl -sf --max-time 30 -X POST \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO_SLUG/pulls" \
    -d "$(python3 -c "
import json, sys
print(json.dumps({
    'title': sys.argv[1],
    'head': sys.argv[2],
    'base': 'main',
    'body': open(sys.argv[3]).read(),
}))
" "$PR_TITLE" "$BRANCH" "$PR_BODY_FILE")")" || PR_RESPONSE=""

PR_URL=""
if [[ -n "$PR_RESPONSE" ]]; then
    PR_URL="$(echo "$PR_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('html_url', ''))" 2>/dev/null || true)"
fi
if [[ -z "$PR_URL" ]]; then
    log_error "PR creation may have failed - no html_url in the API response"
    log_error "(the branch was pushed successfully: $BRANCH)"
    exit 1
fi

log_info "Opened: $PR_URL"
echo "PR_URL=$PR_URL"
