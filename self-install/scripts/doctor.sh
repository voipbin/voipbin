#!/bin/bash
# VoIPBin Sandbox - Install Doctor (VOIP-1280)
# Unprivileged, read-only, any-stage diagnose-and-prescribe helper. Runs before
# init.sh has ever run, mid-install, or against a running stack; diagnoses
# everything it can and prints the exact recovery command for every failure.
# The doctor never mutates anything - it only diagnoses and prescribes.
#
# Usage: ./scripts/doctor.sh
# Output contract (docs/plans/2026-08-02-sandbox-doctor-design.md §3):
#   DOCTOR <name>: pass|fail|warn|skip <detail>   (one line per check)
#   FIX <name>: <exact command or action>   (immediately after its DOCTOR fail/warn line)
# `grep '^FIX '` yields every prescription exactly once, in check order - the
# summary re-lists them as indented, non-FIX-prefixed lines.
# Final machine-parseable line (the last line of output):
#   VOIPBIN_DOCTOR: status=pass|fail passed=N failed=M warned=K mode=<internal|external|unknown>
#   early abort (cannot even start): VOIPBIN_DOCTOR: status=error reason="..."
# Exit codes: 0 only when failed=0 (warns and skips never fail the run); 1 when
# any check failed; 2 environment error. Counter semantics: pass/fail/warn
# increment their counters, skip increments nothing.
# Deliberately NO `set -e`: individual probes are expected to fail on a broken
# install, and the script's job is to keep going and report all of them.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on - deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
CERTS_DIR="${CERTS_DIR:-$PROJECT_DIR/certs}"
# resolv.conf path (overridable so tests can stub host probes)
RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"
# Disk thresholds in GiB (overridable so tests can exercise both edges)
DOCTOR_DISK_MIN_GB="${DOCTOR_DISK_MIN_GB:-3}"
DOCTOR_DISK_WARN_GB="${DOCTOR_DISK_WARN_GB:-15}"

# Guarded single-line source: the only exit-2 path, taken before any helper
# exists. The bats loader's sed substitution comments out this whole line,
# guard suffix included.
source "$SCRIPT_DIR/common.sh" || { echo 'VOIPBIN_DOCTOR: status=error reason="common.sh missing"'; exit 2; }

# =============================================================================
# Check bookkeeping
# =============================================================================
CHECKS_PASSED=0
CHECKS_FAILED=0
CHECKS_WARNED=0
PRESCRIPTIONS=()
DOCTOR_DOCKER_OK=false
DOCTOR_STAGE=""
DOCTOR_ENVFILE_FAILED=false
# All mysql invocations read the password from the CONTAINER's env
# (MYSQL_ROOT_PASSWORD), never the host argv (the migrate.sh MYSQL_IN_DB idiom).
DOCTOR_MYSQL_IN_DB='exec mysql -u"$0" -p"${MYSQL_ROOT_PASSWORD:-root_password}"'
DOCTOR_MYSQLADMIN_IN_DB='exec mysqladmin -u"$0" -p"${MYSQL_ROOT_PASSWORD:-root_password}" ping'

# doctor_result <name> <pass|fail|warn|skip> [detail...]
doctor_result() {
    local name="$1" result="$2"
    shift 2
    if [[ $# -gt 0 ]]; then echo "DOCTOR $name: $result $*"; else echo "DOCTOR $name: $result"; fi
    case "$result" in
        pass) CHECKS_PASSED=$((CHECKS_PASSED + 1)) ;;
        fail) CHECKS_FAILED=$((CHECKS_FAILED + 1)) ;;
        warn) CHECKS_WARNED=$((CHECKS_WARNED + 1)) ;;
    esac
    # skip increments nothing
}

# doctor_fix <name> <command...>: FIX line + collect for the summary re-list.
doctor_fix() { local name="$1"; shift; echo "FIX $name: $*"; PRESCRIPTIONS+=("[$name] $*"); }

# doctor_fail/doctor_warn <name> <detail> <fix>: result line + its FIX in one call.
doctor_fail() { doctor_result "$1" fail "$2"; doctor_fix "$1" "$3"; }
doctor_warn() { doctor_result "$1" warn "$2"; doctor_fix "$1" "$3"; }

doctor_join() {  # doctor_join <sep> <items...>
    local sep="$1" out="" x; shift
    for x in "$@"; do out+="${out:+$sep}$x"; done
    printf '%s' "$out"
}

# =============================================================================
# .env-derived values, stage detection, gates
# =============================================================================
load_doctor_env() {
    DOCTOR_MODE=$(get_domain_mode "$ENV_FILE")
    DOCTOR_TLS_MODE=$(get_env_var "$ENV_FILE" TLS_MODE)
    DOCTOR_BASE_DOMAIN=$(get_env_var "$ENV_FILE" BASE_DOMAIN)
    [[ -z "$DOCTOR_BASE_DOMAIN" ]] && DOCTOR_BASE_DOMAIN="voipbin.test"
    DOCTOR_HOST_IP=$(get_env_var "$ENV_FILE" HOST_EXTERNAL_IP)
    DOCTOR_KAMAILIO_IP=$(get_env_var "$ENV_FILE" KAMAILIO_EXTERNAL_IP)
    DOCTOR_DOMAIN_EXT=$(get_env_var "$ENV_FILE" DOMAIN_NAME_EXTENSION)
    DOCTOR_PROFILES=$(get_env_var "$ENV_FILE" COMPOSE_PROFILES)
    DOCTOR_OS=$(detect_os)
}

# detect_stage -> preinstall|prestart|running (design §4). Needs
# DOCTOR_DOCKER_OK, so it runs right after the docker check.
detect_stage() {
    if [[ ! -f "$ENV_FILE" ]]; then echo "preinstall"; return; fi
    local running=0
    if [[ "$DOCTOR_DOCKER_OK" == "true" ]]; then
        running=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps --status running -q 2>/dev/null | grep -c .)
    fi
    if [[ "$running" -gt 0 ]]; then echo "running"; else echo "prestart"; fi
}

