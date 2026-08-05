# VoIPBin Sandbox — Full Service Parity With Monorepo (versions.lock Advance, direct/webchat Onboarding, PostgreSQL+pgvector, ClickHouse, storage-manager Fix, compose↔lock Sync)

Status: APPROVED (Design Review Rounds 5-6: two consecutive APPROVE, review loop closed)
Author: Hermes (CPO) with pchero (CEO/CTO)
Date: 2026-07-31
Repo: sandbox (one monorepo-side fix, §2.6, lands as its own monorepo PR; everything else lands here)

## 0. Mandate

Continuation and expansion of `2026-07-30-sandbox-install-reliability-design.md`. That
cycle's Phase A (call-manager/timeline-manager sentinel-subscribe fix, transcribe-manager
STT degradation) shipped and was verified sound against real registry images. But its
Phase C clean-room verification of the versions.lock refresh **failed** with findings that
revealed a structural problem: the sandbox's service catalog and infrastructure have not
tracked the monorepo. pchero's direction: **stop patching around drift; bring the sandbox
fully current with today's monorepo in one cycle** — "기존의 sandbox 내용은 현재의
voipbin 내용과 많이 안맞아. 이를 수정하는 것도 포함해. 최신의 voipbin 내용을 담을 수
있어야 해." Everything ships together as one cycle (explicitly confirmed), except the one
service that is architecturally impossible in Compose (§4).

## 1. Findings driving this design (all verified this cycle)

### 1.1 Phase C failure findings (from the 2026-07-30 clean-room run against the refreshed lock)

