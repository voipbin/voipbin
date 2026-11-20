#!/usr/bin/env bats
# Tests for scripts/komodo-setup.sh and the static Komodo assets
# (komodo/docker-compose.yml, komodo/versions.env, .gitignore entries).
#
# Isolation: docker, systemctl, curl, openssl, uname, ss, id and chown are
# stubbed, komodo_lib.py is replaced by a recording fake, and every system path
# (/usr, /etc, /run) is redirected under a temp prefix. Nothing touches the
# network, a real docker daemon, or real system files.

load 'test_helper'

REAL_KOMODO_ASSETS="$BATS_TEST_DIRNAME/../komodo"

setup() {
    setup_test_env
    export T="$TEST_TEMP_DIR"
    export INSTALL="$T/install"
    export ROOT="$T/root"
    mkdir -p "$INSTALL/komodo" "$ROOT/run/systemd/system" "$ROOT/usr/local/bin"
    cp "$REAL_KOMODO_ASSETS/docker-compose.yml" "$INSTALL/komodo/docker-compose.yml"

    # Fake release asset and a versions.env pinned to it
    printf 'fake-periphery-binary-v1' > "$T/asset"
    ASSET_SHA=$(sha256sum "$T/asset" | cut -d' ' -f1)
    sed -E "s/^KOMODO_PERIPHERY_SHA256=.*/KOMODO_PERIPHERY_SHA256=$ASSET_SHA/" \
        "$REAL_KOMODO_ASSETS/versions.env" > "$INSTALL/komodo/versions.env"

    # Defaults of the stubbed host: images present locally, nothing listening
    touch "$T/image_present"
    echo x86_64 > "$T/arch"
    echo 0 > "$T/uid"
    : > "$T/ss_listen"

    unset SUDO_USER
    export COMPOSE_ENV_FILES=".env,.env.komodo"
    export COMPOSE_PROJECT_NAME="mainstack"
    export NO_COLOR=1

    write_fake_lib
    write_stubs
}

teardown() {
    teardown_test_env
}

# -----------------------------------------------------------------------------
# Stubs
# -----------------------------------------------------------------------------

