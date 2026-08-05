#!/bin/bash
# VoIPBin Sandbox - Post-Install Smoke Check (design §2.8, VOIP-1275)
# Unprivileged, read-only, mode-aware self-verification an agent (or human)
# runs after start.sh, replacing "eyeball the start.sh summary".
#
# Usage: ./scripts/check-install.sh
#
# Output contract: one "CHECK <name>: pass|fail|skip <detail>" line per check,
# then a final machine-parseable line:
#   VOIPBIN_CHECK: status=pass|fail passed=N failed=M mode=<internal|external>
# Exit 0 only when every check passes (skips do not fail the run).
#
# Deliberately NO `set -e`: individual probes are expected to fail on a broken
# install, and the script's job is to keep going and report all of them.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on — deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
CERTS_DIR="${CERTS_DIR:-$PROJECT_DIR/certs}"
# resolv.conf paths (overridable so tests can stub host probes)
RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"
RESOLV_BACKUP="${RESOLV_BACKUP:-/etc/resolv.conf.voipbin-backup}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

# =============================================================================
# Check bookkeeping
# =============================================================================

CHECKS_PASSED=0
CHECKS_FAILED=0

# check_result <name> <pass|fail|skip> [detail...]
check_result() {
    local name="$1"
    local result="$2"
    shift 2

    if [[ $# -gt 0 ]]; then
        echo "CHECK $name: $result $*"
    else
        echo "CHECK $name: $result"
    fi

    case "$result" in
        pass) CHECKS_PASSED=$((CHECKS_PASSED + 1)) ;;
        fail) CHECKS_FAILED=$((CHECKS_FAILED + 1)) ;;
    esac
}

# =============================================================================
# .env-derived values (read once in main, used by the checks)
# =============================================================================

load_check_env() {
    CHECK_MODE=$(get_domain_mode "$ENV_FILE")
    CHECK_TLS_MODE=$(get_env_var "$ENV_FILE" TLS_MODE)
    CHECK_BASE_DOMAIN=$(get_env_var "$ENV_FILE" BASE_DOMAIN)
    [[ -z "$CHECK_BASE_DOMAIN" ]] && CHECK_BASE_DOMAIN="voipbin.test"
    CHECK_HOST_IP=$(get_env_var "$ENV_FILE" HOST_EXTERNAL_IP)
    CHECK_KAMAILIO_IP=$(get_env_var "$ENV_FILE" KAMAILIO_EXTERNAL_IP)
    CHECK_DOMAIN_EXT=$(get_env_var "$ENV_FILE" DOMAIN_NAME_EXTENSION)
    CHECK_PROFILES=$(get_env_var "$ENV_FILE" COMPOSE_PROFILES)
}

# =============================================================================
# Checks (design §2.8 table). Each emits exactly one CHECK line.
# =============================================================================

# COMPOSE_PROFILES shell-vs-.env conflict + stale pre-dual-mode .env (§2.5/§6).
# Shares check_compose_profiles_conflict with start.sh (defined in common.sh).
check_compose_profiles() {
    if check_compose_profiles_conflict "$ENV_FILE" > /dev/null 2>&1; then
        check_result compose-profiles pass "COMPOSE_PROFILES consistent with .env"
    else
        check_result compose-profiles fail "$COMPOSE_PROFILES_CONFLICT_REASON"
    fi
}

# Service count: `config --services` vs `ps --status running`, derived (not
# hardcoded) so it self-maintains as services are added. Both commands run
# with the identical environment and the .env-sourced COMPOSE_PROFILES —
# the count only means something if both apply the same profile filter.
check_services() {
    local expected running unhealthy
    expected=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$CHECK_PROFILES" docker compose config --services 2>/dev/null | grep -c .)
    running=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$CHECK_PROFILES" docker compose ps --status running -q 2>/dev/null | grep -c .)
    unhealthy=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$CHECK_PROFILES" docker compose ps --format '{{.Name}}\t{{.Status}}' 2>/dev/null | grep -iE 'unhealthy|restarting|exited' || true)

    if [[ "$expected" -eq 0 ]]; then
        check_result services fail "docker compose config --services returned nothing (docker unavailable?)"
        return
    fi
    if [[ -n "$unhealthy" ]]; then
        check_result services fail "unhealthy/restarting/exited: $(echo "$unhealthy" | cut -f1 | tr '\n' ' ' | sed 's/ $//')"
        return
    fi
    if [[ "$running" -ne "$expected" ]]; then
        check_result services fail "running=$running expected=$expected (docker compose ps for details)"
        return
    fi
    check_result services pass "running=$running/$expected, none unhealthy"
}

