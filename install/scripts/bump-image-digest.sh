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

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source common functions (log_info / log_warn / log_error / log_step /
# resolve_image_digest)
source "$SCRIPT_DIR/common.sh"

LOCK_FILE="${LOCK_FILE:-$PROJECT_DIR/versions.lock}"

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
import sys

lock_file, image_repo, digest, source_commit, generated_at = sys.argv[1:6]

with open(lock_file) as f:
    lock = json.load(f)

images = lock.get("images", {})
if image_repo not in images:
    print(
        f"versions.lock has no established entry for {image_repo!r} - "
        "this script only bumps EXISTING pins. To onboard a brand-new "
        "image, hand-seed it with the literal digest \"NEW\" first "
        "(see generate-versions-lock.sh's header for the seeding contract).",
        file=sys.stderr,
    )
    sys.exit(1)

old_digest = images[image_repo]
images[image_repo] = digest
lock.setdefault("image_source_tags", {})[image_repo] = source_commit
# NOTE: target_commit/target_commit_desc/dbscheme_monorepo_commit are
# deliberately left untouched - see this script's header comment.
lock["generated"] = generated_at
lock["generated_by"] = f"scripts/bump-image-digest.sh ({image_repo}, CI single-image bump)"

with open(lock_file, "w") as f:
    json.dump(lock, f, indent=2)
    f.write("\n")

print(f"OLD_DIGEST={old_digest}")
PYEOF

log_info "  Updated $LOCK_FILE"

SYNC_SCRIPT="$SCRIPT_DIR/sync-compose-images.sh"
if [[ ! -x "$SYNC_SCRIPT" && ! -f "$SYNC_SCRIPT" ]]; then
    log_error "sync-compose-images.sh not found at $SYNC_SCRIPT"
    exit 1
fi

log_step "Propagating to compose file via sync-compose-images.sh..."
bash "$SYNC_SCRIPT"
