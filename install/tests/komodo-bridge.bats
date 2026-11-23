#!/usr/bin/env bats
# Tests for the Komodo bridge in scripts/common.sh: install mode, compose
# entry points, host .env writes, rendered-copy reads, guidance text, and the
# entry points that only changed their compose calls (stop.sh, init.sh).
#
# Isolation: docker is a recording stub, komodo_lib.py is replaced by a
# recording fake (VOIPBIN_KOMODO_LIB), PROJECT_DIR is a temp directory.
# Nothing touches a real Komodo, docker daemon, or network.

bats_require_minimum_version 1.5.0

load 'test_helper'

setup() {
    setup_test_env
    export T="$TEST_TEMP_DIR"
    export NO_COLOR=1
    unset COMPOSE_ENV_FILES
    export VOIPBIN_KOMODO_LIB="$T/fake_lib.py"
    write_fake_lib
    write_fake_docker
    source "$SCRIPTS_DIR/common.sh"
}

teardown() {
    chmod -R u+rwx "$T" 2>/dev/null || true
    teardown_test_env
}

# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------

# Records every call; the exit code of a sub command comes from $T/<sub>.rc,
# its stderr text from $T/<sub>.err. var-set input is appended to
# $T/stdin.log.
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
rc_file = t + "/" + sub + ".rc"
sys.exit(int(open(rc_file).read().strip()) if os.path.exists(rc_file) else 0)
PY
}

write_fake_docker() {
    mock_command_script docker 'echo "$*" >> "$T/docker.log"
if [[ -f "$T/docker.rc" ]]; then exit "$(cat "$T/docker.rc")"; fi
exit 0'
}

mark_adopted() {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/.adopted"
}

make_copy() {
    printf '%s\n' "$@" > "$PROJECT_DIR/.env.komodo"
}

lib_calls() { [[ -f "$T/lib.log" ]] && cat "$T/lib.log" || true; }
docker_calls() { [[ -f "$T/docker.log" ]] && cat "$T/docker.log" || true; }

# -----------------------------------------------------------------------------
# komodo_mode and its equivalence with komodo_lib.py mode
# -----------------------------------------------------------------------------

build_state() {
    local d="$1" name="$2"
    mkdir -p "$d"
    case "$name" in
        none) ;;
        marker) mkdir -p "$d/komodo"; : > "$d/komodo/.adopted" ;;
        copy-only) : > "$d/.env.komodo" ;;
        komodo-env-only) mkdir -p "$d/komodo"; : > "$d/komodo/.env" ;;
        cli-key-only) mkdir -p "$d/komodo"; : > "$d/komodo/cli-key" ;;
        marker-and-copy) mkdir -p "$d/komodo"; : > "$d/komodo/.adopted"; : > "$d/.env.komodo" ;;
        dangling-marker) mkdir -p "$d/komodo"; ln -s /nonexistent "$d/komodo/.adopted" ;;
        dangling-copy) ln -s /nonexistent "$d/.env.komodo" ;;
        komodo-is-a-file) : > "$d/komodo" ;;
        komodo-dir-only) mkdir -p "$d/komodo"; : > "$d/komodo/docker-compose.yml" ;;
        unsearchable-komodo) mkdir -p "$d/komodo"; : > "$d/komodo/.adopted"; chmod 000 "$d/komodo" ;;
    esac
}

@test "komodo_mode: none, adopted and broken by file existence only" {
    [[ "$(komodo_mode)" == "none" ]]
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    [[ "$(komodo_mode)" == "broken" ]]
    : > "$PROJECT_DIR/komodo/.adopted"
    [[ "$(komodo_mode)" == "adopted" ]]
}

@test "komodo_mode: none makes no docker, python or network call" {
    run komodo_mode
    [ "$output" = "none" ]
    [ ! -e "$T/docker.log" ]
    [ ! -e "$T/lib.log" ]
}

@test "komodo_mode equals komodo_lib.py mode for every state" {
    local name d bash_mode py_mode
    for name in none marker copy-only komodo-env-only cli-key-only marker-and-copy \
                dangling-marker dangling-copy komodo-is-a-file komodo-dir-only unsearchable-komodo; do
        if [[ "$name" == unsearchable-komodo && $EUID -eq 0 ]]; then continue; fi
        d="$T/state-$name"
        build_state "$d" "$name"
        bash_mode=$(PROJECT_DIR="$d" komodo_mode)
        py_mode=$(VOIPBIN_PROJECT_DIR="$d" python3 "$SCRIPTS_DIR/komodo_lib.py" mode)
        chmod -R u+rwx "$d" 2>/dev/null || true
        if [[ "$bash_mode" != "$py_mode" ]]; then
            echo "state $name: bash=$bash_mode python=$py_mode"
            false
        fi
    done
}