# Cert trust chain.
# internal: the issuer of certs/api/cert.pem must be the mkcert CA actually
#   installed on this host (catches the CAROOT-under-sudo mismatch, §2.5).
# external: local expiry must be >14 days; the real chain validation is the
#   tls check below (curl without -k).
check_cert_trust() {
    local api_cert="$CERTS_DIR/api/cert.pem"

    if [[ "$CHECK_MODE" == "external" ]]; then
        if [[ ! -f "$api_cert" ]]; then
            check_result cert-trust fail "no $api_cert — install your certificate: ./scripts/install-certs.sh <fullchain.pem> <privkey.pem>"
            return
        fi
        if openssl x509 -in "$api_cert" -noout -checkend $((14 * 24 * 3600)) &> /dev/null; then
            local expires
            expires=$(openssl x509 -in "$api_cert" -noout -enddate 2>/dev/null | cut -d'=' -f2)
            check_result cert-trust pass "certificate valid >14 days (until $expires)"
        else
            check_result cert-trust fail "certificate expires within 14 days (or is expired) — renew and re-run ./scripts/install-certs.sh"
        fi
        return
    fi

    # internal: browser trust is only expected with mkcert-managed certs
    if [[ "$CHECK_TLS_MODE" == "selfsigned" ]]; then
        check_result cert-trust skip "TLS_MODE=selfsigned (browser trust not expected)"
        return
    fi
    if [[ ! -f "$api_cert" ]]; then
        check_result cert-trust fail "no $api_cert — re-run ./scripts/init.sh"
        return
    fi
    if ! command -v mkcert &> /dev/null; then
        check_result cert-trust fail "mkcert not installed — run: sudo ./scripts/setup-host.sh"
        return
    fi

    local caroot ca_subject cert_issuer
    caroot=$(mkcert -CAROOT 2>/dev/null)
    if [[ -z "$caroot" || ! -f "$caroot/rootCA.pem" ]]; then
        check_result cert-trust fail "no mkcert root CA at ${caroot:-<unknown>}/rootCA.pem — run: sudo ./scripts/setup-host.sh"
        return
    fi

    ca_subject=$(openssl x509 -in "$caroot/rootCA.pem" -noout -subject 2>/dev/null | sed 's/^subject= *//')
    cert_issuer=$(openssl x509 -in "$api_cert" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')
    if [[ -n "$cert_issuer" && "$cert_issuer" == "$ca_subject" ]]; then
        check_result cert-trust pass "cert issuer matches the mkcert CA at $caroot"
    else
        check_result cert-trust fail "cert issuer does not match the mkcert CA at $caroot (CAROOT mismatch?) — run: sudo ./scripts/setup-host.sh"
    fi
}

# DNS resolution: api.<d> -> HOST_EXTERNAL_IP, sip.<d> -> KAMAILIO_EXTERNAL_IP.
# internal: against CoreDNS directly (dig @127.0.0.1).
# external: against the system resolver; a mismatch fails and lists the
#   expected records (the operator owns them, §2.9 runbook).
check_dns() {
    if ! command -v dig &> /dev/null; then
        check_result dns skip "dig not installed"
        return
    fi

    local resolver=()
    if [[ "$CHECK_MODE" == "internal" ]]; then
        resolver=("@127.0.0.1")
    fi

    local api_result sip_result
    api_result=$(dig +short +time=2 +tries=1 "${resolver[@]}" "api.$CHECK_BASE_DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
    sip_result=$(dig +short +time=2 +tries=1 "${resolver[@]}" "sip.$CHECK_BASE_DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)

    if [[ "$api_result" == "$CHECK_HOST_IP" && "$sip_result" == "$CHECK_KAMAILIO_IP" ]]; then
        check_result dns pass "api.$CHECK_BASE_DOMAIN -> $api_result, sip.$CHECK_BASE_DOMAIN -> $sip_result"
        return
    fi

    if [[ "$CHECK_MODE" == "external" ]]; then
        check_result dns fail "expected records: api.$CHECK_BASE_DOMAIN A $CHECK_HOST_IP, sip.$CHECK_BASE_DOMAIN A $CHECK_KAMAILIO_IP (got api=${api_result:-none}, sip=${sip_result:-none}) — see the DNS runbook in README.md"
    else
        check_result dns fail "api.$CHECK_BASE_DOMAIN=${api_result:-none} (want $CHECK_HOST_IP), sip.$CHECK_BASE_DOMAIN=${sip_result:-none} (want $CHECK_KAMAILIO_IP) — run: sudo ./scripts/setup-host.sh"
    fi
}

# TLS chain to the API.
# internal: validate against the mkcert CA, falling back to -k (selfsigned).
# external: NO -k — the chain must validate with the system trust store.
check_tls() {
    local url="https://api.$CHECK_BASE_DOMAIN:8443/ping"

    if [[ "$CHECK_MODE" == "external" ]]; then
        if curl -fsS -o /dev/null --max-time 10 "$url" 2>/dev/null; then
            check_result tls pass "chain validates without -k for $url"
        else
            check_result tls fail "TLS chain did not validate for $url (without -k) — check DNS and ./scripts/install-certs.sh"
        fi
        return
    fi

    # internal: mkcert CA first, then -k fallback (selfsigned installs)
    local caroot=""
    if command -v mkcert &> /dev/null; then
        caroot=$(mkcert -CAROOT 2>/dev/null)
    fi
    if [[ -n "$caroot" && -f "$caroot/rootCA.pem" ]] && \
       curl -fsS -o /dev/null --max-time 10 --cacert "$caroot/rootCA.pem" "$url" 2>/dev/null; then
        check_result tls pass "chain validates against the mkcert CA for $url"
        return
    fi
    if curl -fsSk -o /dev/null --max-time 10 "$url" 2>/dev/null; then
        check_result tls pass "reachable with -k fallback (certificate not browser-trusted) for $url"
        return
    fi
    check_result tls fail "$url unreachable over TLS"
}

# API liveness: /ping responds (independent of chain validity).
check_api_ping() {
    local url="https://api.$CHECK_BASE_DOMAIN:8443/ping"
    local http_code
    http_code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)

    if [[ "$http_code" == "200" ]]; then
        check_result api-ping pass "$url -> 200"
    else
        check_result api-ping fail "$url -> ${http_code:-no response}"
    fi
}

# Realm config sanity: the RUNNING registrar-manager's DOMAIN_NAME_EXTENSION
# must equal the .env value — a stale container keeps minting extensions with
# the old realm domain.
check_realm() {
    local running_ext
    running_ext=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' voipbin-registrar-mgr 2>/dev/null \
        | grep '^DOMAIN_NAME_EXTENSION=' | head -1 | cut -d'=' -f2-)

    if [[ -z "$running_ext" ]]; then
        check_result realm fail "registrar-manager not running or not inspectable (docker inspect voipbin-registrar-mgr)"
        return
    fi
    if [[ "$running_ext" == "$CHECK_DOMAIN_EXT" ]]; then
        check_result realm pass "registrar-manager DOMAIN_NAME_EXTENSION=$running_ext matches .env"
    else
        check_result realm fail "running=$running_ext .env=$CHECK_DOMAIN_EXT — recreate: docker compose rm -fsv registrar-manager && docker compose up -d registrar-manager"
    fi
}

# Scheduler present and firing (VOIP-1281): schedule-manager is running, and
# its seeded platform schedules (number-renew, execution-retention,
# database-backup) are enabled — proof the DB seed migration applied and the
# dispatch loop has something to fire. Does not assert a recent tm_last_run:
# right after a fresh install none of the daily/nightly jobs have had a slot
# yet, so that would false-fail a healthy install.
check_scheduler() {
    local list_json
    list_json=$(docker exec voipbin-schedule-mgr /app/bin/schedule-control schedule list 2>/dev/null)

    if [[ -z "$list_json" ]]; then
        check_result scheduler fail "schedule-manager not running or not inspectable (docker inspect voipbin-schedule-mgr)"
        return
    fi
    if ! command -v jq &> /dev/null; then
        check_result scheduler skip "jq not installed"
        return
    fi

    local missing enabled_count
    missing=""
    for name in number-renew execution-retention database-backup; do
        local enabled
        enabled=$(echo "$list_json" | jq -r --arg n "$name" '.[] | select(.name == $n) | .enabled')
        if [[ "$enabled" != "true" ]]; then
            missing="$missing $name"
        fi
    done
    enabled_count=$(echo "$list_json" | jq -r '[.[] | select(.enabled == true)] | length')

    if [[ -n "$missing" ]]; then
        check_result scheduler fail "schedule(s) missing or disabled:$missing — check the schedule_schedules seed migration and docker logs voipbin-schedule-mgr"
        return
    fi
    check_result scheduler pass "schedule-manager present, $enabled_count schedule(s) enabled (number-renew, execution-retention, database-backup)"
}

# resolv.conf state matches the mode.
# internal: must point at CoreDNS (127.0.0.1).
# external: must be untouched — no 127.0.0.1 hijack and no .voipbin-backup.
check_resolv_conf() {
    local hijacked="false" backup_exists="false" upstreams_exists="false" fallback_count=0
    grep -qE "^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.1" "$RESOLV_CONF" 2>/dev/null && hijacked="true"
    [[ -f "$RESOLV_BACKUP" ]] && backup_exists="true"
    if [[ -f "$RESOLV_UPSTREAMS" ]]; then
        upstreams_exists="true"
        # $RESOLV_UPSTREAMS stores bare IPs, one per line (no "nameserver "
        # prefix — that's added when write_resolv_conf_with_fallback
        # composes $RESOLV_CONF from this file).
        # `grep -c` exits 1 on a zero match, and `|| echo 0` inside a
        # command substitution would then capture BOTH grep's own "0"
        # stdout and the echo's "0" — a two-line result that corrupts the
        # single-line CHECK output contract. Split the fallback out.
        fallback_count=$(grep -c '.' "$RESOLV_UPSTREAMS" 2>/dev/null)
        fallback_count="${fallback_count:-0}"
    fi

    if [[ "$CHECK_MODE" == "internal" ]]; then
        if [[ "$hijacked" == "true" ]]; then
            check_result resolv-conf pass "$RESOLV_CONF points at CoreDNS (127.0.0.1), fallback: $fallback_count upstream(s)"
        else
            check_result resolv-conf fail "$RESOLV_CONF does not point at 127.0.0.1 — run: sudo ./scripts/setup-host.sh"
        fi
        return
    fi

    # external: leftover internal-mode DNS hijack state is a failure
    if [[ "$hijacked" == "true" || "$backup_exists" == "true" || "$upstreams_exists" == "true" ]]; then
        check_result resolv-conf fail "leftover internal-mode DNS state (hijacked=$hijacked backup=$backup_exists upstreams=$upstreams_exists) — run: sudo ./scripts/setup-dns.sh --uninstall"
    else
        check_result resolv-conf pass "resolv.conf untouched (no hijack, no backup, no upstreams state)"
    fi
}

# =============================================================================
# Main
# =============================================================================

main() {
    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - Install Check"
    echo "=============================================="
    echo ""

    if [[ ! -f "$ENV_FILE" ]]; then
        log_error "no .env at $ENV_FILE — run ./scripts/init.sh first"
        echo "VOIPBIN_CHECK: status=fail passed=0 failed=1 mode=unknown reason=\"no .env — run ./scripts/init.sh first\""
        exit 1
    fi

    load_check_env
    log_info "Install mode: $CHECK_MODE (base domain: $CHECK_BASE_DOMAIN)"
    echo ""

    check_compose_profiles
    check_services
    check_cert_trust
    check_dns
    check_tls
    check_api_ping
    check_realm
    check_scheduler
    check_resolv_conf

    echo ""
    local status="pass"
    [[ "$CHECKS_FAILED" -gt 0 ]] && status="fail"
    echo "VOIPBIN_CHECK: status=$status passed=$CHECKS_PASSED failed=$CHECKS_FAILED mode=$CHECK_MODE"

    [[ "$status" == "pass" ]] && exit 0
    exit 1
}

main "$@"
