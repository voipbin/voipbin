#!/bin/bash
# VoIPBin Sandbox - versions.lock Generator
# Regenerates versions.lock by pinning every tracked voipbin/* image to the
# nearest registry tag at-or-before a target monorepo commit.
#
# Usage:
#   ./scripts/generate-versions-lock.sh [<monorepo-git-ref>]
#
#   <monorepo-git-ref>  commit SHA or branch name in the monorepo checkout
#                       (default: main)
#
# Environment:
#   MONOREPO_PATH  path to the monorepo checkout (default: ~/gitvoipbin/monorepo)
#   MAX_LOOKBACK   max commits to walk back per service (default: 200)
#   OUTPUT_FILE    where to write the result (default: <project>/versions.lock)
#
# How the resolution works:
#   CircleCI (.circleci/config_work.yml) tags every service image as
#   voipbin/<service-dir>:$CIRCLE_SHA1, and .circleci/config.yml's path-filtering
#   mapping only builds a service when its own directory changed. CI runs on the
#   main-line commit (the merge commit for a PR), so for a target commit T and
#   service directory D the candidate tags are exactly the commits returned by
#   `git log --first-parent T -- D`, newest first. The first candidate whose tag
#   actually exists in the registry is the pin.
#
#   --first-parent is load-bearing: without it, git's history simplification
#   returns the PR's branch commit instead of the merge commit CI built, and that
#   branch commit has no registry tag. (Verified against the existing lock: with
#   --first-parent, all 31 monorepo-built images (30 bin-* plus
#   voip-asterisk-proxy) resolve to exactly the image_source_tags recorded for
#   target 0ce70d7d.)
#
# Fallback: images that are not built from the monorepo (square-*, voip-kamailio,
# voip-rtpengine, voip-asterisk-call/conference/registrar — see the existing
# lock's note_separate_repos) and services with no registry tag at-or-before the
# target keep their CURRENT pinned digest, with a warning on stderr. The script
# never pins to something newer than the target and never drops an entry.
#
# Seeded (new) entries: onboarding a service that the lock has never tracked is
# done by hand-adding its image key to versions.lock's "images" map with the
# literal digest value "NEW" (see SEEDED_DIGEST_MARKER below). Such an entry has
# NO current pin to fall back to, so the fallback path above is a trap for it: a
# placeholder that failed to resolve would be written back as a legitimate-looking
# pin, propagate into docker-compose.yml via scripts/sync-compose-images.sh, and
# only fail at `docker compose pull` on a customer machine. Therefore a seeded
# entry that cannot be resolved at the target commit is a HARD ERROR: the script
# exits non-zero and versions.lock is NOT written. Established entries are
# unaffected and keep the fallback behavior described above.
#
# Idempotency: two runs against the same target with no new commits produce a
# byte-identical versions.lock apart from the "generated" timestamp.
#
# NOTE: this only rewrites versions.lock. Bumping the pin for real is gated on
# the corresponding monorepo changes being merged and CI having published images
# at that merge commit.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source common functions (log_info / log_warn / log_error / log_step)
source "$SCRIPT_DIR/common.sh"

LOCK_FILE="$PROJECT_DIR/versions.lock"
MONOREPO_PATH="${MONOREPO_PATH:-$HOME/gitvoipbin/monorepo}"
MAX_LOOKBACK="${MAX_LOOKBACK:-200}"
OUTPUT_FILE="${OUTPUT_FILE:-$LOCK_FILE}"
TARGET_REF="${1:-main}"

# Digest placeholder marking an image as newly seeded (never pinned before).
# See the "Seeded (new) entries" note in the header.
SEEDED_DIGEST_MARKER="NEW"

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ "$TARGET_REF" == "-h" || "$TARGET_REF" == "--help" ]]; then
    usage
    exit 0
fi