@test "komodo_mode uses file signals only: a docker container does not change it" {
    mock_command_script docker 'echo "voipbin-db"; exit 0'
    [[ "$(komodo_mode)" == "none" ]]
}

# -----------------------------------------------------------------------------
# compose_env_setup
# -----------------------------------------------------------------------------

@test "compose_env_setup: nothing is exported without Komodo files" {
    compose_env_setup
    [ -z "${COMPOSE_ENV_FILES+x}" ]
}

@test "compose_env_setup: a readable rendered copy adds it to the compose input" {
    make_copy "A=1"
    compose_env_setup
    [ "$COMPOSE_ENV_FILES" = ".env,.env.komodo" ]
}

@test "compose_env_setup: a marker without a rendered copy is an error" {
    mark_adopted
    run compose_env_setup
    [ "$status" -eq 21 ]
    [[ "$output" == *'komodo setup'* ]]
}

@test "compose_env_setup: an unreadable rendered copy is an error" {
    if [[ $EUID -eq 0 ]]; then skip "root can read everything"; fi
    make_copy "A=1"
    chmod 000 "$PROJECT_DIR/.env.komodo"
    run compose_env_setup
    [ "$status" -eq 21 ]
    [[ "$output" == *'cannot be read'* ]]
}

@test "compose_env_setup --readonly: returns codes and prints nothing" {
    if [[ $EUID -eq 0 ]]; then skip "root can read everything"; fi
    mark_adopted
    run compose_env_setup --readonly
    [ "$status" -eq 2 ]
    [ -z "$output" ]
    make_copy "A=1"
    chmod 000 "$PROJECT_DIR/.env.komodo"
    run compose_env_setup --readonly
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    chmod 600 "$PROJECT_DIR/.env.komodo"
    run compose_env_setup --readonly
    [ "$status" -eq 0 ]
}

# -----------------------------------------------------------------------------
# compose_exec
# -----------------------------------------------------------------------------

@test "compose_exec none: the arguments reach docker compose unchanged, Komodo is not called" {
    compose_exec up -d db redis rabbitmq
    compose_exec rm -sf api-manager hook-manager
    compose_exec down -v
    [ "$(docker_calls)" = "compose up -d db redis rabbitmq
compose rm -sf api-manager hook-manager
compose down -v" ]
    [ ! -e "$T/lib.log" ]
}

@test "compose_exec none: the exit code of docker compose is returned" {
    echo 7 > "$T/docker.rc"
    run compose_exec stop
    [ "$status" -eq 7 ]
}

@test "compose_exec adopted: service names only go to komodo_lib.py exec, docker is not called" {
    mark_adopted
    compose_exec up -d db redis rabbitmq
    compose_exec rm -sf api-manager hook-manager
    compose_exec stop
    [ "$(lib_calls)" = "exec up db redis rabbitmq
exec rm api-manager hook-manager
exec stop" ]
    [ ! -e "$T/docker.log" ]
}

@test "compose_exec adopted: the exit code of the bridge is returned" {
    mark_adopted
    echo 16 > "$T/exec.rc"
    run compose_exec up -d
    [ "$status" -eq 16 ]
    echo 21 > "$T/exec.rc"
    run compose_exec restart kamailio
    [ "$status" -eq 21 ]
}

@test "compose_exec adopted: the project directory reaches komodo_lib.py" {
    mark_adopted
    cat > "$T/fake_lib.py" <<'PY'
import os, sys
open(os.environ["T"] + "/pd.log", "w").write(os.environ.get("VOIPBIN_PROJECT_DIR", ""))
PY
    compose_exec up -d
    [ "$(cat "$T/pd.log")" = "$PROJECT_DIR" ]
}

@test "compose_exec broken: refused with 21, neither docker nor Komodo is called" {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    run compose_exec up -d
    [ "$status" -eq 21 ]
    [[ "$output" == *'sudo ./voipbin komodo setup'* ]]
    [ ! -e "$T/docker.log" ]
    [ ! -e "$T/lib.log" ]
}

# -----------------------------------------------------------------------------
# compose_ping
# -----------------------------------------------------------------------------

@test "compose_ping none: nothing is called" {
    run compose_ping
    [ "$status" -eq 0 ]
    [ ! -e "$T/lib.log" ]
}

@test "compose_ping adopted: runs komodo_lib.py ping and keeps stdout empty" {
    mark_adopted
    echo "reachable" > "$T/ping.err"
    run --separate-stderr compose_ping
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ "$(lib_calls)" = "ping" ]
}

@test "compose_ping adopted: the failure code and message come through" {
    mark_adopted
    echo 21 > "$T/ping.rc"
    echo "Komodo is not available (core)" > "$T/ping.err"
    run compose_ping
    [ "$status" -eq 21 ]
    [[ "$output" == *'not available (core)'* ]]
}

# -----------------------------------------------------------------------------
# host_env_write
# -----------------------------------------------------------------------------

@test "host_env_write none: replaces the line like the historic sed and leaves the rest alone" {
    printf 'A=1\nHOST_EXTERNAL_IP=old\nB=2\n' > "$PROJECT_DIR/.env"
    host_env_write "HOST_EXTERNAL_IP=203.0.113.5" "API_URL=https://x/"
    [ "$(cat "$PROJECT_DIR/.env")" = "A=1
HOST_EXTERNAL_IP=203.0.113.5
B=2" ]
    [ ! -e "$T/lib.log" ]
}

@test "host_env_write none: a key that is absent stays absent, a failing sed is swallowed" {
    printf 'A=1\n' > "$PROJECT_DIR/.env"
    run host_env_write "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$status" -eq 0 ]
    [ "$(cat "$PROJECT_DIR/.env")" = "A=1" ]
    rm -f "$PROJECT_DIR/.env"
    run host_env_write "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$status" -eq 0 ]
    # also under set -e: the caller goes on, as it did with the bare sed
    run bash -c 'set -e; source "$1"; PROJECT_DIR="$2"; host_env_write "HOST_EXTERNAL_IP=203.0.113.5"; echo after' _ "$SCRIPTS_DIR/common.sh" "$PROJECT_DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *after ]]
}