| # | Finding | Class |
|---|---|---|
| F1 | `docker-compose.yml` hardcodes image digests independently of `versions.lock`; nothing syncs them. The regenerated lock had zero effect on what actually runs. First Phase C run reproduced the ORIGINAL pre-fix crashes because compose was still pinned to pre-fix digests. | Missing mechanism (this design's §2.2) |
| F2 | With digests hand-synced, the Phase A fixes held: call-manager RestartCount 0, transcribe-manager degrades with a warning instead of crashing, timeline-manager steady at RestartCount 0 over a 3-minute watch. | Positive — Phase A is sound |
| F3 | `rag-manager` and `storage-manager` crash-loop at the new pin (GCP_PROJECT_ID / GOOGLE_APPLICATION_CREDENTIALS now fatal at boot). | §1.2/§1.3 below — two DIFFERENT root causes |
| F4 | `setup_test_customer.sh` broken at the new pin: agent-manager now RPCs `bin-manager.direct-manager.request` during agent creation; no direct-manager exists in sandbox; `EventCustomerCreated` times out; no admin agent is created. | Service-catalog drift (§2.3) |

### 1.2 storage-manager — a genuine monorepo regression (fix in monorepo, §2.6)

Commit `1f9002953` (2026-07-08, VOIP-1229, #1060) removed the GKE workload-identity
signing fallback and, in the same change, made a missing `GOOGLE_APPLICATION_CREDENTIALS`
fatal at boot (`bin-storage-manager/pkg/filehandler/main.go:93-100`, constructor now
returns an error that kills startup). Before that commit there was a three-tier fallback
(env key file → ADC/metadata → env service-account email) and no fatal path.

**Signing-dependent surface, corrected (Round-3 review caught an earlier draft claiming
"only signed download URLs" — the real set is wider)**: `h.privateKey` has one consumer,
`bucketfileGenerateDownloadURI` (`pkg/filehandler/bucketfile.go:141`, existing nil-guard
at `:158-160` returning a bare unstructured error), but that helper has THREE call sites:
`DownloadURIGet` (`downloaduri.go:29`), `DownloadURIRefresh` (`download.go:32`), and —
critically — **inside `fileHandler.Create` itself** (`file.go:81`, error returned at
`:84`, and by that point `bucketfileMove` at `:70` has already relocated the GCS object,
so an aborted Create also orphans it; `URIDownload` is a required field of the persisted
record, `file.go:108`). Two more paths inherit the dependency through `DownloadURIGet`:
`pkg/storagehandler/compressfile.go:52` and `pkg/storagehandler/recording.go:40`. So
without a usable signing key, the degraded set is at least: signed URL get/refresh,
compress-file download, recording fetch, AND file Create (the primary write path) — not
"one capability". What genuinely does NOT need the key: file Get/List/Delete, account
bookkeeping, customer-deleted cascade.

Even so, the boot-kill remains an overshoot — read-side RPC, deletion, and account
bookkeeping all work keyless, and a service that can serve those while clearly erroring
on the signing-dependent paths beats a crash loop. Same shape as the transcribe-manager
STT fix, with the Create-path behavior now explicitly specified (§2.6).

### 1.3 rag-manager — not a regression; the service was rewritten (sandbox must catch up, §2.4)

Between the old pin (2026-02-21) and HEAD, rag-manager was re-founded: OpenAI embeddings +
GCS `.gob` blob storage → **Vertex AI embeddings + PostgreSQL/pgvector** (commits
`7f12266d3` 2026-03-17 postgresql-foundation #695, `daca8c74f` #706, `0124a0e40` #713
which added the strict `Validate()`: `RABBITMQ_ADDRESS`, `GCP_PROJECT_ID`, `GCP_REGION`,
`POSTGRESQL_DSN` all required — list at `internal/config/config.go:101-115`, call site `cmd/rag-manager/main.go:86-88`). There is exactly one
embedder implementation at HEAD (`embedder.NewGoogleEmbedder`, `main.go:186`) — no
fallback exists to degrade to, so a transcribe-style monorepo softening is NOT appropriate
here; booting without GCP would yield a service that accepts ingestion and fails 100% of
requests. The sandbox's entire current rag-manager compose block (env vars
`OPENAI_API_KEY`, `GCS_EMBEDDINGS_PATH`, etc.) references a config surface that no longer
exists. rag-manager at HEAD needs: PostgreSQL with the `vector` extension (its
`migrations/000001_create_rag_tables.up.sql` opens with `CREATE EXTENSION IF NOT EXISTS
vector;`, applied by golang-migrate at startup, `main.go:114-147`), a GCS client, and real
GCP credentials for actual embedding work. Decision (pchero, this cycle): include it —
sandbox adds PostgreSQL+pgvector (§2.4); real GCP credentials remain the operator's to
supply, with the boot-versus-function distinction documented (§2.4).

### 1.4 Service-catalog audit (monorepo main vs sandbox docker-compose.yml)

34 distinct `bin-*` service directories at monorepo HEAD. Sandbox deploys 29. The five absent:

| Service | Verdict |
|---|---|
| `bin-dbscheme-manager` | Correctly absent — migration tooling, not a runtime service; sandbox already consumes it via `scripts/migrate.sh`. |
| `bin-openapi-manager` | Correctly absent — codegen tooling, no `cmd/`, nothing to run. |
| `bin-sentinel-manager` | Stays absent — architecturally impossible in Compose (§4). |
| `bin-direct-manager` | **Must be added** (§2.3) — added to monorepo 2026-03-25; agent-manager now hard-depends on it during agent creation (F4). |
| `bin-webchat-manager` | **Must be added** (§2.3) — added 2026-07-16; depends on direct-manager (its widgets carry a `direct_hash` column, migration `4dd9760302b8`). |

### 1.5 Infrastructure-catalog gaps

- **PostgreSQL + pgvector**: required by rag-manager at HEAD (§1.3). Not present.
- **ClickHouse**: timeline-manager's storage backend (sole ClickHouse consumer in Go code,
  verified last cycle). Currently absent; timeline-manager boots but logs a
  ClickHouse-ping error every 30s and its entire write path is dead — a silent degradation
  flagged (and deferred) in last cycle's design. pchero: include it now.
- The two dead `CLICKHOUSE_ADDRESS` compose vars on call-manager/flow-manager (leftover
  pre-centralization config, flagged last cycle) get cleaned up while we're in the file.

### 1.6 The dbscheme pin advance is destructive — this is the riskiest single element

Advancing `dbscheme_monorepo_commit` past 2026-03-25 applies migration `08d686855c69`
("generic direct hash"), which is not additive: it creates `direct_directs`, adds
`direct_id`/`direct_hash` columns to five existing tables (`registrar_extensions`,
`conference_conferences`, `ai_ais`, `ai_teams`, `agent_agents`), backfills
`registrar_extensions` from `registrar_directs` (the other four tables get empty
`direct_id`/`direct_hash` — by design, they had no prior direct records; and the backfill
INSERT is `WHERE tm_delete IS NULL`, so soft-deleted rows are dropped rather than
migrated), then **drops `registrar_directs`**. Plus seven webchat migrations
(2026-07-16..22). Consequence: after the pin advance, ALL service images must move to the
new pin together — mixing old images against the new schema (or vice versa) is not
supported.

**Honest accounting of the two upgrade paths (Round-1 corrected an earlier draft that
cited a nonexistent `scripts/update.sh` and overclaimed "no mixed-schema window")**:

- **Fresh install (the §3 clean-room path)**: `down -v` → migrate → up. Truly no
  mixed-schema window; this is the path §3 proves.
