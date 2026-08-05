# Single-Server Production Mode — Design (Iteration 1)

Status: Round 4 (Round-3 convergence findings applied)
Date: 2026-07-04
Branch: NOJIRA-Single-server-production-mode

## 1. Goal & Scope

Evolve the sandbox repo from "local evaluation" into a **single-server production
deployment path** where deploy management is simpler than k8s. Research confirmed
(2026-07-04, monorepo k8s analysis) that VoIPBin's k8s usage is shallow:

- Secrets = env vars (1:1 mappable to `.env`), no ConfigMap/volume mounts.
- Service-to-service = RabbitMQ RPC (no k8s Service discovery on the hot path).
- No HPA/PDB/livenessProbe in the real manifests (docs-only).
- Prod DB migration is already manual (dbscheme has no CI release step).
- The ONLY hard k8s dependency is bin-sentinel-manager (client-go pod watch →
  call-manager recovery of calls stranded on a dead Asterisk). asterisk-proxy
  already supports `--kubernetes_disabled=true`.

**Touchpoint summary (what this PR actually changes):** `docker-compose.yml`
(restart policies, persistence volumes, healthchecks, log rotation, sentinel
removal), `scripts/voipbin-cli.py` (new `backup`, `restore` commands; `upgrade`
flow inside `update`; pinned-repo guard in `rollback`), new `scripts/migrate.sh`
(containerized alembic), `scripts/init_database.sh` (read pin from
versions.lock instead of hardcode; delegate alembic to migrate.sh),
`scripts/start.sh` (drop the host-alembic gate at L638-644, delegate to
migrate.sh), new `docker-compose.test.yml` (isolated test override), docs.
No monorepo/back-end changes.

**In scope (P0, this iteration):**
1. Infra reliability: restart policies for db/redis/rabbitmq, Redis/RabbitMQ
   persistence, RabbitMQ delayed-message plugin offline availability.
2. Healthchecks for app services (bin-*, square-*), log rotation defaults.
3. Containerized DB migration (drop host pip/alembic dependency) with the
   monorepo pin read from `versions.lock` (single source of truth).
4. `voipbin backup` / `voipbin restore` (MySQL dump + recordings + .env/certs).
5. Upgrade flow: `voipbin update` becomes safe-ordered
   (backup → pull pinned digests → migrate → recreate services in order).

**Out of scope (deferred, recorded in §9):**
- Real public domain + Let's Encrypt pipeline (Iteration 2).
- Resource limits per service (needs measurement, not guesses).
- sentinel-manager replacement (docker-events watcher) — single-Asterisk
  deployments have a reduced blast radius; documented limitation for now.
- Secrets hardening (non-root DB user, generated passwords) — Iteration 2.
- Monitoring/alerting stack (Prometheus scraper + alertmanager) — Iteration 2.

## 2. Current State (facts from 2026-07-04 research)

- compose: 45 services, 6 named volumes. Healthchecks on only 8 (infra + VoIP).
- db/redis/rabbitmq have **no restart policy**; bin-*/square-* have `restart: always`.
- Redis: no volume. RabbitMQ: no volume, and wget's the delayed-message plugin
  from GitHub **on every container recreate** (compose L40-48; an existence
  check skips it on plain restarts, but `/plugins` is not persisted so any
  recreate re-downloads) — offline recreate fails.
- versions.lock (93 lines): `target_commit` 0ce70d7d, 39 image digests; consumed
  only by the `update` PIN GUARD (voipbin-cli.py L4649-4677). Digests are ALSO
  baked into compose image lines; dbscheme pin is ALSO hardcoded in
  init_database.sh L20 (`MONOREPO_PIN`). Three-way manual sync.
- `update` never runs migrations. No backup command exists anywhere (mysqldump: 0 hits).
- Migration = host pip alembic (SQLAlchemy<2.0 pitfall) + sparse clone of monorepo.

## 3. Design

### 3.1 Infra reliability (compose changes)

- Add `restart: always` to db, redis, rabbitmq, coredns (coredns already has it).
- Redis: add `redis_data:/data` named volume + `--appendonly yes` command flag.
  Rationale: registrar cache / rate-limit state survive restarts; still safe to
  lose (cache semantics) but persistence removes the cold-start stampede.
