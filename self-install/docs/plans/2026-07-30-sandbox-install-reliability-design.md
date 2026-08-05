# VoIPBin Sandbox — Install Reliability Fix (Call-Manager / Transcribe-Manager / Timeline-Manager Crash Loops, Version Pin Refresh, Env Template Sync)

Status: APPROVED (Design Review Rounds 5-6: two consecutive APPROVE, review loop closed)
Author: Hermes (CPO) with pchero (CEO/CTO)
Date: 2026-07-30
Repo: sandbox (fixes 2.1/2.2 land in monorepo, §2.3 is a sandbox-side non-goal with no code change; this doc is the sandbox-side design of record for the whole effort)

## 0. Mandate

A customer reported that installing VoIPBin via the sandbox failed. We do not have their
logs. **This document treats "call-manager's sentinel-manager crash loop is the customer's
failure" as a hypothesis, not a confirmed diagnosis** — it is the single most severe,
100%-reproducible failure found in a fresh clean-room install, and call-manager is core to
all call handling, so it is the leading candidate. Closing the loop with certainty requires
either the customer's logs or their confirmation after this fix ships; this document does
not claim that confirmation.

Goal: after this cycle, a fresh sandbox install reaches a stable, fully-running state (no
unexplained crash-looping containers) for a new self-hoster — **both today, against the
currently-pinned images, and after this cycle's own version-pin refresh (§2.4)**, which
Round-3 review found would otherwise introduce a new crash of its own (§1). The
version/credential bookkeeping that made this hard to diagnose is cleaned up, and the
monorepo-level bugs found are fixed in a way that does not silently disable functionality.
Everything this document cannot verify without an interactive `sudo` session is deferred to
§6, not hidden.

## 1. Reproduction findings (verified facts)

Full clean-room reproduction was run in this cycle: infra (db/redis/rabbitmq) up →
containerized alembic migration against the pinned Feb-21 dbscheme commit → full 44-service
`docker compose up -d` → `setup_test_customer.sh` against `https://localhost:8443`. Sudo-gated
steps (mkcert CA install, DNS forwarding, VoIP macvlan network setup) could not be exercised
in that session and are explicitly deferred (§6).

**Result: 40/44 containers healthy, 4 down** — 2 real bugs, 2 expected/sudo-gated (not new
findings).

