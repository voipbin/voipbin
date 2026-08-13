# Follow-ups

Deferred work tracked here rather than in the PR that introduced the
feature, so it isn't lost once the PR merges. File a Jira ticket
(project `VOIP`) before starting any of these.

## Monitoring stack (VOIP-1336)

The initial Prometheus/Grafana deployment (`docker-compose.yml.dist`'s
`prometheus`/`grafana`/`node-exporter`/`cadvisor`/`redis-exporter`/
`mysqld-exporter` services, `config/prometheus/`, `config/grafana/`)
deliberately left the following out of scope:

- **Alertmanager.** No alerting/paging is wired up yet — Prometheus only
  collects and Grafana only displays. Alert rules, an Alertmanager
  container/config, and a notification channel (Slack/PagerDuty/email) are
  a follow-up ticket. **Addressed in VOIP-1338** — see below; a real
  notification channel is still deferred.
- **Dedicated MySQL monitoring user.** `mysqld-exporter` currently
  authenticates as `root`/`MYSQL_ROOT_PASSWORD` (see the comment on the
  `mysqld-exporter` service in `docker-compose.yml.dist`). A
  least-privilege `PROCESS, REPLICATION CLIENT, SELECT` user, provisioned
  the same way `DATABASE_ASTERISK_USERNAME`/`_PASSWORD` are provisioned in
  `scripts/common.sh`'s `provision_asterisk_db_user()`, would narrow this
  exporter's blast radius.
- **Asterisk `res_prometheus`.** The `voipbin/voip-asterisk-call`/
  `-conference`/`-registrar` images this stack pulls (built from
  `monorepo-voip`) do not currently enable or expose the `res_prometheus`
  module/HTTP endpoint — there is nothing to scrape yet.
  `config/prometheus/prometheus.yml` has the `asterisk` scrape job
  commented out with instructions for when this lands. Enabling it means
  rebuilding those images upstream (out of scope for this repo, which only
  consumes pre-built, digest-pinned images) — track as a `monorepo-voip`
  ticket, then flip the compose/prometheus config on here once the images
  publish a reachable port.

## kamailio-exporter / RTPEngine / Alertmanager monitoring (VOIP-1338)

Ported the battle-tested pieces of `monorepo-etc/infra-prometheus`'s GKE
production monitoring stack that VOIP-1336 did not carry over: a
`kamailio-exporter` sidecar (`docker-compose.yml.dist`, port 9105 — see that
service's own comment for why not RTPEngine's 9101), scrape jobs for
RTPEngine and kamailio-exporter (`config/prometheus/prometheus.yml`, via
`host.docker.internal` since both run `network_mode: host`), an
`alertmanager` service with ported/adapted alert rules
(`config/prometheus/alert-rules.yml`), and two new Grafana dashboards
(`kamailio-overview.json`, `rtpengine-overview.json`). Extended the existing
`rabbitmq-overview.json` with alarm/memory/disk/fd panels using the
built-in `rabbitmq_prometheus` plugin's actual metric names (the GCP
dashboard's community-exporter metric names — `rabbitmq_node_mem_used`,
`rabbitmq_queue_messages_published_total`, etc — don't exist under this
stack's plugin and would render "No data").

Deliberately left out of scope:

- **Real alert notification channel.** `config/alertmanager/alertmanager.yml`
  ships with a `null`-equivalent default receiver — alerts are visible in
  Alertmanager's own UI (`http://localhost:9093` via the same SSH-tunnel
  pattern as prometheus/grafana) and in Grafana, but nothing pages an
  operator automatically. This repo has no established outbound
  notification integration (Slack/Discord/PagerDuty/email) to wire up by
  default; see the header comment in `alertmanager.yml` for how an operator
  adds one today. A follow-up ticket should decide on and implement a
  default-on channel.
- **`asterisk-overview.json` was NOT ported**, same reasoning as the
  Asterisk `res_prometheus` item above — it references metric names
  (`asterisk_crruent_channel_tech`, including a typo in the name itself)
  with no corresponding exporter anywhere in this stack's images.
- **kamailio_exporter version.** Pinned to `2.0.0` (latest stable), not the
  `1.0.1` monorepo-voip's `voip-kamailio-ansible/roles/kamailio-docker`
  design doc defaults to. Verified directly against the `2.0.0` image's
  own `--help` output before switching: `--web.listen-address`,
  `--kamailio.scrape-uri`, and `--kamailio.methods` (with the same method
  names this stack uses) are all unchanged from `1.0.1`; only the `-l`
  short flag and the log output format changed, neither used here.
- **Pre-existing gap found during verification, unrelated to this ticket:**
  `tests/test_cli_mode.py` requires a `cli_module` pytest fixture that has
  no definition anywhere in this repo — `pytest tests/test_cli_mode.py`
  errors on all 18 of its tests with "fixture 'cli_module' not found".
  Confirmed pre-existing on `origin/main` (not introduced by this PR):
  `git show origin/main:install/tests/conftest.py` doesn't exist, and
  `git diff --stat origin/main -- install/tests/` shows only this PR's
  `config.bats` additions. Likely lost in the migration from the old
  standalone `voipbin/install` repo (which does have a `tests/conftest.py`
  defining this fixture) into this monorepo. Worth a small follow-up
  ticket to port that `conftest.py` over.
- **RabbitMQ per-queue alert cardinality.** `RabbitMQMessagesBacklog`/
  `RabbitMQNoConsumers` alert per-queue, same as the GCP source rules; on a
  host running many `bin-*-manager` queues this could produce one alert per
  affected queue rather than one aggregated alert. Acceptable for now
  (matches the proven source behavior); revisit if it turns out to be noisy
  in practice.

## Loopback-only db/redis/rabbitmq port binding (VOIP-1336)

`docker-compose.yml.dist`'s `db`/`redis`/`rabbitmq` services now default to
binding their published host ports (`3306`/`6379`/`5672`,`15672`) to
`127.0.0.1` instead of `0.0.0.0` (`DB_BIND_ADDRESS`/`REDIS_BIND_ADDRESS`/
`RABBITMQ_BIND_ADDRESS` in `.env.template`/`scripts/init.sh`), matching the
prometheus/grafana access pattern from the section above.

**This does not retroactively apply to already-live servers.** Per this
repo's dist/live split (`docker-compose.yml.dist`'s own header comment),
a `git pull` only updates the tracked `.dist` template — it never rewrites
an existing, untracked, live `docker-compose.yml`. `bm-nyc-01` (and any
other already-deployed host) keeps publishing these three services on
`0.0.0.0` — with the existing nftables drop-external rules as the
defense-in-depth backstop — until an operator deliberately diffs/merges
this change into that host's live `docker-compose.yml` and `.env`, per
this repo's documented update model (see `docker-compose.yml.dist`'s
header comment and `install/CLAUDE.md`'s "docker-compose.yml.dist vs
docker-compose.yml" section).

Applying this to `bm-nyc-01` itself — diffing/merging the live
`docker-compose.yml`, adding the three new `.env` vars, verifying nothing
outside that host depends on `0.0.0.0` reachability for these ports (e.g.
no split-host consumer other than Kamailio's already-loopback-routed
`KAMAILIO_DB_HOST`/`KAMAILIO_REDIS_HOST` path) — is a separate follow-up
deployment step, not part of this PR.
