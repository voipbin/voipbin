#!/bin/bash
# VoIPBin Sandbox - versions.lock -> docker-compose.yml image sync
# Rewrites every `image: voipbin/<name>@sha256:...` line in docker-compose.yml to
# the digest versions.lock pins for that image.
#
# Usage:
#   ./scripts/sync-compose-images.sh
#
# Environment:
#   LOCK_FILE     path to versions.lock       (default: <project>/versions.lock)
#   COMPOSE_FILE  path to docker-compose.yml  (default: <project>/docker-compose.yml)
#
# Why this exists:
#   docker-compose.yml hardcodes image digests independently of versions.lock and
#   nothing used to reconcile them, so regenerating the lock had ZERO effect on
#   what actually runs. Both files stay committed and standalone-runnable; this
#   script is the explicit reconciliation step, so `git diff` remains meaningful
#   for image bumps. (The rejected alternative was templating compose image
#   references from the lock via ${VAR} interpolation, which makes the committed
#   compose file non-runnable and defers the failure to first boot on a customer
#   machine.)
#
# Scope:
#   - Only `image:` lines whose repository matches a `voipbin/*` name tracked in
#     the lock are touched. Third-party images (mysql, redis, rabbitmq, coredns,
#     postgres/pgvector, clickhouse) are never rewritten.
#   - A tracked image may legitimately appear on SEVERAL `image:` lines
#     (voipbin/voip-asterisk-proxy is shared by the three asterisk-*-proxy
#     services). EVERY occurrence is rewritten to the single lock digest — a
#     repeated image is normal, not a duplicate-key error, and converging all
#     occurrences on one digest is precisely the point.
#
# Drift (both directions is an error, since either means the lock and the stack
# have diverged):
#   - compose references a tracked-looking voipbin/* image the lock does not pin
#   - the lock pins a voipbin/* image compose never references
#     ... except images on LOCK_ONLY_IMAGES below.
#
# Idempotency: running it twice is a no-op the second time.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source common functions (log_info / log_warn / log_error / log_step)
source "$SCRIPT_DIR/common.sh"

LOCK_FILE="${LOCK_FILE:-$PROJECT_DIR/versions.lock}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_DIR/docker-compose.yml}"

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

# =============================================================================
# Images deliberately pinned in versions.lock but absent from docker-compose.yml
# =============================================================================
# Each entry needs a rationale — without one the bidirectional drift check would
# fail forever on an intentional omission, and people would learn to ignore it.
#
# voipbin/bin-sentinel-manager
#   Permanently excluded from compose: it authenticates only via
#   rest.InClusterConfig() and exists solely to watch Kubernetes pod lifecycle
#   events, so a Compose deployment has nothing for it to do. It stays pinned in
#   the lock anyway so the lock keeps describing the full monorepo snapshot (a
#   commitment carried over from the single-server-production-mode design).
LOCK_ONLY_IMAGES=(
    voipbin/bin-sentinel-manager
)

# =============================================================================
# Main
# =============================================================================
main() {
    if [[ ! -f "$LOCK_FILE" ]]; then
        log_error "versions.lock not found at $LOCK_FILE"
        exit 1
    fi
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        log_error "docker-compose.yml not found at $COMPOSE_FILE"
        exit 1
    fi
    if ! command -v python3 &> /dev/null; then
        log_error "Required command not found: python3"
        exit 1
    fi

    log_step "Syncing docker-compose.yml image digests from versions.lock"
    log_info "  Lock:    $LOCK_FILE"
    log_info "  Compose: $COMPOSE_FILE"
    echo ""

    python3 - "$LOCK_FILE" "$COMPOSE_FILE" "${LOCK_ONLY_IMAGES[@]}" <<'PYEOF'
import json
import os
import re
import sys

lock_path, compose_path = sys.argv[1:3]
lock_only = set(sys.argv[3:])

with open(lock_path) as f:
    lock = json.load(f)

locked = lock.get("images", {})
if not locked:
    sys.exit(f"versions.lock has no images: {lock_path}")

for name, digest in sorted(locked.items()):
    if not digest.startswith("sha256:"):
        sys.exit(
            f"versions.lock entry {name} is not a resolved digest ({digest!r}); "
            "regenerate the lock before syncing compose"
        )

with open(compose_path) as f:
    original = f.read()

# `image: voipbin/<name>@sha256:<hex>` — the only form compose uses for
# monorepo-built images. Third-party images never match, since the repository
# prefix is anchored to `voipbin/`. The optional trailing group tolerates an
# inline `# comment` after the digest so such a line is rewritten (comment
# preserved) rather than silently skipped — a skipped-but-present tracked image
# would otherwise dodge both the rewrite and the drift check.
LINE_RE = re.compile(
    r"^(?P<indent>\s*)image:\s*(?P<name>voipbin/[A-Za-z0-9._-]+)@sha256:(?P<digest>[0-9a-f]+)(?P<trail>\s*(?:#.*)?)$"
)

referenced = {}   # image name -> number of compose lines referencing it
rewritten = 0
unknown = []
out_lines = []

for lineno, line in enumerate(original.splitlines(keepends=True), start=1):
    match = LINE_RE.match(line.rstrip("\n"))
    if not match:
        out_lines.append(line)
        continue

    name = match.group("name")
    referenced[name] = referenced.get(name, 0) + 1

    if name not in locked:
        unknown.append((lineno, name))
        out_lines.append(line)
        continue

    want = locked[name]
    have = "sha256:" + match.group("digest")
    if have == want:
        out_lines.append(line)
        continue

    newline = "\n" if line.endswith("\n") else ""
    trail = match.group("trail").rstrip()
    out_lines.append(f"{match.group('indent')}image: {name}@{want}{trail}{newline}")
    rewritten += 1
    print(f"  {name}: {have[:19]}... -> {want[:19]}...  (line {lineno})")

errors = []

for lineno, name in unknown:
    errors.append(
        f"docker-compose.yml:{lineno} references {name}, which versions.lock does not pin"
    )

for name in sorted(locked):
    if name in referenced or name in lock_only:
        continue
    errors.append(
        f"versions.lock pins {name}, which docker-compose.yml never references "
        "(add it to compose, or to LOCK_ONLY_IMAGES with a rationale)"
    )

if errors:
    for err in errors:
        print(f"DRIFT: {err}", file=sys.stderr)
    sys.exit(1)

updated = "".join(out_lines)
if updated != original:
    tmp_path = compose_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(updated)
    os.replace(tmp_path, compose_path)

total_lines = sum(referenced.values())
print(
    f"\n{rewritten} image line(s) rewritten; "
    f"{len(referenced)} tracked image(s) across {total_lines} compose line(s); "
    f"{len(lock_only & set(locked))} lock-only image(s) allowlisted"
)
PYEOF

    echo ""
    log_info "docker-compose.yml is in sync with versions.lock"
}

main "$@"