| Container | Cause | Verdict |
|---|---|---|
| `voipbin-call-mgr` | `bin-call-manager/pkg/subscribehandler/main.go:124-134` (`subscribeHandler.Run()`): after `QueueCreate` (124-126), the loop over 4 targets (129-134) calls `QueueSubscribe`, which delegates to `QueueBind` (`bin-common-handler/pkg/rabbitmqhandler/queue.go:166-191`, interface declared at `main.go:34-35`). The target list is built in `cmd/call-manager/main.go:180-185` and includes `commonoutline.QueueNameSentinelEvent`, added 2025-06-22 (`git blame`, commit `4eee7e8062`) — well before the current pin, so this bug is present in the image actually deployed today. Sandbox's `docker-compose.yml` deliberately omits the `sentinel-manager` service (requires the Kubernetes API, crash-loops in Compose per its own inline comment), so that exchange is never declared, `QueueBind` returns an AMQP 404, and the wrapped error propagates to a `logrus.Fatalf` exit. Not sandbox-specific — **any non-Kubernetes Compose-based deployment of call-manager hits this**. Sentinel is last in the target list, so bindings 1-3 (asterisk-event-all, customer-event, flow-event) succeed before the 4th call fails and the channel is torn down — `Run()` returns the error at line 132, before the `ConsumeMessage` goroutine (137-141) ever starts. | **Fatal, highest priority.** Leading hypothesis for the customer's reported failure (see §0). |
| `sandbox-transcribe-manager-1` | `bin-transcribe-manager/pkg/streaminghandler/main.go` (`NewStreamingHandler`, spans 83-137) already treats GCP/AWS client init failure as non-fatal (`log.Warnf`, continues) for each provider individually. The actual fatal: if both providers end up nil, the function returns a bare `nil` interface (113-116), and `cmd/transcribe-manager/main.go:144-146` turns the `streamingHandler == nil` check into a returned error, unwinding to a fatal exit (`logrus.Errorf` + `os.Exit(1)` at `cmd/transcribe-manager/main.go:60` — same observable effect as call-manager's `logrus.Fatalf`, different mechanism). Root cause: `init.sh`/`init_no_sudo.sh` generate a syntactically-present but unparseable dummy GCP private key (§2.5), so GCP init fails, and no AWS credentials are configured by default. Only `runStreaming(streamingHandler)` (`cmd/transcribe-manager/main.go:224`, calling `.Run()`) touches the interface at boot — `transcribehandler.NewTranscribeHandler` (`pkg/transcribehandler/main.go:99-119`) only stores it in a struct field, it does not call a method on it. | **Fatal, second priority.** Inconsistent with the intent already expressed in the per-provider warnings. |
| `voipbin-kamailio` | Requires the `KAMAILIO_EXTERNAL_IP` macvlan interface, only created by the sudo-gated `setup-voip-network.sh`. Expected, not run this session. | Not a bug, deferred to §6. |
| `voipbin-dns` (CoreDNS) | Requires `config/coredns/Corefile`, only generated by the sudo-gated DNS setup step. Expected, not run this session. | Not a bug, deferred to §6. |

**A third, previously-unlisted service has the same sentinel-subscribe bug — but it is
latent today and will only surface after this cycle's own version-pin refresh (§2.4).**
`bin-timeline-manager/pkg/subscribehandler/main.go:47` includes
`commonoutline.QueueNameSentinelEvent` in a package-level `subscribeTargets
[]commonoutline.QueueName` (line 27), and its `Run(ctx context.Context) (<-chan struct{},
error)` (`main.go:112-130` for the queue-create-then-subscribe-loop portion; the full
function spans to 162, with a separate, deliberately non-fatal 27th bind — the
`QueueNameWebhookEventTopic` `#`-wildcard cutover — at 131-144) has the fix-relevant structure in common with call-manager's
`Run() error` — `QueueCreate`, then a loop that returns the first `QueueSubscribe` failure
— even though the signature, target-list type, and surrounding scaffolding differ. That
error is fatal via the call site at `cmd/timeline-manager/main.go:221-224` (inside
`runServices`) → `runDaemon` → cobra's `RunE` → `logrus.Errorf` + `os.Exit(1)` at
`main.go:60-63` (`runSubscribe` itself, the function definition, is at line 241). Round-3
review correctly flagged this as a live contradiction against
this document's own 40/44 count, since `timeline-manager` **is** deployed
(`docker-compose.yml:1140`) and reported healthy in this cycle's reproduction.

**Root cause of the apparent contradiction — structural, not just dating.**
`bin-timeline-manager` had **no `subscribehandler` package at all** before
`~/gitvoipbin/monorepo/docs/plans/2026-03-15-centralize-clickhouse-writes-design.md`
(dated 2026-03-15, §3: "Add a `subscribehandler` package following the established
pattern... Create `pkg/subscribehandler/main.go`") was implemented as commit `9ad08f416`
(2026-03-16, per `git blame` on the `QueueNameSentinelEvent` line). `git merge-base
--is-ancestor 9ad08f416 0ce70d7d3a0df3c49af817d6c2c14e6a9b2f7f9b` (the pinned target
commit, 2026-02-21) returns false — that commit postdates the pin. So the
currently-**deployed** `timeline-manager` image predates the package's existence entirely:
there is no subscribe attempt of any kind possible on that build, not merely "a sentinel
subscribe that happens to be skipped." Re-confirmed empirically: bringing up a fresh
`rabbitmq` + the currently-pinned `timeline-manager` image in isolation (`docker inspect
RestartCount: 0`, no sentinel/exchange/404 log lines, no `sentinel` exchange created per
the RabbitMQ management API) shows no crash and no subscribe attempt on that path.
Call-manager needs no dating argument for the same conclusion in reverse: this cycle's
reproduction directly observed it crash-looping against the pinned image, which is direct
proof the sentinel target is present and active in that deployed build — the "100%
reproducible" claim for call-manager stands unweakened regardless of when
`4eee7e8062` landed.

This is a genuinely new finding with real consequence: **§2.4's `versions.lock` refresh, if
shipped without also fixing timeline-manager, trades one known crash-looping service for
two** — a working-today service would start crash-looping the moment the pin moves past
2026-03-16. `grep -rln QueueNameSentinelEvent` across the monorepo confirms only three
services reference it as a subscribe target: `bin-call-manager` (broken today),
`bin-sentinel-manager` (the exchange's owner/publisher), and `bin-timeline-manager` (broken
only after a future version bump). §2.1's fix is therefore scoped to both call-manager and
timeline-manager (§2.2 renumbered accordingly below), and — because Phase A must ship before
Phase B moves the pin (§2.4) — timeline-manager's fix must land in the same monorepo PR
wave as call-manager's, not as a follow-up.

**`timeline-manager` may be silently degraded rather than genuinely healthy** (unrelated to
the sentinel bug): it reads `CLICKHOUSE_ADDRESS=${CLICKHOUSE_ADDRESS:-}` with no ClickHouse
service defined anywhere in `docker-compose.yml`, and sandbox `CLAUDE.md` documents it as
requiring one. It exposes only a generic `:2112/metrics` healthcheck, which can report
healthy independent of whether its ClickHouse-backed write path works. Not separately
confirmed broken this cycle (reported healthy); flagged rather than silently counted as a
clean pass, since the "loud crash beats silent degradation" argument this document makes
for call-manager/transcribe-manager applies here too. Out of scope to fix this cycle (§4).
(An earlier draft of this document also named `campaign-manager`/`hook-manager` here —
wrong: `docker-compose.yml:589` and `:783` are actually `call-manager` and `flow-manager`,
and neither reads `CLICKHOUSE_ADDRESS` in Go code at all per `grep -rl CLICKHOUSE_ADDRESS
--include=*.go`, which matches only `bin-timeline-manager`. Those two compose lines are
leftover per-service ClickHouse config, dead at HEAD (the 2026-03-15 design doc confirms
`notifyhandler` took a `clickhouseAddress` parameter directly before that change, so these
were live-but-unused at the Feb-21 pin and only became fully dead once that parameter was
removed) — see §2.5's dead-var handling.)

**Version drift is not implicated in the two bugs found and shipped-today.** `versions.lock`
pins the Feb-21 monorepo commit while monorepo HEAD is now Jul-30 (~5 months). Migrations
against the Feb-21 pin completed with zero errors. Both the call-manager and
transcribe-manager bugs were confirmed to exist unchanged at current monorepo HEAD
(`a0438c1f2`) as well as at the Feb-21 pin, so bumping the pin alone would not have fixed
the customer's install — but, per the timeline-manager finding above, bumping the pin
**without** this cycle's fixes would make things measurably worse, not neutral.

**Dummy GCP credential is mounted into 3 services, not just transcribe-manager**
(`docker-compose.yml:529` api-manager, `:989` rag-manager, `:1193` transcribe-manager, all
`${GOOGLE_APPLICATION_CREDENTIALS:-./config/dummy-gcp-credentials.json}`). Confirmed in this
session's reproduction that api-manager and rag-manager stayed healthy with the same
unparseable credential (both lazy-init on that path). Only transcribe-manager parses it
eagerly at startup.

**Env var audit**: no genuinely required variable was found missing or broken. See §2.5 for
the concrete `.env.template` sync plan.

The customer/agent bootstrap flow fixed in PR #7 (da0c1cd) — customer create → agent password
set → JWT login → billing plan → extensions — was re-verified working with **no regression**.

## 2. Scope

### 2.1 monorepo `bin-call-manager` and `bin-timeline-manager` — sentinel-manager subscribe fix

**Rejected approach 1: "log the error and continue".** `QueueBind` failure with AMQP 404
closes the underlying channel, which all subscribe targets on a given service share on one
queue — leaving that service deaf to whichever targets bind *after* the failing one. Fragile
to future target-list reordering, and does nothing to prevent the crash in the first place.

**Rejected approach 2 (an earlier draft of this document): "add `ExchangeDeclare` to the
`Rabbit`/`SockHandler` interface, hand-match kind/durability parameters before the bind".**
Round-2 review caught: `ExchangeDeclare` is defined only on the unexported `amqpChannel`/
`*rabbit` types, not reachable through `SockHandler` — extending it means a
`bin-common-handler` public-interface change, which per root `CLAUDE.md` triggers a
verification pass across all 37 consumer services. Hand-matching kind/durability also risks
an AMQP 406 `PRECONDITION_FAILED` on any mismatch, which closes the channel — the exact
failure mode this fix exists to avoid.

**Chosen approach**: `SockHandler` (which both `subscribeHandler` implementations already
hold as `h.sockHandler`) already exposes `TopicCreate(name string) error`
(`bin-common-handler/pkg/sockhandler/main.go:20`), which internally calls
`ExchangeDeclare(name, "fanout", true, false, false, false, nil)`
(`rabbitmqhandler/topic.go:5-12`). This is exactly how sentinel-manager declares the same
exchange today: `bin-sentinel-manager/cmd/sentinel-manager/main.go:93` constructs a
`notifyhandler.NewNotifyHandler(...)`, whose constructor
(`bin-common-handler/pkg/notifyhandler/main.go:112-131`) calls
`sockHandler.TopicCreate(string(queueEvent))` at line 122. Calling
`h.sockHandler.TopicCreate(string(commonoutline.QueueNameSentinelEvent))` immediately
before that specific target's `QueueSubscribe` call — in both
`bin-call-manager/pkg/subscribehandler/main.go`'s loop and
`bin-timeline-manager/pkg/subscribehandler/main.go`'s equivalent loop — requires no
interface change and gets kind/durability parity by construction. If sentinel-manager is
deployed, its own `TopicCreate` call already made this a no-op declare (idempotent,
matching params); if not, the calling service declares the exchange itself and the
subsequent bind/subscribe succeeds.

**Scoping decision (explicitly a single-target guard, not a blanket declare-all)**: the
`TopicCreate` call is added only for the `QueueNameSentinelEvent` target specifically — via
a name comparison inside each service's existing loop. The two services' target lists have
different element types, so the guard is not one shared snippet:
call-manager's `h.subscribeTargets` is `[]string` (`cmd/call-manager/main.go:180-185`), so
the guard is `if target == string(commonoutline.QueueNameSentinelEvent) { ... }`;
timeline-manager's package-level `subscribeTargets` is `[]commonoutline.QueueName`
(`pkg/subscribehandler/main.go:27`), so the guard there is `if target ==
commonoutline.QueueNameSentinelEvent { ... }` (no `string()` cast — comparing a `QueueName`
to a `string` would not compile). Reasoning for scoping narrowly rather than declaring all
targets unconditionally: every other target in both services' lists (asterisk events,
customer/flow/webhook events, etc.) is fanout-kind today, so declaring all of them via
`TopicCreate` would currently be harmless, but `TopicCreateWithKind` already exists on the
`SockHandler` interface itself (`sockhandler/main.go:21`, backed by
`rabbitmqhandler/topic.go:17`, added by VOIP-1258) specifically because not every exchange
is fanout going forward (the new topic-exchange webhook routing introduced by that same
change) — a caller can already choose a kind through the public interface today. A blanket
declare-all-as-fanout would silently paper over a future topic-kind target the same way
approach 2's hand-matching would, just via a different mechanism. Scoping narrowly to the
one target this document has actually verified is safe keeps the fix's blast radius equal
to its verified justification.

**A separate, pre-existing startup-order sensitivity in timeline-manager, explicitly not
fixed by this scoping decision**: timeline-manager's `subscribeTargets` has 26 entries
(`pkg/subscribehandler/main.go:27-54`), each owned/declared by a different service's own
`notifyhandler.NewNotifyHandler` → `TopicCreate` call at that service's boot, and
timeline-manager's compose `depends_on` is `rabbitmq` only (`docker-compose.yml:1150-1152`)
— nothing sequences it after its 25 non-sentinel event-owning peers. Any of those 25 can, in
principle, hit the identical AMQP-404-on-`QueueBind` fatal path this document is fixing for
the sentinel target specifically, if timeline-manager's `QueueSubscribe` loop reaches that
target before the owning service has declared its exchange. This is not a new bug
introduced by this design — it is a pre-existing characteristic of an unordered
service-mesh boot with `restart: always`. This document's own reproduction cannot speak to
whether the other 25 targets are, in practice, an observed problem: the currently-deployed
timeline-manager build predates the `subscribehandler` package entirely (§1), so it has
never attempted any of the 26 binds, sentinel or otherwise, in production either. This
paragraph is a disclosed, accepted risk based on the boot-order/`restart: always` mechanics
themselves, not on an absence-of-evidence argument — it should be watched for during Phase
C and beyond, not treated as pre-validated. This document's fix does not extend the same
guard to all 26 targets (§2.1 above explains why a
blanket declare would be the wrong general-purpose fix); instead this cycle accepts that
timeline-manager may still experience **transient**, self-resolving restarts from ordinary
compose boot-order races, distinct from the **sustained** sentinel-caused crash loop this
fix targets. §3's Phase-C pass criterion for timeline-manager is worded accordingly (steady
state, not zero restarts ever observed).

**Acceptance criterion**: after the fix, call-manager must still receive and process ARI
events end-to-end — verified by confirming an actual call/ARI event flows through in the
clean-room verification (§3), not merely "container does not restart". Timeline-manager
must be confirmed to still receive its other (non-sentinel) subscribed events.

**Doc-sync obligation**: this touches `pkg/subscribehandler/main.go` in both services, which
root `CLAUDE.md`'s service-docs-sync table (enforced by a PostToolUse hook,
`scripts/check-service-docs.sh`) requires be reflected in each service's
`docs/architecture.md` events section, in the same PR — not conditional on whether the doc
"happens to" cover this.

