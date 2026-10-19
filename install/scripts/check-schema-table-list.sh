#!/bin/bash
# VoIPBin Install - check SERVICE_SCHEMA_TABLES against the monorepo
#
# SERVICE_SCHEMA_TABLES (common.sh) lists the version table of every service
# that applies its own embedded migrations. The start.sh schema gate and the
# doctor database check both trust it, so a wrong or missing entry makes them
# wait for, or report, the wrong thing.
#
# This script compares the list with the monorepo: for every
# bin-*-manager/migrations/embed.go it reads the VersionTable constant and
# checks that the same table is listed for the same compose service, and that
# no listed service lacks an embed.go.
#
# Usage:
#   ./scripts/check-schema-table-list.sh <monorepo-checkout>
#
#   <monorepo-checkout>  directory containing the bin-*-manager/ directories
#                        (a checkout of the commit the install pins)
#
# Exit codes: 0 list matches, 1 mismatch, 2 usage error.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

if [[ $# -ne 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    [[ $# -eq 1 ]] && exit 0
    exit 2
fi

MONOREPO="$1"
if [[ ! -d "$MONOREPO" ]]; then
    log_error "Not a directory: $MONOREPO"
    exit 2
fi

failed=0
declare -A listed=()

for entry in "${SERVICE_SCHEMA_TABLES[@]}"; do
    table="${entry%%:*}"
    service="${entry#*:}"
    listed["$service"]=1
    embed="$MONOREPO/bin-$service/migrations/embed.go"
    if [[ ! -f "$embed" ]]; then
        log_error "$service: listed with table $table but $embed does not exist"
        failed=1
        continue
    fi
    actual="$(sed -n 's/^const VersionTable = "\([^"]*\)".*/\1/p' "$embed" | head -n 1)"
    if [[ -z "$actual" ]]; then
        log_error "$service: no VersionTable constant found in $embed"
        failed=1
    elif [[ "$actual" != "$table" ]]; then
        log_error "$service: list has $table but VersionTable is $actual"
        failed=1
    fi
done

# Services with embedded migrations that the list does not know about.
for embed in "$MONOREPO"/bin-*/migrations/embed.go; do
    [[ -f "$embed" ]] || continue
    dir="$(basename "$(dirname "$(dirname "$embed")")")"
    service="${dir#bin-}"
    if [[ -z "${listed[$service]:-}" ]]; then
        log_error "$service: has $embed but is missing from SERVICE_SCHEMA_TABLES"
        failed=1
    fi
done

if [[ $failed -ne 0 ]]; then
    exit 1
fi
log_info "SERVICE_SCHEMA_TABLES matches the monorepo (${#SERVICE_SCHEMA_TABLES[@]} services)"
exit 0
