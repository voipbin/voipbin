# VoIPBin Sandbox → Production-Grade, Horizontally-Scalable Deployment Path

Status: DRAFT (Design Review Round 0)
Author: Hermes (CPO) with pchero (CEO/CTO)
Date: 2026-07-05
Branch: NOJIRA-Production-grade-horizontal-scale-architecture

## 0. Mandate (from pchero, verbatim intent)

1. Sandbox is no longer an "evaluation demo". It becomes VoIPBin's **primary, supported
   production deployment path**.
2. The GCP install repo (Terraform GKE+CloudSQL, Ansible GCE) is demoted over time to a
   secondary/reference path. Sandbox-derived Compose becomes the default users are told
   to run.
3. The architecture must not block **future horizontal (multi-node) scale-out**, even
   though this cycle ships a single-node artifact. "Does not block" = concrete structural
   choices now (address externalization, no baked-in single-host assumptions), not a
   promise to revisit later.
4. Scope of this cycle: design → design review loop (≥3 rounds) → full implementation →
   PR review loop (≥3 rounds, until convergence). "될때까지" — no partial stop at design-only.

## 1. Where we start (verified facts, not re-derived)

### 1.1 Sandbox today (main @ b4bcd1e, PR #8 merged)

Already shipped: pinned-image upgrade automation (`update all`: backup → git pull →
compose pull → migrate → up → verify), backup/restore (mysqldump, volume archive,
retention), 40/44 services have healthchecks, containerized alembic migration
(`migrate.sh`). This is the "single box, one update command" baseline. This cycle
builds ON TOP of it, not instead of it.

### 1.2 Structural blockers to multi-node scale-out (full inventory, file:line-verified)