**Test impact**: adding a `TopicCreate` call inside each `Run()` requires new
`MockSockHandler.EXPECT().TopicCreate(...)` expectations in both services' existing
`subscribehandler` unit tests — expected, called out here so the monorepo review loop isn't
surprised by it (§3).

### 2.2 monorepo `bin-transcribe-manager` — graceful STT-unavailable degradation

**Chosen approach**: change `NewStreamingHandler` itself to return a working no-op/disabled
implementation of the `StreamingHandler` interface instead of a bare `nil` when both
providers are unavailable — an exported constructor path (e.g.
`streaminghandler.NewDisabledStreamingHandler()`, or folding the disabled case into
`NewStreamingHandler`'s existing return) so callers never see a nil sentinel at all. This
removes the `streamingHandler == nil` check in `cmd/transcribe-manager/main.go:144-146`
entirely — there is no nil interface for `runStreaming` or any `transcribehandler` call
site to dereference, by construction.

**Defined behavior when STT is disabled**: the disabled implementation's `Run()` method
returns `nil`, matching the real `streamingHandler.Run()`'s existing no-op behavior
(`pkg/streaminghandler/run.go:6-8` — it is not on any API request path, it is a
boot-time loop starter, and any non-nil return from it is what currently causes a fatal
exit via `cmd/transcribe-manager/main.go:160-162`/`218-229`; the disabled implementation
must not reintroduce that). `STT_NOT_CONFIGURED`-class errors are returned instead from the
disabled implementation's per-request methods — `Start(...)` and `Stop(...)` on the
`StreamingHandler` interface (`streaminghandler/main.go:37-43`) — which is what an actual
transcribe-start API call reaches.

**Doc-sync obligation**: `bin-transcribe-manager/CLAUDE.md` currently documents "at least
one provider must be configured at startup" as an intentional invariant. This design
reverses that invariant — rewrite it in the same PR, along with the failure-mode section of
`docs/operations.md`, to describe the degrade-instead-of-crash behavior and the
`STT_NOT_CONFIGURED` API error.

### 2.3 sandbox dummy GCP credential — explicit non-goal, not silently dropped

This cycle does not change the dummy credential's shape/validity. §2.2's fix makes
transcribe-manager tolerate it (and any other absent/invalid STT credential) without
crashing — a real self-hoster who wants working STT still needs real credentials via
`.env`, same as today. api-manager and rag-manager already tolerate the dummy credential
(§1) and need no change.

### 2.4 sandbox `versions.lock` refresh — sequencing and count reconciliation

**Resolved sequencing**, three explicit phases, run in order:

1. **Phase A (this cycle, first)**: land 2.1 (both call-manager and timeline-manager) and
   2.2 as monorepo PRs, through the standard monorepo review loop (CLAUDE.md policy:
   minimum 3 code-review rounds, until 2 consecutive approvals). Verification during this
   phase does not use `docker compose build` (sandbox's `docker-compose.yml` has no
   `build:` stanzas — every service is a digest-pinned `image:` reference, and the
   Dockerfiles live in the monorepo). Instead: build the affected images directly in the
   monorepo worktree (`docker build -f bin-call-manager/Dockerfile -t
   voipbin/bin-call-manager:local-fix .` run from the monorepo root, since the Dockerfiles
   expect a repo-root build context for `go mod vendor`; same pattern for
   `bin-timeline-manager` and `bin-transcribe-manager`), then apply a
   `docker-compose.local-fix.yml` override in the sandbox repo (same layering mechanism
   already used for `docker-compose.test.yml`: `docker compose -f docker-compose.yml -f
   docker-compose.local-fix.yml up -d`) that replaces just those services' `image:` value
   with the local tag. This override file is a throwaway verification artifact for Phase A,
   not committed to the sandbox repo.

   **Version skew during Phase A, explicitly acknowledged**: the three locally-built images
   are built from ~monorepo HEAD (Jul-30), while the other ~37 sandbox services and the
   database schema remain on the Feb-21 pin during this phase. This is fine for
   call-manager and transcribe-manager (both fixes are self-contained to those binaries'
   own startup logic and don't depend on peer version). It is **not** fully representative
   for timeline-manager: a HEAD-built timeline-manager is the sole ClickHouse writer per
   the 2026-03-15 centralization (no ClickHouse container exists in sandbox, so its write
   path degrades, not crashes — expected, unrelated to this fix), and it binds 26
   exchanges against Feb-21 peers whose exchange/queue-name constants may not fully match
   HEAD's `commonoutline` package. Phase A's timeline-manager check therefore only proves
   "does not fatally exit immediately at boot from the sentinel gap" — it is not the
   authoritative check that timeline-manager is fully healthy against a consistent build.
   That authoritative check is Phase C, where all services (including timeline-manager)
   are built from the same target commit and skew is not a factor.
2. **Phase B (after Phase A merges to monorepo main and CI publishes images at that merge
   commit)**: regenerate `versions.lock` targeting that merge commit (or monorepo HEAD at
   that point, whichever is later — default to HEAD, since the refresh itself is in scope
   this cycle). Because Phase A already fixed timeline-manager's latent bug, this refresh
   no longer trades one crash for another (§1).
3. **Phase C**: rerun the full clean-room procedure (§3) against the Phase-B
   `versions.lock`, using the registry-pinned images like a real customer install would.

**Count reconciliation (44 services vs. 39 pinned images)**: 44 compose services break down
as 4 third-party-image services (`db`, `redis`, `rabbitmq`, `coredns` — not tracked in
`versions.lock`) + 40 `voipbin`-owned-image services. Those 40 map to 38 distinct images,
since the three `asterisk-*-proxy` services share one `voip-asterisk-proxy` image. The 39th
pinned image, `voipbin/bin-sentinel-manager`, corresponds to zero deployed services — it is
still built and tagged in CI (monorepo builds every service, not just the ones sandbox
deploys) and the current `versions.lock` does pin it defensively even though sandbox
doesn't run it. The generator (below) keeps pinning it: cheap to track, and stops being a
surprise gap the day sandbox does add a lightweight sentinel-manager stub (not this cycle,
§4).

**Reusable generator script**:
- Path: `scripts/generate-versions-lock.sh`.
- Input: a target monorepo git ref (commit SHA or branch), read from an argument (default:
  monorepo's current `main` HEAD via `git ls-remote`/local checkout).
- Process: for each of the 39 `voipbin/*` images currently tracked in `versions.lock`,
  resolve the nearest registry tag at-or-before the target commit (mirroring how the
  original ancestry pin was built for da0c1cd), pull it, record its resolved sha256 digest
  and source git commit SHA.
- Output: a regenerated `versions.lock` with the same schema as today's, plus a new
  `generated_by: scripts/generate-versions-lock.sh` field so a hand-edited lock is visually
  distinguishable from a generated one going forward.
- Fallback behavior: if a service has no registry tag at or before the target commit, keep
  that service's current pinned digest unchanged and print an explicit warning line — never
  silently pin to an unrelated/newer tag.
- Idempotency: running it twice against the same target with no new merges produces a
  byte-identical `versions.lock` (excluding a `generated` timestamp field).
- Out of scope for this cycle: wiring this into CI as an automatic drift-detector. Worth
  doing later; not blocking this fix. Also out of scope: an automated "does bumping the pin
  introduce a newly-latent bug like timeline-manager's" checker — the discovery method used
  in §1 (`git blame` + `git merge-base --is-ancestor` on subscribe-target changes across
  the affected commit range) is manual this cycle; a scripted version of it is a natural
  follow-up, not built here.

### 2.5 sandbox `.env.template` sync — concrete, resolvable spec

Per-variable resolution, four categories (corrected from an earlier draft that undercounted
and mis-cited several lines):
- **Add to template** (generated by `init.sh` today but undocumented): `BASE_HOSTNAME`,
  `API_URL`, `WEBSOCKET_URL`, `REGISTRAR_URL`, `REGISTRAR_DOMAIN`, `CONFERENCE_URL`,
  `CONFERENCE_DOMAIN` — document with their actual generated defaults (from `CLAUDE.md`'s
  own table).
- **Keep in template, annotate as compose-internal-default**: `DB_HOST`/`DB_PORT`
  (e.g. `docker-compose.yml:458`; read via `${VAR:-default}` by nearly every manager
  service, 20+ occurrences, not a single call site), `REDIS_HOST`/`REDIS_PORT` (e.g. :292),
  `RABBITMQ_HOST`/`RABBITMQ_PORT` (e.g. :287), `KAMAILIO_DB_HOST` (:183),
  `KAMAILIO_REDIS_HOST` (:196), `RTPENGINE_PORT_MIN`/`RTPENGINE_PORT_MAX` (:131-132),
  `RTPENGINE_LISTEN_HTTP` (:129) — all verified as genuine `${VAR:-default}` reads with
  matching template defaults. Add a one-line comment next to each: "compose default is
  used unless you override this — see Track A externalization,
  `docs/plans/2026-07-05-production-grade-horizontal-scale-design.md`, for the multi-host
  case."
- **Mark dead, do not imply overridable**: `RTPENGINE_INTERFACE`. `.env.template:76`
  documents `RTPENGINE_INTERFACE=any`, but `docker-compose.yml:128` sets it unconditionally
  to `pub/${RTPENGINE_EXTERNAL_IP:-127.0.0.1};priv/10.100.0.201` — not a `${VAR:-default}`
  read, a hard override. Setting `RTPENGINE_INTERFACE` in `.env` has no effect today.
  Either delete the line or mark it explicitly `# currently ignored — docker-compose.yml
  hardcodes this from RTPENGINE_EXTERNAL_IP`.
- **Documented, genuinely consumed, but no backing service — mark aspirational**:
  `CLICKHOUSE_ADDRESS`/`CLICKHOUSE_DATABASE` (`.env.template:137-138`). A real read exists
  — `bin-timeline-manager` only (§1; confirmed via `grep -rl CLICKHOUSE_ADDRESS
  --include=*.go`, which matches no other service) — but there is no ClickHouse container
  in `docker-compose.yml` for a value to point at by default. Annotate as "set this only if
  you run your own ClickHouse instance; timeline-manager degrades without it (§1) — no
  local default exists".
- **Dead at the compose level (not a `.env.template` item, flagged for awareness, not fixed
  this cycle)**: `docker-compose.yml:589` (`call-manager`) and `:783` (`flow-manager`) each
  set `CLICKHOUSE_ADDRESS=${CLICKHOUSE_ADDRESS:-}`, but neither service's Go code reads
  that variable — leftover from before the 2026-03-15 ClickHouse-write centralization into
  timeline-manager. Harmless (defaults to empty, nothing consumes it), but is exactly the
  kind of stale config `.env.template` sync is meant to prevent. Out of this design's scope
  since it is a `docker-compose.yml` edit, not a `.env.template` one — noted here so it
  isn't silently lost, candidate for the same cleanup pass as §2.1's doc-sync obligations.
- **Drift check**: add `scripts/check-env-template-sync.sh`, run manually for now (CI
  wiring out of scope, same as §2.4's generator) — greps `init.sh` (the only committed
  `.env`-generating script on `main`; `init_no_sudo.sh` was an ad-hoc, uncommitted repro
  helper used during this cycle's investigation, not a real repo file — an earlier draft
  of this document incorrectly treated it as one. The checker's script list is an array so
  it silently starts covering `init_no_sudo.sh` too if that script is ever committed) for
  `.env` variable names, greps `.env.template` for documented variable names, and prints any
  variable present in one but not the other, exit non-zero if any found (excluding the
  compose-internal-default, dead, and aspirational sets above, which are intentionally
  template-only or template-stale by design, not drift).

## 3. Verification plan

**Phase A verification (locally-built images via override file, §2.4):**
- Build `call-manager`, `timeline-manager`, and `transcribe-manager` from the fix branch in
  the monorepo worktree, tag locally, apply `docker-compose.local-fix.yml` in the
  clean-room `docker compose up -d` run per §2.4.
- Confirm `voipbin-call-mgr` and `sandbox-transcribe-manager-1` reach a running,
  non-restarting state. Confirm `timeline-manager` does not fatally exit from the sentinel
  gap specifically — given the version-skew caveat above (§2.4), this phase's
  timeline-manager check is a smoke test, not the authoritative pass/fail; see Phase C.
- **Confirm call-manager still processes ARI events** — drive a test call or synthetic ARI
  event through and confirm call-manager's logs show it consumed from the
  asterisk-event-all queue, satisfying §2.1's acceptance criterion.
- **Confirm timeline-manager still receives its other subscribed events, to the extent
  Phase A's skewed environment allows** (§2.1's acceptance criterion for the second
  service; full confidence deferred to Phase C, §2.4).
- **Confirm transcribe-manager's disabled-STT behavior is correct** — issue a
  transcribe-start API request against it with the dummy credential in place and confirm it
  returns the defined `STT_NOT_CONFIGURED`-class error from `Start(...)`, and separately
  confirm the container itself does not restart (its `Run()` returning `nil` at boot).
- Rerun `setup_test_customer.sh`, confirm no regression.

**Phase C verification (registry-pinned images, §2.4, after monorepo merge + versions.lock
refresh):**
- Full clean-room procedure (infra → migration → full `docker compose up -d`) against the
  refreshed `versions.lock`.
- **Pass criterion**: only `voipbin-kamailio` and `voipbin-dns` are down, both attributable
  solely to the sudo-gated setup not having run in this session (§1, §6) — not "zero
  containers down", which is unachievable without sudo. `timeline-manager` must reach a
  **steady state** (RestartCount stops increasing within a few minutes of full-stack boot)
  rather than "zero restarts ever observed" — §2.1 explicitly accepts transient restarts
  from the pre-existing, unrelated 26-target boot-order sensitivity, while a *sustained*
  crash loop would indicate the sentinel-specific fix (the actual regression §1 identified)
  did not hold.
- Confirm image pulls succeed for all 39 pinned images at the new target commit (including
  `bin-sentinel-manager`, per §2.4's count reconciliation); any fallback-pin exceptions are
  documented in the regenerated `versions.lock` and called out explicitly in the PR
  description.

Deferred, sudo-gated verification: see §6.

## 4. Non-goals

- Not implementing `sentinel-manager` itself in sandbox — it remains an intentional,
  documented Kubernetes-only exclusion. (§2.4 keeps pinning its image defensively, which is
  not the same as deploying it.)
- Not changing the dummy GCP credential's content/validity (§2.3).
- Not fixing `timeline-manager`'s ClickHouse-dependent functionality or its healthcheck
  blind spot (§1), and not cleaning up the two dead `CLICKHOUSE_ADDRESS` compose entries on
  `call-manager`/`flow-manager` (§2.5) — flagged, not silently dropped, but
  out of scope this cycle.
- Not touching secrets storage — plaintext `.env` stays as-is (decided this cycle: the
  meaningful risk is backup-archive exposure, out of scope here).
- Track A (horizontal-scale enablement) is already complete and out of scope.
- Not a general Track B (install-parity hardening: TLS lifecycle, scheduled backup,
  monitoring stack, public DNS) — that remains a separate, not-yet-started body of work.
- Not wiring §2.4's generator or §2.5's drift check into CI as an automated recurring job —
  both ship as manually-invoked scripts this cycle; CI automation is explicitly future work.
- Not building an automated "does this version bump introduce a newly-latent subscribe bug"
  checker (§2.4) — this cycle's discovery of the timeline-manager case was manual;
  automating it is a natural follow-up, not in scope now.

## 5. Risks

- **R1 — production behavior regression from making sentinel-subscribe non-fatal, with a
  narrower and more honest framing than an earlier draft.** `TopicCreate` declares
  `durable=true` (`topic.go:7`), so in a real Kubernetes cluster the sentinel exchange
  survives broker restarts once sentinel-manager has started even once — meaning
  call-manager/timeline-manager **already** start fine through a sentinel-manager outage on
  a broker that has seen sentinel-manager before; today's crash loop is only a signal on a
  fresh/never-seen-sentinel broker, not a general "sentinel-manager is down" signal. The
  `TopicCreate`-before-subscribe fix (§2.1) removes even that narrower signal: a
  genuinely fresh-broker deployment that's missing sentinel-manager now starts these
  services successfully instead of crash-looping. **Mitigation is honestly limited**:
  `ExchangeDeclare`/`TopicCreate` is idempotent with no created-vs-already-existed return
  signal, so there is no way to distinguish "I just declared this because nobody else did"
  from "sentinel-manager already declared this" at declare time — a metric/log at declare
  time cannot work. The workable alternative is a runtime liveness signal instead of a
  declare-time one: track a last-sentinel-event-received timestamp and expose it as a
  gauge/health field, so an operator who expects sentinel events can alert on "no sentinel
  event in N minutes". This is a monorepo-PR-scope addition to consider, not blocking
  sandbox's fix from shipping if deferred. Same shape applies to §2.2: the
  `STT_NOT_CONFIGURED` API-level error is the primary mitigation for callers, but there is
  no standing "STT is disabled" health/metric signal for an operator who isn't actively
  calling the API.
- **R2 — version refresh safety is not fully covered by this cycle's verification, even
  after §1's timeline-manager finding is fixed.** The 5-month drift is now confirmed to
  contain at least one real, previously-undiscovered regression risk (timeline-manager) that
  this cycle happened to catch via manual investigation, not systematic checking (§2.4).
  Phase C's verification (§3) checks for crash loops, the customer-bootstrap flow, and the
  specific timeline-manager regression already found — it does not exercise real SIP/call
  flow (deferred, §6) and cannot rule out other undiscovered regressions of the same shape
  elsewhere in the 5 months of changes. The refresh can plausibly introduce breakage this
  verification cannot detect until the deferred sudo-gated pass runs, or until a future
  systematic drift-regression check (§4) exists.
- **R3 — rollback plan.** If Phase C's refreshed `versions.lock` regresses something:
  `git revert` the versions.lock commit restores the Feb-21 pin immediately (sandbox side,
  no dependency on anything else). If any monorepo fix (2.1/2.2) regresses something
  post-merge: standard monorepo PR revert; since Phase B's `versions.lock` refresh depends
  on Phase A having merged, reverting a Phase-A monorepo PR after Phase B already shipped a
  lock pointing past it would require re-running Phase B's generator against the
  pre-revert commit as well, and would re-expose the timeline-manager regression if the
  timeline-manager fix specifically were the one reverted — call this out explicitly in the
  Phase-B PR description.
- **R4 — hypothesis, not confirmed diagnosis.** We do not have the customer's logs. This
  fix addresses the most severe, 100%-reproducible failure found, which is a strong
  candidate, not a confirmed match to the specific customer report.

## 6. Deferred verification (sudo-gated, end of this cycle)

Requires an interactive `sudo` password, not available in the session that produced this
design and its reproduction (§0, §1) — requires pchero to run directly or supply results
from:

- Full `sudo ./scripts/start.sh` (mkcert CA, DNS forwarding, VoIP macvlan network).
- Real SIP registration / call flow (`softphone.py`, `test_call.py`).
- Browser-based admin/talk/meet UI check.

Until this runs, "fixed" in §3 means "no unexplained crash loops observed (including the
timeline-manager regression §1 found), ARI events and STT-disabled behavior verified" — it
does not mean "verified working for real SIP calls" (also noted in R2 above). This gap is
explicit, not hidden.