@test "host_env_write adopted: values go to var-set on stdin, never on a command line" {
    mark_adopted
    printf 'A=1\n' > "$PROJECT_DIR/.env"
    host_env_write "API_SSL_CERT_BASE64=SECRETVALUE123" "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$(lib_calls)" = "var-set" ]
    [ "$(cat "$T/stdin.log")" = "API_SSL_CERT_BASE64=SECRETVALUE123
HOST_EXTERNAL_IP=203.0.113.5" ]
    # .env is left alone
    [ "$(cat "$PROJECT_DIR/.env")" = "A=1" ]
}

@test "host_env_write adopted: the exit code of var-set is returned" {
    mark_adopted
    echo 21 > "$T/var-set.rc"
    run host_env_write "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$status" -eq 21 ]
    echo 2 > "$T/var-set.rc"
    run host_env_write "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$status" -eq 2 ]
}

@test "host_env_write adopted: nothing appears on stdout" {
    mark_adopted
    echo "VOIPBIN_ENV_WRITE: status=ok written=HOST_EXTERNAL_IP unchanged=" > "$T/var-set.err"
    run --separate-stderr host_env_write "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "host_env_write broken: refused with 21, nothing is written or called" {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    printf 'HOST_EXTERNAL_IP=old\n' > "$PROJECT_DIR/.env"
    run host_env_write "HOST_EXTERNAL_IP=203.0.113.5"
    [ "$status" -eq 21 ]
    [ "$(cat "$PROJECT_DIR/.env")" = "HOST_EXTERNAL_IP=old" ]
    [ ! -e "$T/lib.log" ]
}

# -----------------------------------------------------------------------------
# get_env_var with a rendered copy
# -----------------------------------------------------------------------------

@test "get_env_var: without a copy it reads .env as before (last match wins)" {
    printf 'A=1\nA=2\n' > "$PROJECT_DIR/.env"
    [ "$(get_env_var "$PROJECT_DIR/.env" A)" = "2" ]
}

@test "get_env_var: with a copy the later file wins, keys in either file are found" {
    printf 'A=env\nONLY_ENV=e\n' > "$PROJECT_DIR/.env"
    make_copy "A=copy" "ONLY_COPY=c"
    [ "$(get_env_var "$PROJECT_DIR/.env" A)" = "copy" ]
    [ "$(get_env_var "$PROJECT_DIR/.env" ONLY_ENV)" = "e" ]
    [ "$(get_env_var "$PROJECT_DIR/.env" ONLY_COPY)" = "c" ]
}

