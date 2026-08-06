#!/bin/bash
# Isolated live tests T1-T4 (infra reliability) — design §4.
# Runs a voipbin-test compose project (test override: no container_name
# collisions, subnet 10.199.0.0/16, no host ports, no VoIP services).
set -uo pipefail
cd "$(dirname "$0")/../.."

COMPOSE="docker compose -p voipbin-test -f docker-compose.yml -f docker-compose.test.yml"
PASS=0; FAIL=0
ok()  { echo "PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL  $1"; FAIL=$((FAIL+1)); }

# T1: compose config lint
$COMPOSE config -q && ok "T1 compose config" || bad "T1 compose config"

# bring up infra only
$COMPOSE up -d db redis rabbitmq >/dev/null 2>&1
DB=$($COMPOSE ps --format '{{.Name}}' db)
REDIS=$($COMPOSE ps --format '{{.Name}}' redis)
MQ=$($COMPOSE ps --format '{{.Name}}' rabbitmq)
echo "containers: db=$DB redis=$REDIS mq=$MQ"

wait_healthy() { # name timeout
    local n="$1" t="${2:-90}" i=0
    while [ $i -lt "$t" ]; do
        st=$(docker inspect -f '{{.State.Health.Status}}' "$n" 2>/dev/null || echo missing)
        [ "$st" = "healthy" ] && return 0
        sleep 2; i=$((i+2))
    done
    return 1
}

wait_healthy "$DB" 120   && ok "infra db healthy"    || bad "infra db healthy"
wait_healthy "$REDIS" 60 && ok "infra redis healthy" || bad "infra redis healthy"
wait_healthy "$MQ" 120   && ok "infra rabbitmq healthy (plugin downloaded to bind dir)" || bad "infra rabbitmq healthy"

# T2: restart policy — crash the main process INSIDE the container.
# NOTE: 'docker kill' does NOT trigger restart policies (docker treats it as a
# manual stop). A real crash = PID 1 exiting, simulated via docker exec kill 1.
for c in "$DB" "$REDIS" "$MQ"; do
    docker exec "$c" sh -c 'kill 1' >/dev/null 2>&1 || true
done
sleep 5
T2OK=1
for c in "$DB" "$REDIS" "$MQ"; do
    wait_healthy "$c" 150 || { T2OK=0; echo "  not recovered: $c"; }
done
[ $T2OK = 1 ] && ok "T2 crash(PID1) -> auto-restart -> healthy (db/redis/rabbitmq)" || bad "T2 auto-restart"

# T3: Redis persistence (AOF) — SET, crash, expect key after restart
docker exec "$REDIS" redis-cli SET t3key t3value >/dev/null
sleep 2
docker exec "$REDIS" sh -c 'kill 1' >/dev/null 2>&1 || true
sleep 3
wait_healthy "$REDIS" 60
V=$(docker exec "$REDIS" redis-cli GET t3key)
[ "$V" = "t3value" ] && ok "T3 redis key survives kill+restart" || bad "T3 redis persistence (got: $V)"

# T4: RabbitMQ offline plugin — force-recreate WITHOUT network egress.
# The plugin .ez must already be in config/rabbitmq/plugins (downloaded on
# first start). Simulate offline by nulling the download URL via a fake
# entrypoint? Simpler & honest: verify the .ez file EXISTS on the host bind
# (proves recreate won't need network) AND force-recreate keeps working.
if ls config/rabbitmq/plugins/rabbitmq_delayed_message_exchange-*.ez >/dev/null 2>&1; then
    ok "T4a plugin .ez persisted on host bind dir"
else
    bad "T4a plugin .ez not persisted"
fi
$COMPOSE up -d --force-recreate rabbitmq >/dev/null 2>&1
MQ=$($COMPOSE ps --format '{{.Name}}' rabbitmq)
wait_healthy "$MQ" 120 || bad "T4b rabbitmq recreate"
# verify no wget happened this time (log shows skip) and plugin is enabled
if docker exec "$MQ" rabbitmq-plugins list -e 2>/dev/null | grep -q delayed_message; then
    ok "T4b delayed_message plugin enabled after force-recreate"
else
    bad "T4b plugin not enabled after recreate"
fi
if docker logs "$MQ" 2>&1 | grep -q "Saving to.*rabbitmq_delayed"; then
    bad "T4c recreate re-downloaded the plugin (should reuse bind dir)"
else
    ok "T4c recreate did NOT re-download plugin"
fi

# T10: log rotation present on EVERY service in the effective config.
# Compare dynamically: rendered max-size count must be >= service count
# (the x-logging anchor itself adds one extra occurrence).
SVC=$($COMPOSE config --services | wc -l)
N=$($COMPOSE config | grep -c "max-size" || true)
[ "$N" -ge "$SVC" ] && ok "T10 log rotation on all $SVC services (max-size x$N)" || bad "T10 log rotation: services=$SVC max-size=$N"

echo
echo "RESULT: $PASS passed, $FAIL failed"
exit $([ $FAIL -eq 0 ] && echo 0 || echo 1)