- RabbitMQ: add `rabbitmq_data:/var/lib/rabbitmq` named volume AND pin
  `hostname: voipbin-rabbitmq`. Without a fixed hostname the mnesia directory
  is keyed `rabbit@<container-id>`, so every container recreate orphans the
  volume's prior data (silent data reset). hostname pin is a P0 prerequisite
  for the volume to be meaningful.
  Plugin: replace per-start wget with a one-time download into a HOST BIND
  directory `config/rabbitmq/plugins/` mounted at a SEPARATE path (e.g.
  `/opt/rabbitmq-extra-plugins`) with `RABBITMQ_PLUGINS_DIR` extended to
  include it. Deliberately NOT a named volume over `/plugins`: a named volume
  over the image's plugin dir would be populated once and then SHADOW newer
  plugins on image upgrade. The .ez file is version-coupled to the RabbitMQ
  image tag — record the pair in versions.lock notes; the download script
  checks file existence first (offline-safe start).
- Log rotation: add YAML anchor `x-logging: &default-logging` (json-file,
  max-size 10m, max-file 3) and apply to ALL services. Kamailio keeps its
  existing block (identical values).

### 3.2 Healthchecks for app services

bin-* Go images are NOT distroless (Round 2 verified: runtime base is
`alpine:latest` for call/flow/agent managers, `golang:1.25-bookworm` for
api-manager — both ship wget). Expected answer: option (a) below works for
all bin-* services; the per-digest verification at implementation time is a
confirmation step, not an open question.

- (a) HTTP GET on the Prometheus metrics port :2112 via wget — only if the image
  has wget/busybox. MUST be verified against the pinned images at implementation
  time (`docker run --rm --entrypoint sh <img> -c 'which wget'`).
- (b)/(c) shell/pgrep fallbacks are listed for completeness but are NOT real
  alternatives: an image lacking wget almost certainly lacks bash and procps
  too. Honest tiering: (a) works or the service gets NO healthcheck.

Decision: implement (a) where possible; for images without tooling, document
the per-image result in the compose comments and rely on `restart: always` as
the floor. The healthcheck adds visibility (`voipbin status` shows unhealthy)
even when no orchestrator reacts to it.
square-* are nginx-based: use `wget -q -O /dev/null http://127.0.0.1:80/` (nginx
images ship busybox wget) — verify at implementation.

### 3.3 Containerized migration (`scripts/migrate.sh`)

New script, replaces host-alembic usage for BOTH init and upgrade paths:

1. Read `dbscheme_monorepo_commit` from `versions.lock` (jq or python3 -c).
2. Clone bin-dbscheme-manager at that commit into a temp dir (sparse, depth 1
   via `git fetch origin <sha>` — GitHub allows SHA fetch).