write_fake_lib() {
    cat > "$T/fake_lib.py" <<'PY'
import os, sys
t = os.environ["T"]
sub = sys.argv[1]
with open(t + "/lib.log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
if sub == "onboard":
    args = sys.argv[2:]
    key = args[args.index("--out-key") + 1]
    pub = args[args.index("--out-pubkey") + 1]
    open(key, "w").write("ONBOARDKEYSECRET\n")
    open(pub, "w").write("COREPUBKEYVALUE\n")
rc_file = t + "/" + sub + ".rc"
rc = int(open(rc_file).read().strip()) if os.path.exists(rc_file) else 0
if sub == "guard" and rc:
    print("fake guard: refused")
sys.exit(rc)
PY
}

write_stubs() {
    mock_command_script id '
if [[ "$1" == "-u" ]]; then cat "$T/uid"; exit 0; fi
exec /usr/bin/id "$@"'
    mock_command_script chown 'echo "chown $*" >> "$T/chown.log"; exit 0'
    mock_command_script uname '
if [[ "$1" == "-m" ]]; then cat "$T/arch"; exit 0; fi
exec /usr/bin/uname "$@"'
    mock_command_script openssl '
if [[ "$1" == "rand" && "$2" == "-hex" ]]; then
    n=$(cat "$T/rand.n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$T/rand.n"
    printf "%0*x\n" $(($3 * 2)) "$n"; exit 0
fi
exit 1'
    mock_command_script ss '
case "$1" in
  -Hltn) cat "$T/ss_listen" ;;
  -Htnp)
    if [[ -f "$T/connected" ]]; then
        echo "ESTAB 0 0 127.0.0.1:40000 127.0.0.1:9120 users:((\"periphery\",pid=7,fd=9))"
    fi ;;
esac
exit 0'
    mock_command_script systemctl '
echo "systemctl $*" >> "$T/systemctl.log"
case "$1" in
  is-active) [[ -f "$T/unit_active" ]]; exit $? ;;
  restart|start|enable)
    if [[ "$1" == "restart" || "$1" == "start" || "$*" == *"--now"* ]]; then
        touch "$T/unit_active"
        n=$(cat "$T/restart.n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$T/restart.n"
        cp "$ROOT/etc/komodo/periphery.config.toml" "$T/cfg.snapshot.$n" 2>/dev/null || true
        [[ -f "$T/no_connect" ]] || touch "$T/connected"
    fi ;;
esac
exit 0'
    mock_command_script curl '
echo "curl $*" >> "$T/curl.log"
out=""; prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && out="$a"
    prev="$a"
done
if [[ "$*" == *"/auth"* ]]; then [[ -f "$T/core_down" ]] && exit 22; exit 0; fi
if [[ "$*" == *"-fsSIL"* ]]; then [[ -f "$T/release_unreachable" ]] && exit 22; exit 0; fi
if [[ "$*" == *"periphery-x86_64"* && -n "$out" ]]; then
    [[ -f "$T/asset_bad" ]] && echo "tampered" > "$out" || cp "$T/asset" "$out"
    exit 0
fi
exit 0'
    mock_command_script docker '
echo "docker $* [COMPOSE_ENV_FILES=${COMPOSE_ENV_FILES-unset} COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME-unset}]" >> "$T/docker.log"
case "$1 $2" in
  "compose version") exit 0 ;;
  "volume inspect") [[ -f "$T/volume" ]]; exit $? ;;
  "image inspect") [[ -f "$T/image_present" ]]; exit $? ;;
  "manifest inspect") [[ -f "$T/registry_down" ]] && exit 1; exit 0 ;;
  "ps --filter") [[ -f "$T/core_running" ]] && echo voipbin-komodo-core; exit 0 ;;
esac
if [[ "$1" == "compose" && "$*" == *" up "* ]]; then touch "$T/volume"; exit 0; fi
exit 0'
}

run_setup() {
    run env VOIPBIN_PROJECT_DIR="$INSTALL" VOIPBIN_KOMODO_LIB="$T/fake_lib.py" \
        VOIPBIN_KOMODO_ROOT="$ROOT" VOIPBIN_KOMODO_WAIT_INTERVAL=0 \
        bash "$SCRIPTS_DIR/komodo-setup.sh"
}

last_line_is() {
    [[ "${lines[${#lines[@]}-1]}" == "$1"* ]]
}

# Snapshot of everything the script may change on the host, for "no change" checks
host_state() {
    {
        find "$INSTALL" "$ROOT" -mindepth 1 | sort
        find "$INSTALL" "$ROOT" -type f -print0 | sort -z | xargs -0 -r sha256sum
    }
}

# Writes the two files the steady state keeps: a hardened config and a unit
preinstall_connected_periphery() {
    mkdir -p "$ROOT/etc/komodo" "$ROOT/etc/systemd/system"
    cp "$T/asset" "$ROOT/usr/local/bin/periphery"
    chmod 755 "$ROOT/usr/local/bin/periphery"
}

# =============================================================================
# Happy path
# =============================================================================

@test "fresh install: ok result line, secrets 600, directory 700, no secret in output" {
    run_setup
    [ "$status" -eq 0 ]
    last_line_is "VOIPBIN_KOMODO: status=ok"

    [ "$(stat -c %a "$INSTALL/komodo")" = "700" ]
    [ "$(stat -c %a "$INSTALL/komodo/.env")" = "600" ]
    [ "$(stat -c %a "$INSTALL/komodo/admin-password")" = "600" ]
    for key in KOMODO_DATABASE_PASSWORD KOMODO_INIT_ADMIN_PASSWORD KOMODO_JWT_SECRET KOMODO_WEBHOOK_SECRET; do
        grep -q "^$key=." "$INSTALL/komodo/.env"
    done
    local pw
    pw=$(grep '^KOMODO_DATABASE_PASSWORD=' "$INSTALL/komodo/.env" | cut -d= -f2)
    [[ "$output" != *"$pw"* ]]
    pw=$(cat "$INSTALL/komodo/admin-password")
    [[ "$output" != *"$pw"* ]]
    # admin password in .env is the same value as the file
    grep -qx "KOMODO_INIT_ADMIN_PASSWORD=$pw" "$INSTALL/komodo/.env"
    # no leftover temp files
    [ -z "$(find "$INSTALL/komodo" "$ROOT/etc" "$ROOT/usr" -name '.*.??????' -type f 2>/dev/null)" ]
}

