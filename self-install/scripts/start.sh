#!/bin/bash
# VoIPBin Sandbox - Start Script
# Orchestrates the full startup process with dependency and environment checks
#
# Usage: sudo ./voipbin start
#
# This script requires sudo for:
#   - VoIP network interface setup (macvlan)
#   - DNS forwarding configuration

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on — deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
# resolv.conf path (overridable so tests can stub host probes)
RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"

# All mysql invocations against voipbin-db read the password from the
# CONTAINER's env (MYSQL_ROOT_PASSWORD, injected via docker-compose.yml)
# inside a `sh -c`, never on the host's `docker exec` argv — the naive
# `-p"${MYSQL_ROOT_PASSWORD:-root_password}"` substitution expands on the
# HOST shell and puts the real password in `docker exec`'s argv, visible to
# any local user via `ps aux`. Matches migrate.sh's MYSQL_IN_DB idiom.
START_MYSQL_IN_DB='exec mysql -u"$0" -p"${MYSQL_ROOT_PASSWORD:-root_password}"'

# Source common functions
source "$SCRIPT_DIR/common.sh"

# =============================================================================
# Result line + exit helpers (design §2.2 pattern, shared with init.sh)
# Every exit path — including set -e aborts — must end with a
# VOIPBIN_START: status=ok|error line on stdout.
# =============================================================================