3. Run `python:3.11-slim` container ON THE COMPOSE NETWORK with the clone
   mounted read-only; inside: `pip install 'alembic==1.11.3'
   'SQLAlchemy==1.4.52' 'PyMySQL==1.1.0' 'cryptography==42.0.8'` (exact pins
   for reproducibility; version floats were rejected in review; cryptography
   is REQUIRED by PyMySQL for MySQL 8's caching_sha2_password default auth,
   discovered in live T6 testing) then `alembic upgrade head` for
   bin-manager and asterisk streams, sequentially, aborting on first failure.
   Databases are created with utf8/utf8_general_ci (matching init_database.sh;
   MySQL 8's default utf8mb4 breaks long-VARCHAR indexes with Error 1071).
   PyMySQL avoids the mysqlclient C build dependency entirely. Non-interactive
   by design (no re-run prompt).
4. Exit non-zero on failure; caller aborts the upgrade.

`init_database.sh` keeps its interface but delegates the alembic step to
migrate.sh; its `MONOREPO_PIN` hardcode is removed (reads versions.lock).
This kills the 3-way sync (compose digests remain, that is 2-way and is the
release artifact by design).

### 3.4 Backup / restore (`voipbin backup`, `voipbin restore`)

`backup`:
- `docker exec voipbin-db mysqldump --single-transaction --routines
  --all-databases` → gzip → `backups/<ts>/mysql.sql.gz`.
- Recordings volumes: `docker run --rm -v <vol>:/src -v backups:/dst alpine tar`
  → `backups/<ts>/recordings-{call,conf}.tar.gz`.
- Config: copy `.env`, `certs/`, `versions.lock` into `backups/<ts>/config/`.
- Write `backups/<ts>/manifest.json` (timestamp, versions.lock target_commit,
  compose file hash, sizes). Retention: keep last N (default 7), prune older.
- Secrets surface: `backups/` is created `chmod 700`, files `600` — the backup
  contains the full `.env` API-key set in plaintext. Operator docs state
  plainly: a backup on the same disk dies with the disk; copy `backups/<ts>`
  off-host (rsync/rclone examples given). Off-host automation is Iteration 2.
- Runs while services are up (mysqldump --single-transaction is consistent for
  InnoDB). No downtime. Backup failure ABORTS whatever invoked it.

`restore <ts>`:
- Refuses unless `--force` AND services stopped (except db and redis).
- Streams the dump into mysql, untars recordings into volumes, restores config
  copies (never overwrites current .env without `.env.pre-restore` backup).
- **Flushes Redis** (`docker exec voipbin-redis redis-cli FLUSHALL`) after the
  dump import. Rationale (Round 2 finding): every bin-* service reads
  cache-first with 24h TTLs (23 of 25 Set sites use `time.Hour*24`; e.g.
  bin-call-manager dbhandler/call.go L318-325), so restoring MySQL to an older
  state while Redis keeps newer rows would serve ghost/stale data for up to a
  day — and §3.1 makes Redis persistent (AOF), removing the accidental
  self-heal a restart used to provide. Services are stopped anyway; flush is free.
- Prints post-restore instructions (start services, verify status).

### 3.5 Upgrade flow (extend `cmd_update`)

**Delivery channel (the part Round 1 was missing):** for a pinned repo, new
digests arrive ONLY via `update scripts` (git pull of compose + versions.lock +
scripts, cli L4939-4940). `docker compose pull` alone is a no-op because the
digests are baked into compose image lines. Therefore the ONLY meaningful
upgrade entry point is `update all`, and its order must be fixed:

```
update all (pinned) →
  1. backup                  # BEFORE git pull — captures the OLD pin state
     (abort on failure; --skip-backup opts out)
  2. update scripts          # git pull → new compose + versions.lock + scripts
  3. compose pull            # fetch the NEW pinned digests
  4. migrate.sh              # alembic head at the NEW dbscheme pin (non-interactive)
     (abort on failure — see failure modes below)
  5. docker compose up -d    # recreates only containers whose digest changed
  6. verify                  # poll `docker compose ps --format json` until no
                             # starting/unhealthy (services WITHOUT healthchecks
                             # have no health field — treat absent as running),
                             # timeout 120s; then GET api-manager /ping from a
                             # curlimages/curl helper container on the compose
                             # network (bin-api-manager image may lack curl)
```

`update` / `update images` on a pinned repo keeps today's PIN GUARD behavior
(pull-only, effectively no-op) but now PRINTS an explicit hint that real
upgrades go through `update all`. `--skip-backup` is threaded through
`cmd_update` arg filtering (same pattern as `--check`, cli L4622-4623).

**Failure modes (explicit contract):**
- backup fails → ABORT before anything changed. State: untouched.
- git pull fails/conflicts → ABORT. State: old pin intact, backup exists.
- pull fails midway → old containers still run old digests; safe to re-run.
- migrate.sh fails → MySQL DDL is non-transactional, so a PARTIAL migration
  is possible (the two DB streams also run independently). Recovery procedure
  (documented in operator docs): stop services → `voipbin restore <ts>` from
  step-1 backup (restores pre-upgrade schema+data) → `git checkout` the prior
  repo commit (compose+lock+scripts are git-versioned, step 2 is reversible)
  → start. The current `cmd_update` behavior of swallowing migration output
  and exit code (`os.system(... > /dev/null 2>&1)` then unconditional "done",
  cli L4784-4785) is REMOVED — the new flow surfaces output and aborts on
  non-zero. init_database.sh's interactive re-run prompt (L387) is bypassed:
  the upgrade path calls migrate.sh directly, never init_database.sh.
- verify times out → print status of unhealthy services and point to
  `voipbin restore` (data rollback, requires stop) and manual `git checkout`
  of the previous repo commit (image rollback). Do NOT point to
  `voipbin rollback`: that command replays docker-compose.override.yml
  snapshots (cli L5042-5063) which are only written on the UNPINNED path;
  on a pinned repo it would either find nothing or restore a stale override
  that silently shadows the pinned digests (unpinning the repo). P0 adds a
  pinned-repo guard to `cmd_rollback` that explains this and refuses.

Rationale for migrate-before-recreate: monorepo migrations follow an
**expand/contract convention**, NOT pure additivity (Round 3 re-measurement,
counting both alembic ops AND raw-SQL DDL inside upgrade() — migrations here
mostly use `op.execute("ALTER TABLE ...")`, so op-only counting undercounts:
64 of 232 bin-manager and 24 of 130 asterisk migrations contain
DROP COLUMN/DROP TABLE/ALTER...DROP|MODIFY|CHANGE; e.g. e234a24addec
2026-06-22). The discipline is that destructive steps land only AFTER code
stopped referencing the column (e234a24addec documents
expand→backfill→contract). This makes
a ONE-release-step upgrade safe, but a MULTI-release jump can drop a column
the still-running old containers reference between steps 4 and 5. Operator
docs gate: prefer sequential release upgrades; for multi-version jumps,
review the migration list first (command provided in docs). Backup-first +
abort-on-failure keeps the order recoverable either way.

**CLI self-modification note:** step 2's git pull overwrites the running
voipbin-cli.py. CPython loads the file fully at start, so without a re-exec,
steps 3-6 would execute OLD cli code orchestrating the NEW pin. P0 therefore
re-execs after step 2 with EXPLICIT resume state to prevent re-entry loops:
`os.execv(sys.executable, [python, cli, 'update', 'all',
'--resume-from=pull', '--backup-ts=<ts>'] + passthrough flags)`.
The re-exec'd process (new code) skips steps 1-2 (`--resume-from=pull`),
carries the step-1 backup timestamp for failure-mode hints, inherits
`--skip-backup`/`--check` flags, and NEVER re-execs again (the resume flag
doubles as the loop guard). The abort-on-failure contract applies unchanged
in the resumed process. migrate.sh runs as a subprocess post-pull and is
always the new version regardless.

Naming: `restore` = DATA (destructive, from backups/). `rollback` = image
override history (unpinned repos only). Help text will state this distinction.

Advancing the pin itself (building a new versions.lock + compose digest set)
remains maintainer release tooling (Iteration 2). This iteration makes
CONSUMING a delivered pin safe, and the delivery mechanism (git pull via
`update scripts`) already exists today.

### 3.6 sentinel-manager stance (remove from compose)

sentinel-manager cannot start outside k8s (`rest.InClusterConfig()` fatal).
Contrary to the earlier draft, it IS present in compose today (L858-869) with
`restart: always` + pinned digest — i.e. a permanent crash-loop burning CPU
and log volume. P0 REMOVES the service block from compose (with a comment
explaining why and pointing to the Iteration 2 docker-events bridge design).
Consequence (unchanged from today, since it never worked in compose): if an
Asterisk container dies mid-call, stranded call rows are not auto-recovered.
Mitigations documented: (1) single asterisk-call instance + `restart: always`
means the window is container-crash-only; (2) a docker-events→RabbitMQ bridge
emitting the sentinel event contract is the Iteration 2+ design (needs
contract validation against call-manager consumer).

## 4. Test Plan

Host-safety rule: never run `./voipbin start`/setup-dns on the work machine.
All verification via ISOLATED compose project. Round 2 identified the real
isolation blockers: 45 hardcoded `container_name`s, fixed subnet
10.100.0.0/16 (+ static ipv4_address on asterisk-*), and host-network /
host-port services (kamailio, rtpengine, coredns :53). Isolation strategy:
a test override + rendered config (`docker compose -p voipbin-test -f
docker-compose.yml -f docker-compose.test.yml config`) that (a) nulls all
container_names, (b) swaps the subnet and strips static IPs, (c) excludes
kamailio/rtpengine/coredns/asterisk-* (host-mutating or host-port services).
T2-T4 and T6-T10 run on this subset; T5 reports excluded services as
documented N/A. T9 runs in a SCRATCH CLONE whose git origin points to a
local bare-repo fixture (so `update scripts` git pull pulls the synthetic
next-pin commit, never the real origin).

| # | Test | Pass criteria |
|---|---|---|
| T1 | compose config lint (`docker compose config -q`) | exit 0 |
| T2 | infra restart: `docker kill` db/redis/rabbitmq | all three auto-restart, healthy again |
| T3 | Redis persistence: SET key → kill+restart → GET | key survives |
| T4 | RabbitMQ offline plugin: `docker compose up -d --force-recreate rabbitmq` with the container's network egress blocked | starts, plugin listed (plain restart would pass even today via the existence check — recreate is the failing mode) |
| T5 | healthcheck rollout: `compose ps` | every service shows health state or documented N/A |
| T6 | migrate.sh on empty MySQL | alembic head on both DBs, exit 0, no host pip used |
| T7 | backup: run with services up | dump+tars+manifest exist, gunzip -t passes, mysqldump restorable into scratch db container |
| T8 | restore: wipe db volume, restore from T7 | row counts match pre-backup snapshot |
| T9 | pin-advance simulation: create a synthetic "next pin" fixture (edit one service's compose digest to a second known-good tag + bump versions.lock in a scratch git commit), run `update all` | backup taken first, git pull applies fixture, only that container recreated, verify passes; then `update all --check` shows no-change |
| T9b | `voipbin rollback` on pinned repo | refuses with pinned-repo guard message |
| T10 | log rotation: inspect effective compose config | max-size present on all services |

**Execution status (Iteration 1, honest accounting):** implemented and run
live: T1-T4 (+T4b plugin-enabled + T4c no-redownload), T6 (migrate.sh on empty
MySQL: both streams to head, 61+27 tables), T7/T8 (backup/restore round-trip
incl. FLUSHALL and stopped-guard firing), T10, plus B1-B6 behavioral contracts
for the upgrade flow (test_upgrade_flow.py). NOT yet implemented as live
tests: T5 (healthcheck rollout across the full 44-service stack — requires a
full stack bring-up; deferred to the full-stack smoke in Iteration 2), T9/T9b
live pin-advance with a bare-repo fixture origin (B1-B3 cover the same
contracts at unit level; the live scratch-clone scenario is Iteration 2).

## 5. Risks & Trade-offs

- Healthcheck feasibility depends on image contents (mitigated: per-image verify
  step; worst case = no healthcheck, same as today).
- mysqldump on a large DB pauses nothing (single-transaction) but consumes IO;
  acceptable for single-server scale. PITR/binlog is out of scope (documented).
- Restore is destructive by design; guarded by --force + stopped-services check.
- Compose `up -d` after pull recreates ALL changed containers at once (brief
  full-stack blip on major upgrades). Per-service ordered recreate is a P1
  refinement; RabbitMQ buffering bounds the impact (research: RPC queues).

## 6. Alternatives Considered

- k3s single-node: keeps rolling updates + sentinel, but reintroduces kubectl
  operational burden and media hostNetwork issues; contradicts the "simpler
  than k8s" goal. Rejected for this track (recorded 2026-07-04 discussion).
- Separate "production" repo fork: splits maintenance, drifts from sandbox.
  Rejected: single repo, production-grade defaults benefit eval users too.

## 7. Open Questions

1. RabbitMQ plugin pinning: exact .ez version compatible with the pinned
   rabbitmq image tag — resolve at implementation (inspect current image tag).

**Accepted dependencies (recorded for transparency):**
- migrate.sh runs `pip install` at migration time — a network dependency at
  upgrade time. Accepted because `update all` already requires the network
  (git pull + docker pull); unlike the monorepo fetch (which has the
  VOIPBIN_MONOREPO_DIR offline fallback) there is no pip fallback. A
  pre-built migration image would remove this (Iteration 2 candidate).
- Recording restore is asymmetric when the volume did not exist at backup
  time: the archive is skipped on backup, so restore leaves current
  recordings in place (conservative direction, documented).

(Former OQ2 — whether bin-* images carry wget — was resolved in Round 2:
they are alpine/bookworm based, wget available; §3.2 records the per-digest
confirmation step.)

## 8. Follow-ups (Iteration 2+ candidates)

- Release channel: maintainer tooling to advance versions.lock + compose
  digests atomically (lock → compose render).
- Let's Encrypt + real-domain path (drop CoreDNS/resolv.conf hijack).
- Secrets: generated per-install passwords, non-root DB user.
- docker-events sentinel bridge (contract-validated).
- Resource limits from measured footprints; Prometheus+alerting compose profile.