@test "fresh install: steps run in order guard, onboard, provision" {
    run_setup
    [ "$status" -eq 0 ]
    run grep -o '^[a-z]*' "$T/lib.log"
    [ "$output" = "$(printf 'guard\nonboard\nprovision')" ]
}

@test "fresh install: core and mongo come up through the komodo project with a clean compose environment" {
    run_setup
    [ "$status" -eq 0 ]
    run grep ' up ' "$T/docker.log"
    [ "$status" -eq 0 ]
    [[ "$output" == *"docker compose -p komodo -f $INSTALL/komodo/docker-compose.yml --env-file $INSTALL/komodo/versions.env --env-file $INSTALL/komodo/.env up -d"* ]]
    [[ "$output" == *"COMPOSE_ENV_FILES=unset"* ]]
    [[ "$output" == *"COMPOSE_PROJECT_NAME=unset"* ]]
}

@test "periphery: pinned binary installed 755, unit has Restart=on-failure and RestartSec=5, enabled" {
    run_setup
    [ "$status" -eq 0 ]
    cmp "$T/asset" "$ROOT/usr/local/bin/periphery"
    [ "$(stat -c %a "$ROOT/usr/local/bin/periphery")" = "755" ]
    local unit="$ROOT/etc/systemd/system/periphery.service"
    grep -qx 'Restart=on-failure' "$unit"
    grep -qx 'RestartSec=5' "$unit"
    grep -q 'ExecStart=.*/usr/local/bin/periphery --config-path /etc/komodo/periphery.config.toml' "$unit"
    grep -q 'enable.*periphery' "$T/systemctl.log"
    grep -q 'enable --now periphery\|enable periphery' "$T/systemctl.log"
}

@test "periphery config: 600, terminals disabled, core key pinned, onboarding key removed after connect" {
    run_setup
    [ "$status" -eq 0 ]
    local cfg="$ROOT/etc/komodo/periphery.config.toml"
    [ "$(stat -c %a "$cfg")" = "600" ]
    grep -qx 'disable_terminals = true' "$cfg"
    grep -qx 'disable_container_terminals = true' "$cfg"
    grep -qx 'core_address = "http://127.0.0.1:9120"' "$cfg"
    grep -q '^core_public_keys = "COREPUBKEYVALUE"' "$cfg"
    if grep -q 'onboarding_key' "$cfg"; then false; fi
    # the first start carried the one-time key, the last restart did not
    grep -q 'onboarding_key = "ONBOARDKEYSECRET"' "$T/cfg.snapshot.1"
    local last
    last=$(ls "$T"/cfg.snapshot.* | sort -V | tail -1)
    if grep -q 'onboarding_key' "$last"; then false; fi
    # no listening port is configured
    if grep -Eq '^(port|bind_ip)' "$cfg"; then false; fi
}

@test "secrets never appear on any command line or log" {
    run_setup
    [ "$status" -eq 0 ]
    local logs="$T/docker.log $T/systemctl.log $T/curl.log $T/lib.log $T/chown.log"
    if grep -q 'ONBOARDKEYSECRET\|COREPUBKEYVALUE' $logs; then false; fi
    local pw
    pw=$(grep '^KOMODO_DATABASE_PASSWORD=' "$INSTALL/komodo/.env" | cut -d= -f2)
    if grep -q "$pw" $logs; then false; fi
    [[ "$output" != *ONBOARDKEYSECRET* ]]
}