- **Existing-install upgrade**: the real flow is `voipbin update all` →
  `VoIPBinCLI._upgrade_pinned` (`scripts/voipbin-cli.py:~4897`): backup → git pull →
  re-exec → compose pull → **migrate → up -d**. Between `migrate` and `up -d`, the OLD
  containers are still running against the NEW schema — for this specific migration, that
  means every old service runs against a database where `registrar_directs` has already
  been dropped, for the duration of that gap. Additional exposure: (a) if `up -d` fails
  partway, the operator is left with old images on new schema and the pre-upgrade backup
  as the only recovery; (b) MySQL DDL is not transactional, and this migration is
  CREATE → 5×ALTER → INSERT → UPDATE → DROP — a mid-migration failure leaves a
  half-migrated schema (the upgrade flow's existing abort-before-up behavior, verified in
  `scripts/tests/test_upgrade_flow.py`, correctly stops before `up -d` in that case, but
  that still leaves old containers on a half-new schema until restore).

Mitigations, honestly weighted: the pre-upgrade backup (existing, automatic in
`_upgrade_pinned`) is the real safety net for all of the above; the mixed-schema gap is
seconds-to-minutes on a sandbox-scale DB; and the backfill logic is monorepo-authored and
already ran in production. Considered and NOT adopted this cycle: reordering the upgrade
flow to stop-managers → migrate → up (eliminates the gap, but changes long-standing
upgrade semantics for every future upgrade and deserves its own change + tests, not a
rider on this cycle — recorded as follow-up work in §4).

## 2. Scope

### 2.1 versions.lock advance (carried over from last cycle's Phase B, now shippable)

Regenerate against monorepo main HEAD via the existing `scripts/generate-versions-lock.sh`
(already merged), **extended first** to also track the two new images
(`voipbin/bin-direct-manager`, `voipbin/bin-webchat-manager`): the generator currently
iterates the lock's existing `images` keys only, so seeding two new entries into the lock
is the mechanism — BUT (Round-1 blocking finding) the generator's fallback semantics
make naive placeholder-seeding dangerous: an unresolvable image today falls back to
"keep current pin, warn, exit 0", so a seeded placeholder that fails to resolve would be
written back into the lock as a legitimate-looking pin, §2.2 would propagate it into
compose, and the failure would surface only at `docker compose pull` on a customer
machine. **Required generator change**: distinguish seeded/new entries (e.g. digest value
`"NEW"` or a dedicated `new_images` input list) from established pins — a seeded entry
that cannot be resolved at the target commit is a HARD ERROR (non-zero exit, lock not
written), never a fallback. Established entries keep today's fallback behavior. The
preflight registry probe must also SKIP seeded entries when choosing its probe image
(it currently probes `sorted(images)[0]` at its current pinned digest — a seeded
placeholder digest sorting first would fail the probe with a misleading
"registry unreachable" message; latent today since `bin-agent-manager` sorts first, but
the generator change closes it). `dbscheme_monorepo_commit` advances to the same target
commit (the generator already does this unconditionally — noted for completeness, not
new work).

### 2.2 compose↔lock sync mechanism (closes F1)

New script `scripts/sync-compose-images.sh`: reads `versions.lock`, rewrites every
`image: voipbin/<name>@sha256:...` line in `docker-compose.yml` to the lock's digest for
that image. Properties:
- Only touches `image:` lines whose repo matches a tracked `voipbin/*` name; third-party
  images (mysql, redis, rabbitmq, coredns, postgres, clickhouse) are never rewritten.
- **Multi-occurrence images (Round-1 required this be specified)**:
  `voipbin/voip-asterisk-proxy` appears at three `image:` lines in compose (the three
  `asterisk-*-proxy` services share one image) — the script rewrites ALL occurrences of a
  tracked image to the lock's digest; a repeated image is normal, not a duplicate-key
  error. Two distinct drift cases, one behavior: (a) compose-vs-lock drift — the actual
  current state: all three voip-asterisk-proxy compose lines agree with EACH OTHER but
  differ from the lock's digest; (b) within-compose divergence — hypothetical today, the
  same tracked image at two different digests across compose lines. In both cases every
  occurrence is rewritten to the single lock digest — that convergence is the point.
