#!/bin/bash
# VoIPBin Install - First admin account helper
#
# Creates (or resets) the admin login of a customer on a running stack:
#   1. customer-control customer create (skipped when the email exists)
#   2. waits for agent-manager to create the customer's admin agent
#      (username == the customer email)
#   3. sets the admin agent password
#   4. verifies the password with POST /auth/login
#   5. sets the customer's billing plan to unlimited (self-hosted installs
#      have no metering; same as the dev seed path)
#
# Usage: VOIPBIN_ADMIN_PASSWORD=... ./scripts/create-admin.sh --email <email>
#            [--name <name>] [--reset] [--wait <seconds>]
#
# The password is read from the VOIPBIN_ADMIN_PASSWORD environment variable
# only, never from argv: it reaches agent-control through `docker exec -e
# PASSWORD` (name only, the value stays in the environment) and /auth/login
# through curl's stdin.
#
# Output: progress on stderr; the last stdout line is
#   VOIPBIN_ADMIN: status=ok customer_id=<id> agent_id=<id>
#   VOIPBIN_ADMIN: status=exists customer_id=<id>      (exit 3, nothing changed)
#   VOIPBIN_ADMIN: status=error reason="..."
# Exit codes: 0 ok; 1 failure; 3 the email already exists and --reset was
# not given (nothing was changed).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_URL="${VOIPBIN_ADMIN_API_URL:-https://localhost:8443}"
# Seconds between polls (tests set 0). The wait budgets below count 2s per
# poll when the interval is 0, so they always end.
POLL_INTERVAL="${VOIPBIN_ADMIN_POLL_INTERVAL:-2}"
[[ "$POLL_INTERVAL" =~ ^[0-9]+$ ]] || POLL_INTERVAL=2
POLL_TICK="$POLL_INTERVAL"
[[ "$POLL_TICK" -gt 0 ]] || POLL_TICK=2
CUSTOMER_CONTAINER="voipbin-customer-mgr"
AGENT_CONTAINER="voipbin-agent-mgr"
BILLING_CONTAINER="voipbin-billing-mgr"

log() { echo "[admin] $*" >&2; }

fail() {
    log "ERROR: $1"
    echo "VOIPBIN_ADMIN: status=error reason=\"$1\""
    exit 1
}

# JSON helpers (python3 is already a CLI requirement; jq is not).
# json_id_where <field>: stdin is a JSON list; prints the "id" of the first
# item whose <field> equals $ADMIN_EMAIL ('' when none or on parse error).
json_id_where() {
    python3 -c '
import json, os, sys
try:
    items = json.load(sys.stdin)
    print(next((i.get("id") or "" for i in items if i.get(sys.argv[1]) == os.environ["ADMIN_EMAIL"]), ""))
except Exception:
    print("")
' "$1"
}

# json_field <name>: stdin is a JSON object; prints its <name> field ('').
json_field() {
    python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1])
except Exception:
    v = None
print("" if v is None else v)
' "$1"
}

EMAIL=""
NAME="Admin"
RESET="false"
WAIT=60
while [[ $# -gt 0 ]]; do
    case "$1" in
        --email) EMAIL="${2:-}"; shift 2 || fail "--email requires a value" ;;
        --name) NAME="${2:-}"; shift 2 || fail "--name requires a value" ;;
        --reset) RESET="true"; shift ;;
        --wait) WAIT="${2:-}"; shift 2 || fail "--wait requires a value" ;;
        *) fail "unknown argument: $1" ;;
    esac
done

[[ -n "$EMAIL" ]] || fail "--email is required"
[[ "$WAIT" =~ ^[0-9]+$ ]] || fail "--wait must be a number of seconds"
[[ -n "${VOIPBIN_ADMIN_PASSWORD:-}" ]] || fail "VOIPBIN_ADMIN_PASSWORD is not set"
export ADMIN_EMAIL="$EMAIL"

# Step 1: find or create the customer
find_customer_id() {
    docker exec "$CUSTOMER_CONTAINER" /app/bin/customer-control customer list --limit 1000 2>/dev/null \
        | json_id_where email
}