@test "get_env_var: a .env without a trailing newline does not swallow the copy's first key" {
    printf 'A=env' > "$PROJECT_DIR/.env"
    make_copy "B=copy"
    [ "$(get_env_var "$PROJECT_DIR/.env" B)" = "copy" ]
    [ "$(get_env_var "$PROJECT_DIR/.env" A)" = "env" ]
}

@test "get_env_var: only a file named .env pulls in the copy" {
    printf 'A=other\n' > "$PROJECT_DIR/other.env"
    make_copy "A=copy"
    [ "$(get_env_var "$PROJECT_DIR/other.env" A)" = "other" ]
}

@test "get_env_var: an unreadable copy is ignored and .env still answers" {
    if [[ $EUID -eq 0 ]]; then skip "root can read everything"; fi
    printf 'A=env\n' > "$PROJECT_DIR/.env"
    make_copy "A=copy"
    chmod 000 "$PROJECT_DIR/.env.komodo"
    [ "$(get_env_var "$PROJECT_DIR/.env" A)" = "env" ]
}

@test "get_configured_host_ip: none keeps the first-match read, a copy is taken into account otherwise" {
    printf 'HOST_EXTERNAL_IP=1.1.1.1\nHOST_EXTERNAL_IP=2.2.2.2\n' > "$PROJECT_DIR/.env"
    [ "$(get_configured_host_ip)" = "1.1.1.1" ]
    make_copy "HOST_EXTERNAL_IP=3.3.3.3"
    [ "$(get_configured_host_ip)" = "3.3.3.3" ]
}

@test "host_env_read: none is the anchored first-match grep" {
    printf 'KAMAILIO_EXTERNAL_IP=1.1.1.1\n#KAMAILIO_EXTERNAL_IP=9.9.9.9\nX_KAMAILIO_EXTERNAL_IP=8.8.8.8\n' > "$PROJECT_DIR/.env"
    [ "$(host_env_read KAMAILIO_EXTERNAL_IP)" = "1.1.1.1" ]
}

# -----------------------------------------------------------------------------
# compose_hint (guidance text)
# -----------------------------------------------------------------------------

@test "compose_hint none: the plain docker compose command line, unchanged" {
    [ "$(compose_hint up -d db)" = "docker compose up -d db" ]
    [ "$(compose_hint rm -fsv registrar-manager)" = "docker compose rm -fsv registrar-manager" ]
    [ "$(compose_hint down)" = "docker compose down" ]
    [ "$(compose_hint restart '<service>')" = "docker compose restart <service>" ]
}

@test "compose_hint with Komodo: a CLI command, never a bare docker compose" {
    mark_adopted
    [ "$(compose_hint up -d db)" = "voipbin start db" ]
    [ "$(compose_hint up -d)" = "voipbin start" ]
    [ "$(compose_hint restart kamailio rtpengine)" = "voipbin restart kamailio rtpengine" ]
    [ "$(compose_hint down)" = "voipbin stop" ]
    [ "$(compose_hint rm -fsv registrar-manager)" = "voipbin start registrar-manager" ]
}

@test "compose_exec with Komodo: value-less flags are dropped, an unknown option is refused with 1 and nothing runs" {
    mark_adopted
    run compose_exec rm -fsv web-proxy
    [ "$status" -eq 0 ]
    [ "$(lib_calls | tail -1)" = "exec rm web-proxy" ]
    : > "$T/lib.log"
    run compose_exec stop -t 30 coredns
    [ "$status" -eq 1 ]
    [[ "$output" == *"option -t is not supported"* ]]
    [ ! -s "$T/lib.log" ]
}

@test "realm_recreate_hint: none keeps the two historic steps, Komodo prints one start" {
    [ "$(realm_recreate_hint)" = "docker compose rm -fsv registrar-manager && docker compose up -d registrar-manager" ]
    mark_adopted
    [ "$(realm_recreate_hint)" = "voipbin start registrar-manager" ]
}

# -----------------------------------------------------------------------------
# stop.sh
# -----------------------------------------------------------------------------

@test "stop.sh none: runs docker compose stop with no other compose call" {
    run bash "$SCRIPTS_DIR/stop.sh"
    [ "$status" -eq 0 ]
    [ "$(docker_calls)" = "compose stop" ]
    [ ! -e "$T/lib.log" ]
}