@test "hook: installed root-owned 755 with the repository script, first line is the shebang" {
    run_setup
    [ "$status" -eq 0 ]
    local dest="$ROOT/usr/local/lib/voipbin/komodo-hook.sh"
    cmp "$SCRIPTS_DIR/komodo-hook.sh" "$dest"
    [ "$(stat -c %a "$dest")" = "755" ]
    grep -q 'chown root:root.*komodo-hook' "$T/chown.log"
    [ "$(sed -n 1p "$dest")" = "#!/bin/bash" ]
    sed -n 2p "$dest" | grep -Eq '^# komodo-hook-format: [0-9]+$'
}

@test "hook: an older installed copy is replaced" {
    mkdir -p "$ROOT/usr/local/lib/voipbin"
    printf '#!/bin/bash\n# komodo-hook-format: 0\nexit 0\n' > "$ROOT/usr/local/lib/voipbin/komodo-hook.sh"
    chmod 755 "$ROOT/usr/local/lib/voipbin/komodo-hook.sh"
    run_setup
    [ "$status" -eq 0 ]
    cmp "$SCRIPTS_DIR/komodo-hook.sh" "$ROOT/usr/local/lib/voipbin/komodo-hook.sh"
    [[ "$output" == *"hook=installed"* ]]
}

@test "initial admin password is generated once and only its path is printed" {
    run_setup
    [ "$status" -eq 0 ]
    [[ "$output" == *"$INSTALL/komodo/admin-password"* ]]
}

@test "an existing admin-password is reused and never rewritten" {
    printf 'my-known-admin-pass\n' > "$INSTALL/komodo/admin-password"
    chmod 600 "$INSTALL/komodo/admin-password"
    run_setup
    [ "$status" -eq 0 ]
    [ "$(cat "$INSTALL/komodo/admin-password")" = "my-known-admin-pass" ]
    grep -qx 'KOMODO_INIT_ADMIN_PASSWORD=my-known-admin-pass' "$INSTALL/komodo/.env"
}

# =============================================================================
# Idempotent re-run
# =============================================================================

@test "re-run: secrets are not regenerated and nothing is reinstalled" {
    run_setup
    [ "$status" -eq 0 ]
    local env_sum pw_sum
    env_sum=$(sha256sum "$INSTALL/komodo/.env")
    pw_sum=$(sha256sum "$INSTALL/komodo/admin-password")
    : > "$T/lib.log"
    : > "$T/curl.log"
    rm -f "$T/restart.n"

    run_setup
    [ "$status" -eq 0 ]
    last_line_is "VOIPBIN_KOMODO: status=ok periphery=unchanged hook=unchanged"
    [ "$env_sum" = "$(sha256sum "$INSTALL/komodo/.env")" ]
    [ "$pw_sum" = "$(sha256sum "$INSTALL/komodo/admin-password")" ]
    # connected and hardened: no new onboarding key, no download, no restart
    if grep -q '^onboard' "$T/lib.log"; then false; fi
    grep -q '^provision' "$T/lib.log"
    if grep -q 'periphery-x86_64' "$T/curl.log"; then false; fi
    [ ! -f "$T/restart.n" ]
}

@test "re-run: an upstream style unit without RestartSec is rewritten and reloaded" {
    run_setup
    [ "$status" -eq 0 ]
    printf '[Service]\nRestart=on-failure\n' > "$ROOT/etc/systemd/system/periphery.service"
    : > "$T/systemctl.log"
    run_setup
    [ "$status" -eq 0 ]
    grep -qx 'RestartSec=5' "$ROOT/etc/systemd/system/periphery.service"
    grep -q 'systemctl daemon-reload' "$T/systemctl.log"
}

@test "re-run on a port held by the komodo core container passes the port check" {
    echo 'LISTEN 0 4096 127.0.0.1:9120 0.0.0.0:*' > "$T/ss_listen"
    touch "$T/core_running"
    run_setup
    [ "$status" -eq 0 ]
}