# install_gate <name>: category-2 stage gate - skip + return 1 in pre-install.
install_gate() {
    if [[ "$DOCTOR_STAGE" == "preinstall" ]]; then doctor_result "$1" skip "no .env"; return 1; fi
    return 0
}

# runtime_gate <name>: category-3 gate. Docker gate first (design §4: skip,
# never cascade-fail), then stage gates.
runtime_gate() {
    local name="$1"
    if [[ "$DOCTOR_DOCKER_OK" != "true" ]]; then doctor_result "$name" skip "docker unavailable"; return 1; fi
    if [[ "$DOCTOR_STAGE" == "preinstall" ]]; then doctor_result "$name" skip "no .env"; return 1; fi
    if [[ "$DOCTOR_STAGE" != "running" ]]; then
        doctor_result "$name" skip "stack not running — run ./scripts/start.sh"
        return 1
    fi
    return 0
}

# =============================================================================
# Category 1 - prerequisites (always run, no .env needed)
# =============================================================================
check_docker() {
    DOCTOR_DOCKER_OK=false
    command -v docker &> /dev/null || { doctor_fail docker "docker not installed" "install docker (https://docs.docker.com/engine/install/)"; return; }
    docker info &> /dev/null || { doctor_fail docker "docker daemon unreachable" "sudo systemctl start docker"; return; }
    DOCTOR_DOCKER_OK=true
    doctor_result docker pass "docker installed and daemon reachable"
}

# Group membership matters only when the daemon is not reachable unprivileged.
# EUID=0: evaluate for SUDO_USER; real root login (SUDO_USER unset) skips (§7).
check_docker_group() {
    if [[ "$EUID" -eq 0 && -z "${SUDO_USER:-}" ]]; then doctor_result docker-group skip "running as root"; return; fi
    if [[ "$EUID" -eq 0 ]]; then
        if id -nG "$SUDO_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
            doctor_result docker-group pass "$SUDO_USER is in the docker group"
        else
            doctor_warn docker-group "$SUDO_USER is not in the docker group" "sudo usermod -aG docker $SUDO_USER and re-login"
        fi
        return
    fi
    if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        doctor_result docker-group pass "current user is in the docker group"
    elif [[ "$DOCTOR_DOCKER_OK" == "true" ]]; then
        doctor_result docker-group pass "docker reachable unprivileged (group membership not required)"
    else
        doctor_warn docker-group "not in the docker group and the daemon is not reachable unprivileged" 'sudo usermod -aG docker $USER and re-login'
    fi
}

# >= 2.24.4: the docker-compose.test.yml !reset/!override merge tags need it.
check_compose_version() {
    if [[ "$DOCTOR_DOCKER_OK" != "true" ]]; then doctor_result compose-version skip "docker unavailable"; return; fi
    local ver min="2.24.4"
    ver=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose version --short 2>/dev/null); ver="${ver#v}"
    if [[ -z "$ver" ]]; then
        doctor_fail compose-version "docker compose v2 not available" "upgrade docker compose to >= $min"
    elif [[ "$(printf '%s\n%s\n' "$min" "$ver" | sort -V | head -1)" != "$min" ]]; then
        doctor_fail compose-version "docker compose $ver < $min (merge-tag support required)" "upgrade docker compose to >= $min"
    else
        doctor_result compose-version pass "docker compose $ver (>= $min)"
    fi
}

