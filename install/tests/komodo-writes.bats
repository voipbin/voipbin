#!/usr/bin/env bats
# Tests for the Komodo write paths: sync_cert_vars, the IP and certificate
# regeneration functions, the deferred write result, start.sh, install-certs.sh,
# clean.sh and komodo-setup.sh --teardown.
#
# Isolation: docker, mkcert, systemctl, sudo and id are stubs, komodo_lib.py is
# replaced by a recording fake (VOIPBIN_KOMODO_LIB), PROJECT_DIR is a temp
# directory, and every system path of komodo-setup.sh is redirected under a
# temp prefix (VOIPBIN_KOMODO_ROOT). Nothing touches a real Komodo, docker
# daemon, systemd, or network.

bats_require_minimum_version 1.5.0

load 'test_helper'

setup() {
    setup_test_env
    export T="$TEST_TEMP_DIR"
    export NO_COLOR=1
    unset COMPOSE_ENV_FILES COMPOSE_PROFILES
    export VOIPBIN_KOMODO_LIB="$T/fake_lib.py"
    export VOIPBIN_KOMODO_RETRY_INTERVAL=0
    FIXTURES="$T/fixtures"
    mkdir -p "$FIXTURES"
    write_fake_lib
    write_fake_docker
    source "$SCRIPTS_DIR/common.sh"
}

teardown() {
    chmod -R u+rwx "$T" 2>/dev/null || true
    teardown_test_env
}

# -----------------------------------------------------------------------------
# Fakes and fixtures
# -----------------------------------------------------------------------------