@test "a changed periphery config (terminals on) is repaired by re-registering" {
    run_setup
    [ "$status" -eq 0 ]
    sed -i 's/disable_terminals = true/disable_terminals = false/' "$ROOT/etc/komodo/periphery.config.toml"
    : > "$T/lib.log"
    run_setup
    [ "$status" -eq 0 ]
    grep -q '^onboard' "$T/lib.log"
    grep -qx 'disable_terminals = true' "$ROOT/etc/komodo/periphery.config.toml"
}

# =============================================================================
# Refusals that must change nothing
# =============================================================================

@test "non-root run is refused with exit 1 and no change" {
    echo 1000 > "$T/uid"
    local before; before=$(host_state)
    run_setup
    [ "$status" -eq 1 ]
    last_line_is "VOIPBIN_KOMODO: status=error"
    [ "$before" = "$(host_state)" ]
    [ ! -f "$T/lib.log" ]
    [ ! -f "$T/docker.log" ]
}

@test "guard refusal returns the guard exit code and changes nothing" {
    echo 2 > "$T/guard.rc"
    local before; before=$(host_state)
    run_setup
    [ "$status" -eq 2 ]
    last_line_is "VOIPBIN_KOMODO: status=error step=guard"
    [ "$before" = "$(host_state)" ]
    [ "$(cat "$T/lib.log")" = "guard" ]
    [ ! -f "$T/docker.log" ]
    [ ! -f "$T/systemctl.log" ]
}

@test "preflight: not x86_64 exits 2 with the reason and changes nothing" {
    echo aarch64 > "$T/arch"
    local before; before=$(host_state)
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"aarch64"* ]]
    last_line_is "VOIPBIN_KOMODO: status=error step=preflight"
    [ "$before" = "$(host_state)" ]
    if grep -q ' up ' "$T/docker.log" 2>/dev/null; then false; fi
    [ ! -f "$T/systemctl.log" ]
}

@test "preflight: port 9120 held by another process exits 2 and changes nothing" {
    echo 'LISTEN 0 128 0.0.0.0:9120 0.0.0.0:*' > "$T/ss_listen"
    local before; before=$(host_state)
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"9120"* ]]
    [ "$before" = "$(host_state)" ]
    if grep -q ' up ' "$T/docker.log" 2>/dev/null; then false; fi
}

@test "preflight: no systemd exits 2 and changes nothing" {
    rmdir "$ROOT/run/systemd/system"
    local before; before=$(host_state)
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"systemd"* ]]
    [ "$before" = "$(host_state)" ]
    [ ! -f "$T/systemctl.log" ]
}

@test "preflight: release assets unreachable exits 2 before any host change when a download is needed" {
    touch "$T/release_unreachable"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"release assets"* ]]
    [ ! -e "$ROOT/usr/local/bin/periphery" ]
    [ ! -e "$ROOT/etc/komodo/periphery.config.toml" ]
}

@test "preflight: registry unreachable exits 2 only when an image is missing locally" {
    rm -f "$T/image_present"
    touch "$T/registry_down"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"registry"* ]]
    [ ! -e "$INSTALL/komodo/.env" ]
    if grep -q ' up ' "$T/docker.log"; then false; fi
}

@test "offline re-run: binary and images already present need no registry or release access" {
    run_setup
    [ "$status" -eq 0 ]
    : > "$T/docker.log"
    : > "$T/curl.log"
    touch "$T/registry_down" "$T/release_unreachable"
    run_setup
    [ "$status" -eq 0 ]
    if grep -q 'manifest' "$T/docker.log"; then false; fi
    if grep -q -- '-fsSIL' "$T/curl.log"; then false; fi
}

@test "file and volume mismatch: volume without komodo/.env stops with guidance" {
    touch "$T/volume"
    local before; before=$(host_state)
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"komodo_mongo-data"* ]]
    [ ! -e "$INSTALL/komodo/.env" ]
    if grep -q ' up ' "$T/docker.log"; then false; fi
}

