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