# openssl/curl missing -> fail; dig missing -> warn (dns checks will skip);
# mkcert -> fail only when mode=internal and TLS_MODE=mkcert.
check_tools() {
    local missing_fail=() t mkcert_required="false" dig_missing="false"
    for t in openssl curl; do command -v "$t" &> /dev/null || missing_fail+=("$t"); done
    if [[ "$DOCTOR_MODE" == "internal" && "$DOCTOR_TLS_MODE" == "mkcert" ]]; then
        mkcert_required="true"
        command -v mkcert &> /dev/null || missing_fail+=("mkcert")
    fi
    command -v dig &> /dev/null || dig_missing="true"
    if [[ ${#missing_fail[@]} -gt 0 ]]; then
        local nonmkcert=() m fix=""
        for m in "${missing_fail[@]}"; do [[ "$m" == "mkcert" ]] || nonmkcert+=("$m"); done
        [[ ${#nonmkcert[@]} -gt 0 ]] && fix="sudo apt install ${nonmkcert[*]} (or your distro equivalent)"
        if [[ " ${missing_fail[*]} " == *" mkcert "* ]]; then
            fix+="${fix:+; }mkcert: sudo ./scripts/setup-host.sh"
        fi
        local detail="missing: ${missing_fail[*]}"  # a missing dig is not swallowed by the fail path
        [[ "$dig_missing" == "true" ]] && detail+="; dig also missing (dns checks will skip)"
        doctor_fail tools "$detail" "$fix"
        return
    fi
    if [[ "$dig_missing" == "true" ]]; then
        doctor_warn tools "dig not installed (dns resolution checks will skip)" "sudo apt install dnsutils (or your distro equivalent)"
        return
    fi
    local present="openssl curl dig"
    [[ "$mkcert_required" == "true" ]] && present+=" mkcert"
    doctor_result tools pass "$present present"
}

# Bind-address-aware port scan (design §5 port policy). 53: only 127.0.0.1 +
# HOST_EXTERNAL_IP matter (pre-install: detect_current_host_ip), with the
# systemd-resolved stubs 127.0.0.53/127.0.0.54 whitelisted; external mode
# deploys no coredns so 53 is not ours to claim. 5060/5061/5066: wildcard or
# the addresses kamailio needs. 8443/3003-3005: the published 0.0.0.0 bind
# only. Ports held by our own running services pass; anything else on a needed
# address fails (best-effort process name via ss -tulnp).
check_ports() {
    if [[ "$DOCTOR_OS" == "macos" ]]; then doctor_result ports skip "unsupported on macos"; return; fi
    if ! command -v ss &> /dev/null; then doctor_result ports skip "ss not available"; return; fi
    local host_ip="$DOCTOR_HOST_IP" kamailio_ip="$DOCTOR_KAMAILIO_IP"
    if [[ "$DOCTOR_STAGE" == "preinstall" ]]; then host_ip=$(detect_current_host_ip); kamailio_ip=""; fi
    local running_services=""
    [[ "$DOCTOR_DOCKER_OK" == "true" && "$DOCTOR_STAGE" == "running" ]] && running_services=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps --status running --format '{{.Service}}' 2>/dev/null)
    local conflicts=() seen=$'\n' addrport procfield addr port proc owner relevant wildcard key
    while read -r addrport procfield; do
        [[ "$addrport" == *:* ]] || continue
        port="${addrport##*:}"
        addr="${addrport%:*}"
        addr="${addr%\%*}"
        proc=$(sed -n 's/.*users:((\"\([^"]*\)\".*/\1/p' <<< "$procfield")
        wildcard="false"
        case "$addr" in 0.0.0.0|\*|\[::\]|::) wildcard="true" ;; esac
        owner=""
        relevant="false"
        case "$port" in
            53)
                [[ "$DOCTOR_MODE" == "external" ]] && continue
                [[ "$addr" == "127.0.0.53" || "$addr" == "127.0.0.54" ]] && continue
                if [[ "$wildcard" == "true" || "$addr" == "127.0.0.1" ]] || [[ -n "$host_ip" && "$addr" == "$host_ip" ]]; then relevant="true"; owner="coredns"; fi
                ;;
            5060|5061|5066)
                if [[ "$wildcard" == "true" || "$addr" == "10.100.0.200" ]] || [[ -n "$kamailio_ip" && "$addr" == "$kamailio_ip" ]]; then relevant="true"; owner="kamailio"; fi
                ;;
            8443) [[ "$wildcard" == "true" ]] && { relevant="true"; owner="api-manager"; } ;;
            3003) [[ "$wildcard" == "true" ]] && { relevant="true"; owner="square-admin"; } ;;
            3004) [[ "$wildcard" == "true" ]] && { relevant="true"; owner="square-meet"; } ;;
            3005) [[ "$wildcard" == "true" ]] && { relevant="true"; owner="square-talk"; } ;;
            *) continue ;;
        esac
        [[ "$relevant" == "true" ]] || continue
        # our own stack holding the port while running is a pass
        if [[ -n "$owner" ]] && grep -qx "$owner" <<< "$running_services"; then continue; fi
        key="$addr:$port"
        [[ "$seen" == *$'\n'"$key"$'\n'* ]] && continue
        seen+="$key"$'\n'
        conflicts+=("$addr:$port(${proc:-unknown})")
    done < <(ss -tulnp 2>/dev/null | awk 'NR>1 && $5 ~ /:/ {print $5, $7}')
    if [[ ${#conflicts[@]} -gt 0 ]]; then
        local first="${conflicts[0]}"
        first="${first%%(*}"; first="${first##*:}"
        doctor_fail ports "conflicting listeners: ${conflicts[*]}" "sudo lsof -i :$first to identify, then stop the conflicting service"
    else
        doctor_result ports pass "no conflicting listeners on 53 5060 5061 5066 8443 3003-3005"
    fi
}

check_disk_space() {
    local root=""
    [[ "$DOCTOR_DOCKER_OK" == "true" ]] && root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)
    [[ -z "$root" ]] && root="/var/lib/docker"
    [[ -d "$root" ]] || root="$PROJECT_DIR"
    local free_kb free_gb; free_kb=$(df -P "$root" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ ! "$free_kb" =~ ^[0-9]+$ ]]; then doctor_result disk-space skip "df failed for $root"; return; fi
    free_gb=$((free_kb / 1024 / 1024))
    if [[ "$free_gb" -lt "$DOCTOR_DISK_MIN_GB" ]]; then
        doctor_fail disk-space "free=${free_gb}GiB < ${DOCTOR_DISK_MIN_GB}GiB at $root" "free disk space at $root or run: docker system prune"
    elif [[ "$free_gb" -lt "$DOCTOR_DISK_WARN_GB" ]]; then
        doctor_warn disk-space "free=${free_gb}GiB < ${DOCTOR_DISK_WARN_GB}GiB at $root" "free disk space at $root or run: docker system prune"
    else
        doctor_result disk-space pass "free=${free_gb}GiB at $root"
    fi
}

# =============================================================================
# Category 2 - install state (needs .env; otherwise skip)
# =============================================================================
# The exception to the category's skip rule: this check itself fails (not
# skips) in the pre-install stage - it is the check that defines that stage.
check_env_file() {
    if [[ -f "$ENV_FILE" ]]; then doctor_result env-file pass ".env present at $ENV_FILE"; return; fi
    doctor_fail env-file "no .env at $ENV_FILE" "./scripts/init.sh --yes"
    DOCTOR_ENVFILE_FAILED=true
}

check_domain_mode() {
    install_gate domain-mode || return
    local raw; raw=$(get_env_var "$ENV_FILE" DOMAIN_MODE)
    if [[ -n "$raw" && "$raw" != "internal" && "$raw" != "external" ]]; then
        doctor_fail domain-mode "DOMAIN_MODE='$raw' invalid (expected internal or external)" "./scripts/init.sh --yes"
    elif ! grep -q '^COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null; then
        doctor_fail domain-mode "COMPOSE_PROFILES key missing: stale .env, re-run ./scripts/init.sh --yes" "./scripts/init.sh --yes"
    else
        doctor_result domain-mode pass "DOMAIN_MODE=$DOCTOR_MODE, COMPOSE_PROFILES key present"
    fi
}

# Shares check_compose_profiles_conflict with start.sh/check-install.sh
# (common.sh); the reason is emitted verbatim.
check_compose_profiles() {
    install_gate compose-profiles || return
    if check_compose_profiles_conflict "$ENV_FILE" > /dev/null 2>&1; then
        doctor_result compose-profiles pass "COMPOSE_PROFILES consistent with .env"
    elif [[ "$COMPOSE_PROFILES_CONFLICT_REASON" == *"unset COMPOSE_PROFILES"* ]]; then
        doctor_fail compose-profiles "$COMPOSE_PROFILES_CONFLICT_REASON" "unset COMPOSE_PROFILES"
    else
        doctor_fail compose-profiles "$COMPOSE_PROFILES_CONFLICT_REASON" "./scripts/init.sh --yes"
    fi
}