@test "stop.sh adopted: stops through Komodo, docker compose is not called" {
    mark_adopted
    run bash "$SCRIPTS_DIR/stop.sh"
    [ "$status" -eq 0 ]
    [ "$(lib_calls)" = "exec stop" ]
    [ ! -e "$T/docker.log" ]
}

@test "stop.sh adopted: a Komodo failure is the exit code" {
    mark_adopted
    echo 21 > "$T/exec.rc"
    run bash "$SCRIPTS_DIR/stop.sh"
    [ "$status" -eq 21 ]
}

@test "stop.sh broken: refused with 21 and the setup hint" {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    run bash "$SCRIPTS_DIR/stop.sh"
    [ "$status" -eq 21 ]
    [[ "$output" == *'sudo ./voipbin komodo setup'* ]]
    [ ! -e "$T/docker.log" ]
}

# -----------------------------------------------------------------------------
# init.sh: a Komodo managed install is not re-initialized
# -----------------------------------------------------------------------------

@test "init.sh refuses --yes on an install with the Komodo marker (exit 2, .env unchanged)" {
    mark_adopted
    cp "$PROJECT_ROOT/.env.template" "$PROJECT_DIR/.env.template"
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test"
    local before; before=$(sha256sum "$PROJECT_DIR/.env")
    run bash "$SCRIPTS_DIR/init.sh" --yes
    [ "$status" -eq 2 ]
    [[ "$output" == *'managed by Komodo'* ]]
    [[ "$output" == *'sudo ./voipbin start'* ]]
    [ "$(sha256sum "$PROJECT_DIR/.env")" = "$before" ]
}

@test "init.sh refuses --force-reinit on an install with the Komodo marker (exit 2)" {
    mark_adopted
    cp "$PROJECT_ROOT/.env.template" "$PROJECT_DIR/.env.template"
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test"
    run bash "$SCRIPTS_DIR/init.sh" --force-reinit --mode internal --yes
    [ "$status" -eq 2 ]
    [[ "$output" == *'managed by Komodo'* ]]
}

@test "init.sh also refuses when the Komodo state is broken" {
    mkdir -p "$PROJECT_DIR/komodo"
    : > "$PROJECT_DIR/komodo/cli-key"
    cp "$PROJECT_ROOT/.env.template" "$PROJECT_DIR/.env.template"
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test"
    run bash "$SCRIPTS_DIR/init.sh" --yes
    [ "$status" -eq 2 ]
    [[ "$output" == *'managed by Komodo'* ]]
}

@test "init.sh none: an existing .env is not refused by the Komodo check" {
    cp "$PROJECT_ROOT/.env.template" "$PROJECT_DIR/.env.template"
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test"
    run bash "$SCRIPTS_DIR/init.sh" --yes
    [[ "$output" != *'managed by Komodo'* ]]
}

# -----------------------------------------------------------------------------
# Guidance text of the scripts that only print compose commands
# -----------------------------------------------------------------------------

@test "init_database.sh and migrate.sh print the CLI command with Komodo, docker compose without" {
    local f
    for f in init_database.sh migrate.sh; do
        grep -q 'compose_hint up -d' "$SCRIPTS_DIR/$f"
    done
    # no literal state-changing docker compose command is left in their strings
    if grep -nE '"[^"]*docker compose (up|rm|down|restart|stop|start)' "$SCRIPTS_DIR/init_database.sh" "$SCRIPTS_DIR/migrate.sh"; then
        false
    fi
}

@test "init_local.sh prepares the compose input at entry" {
    grep -q '^compose_env_setup || exit \$?$' "$SCRIPTS_DIR/init_local.sh"
    grep -q '^compose_env_setup || exit \$?$' "$SCRIPTS_DIR/init_database.sh" || \
        grep -q '    compose_env_setup || exit \$?$' "$SCRIPTS_DIR/init_database.sh"
}

@test "check-install.sh with a copy that cannot be used fails with a result line" {
    if [[ $EUID -eq 0 ]]; then skip "root can read everything"; fi
    mark_adopted
    create_env_file "DOMAIN_MODE=internal" "COMPOSE_PROFILES=internal-dns" "BASE_DOMAIN=voipbin.test"
    run bash "$SCRIPTS_DIR/check-install.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *'VOIPBIN_CHECK: status=fail'* ]]
    [[ "$output" == *'Komodo environment files unusable'* ]]
}