# emit_result <status> [detail...]
emit_result() {
    local status="$1"
    shift
    if [ $# -gt 0 ]; then
        echo "VOIPBIN_START: status=$status $*"
    else
        echo "VOIPBIN_START: status=$status"
    fi
    START_RESULT_EMITTED="true"
}

# EXIT trap: registered inside main() (NOT at top level — the bats helper
# sources this file minus 'main "$@"', so a top-level trap would fire at
# every test-shell exit). The already-emitted flag prevents double printing.
start_exit_trap() {
    local code=$?
    if [ "${START_RESULT_EMITTED:-false}" != "true" ]; then
        if [ $code -eq 0 ]; then
            echo "VOIPBIN_START: status=ok"
        else
            echo "VOIPBIN_START: status=error reason=\"start aborted (exit $code)\""
        fi
    fi
}

# Check if a command exists
check_command() {
    if command -v "$1" &> /dev/null; then
        return 0
    fi
    return 1
}

# Check all required dependencies
check_dependencies() {
    log_step "Checking dependencies..."
    local missing=()

    # Docker
    if check_command docker; then
        local docker_version=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
        log_info "Docker: $docker_version"
    else
        missing+=("docker")
    fi

    # Docker Compose
    if docker compose version &>/dev/null; then
        local compose_version=$(docker compose version --short 2>/dev/null)
        log_info "Docker Compose: $compose_version"
    else
        missing+=("docker-compose")
    fi

    # Python3
    if check_command python3; then
        local python_version=$(python3 --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+')
        log_info "Python: $python_version"
    else
        missing+=("python3")
    fi

    # Alembic is NO LONGER required on the host: schema migrations run inside
    # a container via scripts/migrate.sh (delegated by start.sh and
    # init_database.sh). Only informational.
    if check_command alembic; then
        local alembic_version=$(alembic --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' || echo "installed")
        log_info "Alembic: $alembic_version (host install not needed - migrations are containerized)"
    fi

    # OpenSSL (for certificate generation)
    if check_command openssl; then
        local openssl_version=$(openssl version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
        log_info "OpenSSL: $openssl_version"
    else
        missing+=("openssl")
    fi

    # Git (optional, for downloading dbscheme)
    if check_command git; then
        local git_version=$(git --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+')
        log_info "Git: $git_version"
    else
        log_warn "Git: not installed (optional, for downloading database schema)"
    fi

    # Check if Docker daemon is running
    if ! docker info &>/dev/null; then
        log_error "Docker daemon is not running!"
        missing+=("docker-daemon")
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        echo ""
        log_error "Missing required dependencies: ${missing[*]}"
        log_error "Please install them and try again."
        exit 1
    fi

    log_info "All required dependencies found!"
}

# Setup mkcert for browser-trusted certificates
setup_mkcert() {
    log_step "Checking SSL certificate setup..."

    # TLS gate (§2.4): with BYO certificates, never install mkcert, never
    # inspect issuers, never delete certs/. Missing/expired cert is a
    # fail-fast error naming install-certs.sh, never an auto-regeneration.
    local tls_mode
    tls_mode=$(get_env_var "$PROJECT_DIR/.env" TLS_MODE)
    if [ "$tls_mode" = "byo" ]; then
        local byo_cert="$PROJECT_DIR/certs/api/cert.pem"
        if [ ! -f "$byo_cert" ]; then
            log_error "TLS_MODE=byo but $byo_cert is missing."
            log_error "Install your certificate: ./scripts/install-certs.sh <fullchain.pem> <privkey.pem>"
            return 1
        fi
        if ! openssl x509 -in "$byo_cert" -noout -checkend 0 &>/dev/null; then
            log_error "TLS_MODE=byo and $byo_cert is expired."
            log_error "Renew and reinstall: ./scripts/install-certs.sh <fullchain.pem> <privkey.pem>"
            return 1
        fi
        log_info "Certificates: BYO (TLS_MODE=byo) - skipping mkcert management"
        return 0
    fi

    # Presence check only (design §2.5): package install and CA trust install
    # live exclusively in setup-host.sh — the two inline escalation sites
    # (apt install, mkcert -install) were removed from here. Missing mkcert in
    # internal mode is a fail-fast, never an inline escalation.
    if ! command -v mkcert &> /dev/null; then
        log_error "mkcert is not installed."
        log_error "Run host setup first: sudo ./scripts/setup-host.sh"
        return 1
    fi

    log_info "mkcert: installed"

    # Check if certificates were generated with mkcert (mkcert certs are larger)
    local api_cert="$PROJECT_DIR/certs/api/cert.pem"
    local needs_regen=false

    if [ -f "$api_cert" ]; then
        # mkcert certs typically include "mkcert" in the issuer
        if ! openssl x509 -in "$api_cert" -noout -issuer 2>/dev/null | grep -qi "mkcert"; then
            log_warn "Existing certificates are self-signed (not browser-trusted)"
            needs_regen=true
        else
            log_info "Certificates: mkcert (browser-trusted)"
        fi
    else
        needs_regen=true
    fi

    if [ "$needs_regen" = true ]; then
        log_info "Regenerating certificates with mkcert..."
        rm -rf "$PROJECT_DIR/certs"

        # Get host IP for certificate
        local host_ip
        host_ip=$(grep HOST_EXTERNAL_IP "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
        [ -z "$host_ip" ] && host_ip="127.0.0.1"

        # Create cert directories
        mkdir -p "$PROJECT_DIR/certs/api"
        for domain in registrar.voipbin.test conference.voipbin.test sip.voipbin.test sip-service.voipbin.test trunk.voipbin.test; do
            mkdir -p "$PROJECT_DIR/certs/$domain"
            mkcert -cert-file "$PROJECT_DIR/certs/$domain/fullchain.pem" \
                   -key-file "$PROJECT_DIR/certs/$domain/privkey.pem" \
                   "$domain" "*.$domain" localhost 127.0.0.1 ::1 2>/dev/null
        done

        # Generate API certificate
        mkcert -cert-file "$PROJECT_DIR/certs/api/cert.pem" \
               -key-file "$PROJECT_DIR/certs/api/privkey.pem" \
               voipbin.test "*.voipbin.test" localhost 127.0.0.1 ::1 "$host_ip" 2>/dev/null

        # Update .env with new base64-encoded certs (if .env exists)
        if [ -f "$PROJECT_DIR/.env" ]; then
            local api_cert_b64=$(cat "$PROJECT_DIR/certs/api/cert.pem" | base64 -w0)
            local api_key_b64=$(cat "$PROJECT_DIR/certs/api/privkey.pem" | base64 -w0)

            sed -i "s|^API_SSL_CERT_BASE64=.*|API_SSL_CERT_BASE64=$api_cert_b64|" "$PROJECT_DIR/.env"
            sed -i "s|^API_SSL_PRIVKEY_BASE64=.*|API_SSL_PRIVKEY_BASE64=$api_key_b64|" "$PROJECT_DIR/.env"
            sed -i "s|^HOOK_SSL_CERT_BASE64=.*|HOOK_SSL_CERT_BASE64=$api_cert_b64|" "$PROJECT_DIR/.env"
            sed -i "s|^HOOK_SSL_PRIVKEY_BASE64=.*|HOOK_SSL_PRIVKEY_BASE64=$api_key_b64|" "$PROJECT_DIR/.env"
        else
            log_warn ".env not found - skipping certificate update in .env"
        fi

        log_info "Certificates regenerated with mkcert (browser-trusted)"

        # Restart services that use the certificates if they're running
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "voipbin-api-mgr"; then
            log_info "Restarting API services to use new certificates..."
            docker compose restart api-manager hook-manager 2>/dev/null || true
        fi
    fi

    return 0
}

# Check if this is first time run
check_first_run() {
    local is_first_run=true
    local reasons=()

    # Check .env file
    if [ -f "$PROJECT_DIR/.env" ]; then
        is_first_run=false
    else
        reasons+=(".env file not found")
    fi

    # Check certificates directory
    if [ -d "$PROJECT_DIR/certs" ] && [ "$(ls -A $PROJECT_DIR/certs 2>/dev/null)" ]; then
        is_first_run=false
    else
        reasons+=("certificates not generated")
    fi

    # Check if database volume has data
    local compose_project derive_rc
    compose_project="$(derive_compose_project_name)"
    derive_rc=$?
    if [[ "$derive_rc" -ne 0 ]]; then
        if [[ "$derive_rc" -eq 2 ]]; then
            log_error "invalid COMPOSE_PROJECT_NAME \"${COMPOSE_PROJECT_NAME:-}\": project names must consist only of lowercase alphanumeric characters, hyphens, and underscores as well as start with a letter or number"
        else
            log_error "could not derive a compose project name from $PROJECT_DIR (set COMPOSE_PROJECT_NAME)"
        fi
        exit 1
    fi
    if docker volume ls --format '{{.Name}}' | grep -q "${compose_project}_db_data"; then
        is_first_run=false
    else
        reasons+=("database volume not created")
    fi

    if [ "$is_first_run" = true ]; then
        return 0  # Is first run
    fi
    return 1  # Not first run
}

# Validate .env file
validate_env() {
    log_step "Validating environment configuration..."

    if [ ! -f "$PROJECT_DIR/.env" ]; then
        log_error ".env file not found!"
        return 1
    fi

    local warnings=()
    local errors=()

    # Source the .env file
    set -a
    source "$PROJECT_DIR/.env"
    set +a

    # Check HOST_EXTERNAL_IP
    if [ -z "$HOST_EXTERNAL_IP" ] || [ "$HOST_EXTERNAL_IP" = "127.0.0.1" ]; then
        warnings+=("HOST_EXTERNAL_IP is set to localhost - external SIP clients won't work")
    else
        log_info "HOST_EXTERNAL_IP: $HOST_EXTERNAL_IP"
    fi

    # Check API SSL certificates
    if [ -z "$API_SSL_CERT_BASE64" ] || [ -z "$API_SSL_PRIVKEY_BASE64" ]; then
        errors+=("API SSL certificates not configured")
    else
        log_info "API SSL certificates: configured"
    fi

    # Check GCP credentials (optional but important)
    if [ -z "$GOOGLE_APPLICATION_CREDENTIALS" ] || [ "$GOOGLE_APPLICATION_CREDENTIALS" = "/path/to/your/google_service_account.json" ]; then
        warnings+=("GOOGLE_APPLICATION_CREDENTIALS not configured - TTS/storage features won't work")
    else
        if [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
            log_info "GCP credentials: $GOOGLE_APPLICATION_CREDENTIALS"
        else
            warnings+=("GCP credentials file not found: $GOOGLE_APPLICATION_CREDENTIALS")
        fi
    fi

    # Check optional API keys
    if [ -n "$OPENAI_API_KEY" ] && [ "$OPENAI_API_KEY" != "" ]; then
        log_info "OpenAI API key: configured"
    else
        warnings+=("OPENAI_API_KEY not set - AI features won't work")
    fi

    # Check domain configuration
    log_info "BASE_DOMAIN: ${BASE_DOMAIN:-voipbin.test}"
    log_info "DOMAIN_NAME_EXTENSION: ${DOMAIN_NAME_EXTENSION:-registrar.voipbin.test}"

    # Print warnings
    if [ ${#warnings[@]} -gt 0 ]; then
        echo ""
        log_warn "Configuration warnings:"
        for warn in "${warnings[@]}"; do
            echo "  - $warn"
        done
    fi

    # Print errors and exit if any
    if [ ${#errors[@]} -gt 0 ]; then
        echo ""
        log_error "Configuration errors:"
        for err in "${errors[@]}"; do
            echo "  - $err"
        done
        return 1
    fi

    log_info "Environment configuration is valid!"
    return 0
}

# Check if VoIP network interfaces exist
check_voip_interfaces() {
    if ip link show kamailio-int &>/dev/null && ip link show rtpengine-int &>/dev/null; then
        return 0
    fi
    return 1
}

# Host prerequisite check (design §2.5) — replaces check_root. Reuses
# check_voip_interfaces, which probes BOTH kamailio-int and rtpengine-int,
# making the Step-9 sudo below provably unreachable on the unprivileged path
# (a narrower probe would reintroduce a sudo prompt into the AI flow).
# Internal mode additionally requires resolv.conf → 127.0.0.1 (or a CoreDNS
# that answers). Returns 0 when satisfied; else sets HOST_PREREQS_MISSING
# and returns 1 (the caller decides root-inline-setup vs fail-fast).
check_host_prereqs() {
    HOST_PREREQS_MISSING=""

    if ! check_voip_interfaces; then
        HOST_PREREQS_MISSING="VoIP network interfaces (kamailio-int/rtpengine-int) not configured"
        return 1
    fi

    if [ "$(get_domain_mode "$PROJECT_DIR/.env")" = "internal" ]; then
        if grep -qE "^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.1" "$RESOLV_CONF" 2>/dev/null; then
            return 0
        fi
        # resolv.conf not pointing at CoreDNS — accept a CoreDNS that answers
        if command -v dig &> /dev/null && \
           [ -n "$(dig +short +time=1 +tries=1 @127.0.0.1 voipbin.test 2>/dev/null)" ]; then
            return 0
        fi
        HOST_PREREQS_MISSING="internal-mode DNS not configured (resolv.conf does not point at 127.0.0.1 and CoreDNS is not answering)"
        return 1
    fi

    return 0
}

# Check if database is initialized. Mirrors doctor.sh's check_database(): a
# partial migration (bin_manager gets some tables, then alembic aborts before
# the asterisk_config stream ever runs) must NOT be mistaken for "done", or
# every subsequent start.sh run silently skips migrate.sh forever and the
# asterisk schema (e.g. ps_aors, needed for extension/AOR creation) never
# gets created.
check_database_initialized() {
    local tables alembic_bin alembic_ast
    tables=$(docker exec voipbin-db sh -c "$START_MYSQL_IN_DB -N -e \"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'bin_manager';\"" root 2>/dev/null || echo "0")
    alembic_bin=$(docker exec voipbin-db sh -c "$START_MYSQL_IN_DB -N -e 'SELECT version_num FROM bin_manager.alembic_version LIMIT 1;'" root 2>/dev/null)
    alembic_ast=$(docker exec voipbin-db sh -c "$START_MYSQL_IN_DB -N -e 'SELECT version_num FROM asterisk.alembic_version LIMIT 1;'" root 2>/dev/null)

    if [[ "$tables" =~ ^[0-9]+$ ]] && [ "$tables" -gt "0" ] && [ -n "$alembic_bin" ] && [ -n "$alembic_ast" ]; then
        return 0
    fi
    return 1
}

# Wait for database to be ready (with actual connection test, not just ping)
wait_for_database() {
    log_info "Waiting for database to be ready..."
    local max_wait=60
    local waited=0

    while [ $waited -lt $max_wait ]; do
        # Use actual SELECT query to verify root authentication works
        if docker exec voipbin-db sh -c "$START_MYSQL_IN_DB -e 'SELECT 1'" root &>/dev/null; then
            log_info "Database is ready!"
            return 0
        fi
        echo -n "."
        sleep 2
        waited=$((waited + 2))
    done

    echo ""
    log_error "Database did not become ready in time"
    return 1
}

# Check if test data setup was already completed (using marker file)
# This allows users to delete the test customer without it being recreated
check_test_data_initialized() {
    [ -f "$PROJECT_DIR/.test_data_initialized" ]
}

# Test/dev seed data (admin@localhost account, extensions 1000/2000/3000 with
# fixed passwords) is only created when explicitly opted into via
# VOIPBIN_SANDBOX_DEV_SEED=true in .env. This is now the primary, documented
# self-install path, including production use — auto-seeding known credentials
# by default is not acceptable there.
dev_seed_enabled() {
    [ "${VOIPBIN_SANDBOX_DEV_SEED:-false}" = "true" ]
}

# Wait for API to be ready
wait_for_api() {
    log_info "Waiting for API to be ready..."
    local max_wait=60
    local waited=0

    while [ $waited -lt $max_wait ]; do
        if curl -sk -o /dev/null -w "%{http_code}" "https://localhost:8443/health" 2>/dev/null | grep -q "200\|404"; then
            log_info "API is ready!"
            return 0
        fi
        echo -n "."
        sleep 2
        waited=$((waited + 2))
    done

    echo ""
    log_warn "API may not be fully ready yet"
    return 1
}

# Fetch customer ID for existing customer
fetch_customer_id() {
    local api_host="localhost"
    local api_port="8443"
    local customer_email="admin@localhost"
    local customer_password="admin@localhost"

    # Login to get token
    local login_response
    login_response=$(curl -sk -X POST "https://${api_host}:${api_port}/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\": \"$customer_email\", \"password\": \"$customer_password\"}" 2>/dev/null)

    local token
    token=$(echo "$login_response" | jq -r '.token' 2>/dev/null)

    if [ "$token" != "null" ] && [ -n "$token" ]; then
        local customer_info
        customer_info=$(curl -sk -X GET "https://${api_host}:${api_port}/v1.0/customer" \
            -H "Authorization: Bearer $token" 2>/dev/null)
        CUSTOMER_ID=$(echo "$customer_info" | jq -r '.id' 2>/dev/null)
    fi
}

# Wait for agent-manager to process customer creation event and create admin agent
wait_for_admin_agent() {
    local customer_id="$1"
    local max_wait=30
    local waited=0

    # This function's stdout is captured by the caller (AGENT_ID=$(...)), so
    # progress logging must go to stderr or it corrupts the returned agent ID.
    log_info "  Waiting for admin agent to be created..." >&2
    while [ $waited -lt $max_wait ]; do
        local agent_list
        agent_list=$(docker exec voipbin-agent-mgr /app/bin/agent-control agent list \
            --customer-id "$customer_id" 2>/dev/null || true)

        local agent_id
        agent_id=$(echo "$agent_list" | jq -r '.[0].id' 2>/dev/null)

        if [ -n "$agent_id" ] && [ "$agent_id" != "null" ]; then
            echo "$agent_id"
            return 0
        fi

        sleep 2
        waited=$((waited + 2))
    done

    return 1
}

# Enable the scheduler's in-stack DB backup (VOIP-1281). It ships disabled
# upstream (bin-dbscheme-manager seed migration a5e6f559299c: "production
# uses managed Cloud SQL backups; self-hosted installs enable it"), since a
# self-hosted sandbox has no managed-backup equivalent. Idempotent: a no-op
# if already enabled (re-runs of start.sh, restarts).
ensure_scheduled_backup_enabled() {
    local list_json enabled
    list_json=$(docker exec voipbin-schedule-mgr /app/bin/schedule-control schedule list 2>/dev/null)
    if [ -z "$list_json" ]; then
        log_warn "  Could not reach schedule-manager to enable database-backup. Run manually:"
        log_warn "    docker exec voipbin-schedule-mgr /app/bin/schedule-control schedule enable database-backup"
        return
    fi

    enabled=$(echo "$list_json" | jq -r '.[] | select(.name == "database-backup") | .enabled' 2>/dev/null)
    if [ "$enabled" = "true" ]; then
        return
    fi

    if docker exec voipbin-schedule-mgr /app/bin/schedule-control schedule enable database-backup > /dev/null 2>&1; then
        log_info "  Enabled scheduled DB backup (database-backup)"
    else
        log_warn "  Could not enable database-backup. Run manually:"
        log_warn "    docker exec voipbin-schedule-mgr /app/bin/schedule-control schedule enable database-backup"
    fi
}

# Setup test customer and extensions
setup_test_customer() {
    local api_host="localhost"
    local api_port="8443"
    local customer_email="admin@localhost"
    local customer_password="admin@localhost"
    local customer_name="Sandbox Admin"

    # Step 1: Create customer via CLI
    # agent-manager will auto-create an admin agent with a random unusable password
    log_info "  Creating customer: $customer_email"
    docker exec voipbin-customer-mgr /app/bin/customer-control customer create \
        --name "$customer_name" \
        --email "$customer_email" 2>&1 | grep -E "(Success|ID:)" || true

    # Step 2: Get customer ID from the customer-manager CLI
    log_info "  Fetching customer ID..."
    local customer_list
    customer_list=$(docker exec voipbin-customer-mgr /app/bin/customer-control customer list 2>/dev/null || true)
    CUSTOMER_ID=$(echo "$customer_list" | jq -r '.[] | select(.email == "'"$customer_email"'") | .id' 2>/dev/null | head -1)

    if [ -z "$CUSTOMER_ID" ] || [ "$CUSTOMER_ID" == "null" ]; then
        log_warn "  Could not find customer ID. You can run setup_test_customer.sh manually."
        return 1
    fi
    log_info "  Customer ID: $CUSTOMER_ID"

    # Step 3: Wait for agent-manager to create the admin agent (via RabbitMQ event)
    local admin_agent_id
    admin_agent_id=$(wait_for_admin_agent "$CUSTOMER_ID")

    if [ -z "$admin_agent_id" ]; then
        log_warn "  Admin agent was not created in time. You can run setup_test_customer.sh manually."
        return 1
    fi
    log_info "  Admin agent ID: $admin_agent_id"

    # Step 4: Set admin password using agent-control CLI
    # The agent was created with a random password, so we must set it explicitly
    log_info "  Setting admin password..."
    docker exec voipbin-agent-mgr /app/bin/agent-control agent update-password \
        --id "$admin_agent_id" \
        --password "$customer_password" 2>&1 | grep -v severity || true

    # Step 5: Login to get JWT token
    sleep 1
    local login_response
    login_response=$(curl -sk -X POST "https://${api_host}:${api_port}/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\": \"$customer_email\", \"password\": \"$customer_password\"}")

    local token
    token=$(echo "$login_response" | jq -r '.token' 2>/dev/null)

    if [ "$token" == "null" ] || [ -z "$token" ]; then
        log_warn "  Could not login to create extensions. You can run setup_test_customer.sh manually."
        return 1
    fi

    # Step 5b: Set billing plan type BEFORE creating extensions.
    # A new customer's billing account is created ASYNCHRONOUSLY by
    # billing-manager (it subscribes to the customer_created event, creates
    # the account, then calls back UpdateBillingAccountID on the customer).
    # So billing_account_id can still be the zero UUID right after creation.
    # We POLL until it is populated, then set the plan. Pinned images do not
    # set a default plan, and resource-limited resources (extensions, agents)
    # fail their billing resource-limit check until a plan is set.
    local billing_account_id=""
    local customer_info_plan=""
    local plan_waited=0
    local plan_max_wait=30
    log_info "  Waiting for billing account to be provisioned..."
    while [ $plan_waited -lt $plan_max_wait ]; do
        customer_info_plan=$(curl -sk -X GET "https://${api_host}:${api_port}/v1.0/customer" \
            -H "Authorization: Bearer $token" 2>/dev/null) || true
        billing_account_id=$(echo "$customer_info_plan" | jq -r '.billing_account_id' 2>/dev/null) || true
        if [ -n "$billing_account_id" ] && [ "$billing_account_id" != "null" ] && \
           [ "$billing_account_id" != "00000000-0000-0000-0000-000000000000" ]; then
            break
        fi
        echo -n "."
        sleep 2
        plan_waited=$((plan_waited + 2))
    done
    echo ""
    if [ -n "$billing_account_id" ] && [ "$billing_account_id" != "null" ] && \
       [ "$billing_account_id" != "00000000-0000-0000-0000-000000000000" ]; then
        log_info "  Setting billing plan type (unlimited) for sandbox..."
        docker exec voipbin-billing-mgr /app/bin/billing-control account update-plan-type \
            --id "$billing_account_id" \
            --plan-type unlimited 2>&1 | grep -v severity || true
    else
        log_warn "  Billing account not ready after ${plan_max_wait}s; plan type not set. Extensions may fail."
    fi

    # Step 6: Create extensions
    local extensions_created=0
    for ext in 1000 2000 3000; do
        log_info "  Creating extension: $ext"
        local ext_response ext_http_code
        ext_response=$(curl -sk -w '\n%{http_code}' -X POST "https://${api_host}:${api_port}/v1.0/extensions" \
            -H "Content-Type: application/json" \
                -H "Authorization: Bearer $token" \
            -d "{\"extension\": \"$ext\", \"password\": \"pass$ext\", \"name\": \"Extension $ext\"}" 2>&1) || true
        ext_http_code=$(echo "$ext_response" | tail -1)
        if [ "$ext_http_code" == "200" ] || [ "$ext_http_code" == "201" ]; then
            extensions_created=$((extensions_created + 1))
        else
            log_warn "  Could not create extension $ext (HTTP ${ext_http_code:-?}): $(echo "$ext_response" | head -n -1)"
        fi
    done

    # Step 7: Get billing account ID
    local customer_info
    customer_info=$(curl -sk -X GET "https://${api_host}:${api_port}/v1.0/customer" \
        -H "Authorization: Bearer $token")

    local billing_account_id
    billing_account_id=$(echo "$customer_info" | jq -r '.billing_account_id' 2>/dev/null)

    # Step 8: Create accesskey for API access
    log_info "  Creating API access key..."
    local accesskey_output
    accesskey_output=$(docker exec voipbin-customer-mgr /app/bin/customer-control accesskey create \
        --customer-id "$CUSTOMER_ID" \
        --name "Sandbox API Key" \
        --detail "Default API key for sandbox testing" \
        --expire 87600h 2>&1 | grep -v severity || true)
    # Extract the token from the output (format: "token: <token>")
    ACCESSKEY_TOKEN=$(echo "$accesskey_output" | grep -oP '(?<=token:\s)[^\s]+' | head -1)
    if [ -z "$ACCESSKEY_TOKEN" ]; then
        # Try alternative format (JSON output)
        ACCESSKEY_TOKEN=$(echo "$accesskey_output" | jq -r '.token' 2>/dev/null || true)
    fi

    # Step 9: Add initial balance to billing account
    if [ -n "$billing_account_id" ] && [ "$billing_account_id" != "null" ]; then
        log_info "  Adding initial balance to billing account..."
        docker exec voipbin-billing-mgr /app/bin/billing-control account add-balance \
            --id "$billing_account_id" \
            --amount 100000 2>&1 | grep -v severity || true
    fi

    # Create marker file only when all three extensions actually came up —
    # an unconditional touch here defeats the obvious recovery path (just
    # re-run start.sh) when extension creation failed, since a stale marker
    # makes check_test_data_initialized skip setup_test_customer forever.
    if [ "$extensions_created" -eq 3 ]; then
        touch "$PROJECT_DIR/.test_data_initialized"
        log_info "  Test customer created successfully!"
    else
        log_warn "  Test customer created, but only $extensions_created/3 extensions succeeded."
        log_warn "  Marker file not written; re-run './scripts/start.sh' after fixing the underlying issue (see 'voipbin> doctor')."
    fi
}

main() {
    # Result-line guarantee: registered here, not at top level (§2.2 pattern).
    trap start_exit_trap EXIT

    # Global variables (set by setup_test_customer or fetch_customer_id)
    CUSTOMER_ID=""
    ACCESSKEY_TOKEN=""

    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - Startup"
    echo "=============================================="

    # Stale-.env / COMPOSE_PROFILES conflict guard (§2.5/§6). Must run before
    # validate_env's 'set -a; source .env' overwrites the shell's
    # COMPOSE_PROFILES value and hides the conflict. Only when .env exists —
    # a missing .env keeps the "run init first" message below.
    if [ -f "$PROJECT_DIR/.env" ]; then
        if ! check_compose_profiles_conflict "$PROJECT_DIR/.env"; then
            emit_result error "reason=\"$COMPOSE_PROFILES_CONFLICT_REASON\""
            exit 1
        fi
    fi

    # Host prerequisites (replaces check_root, design §2.5). Root + missing →
    # host setup inline via the setup-host.sh subprocess (single
    # implementation, no sourcing). Unprivileged + missing → fail fast with
    # next=. A missing .env skips the check in both cases so the
    # "run init first" message below stays authoritative.
    if [ -f "$PROJECT_DIR/.env" ] && ! check_host_prereqs; then
        if [ "$EUID" -eq 0 ]; then
            log_warn "Host prerequisites missing: $HOST_PREREQS_MISSING"
            log_info "Running host setup (setup-host.sh)..."
            # The compose default network is pre-created by setup-host.sh's
            # step_ensure_docker_network, so the interfaces step normally
            # succeeds here even on a fresh host. The || tolerance remains as
            # a defensive fallback: if host setup still fails for another
            # reason, Steps 7-9 below retry after services start.
            "$SCRIPT_DIR/setup-host.sh" || \
                log_warn "Host setup incomplete; Steps 7-9 below will retry after services start"
        else
            log_error "Host prerequisites missing: $HOST_PREREQS_MISSING"
            log_error "Run host setup first: sudo ./scripts/setup-host.sh"
            emit_result error "reason=\"host setup missing\" next=\"sudo ./scripts/setup-host.sh\""
            exit 1
        fi
    fi

    cd "$PROJECT_DIR"

    # Step 1: Check dependencies
    check_dependencies

    # Step 2: Check if .env exists
    log_step "Checking installation status..."
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        log_error ".env file not found!"
        echo ""
        log_info "Please run initialization first:"
        echo ""
        echo "  voipbin> init"
        echo ""
        exit 1
    else
        log_info "Configuration found"
    fi

    # Step 3: Setup mkcert for browser-trusted certificates
    setup_mkcert

    # Step 4: Validate environment
    if ! validate_env; then
        echo ""
        log_error "Environment validation failed!"
        log_error "Please fix the errors above and try again."
        log_error "You can regenerate .env with: voipbin> init"
        exit 1
    fi

    # Step 4.5: Check if host IP has changed and regenerate configs if needed
    log_step "Checking host IP configuration..."
    if check_ip_changed; then
        local current_ip=$(detect_current_host_ip)
        local configured_ip=$(get_configured_host_ip)
        log_warn "Host IP changed: $configured_ip -> $current_ip"
        log_info "Regenerating IP configuration..."
        regenerate_ip_config
    else
        log_info "Host IP unchanged: $(get_configured_host_ip)"
    fi

    # Step 5: Start infrastructure services first
    log_step "Starting infrastructure services (db, redis, rabbitmq)..."
    # Unset all environment variables that might override .env file values
    # This ensures docker compose reads from .env only
    unset API_SSL_CERT_BASE64 API_SSL_PRIVKEY_BASE64 HOOK_SSL_CERT_BASE64 HOOK_SSL_PRIVKEY_BASE64
    unset HOST_EXTERNAL_IP KAMAILIO_INTERNAL_ADDR
    docker compose up -d db redis rabbitmq

    # Wait for db to be healthy
    wait_for_database || exit 1

    # Step 6: Initialize database if needed
    log_step "Checking database initialization..."
    if check_database_initialized; then
        log_info "Database is already initialized"
    else
        log_warn "Database not initialized. Running containerized migration (migrate.sh)..."
        # migrate.sh runs alembic inside a python:3.11-slim container on the
        # compose network — no host pip/alembic required (design §3.3).
        if ! "$SCRIPT_DIR/migrate.sh"; then
            log_error "Database migration failed!"
            log_error "Check the output above, then re-run: voipbin> start"
            exit 1
        fi
    fi

    # Mode gate for Steps 7-8 (§2.4): external mode never rewrites the
    # Corefile and never invokes setup-dns.sh.
    local domain_mode
    domain_mode=$(get_domain_mode "$PROJECT_DIR/.env")

    # Step 7: Generate CoreDNS config and start all services
    if [ "$domain_mode" = "internal" ]; then
        log_step "Generating CoreDNS configuration..."
        local host_ip=$(grep '^HOST_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
        local kamailio_ip=$(grep '^KAMAILIO_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
        [ -z "$host_ip" ] && host_ip="127.0.0.1"
        [ -z "$kamailio_ip" ] && kamailio_ip="$host_ip"  # Fallback to host_ip if not set
        generate_coredns_config "$host_ip" "$PROJECT_DIR/config/coredns" "$kamailio_ip"
        log_info "  Web services → $host_ip (Docker port mapping)"
        log_info "  SIP services → $kamailio_ip"
    else
        log_info "DNS is operator-managed (external mode)"
    fi

    log_step "Starting all services..."
    docker compose up -d

    # Step 8: Check DNS configuration
    if [ "$domain_mode" = "internal" ]; then
        log_step "Checking DNS configuration..."
        if grep -qE "^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.1" "$RESOLV_CONF" 2>/dev/null; then
            log_info "DNS is configured (resolv.conf → CoreDNS)"
        else
            log_warn "DNS not configured. Setting up..."
            "$SCRIPT_DIR/setup-dns.sh" -y 2>/dev/null || log_warn "DNS setup failed. Run 'dns setup' manually."
        fi
    fi

    # Step 9: Setup VoIP network interfaces
    # Only reachable on the root path: the check_host_prereqs gate at the top
    # of main() fails fast unprivileged unless both interfaces already exist,
    # so this sudo can never prompt in the unprivileged (AI) flow (§2.5).
    log_step "Checking VoIP network interfaces..."
    if check_voip_interfaces; then
        log_info "VoIP network interfaces already configured"
    else
        log_warn "VoIP network interfaces not found"
        log_info "Setting up VoIP network interfaces (requires sudo)..."
        sudo "$SCRIPT_DIR/setup-voip-network.sh"

        # Restart kamailio and rtpengine
        log_info "Restarting kamailio and rtpengine..."
        docker compose restart kamailio rtpengine
    fi

    # Step 10: Wait for services to stabilize
    log_step "Waiting for services to stabilize..."
    sleep 5

    # Step 10b: Enable the scheduler's in-stack DB backup (VOIP-1281) — ships
    # disabled upstream, self-hosted installs turn it on.
    log_step "Checking scheduled DB backup..."
    ensure_scheduled_backup_enabled

    # Step 11: Wait for API to be ready
    log_step "Waiting for API..."
    wait_for_api

    # Step 12: Setup test data if needed
    log_step "Checking test data..."
    if check_test_data_initialized; then
        log_info "Test data already initialized (delete .test_data_initialized to recreate)"
        # Get customer ID for display (if customer still exists)
        fetch_customer_id
    elif dev_seed_enabled; then
        log_info "Creating test customer and extensions..."
        setup_test_customer
    else
        log_info "Skipping dev seed data (VOIPBIN_SANDBOX_DEV_SEED not set to true) — no test customer/extensions created."
    fi

    # Step 13: Show status
    log_step "Service Status"
    echo ""
    docker compose ps --format "table {{.Name}}\t{{.Status}}" | head -25

    # Count services
    local total=$(docker compose ps -q 2>/dev/null | wc -l)
    local running=$(docker compose ps --status running -q 2>/dev/null | wc -l)
    echo ""
    log_info "Services: $running/$total running"

    # Show any unhealthy/restarting services
    local issues=$(docker compose ps --format "{{.Name}}\t{{.Status}}" 2>/dev/null | grep -iE "unhealthy|restarting|exit" || true)
    if [ -n "$issues" ]; then
        echo ""
        log_warn "Some services may need attention:"
        echo "$issues" | while read line; do
            echo "  - $line"
        done
        echo ""
        log_info "These may be due to missing API keys (OpenAI, GCP, etc.)"
        log_info "Core VoIP services should still work."
    fi

    echo ""
    echo "=============================================="
    echo "  Startup Complete!"
    echo "=============================================="
    echo ""
    # Summary URLs/domains derive from .env (sourced by validate_env) — a
    # hardcoded voipbin.test summary would misinform an external-mode operator.
    local base_domain="${BASE_DOMAIN:-voipbin.test}"
    local ext_domain="${DOMAIN_NAME_EXTENSION:-registrar.voipbin.test}"
    echo "-----------------------------------------------"
    echo "  Web Consoles"
    echo "-----------------------------------------------"
    echo "  Admin UI:      http://admin.${base_domain}:3003"
    echo "  Meet:          http://meet.${base_domain}:3004"
    echo "  Talk:          http://talk.${base_domain}:3005"
    echo "  API Manager:   https://api.${base_domain}:8443"
    echo "  RabbitMQ:      http://localhost:15672 (${RABBITMQ_DEFAULT_USER:-guest} / ${RABBITMQ_DEFAULT_PASS:-guest})"
    echo ""
    echo "  NOTE: If you see ERR_CERT_AUTHORITY_INVALID, visit"
    echo "        https://api.${base_domain}:8443 first and accept the certificate."
    echo ""
    if dev_seed_enabled; then
        echo "-----------------------------------------------"
        echo "  Default Admin Account (created on first run)"
        echo "-----------------------------------------------"
        echo "  Username:      admin@localhost"
        echo "  Password:      admin@localhost"
        echo ""
        echo "  To verify: voipbin> customer list"
        echo ""
    fi
    if [ -n "$ACCESSKEY_TOKEN" ] && [ "$ACCESSKEY_TOKEN" != "null" ]; then
        echo "-----------------------------------------------"
        echo "  Default API Key (created on first run)"
        echo "-----------------------------------------------"
        echo "  Token:         $ACCESSKEY_TOKEN"
        echo ""
        echo "  Usage: curl https://api.${base_domain}:8443/v1.0/calls?accesskey=$ACCESSKEY_TOKEN"
        echo ""
        echo "  To verify: voipbin> customer accesskey list"
        echo ""
    fi
    if dev_seed_enabled; then
        echo "-----------------------------------------------"
        echo "  Default SIP Extensions (created on first run)"
        echo "-----------------------------------------------"
        echo "  1000 / pass1000"
        echo "  2000 / pass2000"
        echo "  3000 / pass3000"
        if [ -n "$CUSTOMER_ID" ] && [ "$CUSTOMER_ID" != "null" ]; then
            echo ""
            echo "  SIP Domain:    ${CUSTOMER_ID}.${ext_domain}"
            echo "  SIP Server:    $(grep HOST_EXTERNAL_IP "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1):5060"
        fi
        echo ""
        echo "  To verify: voipbin> registrar extension list --customer_id <id>"
        echo ""
    fi
    echo "-----------------------------------------------"
    echo "  Useful Commands"
    echo "-----------------------------------------------"
    echo "  View logs:     voipbin> logs <service>"
    echo "  Stop:          voipbin> stop"
    echo "  Full reset:    voipbin> clean --all"
    echo ""

    # Machine-parseable result line (§2.2)
    emit_result ok "services=$running/$total"
}

main "$@"