| Layer | Blocker | Evidence |
|---|---|---|
| Network | Fixed bridge subnet `10.100.0.0/16`, not routable across hosts | docker-compose.yml:1380-1385 |
| Network | 7 services pinned to static `ipv4_address` (4 removed in §3.2 Phase 1: api-manager, square-admin/meet/talk; 3 removed in §3.2 Phase 3 together with dispatcher-list generation: asterisk-call/registrar/conference — see §3.2 for why these split across phases) | compose:225,363,296,498,1302,1329,1355 |
| Network | Kamailio↔Asterisk routing hardcoded to those static IPs | compose:185-186,229 |
| Network | `network_mode: host` on kamailio, rtpengine; `service:<asterisk>` netns-sharing on 3 proxy sidecars | compose:124,164,255,322,391 |
| Network | Host-level macvlan interfaces created by `setup-voip-network.sh` to bridge host-net services into the compose subnet (10.100.0.200/201) | scripts/setup-voip-network.sh:23-24,242-243 |
| Network | `.voipbin.test` domain resolved by hijacking `/etc/resolv.conf` to a CoreDNS singleton | scripts/setup-dns.sh:37-59, compose:104-108 |
| Identity | 44/44 services have hardcoded `container_name`; `docker compose up --scale` is structurally impossible as-is | compose entire file |
| Identity | Several ops scripts `docker exec <hardcoded-name>` directly (init_database.sh, start.sh, setup_test_customer.sh, voipbin-cli.py) | scripts/*.sh, scripts/voipbin-cli.py:54-57 |
| State | MySQL/Redis/RabbitMQ are single instances, no clustering; RabbitMQ `hostname: voipbin-rabbitmq` fixed (mnesia path keyed to it) | compose:16-88,56 |
| State | Kamailio reaches DB/Redis via `127.0.0.1` (host-net side effect) | compose:180,193 |
| Storage | 8 named volumes, local driver only, no shared/networked FS; call/conference recordings are node-local until GCS upload | compose:1369-1377, 236-237,283-284 |
| Env | `.env` bakes in single-host assumptions (`HOST_EXTERNAL_IP`, `KAMAILIO_EXTERNAL_IP`, `RTPENGINE_EXTERNAL_IP`, per-frontend `*_EXTERNAL_IP`) | .env.template, scripts/init.sh, scripts/common.sh |

**One-line verdict (unchanged from research)**: the **app layer (bin-* , 26 services)**
is already scale-out-safe (RabbitMQ RPC + shared DB/Redis addressing, no peer-to-peer
service addressing). The **state layer** needs address externalization + HA design. The
**media layer** carries the real coupling — but see 1.3, it is less severe than initially
assumed.

### 1.3 Correction that materially changes the roadmap: media layer already has the multi-instance mechanism, just not wired up in sandbox

- Kamailio's `dispatcher` module and `ds_select_dst()` calls already exist in the
  **production** kamailio.cfg (voip-kamailio-docker/templates/kamailio.cfg:105,170-175,
  1051,1057) — Asterisk call/registrar groups are dispatch targets, not single IPs, by
  design.
- RTPEngine already supports **multiple sockets** via `rtpengine_sock` /
  `RTPENGINE_SOCKS` (space-separated list) — kamailio.cfg:158,165; production Ansible
  group_vars documents the multi-IP format (`udp:<ip1>:22222 udp:<ip2>:22222`)
  (voip-kamailio-ansible/inventory/group_vars/kamailio.yml:23-24).
- **What's actually missing**: production Ansible currently flattens this to ONE LB IP /
  ONE socket string per group (`asterisk_call_lb_ip`, `rtpengine_socks: ""` scalar) —
  i.e. even the GCP path doesn't exercise true multi-instance media today. Sandbox
  needs to adopt the SAME dispatcher/multi-socket config shape, populated from a
  Compose-native scale unit, not invent a new mechanism.
- Consequence for the roadmap: media-layer scale-out is a **config/wiring** problem
  (dispatcher.list generation + multi-socket string generation from N asterisk-call /
  N rtpengine replicas), not a Kamailio/RTPEngine redesign. Still Phase-3-or-later
  because it is genuinely the hardest layer (host-net, RTP ports, recording storage),
  but it is not the wall it first appeared to be.

### 1.4 State-layer consumer-safety verification (5-6 representative bin-* services)

- Standard request/subscribe queues are `durable=true, exclusive=false,
  autoDelete=false` (bin-common-handler/pkg/rabbitmqhandler/queue.go:53) — safe for N
  competing consumers, standard RabbitMQ work-queue semantics. No process-local cache
  found in the consume path of call-manager (cache is Redis-backed,
  pkg/cachehandler/handler.go:184); email/tts/storage-manager use **redsync** distributed
  locks already, i.e. designed for concurrency.
- **One real gap found**: `bin-billing-manager` (main.go:145-158, monthly token top-up)
  and the failed-event retry loop (main.go:206-219), plus `bin-ai-manager` (main.go:167),
  run `time.Ticker`-based periodic jobs with **no leader election or distributed lock**.
  Running N replicas of these two services today would duplicate top-ups / retries.
  Round-1 review correctly caught self-contradictory framing here ("named exception to
  the no-premature-hardening policy" followed by an action that is, in substance,
  identical to the policy's normal gating). Resolved: this is treated as an ordinary
  application of pchero's standing policy, not an exception. Concretely, in-scope THIS
  cycle (Track A) is only a **startup-time guard**: if `bin-billing-manager` or
  `bin-ai-manager` is started with replica count >1 (detectable via the same
  `docker compose ps` scale check used elsewhere in this design), log a loud warning
  naming the duplicate-execution risk and the two affected jobs. The actual fix
  (redsync distributed lock around `runMonthlyTopUp` / the retry loop) is explicitly
  OUT of this cycle, deferred until an operator actually requests N>1 for either
  service — at which point it becomes a real, not theoretical, requirement and gets
  built then. No "exception" framing needed; the guard is the entire Track A
  commitment here.
- Ordering: no production code path found that assumes RabbitMQ FIFO strictly (audio
  streaming and AI message deltas already use sequence numbers, not queue order).

## 2. What "GCP install replacement" requires (gap inventory, sandbox vs install)

Priority order (highest gap first), all cross-verified against sandbox main:

1. **Secrets**: install uses SOPS + GCP KMS with a 31-key enforced schema; sandbox uses
   plaintext `.env` (present even in backup archives). Largest gap.
2. **TLS lifecycle**: install auto-renews (30-day threshold) + supports BYO
   Let's Encrypt/PKI (`manual` mode); sandbox only does a one-shot 365-day
   self-signed/mkcert cert with no renewal, no real-CA mode.
3. **DB backup**: install has scheduled daily CloudSQL backups + binlog PITR + offsite
   retention; sandbox (PR #8) only has manual/pre-upgrade backup, no schedule, no PITR,
   no offsite copy.
4. **Monitoring/alerting**: install has node_exporter, kamailio_exporter, full Homer/HEP
   SIP capture (ClickHouse-backed), GCP Cloud Logging/Monitoring; sandbox has compose
   healthchecks only (PR #8), no metrics scrape target, no dashboards, no alert channel.
   NOTE: install itself has no self-hosted Prometheus/Grafana/Alertmanager either — it
   leans on GCP Cloud Monitoring. Sandbox replacing GCP must ADD a self-hosted stack,
   not just port install's.
5. **Public DNS / firewall**: install provisions real DNS A-records + firewall rules;
   sandbox is `.voipbin.test` + resolv.conf hijack only, not documented for a real
   domain.
6. **HA / DB**: install supports CloudSQL `REGIONAL` HA; sandbox is a single MySQL
   container with no replica.

This list (not the horizontal-scale question) is what actually blocks "sandbox becomes
the default production path" — it is a distinct axis from horizontal scale-out and
must be tracked as its own phase, not conflated with network/identity rework.

## 2.1 Track split (post Design Review Round 1)

Round 1 review correctly caught that this document was conflating two independent
axes under one branch/PR-loop: horizontal-scale enablement (network/identity
addressing) and production-hardening/install-parity (secrets/TLS/backup/monitoring).
These require different expertise (distributed networking vs security/ops) and
bundling them risks the same reviewer-timeout failure mode already seen once in this
cycle (a 4-axis review died at 600s). Splitting now, before implementation:

- **Track A — Horizontal-scale enablement** (this document, this branch
  `NOJIRA-Production-grade-horizontal-scale-architecture`): §1, §3.1, Phase 1
  (address externalization) + Phase 3 (media multi-instance wiring). Two PRs, each
  with its own ≥3-round review loop, run sequentially in this cycle.
- **Track B — Production hardening / install-parity** (separate design doc + branch,
  opened as a follow-on immediately after Track A Phase 1 merges, so Track B's DB/Redis
  address changes build on Track A's externalized addressing rather than fighting it):
  what was Phase 2 below (secrets, TLS, backup, monitoring, DNS docs). Tracked as its
  own item in this cycle's overall scope (mandate said "구현까지 이번 사이클에 끝낸다" —
  interpreted as: both tracks ship before this cycle is called done, but as two
  independently-reviewed PR sequences, not one).
- Phase 4 items previously listed here are now assigned directly: the
  billing/ai-manager ticker item belongs to Track A (it's a scale-out correctness gap,
  covered as a startup guard in §3.2 Phase 1); Homer/HEP belongs to Track B (it's an
  install-parity monitoring gap, covered in the separate Track B doc).

This section supersedes any single-PR reading of §0.4; the mandate's "PR review loop"
means one *complete* loop per track/phase, run sequentially, not one loop covering
everything.

## 3. Target architecture

### 3.1 Layered Compose profiles, not a new orchestrator

Kept from the already-approved Option A rationale (voipbin-single-server-production
skill): reject k3s/k8s re-introduction. The media stack is deliberately outside k8s
even in production today (host-networking incompatibility). Extend the SAME Compose
model with **role-based profiles** so a single repo checkout can run:

- `profile=all` (today's default): everything on one box — unchanged sandbox/eval UX.
- `profile=app`: the 26 bin-* services + square-* frontends only, pointing at
  externally-supplied `DB_HOST`, `REDIS_HOST`, `RABBITMQ_HOST`.
- `profile=state`: MySQL + Redis + RabbitMQ only, bound to externally-reachable
  addresses instead of `127.0.0.1`/docker-internal DNS only.
- `profile=media`: Kamailio + RTPEngine + Asterisk trio + proxies, unchanged internal
  wiring, but externalized DB/Redis addressing.

This is additive: `profile=all` stays the one-command eval/dev path (no regression);
`app`/`state`/`media` are new opt-in split points a second box can adopt later. No
operator is forced to split anything to keep running today's workflow.

### 3.2 Concrete changes, phased (each phase = one PR, its own review loop)

**This document (branch `NOJIRA-Production-grade-horizontal-scale-architecture`) covers
Track A only: Phase 1 and Phase 3 below. Phase 2 (install-parity hardening) moves to a
separate Track B design doc per §2.1 and is out of scope for this branch's PRs.**

**Phase 1 — Address externalization (removes the structural blocker, ships as N=1 default)**
- Replace static `ipv4_address` pins with Compose service-name DNS **for the 4 services
  where this is safe and sufficient**: api-manager, square-admin, square-meet,
  square-talk. These are only referenced by browser-facing URLs/iptables port
  forwarding, never by another compose service's hardcoded IP — verified against
  compose:498,1302,1329,1355 and no cross-references found elsewhere in the file.
- **Round-2 review correctly caught a gap here**: the 3 Asterisk static IPs
  (asterisk-call/registrar/conference, compose:225,363,296 → 10.100.0.210/.211/.212)
  are NOT safe to remove in Phase 1 alone, because Kamailio hardcodes them as routing
  targets (`ASTERISK_CALL_LB_ADDR`/`ASTERISK_REGISTRAR_LB_ADDR`, compose:185-186,229),
  AND Kamailio runs `network_mode: host` (compose:164) — it never joins the compose
  bridge network, so it structurally cannot resolve compose service-name DNS
  (127.0.0.11) at all. "Replace with service-name DNS" is not an available option for
  this specific link, at any phase. Resolution: the 3 Asterisk static IPs stay exactly
  as they are in Phase 1 (no change, no regression) and are superseded — not replaced
  by DNS, but by the **dispatcher-list mechanism already scoped for Phase 3** (§3.2
  Phase 3 below), which is the real, already-proven-in-production answer to
  Kamailio-can't-use-compose-DNS: `dispatcher.list` is a flat-file Kamailio reads at
  startup/reload, generated by a script from however many Asterisk replicas exist,
  independent of compose DNS entirely. This means Phase 1 and Phase 3 are more tightly
  coupled than originally scoped: Phase 1 ships the 4 safe IP removals + container_name
  cleanup; the Asterisk static-IP removal is explicitly MOVED into Phase 3 where the
  dispatcher-list generator that actually replaces it lives, rather than left as a
  dangling Phase-1 promise it can't keep alone.
- Kamailio's own static IP (10.100.0.200, macvlan-side) is kept — documented exception,
  not removed, for the same host-network reason plus SIP loop avoidance.
- Remove the 44 hardcoded `container_name`s from service definitions used only for
  human convenience (`docker compose ps` already shows service names); keep the small
  subset genuinely read by ops scripts, and refactor those scripts to resolve the name
  via `docker compose ps -q <service>` / config lookup instead of a literal.
- Externalize DB/Redis/RabbitMQ endpoints via `.env` (`DB_HOST`, `REDIS_HOST`,
  `RABBITMQ_HOST` already mostly exist as compose-internal DNS names — make them
  operator-overridable to a non-localhost value) so the `state` profile can live on a
  separate host later without code changes, only `.env` changes.
- Fix Kamailio's two `127.0.0.1` DB/Redis references (compose:180,193) to use the
  externalized host vars — currently only valid because of `network_mode: host`.
- Add the billing-manager/ai-manager replica>1 startup guard described in §1.4.

**Phase 3 — Media layer multi-instance wiring (adopts existing Kamailio/RTPEngine mechanism)**
- Now includes, in addition to the original scope: removing the 3 Asterisk static
  `ipv4_address` pins (moved from Phase 1 per the resolution above) as part of
  generating `dispatcher.list` from N replicas — the static IPs and the dispatcher
  mechanism are replaced together in one PR since they're the same routing path.

- Generate `dispatcher.list` and `RTPENGINE_SOCKS` from however many `asterisk-call`/
  `rtpengine` replicas are declared (Compose `deploy.replicas` equivalent for standalone
  compose: parameterize via `.env` count + a small generator script, since plain
  `docker compose up --scale` can't be used while `container_name`/static IP survive on
  those specific services — Phase 1 must land first).
- Recording storage: make GCS (or S3-compatible) upload the default rather than an
  optional path, so recordings are never node-pinned even at N=1.
- Explicitly OUT OF SCOPE this cycle: actual multi-HOST media deployment (that needs
  routable networking across hosts, a separate infra decision) — Phase 3 ships the
  config-generation mechanism and multi-replica-on-one-host, proving the wiring works,
  without requiring a second physical/virtual machine.

**Track A follow-ups, deferred with reasons (not silently dropped, not shipped this branch)**
- billing-manager/ai-manager ticker duplication: the startup guard ships in Phase 1
  (§1.4); the actual distributed-lock fix is deferred until an operator requests N>1
  for either service — ordinary application of the no-premature-hardening policy, not
  an exception.
- True multi-HOST deployment topology (routable overlay network, cross-host storage
  for recordings, RabbitMQ/MySQL/Redis actual clustering): this design makes it
  POSSIBLE without further rework, but does not ship it. Tracked as future work beyond
  both tracks of this cycle.

**Track B (separate design doc) will cover**: secrets (sops+age), TLS lifecycle,
scheduled backup, monitoring/alerting stack, public DNS runbook, Homer/HEP (deferred
within Track B itself, noted not hidden). Listed here only so the split is traceable;
full design lives in the Track B doc once opened.

## 4. Non-goals (explicit, to prevent scope creep mid-review)

- NOT re-introducing k3s/k8s. Rejected already (rationale in
  voipbin-single-server-production skill); media stack incompatibility argument still
  holds.
- NOT shipping actual cross-host clustering of MySQL/Redis/RabbitMQ this cycle — only
  removing the structural blockers (hardcoded localhost/container-name assumptions) so
  a future cycle can add it without a rewrite.
- NOT changing the default one-command `./scripts/start.sh` eval experience. `profile=all`
  remains identical UX.
- NOT fixing billing-manager/ai-manager ticker duplication with an actual distributed
  lock in this branch — only the startup guard (§1.4, §3.2 Phase 1). The real fix is
  ordinary policy-gated future work, not an exception.
- NOT covering Track B (install-parity hardening: secrets/TLS/backup/monitoring/DNS) —
  moved to a separate design doc and branch per §2.1.

## 5. Honest accounting (what this branch/Track A does NOT achieve)

The mandate says "GCP install을 대체" as the long-term goal, requiring both tracks.
This document (Track A) closes the address-externalization blocker and wires up media
multi-instance CONFIG on a single host. It does NOT deliver: actual multi-host
clustering, DB HA/replication, or any of the install-parity items (secrets, TLS,
backup, monitoring, DNS) — those are Track B, tracked separately and not hidden from
the overall "GCP replacement" claim. Track B must ship before the overall mandate is
complete; this document only claims completion of Track A.

## 6. Risks / tradeoffs (A/B framing per pchero's standing preference)

**A. Ship Phase 1 and Phase 3 as two sequential PRs (this document's plan)** vs **B. Ship
both phases in one PR**
- A: smaller review surface per PR, Phase 1 alone already delivers the "not blocked"
  goal even if Phase 3 slips; Phase 3 (media) is higher-risk (host networking) and
  benefits from being isolated for revert-safety. Phase 1 must merge first regardless,
  since Phase 3's replica generation needs Phase 1's container_name/static-IP removal.
- B: one coherent "Track A done" milestone, but a media-layer regression blocks
  address externalization too, and a combined review re-creates the four-axis
  timeout risk already hit once this cycle.
- Recommendation: **A**.

**A. sops+age for secrets (Track B)** vs **B. keep plaintext .env, document the risk only**
- Deferred to the Track B design doc — out of scope for this document's review loop.
  Recorded here only as a pointer so Round 1's finding isn't lost.

Track A (this document) is now scoped to Phase 1 + Phase 3 only, sequential PRs.
Proceeding to Design Review Round 2 on this narrowed scope.
