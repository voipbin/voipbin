#!/bin/bash
# VoIPBin Install - single-image versions.lock bump
#
# Updates ONE image's entry in versions.lock (images + image_source_tags)
# and propagates it into a compose file via sync-compose-images.sh, without
# touching any other entry.
#
# Usage:
#   ./scripts/bump-image-digest.sh <image-repo> <ref> <source-commit>
#
#   <image-repo>      e.g. voipbin/bin-agent-manager - MUST already exist as
#                     a key in versions.lock's "images" map (this script
#                     only bumps established pins; see generate-versions-lock.sh's
#                     header for how a brand-new entry is seeded).
#   <ref>             the just-pushed image ref to resolve, e.g.
#                     voipbin/bin-agent-manager:$CIRCLE_SHA1 - resolved to its
#                     registry digest via resolve_image_digest() (common.sh).
#                     Accepts a bare "sha256:<64hex>" too, skipping resolution
#                     (useful for tests/local use when the digest is already known).
#   <source-commit>   full git commit SHA the image was built from, recorded
#                     in image_source_tags for traceability.
#
# Environment:
#   LOCK_FILE     path to versions.lock       (default: <project>/versions.lock)
#   COMPOSE_FILE  passed through to sync-compose-images.sh unchanged (its own
#                 default is docker-compose.yml.dist - see that script's header)
#
# Why this exists (not generate-versions-lock.sh):
#   generate-versions-lock.sh resolves ALL tracked images by walking monorepo
#   git history to find, for each service, the newest commit at-or-before a
#   target ref that has a published registry tag - necessary because it runs
#   AFTER the fact, with no direct knowledge of which commit's build actually
#   succeeded. A CI job for ONE service, running immediately after its own
#   `docker push`, already knows exactly which tag it just pushed - no history
#   walk needed. Regenerating all 41 entries for a single-service bump would
#   also touch entries this job has no business touching.
#
# Note on versions.lock's top-level target_commit/target_commit_desc/
# dbscheme_monorepo_commit fields: these describe the state of the LAST FULL
# generate-versions-lock.sh regeneration (a "baseline anchor"), not the
# current state of every individual image - that per-image truth already
# lives in image_source_tags. This script deliberately does not touch those
# three fields; only generate-versions-lock.sh (the full-regen tool) does.
#
# Failure ordering: image-repo membership in versions.lock is checked BEFORE
# any registry call, so a typo'd/untracked image-repo fails fast and cheap
# rather than after an (possibly slow, possibly auth-failing) digest
# resolution. versions.lock is written atomically (temp file + rename,
# matching generate-versions-lock.sh's own write path) so a killed/failed
# run can never leave it truncated or partially written.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source common functions (log_info / log_warn / log_error / log_step /
# resolve_image_digest)
source "$SCRIPT_DIR/common.sh"

export LOCK_FILE="${LOCK_FILE:-$PROJECT_DIR/versions.lock}"

usage() {
    sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -ne 3 ]]; then
    usage
    exit 1
fi

IMAGE_REPO="$1"
REF="$2"
SOURCE_COMMIT="$3"

if [[ ! -f "$LOCK_FILE" ]]; then
    log_error "versions.lock not found at $LOCK_FILE"
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

# Fail fast and cheap on an untracked image-repo BEFORE the (network,
# possibly slow/auth-failing) digest resolution below - a typo shouldn't
# masquerade as a registry-reachability error.
if ! python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    images = json.load(f).get('images', {})
sys.exit(0 if sys.argv[2] in images else 1)
" "$LOCK_FILE" "$IMAGE_REPO"; then
    log_error "versions.lock has no established entry for '$IMAGE_REPO' - this script only"
    log_error "bumps EXISTING pins. To onboard a brand-new image, hand-seed it with the"
    log_error "literal digest \"NEW\" first (see generate-versions-lock.sh's header for the"
    log_error "seeding contract)."
    exit 1
fi

# Resolve the digest: a bare sha256:<64hex> is used as-is (test/local
# convenience), anything else is resolved against the registry.
if [[ "$REF" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    DIGEST="$REF"
else
    log_step "Resolving digest for $REF..."
    if ! DIGEST=$(resolve_image_digest "$REF"); then
        log_error "Could not resolve a digest for $REF (registry unreachable, not authenticated, or ref doesn't exist)"
        exit 1
    fi
fi

if ! [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    log_error "Resolved digest has an unexpected shape: $DIGEST"
    exit 1
fi

log_info "  $IMAGE_REPO -> $DIGEST (source commit $SOURCE_COMMIT)"

GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"

python3 - "$LOCK_FILE" "$IMAGE_REPO" "$DIGEST" "$SOURCE_COMMIT" "$GENERATED_AT" <<'PYEOF'
import json
import os
import sys

lock_file, image_repo, digest, source_commit, generated_at = sys.argv[1:6]

with open(lock_file) as f:
    lock = json.load(f)

images = lock.get("images", {})
# image_repo's presence was already checked by the shell before we got
# here; re-checking would just duplicate that gate, so trust it here.
old_digest = images[image_repo]
images[image_repo] = digest
lock.setdefault("image_source_tags", {})[image_repo] = source_commit
# NOTE: target_commit/target_commit_desc/dbscheme_monorepo_commit are
# deliberately left untouched - see this script's header comment.
lock["generated"] = generated_at
lock["generated_by"] = f"scripts/bump-image-digest.sh ({image_repo}, CI single-image bump)"

# Atomic write: temp file + rename, so a killed/failed run never leaves
# versions.lock truncated or partially written (matches
# generate-versions-lock.sh's own write path).
tmp_path = lock_file + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(lock, f, indent=2)
    f.write("\n")
os.replace(tmp_path, lock_file)

print(f"OLD_DIGEST={old_digest}")
PYEOF

log_info "  Updated $LOCK_FILE"

SYNC_SCRIPT="$SCRIPT_DIR/sync-compose-images.sh"
if [[ ! -f "$SYNC_SCRIPT" ]]; then
    log_error "sync-compose-images.sh not found at $SYNC_SCRIPT"
    log_error "versions.lock was already updated (see above) but the compose file was NOT"
    log_error "synced - re-run scripts/sync-compose-images.sh manually once it exists, or"
    log_error "'git checkout -- versions.lock' to fully undo this run."
    exit 1
fi

log_step "Propagating to compose file via sync-compose-images.sh..."
if ! bash "$SYNC_SCRIPT"; then
    log_error "sync-compose-images.sh failed - versions.lock was updated but the compose"
    log_error "file was NOT. The working tree is now inconsistent; recover with:"
    log_error "  git checkout -- versions.lock"
    log_error "and retry, or investigate the sync-compose-images.sh failure above first."
    exit 1
fi