@test "file and volume mismatch: komodo/.env without the volume stops with guidance" {
    printf 'KOMODO_DATABASE_PASSWORD=old\n' > "$INSTALL/komodo/.env"
    chmod 600 "$INSTALL/komodo/.env"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"komodo/.env exists but docker volume"* ]]
    [ "$(cat "$INSTALL/komodo/.env")" = "KOMODO_DATABASE_PASSWORD=old" ]
    if grep -q ' up ' "$T/docker.log"; then false; fi
}

@test "a symlinked komodo directory is refused" {
    rm -rf "$INSTALL/komodo"
    mkdir "$T/elsewhere"
    cp "$REAL_KOMODO_ASSETS/docker-compose.yml" "$T/elsewhere/"
    cp "$REAL_KOMODO_ASSETS/versions.env" "$T/elsewhere/"
    ln -s "$T/elsewhere" "$INSTALL/komodo"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"symlink"* ]]
    [ ! -e "$T/elsewhere/.env" ]
}

# =============================================================================
# Binary verification
# =============================================================================

@test "sha256 mismatch stops the installation and keeps the existing binary" {
    printf 'previous-binary' > "$ROOT/usr/local/bin/periphery"
    chmod 755 "$ROOT/usr/local/bin/periphery"
    touch "$T/asset_bad"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"sha256"* ]]
    last_line_is "VOIPBIN_KOMODO: status=error step=periphery"
    [ "$(cat "$ROOT/usr/local/bin/periphery")" = "previous-binary" ]
    [ -z "$(find "$ROOT/usr/local/bin" -name '.periphery.*')" ]
    if grep -q '^onboard' "$T/lib.log"; then false; fi
    [ ! -e "$ROOT/etc/komodo/periphery.config.toml" ]
}

@test "sha256 mismatch with no previous binary leaves none behind" {
    touch "$T/asset_bad"
    run_setup
    [ "$status" -eq 2 ]
    [ ! -e "$ROOT/usr/local/bin/periphery" ]
}

@test "an installed binary that already matches the pin is not downloaded again" {
    preinstall_connected_periphery
    run_setup
    [ "$status" -eq 0 ]
    if grep -q 'periphery-x86_64' "$T/curl.log"; then false; fi
}

@test "a stale binary with another hash is replaced after verification" {
    printf 'old-version' > "$ROOT/usr/local/bin/periphery"
    run_setup
    [ "$status" -eq 0 ]
    cmp "$T/asset" "$ROOT/usr/local/bin/periphery"
}

# =============================================================================
# Failure propagation
# =============================================================================

@test "provision exit codes 2 and 21 pass through, others become 1" {
    echo 2 > "$T/provision.rc"
    run_setup
    [ "$status" -eq 2 ]
    last_line_is "VOIPBIN_KOMODO: status=error step=provision"

    echo 21 > "$T/provision.rc"
    run_setup
    [ "$status" -eq 21 ]

    echo 5 > "$T/provision.rc"
    run_setup
    [ "$status" -eq 1 ]
}

@test "core that never answers fails with exit 1 and an error result line" {
    touch "$T/core_down"
    run_setup
    [ "$status" -eq 1 ]
    last_line_is "VOIPBIN_KOMODO: status=error step=core"
    if grep -q '^onboard' "$T/lib.log"; then false; fi
}

@test "periphery that never connects fails with exit 1" {
    touch "$T/no_connect"
    run_setup
    [ "$status" -eq 1 ]
    last_line_is "VOIPBIN_KOMODO: status=error step=periphery"
    if grep -q '^provision' "$T/lib.log"; then false; fi
}

@test "onboard failure stops before provision" {
    echo 3 > "$T/onboard.rc"
    run_setup
    [ "$status" -eq 1 ]
    if grep -q '^provision' "$T/lib.log"; then false; fi
}

# =============================================================================
# The main stack is never touched
# =============================================================================