CUSTOMER_ID=$(find_customer_id)
if [[ -n "$CUSTOMER_ID" ]]; then
    if [[ "$RESET" != "true" ]]; then
        log "A customer with $EMAIL already exists; nothing changed (use --reset to set a new password)."
        echo "VOIPBIN_ADMIN: status=exists customer_id=$CUSTOMER_ID"
        exit 3
    fi
    log "Customer $EMAIL exists ($CUSTOMER_ID); resetting the admin password."
else
    log "Creating customer $EMAIL..."
    if ! docker exec "$CUSTOMER_CONTAINER" /app/bin/customer-control customer create \
            --name "$NAME" --email "$EMAIL" > /dev/null 2>&1; then
        fail "customer create failed (docker logs $CUSTOMER_CONTAINER)"
    fi
    CUSTOMER_ID=$(find_customer_id)
    [[ -n "$CUSTOMER_ID" ]] || fail "customer was created but not found in customer list"
fi
log "Customer ID: $CUSTOMER_ID"

# Step 2: wait for the admin agent (created asynchronously by agent-manager)
AGENT_ID=""
waited=0
while :; do
    AGENT_ID=$(docker exec "$AGENT_CONTAINER" /app/bin/agent-control agent list --customer-id "$CUSTOMER_ID" 2>/dev/null \
        | json_id_where username)
    [[ -n "$AGENT_ID" ]] && break
    [[ "$waited" -ge "$WAIT" ]] && break
    sleep "$POLL_INTERVAL"
    waited=$((waited + POLL_TICK))
done
[[ -n "$AGENT_ID" ]] || fail "admin agent with username $EMAIL not created within ${WAIT}s (docker logs $AGENT_CONTAINER)"
log "Admin agent ID: $AGENT_ID"

# Step 3: set the password (value passed by environment name only)
if ! PASSWORD="$VOIPBIN_ADMIN_PASSWORD" docker exec -e PASSWORD "$AGENT_CONTAINER" \
        /app/bin/agent-control agent update-password --id "$AGENT_ID" > /dev/null 2>&1; then
    fail "agent update-password failed (docker logs $AGENT_CONTAINER)"
fi

# Step 4: verify the login. The request body is built from the environment
# and sent on stdin, so the password is not in curl's argv.
login_ok="false"
for _ in 1 2 3 4 5; do
    token=$(python3 -c '
import json, os
print(json.dumps({"username": os.environ["ADMIN_EMAIL"], "password": os.environ["VOIPBIN_ADMIN_PASSWORD"]}))
' | curl -sk -X POST "$API_URL/auth/login" -H "Content-Type: application/json" --data-binary @- 2>/dev/null \
        | json_field token)
    if [[ -n "$token" ]]; then
        login_ok="true"
        break
    fi
    sleep "$POLL_INTERVAL"
done
unset token
[[ "$login_ok" == "true" ]] || fail "password was set but POST /auth/login did not return a token"
log "Login verified."

# Step 5: billing plan (unlimited). A new customer's billing account is
# created asynchronously, so poll until billing_account_id is set.
billing_id=""
waited=0
while :; do
    billing_id=$(docker exec "$CUSTOMER_CONTAINER" /app/bin/customer-control customer get --id "$CUSTOMER_ID" 2>/dev/null \
        | json_field billing_account_id)
    if [[ -n "$billing_id" && "$billing_id" != "00000000-0000-0000-0000-000000000000" ]]; then
        break
    fi
    billing_id=""
    [[ "$waited" -ge 30 ]] && break
    sleep "$POLL_INTERVAL"
    waited=$((waited + POLL_TICK))
done
if [[ -n "$billing_id" ]]; then
    if docker exec "$BILLING_CONTAINER" /app/bin/billing-control account update-plan-type \
            --id "$billing_id" --plan-type unlimited > /dev/null 2>&1; then
        log "Billing plan set to unlimited."
    else
        log "WARNING: could not set the billing plan; run: docker exec $BILLING_CONTAINER /app/bin/billing-control account update-plan-type --id $billing_id --plan-type unlimited"
    fi
else
    log "WARNING: billing account not ready after 30s; the billing plan was not set."
fi

echo "VOIPBIN_ADMIN: status=ok customer_id=$CUSTOMER_ID agent_id=$AGENT_ID"
exit 0
