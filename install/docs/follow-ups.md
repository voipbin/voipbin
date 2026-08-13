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
  a follow-up ticket.
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

## Komodo web UI container management (VOIP-1339)

`install/komodo/` adds a standalone Komodo (core/periphery) deployment for
restarting/stopping containers and reading logs from a browser, separate
from the main stack's Prometheus/Grafana (VOIP-1336, metrics/dashboards
only — the two don't overlap). Full design, empirical verification, and
review history: `docs/plans/2026-08-14-komodo-container-management-design.md`.

**This does not retroactively deploy to `bm-nyc-01` either**, for the same
reason as the monitoring stack above — merging this PR only updates the
tracked `install/komodo/docker-compose.yml.dist`. Deploying it means
running `install/komodo/scripts/komodo.sh init && ./scripts/komodo.sh up`
on the host as a separate, deliberate operator step.

Left out of this first pass, tracked as follow-ups:

- **CircleCI → Komodo API/webhook deploy trigger.** Out of scope entirely —
  the main stack's only deploy path stays CircleCI → SSH. If this is ever
  reconsidered, it needs its own design/review (deploy-path duplication risk
  was the main reason Komodo was scoped down to "no Stack resource" here).
- **Komodo managing the main voipbin stack as a "Stack" resource** (i.e.
  Deploy/Destroy from the Komodo UI). Deliberately not configured — see the
  design doc's "1차 스코프" rationale. Revisit only with a fresh
  design/review if the need becomes concrete.
- **`install/komodo`'s 3 images in the `versions.lock`/`sync-compose-images.sh`
  digest-tracking pipeline** — tracked as
  [VOIP-1340](https://voipbin.atlassian.net/browse/VOIP-1340). Digests are
  pinned today, just not auto-tracked; updates are manual.
- **A working `komodo-periphery` healthcheck.** Empirically, a
  `wget --no-check-certificate` probe against its self-signed `:8120`
  listener reported `unhealthy` even while the container functioned
  correctly (see the design doc's "empirical verification" section for what
  was actually run) — removed rather than shipped broken, per this repo's
  own VOIP-1336 `redis-exporter` lesson. `mongo`/`core`'s healthchecks
  already gate cold-boot ordering, since nothing depends on periphery's own
  health status.
- Komodo OIDC/SSO login, terminal access (`PERIPHERY_DISABLE_TERMINALS`),
  and backups for the `komodo_mongo_data` volume.