# =============================================================================
# Preflight
# =============================================================================
preflight() {
    local missing=0

    for cmd in git docker python3; do
        if ! command -v "$cmd" &> /dev/null; then
            log_error "Required command not found: $cmd"
            missing=1
        fi
    done
    [[ $missing -eq 0 ]] || exit 1

    if [[ ! -f "$LOCK_FILE" ]]; then
        log_error "versions.lock not found at $LOCK_FILE"
        log_error "This script regenerates an existing lock; it cannot bootstrap one."
        exit 1
    fi

    if ! git -C "$MONOREPO_PATH" rev-parse --git-dir &> /dev/null; then
        log_error "MONOREPO_PATH is not a git checkout: $MONOREPO_PATH"
        log_error "Set MONOREPO_PATH to your monorepo checkout, e.g.:"
        log_error "  MONOREPO_PATH=/path/to/monorepo $0 main"
        exit 1
    fi

    if ! docker info &> /dev/null; then
        log_error "Cannot talk to the Docker daemon (needed for registry lookups)"
        exit 1
    fi

    # Registry reachability/auth smoke check: probe one image at its CURRENT
    # pinned digest (from the existing lock, not a tag we're trying to resolve).
    # This must always succeed if the registry is reachable and we're
    # authenticated, regardless of what target commit we're regenerating for.
    # Without this, an unauthenticated/unreachable registry makes every single
    # tag-resolution attempt fail the exact same way as "tag genuinely never
    # published" — every image silently falls back to its current pin and the
    # script exits 0, looking like a successful no-op run instead of a broken
    # registry connection.
    #
    # Seeded entries (digest == SEEDED_DIGEST_MARKER) are SKIPPED when choosing
    # the probe image: they have no real pin yet, so probing one would fail with
    # a misleading "registry unreachable" message.
    local probe_image probe_digest
    # LOCK_FILE passed via argv, not string-interpolated into the Python source,
    # matching the safer pattern the write step below already uses.
    probe_image=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    lock = json.load(f)
images = lock.get('images', {})
for name in sorted(images):
    if images[name].startswith('sha256:'):
        print(f'{name}@{images[name]}')
        break
" "$LOCK_FILE")
    if [[ -z "$probe_image" ]]; then
        log_error "versions.lock has no already-pinned image to probe registry access with"
        log_error "(every entry is a seeded '$SEEDED_DIGEST_MARKER' placeholder — at least one"
        log_error "established pin is required for the registry reachability check)"
        exit 1
    fi
    if ! probe_digest=$(resolve_digest "$probe_image"); then
        log_error "Registry reachability/auth check failed: could not inspect $probe_image"
        log_error "This image is already pinned in the current versions.lock, so this"
        log_error "failure means the registry is unreachable or you are not authenticated"
        log_error "(try 'docker login'), not that the image doesn't exist."
        exit 1
    fi
    if [[ "$probe_digest" != "${probe_image##*@}" ]]; then
        log_error "Registry probe returned an unexpected digest for $probe_image — refusing to proceed"
        exit 1
    fi
}

# =============================================================================
# Registry digest lookup for a single image:tag (no pull — manifest only)
# =============================================================================
# Prints the manifest digest on success, exits non-zero if the tag is absent.
resolve_digest() {
    local ref="$1"

    docker manifest inspect -v "$ref" 2> /dev/null | python3 -c '
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

# Single-platform image: a dict with Descriptor. Multi-platform manifest list:
# a list of per-platform entries. Prefer linux/amd64 for the latter.
if isinstance(data, dict):
    digest = data.get("Descriptor", {}).get("digest", "")
else:
    digest = ""
    for entry in data:
        desc = entry.get("Descriptor", {})
        platform = desc.get("platform", {})
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            digest = desc.get("digest", "")
            break
    if not digest and data:
        digest = data[0].get("Descriptor", {}).get("digest", "")

if not digest.startswith("sha256:"):
    sys.exit(1)
print(digest)
'
}