# Internal-mode DNS: active resolv.conf nameserver line (commented lines do
# not count), coredns container up (docker-gated clause), CoreDNS answers
# matching .env. macOS parity caveat: probes resolv.conf, exactly like
# check-install's check_resolv_conf (deliberately not improved here).
check_dns_internal() {
    install_gate dns-internal || return
    local mode_why="external mode"; [[ "$DOCTOR_MODE" == "external" ]] || mode_why="domain mode invalid"
    if [[ "$DOCTOR_MODE" != "internal" ]]; then doctor_result dns-internal skip "$mode_why"; return; fi
    local resolv_bad="false" coredns_down="false" dig_bad="false" problems=() notes=()
    if ! grep -qE "^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.1" "$RESOLV_CONF" 2>/dev/null; then
        resolv_bad="true"
        problems+=("$RESOLV_CONF has no active nameserver 127.0.0.1 line")
    fi
    if [[ "$DOCTOR_DOCKER_OK" != "true" ]]; then
        notes+=("coredns clause skipped: docker unavailable")
    elif [[ -z "$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps --status running -q coredns 2>/dev/null)" ]]; then
        coredns_down="true"
        problems+=("coredns container not running")
    fi
    if command -v dig &> /dev/null; then
        local api_r sip_r
        api_r=$(dig +short +time=2 +tries=1 @127.0.0.1 "api.$DOCTOR_BASE_DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
        sip_r=$(dig +short +time=2 +tries=1 @127.0.0.1 "sip.$DOCTOR_BASE_DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
        if [[ "$api_r" != "$DOCTOR_HOST_IP" || "$sip_r" != "$DOCTOR_KAMAILIO_IP" ]]; then
            dig_bad="true"
            problems+=("api.$DOCTOR_BASE_DOMAIN=${api_r:-none} (want $DOCTOR_HOST_IP), sip.$DOCTOR_BASE_DOMAIN=${sip_r:-none} (want $DOCTOR_KAMAILIO_IP)")
        fi
    else
        notes+=("resolution clause skipped: dig not installed")
    fi
    if [[ ${#problems[@]} -eq 0 ]]; then
        local detail="resolv.conf -> 127.0.0.1, coredns running, api/sip resolve to .env IPs"
        [[ ${#notes[@]} -gt 0 ]] && detail+=" ($(doctor_join '; ' "${notes[@]}"))"
        doctor_result dns-internal pass "$detail"
        return
    fi
    # setup-host owns resolv.conf/dns; the container-only remedy applies when
    # only the container is down (a consequent dig failure included).
    if [[ "$resolv_bad" == "true" || ( "$coredns_down" != "true" && "$dig_bad" == "true" ) ]]; then
        doctor_fail dns-internal "$(doctor_join '; ' "${problems[@]}")" "sudo ./scripts/setup-host.sh"
    else
        doctor_fail dns-internal "$(doctor_join '; ' "${problems[@]}")" "docker compose up -d coredns"
    fi
}

# External-mode DNS: every runbook record via the system resolver, diffed
# against .env - one indented line per mismatch/missing record, one DOCTOR
# fail with the count. The synthetic doctor-probe label tests the *.registrar
# wildcard (the only way to test one).
check_dns_external() {
    install_gate dns-external || return
    local mode_why="internal mode"; [[ "$DOCTOR_MODE" == "internal" ]] || mode_why="domain mode invalid"
    if [[ "$DOCTOR_MODE" != "external" ]]; then doctor_result dns-external skip "$mode_why"; return; fi
    if ! command -v dig &> /dev/null; then doctor_result dns-external skip "dig not installed"; return; fi
    local d="$DOCTOR_BASE_DOMAIN" mismatches=0 total=0 r fqdn expected got diff_lines=()
    for r in api admin meet talk sip sip-service conference trunk pstn registrar doctor-probe.registrar; do
        case "$r" in
            api|admin|meet|talk) expected="$DOCTOR_HOST_IP" ;;
            *) expected="$DOCTOR_KAMAILIO_IP" ;;
        esac
        fqdn="$r.$d"; total=$((total + 1))
        # ALL A records comma-joined: a round-robin set matches when the expected IP is among the answers; mismatches show the full set
        got=$(dig +short +time=2 +tries=1 "$fqdn" 2>/dev/null | grep -E '^[0-9.]+$' | paste -sd, -)
        if [[ ",$got," != *",$expected,"* ]]; then
            mismatches=$((mismatches + 1))
            diff_lines+=("  $fqdn expected $expected got ${got:-NXDOMAIN}")
        fi
    done
    if [[ "$mismatches" -eq 0 ]]; then
        doctor_result dns-external pass "all $total records resolve to the .env IPs"
        return
    fi
    # diff lines print AFTER the DOCTOR/FIX pair (they attach to this check)
    doctor_fail dns-external "$mismatches of $total records missing or mismatched (see the DNS runbook in README.md)" "sudo ./voipbin dns (prints the record table with real values); see the DNS runbook in README.md"
    printf '%s\n' "${diff_lines[@]}"
}

# cert_san_covers <cert> <fqdn>: literal SAN match, or wildcard-aware -
# a SAN entry *.<parent> satisfies any single-label name under <parent>
# (mkcert certs carry voipbin.test + *.voipbin.test, never api.voipbin.test).
cert_san_covers() {
    local cert="$1" fqdn="$2" sans parent san
    sans=$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | grep -o 'DNS:[^, ]*' | sed 's/^DNS://'); parent="${fqdn#*.}"
    [[ -z "$sans" ]] && return 1
    while IFS= read -r san; do
        [[ "$san" == "$fqdn" || "$san" == "*.$parent" ]] && return 0
    done <<< "$sans"
    return 1
}

# Presence + expiry (-checkend 0) + near-expiry (-checkend 1209600 - the
# option takes SECONDS; 1209600 = 14 days) run for every TLS_MODE. SAN and
# issuer clauses are TLS_MODE-gated: selfsigned skips both (the init.sh
# fallback generates a CN-only cert with no subjectAltName at all); mkcert
# checks SAN + CAROOT issuer (EUID=0 rules from §7); external/byo checks
# SAN + issuer != subject (not self-signed).
check_certs() {
    install_gate certs || return
    local cert="$CERTS_DIR/api/cert.pem" key="$CERTS_DIR/api/privkey.pem"
    if [[ ! -f "$cert" || ! -f "$key" ]]; then
        local mfix="re-run ./scripts/init.sh"
        [[ "$DOCTOR_MODE" == "external" ]] && mfix="./scripts/install-certs.sh <fullchain.pem> <privkey.pem>"
        doctor_fail certs "missing $cert and/or $key" "$mfix"
        return
    fi
    local problems=() warns=() notes=() fix=""
    if ! openssl x509 -in "$cert" -noout -checkend 0 &> /dev/null; then
        problems+=("certificate expired")
        if [[ "$DOCTOR_MODE" == "external" ]]; then fix="renew and re-run ./scripts/install-certs.sh"; else fix="re-run ./scripts/init.sh"; fi
    elif ! openssl x509 -in "$cert" -noout -checkend 1209600 &> /dev/null; then
        warns+=("certificate expires within 14 days")
    fi
    if [[ -z "$DOCTOR_TLS_MODE" ]]; then
        # a legacy .env without TLS_MODE must not false-fail as BYO self-signed
        notes+=("TLS_MODE unset (stale .env, re-run ./scripts/init.sh --yes): SAN/issuer clauses skipped")
    elif [[ "$DOCTOR_TLS_MODE" == "selfsigned" ]]; then
        notes+=("TLS_MODE=selfsigned: SAN/issuer clauses skipped (CN-only cert by design)")
    else
        cert_san_covers "$cert" "api.$DOCTOR_BASE_DOMAIN" || problems+=("SAN does not cover api.$DOCTOR_BASE_DOMAIN (wildcard-aware match)")
        if [[ "$DOCTOR_MODE" == "internal" && "$DOCTOR_TLS_MODE" == "mkcert" ]]; then
            # CAROOT belongs to the invoking user, not root (§7)
            local caroot=""
            if [[ "$EUID" -eq 0 && -n "${SUDO_USER:-}" ]]; then
                caroot=$(sudo -u "$SUDO_USER" mkcert -CAROOT 2>/dev/null)
            elif [[ "$EUID" -eq 0 ]]; then
                caroot="__skip__"
                notes+=("running as root without SUDO_USER: issuer clause skipped")
            elif command -v mkcert &> /dev/null; then
                caroot=$(mkcert -CAROOT 2>/dev/null)
            fi
            if [[ "$caroot" != "__skip__" ]]; then
                if [[ -z "$caroot" || ! -f "$caroot/rootCA.pem" ]]; then
                    problems+=("no mkcert root CA at ${caroot:-<unknown>}/rootCA.pem")
                else
                    local ca_subject cert_issuer
                    ca_subject=$(openssl x509 -in "$caroot/rootCA.pem" -noout -subject 2>/dev/null | sed 's/^subject= *//')
                    cert_issuer=$(openssl x509 -in "$cert" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')
                    if [[ -z "$cert_issuer" || "$cert_issuer" != "$ca_subject" ]]; then
                        problems+=("cert issuer does not match the mkcert CA at $caroot (CAROOT mismatch?)")
                    fi
                fi
            fi
        else
            local subj iss
            subj=$(openssl x509 -in "$cert" -noout -subject 2>/dev/null | sed 's/^subject= *//')
            iss=$(openssl x509 -in "$cert" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')
            [[ -n "$subj" && "$subj" == "$iss" ]] && problems+=("certificate is self-signed (issuer equals subject)")
        fi
    fi
    if [[ ${#problems[@]} -gt 0 ]]; then
        if [[ -z "$fix" ]]; then
            if [[ "$DOCTOR_MODE" == "external" ]]; then fix="./scripts/install-certs.sh <fullchain.pem> <privkey.pem>"; else fix="sudo ./scripts/setup-host.sh"; fi
        fi
        doctor_fail certs "$(doctor_join '; ' "${problems[@]}")" "$fix"
        return
    fi
    if [[ ${#warns[@]} -gt 0 ]]; then
        if [[ "$DOCTOR_MODE" == "external" ]]; then fix="renew and re-run ./scripts/install-certs.sh"; else fix="re-run ./scripts/init.sh"; fi
        doctor_warn certs "$(doctor_join '; ' "${warns[@]}")" "$fix"
        return
    fi
    local detail="certificate valid >14 days"
    [[ ${#notes[@]} -gt 0 ]] && detail+=" ($(doctor_join '; ' "${notes[@]}"))"
    doctor_result certs pass "$detail"
}

# install-certs.sh rewrites the four vars; init.sh does in internal mode.
check_certs_env_sync() {
    install_gate certs-env-sync || return
    local cert="$CERTS_DIR/api/cert.pem" key="$CERTS_DIR/api/privkey.pem"
    if [[ ! -f "$cert" || ! -f "$key" ]]; then
        doctor_result certs-env-sync skip "no certs at $CERTS_DIR/api to compare (see certs)"
        return
    fi
    # Unreadable files (root-owned 0600 privkey on sudo-created installs) skip actionably.
    local cert_b64 key_b64 out_of_sync=() f
    for f in "$cert" "$key"; do [[ -r "$f" ]] || { doctor_result certs-env-sync skip "${f##*/} not readable unprivileged (run sudo ./voipbin doctor)"; return; }; done
    # openssl base64 -A: portable single-line encoding (BSD base64 lacks -w0);
    # empty encodings skip instead of false-passing against empty .env vars.
    cert_b64=$(openssl base64 -A -in "$cert" 2>/dev/null)
    key_b64=$(openssl base64 -A -in "$key" 2>/dev/null)
    [[ -z "$cert_b64" || -z "$key_b64" ]] && { doctor_result certs-env-sync skip "cannot base64-encode certs/api (openssl base64 failed)"; return; }
    [[ "$(get_env_var "$ENV_FILE" API_SSL_CERT_BASE64)" == "$cert_b64" ]] || out_of_sync+=(API_SSL_CERT_BASE64)
    [[ "$(get_env_var "$ENV_FILE" API_SSL_PRIVKEY_BASE64)" == "$key_b64" ]] || out_of_sync+=(API_SSL_PRIVKEY_BASE64)
    [[ "$(get_env_var "$ENV_FILE" HOOK_SSL_CERT_BASE64)" == "$cert_b64" ]] || out_of_sync+=(HOOK_SSL_CERT_BASE64)
    [[ "$(get_env_var "$ENV_FILE" HOOK_SSL_PRIVKEY_BASE64)" == "$key_b64" ]] || out_of_sync+=(HOOK_SSL_PRIVKEY_BASE64)
    if [[ ${#out_of_sync[@]} -eq 0 ]]; then
        doctor_result certs-env-sync pass "the four SSL base64 vars match certs/api"
    elif [[ "$DOCTOR_MODE" == "external" ]]; then
        doctor_fail certs-env-sync "out of sync with certs/api: ${out_of_sync[*]}" "./scripts/install-certs.sh <fullchain.pem> <privkey.pem> (rewrites the four SSL vars)"
    else
        doctor_fail certs-env-sync "out of sync with certs/api: ${out_of_sync[*]}" "./scripts/init.sh (rewrites the four SSL vars)"
    fi
}

# Same probe as start.sh's check_voip_interfaces (macvlan is Linux-only).
check_voip_interfaces() {
    if [[ "$DOCTOR_OS" == "macos" ]]; then doctor_result voip-interfaces skip "unsupported on macos"; return; fi
    install_gate voip-interfaces || return
    local missing=() iface
    for iface in kamailio-int rtpengine-int; do ip link show "$iface" &> /dev/null || missing+=("$iface"); done
    if [[ ${#missing[@]} -gt 0 ]]; then
        doctor_fail voip-interfaces "missing macvlan interfaces: ${missing[*]}" "sudo ./scripts/setup-voip-network.sh"
    else
        doctor_result voip-interfaces pass "kamailio-int and rtpengine-int present"
    fi
}

# setup-host.sh's step_ensure_docker_network owns network creation with
# compose-compatible labels.
check_compose_network() {
    if [[ "$DOCTOR_DOCKER_OK" != "true" ]]; then doctor_result compose-network skip "docker unavailable"; return; fi
    install_gate compose-network || return
    local project network_name label derive_rc
    project=$(derive_compose_project_name)
    derive_rc=$?
    if [[ "$derive_rc" -ne 0 ]]; then
        if [[ "$derive_rc" -eq 2 ]]; then
            doctor_fail compose-network "invalid COMPOSE_PROJECT_NAME \"${COMPOSE_PROJECT_NAME:-}\"" "unset COMPOSE_PROJECT_NAME or set it to lowercase alphanumeric characters, hyphens, and underscores, starting with a letter or number"
        else
            doctor_result compose-network skip "could not derive a compose project name from $PROJECT_DIR"
        fi
        return
    fi
    network_name="${project}_default"
    label=$(docker network inspect -f '{{index .Labels "com.docker.compose.project"}}' "$network_name" 2>/dev/null)
    if [[ -n "$label" ]]; then
        doctor_result compose-network pass "$network_name exists with the compose project label"
    else
        doctor_fail compose-network "$network_name missing or lacks the com.docker.compose.project label" "sudo ./scripts/setup-host.sh"
    fi
}

# =============================================================================
# Category 3 - runtime pathology (needs a running stack; otherwise skip)
# =============================================================================
# `ps -a` is mandatory: since compose v2.21 plain `ps` hides exited
# containers, which are this check's primary target (stage detection elsewhere
# keeps the running-only default deliberately).
check_containers() {
    runtime_gate containers || return
    local rows fix="docker compose logs --tail 50 <service> then docker compose restart <service>"
    rows=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps -a --format '{{.Name}}\t{{.State}}\t{{.Status}}' 2>/dev/null)
    if [[ -z "$rows" ]]; then doctor_fail containers "docker compose ps -a returned nothing" "$fix"; return; fi
    local bad=() detail_lines=() total=0 name state status why lastlog
    while IFS=$'\t' read -r name state status; do
        [[ -z "$name" ]] && continue
        total=$((total + 1))
        if [[ "$state" != "running" || "$status" == *"unhealthy"* ]]; then
            why="$state"
            [[ "$status" == *"unhealthy"* ]] && why="unhealthy"
            bad+=("$name($why)")
            # best-effort last error line, bounded to the (usually few) broken ones
            lastlog=$(docker logs --tail 20 "$name" 2>&1 | grep -iE 'error|fatal|panic' | tail -1 | head -c 200)
            [[ -n "$lastlog" ]] && detail_lines+=("  $name: $lastlog")
        fi
    done <<< "$rows"
    if [[ ${#bad[@]} -gt 0 ]]; then
        # detail lines print AFTER the DOCTOR/FIX pair (they attach to this check)
        doctor_fail containers "not running/unhealthy: ${bad[*]}" "$fix"
        [[ ${#detail_lines[@]} -gt 0 ]] && printf '%s\n' "${detail_lines[@]}"
    else
        doctor_result containers pass "all $total containers running, none unhealthy"
    fi
}

# Mirrors t_infra T4b. VOIP-1278 failure mode: the first boot needs network
# access to fetch the delayed_message .ez.
check_rabbitmq_plugin() {
    runtime_gate rabbitmq-plugin || return
    local plugins; plugins=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose exec -T rabbitmq rabbitmq-plugins list -e 2>/dev/null)
    if grep -q delayed_message <<< "$plugins"; then
        doctor_result rabbitmq-plugin pass "delayed_message exchange plugin enabled"
    else
        doctor_fail rabbitmq-plugin "delayed_message exchange plugin not enabled" "docker compose restart rabbitmq and check docker compose logs rabbitmq for the plugin download failure"
    fi
}

# Zero consumers on an asterisk.*.request queue while the matching proxy is
# Up is the VOIP-1279 silent-death signature. Restarting the proxies alone is
# safe; the namespace hazard is owner-alone restarts (see proxy-netns).
check_queue_consumers() {
    runtime_gate queue-consumers || return
    local out; out=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose exec -T rabbitmq rabbitmqctl list_queues name consumers --no-table-headers 2>/dev/null)
    if [[ -z "$out" ]]; then doctor_result queue-consumers skip "cannot list queues (rabbitmq not responding)"; return; fi
    local issues=() t q count proxy_up name
    for t in call conference registrar; do
        q="asterisk.$t.request"
        # fallback parse: match the queue name column, skipping any header rows
        count=$(awk -v q="$q" '$1 == q {print $2}' <<< "$out" | head -1)
        if [[ -z "$count" ]]; then
            issues+=("$q not found")
        elif [[ "$count" == "0" ]]; then
            proxy_up=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps --status running -q "asterisk-$t-proxy" 2>/dev/null)
            if [[ -n "$proxy_up" ]]; then
                issues+=("$q consumers=0 while asterisk-$t-proxy is Up (silent proxy death)")
            else
                issues+=("$q consumers=0")
            fi
        fi
    done
    # additionally report every discovered asterisk.*.request queue at zero
    while read -r name count; do
        [[ "$name" == asterisk.*.request && "$count" == "0" ]] || continue
        case "$name" in
            asterisk.call.request|asterisk.conference.request|asterisk.registrar.request) continue ;;
        esac
        issues+=("$name consumers=0")
    done <<< "$out"
    if [[ ${#issues[@]} -gt 0 ]]; then
        doctor_fail queue-consumers "$(doctor_join '; ' "${issues[@]}")" "docker compose restart asterisk-call-proxy asterisk-conference-proxy asterisk-registrar-proxy"
    else
        doctor_result queue-consumers pass "all asterisk.*.request queues have consumers"
    fi
}

# For each {call,conference,registrar} pair: (a) the proxy's
# HostConfig.NetworkMode container:<id> must reference the CURRENT owner id
# from `docker compose ps -q` (mismatch = owner recreated under the proxy);
# (b) the owner starting AFTER the proxy = owner-alone restart, proxy attached
# to the orphaned old netns - compared as epoch seconds via `date -d`, never
# lexically (RFC3339Nano formatting varies). Ids always resolve via `docker
# compose ps -q <service>` (the proxies have no container_name);
# NetworkSettings.SandboxKey is empty on service-mode netns, must NOT be used.
check_proxy_netns() {
    if [[ "$DOCTOR_OS" == "macos" ]]; then doctor_result proxy-netns skip "unsupported on macos"; return; fi
    runtime_gate proxy-netns || return
    local bad=() targets_bad=() t proxy_id owner_id netmode ref owner_started proxy_started oe pe
    for t in call conference registrar; do
        proxy_id=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps -q "asterisk-$t-proxy" 2>/dev/null | head -1)
        owner_id=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps -q "asterisk-$t" 2>/dev/null | head -1)
        [[ -z "$proxy_id" || -z "$owner_id" ]] && continue
        netmode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$proxy_id" 2>/dev/null)
        ref="${netmode#container:}"
        if [[ "$netmode" == container:* && "$ref" != "$owner_id" ]]; then
            bad+=("asterisk-$t-proxy attached to a stale netns: owner asterisk-$t was recreated under it")
            targets_bad+=("asterisk-$t")
            continue
        fi
        owner_started=$(docker inspect -f '{{.State.StartedAt}}' "$owner_id" 2>/dev/null)
        proxy_started=$(docker inspect -f '{{.State.StartedAt}}' "$proxy_id" 2>/dev/null)
        oe=$(date -d "$owner_started" +%s 2>/dev/null)
        pe=$(date -d "$proxy_started" +%s 2>/dev/null)
        if [[ -n "$oe" && -n "$pe" && "$oe" -gt "$pe" ]]; then
            bad+=("asterisk-$t restarted after asterisk-$t-proxy: proxy attached to the orphaned old netns")
            targets_bad+=("asterisk-$t")
        fi
    done
    if [[ ${#bad[@]} -gt 0 ]]; then
        local cmds=() b
        for b in "${targets_bad[@]}"; do cmds+=("sudo ./voipbin restart $b"); done
        doctor_fail proxy-netns "$(doctor_join '; ' "${bad[@]}")" "$(doctor_join '; ' "${cmds[@]}") (pair-ordered restart)"
    else
        doctor_result proxy-netns pass "proxy/owner netns pairs consistent (call conference registrar)"
    fi
}

check_database() {
    runtime_gate database || return
    local db_id; db_id=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps -q db 2>/dev/null | head -1)
    if [[ -z "$db_id" ]]; then doctor_fail database "db container not running" "docker compose up -d db"; return; fi
    if ! docker exec "$db_id" sh -c "$DOCTOR_MYSQLADMIN_IN_DB" root &> /dev/null; then
        doctor_fail database "db not responding to mysqladmin ping" "docker compose up -d db"
        return
    fi
    local tables alembic_bin alembic_ast
    tables=$(docker exec "$db_id" sh -c "$DOCTOR_MYSQL_IN_DB -N -e \"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'bin_manager';\"" root 2>/dev/null)
    alembic_bin=$(docker exec "$db_id" sh -c "$DOCTOR_MYSQL_IN_DB -N -e 'SELECT version_num FROM bin_manager.alembic_version LIMIT 1;'" root 2>/dev/null)
    alembic_ast=$(docker exec "$db_id" sh -c "$DOCTOR_MYSQL_IN_DB -N -e 'SELECT version_num FROM asterisk.alembic_version LIMIT 1;'" root 2>/dev/null)
    if [[ ! "$tables" =~ ^[0-9]+$ || "$tables" -eq 0 || -z "$alembic_bin" || -z "$alembic_ast" ]]; then
        doctor_fail database "schema not initialized (bin_manager tables=${tables:-unknown}, alembic bin_manager=${alembic_bin:-none} asterisk=${alembic_ast:-none})" "./scripts/migrate.sh (invoked by start.sh) or ./scripts/start.sh"
    else
        doctor_result database pass "db responding, bin_manager tables=$tables, alembic versions present in bin_manager and asterisk"
    fi
}

# The RUNNING registrar-manager's DOMAIN_NAME_EXTENSION must equal .env - a
# stale container keeps minting extensions with the old realm domain.
check_realm() {
    runtime_gate realm || return
    local running_ext
    running_ext=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' voipbin-registrar-mgr 2>/dev/null | grep '^DOMAIN_NAME_EXTENSION=' | head -1 | cut -d'=' -f2-)
    if [[ -z "$running_ext" ]]; then
        doctor_fail realm "registrar-manager not running or not inspectable (docker inspect voipbin-registrar-mgr)" "docker compose up -d registrar-manager"
    elif [[ "$running_ext" == "$DOCTOR_DOMAIN_EXT" ]]; then
        doctor_result realm pass "registrar-manager DOMAIN_NAME_EXTENSION=$running_ext matches .env"
    else
        doctor_fail realm "running=$running_ext .env=$DOCTOR_DOMAIN_EXT" "docker compose rm -fsv registrar-manager && docker compose up -d registrar-manager"
    fi
}

# Marker vs DB reality, deliberately at schema level (the start.sh
# check_database_initialized probe), NOT a data-row query: marker + zero
# schema tables = the volume-wipe-with-stale-marker case. Marker + schema +
# zero customer rows is explicitly out of scope.
check_test_data() {
    runtime_gate test-data || return
    local marker="$PROJECT_DIR/.test_data_initialized" db_id tables
    db_id=$(cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ps -q db 2>/dev/null | head -1)
    if [[ -z "$db_id" ]]; then doctor_result test-data skip "db container not running (see database)"; return; fi
    tables=$(docker exec "$db_id" sh -c "$DOCTOR_MYSQL_IN_DB -N -e \"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'bin_manager';\"" root 2>/dev/null)
    if [[ ! "$tables" =~ ^[0-9]+$ ]]; then doctor_result test-data skip "cannot query the db (see database)"; return; fi
    if [[ -f "$marker" && "$tables" -eq 0 ]]; then
        doctor_fail test-data "marker .test_data_initialized present but the bin_manager schema is empty (stale marker after a volume wipe)" "rm .test_data_initialized && ./scripts/start.sh (re-seeds the test data)"
    elif [[ ! -f "$marker" && "$tables" -gt 0 ]]; then
        doctor_result test-data warn "schema present but no .test_data_initialized marker (start.sh will re-seed test data on its next run)"
    elif [[ -f "$marker" ]]; then
        doctor_result test-data pass "marker present and the bin_manager schema is populated"
    else
        doctor_result test-data pass "not initialized yet (no marker, empty schema)"
    fi
}

# =============================================================================
# Main
# =============================================================================
main() {
    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - Install Doctor"
    echo "=============================================="
    echo ""
    load_doctor_env

    # Category 1 - prerequisites. The docker check gates every docker-
    # dependent probe; the stage is detected right after it.
    check_docker
    DOCTOR_STAGE=$(detect_stage)
    if [[ "$DOCTOR_STAGE" == "preinstall" ]]; then
        log_info "Stage: pre-install (no .env yet)"
    else
        log_info "Stage: $DOCTOR_STAGE, mode: $DOCTOR_MODE (base domain: $DOCTOR_BASE_DOMAIN)"
    fi
    check_docker_group
    check_compose_version
    check_tools
    check_ports
    check_disk_space

    # Category 2 - install state
    check_env_file
    check_domain_mode
    check_compose_profiles
    check_dns_internal
    check_dns_external
    check_certs
    check_certs_env_sync
    check_voip_interfaces
    check_compose_network

    # Category 3 - runtime pathology
    check_containers
    check_rabbitmq_plugin
    check_queue_consumers
    check_proxy_netns
    check_database
    check_realm
    check_test_data

    # Summary: counts + the collected prescriptions in check order, re-listed
    # indented and non-FIX-prefixed so `grep '^FIX '` yields each exactly once.
    echo ""
    echo "=============================================="
    echo "  Doctor Summary"
    echo "=============================================="
    echo "  passed=$CHECKS_PASSED failed=$CHECKS_FAILED warned=$CHECKS_WARNED"
    if [[ ${#PRESCRIPTIONS[@]} -gt 0 ]]; then
        echo ""
        echo "  Prescriptions (in check order):"
        local p; for p in "${PRESCRIPTIONS[@]}"; do echo "  $p"; done
    fi
    if [[ "$DOCTOR_ENVFILE_FAILED" == "true" ]]; then
        echo ""
        echo "  External-mode installs instead run:"
        echo "    ./scripts/init.sh --mode external --domain <domain> --tls byo --cert <fullchain.pem> --key <privkey.pem> --yes"
    fi
    echo ""

    # mode=<internal|external|unknown> is pinned grammar: pre-install and an
    # invalid DOMAIN_MODE both clamp to unknown.
    local mode_out="$DOCTOR_MODE" status="pass"
    [[ "$DOCTOR_STAGE" == "preinstall" ]] && mode_out="unknown"
    [[ "$mode_out" == "internal" || "$mode_out" == "external" ]] || mode_out="unknown"
    [[ "$CHECKS_FAILED" -gt 0 ]] && status="fail"
    echo "VOIPBIN_DOCTOR: status=$status passed=$CHECKS_PASSED failed=$CHECKS_FAILED warned=$CHECKS_WARNED mode=$mode_out"
    [[ "$status" == "pass" ]] && exit 0
    exit 1
}

main "$@"