# Records every call. Exit code of a sub command: first line of $T/<sub>.seq
# (consumed one per call) when that file exists, else $T/<sub>.rc, else 0.
# stderr text from $T/<sub>.err. var-set input is appended to $T/stdin.log.
write_fake_lib() {
    cat > "$T/fake_lib.py" <<'PY'
import os, sys
t = os.environ["T"]
sub = sys.argv[1]
with open(t + "/lib.log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
if sub == "var-set":
    data = sys.stdin.read()
    with open(t + "/stdin.log", "a") as f:
        f.write(data)
err = t + "/" + sub + ".err"
if os.path.exists(err):
    sys.stderr.write(open(err).read())
seq = t + "/" + sub + ".seq"
rc_file = t + "/" + sub + ".rc"
if os.path.exists(seq):
    lines = open(seq).read().split("\n")
    rc = int(lines[0])
    open(seq, "w").write("\n".join(lines[1:]) if len(lines) > 2 else "")
    if not os.path.getsize(seq):
        os.remove(seq)
    sys.exit(rc)
sys.exit(int(open(rc_file).read().strip()) if os.path.exists(rc_file) else 0)
PY
}

# Records `args` and `ENV|args`; `compose ps` prints $T/compose-ps.out, `ps`
# prints $T/ps.out; exit code from $T/docker.rc.
write_fake_docker() {
    mock_command_script docker 'echo "$*" >> "$T/docker.log"
echo "${COMPOSE_ENV_FILES-unset}|$*" >> "$T/docker.env.log"
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
    [[ -f "$T/compose-ps.out" ]] && cat "$T/compose-ps.out"
    exit 0
fi
if [[ "$1" == "ps" ]]; then
    [[ -f "$T/ps.out" ]] && cat "$T/ps.out"
    exit 0
fi
if [[ -f "$T/docker.rc" ]]; then exit "$(cat "$T/docker.rc")"; fi
exit 0'
}

lib_calls() { [[ -f "$T/lib.log" ]] && cat "$T/lib.log" || true; }
docker_calls() { [[ -f "$T/docker.log" ]] && cat "$T/docker.log" || true; }
lib_count() { lib_calls | grep -c "^$1" || true; }

mark_adopted() {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/.adopted"
}

make_copy() {
    printf '%s\n' "$@" > "$PROJECT_DIR/.env.komodo"
}

file_hash() { sha256sum "$1" | cut -d' ' -f1; }

make_certs_api() {
    mkdir -p "$PROJECT_DIR/certs/api"
    printf 'CERTDATA\n' > "$PROJECT_DIR/certs/api/cert.pem"
    printf 'KEYDATA\n' > "$PROJECT_DIR/certs/api/privkey.pem"
    CERT_B64=$(base64 -w0 "$PROJECT_DIR/certs/api/cert.pem")
    KEY_B64=$(base64 -w0 "$PROJECT_DIR/certs/api/privkey.pem")
}

# -----------------------------------------------------------------------------
# sync_cert_vars
# -----------------------------------------------------------------------------

@test "sync_cert_vars none: nothing is done" {
    make_certs_api
    printf 'API_SSL_CERT_BASE64=old\n' > "$PROJECT_DIR/.env"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
    [ "$(cat "$PROJECT_DIR/.env")" = "API_SSL_CERT_BASE64=old" ]
}

@test "sync_cert_vars adopted: writes the four keys in pair order when all differ" {
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=old" "API_SSL_PRIVKEY_BASE64=old" "HOOK_SSL_CERT_BASE64=old" "HOOK_SSL_PRIVKEY_BASE64=old"
    printf 'A=1\n' > "$PROJECT_DIR/.env"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [ "$(lib_calls)" = "var-set" ]
    [ "$(cat "$T/stdin.log")" = "API_SSL_CERT_BASE64=$CERT_B64
API_SSL_PRIVKEY_BASE64=$KEY_B64
HOOK_SSL_CERT_BASE64=$CERT_B64
HOOK_SSL_PRIVKEY_BASE64=$KEY_B64" ]
}

@test "sync_cert_vars adopted: writes only the keys that differ" {
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=$CERT_B64" "API_SSL_PRIVKEY_BASE64=$KEY_B64" "HOOK_SSL_CERT_BASE64=old" "HOOK_SSL_PRIVKEY_BASE64=$KEY_B64"
    printf 'A=1\n' > "$PROJECT_DIR/.env"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [ "$(cat "$T/stdin.log")" = "HOOK_SSL_CERT_BASE64=$CERT_B64" ]
}

@test "sync_cert_vars adopted: files and copy equal means zero calls and the marker is cleared" {
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=$CERT_B64" "API_SSL_PRIVKEY_BASE64=$KEY_B64" "HOOK_SSL_CERT_BASE64=$CERT_B64" "HOOK_SSL_PRIVKEY_BASE64=$KEY_B64"
    printf 'A=1\n' > "$PROJECT_DIR/.env"
    echo "x" > "$PROJECT_DIR/.certs-sync-pending"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
    [ ! -e "$PROJECT_DIR/.certs-sync-pending" ]
}

@test "sync_cert_vars adopted: a successful write clears an earlier marker" {
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=old" "API_SSL_PRIVKEY_BASE64=old" "HOOK_SSL_CERT_BASE64=old" "HOOK_SSL_PRIVKEY_BASE64=old"
    echo "x" > "$PROJECT_DIR/.certs-sync-pending"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [ ! -e "$PROJECT_DIR/.certs-sync-pending" ]
}

@test "sync_cert_vars adopted: no certificate files means no call and the marker is left alone" {
    mark_adopted
    make_copy "API_SSL_CERT_BASE64=old"
    echo "x" > "$PROJECT_DIR/.certs-sync-pending"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
}

@test "sync_cert_vars adopted: an unreadable key skips all four keys, warns and leaves the marker" {
    if [[ $EUID -eq 0 ]]; then skip "root can read everything"; fi
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=old"
    chmod 000 "$PROJECT_DIR/certs/api/privkey.pem"
    run sync_cert_vars
    [ "$status" -eq 0 ]
    [[ "$output" == *'sudo ./voipbin start'* ]]
    [ ! -e "$T/lib.log" ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
    [ "$(stat -c %a "$PROJECT_DIR/.certs-sync-pending")" = "600" ]
}

@test "sync_cert_vars adopted: a failed write leaves the marker and returns the code" {
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=old"
    echo 21 > "$T/var-set.rc"
    run sync_cert_vars
    [ "$status" -eq 21 ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
}

@test "sync_cert_vars adopted: rejected values (2) neither create nor remove the marker" {
    mark_adopted
    make_certs_api
    make_copy "API_SSL_CERT_BASE64=old"
    echo 2 > "$T/var-set.rc"
    run sync_cert_vars
    [ "$status" -eq 2 ]
    [ ! -e "$PROJECT_DIR/.certs-sync-pending" ]
    echo "x" > "$PROJECT_DIR/.certs-sync-pending"
    run sync_cert_vars
    [ "$status" -eq 2 ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
}

@test "sync_cert_vars broken: refused with 21" {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    make_certs_api
    run sync_cert_vars
    [ "$status" -eq 21 ]
    [ ! -e "$T/lib.log" ]
}

@test "sync_cert_vars leaves the marker before it returns a failure (set -e safe order)" {
    run bash -c '
        sed -n "/^sync_cert_vars()/,/^}/p" "$1" | grep -n "komodo_write_marker\|return \"\$rc\"" | head -4
    ' _ "$SCRIPTS_DIR/common.sh"
    local first_marker first_return
    first_marker=$(echo "$output" | grep komodo_write_marker | tail -1 | cut -d: -f1)
    first_return=$(echo "$output" | grep 'return "\$rc"' | head -1 | cut -d: -f1)
    [ -n "$first_marker" ]
    [ -n "$first_return" ]
    [ "$first_marker" -lt "$first_return" ]
}

# -----------------------------------------------------------------------------
# update_env_ips
# -----------------------------------------------------------------------------

env_for_ips() {
    printf '%s\n' "DOMAIN_MODE=internal" "BASE_DOMAIN=voipbin.test" "KAMAILIO_EXTERNAL_IP=203.0.113.1" \
        "RTPENGINE_EXTERNAL_IP=203.0.113.2" "HOST_EXTERNAL_IP=203.0.113.9" "API_URL=old" "WEBSOCKET_URL=old" "EXTERNAL_IP_PINNED=false" \
        "$@" > "$PROJECT_DIR/.env"
}

@test "update_env_ips none: the historic sed rewrite, stdout is the IP, Komodo is not called" {
    env_for_ips
    run --separate-stderr update_env_ips 203.0.113.5
    [ "$status" -eq 0 ]
    [ "$output" = "203.0.113.13" ]
    [ ! -e "$T/lib.log" ]
    [ "$(cat "$PROJECT_DIR/.env")" = "DOMAIN_MODE=internal
BASE_DOMAIN=voipbin.test
KAMAILIO_EXTERNAL_IP=203.0.113.13
RTPENGINE_EXTERNAL_IP=203.0.113.14
HOST_EXTERNAL_IP=203.0.113.5
API_URL=https://api.voipbin.test:8443/
WEBSOCKET_URL=wss://api.voipbin.test:8443/v1.0/ws
EXTERNAL_IP_PINNED=false" ]
}

@test "update_env_ips adopted: ping first, then one var-set with HOST_EXTERNAL_IP last, stdout is only the IP" {
    mark_adopted
    env_for_ips
    make_copy "KAMAILIO_EXTERNAL_IP=203.0.113.1"
    run --separate-stderr update_env_ips 203.0.113.5
    [ "$status" -eq 0 ]
    [ "$output" = "203.0.113.13" ]
    [ "$(lib_calls)" = "ping
var-set" ]
    [ "$(cat "$T/stdin.log")" = "KAMAILIO_EXTERNAL_IP=203.0.113.13
RTPENGINE_EXTERNAL_IP=203.0.113.14
API_URL=https://api.voipbin.test:8443/
WEBSOCKET_URL=wss://api.voipbin.test:8443/v1.0/ws
HOST_EXTERNAL_IP=203.0.113.5" ]
}

@test "update_env_ips adopted: the status lines go to stderr, never into the captured value" {
    mark_adopted
    env_for_ips
    make_copy "A=1"
    local captured
    captured=$(update_env_ips 203.0.113.5 2>/dev/null)
    [ "$captured" = "203.0.113.13" ]
    [ "$(printf '%s' "$captured" | wc -l)" -eq 0 ]
}

@test "update_env_ips adopted: pinned IPs are not recomputed, the existing Kamailio IP is returned" {
    mark_adopted
    env_for_ips
    make_copy "KAMAILIO_EXTERNAL_IP=198.51.100.1" "EXTERNAL_IP_PINNED=true"
    run --separate-stderr update_env_ips 203.0.113.5
    [ "$status" -eq 0 ]
    [ "$output" = "198.51.100.1" ]
    [[ "$(cat "$T/stdin.log")" != *KAMAILIO_EXTERNAL_IP* ]]
    [[ "$(cat "$T/stdin.log")" == *HOST_EXTERNAL_IP=203.0.113.5 ]]
}

@test "update_env_ips adopted: external mode leaves the URLs alone" {
    mark_adopted
    env_for_ips "DOMAIN_MODE=external"
    make_copy "A=1"
    run --separate-stderr update_env_ips 203.0.113.5
    [ "$status" -eq 0 ]
    [[ "$(cat "$T/stdin.log")" != *API_URL* ]]
    [[ "$(cat "$T/stdin.log")" != *WEBSOCKET_URL* ]]
}

@test "update_env_ips adopted: an unreachable Komodo stops before anything is written, no value on stdout" {
    mark_adopted
    env_for_ips
    make_copy "A=1"
    echo 21 > "$T/ping.rc"
    run --separate-stderr update_env_ips 203.0.113.5
    [ "$status" -eq 21 ]
    [ -z "$output" ]
    [ "$(lib_calls)" = "ping" ]
}

@test "update_env_ips adopted: a failed write is returned and no value is printed" {
    mark_adopted
    env_for_ips
    make_copy "A=1"
    echo 21 > "$T/var-set.rc"
    run --separate-stderr update_env_ips 203.0.113.5
    [ "$status" -eq 21 ]
    [ -z "$output" ]
}

@test "update_env_ips broken: refused with 21 before any write" {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    env_for_ips
    local before; before=$(file_hash "$PROJECT_DIR/.env")
    echo 21 > "$T/ping.rc"
    run update_env_ips 203.0.113.5
    [ "$status" -eq 21 ]
    [ "$(file_hash "$PROJECT_DIR/.env")" = "$before" ]
}

# -----------------------------------------------------------------------------
# regenerate_ssl_certs and regenerate_ip_config
# -----------------------------------------------------------------------------

stub_mkcert() {
    mock_command_script mkcert '
cert=""; key=""
echo "$*" >> "$T/mkcert.args"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -cert-file) cert="$2"; shift 2 ;;
        -key-file) key="$2"; shift 2 ;;
        *) shift ;;
    esac
done
printf "NEWCERT\n" > "$cert"
printf "NEWKEY\n" > "$key"
exit 0'
}

@test "regenerate_ssl_certs none: the four keys are rewritten in .env as before" {
    stub_mkcert
    printf 'TLS_MODE=mkcert\nAPI_SSL_CERT_BASE64=x\nAPI_SSL_PRIVKEY_BASE64=x\nHOOK_SSL_CERT_BASE64=x\nHOOK_SSL_PRIVKEY_BASE64=x\n' > "$PROJECT_DIR/.env"
    run regenerate_ssl_certs 203.0.113.5
    [ "$status" -eq 0 ]
    local cert_b64; cert_b64=$(printf 'NEWCERT\n' | base64 -w0)
    [[ "$(cat "$PROJECT_DIR/.env")" == *"API_SSL_CERT_BASE64=$cert_b64"* ]]
    [[ "$(cat "$PROJECT_DIR/.env")" == *"HOOK_SSL_CERT_BASE64=$cert_b64"* ]]
    [ ! -e "$T/lib.log" ]
}

@test "regenerate_ssl_certs adopted: one var-set with the four keys, .env untouched" {
    stub_mkcert
    mark_adopted
    printf 'TLS_MODE=mkcert\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    regenerate_ssl_certs 203.0.113.5
    local cert_b64 key_b64
    cert_b64=$(printf 'NEWCERT\n' | base64 -w0)
    key_b64=$(printf 'NEWKEY\n' | base64 -w0)
    [ "$(cat "$T/stdin.log")" = "API_SSL_CERT_BASE64=$cert_b64
API_SSL_PRIVKEY_BASE64=$key_b64
HOOK_SSL_CERT_BASE64=$cert_b64
HOOK_SSL_PRIVKEY_BASE64=$key_b64" ]
    [ "$(cat "$PROJECT_DIR/.env")" = "TLS_MODE=mkcert" ]
    [ "$KOMODO_WRITE_RC" -eq 0 ]
}

@test "regenerate_ssl_certs adopted: a failed write is deferred (returns 0, KOMODO_WRITE_RC set, marker left)" {
    stub_mkcert
    mark_adopted
    printf 'TLS_MODE=mkcert\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    echo 21 > "$T/var-set.rc"
    regenerate_ssl_certs 203.0.113.5
    [ "$KOMODO_WRITE_RC" -eq 21 ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
}

# Makes `command -v mkcert` fail whatever is installed on the machine.
hide_mkcert() {
    command() {
        if [[ "${1:-}" == "-v" && "${2:-}" == "mkcert" ]]; then return 1; fi
        builtin command "$@"
    }
}

@test "regenerate_ssl_certs adopted: a missing mkcert still returns 1" {
    mark_adopted
    printf 'TLS_MODE=mkcert\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    hide_mkcert
    run regenerate_ssl_certs 203.0.113.5
    [ "$status" -eq 1 ]
    [ ! -e "$T/lib.log" ]
}

@test "regenerate_ssl_certs adopted: byo is skipped without a write" {
    mark_adopted
    printf 'TLS_MODE=byo\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    run regenerate_ssl_certs 203.0.113.5
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
}

@test "regenerate_ip_config adopted: an unreachable Komodo stops it before CoreDNS or certificates are touched" {
    mark_adopted
    mock_ip_route 203.0.113.5
    stub_mkcert
    printf 'DOMAIN_MODE=internal\nTLS_MODE=mkcert\nBASE_DOMAIN=voipbin.test\nHOST_EXTERNAL_IP=203.0.113.9\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    echo 21 > "$T/ping.rc"
    run regenerate_ip_config
    [ "$status" -eq 21 ]
    [ ! -e "$PROJECT_DIR/config/coredns/Corefile" ]
    [ ! -e "$PROJECT_DIR/certs/api/cert.pem" ]
}

@test "regenerate_ip_config adopted: order is IP write, CoreDNS, certificates; HOST_EXTERNAL_IP is last of the IP keys" {
    mark_adopted
    mock_ip_route 203.0.113.5
    stub_mkcert
    printf 'DOMAIN_MODE=internal\nTLS_MODE=mkcert\nBASE_DOMAIN=voipbin.test\nHOST_EXTERNAL_IP=203.0.113.9\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    run regenerate_ip_config
    [ "$status" -eq 0 ]
    [ "$(lib_count var-set)" -eq 2 ]
    [ -f "$PROJECT_DIR/config/coredns/Corefile" ]
    [ -f "$PROJECT_DIR/certs/api/cert.pem" ]
    local first; first=$(sed -n 1,5p "$T/stdin.log" | tail -1)
    [ "$first" = "HOST_EXTERNAL_IP=203.0.113.5" ]
}

@test "regenerate_ip_config adopted: a missing mkcert does not undo the IP write" {
    mark_adopted
    mock_ip_route 203.0.113.5
    printf 'DOMAIN_MODE=internal\nTLS_MODE=mkcert\nBASE_DOMAIN=voipbin.test\nHOST_EXTERNAL_IP=203.0.113.9\n' > "$PROJECT_DIR/.env"
    make_copy "A=1"
    hide_mkcert
    run regenerate_ip_config
    [ "$status" -ne 0 ]
    [[ "$(cat "$T/stdin.log")" == *HOST_EXTERNAL_IP=203.0.113.5 ]]
}

# -----------------------------------------------------------------------------
# komodo_write_rc_exit
# -----------------------------------------------------------------------------

@test "komodo_write_rc_exit: returns 0 and lets a set -e script continue when nothing failed" {
    run bash -c 'set -e; source "$1"; KOMODO_WRITE_RC=0; if false; then :; fi; komodo_write_rc_exit; echo after' _ "$SCRIPTS_DIR/common.sh"
    [ "$status" -eq 0 ]
    [ "$output" = "after" ]
}

@test "komodo_write_rc_exit: exits with the stored code and says what to do" {
    run bash -c 'source "$1"; KOMODO_WRITE_RC=21; komodo_write_rc_exit; echo after' _ "$SCRIPTS_DIR/common.sh"
    [ "$status" -eq 21 ]
    [[ "$output" == *'sudo ./voipbin start'* ]]
    [[ "$output" != *after* ]]
    run bash -c 'source "$1"; KOMODO_WRITE_RC=2; komodo_write_rc_exit' _ "$SCRIPTS_DIR/common.sh"
    [ "$status" -eq 2 ]
    [[ "$output" == *'rejected'* ]]
}

# -----------------------------------------------------------------------------
# setup-dns.sh --regenerate (the deferred result ends the run)
# -----------------------------------------------------------------------------

run_dns_regenerate() {
    export RESOLV_CONF="$T/resolv.conf" RESOLV_UPSTREAMS="$T/upstreams" SYSTEMD_RESOLVE_CONF="$T/none-resolve.conf"
    sed -e '/^main "\$@"$/d' -e "s|^source \"\$SCRIPT_DIR/common.sh\"|source \"$SCRIPTS_DIR/common.sh\"|" \
        "$SCRIPTS_DIR/setup-dns.sh" > "$T/setup_dns_fn.sh"
    run bash -c '
        SCRIPT_DIR="$1"
        source "$2"
        SCRIPT_DIR="$1"
        PROJECT_DIR="$3"
        check_root() { :; }
        sleep() { :; }
        main --regenerate
        echo "reached-after-main"' _ "$SCRIPTS_DIR" "$T/setup_dns_fn.sh" "$PROJECT_DIR"
}

dns_fixture() {
    mock_ip_route 203.0.113.5
    stub_mkcert
    printf 'DOMAIN_MODE=internal\nTLS_MODE=mkcert\nBASE_DOMAIN=voipbin.test\nHOST_EXTERNAL_IP=203.0.113.9\nKAMAILIO_EXTERNAL_IP=203.0.113.1\n' > "$PROJECT_DIR/.env"
    printf 'voipbin-dns\nvoipbin-api-mgr\n' > "$T/ps.out"
}

@test "setup-dns.sh --regenerate none: exits 0 and restarts through docker compose directly" {
    dns_fixture
    run_dns_regenerate
    [ "$status" -eq 0 ]
    [[ "$output" != *reached-after-main* ]]
    [[ "$(docker_calls)" == *"compose rm -sf api-manager"* ]]
    [[ "$(docker_calls)" == *"compose up -d api-manager"* ]]
    [[ "$(docker_calls)" == *"compose restart coredns"* ]]
    [ ! -e "$T/lib.log" ]
}

@test "setup-dns.sh --regenerate adopted: every host step runs, then the failed certificate write ends the run with 21" {
    mark_adopted
    dns_fixture
    make_copy "A=1"
    printf '0\n21\n' > "$T/var-set.seq"
    run_dns_regenerate
    [ "$status" -eq 21 ]
    [[ "$output" == *'sudo ./voipbin start'* ]]
    # the steps after the failed write still ran, through the bridge
    [[ "$(lib_calls)" == *"exec restart coredns"* ]]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
    [ -f "$PROJECT_DIR/config/coredns/Corefile" ]
    [ -z "$(docker_calls | grep '^compose \(up\|rm\|restart\)')" ]
}

# -----------------------------------------------------------------------------
# start.sh
# -----------------------------------------------------------------------------

@test "setup_mkcert none: the inline sed rewrite and the restart stay where they were" {
    load_start_functions
    stub_mkcert
    printf 'TLS_MODE=mkcert\nHOST_EXTERNAL_IP=203.0.113.5\nAPI_SSL_CERT_BASE64=x\nAPI_SSL_PRIVKEY_BASE64=x\nHOOK_SSL_CERT_BASE64=x\nHOOK_SSL_PRIVKEY_BASE64=x\n' > "$PROJECT_DIR/.env"
    printf 'voipbin-api-mgr\n' > "$T/ps.out"
    run setup_mkcert
    [ "$status" -eq 0 ]
    local cert_b64; cert_b64=$(printf 'NEWCERT\n' | base64 -w0)
    [[ "$(cat "$PROJECT_DIR/.env")" == *"API_SSL_CERT_BASE64=$cert_b64"* ]]
    [[ "$(cat "$PROJECT_DIR/.env")" == *"HOOK_SSL_PRIVKEY_BASE64=$(printf 'NEWKEY\n' | base64 -w0)"* ]]
    [ "$(docker_calls | grep '^compose')" = "compose restart api-manager hook-manager" ]
    [ ! -e "$T/lib.log" ]
}

@test "setup_mkcert adopted: only the files are made, no sed, no restart yet" {
    load_start_functions
    stub_mkcert
    mark_adopted
    printf 'TLS_MODE=mkcert\nHOST_EXTERNAL_IP=203.0.113.5\nAPI_SSL_CERT_BASE64=old\nAPI_SSL_PRIVKEY_BASE64=old\nHOOK_SSL_CERT_BASE64=old\nHOOK_SSL_PRIVKEY_BASE64=old\n' > "$PROJECT_DIR/.env"
    make_copy "HOST_EXTERNAL_IP=203.0.113.5"
    printf 'voipbin-api-mgr\n' > "$T/ps.out"
    local before; before=$(file_hash "$PROJECT_DIR/.env")
    MKCERT_RESTART_PENDING=false
    setup_mkcert > "$T/out.txt"
    [ "$(file_hash "$PROJECT_DIR/.env")" = "$before" ]
    [ -f "$PROJECT_DIR/certs/api/cert.pem" ]
    [ "$MKCERT_RESTART_PENDING" = "true" ]
    [ ! -e "$T/lib.log" ]
    [ -z "$(docker_calls | grep '^compose')" ]
}

@test "setup_mkcert: the host address of the new API certificate comes from the Variable copy with Komodo and from .env without" {
    load_start_functions
    stub_mkcert
    printf 'TLS_MODE=mkcert\nHOST_EXTERNAL_IP=203.0.113.5\n' > "$PROJECT_DIR/.env"
    printf 'voipbin-api-mgr\n' > "$T/ps.out"
    setup_mkcert > "$T/out.txt"
    grep -q -- '-key-file .*api/privkey.pem .*203.0.113.5' "$T/mkcert.args"
    rm -rf "$PROJECT_DIR/certs" "$T/mkcert.args"
    mark_adopted
    printf 'TLS_MODE=mkcert\n' > "$PROJECT_DIR/.env"
    make_copy "HOST_EXTERNAL_IP=198.51.100.7"
    MKCERT_RESTART_PENDING=false
    setup_mkcert > "$T/out.txt"
    grep -q -- '-key-file .*api/privkey.pem .*198.51.100.7' "$T/mkcert.args"
}

@test "restart_cert_consumers: restarts through the bridge once, only when mkcert asked for it" {
    load_start_functions
    mark_adopted
    printf 'voipbin-api-mgr\n' > "$T/ps.out"
    MKCERT_RESTART_PENDING=false
    restart_cert_consumers
    [ ! -e "$T/lib.log" ]
    MKCERT_RESTART_PENDING=true
    restart_cert_consumers > /dev/null
    [ "$(lib_calls)" = "exec restart api-manager hook-manager" ]
    [ "$MKCERT_RESTART_PENDING" = "false" ]
}

@test "restart_cert_consumers: only an unreachable Komodo (21) stops the start, other failures are ignored" {
    load_start_functions
    mark_adopted
    printf 'voipbin-api-mgr\n' > "$T/ps.out"
    echo 1 > "$T/exec.rc"
    MKCERT_RESTART_PENDING=true
    run restart_cert_consumers
    [ "$status" -eq 0 ]
    echo 21 > "$T/exec.rc"
    echo "Komodo is not available (core)" > "$T/exec.err"
    MKCERT_RESTART_PENDING=true
    run restart_cert_consumers
    [ "$status" -eq 21 ]
    [[ "$output" == *'not available (core)'* ]]
}

@test "komodo_warn_pending_markers: names both unfinished certificate states" {
    load_start_functions
    run komodo_warn_pending_markers
    [ -z "$output" ]
    : > "$PROJECT_DIR/.certs-pending"
    : > "$PROJECT_DIR/.certs-sync-pending"
    run komodo_warn_pending_markers
    [[ "$output" == *'.certs-pending'* ]]
    [[ "$output" == *'.certs-sync-pending'* ]]
}

# start.sh as a script, up to the first compose call
prepare_start_run() {
    if [[ $EUID -eq 0 ]]; then skip "test requires an unprivileged user"; fi
    mock_command_script ip 'if [[ "$1" == "route" && "$2" == "get" ]]; then echo "8.8.8.8 via 198.51.100.1 dev eth0 src 198.51.100.100 uid 1000"; exit 0; fi
echo master; exit 0'
    export RESOLV_CONF="$T/resolv.conf"
    echo "nameserver 127.0.0.1" > "$RESOLV_CONF"
    printf '%s\n' "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test" "TLS_MODE=byo" > "$PROJECT_DIR/.env"
    openssl req -x509 -nodes -newkey rsa:2048 -keyout "$T/k.pem" -out "$T/c.pem" -days 30 -subj "/CN=x" 2>/dev/null
    mkdir -p "$PROJECT_DIR/certs/api"
    cp "$T/c.pem" "$PROJECT_DIR/certs/api/cert.pem"
    cp "$T/k.pem" "$PROJECT_DIR/certs/api/privkey.pem"
    CERT_B64=$(base64 -w0 "$T/c.pem")
    KEY_B64=$(base64 -w0 "$T/k.pem")
    make_copy "HOST_EXTERNAL_IP=198.51.100.100" "KAMAILIO_EXTERNAL_IP=198.51.100.108" \
        "API_SSL_CERT_BASE64=$CERT_B64" "API_SSL_PRIVKEY_BASE64=$KEY_B64" \
        "HOOK_SSL_CERT_BASE64=$CERT_B64" "HOOK_SSL_PRIVKEY_BASE64=$KEY_B64"
}

@test "start.sh adopted: an unreachable Komodo ends the run with 21 before any host change" {
    prepare_start_run
    mark_adopted
    echo 21 > "$T/ping.rc"
    echo "Komodo is not available (periphery)" > "$T/ping.err"
    local env_before copy_before
    env_before=$(file_hash "$PROJECT_DIR/.env"); copy_before=$(file_hash "$PROJECT_DIR/.env.komodo")
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 21 ]
    [[ "$output" == *'VOIPBIN_START: status=error reason="komodo unavailable"'* ]]
    [ "$(lib_calls)" = "ping" ]
    [ "$(file_hash "$PROJECT_DIR/.env")" = "$env_before" ]
    [ "$(file_hash "$PROJECT_DIR/.env.komodo")" = "$copy_before" ]
    [ -z "$(docker_calls | grep '^compose \(up\|rm\|restart\|down\|stop\)')" ]
}

@test "start.sh broken: refused with 21 before any host change" {
    prepare_start_run
    rm -f "$PROJECT_DIR/.env.komodo"
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    echo 21 > "$T/ping.rc"
    echo "Komodo state is incomplete. Run: sudo ./voipbin komodo setup" > "$T/ping.err"
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 21 ]
    [[ "$output" == *'komodo setup'* ]]
    [ "$(lib_calls)" = "ping" ]
}

@test "start.sh adopted: the pre-change check comes first, then the infrastructure goes up through the bridge only" {
    prepare_start_run
    mark_adopted
    echo 16 > "$T/exec.rc"
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 16 ]
    [ "$(lib_calls)" = "ping
exec up db redis rabbitmq" ]
    [ -z "$(docker_calls | grep '^compose up')" ]
}

@test "start.sh adopted: certificate files that differ from the copy are synced before validation" {
    prepare_start_run
    mark_adopted
    make_copy "HOST_EXTERNAL_IP=198.51.100.100" "KAMAILIO_EXTERNAL_IP=198.51.100.108" \
        "API_SSL_CERT_BASE64=old" "API_SSL_PRIVKEY_BASE64=$KEY_B64" \
        "HOOK_SSL_CERT_BASE64=$CERT_B64" "HOOK_SSL_PRIVKEY_BASE64=$KEY_B64"
    echo 16 > "$T/exec.rc"
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 16 ]
    [ "$(lib_calls | head -2)" = "ping
var-set" ]
    [ "$(cat "$T/stdin.log")" = "API_SSL_CERT_BASE64=$CERT_B64" ]
}

@test "start.sh adopted: a failed certificate sync ends the run with its code and leaves the marker" {
    prepare_start_run
    mark_adopted
    make_copy "HOST_EXTERNAL_IP=198.51.100.100" "API_SSL_CERT_BASE64=old"
    echo 21 > "$T/var-set.rc"
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 21 ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
    [ "$(lib_count exec)" -eq 0 ]
}

@test "start.sh adopted: a certificate write that failed during the IP change ends the start before anything is deployed" {
    prepare_start_run
    mark_adopted
    # mkcert managed certificate (issuer names mkcert) and a changed host IP
    openssl req -x509 -nodes -newkey rsa:2048 -keyout "$PROJECT_DIR/certs/api/privkey.pem" -out "$PROJECT_DIR/certs/api/cert.pem" \
        -days 30 -subj "/O=mkcert dev/CN=mkcert test" 2>/dev/null
    local c k
    c=$(base64 -w0 "$PROJECT_DIR/certs/api/cert.pem"); k=$(base64 -w0 "$PROJECT_DIR/certs/api/privkey.pem")
    sed -i 's/^TLS_MODE=byo/TLS_MODE=mkcert/' "$PROJECT_DIR/.env"
    make_copy "HOST_EXTERNAL_IP=198.51.100.99" "KAMAILIO_EXTERNAL_IP=198.51.100.108" \
        "API_SSL_CERT_BASE64=$c" "API_SSL_PRIVKEY_BASE64=$k" "HOOK_SSL_CERT_BASE64=$c" "HOOK_SSL_PRIVKEY_BASE64=$k"
    mock_command_script mkcert 'cert=""; key=""
while [[ $# -gt 0 ]]; do case "$1" in -cert-file) cert="$2"; shift 2 ;; -key-file) key="$2"; shift 2 ;; *) shift ;; esac; done
[[ -n "$cert" ]] && printf "NEWCERT\n" > "$cert"
[[ -n "$key" ]] && printf "NEWKEY\n" > "$key"
exit 0'
    printf '0\n21\n' > "$T/var-set.seq"
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 21 ]
    [ "$(lib_count var-set)" -eq 2 ]
    [ "$(lib_count exec)" -eq 0 ]
    [ -e "$PROJECT_DIR/.certs-sync-pending" ]
}

@test "start.sh adopted: unfinished certificate work is mentioned at the start" {
    prepare_start_run
    mark_adopted
    : > "$PROJECT_DIR/.certs-pending"
    echo 21 > "$T/ping.rc"
    run bash "$SCRIPTS_DIR/start.sh"
    [[ "$output" == *'.certs-pending'* ]]
}

@test "start.sh none: no Komodo call at all, the infrastructure goes up with docker compose directly" {
    prepare_start_run
    rm -f "$PROJECT_DIR/.env.komodo"
    printf '%s\n' "HOST_EXTERNAL_IP=198.51.100.100" "API_SSL_CERT_BASE64=$CERT_B64" "API_SSL_PRIVKEY_BASE64=$KEY_B64" >> "$PROJECT_DIR/.env"
    docker_stub_stop_after_up() {
        mock_command_script docker 'echo "$*" >> "$T/docker.log"
if [[ "$1 $2" == "compose up" ]]; then exit 16; fi
exit 0'
    }
    docker_stub_stop_after_up
    run bash "$SCRIPTS_DIR/start.sh"
    [ "$status" -eq 16 ]
    [ ! -e "$T/lib.log" ]
    [ "$(docker_calls | grep '^compose up')" = "compose up -d db redis rabbitmq" ]
}

@test "start.sh source: the four state changing compose calls go through compose_exec, in the old order" {
    run bash -c 'grep -n "compose_exec\|docker compose \(up\|restart\|rm\|down\|stop\)" "$1" | grep -v "^[0-9]*:[[:space:]]*#"' _ "$SCRIPTS_DIR/start.sh"
    [[ "$output" == *'compose_exec up -d db redis rabbitmq'* ]]
    # the full start is its own line, not only the prefix of the first
    [ "$(grep -c '^    compose_exec up -d$' "$SCRIPTS_DIR/start.sh")" -eq 1 ]
    [[ "$output" == *'compose_exec restart kamailio rtpengine'* ]]
    [[ "$output" == *'compose_exec restart api-manager hook-manager'* ]]
    # the only direct restart left is the one of the unchanged (none) path
    [ "$(echo "$output" | grep -c 'docker compose restart')" -eq 1 ]
}

# -----------------------------------------------------------------------------
# install-certs.sh
# -----------------------------------------------------------------------------

make_wildcard_cert() {
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "$FIXTURES/wildcard.key" -out "$FIXTURES/wildcard.pem" -days 90 \
        -subj "/CN=example.com" \
        -addext "subjectAltName=DNS:example.com,DNS:*.example.com,DNS:*.registrar.example.com" 2>/dev/null
}

prepare_certs_run() {
    make_wildcard_cert
    printf '%s\n' "DOMAIN_MODE=external" "COMPOSE_PROFILES=" "BASE_DOMAIN=example.com" \
        "API_SSL_CERT_BASE64=old" "API_SSL_PRIVKEY_BASE64=old" \
        "HOOK_SSL_CERT_BASE64=old" "HOOK_SSL_PRIVKEY_BASE64=old" > "$PROJECT_DIR/.env"
    printf 'api-manager\nkamailio\nweb-proxy\n' > "$T/compose-ps.out"
}

run_install_certs() {
    run bash "$SCRIPTS_DIR/install-certs.sh" --domain example.com "$@" "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"
}

@test "install-certs.sh adopted: pre-check, one four-key write, recreate through the bridge, no marker left" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    local env_before; env_before=$(file_hash "$PROJECT_DIR/.env")
    run_install_certs
    [ "$status" -eq 0 ]
    [ "$(lib_calls)" = "ping
var-set
exec rm api-manager hook-manager
exec up api-manager hook-manager
exec restart kamailio
exec restart web-proxy" ]
    local cert_b64 key_b64
    cert_b64=$(base64 -w0 < "$FIXTURES/wildcard.pem"); key_b64=$(base64 -w0 < "$FIXTURES/wildcard.key")
    [ "$(cat "$T/stdin.log")" = "API_SSL_CERT_BASE64=$cert_b64
API_SSL_PRIVKEY_BASE64=$key_b64
HOOK_SSL_CERT_BASE64=$cert_b64
HOOK_SSL_PRIVKEY_BASE64=$key_b64" ]
    [ "$(file_hash "$PROJECT_DIR/.env")" = "$env_before" ]
    [ -f "$PROJECT_DIR/certs/api/cert.pem" ]
    [ ! -e "$PROJECT_DIR/.certs-pending" ]
    [ -z "$(docker_calls | grep '^compose \(up\|rm\|restart\)')" ]
    [[ "$output" == *'status=ok'* ]]
}

@test "install-certs.sh adopted: the key never appears on a command line" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    run_install_certs
    local key_b64; key_b64=$(base64 -w0 < "$FIXTURES/wildcard.key")
    [[ "$(lib_calls)" != *"$key_b64"* ]]
    [[ "$(docker_calls)" != *"$key_b64"* ]]
}

@test "install-certs.sh adopted: an unreachable Komodo retries 4 times, then exits 21 with nothing changed and a marker" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    echo 21 > "$T/ping.rc"
    echo "Komodo is not available (core)" > "$T/ping.err"
    local env_before; env_before=$(file_hash "$PROJECT_DIR/.env")
    run_install_certs
    [ "$status" -eq 21 ]
    [ "$(lib_count ping)" -eq 5 ]
    [ "$(file_hash "$PROJECT_DIR/.env")" = "$env_before" ]
    [ ! -e "$PROJECT_DIR/certs/api" ]
    [ -f "$PROJECT_DIR/.certs-pending" ]
    [ "$(stat -c %a "$PROJECT_DIR/.certs-pending")" = "600" ]
    [ "$(sed -n 1p "$PROJECT_DIR/.certs-pending")" = "$FIXTURES/wildcard.pem" ]
    [ "$(sed -n 2p "$PROJECT_DIR/.certs-pending")" = "$FIXTURES/wildcard.key" ]
    [[ "$output" == *"VOIPBIN_CERTS: status=error reason=komodo-unavailable retry=\"sudo ./scripts/install-certs.sh $FIXTURES/wildcard.pem $FIXTURES/wildcard.key\""* ]]
}

@test "install-certs.sh adopted: a rejected key is not retried" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    echo 21 > "$T/ping.rc"
    echo "Komodo is not available (auth)" > "$T/ping.err"
    run_install_certs
    [ "$status" -eq 21 ]
    [ "$(lib_count ping)" -eq 1 ]
    [ -f "$PROJECT_DIR/.certs-pending" ]
}

@test "install-certs.sh broken: no retry, exit 21, marker left" {
    prepare_certs_run
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    echo 21 > "$T/ping.rc"
    echo "Komodo state is incomplete. Run: sudo ./voipbin komodo setup" > "$T/ping.err"
    run_install_certs
    [ "$status" -eq 21 ]
    [ "$(lib_count ping)" -eq 1 ]
    [ ! -e "$PROJECT_DIR/certs/api" ]
    [ -f "$PROJECT_DIR/.certs-pending" ]
}

@test "install-certs.sh adopted: a Komodo that comes back during the retries finishes the install and clears the marker" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    echo "x" > "$PROJECT_DIR/.certs-pending"
    printf '21\n21\n0\n' > "$T/ping.seq"
    echo "Komodo is not available (periphery)" > "$T/ping.err"
    run_install_certs
    [ "$status" -eq 0 ]
    [ "$(lib_count ping)" -eq 3 ]
    [ -f "$PROJECT_DIR/certs/api/cert.pem" ]
    [ ! -e "$PROJECT_DIR/.certs-pending" ]
}

@test "install-certs.sh adopted: the default wait between tries is 15 seconds, only the test variable shortens it" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    echo 21 > "$T/ping.rc"
    echo "Komodo is not available (core)" > "$T/ping.err"
    mock_command_script sleep 'echo "$1" >> "$T/sleep.log"; exit 0'
    run env -u VOIPBIN_KOMODO_RETRY_INTERVAL bash "$SCRIPTS_DIR/install-certs.sh" --domain example.com "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"
    [ "$status" -eq 21 ]
    [ "$(cat "$T/sleep.log")" = "15
15
15
15" ]
}

@test "install-certs.sh adopted: a failed write after the files were installed leaves the marker" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    echo 21 > "$T/var-set.rc"
    run_install_certs
    [ "$status" -eq 21 ]
    [ -f "$PROJECT_DIR/certs/api/cert.pem" ]
    [ -f "$PROJECT_DIR/.certs-pending" ]
    [ "$(lib_count exec)" -eq 0 ]
    [[ "$output" == *"status=error reason=komodo-unavailable "* ]]
}

@test "install-certs.sh adopted: a failed recreate leaves the marker and keeps its exit code" {
    prepare_certs_run
    mark_adopted
    make_copy "A=1"
    echo 16 > "$T/exec.rc"
    run_install_certs
    [ "$status" -eq 16 ]
    [ -f "$PROJECT_DIR/.certs-pending" ]
    # not a Komodo outage: the result line says so
    [[ "$output" == *"status=error reason=komodo-write-failed "* ]]
}

make_long_chain() {
    make_wildcard_cert
    local i
    cp "$FIXTURES/wildcard.pem" "$FIXTURES/long.pem"
    for i in 1 2 3 4 5 6 7 8; do cat "$FIXTURES/wildcard.pem" >> "$FIXTURES/long.pem"; done
}

@test "install-certs.sh adopted: a value over 10000 characters is refused with 2 before anything is touched" {
    prepare_certs_run
    make_long_chain
    mark_adopted
    make_copy "A=1"
    run bash "$SCRIPTS_DIR/install-certs.sh" --domain example.com "$FIXTURES/long.pem" "$FIXTURES/wildcard.key"
    [ "$status" -eq 2 ]
    [[ "$output" == *'API_SSL_CERT_BASE64'* ]]
    [ ! -e "$T/lib.log" ]
    [ ! -e "$PROJECT_DIR/certs/api" ]
    [ ! -e "$PROJECT_DIR/.certs-pending" ]
}

@test "install-certs.sh adopted: the size check comes before --check-only returns, the Komodo check does not" {
    prepare_certs_run
    make_long_chain
    mark_adopted
    make_copy "A=1"
    echo 21 > "$T/ping.rc"
    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com "$FIXTURES/long.pem" "$FIXTURES/wildcard.key"
    [ "$status" -eq 2 ]
    run bash "$SCRIPTS_DIR/install-certs.sh" --check-only --domain example.com "$FIXTURES/wildcard.pem" "$FIXTURES/wildcard.key"
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
}

@test "install-certs.sh none: the historic path, no size check, no Komodo call, no marker" {
    prepare_certs_run
    make_long_chain
    run bash "$SCRIPTS_DIR/install-certs.sh" --domain example.com "$FIXTURES/long.pem" "$FIXTURES/wildcard.key"
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
    [ ! -e "$PROJECT_DIR/.certs-pending" ]
    local cert_b64; cert_b64=$(base64 -w0 < "$FIXTURES/long.pem")
    [[ "$(cat "$PROJECT_DIR/.env")" == *"API_SSL_CERT_BASE64=$cert_b64"* ]]
    [ "$(docker_calls | grep '^compose \(rm\|up\|restart\)')" = "compose rm -sf api-manager hook-manager
compose up -d api-manager hook-manager
compose restart kamailio
compose restart web-proxy" ]
}

@test "install-certs.sh source: the retry constants are the documented ones" {
    grep -q '^KOMODO_PRECHECK_RETRIES=4$' "$SCRIPTS_DIR/install-certs.sh"
    grep -q '^KOMODO_PRECHECK_INTERVAL_DEFAULT=15$' "$SCRIPTS_DIR/install-certs.sh"
    [ "$(grep -c 'VOIPBIN_KOMODO_RETRY_INTERVAL' "$SCRIPTS_DIR/install-certs.sh")" -eq 2 ]
    grep -q 'Exit codes: 0 success; 1 validation/user error; 2 environment error.*21 Komodo unavailable' <(tr '\n' ' ' < "$SCRIPTS_DIR/install-certs.sh" | sed 's/# / /g;s/  */ /g')
}

# -----------------------------------------------------------------------------
# clean.sh
# -----------------------------------------------------------------------------

prepare_clean_run() {
    printf 'abc123\n' > "$T/compose-ps.out"
    printf 'A=1\n' > "$PROJECT_DIR/.env"
}

@test "clean.sh none: the same compose calls as before, no Komodo call" {
    prepare_clean_run
    run bash "$SCRIPTS_DIR/clean.sh" --images
    [ "$status" -eq 0 ]
    [ "$(docker_calls | grep '^compose \(down\|config\)')" = "compose down
compose down --rmi all" ]
    [ ! -e "$T/lib.log" ]
    [[ "$output" != *'.env.komodo'* ]]
}

@test "clean.sh none --volumes: docker compose down -v directly" {
    prepare_clean_run
    run bash "$SCRIPTS_DIR/clean.sh" --volumes
    [ "$status" -eq 0 ]
    [ "$(docker_calls | grep '^compose down')" = "compose down -v" ]
    [ ! -e "$T/lib.log" ]
}

@test "clean.sh adopted without options: the stack is removed through Komodo, not directly" {
    prepare_clean_run
    mark_adopted
    make_copy "A=1"
    run bash "$SCRIPTS_DIR/clean.sh" --dns
    [ "$status" -eq 0 ]
    [ "$(lib_calls)" = "exec down" ]
    [ -z "$(docker_calls | grep '^compose down')" ]
}

@test "clean.sh adopted --volumes: the direct down -v names both environment files" {
    prepare_clean_run
    mark_adopted
    make_copy "A=1"
    run bash "$SCRIPTS_DIR/clean.sh" --volumes
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
    grep -qx '\.env,\.env\.komodo|compose down -v' "$T/docker.env.log"
}

@test "clean.sh adopted: an unreachable Komodo falls back to a direct down with the environment files" {
    prepare_clean_run
    mark_adopted
    make_copy "A=1"
    echo 21 > "$T/exec.rc"
    run bash "$SCRIPTS_DIR/clean.sh" --dns
    [ "$status" -eq 0 ]
    [[ "$output" == *'removing the containers directly'* ]]
    grep -qx '\.env,\.env\.komodo|compose down' "$T/docker.env.log"
}

@test "clean.sh adopted: any other bridge failure is not hidden by a direct down" {
    prepare_clean_run
    mark_adopted
    make_copy "A=1"
    echo 1 > "$T/exec.rc"
    run bash "$SCRIPTS_DIR/clean.sh" --dns
    [ "$status" -eq 1 ]
    [ -z "$(docker_calls | grep '^compose down')" ]
}

@test "clean.sh adopted --images: the leftover cleanup is direct and sees the environment files" {
    prepare_clean_run
    mark_adopted
    make_copy "A=1"
    : > "$PROJECT_DIR/docker-compose.yml"
    run bash "$SCRIPTS_DIR/clean.sh" --images
    [ "$status" -eq 0 ]
    grep -qx '\.env,\.env\.komodo|compose config --images' "$T/docker.env.log"
    grep -qx '\.env,\.env\.komodo|compose down --rmi all' "$T/docker.env.log"
}

prepare_purge_stubs() {
    export VOIPBIN_KOMODO_ROOT="$T/root"
    export VOIPBIN_PROJECT_DIR="$PROJECT_DIR"
    mkdir -p "$T/root/usr/local/bin" "$T/root/etc/systemd/system" "$T/root/etc/komodo" "$T/root/usr/local/lib/voipbin" "$T/root/run/systemd/system"
    : > "$T/root/usr/local/bin/periphery"
    : > "$T/root/etc/systemd/system/periphery.service"
    : > "$T/root/etc/komodo/periphery.config.toml"
    : > "$T/root/usr/local/lib/voipbin/komodo-hook.sh"
    mock_command_script id 'if [[ "$1" == "-u" ]]; then echo 0; exit 0; fi; exec /usr/bin/id "$@"'
    mock_command_script chown 'exit 0'
    mock_command_script systemctl 'echo "systemctl $*" >> "$T/systemctl.log"; exit 0'
    mock_command_script sudo 'if [[ "$1" == "-n" ]]; then exit 0; fi; exec "$@"'
    mkdir -p "$PROJECT_DIR/komodo"
    cp "$PROJECT_ROOT/komodo/docker-compose.yml" "$PROJECT_DIR/komodo/docker-compose.yml"
    cp "$PROJECT_ROOT/komodo/versions.env" "$PROJECT_DIR/komodo/versions.env"
    : > "$PROJECT_DIR/komodo/.env"
    : > "$PROJECT_DIR/komodo/admin-password"
    : > "$PROJECT_DIR/komodo/cli-key"
    : > "$PROJECT_DIR/komodo/.adopted"
    : > "$PROJECT_DIR/.env.komodo"
    : > "$PROJECT_DIR/.certs-pending"
    : > "$PROJECT_DIR/.certs-sync-pending"
}

@test "clean.sh adopted --purge: .env, the copy and the markers go, Komodo is torn down, the warning is shown" {
    if [[ $EUID -eq 0 ]]; then skip "the sudo path needs an unprivileged user"; fi
    prepare_clean_run
    prepare_purge_stubs
    run bash "$SCRIPTS_DIR/clean.sh" --purge
    [ "$status" -eq 0 ]
    [[ "$output" == *'--purge also removes .env.komodo and the Komodo variables'* ]]
    [[ "$output" == *'Without --volumes the database volume stays, but its password and keys are gone'* ]]
    [ ! -e "$PROJECT_DIR/.env" ]
    [ ! -e "$PROJECT_DIR/.env.komodo" ]
    [ ! -e "$PROJECT_DIR/.certs-pending" ]
    [ ! -e "$PROJECT_DIR/.certs-sync-pending" ]
    [ ! -e "$PROJECT_DIR/komodo/.adopted" ]
    [ ! -e "$PROJECT_DIR/komodo/cli-key" ]
    [ ! -e "$PROJECT_DIR/komodo/.env" ]
    [ ! -e "$PROJECT_DIR/komodo/admin-password" ]
    [ -f "$PROJECT_DIR/komodo/docker-compose.yml" ]
    [ -f "$PROJECT_DIR/komodo/versions.env" ]
    [ ! -e "$T/root/usr/local/bin/periphery" ]
    [ ! -e "$T/root/etc/systemd/system/periphery.service" ]
    [ ! -e "$T/root/usr/local/lib/voipbin/komodo-hook.sh" ]
}

@test "clean.sh none --purge: no Komodo warning, no teardown" {
    prepare_clean_run
    run bash "$SCRIPTS_DIR/clean.sh" --purge
    [ "$status" -eq 0 ]
    [[ "$output" != *'Komodo'* ]]
    [ ! -e "$T/lib.log" ]
}

@test "clean.sh adopted --purge without root or sudo: the rest is purged and the teardown command is named" {
    if [[ $EUID -eq 0 ]]; then skip "the sudo path needs an unprivileged user"; fi
    prepare_clean_run
    prepare_purge_stubs
    mock_command_script sudo 'exit 1'
    run bash "$SCRIPTS_DIR/clean.sh" --purge
    [ "$status" -eq 0 ]
    [[ "$output" == *'sudo ./scripts/komodo-setup.sh --teardown'* ]]
    [ ! -e "$PROJECT_DIR/.env.komodo" ]
    [ -e "$PROJECT_DIR/komodo/cli-key" ]
}

# -----------------------------------------------------------------------------
# komodo-setup.sh --teardown
# -----------------------------------------------------------------------------

run_teardown() {
    run bash "$SCRIPTS_DIR/komodo-setup.sh" --teardown
}

@test "komodo-setup.sh --teardown: removes the service, files, hook, containers and secrets, keeps the copy" {
    prepare_purge_stubs
    run_teardown
    [ "$status" -eq 0 ]
    [[ "$output" == *'VOIPBIN_KOMODO: status=ok teardown=done'* ]]
    [[ "$(cat "$T/systemctl.log")" == *"systemctl stop periphery"* ]]
    [[ "$(cat "$T/systemctl.log")" == *"systemctl disable periphery"* ]]
    [[ "$(cat "$T/systemctl.log")" == *"systemctl daemon-reload"* ]]
    [ ! -e "$T/root/usr/local/bin/periphery" ]
    [ ! -e "$T/root/etc/systemd/system/periphery.service" ]
    [ ! -e "$T/root/etc/komodo/periphery.config.toml" ]
    [ ! -e "$T/root/usr/local/lib/voipbin/komodo-hook.sh" ]
    [ ! -e "$PROJECT_DIR/komodo/.env" ]
    [ ! -e "$PROJECT_DIR/komodo/admin-password" ]
    [ ! -e "$PROJECT_DIR/komodo/cli-key" ]
    [ ! -e "$PROJECT_DIR/komodo/.adopted" ]
    [ -f "$PROJECT_DIR/komodo/docker-compose.yml" ]
    [ -f "$PROJECT_DIR/komodo/versions.env" ]
    [ -e "$PROJECT_DIR/.env.komodo" ]
}

@test "komodo-setup.sh --teardown: only the komodo compose project is brought down, with its volume" {
    prepare_purge_stubs
    run_teardown
    [ "$status" -eq 0 ]
    local down; down=$(docker_calls | grep 'down')
    [[ "$down" == "compose -p komodo -f $PROJECT_DIR/komodo/docker-compose.yml "*" down -v" ]]
    [ "$(docker_calls | grep -c 'down')" -eq 1 ]
    grep -q '^volume rm komodo_mongo-data$' "$T/docker.log"
}

@test "komodo-setup.sh --teardown: repeating it on a torn down host is safe" {
    prepare_purge_stubs
    run_teardown
    [ "$status" -eq 0 ]
    run_teardown
    [ "$status" -eq 0 ]
    [[ "$output" == *'status=ok teardown=done'* ]]
}

@test "komodo-setup.sh --teardown: refuses to run without root" {
    prepare_purge_stubs
    mock_command_script id 'if [[ "$1" == "-u" ]]; then echo 1000; exit 0; fi; exec /usr/bin/id "$@"'
    run_teardown
    [ "$status" -eq 1 ]
    [[ "$output" == *'status=error'* ]]
    [ -e "$PROJECT_DIR/komodo/cli-key" ]
    [ ! -e "$T/systemctl.log" ]
}

@test "komodo-setup.sh --teardown: stays inside the test prefix (no real system path is touched)" {
    prepare_purge_stubs
    run_teardown
    [ "$status" -eq 0 ]
    # every rm / rmdir target in the teardown step is a prefixed or komodo-directory path
    local line
    while IFS= read -r line; do
        [[ "$line" == *'sys_path'* || "$line" == *'$unit'* || "$line" == *'$bin'* \
            || "$line" == *'$cfg'* || "$line" == *'$hook'* || "$line" == *'KOMODO_DIR'* ]] || { echo "unprefixed: $line"; false; }
    done < <(sed -n '/^step_teardown()/,/^}/p' "$SCRIPTS_DIR/komodo-setup.sh" | grep -E '^[[:space:]]*(rm|rmdir) ')
}