- Fails loudly (non-zero, names the image) if compose references a tracked image the lock
  lacks, or the lock tracks an image compose doesn't reference — EXCEPT images on an
  explicit lock-only allowlist hardcoded in the script with rationale per entry.
  Initial allowlist: `voipbin/bin-sentinel-manager` (permanently excluded from compose,
  §4, but deliberately kept pinned in the lock per the predecessor design's commitment —
  without this allowlist the bidirectional check would fail on every run forever,
  Round-1's finding).
- Idempotent; running it twice is a no-op the second time.
- `generate-versions-lock.sh` prints a reminder to run it, and §3's verification includes
  a check that compose and lock agree (run the sync script, assert zero diff).
- Rejected alternative: templating `docker-compose.yml` from the lock at start-time
  (compose `${VAR}` interpolation per image). Rejected because it makes the committed
  compose file non-runnable standalone and pushes the failure to first-boot on a customer
  machine; an explicit committed sync keeps `git diff` meaningful for image bumps.

### 2.3 Onboard direct-manager and webchat-manager

Both are pure RabbitMQ-RPC + MySQL + Redis services (no HTTP/websocket listener — verified
by reading both `cmd/*/main.go`s; webchat's user-facing websocket lives in api-manager).
Both read the identical seven env vars, all defaulted, no fatal validation
(`bin-direct-manager/internal/config/config.go:45-53`, webchat identical). Compose blocks
are modeled verbatim on the existing `tag-manager` block: same healthcheck
(`:2112/metrics`), same `depends_on` (db/redis/rabbitmq healthy), same DSN/address env
lines. direct-manager keeps a `container_name: voipbin-direct-mgr` (it ships a
`direct-control` CLI, matching the voipbin-customer-mgr precedent for ops-script
exec'ing); webchat-manager gets no container_name (no CLI). Ordering: direct-manager must
be schema-ready before agent creation happens; both new services ride the same
`depends_on` infra gates as every other manager, and the schema comes from the dbscheme
pin advance (§2.1) — no special sequencing beyond what `start.sh` already does
(migrate before up).

### 2.4 PostgreSQL + pgvector for rag-manager

- New compose infra service `postgres` using the `pgvector/pgvector:pg16` image (official
  pgvector build; pinning to a specific digest like the other third-party images), own
  named volume `postgres_data`, healthcheck via `pg_isready`. Service env:
  `POSTGRES_USER=voipbin`, `POSTGRES_PASSWORD=voipbin`, `POSTGRES_DB=rag` — the initdb
  user must be the superuser (which `POSTGRES_USER` is, by initdb) because rag-manager's
  first migration runs `CREATE EXTENSION IF NOT EXISTS vector`, which needs superuser.
  NOT published to the host — a deliberate divergence from db/redis/rabbitmq, which ARE
  host-published (Round-2 caught an earlier draft claiming "same posture as redis", which
  was inverted): those three predate this design and operators exec/debug against them;
  postgres is a single-consumer backing store for rag-manager, and keeping it unpublished
  shrinks surface. `bin-rag-manager/docs/operations.md`'s host-`psql` troubleshooting
  examples won't work against an unpublished port — sandbox docs will show the
  `docker exec` equivalent instead.
- rag-manager's compose block rewritten to HEAD's actual config surface:
  `RABBITMQ_ADDRESS`, `GCP_PROJECT_ID` (from `.env`), `GCP_REGION` (from `.env`, default
  `us-central1` in the template), `GCP_BUCKET_NAME_MEDIA` (from `.env` — Round-1 review
  caught this was dropped: it is threaded into `raghandler.NewRagHandler`,
  `cmd/rag-manager/main.go:192`, and listed as required in `bin-rag-manager/CLAUDE.md`;
  not `Validate()`-gated, but omitting it yields a booting service whose GCS ingestion
  points at an empty bucket name), `POSTGRESQL_DSN`
  (`postgres://voipbin:voipbin@postgres:5432/rag?sslmode=disable`), plus the existing
  dummy-GCP-credential pieces — BOTH the
  `GOOGLE_APPLICATION_CREDENTIALS=/tmp/google_service_account.json` env line and the
  volume mount, same both-pieces phrasing as §2.6 (Round-3 caught this list naming only
  "the mount") — and the existing Prometheus lines
  (`PROMETHEUS_ENDPOINT`/`PROMETHEUS_LISTEN_ADDRESS`, which the `:2112` healthcheck
  depends on — kept, not part of the "removed vars" cleanup). Old removed vars
  (`OPENAI_API_KEY` for rag, `GCS_EMBEDDINGS_PATH`, `RAG_DOCS_BASE_PATH`, etc.) deleted
  from the compose block AND from their `init.sh`/`.env.template` counterparts
  (the rag-specific set within `init.sh:370-376` / `.env.template:188-194`, EXCLUDING
  `RAG_TOP_K` at `init.sh:375`/`.env.template:193`, which is still live at HEAD
  (`bin-rag-manager/internal/config/config.go:29,54,65`) and stays: `GCS_BUCKET`,
  `GCS_EMBEDDINGS_PATH`, `RAG_DOCS_BASE_PATH`, `OPENAI_EMBEDDING_MODEL`, `RAG_LLM_MODEL`,
  `RAG_CHUNK_MAX_TOKENS`; a cycle whose mandate is ending drift shouldn't leave dead
  config in the very files it's syncing. `OPENAI_API_KEY` itself stays — other services
  still use it).
- **Validate()-gated variable fix, all three of them (Round-1 found the empty
  `GCP_PROJECT_ID`; Round-2 caught this design half-treating its own new `GCP_REGION`
  the same way)**: `scripts/init.sh:262` currently writes `GCP_PROJECT_ID=` (empty),
  while `.env.template:13` documents `your-gcp-project-id` — they disagree, and
  rag-manager's `Validate()` hard-fails on empty strings BEFORE any client construction.
  `GCP_REGION` is equally `Validate()`-required and is a NEW variable this cycle — adding
  it to the template alone (as an earlier draft did) leaves a freshly init'd `.env`
  without it, reproducing the same crash through a different variable. Fix, one mechanism
  for all three: **`init.sh` writes non-empty placeholder defaults**
  (`GCP_PROJECT_ID=sandbox-placeholder`, `GCP_REGION=us-central1`,
  `GCP_BUCKET_NAME_MEDIA=sandbox-placeholder-media`), and `.env.template` documents the
  identical values with a comment that they satisfy config validation without granting
  GCP access. Side effect worth naming: with `GCP_BUCKET_NAME_MEDIA` now non-empty,
  `RECORDING_BUCKET_NAME` on the two asterisk proxies (compose:291,359) and
  `PROJECT_BUCKET_NAME` on call-manager (:588) stop falling back to
  `voipbin-voip-media-bucket`, and storage-manager's own `GCP_BUCKET_NAME_MEDIA` (:1082)
  goes empty-to-placeholder — harmless (no sandbox deployment can reach either bucket),
  but no longer the production-looking default. init.sh and the template stay in
  agreement (the Round-1 requirement), and
  because init.sh writes `GCP_REGION`, no `check-env-template-sync.sh` exclusion-list
  entry is needed for it. The earlier framing ("maybe an eager client init parses the
  key") was the wrong failure mode — validation, not key parsing, is the first gate.
- rag-manager runs its own golang-migrate at startup against Postgres — no sandbox-side
  migration tooling needed; the compose `depends_on: postgres: service_healthy` gate is
  sufficient.
- **Boot vs. function, documented honestly**: with the placeholder project/region/bucket
  and dummy credential, rag-manager at HEAD passes `Validate()` and boots; actual
  embedding/ingestion calls fail at the Vertex AI / GCS call until the operator supplies
  real credentials. `.env.template` and CLAUDE.md document this explicitly: "rag-manager
  runs, but RAG ingestion/query requires a real GCP project + service-account key." If
  runtime verification (§3) nonetheless shows a boot failure (e.g. an eager GCS client
  init rejecting the dummy key), fallback is documented degradation: keep it deployed and
  document the real-credential requirement as boot-blocking. No `start.sh` change is
  needed for that fallback (Round-1 caught the earlier wording implying one):
  `start.sh:657` is a bare `docker compose up -d`, rag-manager is no service's
  `depends_on`, so a crash-looping rag-manager is already tolerated and merely appears in
  the startup warning list. Which branch applies is settled by evidence in §3.

### 2.5 ClickHouse for timeline-manager

- New compose infra service `clickhouse` using `clickhouse/clickhouse-server` (pinned
  digest), named volume `clickhouse_data`, healthcheck via its HTTP ping endpoint, not
  published to the host by default.
- timeline-manager's existing `CLICKHOUSE_ADDRESS=${CLICKHOUSE_ADDRESS:-}` line (docker-compose.yml:1155) changes its default: `CLICKHOUSE_ADDRESS=${CLICKHOUSE_ADDRESS:-clickhouse:9000}`
  (an overridable `:-` default, keeping `.env`'s `CLICKHOUSE_ADDRESS` a live control
  rather than dead config — Round-2 caught an earlier literal-value wording contradicting
  §2.7) and keeps `CLICKHOUSE_DATABASE=${CLICKHOUSE_DATABASE:-default}`. Port 9000/native
  protocol confirmed against source (`bin-timeline-manager/pkg/dbhandler/main.go` uses
  `clickhouse-go/v2` native; the DSN is built as `clickhouse://addr/db`).
- **`depends_on: clickhouse: {condition: service_healthy}` gate is REQUIRED, not
  optional (Round-2 blocking finding)**: once the address is set, timeline-manager's
  `runMigrations` always runs at boot, and a migration failure is fatal
  (`cmd/timeline-manager/main.go` returns the error → `os.Exit(1)`). Without the gate,
  timeline-manager racing ClickHouse to readiness exits non-zero on every fresh `up`;
  `restart: always` eventually recovers it, but §3 step 5's own ≥3-minute
  RestartCount-stability criterion would then fail or flake on a service it explicitly
  names. Same treatment §2.4 gives postgres, stated with the same explicitness.
- timeline-manager runs its own migrations at startup — no sandbox-side migration
  tooling.
- The dead `CLICKHOUSE_ADDRESS` vars on call-manager/flow-manager are removed in the same
  change (§1.5).

### 2.6 monorepo: storage-manager graceful degradation (separate monorepo PR)

Mirror of last cycle's transcribe-manager fix, same review loop: make the missing/invalid
signing credential non-fatal. `NewFileHandler` returns a working handler with
`privateKey == nil`. All signing-dependent READ paths enumerated in §1.2 return a
structured `*cerrors.VoipbinError` (status `Unavailable`, reason `SIGNING_NOT_CONFIGURED`)
instead of today's bare `errors.New` in `bucketfileGenerateDownloadURI`'s nil-guard —
that means `DownloadURIGet`, `DownloadURIRefresh`, and the storagehandler consumers all
surface the structured error (Round-3 caught an earlier draft that named only
`DownloadURIGet` and falsely claimed "every non-signing capability works normally", which
would have left the Create path returning the legacy unstructured error; Create itself is
handled separately below, hence "read paths" here).

**The degradation covers BOTH keyless and invalid-key deployments (Round-4 caught the
remedy being scoped to `privateKey == nil` only, while the rationale invoked a sandbox
failure mode where the key is non-nil but unusable)**: Create's tolerance (below) applies
to ANY `bucketfileGenerateDownloadURI` failure — the nil-key guard AND a sign-time
failure from `storage.SignedURL`. This matters because the sandbox's dummy key is
present-but-invalid, not absent: **empirically verified this cycle** — the dummy
credential's RSA key is structurally well-formed PEM but mathematically invalid, and Go's
`x509.ParsePKCS1PrivateKey` (which validates primality, unlike a raw openssl sign
operation) rejects it with `crypto/rsa: p * q != n`, so `storage.SignedURL` fails at its
parse step. Round-4 review speculated the dummy key would probably sign; the empirical
test settles it the other way, confirming the original rationale. Widening Create's
tolerance to sign-time failures does swallow transient/genuine signing errors on that one
path — accepted: the alternative is Create failing (and orphaning the moved GCS object)
on any signing blip, which is strictly worse, and the swallowed error is logged plus
recoverable via `DownloadURIRefresh`.

**`Create`-path behavior when signing is unavailable (Round-3 required this be explicitly
decided, not left to the implementer)**: `fileHandler.Create` currently calls
`bucketfileGenerateDownloadURI` at `file.go:81` AFTER the GCS object has already been
moved (`:70`), so a naive error return both fails the primary write path and orphans the
object. Decision: **persist with an empty `URIDownload`** — Create succeeds (upload and
record are real), and the download URI is populated lazily by `DownloadURIRefresh` when a
signing key becomes available; until then, download-URI reads surface an error — the
structured `SIGNING_NOT_CONFIGURED` in the keyless case, or the wrapped sign-time error
in the invalid-key case (the structured error is scoped to the nil-guard; sign-time
failures propagate as-is on read paths). Implementation note: do NOT assert on the
literal `p * q != n` error text anywhere — it is Go-1.24+ FIPS-module wording; older
toolchains say `invalid modulus` for the same check. This keeps the primary write path alive in keyless
deployments and avoids the orphaned-object failure mode. Alternative considered and
rejected: failing Create with the structured error — cleaner contract, but it makes file
upload unusable in every keyless-or-invalid-key deployment (including this sandbox: its
dummy key is present but rejected by Go's `x509.ParsePKCS1PrivateKey` with
`crypto/rsa: p * q != n`, empirically verified this cycle — see the both-cases paragraph
above), which is most of what §1.2 argues against. Doc-sync per monorepo policy (service CLAUDE.md if
present, `docs/operations.md` failure modes, including the new empty-`URIDownload`
semantics). Additionally,
sandbox's storage-manager compose block (which today sets `GCP_PROJECT_ID` but has neither
a credential env var nor a credential mount) gains BOTH pieces rag-manager already has
(Round-1 caught the earlier wording naming only "the mount"): the
`GOOGLE_APPLICATION_CREDENTIALS=/tmp/google_service_account.json` environment line AND the
`${GOOGLE_APPLICATION_CREDENTIALS:-./config/dummy-gcp-credentials.json}:/tmp/google_service_account.json:ro`
volume entry. So whichever lands first (this or the monorepo fix), the sandbox boots.

### 2.7 .env.template additions

`GCP_REGION` (new, `us-central1`, written by `init.sh` per §2.4 — no exclusion-list entry
needed). The `CLICKHOUSE_ADDRESS`/`CLICKHOUSE_DATABASE` entries lose their "aspirational —
no local default exists" annotation (§2.5 gives them a local default) and get real
defaults (`clickhouse:9000` / `default`); `init.sh:357-358` already writes both (address
currently empty — updated to the new default). `check-env-template-sync.sh` updates:
remove `CLICKHOUSE_ADDRESS` from `TEMPLATE_ONLY_VARS` (with init.sh now writing a
non-empty value it is no longer template-only — note its entry was already inert since
init.sh wrote the empty-string form, and the script's category-3 comment describing it as
having "no local service to point it at" becomes false this cycle; both the list and the
comment get corrected in the same change).

## 3. Verification plan

Sequencing (preferred, not a hard gate — Round-1 caught the earlier wording reading as a
cycle-blocking dependency): §2.6's monorepo PR merges first (its own ≥3-round review
loop), CI publishes images, then the sandbox change regenerates the lock at a commit
containing that fix. If the monorepo PR stalls, the sandbox side still ships on R5's
fallback path (dummy-credential mount); §2.6 is a boot-quality improvement for one
service, not a prerequisite for the cycle.

Clean-room, same procedure as prior cycles (no sudo, all checks scriptable):

1. Fresh `.env`/certs (no-sudo init path), `docker compose down -v` baseline.
2. Infra up: db, redis, rabbitmq, **postgres, clickhouse** — all healthy.
3. Migration: `scripts/init_database.sh` at the NEW dbscheme pin — applies cleanly
   including `08d686855c69` (destructive direct-hash migration) and the seven webchat
   migrations, zero errors.
4. Full stack up — **48 compose services** (44 today + direct-manager + webchat-manager +
   postgres + clickhouse); every defined service accounted for by name in the report.
5. **Pass criterion**: only `kamailio` and `coredns` down (sudo-gated, unchanged
   exception). Explicitly: direct-manager, webchat-manager, timeline-manager all running
   and stable (RestartCount watched over ≥3 minutes, not a single sample). rag-manager is
   deliberately NOT part of this step's gate — its pass/fail is governed by step 9's
   branch determination (Round-3 caught steps 5 and 9 contradicting each other on this).
   storage-manager's gate is conditional on sequencing (Round-4): if §2.6's monorepo fix
   is in the pinned images, it must be running and stable like the others; if §2.6 hasn't
   landed (Risk R5's fallback path — Risk R5 in §5, not review round 5), the criterion is R5's documented degraded state — boots
   (`JWTConfigFromJSON` passes on the dummy file), read/delete/bookkeeping RPC works,
   signing-dependent paths fail — with which branch applied recorded in the report.
   timeline-manager additionally shows NO ClickHouse ping errors in its log (its write
   path is now genuinely alive, not silently dead) and its migrations applied.
6. `setup_test_customer.sh` end-to-end — the F4 regression check: customer → **admin agent
   auto-created (direct-manager RPC succeeds)** → password → JWT login → billing → 3
   extensions → access key. This is the single most load-bearing check in the cycle.
7. compose↔lock agreement: run `scripts/sync-compose-images.sh`, assert zero diff.
8. Drift checks: `check-env-template-sync.sh` clean.
9. rag-manager §2.4 branch determination: with placeholder project/region/bucket and
   dummy credentials, record whether it boots (expected, post-§2.4's `GCP_PROJECT_ID`
   fix) — if yes, confirm an ingestion attempt fails with a clear GCP error (not a
   hang/panic); if no, document boot-blocking per §2.4's fallback (no `start.sh` change
   needed either way, §2.4).
10. transcribe start API check (carried over, was blocked by F4 last time): with a JWT
    from step 6, a transcribe-start request returns the `STT_NOT_CONFIGURED` structured
    error.
11. **Image-pull accounting (carried over from the predecessor design's Phase C, Round-1
    caught it dropped)**: confirm pulls succeed for ALL tracked images at the new pin.
    Any fallback-pin exceptions the generator kept (its stderr warnings, captured in the
    run's output — the lock file itself has no warnings field) are called out in the PR
    description, not buried. The two seeded entries (§2.1) cannot appear here by
    construction — under §2.1's hard-error semantics they either resolved or the
    generator refused to write the lock at all; this step just re-confirms both pull.
12. **call-manager ARI event check (carried over, Round-1 caught it dropped)**: beyond
    RestartCount, confirm call-manager's log shows it consuming from the
    asterisk-event-all queue after the 5-month image jump — same criterion as the
    predecessor design's Phase A/C.

Deferred (unchanged): sudo-gated DNS/network/SIP/browser-UI verification remains
VOIP-1274.

## 4. Non-goals

- **sentinel-manager stays out of the sandbox.** Not a deferral — an architectural
  impossibility: `rest.InClusterConfig()` is its only supported auth ("in-cluster only",
  its CLAUDE.md), it exists solely to watch Kubernetes pod lifecycle events, and a Compose
  deployment has no pods to watch and no kube-apiserver to connect to. The Phase-A-fixed
  services now tolerate its absence by design; a Compose stub would be dead weight
  pretending otherwise. If a future sandbox gains a k3s profile, revisit then.
- No monorepo-side change to rag-manager (§1.3: single-embedder architecture makes
  degradation-to-nothing worse than fail-fast; the sandbox catches up instead).
- CI wiring for the generator/sync/drift scripts: still manual-invocation this cycle.
- Frontend (`square-*`) and VoIP-image (`voip-kamailio`, `voip-rtpengine`,
  `voip-asterisk-*` except proxy) version bumps: separate repos, separate cadence,
  unchanged pins this cycle.
- No automated "did the version bump introduce a new latent subscribe/config bug" checker
  (still future work; this cycle's §3 is the manual equivalent).
- No upgrade-flow reordering (stop-managers → migrate → up) to close the mixed-schema
  window described in §1.6 — recorded follow-up, deliberate non-goal this cycle (§1.6
  explains the reasoning).

## 5. Risks

- **R1 — destructive migration (§1.6, which carries the full honest accounting).**
  `registrar_directs` is dropped. Summary of §1.6's analysis: the backfill covers
  `registrar_extensions` only and skips soft-deleted rows; fresh installs (§3's path) have
  no mixed-schema window, but the existing-install upgrade path
  (`voipbin update all` → `_upgrade_pinned`: backup → pull → migrate → up) runs old
  containers against the new schema between migrate and up, and MySQL's non-transactional
  DDL means a mid-migration failure leaves a half-migrated schema. The pre-upgrade backup
  (automatic in `_upgrade_pinned`) is the real safety net for every one of those cases;
  the backfill is monorepo-authored and already ran in production; the sandbox adds no
  novel path.
- **R2 — 5-month image jump for ~29 services, breadth of untested surface.** §3 exercises
  boot, migration, bootstrap flow, and the specific regressions found; it does not
  exercise real SIP/call flow (VOIP-1274) nor every service's business logic. Same honest
  gap as last cycle's R2, now with a larger delta. Mitigation is the same: this is what
  the sandbox's pin model exists for — one verified snapshot at a time — and the rollback
  is one `git revert` of the lock+compose commit.
- **R3 — new infra images (postgres, clickhouse) become part of the sandbox's support
  surface.** Two more third-party images to pin, watch, and document. Accepted
  deliberately: the alternative (a sandbox that silently can't do RAG or timeline
  analytics) is the drift this cycle exists to end.
- **R4 — rag-manager's §2.4 boot-with-dummy-credentials assumption is unverified until
  §3 step 9 runs.** The design carries both branches explicitly; neither branch blocks
  the rest of the cycle.
- **R5 — storage-manager sequencing, boot AND function.** If §2.6's monorepo PR stalls,
  the sandbox side can still ship: the dummy-credential mount lets the CURRENT
  (post-1f9002953) image boot — the boot gate is only `GOOGLE_APPLICATION_CREDENTIALS`
  being set plus `google.JWTConfigFromJSON` succeeding, which merely json-unmarshals and
  checks `type == "service_account"` (it does NOT parse the PEM); the dummy file
  satisfies both. If boot fails at all it would be `storage.NewClient` at
  `filehandler/main.go:116`, not key parsing. But
  boot is not the only exposure (Round-3 corrected an earlier draft implying it was):
  even booted with the dummy key, `storage.SignedURL` fails at actual sign time, so the
  §1.2 signing-dependent set — including file Create until §2.6's empty-`URIDownload`
  change lands — is non-functional in the sandbox regardless of which side ships first.
  Until §2.6 merges, storage-manager in the sandbox is "boots, read/delete/bookkeeping
  work, uploads and signed downloads fail" — documented as such, not hidden.