@test "every compose call is scoped to the komodo project and its own files" {
    run_setup
    [ "$status" -eq 0 ]
    local line n=0
    while IFS= read -r line; do
        [[ "$line" == "docker compose version"* ]] && continue
        [[ "$line" == "docker compose -p komodo -f $INSTALL/komodo/docker-compose.yml "* ]]
        # the caller's compose variables never reach a komodo call
        [[ "$line" == *"COMPOSE_ENV_FILES=unset COMPOSE_PROJECT_NAME=unset"* ]]
        n=$((n + 1))
    done < <(grep '^docker compose' "$T/docker.log")
    [ "$n" -ge 1 ]
}

@test "the script source runs docker compose only as the version probe or through the komodo wrapper" {
    local code
    code=$(grep -v '^[[:space:]]*#' "$SCRIPTS_DIR/komodo-setup.sh" | grep 'docker compose' \
        | grep -v 'docker compose -p "\$COMPOSE_PROJECT"' \
        | grep -v 'docker compose version >/dev/null' \
        | grep -vc 'die ' || true)
    [ "$code" -eq 0 ]
    grep -q 'docker compose -p "\$COMPOSE_PROJECT"' "$SCRIPTS_DIR/komodo-setup.sh"
    run grep -Ev '^[[:space:]]*#' "$SCRIPTS_DIR/komodo-setup.sh"
    [[ "$output" != *"start.sh"* && "$output" != *"install-certs.sh"* && "$output" != *"setup-dns.sh"* ]]
}

# =============================================================================
# Static checks: compose, pinned values, gitignore
# =============================================================================

@test "compose: both services restart unless-stopped" {
    run grep -c '^    restart: unless-stopped$' "$REAL_KOMODO_ASSETS/docker-compose.yml"
    [ "$output" -eq 2 ]
}

@test "compose: images come from the pinned variables, fixed container names, no periphery service" {
    local f="$REAL_KOMODO_ASSETS/docker-compose.yml"
    grep -q 'image: \${KOMODO_CORE_IMAGE' "$f"
    grep -q 'image: \${KOMODO_MONGO_IMAGE' "$f"
    grep -q 'container_name: voipbin-komodo-core' "$f"
    grep -q 'container_name: voipbin-komodo-mongo' "$f"
    if grep -Eq '^  periphery:' "$f"; then false; fi
    if grep -Eq 'image: .*:(latest|[0-9]+)$' "$f"; then false; fi
}

@test "compose: core is bound to loopback only and nothing else is published" {
    local f="$REAL_KOMODO_ASSETS/docker-compose.yml"
    grep -q '^      - 127.0.0.1:9120:9120$' "$f"
    run grep -cE '^\s+- .*[0-9]+:[0-9]+' "$f"
    [ "$output" -eq 1 ]
    if grep -Eq '^\s+- "?(0\.0\.0\.0:)?9120:9120' "$f"; then false; fi
}

@test "compose: no /backups mount, no docker socket, registration disabled, database name explicit" {
    local f="$REAL_KOMODO_ASSETS/docker-compose.yml"
    if grep -q '/backups' "$f"; then false; fi
    if grep -q 'docker.sock' "$f"; then false; fi
    grep -q 'KOMODO_DISABLE_USER_REGISTRATION: "true"' "$f"
    grep -q 'KOMODO_DATABASE_DB_NAME: komodo' "$f"
    grep -q 'KOMODO_INIT_ADMIN_PASSWORD: \${KOMODO_INIT_ADMIN_PASSWORD' "$f"
    grep -q '^  mongo-data:$' "$f"
    grep -q '^  keys:$' "$f"
}

