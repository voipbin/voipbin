#!/usr/bin/env bats
# Tests for scripts/create-admin.sh (first admin account helper).
# docker and curl are stubbed; each stub records its argv (one line per
# call) so the tests can assert the password never reaches a command line.

load 'test_helper'

# generated per run: no password-like literal in the repository
FAKE_PASSWORD="$(openssl rand -hex 12)"

setup() {
    setup_test_env
    export VOIPBIN_ADMIN_POLL_INTERVAL=0
    export TEST_TEMP_DIR
    export ARGV_LOG="$TEST_TEMP_DIR/argv.log"
    export CUSTOMER_EXISTS=false AGENT_USERNAME="admin@example.com" UPDATE_RC=0 LOGIN_TOKEN=fake-jwt
}

teardown() {
    teardown_test_env
}

stub_stack() {
    mock_command_script "docker" '
echo "DOCKER $*" >> "$ARGV_LOG"
[[ -n "${PASSWORD:-}" ]] && echo "DOCKER_ENV_PASSWORD_SET" >> "$ARGV_LOG"
case "$*" in
    *"customer-control customer list"*)
        if [[ "$CUSTOMER_EXISTS" == "true" || -f "$TEST_TEMP_DIR/created" ]]; then echo "[{\"id\":\"cust-1\",\"email\":\"admin@example.com\"}]"; else echo "[]"; fi ;;
    *"customer-control customer create"*) echo "created" > "$TEST_TEMP_DIR/created"; exit 0 ;;
    *"customer-control customer get"*) echo "{\"id\":\"cust-1\",\"billing_account_id\":\"bill-1\"}" ;;
    *"agent-control agent list"*) echo "[{\"id\":\"agent-1\",\"username\":\"$AGENT_USERNAME\"}]" ;;
    *"agent-control agent update-password"*) exit "$UPDATE_RC" ;;
    *"billing-control account update-plan-type"*) exit 0 ;;
esac
exit 0'
    mock_command_script "curl" '
echo "CURL $*" >> "$ARGV_LOG"
body=$(cat)
echo "CURL_STDIN $body" >> "$TEST_TEMP_DIR/curl_stdin.log"
if [[ -n "$LOGIN_TOKEN" ]]; then echo "{\"token\":\"$LOGIN_TOKEN\"}"; else echo "{}"; fi'
}

run_helper() {
    run env VOIPBIN_ADMIN_PASSWORD="$FAKE_PASSWORD" bash "$SCRIPTS_DIR/create-admin.sh" "$@"
}

@test "create-admin: creates the customer, sets the password and verifies the login" {
    stub_stack
    run_helper --email admin@example.com
    [[ "$status" -eq 0 ]]
    [[ "$(echo "$output" | tail -1)" == "VOIPBIN_ADMIN: status=ok customer_id=cust-1 agent_id=agent-1" ]]
    grep -q "customer-control customer create --name Admin --email admin@example.com" "$ARGV_LOG"
    grep -q "DOCKER exec -e PASSWORD voipbin-agent-mgr /app/bin/agent-control agent update-password --id agent-1" "$ARGV_LOG"
    grep -q "DOCKER_ENV_PASSWORD_SET" "$ARGV_LOG"
    grep -q "update-plan-type --id bill-1 --plan-type unlimited" "$ARGV_LOG"
    # the login body went over stdin with the right credentials
    grep -q "\"password\": \"$FAKE_PASSWORD\"" "$TEST_TEMP_DIR/curl_stdin.log"
}

@test "create-admin: the password never appears in argv or in the output" {
    stub_stack
    export CUSTOMER_EXISTS=true
    run_helper --email admin@example.com --reset
    [[ "$status" -eq 0 ]]
    # (`! cmd` does not fail a bats test unless it is the last line)
    if grep -qF "$FAKE_PASSWORD" "$ARGV_LOG"; then false; fi
    [[ "$output" != *"$FAKE_PASSWORD"* ]]
    [[ "$output" != *"fake-jwt"* ]]
}

@test "create-admin: an existing customer without --reset exits 3 and changes nothing" {
    stub_stack
    export CUSTOMER_EXISTS=true
    run_helper --email admin@example.com
    [[ "$status" -eq 3 ]]
    [[ "$(echo "$output" | tail -1)" == "VOIPBIN_ADMIN: status=exists customer_id=cust-1" ]]
    if grep -q "update-password" "$ARGV_LOG"; then false; fi
    if grep -q "customer create" "$ARGV_LOG"; then false; fi
}

@test "create-admin: a failing update-password is an error (not swallowed)" {
    stub_stack
    export CUSTOMER_EXISTS=true UPDATE_RC=1
    run_helper --email admin@example.com --reset
    [[ "$status" -eq 1 ]]
    [[ "$(echo "$output" | tail -1)" == *'status=error reason="agent update-password failed'* ]]
}

@test "create-admin: a login that returns no token is an error" {
    stub_stack
    export CUSTOMER_EXISTS=true LOGIN_TOKEN=""
    run_helper --email admin@example.com --reset
    [[ "$status" -eq 1 ]]
    [[ "$(echo "$output" | tail -1)" == *'POST /auth/login did not return a token'* ]]
}

@test "create-admin: the admin agent is matched by username, not by list position" {
    stub_stack
    export CUSTOMER_EXISTS=true AGENT_USERNAME="someone-else@example.com"
    run_helper --email admin@example.com --reset --wait 2
    [[ "$status" -eq 1 ]]
    [[ "$(echo "$output" | tail -1)" == *'admin agent with username admin@example.com not created'* ]]
}

@test "create-admin: refuses to run without the password variable" {
    stub_stack
    run env -u VOIPBIN_ADMIN_PASSWORD bash "$SCRIPTS_DIR/create-admin.sh" --email admin@example.com
    [[ "$status" -eq 1 ]]
    [[ "$output" == *'VOIPBIN_ADMIN_PASSWORD is not set'* ]]
    [[ ! -s "$ARGV_LOG" ]]
}