# =============================================================================
# Main
# =============================================================================
main() {
    preflight

    local target_commit target_desc
    if ! target_commit=$(git -C "$MONOREPO_PATH" rev-parse --verify --quiet "${TARGET_REF}^{commit}"); then
        log_error "Cannot resolve '$TARGET_REF' to a commit in $MONOREPO_PATH"
        exit 1
    fi
    target_desc=$(git -C "$MONOREPO_PATH" log -1 --format='%s (%cs)' "$target_commit")

    log_step "Regenerating versions.lock"
    log_info "  Monorepo:      $MONOREPO_PATH"
    log_info "  Target ref:    $TARGET_REF"
    log_info "  Target commit: $target_commit"
    log_info "  Target desc:   $target_desc"
    log_info "  Output:        $OUTPUT_FILE"
    echo ""

    local images
    images=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    lock = json.load(f)
for name in sorted(lock.get('images', {})):
    print(name)
" "$LOCK_FILE")

    if [[ -z "$images" ]]; then
        log_error "No images tracked in $LOCK_FILE — nothing to regenerate"
        exit 1
    fi

    # Newly seeded entries: no current pin exists, so failing to resolve one is
    # fatal rather than a fallback. See the header's "Seeded (new) entries" note.
    local seeded
    seeded=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    lock = json.load(f)
for name, digest in sorted(lock.get('images', {}).items()):
    if digest == sys.argv[2]:
        print(name)
" "$LOCK_FILE" "$SEEDED_DIGEST_MARKER")
    if [[ -n "$seeded" ]]; then
        log_info "Seeded (new) images, resolution failure is fatal for these:"
        while IFS= read -r image; do
            [[ -n "$image" ]] && log_info "  $image"
        done <<< "$seeded"
        echo ""
    fi

    # Resolved pins are collected as "image<TAB>tag<TAB>digest" lines. Images
    # absent from this file fall back to their current pin (see fallback note
    # in the header).
    local resolved_file
    resolved_file=$(mktemp)
    # shellcheck disable=SC2064  # expand resolved_file now, not at trap time
    trap "rm -f '$resolved_file'" EXIT

    local total=0 resolved=0 fallback=0
    local image service_dir candidates sha digest

    # True if $1 is a seeded (never-before-pinned) image.
    is_seeded() {
        [[ -n "$seeded" ]] && grep -qxF "$1" <<< "$seeded"
    }

    # Abort the whole run: a seeded entry has no pin to fall back to.
    fail_seeded() {
        log_error "$1: $2"
        log_error "A seeded image has no current pin to fall back to, so this cannot be"
        log_error "carried forward. $OUTPUT_FILE was NOT written."
        log_error "Either pick a target commit where the image was published, or remove the"
        log_error "seeded entry from $LOCK_FILE."
        exit 1
    }

    while IFS= read -r image; do
        [[ -n "$image" ]] || continue
        total=$((total + 1))

        # Image name maps 1:1 to the monorepo directory that builds it:
        # voipbin/bin-agent-manager <-> bin-agent-manager/
        service_dir="${image#voipbin/}"

        # Check the tree AT target_commit, not the live working-tree checkout —
        # MONOREPO_PATH may be on a different branch/commit than target_commit,
        # and a service directory could be renamed/removed after target_commit.
        # A live `-d` filesystem check would misclassify either case as "not
        # built from the monorepo" and skip a service that git log could
        # actually resolve correctly.
        if ! git -C "$MONOREPO_PATH" cat-file -e "$target_commit:$service_dir" 2> /dev/null; then
            if is_seeded "$image"; then
                fail_seeded "$image" "no '$service_dir/' directory at $target_commit"
            fi
            log_warn "$image: no '$service_dir/' directory at $target_commit (built from a separate repo) — keeping current pin"
            fallback=$((fallback + 1))
            continue
        fi

        # --first-parent: follow the main line only, so PR merge commits (which
        # are what CI builds and tags) are returned instead of branch commits.
        candidates=$(git -C "$MONOREPO_PATH" log \
            --first-parent \
            --format='%H' \
            --max-count="$MAX_LOOKBACK" \
            "$target_commit" -- "$service_dir")

        if [[ -z "$candidates" ]]; then
            if is_seeded "$image"; then
                fail_seeded "$image" "no commit touching '$service_dir/' at or before $target_commit"
            fi
            log_warn "$image: no commit touching '$service_dir/' at or before $target_commit — keeping current pin"
            fallback=$((fallback + 1))
            continue
        fi

        digest=""
        while IFS= read -r sha; do
            [[ -n "$sha" ]] || continue
            if digest=$(resolve_digest "$image:$sha"); then
                printf '%s\t%s\t%s\n' "$image" "$sha" "$digest" >> "$resolved_file"
                log_info "$image -> ${sha:0:12} ($digest)"
                resolved=$((resolved + 1))
                break
            fi
            digest=""
        done <<< "$candidates"

        if [[ -z "$digest" ]]; then
            if is_seeded "$image"; then
                fail_seeded "$image" "no registry tag found in the last $MAX_LOOKBACK commits touching '$service_dir/'"
            fi
            log_warn "$image: no registry tag found in the last $MAX_LOOKBACK commits touching '$service_dir/' — keeping current pin"
            fallback=$((fallback + 1))
        fi
    done <<< "$images"

    echo ""
    log_step "Writing $OUTPUT_FILE"

    python3 - "$LOCK_FILE" "$OUTPUT_FILE" "$resolved_file" "$target_commit" "$target_desc" <<'PYEOF'
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone

lock_path, out_path, resolved_path, target_commit, target_desc = sys.argv[1:6]

with open(lock_path) as f:
    old = json.load(f)

old_images = old.get("images", {})
old_tags = old.get("image_source_tags", {})

resolved = {}
if os.path.exists(resolved_path):
    with open(resolved_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            image, tag, digest = line.split("\t")
            resolved[image] = (tag, digest)

images = OrderedDict()
tags = OrderedDict()
for name in sorted(old_images):
    if name in resolved:
        tag, digest = resolved[name]
    else:
        # Fallback: carry the current pin forward untouched (warned on stderr
        # by the caller). Never newer than the target, never dropped.
        tag, digest = old_tags.get(name, ""), old_images[name]
    # Belt-and-braces: the bash loop already hard-errors on an unresolved seeded
    # entry, so reaching here with a non-digest value would be a logic bug. Fail
    # rather than write a lock that looks pinned but isn't.
    if not digest.startswith("sha256:"):
        sys.exit(
            f"refusing to write {out_path}: {name} has no resolved digest "
            f"(value {digest!r})"
        )
    images[name] = digest
    tags[name] = tag

# Same schema as before, plus generated_by so a hand-edited lock stays visually
# distinguishable from a generated one.
new = OrderedDict()
new["_comment"] = old.get(
    "_comment",
    "Single-commit ancestry pin. All bin-* services share one monorepo lineage.",
)
new["pin_strategy"] = old.get("pin_strategy", "ancestry-based single commit")
new["target_commit"] = target_commit
new["target_commit_desc"] = target_desc
new["dbscheme_monorepo_commit"] = target_commit
new["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
new["generated_by"] = "scripts/generate-versions-lock.sh"
new["images"] = images
new["image_source_tags"] = tags
if "note_separate_repos" in old:
    new["note_separate_repos"] = old["note_separate_repos"]

tmp_path = out_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(new, f, indent=2)
    f.write("\n")
os.replace(tmp_path, out_path)
PYEOF

    echo ""
    log_info "Done: $resolved/$total images repinned, $fallback kept at their current pin"
    if [[ $fallback -gt 0 ]]; then
        log_warn "Review the warnings above before committing $OUTPUT_FILE"
    fi

    echo ""
    log_warn "docker-compose.yml pins image digests independently of this lock."
    log_warn "Run ./scripts/sync-compose-images.sh to propagate the new pins, or the"
    log_warn "regenerated lock has no effect on what actually runs."
}

main "$@"