@test "compose: renders with docker compose config when docker is available" {
    if ! /usr/bin/env -i PATH="$ORIGINAL_PATH" docker compose version >/dev/null 2>&1; then
        skip "docker compose not installed"
    fi
    local envf="$TEST_TEMP_DIR/render.env"
    printf 'KOMODO_DATABASE_USERNAME=u\nKOMODO_DATABASE_PASSWORD=p\nKOMODO_INIT_ADMIN_USERNAME=admin\nKOMODO_INIT_ADMIN_PASSWORD=a\nKOMODO_JWT_SECRET=j\nKOMODO_WEBHOOK_SECRET=w\n' > "$envf"
    run env -i PATH="$ORIGINAL_PATH" HOME="$TEST_TEMP_DIR" docker compose -p komodo \
        -f "$REAL_KOMODO_ASSETS/docker-compose.yml" \
        --env-file "$REAL_KOMODO_ASSETS/versions.env" --env-file "$envf" config
    [ "$status" -eq 0 ]
    [[ "$output" == *"@sha256:"* ]]
}

@test "versions.env: plain KEY=value lines, exactly the four pins" {
    local f="$REAL_KOMODO_ASSETS/versions.env" line n=0
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        [[ "$line" =~ ^KOMODO_[A-Z0-9_]+=[^[:space:]\"\'\$]+$ ]]
        n=$((n + 1))
    done < "$f"
    [ "$n" -eq 4 ]
    for k in KOMODO_VERSION KOMODO_PERIPHERY_SHA256 KOMODO_CORE_IMAGE KOMODO_MONGO_IMAGE; do
        [ "$(grep -c "^$k=" "$f")" -eq 1 ]
    done
}

@test "versions.env: version format, 64 hex sha256, digest pinned images" {
    local f="$REAL_KOMODO_ASSETS/versions.env"
    grep -Eq '^KOMODO_VERSION=v[0-9]+\.[0-9]+\.[0-9]+$' "$f"
    grep -Eq '^KOMODO_PERIPHERY_SHA256=[0-9a-f]{64}$' "$f"
    grep -Eq '^KOMODO_CORE_IMAGE=ghcr\.io/moghtech/komodo-core@sha256:[0-9a-f]{64}$' "$f"
    grep -Eq '^KOMODO_MONGO_IMAGE=mongo@sha256:[0-9a-f]{64}$' "$f"
}

@test "setup script refuses a versions.env whose image is not digest pinned" {
    sed -i -E 's#^KOMODO_CORE_IMAGE=.*#KOMODO_CORE_IMAGE=ghcr.io/moghtech/komodo-core:2#' "$INSTALL/komodo/versions.env"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"not digest pinned"* ]]
    if grep -q ' up ' "$T/docker.log"; then false; fi
}

@test "setup script refuses a versions.env with a malformed sha256" {
    sed -i -E 's#^KOMODO_PERIPHERY_SHA256=.*#KOMODO_PERIPHERY_SHA256=abc123#' "$INSTALL/komodo/versions.env"
    run_setup
    [ "$status" -eq 2 ]
    [[ "$output" == *"KOMODO_PERIPHERY_SHA256"* ]]
}

@test "script source carries the unit restart policy" {
    grep -q '^Restart=on-failure$' "$SCRIPTS_DIR/komodo-setup.sh"
    grep -q '^RestartSec=5$' "$SCRIPTS_DIR/komodo-setup.sh"
}

@test ".gitignore ignores every Komodo secret and local state path" {
    cd "$PROJECT_ROOT"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || skip "not a git checkout"
    local p
    for p in .env.komodo .env.komodo.pre-restore.20260101 .env.new komodo/cli-key \
             komodo/cli-key.pre-restore.20260101 komodo/.env komodo/admin-password \
             komodo/.adopted .certs-pending .certs-sync-pending .voipbin-hook-token \
             komodo/some-new-secret; do
        git check-ignore --no-index -q "$p" || { echo "not ignored: $p"; return 1; }
    done
}

@test ".gitignore keeps the committed Komodo assets and .env.template visible" {
    cd "$PROJECT_ROOT"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || skip "not a git checkout"
    local p
    for p in komodo/docker-compose.yml komodo/versions.env komodo/.env.example .env.template; do
        if git check-ignore --no-index -q "$p"; then echo "ignored: $p"; return 1; fi
    done
}
