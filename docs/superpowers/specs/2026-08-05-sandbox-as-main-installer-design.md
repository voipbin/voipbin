# Sandbox as the Main Installer — Design

_Revision 7 — incorporates round-1 through round-6 review findings (see PR discussion for all
six reports)._

## Motivation

Today `voipbin/voipbin` positions `voipbin/install` (GCP + Terraform + Ansible + Kubernetes)
as *the* self-install path, and lists `voipbin/sandbox` (Docker Compose, single-host) only as
"Examples & sandbox" — a dev/test toy, not an install method.

**What is already decided (CEO/CTO, confirmed):** the Docker Compose stack in
`voipbin/sandbox` becomes the primary, documented self-install method for VoIPBin, positioned
alongside `voipbin/install` (GCP/K8s stays available, its long-term fate is a separate future
decision). This is an engineering/documentation restructuring decision and is what this design
document covers.

**What is NOT yet decided, and blocks shipping the README rewrite regardless of engineering
readiness:** `sandbox/README.md` currently tells production users to "use the official VoIPBin
cloud service or contact us for on-premise licensing" — i.e., self-hosted production is
currently framed as commercially restricted. Making it a first-class, openly documented path is
a *commercial positioning change*, not a docs change, and needs explicit CEO sign-off separate
from this design's engineering approval. See Scope.

## Scope

**Phase 1 (this spec, this PR):**

1. Bring the sandbox codebase into `voipbin/voipbin` as an in-repo directory (`self-install/`),
   via clean copy (method specified below) + `self-install/HISTORY.md`.
2. Rewrite the root README so both self-install paths are documented side by side (Option A:
   Docker Compose, primary; Option B: GCP/K8s, existing), fixing every reference point listed
   under README Changes.
3. Rewrite `self-install/README.md`'s security section from "local dev only, don't expose" to
   "these are the defaults, here is what changes before you expose this."
4. **Credential hardening — hard requirement, no fallback to a manual/documented-only
   procedure** (round-2 review found the credentials are literals, not `.env` references, in
   `docker-compose.yml` — ~76 lines across `MYSQL_ROOT_PASSWORD`/`root_password` DSNs,
   `guest:guest` AMQP URLs, and `AMI_PASSWORD=asterisk` — so a "just document how to rotate
   them" escape hatch doesn't work). Round-3 review found the hardcoding is **not limited to the
   compose file** — parameterizing only `docker-compose.yml` would break the installer, since
   these consumers read the same literals directly with no env fallback:
   - `scripts/start.sh` (migration-state check shells out with `mysql -u root -proot_password`)
   - `scripts/init_database.sh` (`DB_ROOT_PASSWORD="root_password"`)
   - `scripts/migrate.sh` (`DB_PASSWORD="root_password"`)
   - `scripts/voipbin-cli.py` (backup/restore/shell subcommands; only two of the several call
     sites already fall back to `${MYSQL_ROOT_PASSWORD:-root_password}`, the rest don't)
   - `scripts/doctor.sh`'s in-container MySQL checks (`${MYSQL_ROOT_PASSWORD:-root_password}` —
     already has an env fallback, so this one keeps working once the env var is set; listed here
     so it isn't missed from the audit, not because it needs a code change)
   - test fixtures — `tests/config.bats` **asserts** the current literals are present (a test
     that needs updating, not a runtime consumer that breaks); `scripts/tests/test_backup_restore_live.py`
     is a live-stack consumer like the others above

   Phase 1 must parameterize **all of the above together**, not just the compose file, plus:
   - Add the corresponding keys to `.env.template`. Note on why this matters:
     `check-env-template-sync.sh` is **not wired into CI today** (its own header says CI
     integration is deliberately out of scope, and `voipbin/sandbox`'s only workflow is the
     Discord-notify one) — round-3 review's original claim that skipping this "fails CI" was
     wrong. It matters anyway because it's the only drift-detection tool that exists for
     `.env`/`.env.template`, must be run manually as part of this PR, and its CI wiring is added
     as a Phase 2 item (folded into the "installation script test CI" line below). `init` should
     generate random values by default instead of shipping `root_password`/`guest`/`guest`/
     `asterisk` as literals — see the existing-install migration conflict called out below before
     assuming this applies unconditionally.
   - Any operational docs that quote these literals as examples (`README.md`'s RabbitMQ
     management-UI note, `CLAUDE.md`'s `mysql -uroot -proot_password` shell examples) get updated
     alongside the code change so they don't silently go stale.
   - `JWT_KEY` generation is **already handled** — `init.sh` already calls
     `generate_random_key()` and compose already defaults to an empty string, not a shipped
     secret (round-3 review found this is not new work). The only loose end is
     `.env.template`'s `JWT_KEY=your-random-jwt-secret-key` placeholder value, which gets
     replaced with a clearer placeholder comment for consistency with the other rotated keys —
     the `JWT_KEY=` key line itself stays (removing the line would itself register as
     `.env`/`.env.template` drift, since `init.sh` still writes that key), plus a regression check
     that generation still happens (see Verification Plan).
   - **Existing-install migration conflict, resolved:** the credential randomization above only
     applies to fresh installs. `MYSQL_ROOT_PASSWORD`/`RABBITMQ_DEFAULT_USER`/`PASS` only take
     effect the first time their respective data volumes are initialized — an existing sandbox
     user's `db_data`/mnesia volumes already have `root_password`/`guest` baked in. Regenerating
     random values in a fresh `.env` while reusing the old volume (the
     `COMPOSE_PROJECT_NAME=sandbox` migration path above) would desync `.env` from what's
     actually in the volume and lock the operator out. So: users migrating an existing install
     keep their existing `.env` file as-is (copy it into the new checkout location rather than
     regenerating), alongside exporting `COMPOSE_PROJECT_NAME=sandbox` — no credential rotation
     for them in Phase 1.

     **Round-5 review found `.env` alone is not enough to migrate; round-6 review found the
     fuller list still had gaps.** Rather than hand-enumerate gitignored paths and risk missing
     one a third time, the migration instructions define the copy set by reference to the
     authoritative list already maintained in code: `voipbin-cli.py`'s `clean --purge` command
     (~line 4802) enumerates every generated artifact the installer considers "local state" —
     `certs/`, `.env`, `config/coredns/`, `config/dummy-gcp-credentials.json`, `tmp/`,
     `docker-compose.override.yml` (version pins), `.voipbin-versions/` (rollback history), and
     `.test_data_initialized`. Migration instructions say "copy this entire set from the existing
     checkout" and point at that command's source as the definition, instead of a fixed list in
     this doc that can drift from the code.

     Two items in that set matter enough to call out explicitly, since missing either causes
     real damage, not just a failed service:
     - **`.test_data_initialized`** — without it, `start.sh` treats the migrated install as a
       fresh one and re-runs `setup_test_customer` against the existing production data: it
       looks up whatever customer/agent already exists at that email, unconditionally resets that
       agent's password to `admin@localhost`, and re-tops-up test balances. This directly defeats
       the credential hardening this same Scope item exists to provide, for exactly the
       operators migrating a real install. `doctor.sh` already warns about this re-seeding
       behavior when the marker is missing.
     - **`docker-compose.override.yml` / `.voipbin-versions/`** — these hold the image
       pins/rollback history that Scope item 6 documents as an existing, working feature. Losing
       them on migration means a "documented" capability breaks for exactly the users being
       migrated onto it.
     - `dummy-gcp-credentials.json` (created only by `init.sh`/`init_no_sudo.sh`, never by
       `start.sh`) and `certs/` remain relevant as before: missing the former breaks several
       services' bind mounts (silently — `start.sh`'s `validate_env` only warns), and missing the
       latter is fine for default mkcert mode (self-heals) but a hard failure under
       `TLS_MODE=byo`.

     Rotating credentials *on an already-running install* (via `ALTER USER`
     / `rabbitmqctl change_password` against the live volume) is real additional work, deferred
     to Phase 2. This also means Option A's documented entrypoint must not tell a migrating user
     to run `./scripts/init.sh --yes` unconditionally — `init.sh` overwrites an existing `.env`
     when `--yes` is passed (logged as "Overwriting existing .env (--yes)"), which would
     regenerate credentials and hit exactly the desync above. The README's Option A steps note
     this explicitly: `--yes` is for fresh installs only; migrating an existing sandbox skips
     step 1 entirely and starts from the copied `.env`.
   - `scripts/start.sh`'s auto-created test account (`admin@localhost` / `admin@localhost`,
     extensions `1000/2000/3000` with `pass1000` etc.) must not be created with these fixed
     values outside of an explicit dev/test mode — either gate it behind a flag that defaults
     off, or force a password change/generation on first production-mode run. No such gate
     exists today (`check_test_data_initialized` only checks whether a
     `.test_data_initialized` marker file exists, not a mode flag).
   - This is real code work in the sandbox scripts/compose/CLI files, done as part of this PR
     (in `self-install/`, or upstream in `voipbin/sandbox` first and carried over — see Merge
     mechanism note on timing).
5. Carry the sandbox repo's CI-adjacent config over: move
   `.github/workflows/discord-merge-notify.yml` to repo root (workflows in a subdirectory don't
   run), and provision its `DISCORD_WEBHOOK_URL` secret on `voipbin/voipbin`.
6. Document, in Phase 1 (not defer to Phase 2), the backup and version-pinning capabilities
   sandbox already has: the existing scheduled in-stack DB backup, `versions.lock`-based
   image pinning/update/rollback. These exist today; round-2 review confirmed it's inaccurate to
   describe the installer as having no upgrade/backup story — the gap is that it isn't
   documented as part of the primary install guide yet, which this PR fixes. Document honestly,
   not as more complete than it is: the `database-backup` scheduler ships **disabled** by
   default and is enabled by `start.sh`, and there is no offsite/remote copy of backups today
   (local retention only, 7 snapshots). Anything actually missing (see Phase 2) is scoped there
   instead.
7. Remove the developer-only hardcoded path `/home/pchero/gitvoipbin/monorepo/bin-dbscheme-manager`
   in `scripts/init_database.sh` (a fallback branch, not on the primary path, but inappropriate
   to carry into a public repo's primary installer as-is) — either delete the fallback or make it
   derive from `$HOME` generically.
8. **Centralize Compose project-name derivation (round-5 review finding — blocking, not
   optional):** `setup-voip-network.sh:21`, `start.sh:253`, and `voipbin-cli.py:4553,4564`
   hardcode the literal `sandbox_default`/`sandbox_db_data`/`sandbox_voip-internal` instead of
   deriving the project name the way `setup-host.sh` does. Without this fix, a **fresh** install
   under `self-install/` (project name naturally `self-install`) fails at
   `setup-voip-network.sh`'s network lookup — the migration path only "works" today by
   coincidence, because exporting `COMPOSE_PROJECT_NAME=sandbox` happens to match the hardcoded
   literal. **Round-6 review corrected the fix's scope:** there isn't one derivation to
   centralize, there are already three independent implementations with subtly different
   normalization rules — `setup-host.sh`'s `derive_compose_project_name()` (validates + strips
   leading `[-_]`), `doctor.sh`'s own copy (`doctor_compose_project_name`, no validation), and
   `voipbin-cli.py`'s `_compose_project_name()` (no leading-char stripping, plus a `"voipbin"`
   fallback) — and Python can't call a bash function directly regardless. The fix is: move the
   bash implementation into `common.sh` and have `setup-host.sh`, `setup-voip-network.sh`, and
   `start.sh` share it (three consumers, not four); fold `doctor.sh`'s separate copy into the
   same shared function; have `voipbin-cli.py`'s hardcoded lookups (4553/4564) call its existing
   `_compose_project_name()` helper instead of the literal, and bring its normalization rule in
   line with the shared bash version, with a test pinning that the two agree. See Repository
   Structure Change for detail.
9. A single, minimal README banner in `voipbin/install` pointing existing GCP users at the new
   primary path while confirming their setup keeps working. **This lands as a separate PR in the
   `voipbin/install` repo**, not bundled into this PR (different repo, different review queue) —
   noted here so the rollout isn't announced with only one half of the pair merged.

**Phase 2 (tracked separately, not implemented in this PR):**
- Rotate credentials on an already-running install (via `ALTER USER` / `rabbitmqctl
  change_password` against the live volume), so migrating sandbox operators aren't stuck on the
  legacy defaults indefinitely — deferred per the migration-conflict resolution in Scope item 4.
- Wire `check-env-template-sync.sh` into CI (today it's a manual-only drift check) — folded into
  the installation-script-test-CI item below.
- Remove/replace the macvlan networking requirement (or clearly document it as a hard
  prerequisite with a guided setup path).
- Fix the internal ↔ external DNS mode switch currently requiring a clean host.
- Live E2E re-verification of the `confbridge_join` call-bridge fix (monorepo `bin-call-manager`,
  commit `f1dd2687a`, PR #1033, merged 2026-07-01, unit-tested in
  `pkg/confbridgehandler/ari_event_test.go`). The bug itself is already fixed — this item is
  re-verification only, currently blocked by an unrelated Kamailio external-IP drift issue. Once
  re-verified, remove the stale "Known Limitations" note from `CLAUDE.md`/README.
- Full security review of the Compose stack for internet-facing, single-server production use,
  beyond the credential-hardening baseline shipped in Phase 1.
- Installation script test CI (the sandbox repo's `tests/*.bats` and `scripts/tests/*.py` suites
  currently don't run as CI anywhere; round-2 review flagged that moving the scripts without
  moving their test automation leaves them untested by any pipeline).
- Any remaining upgrade/backup/rollback gaps beyond what Phase 1 documents (e.g., automated
  restore-and-verify, cross-version migration guidance).
- Each item above gets its own follow-up ticket/PR; this spec only lists them so they aren't
  lost, it does not implement them.

**Explicitly out of scope for Phase 1:**
- Any *functional* change to `voipbin/install` (GCP repo) beyond the one README banner (item 9
  above, its own PR).
- Deciding whether `voipbin/sandbox` (the standalone repo) gets archived, kept as a mirror, or
  deleted. Its existing open issues/PRs stay there until that decision is made; the Contributing
  table routes *new* self-install issues to `voipbin/voipbin` going forward.
- Any of the Phase 2 items above.
- The commercial-positioning sign-off described in Motivation — tracked there as a blocking
  precondition on shipping, not something this document resolves.

## Repository Structure Change

### Directory name: `self-install/`

Matches the README's existing `#-self-install-guide` anchor; avoids the name collision with
`voipbin/install` and avoids underselling the directory as "just a sandbox."

### Merge mechanism: plain copy + `HISTORY.md`, not `git subtree`

`git subtree`'s only advantage (preserved per-file history via `git log --follow`) is destroyed
by the org's mandatory squash-merge policy, which collapses the import into one commit at merge
time. Plain copy avoids that dead-end.

**Copy source, explicit:** a clean clone of `voipbin/sandbox` at a pinned commit (or
`git archive <ref> | tar -x` into `self-install/`), **not** a `cp -r` of the local working
checkout at `~/gitvoipbin/sandbox`. The local checkout's `.gitignore` excludes `.env`, `certs/`,
`tmp/` but **not** `.worktrees/`, and a stray `.worktrees/NOJIRA-Sandbox-versions-lock-refresh/`
checkout currently exists there — a raw copy would drag that whole worktree into the commit.
`self-install/HISTORY.md` records the source commit hash and a link to
`https://github.com/voipbin/sandbox` for pre-move history.

If the credential-hardening work (Scope item 4) lands upstream in `voipbin/sandbox` first and is
then carried over, the copy is taken from that post-hardening commit, not before it — Phase 1
must not ship the pre-hardening defaults into `voipbin/voipbin` even transiently.

### Directory rename and the Compose project name — env var, not `.env` file

Round-1 review found that renaming the checkout directory from `sandbox` to `self-install`
changes the Compose project name (`scripts/setup-host.sh`'s `derive_compose_project_name()`
falls back to `basename "$PROJECT_DIR"`), which changes network and volume names
(`sandbox_default`/`sandbox_db_data` → `self-install_default`/`self-install_db_data`), silently
"losing" an existing installation's data.

Round-2 review found the originally-proposed fix (put `COMPOSE_PROJECT_NAME=sandbox` in `.env`)
doesn't work: `setup-host.sh` and `doctor.sh` read `COMPOSE_PROJECT_NAME` only from the shell
environment, never from `.env` (they don't source it for this value). Corrected fix: anyone
migrating an existing sandbox checkout must `export COMPOSE_PROJECT_NAME=sandbox` in their shell
(or persist it in their shell profile) before running `setup-host.sh`/`doctor.sh`/`start.sh` at
the new location — this is a documentation instruction, not a `.env` edit.

**Round-5 review found this only fixes `setup-host.sh`'s own network — three more places
hardcode the literal string `sandbox_default` / `sandbox_db_data` instead of deriving it, and
none of them have an env override:**
- `scripts/setup-voip-network.sh:21` — `NETWORK_NAME="sandbox_default"`, used to look up the
  bridge interface for VoIP traffic. Fails hard (`exit 1`, "Docker network 'sandbox_default'
  does not exist") if the project name isn't literally `sandbox`.
- `scripts/start.sh:253` — `grep -q 'sandbox_db_data'` as a first-run detection heuristic.
- `scripts/voipbin-cli.py:4553,4564` — diagnostic network inspection for `sandbox_voip-internal`
  / `sandbox_default`.

This means a **fresh install** under `self-install/` — which derives project name
`self-install` and creates `self-install_default` — is what actually breaks today, not the
migration path (which happens to work by coincidence, since exporting
`COMPOSE_PROJECT_NAME=sandbox` matches these hardcoded literals). Phase 1 must fix this before
`self-install/` can be a working fresh-install target at all:
- Round-6 review found three independent implementations already exist, not one to reuse and one
  to fix: `setup-host.sh`'s `derive_compose_project_name()` (validates input, strips leading
  `[-_]`), `doctor.sh`'s separate copy (`doctor_compose_project_name`, no validation), and
  `voipbin-cli.py`'s `_compose_project_name()` (different normalization, `"voipbin"` fallback;
  Python can't call a bash function regardless of centralization). Fix: move the bash
  implementation into `common.sh`, shared by `setup-host.sh`, `setup-voip-network.sh`, and
  `start.sh`, absorbing `doctor.sh`'s separate copy into the same function; have
  `voipbin-cli.py`'s hardcoded lookups (4553/4564) call its existing `_compose_project_name()`
  instead of the literal, with its normalization rule brought in line with the bash version and
  pinned by a test.
- With that in place, new installs work with no special instruction (project name naturally
  derives to `self-install`), and migrating installs work by exporting
  `COMPOSE_PROJECT_NAME=sandbox` as described above — both paths go through equivalent derivation
  logic instead of one being a hardcoded coincidence.

### Path portability check

Sandbox's scripts derive `PROJECT_DIR` from `SCRIPT_DIR`, not from a hardcoded repo name —
confirmed via grep across `scripts/*.sh`. A first grep pass on this point (round-1/round-2
review) missed the hardcoded `sandbox_default`/`sandbox_db_data` literals now covered under
Repository Structure Change above (round-5 review); those are a distinct kind of path
dependency (Compose network/volume naming, not `PROJECT_DIR` resolution) and are fixed as a
Scope item, not left as a caveat. `migrate.sh` / `generate-versions-lock.sh` default to
`$HOME/gitvoipbin/monorepo` for a developer-only cross-repo lookup (overridable via env var) —
pre-existing, left as-is. One item does need cleanup before this becomes the repo's public
primary installer:
`scripts/init_database.sh` hardcodes the author's personal absolute path
(`/home/pchero/gitvoipbin/monorepo/bin-dbscheme-manager`) in a fallback branch — see Scope item 7.

### CI

Move `.github/workflows/discord-merge-notify.yml` to `voipbin/voipbin/.github/workflows/`
(GitHub Actions doesn't run workflows from a nested path) and provision its
`DISCORD_WEBHOOK_URL` secret on `voipbin/voipbin`. Note this is a notification workflow, not a
test suite — see Phase 2 for actual installation-script test CI, which doesn't exist today in
either repo.

## README Changes

Reference points in the current root `README.md` that get updated:
- Badge at line 33 (`installer` label pointing at `voipbin/install` releases)
- The "Self-Install" summary card, lines ~299–312 ("Deploy VoIPBin on your own cloud
  infrastructure" — becomes inaccurate once Docker Compose/single-server is the primary path;
  needs rewording to cover both options)
- "Self-Install Guide" section, lines 378–481 (the GCP 3-stage pipeline walkthrough)
- Repositories table rows for `voipbin/sandbox` (~540) and `voipbin/install` (~536)
- Documentation section bullet, line ~548 ("Examples & Sandbox")
- Contributing table rows, line ~564–565

Restructured as two documented options (keeps `voipbin/install` visible, satisfying "install
stays available"), following the README's existing "Cloud vs Self-host" two-column pattern:

- **Option A — Single-Server Docker Compose (primary, recommended):** points at `self-install/`.
  Entrypoint for a **fresh install**, matching sandbox's actual documented flow (round-2 review
  corrected an earlier draft that collapsed this to a single `init` command):
  1. `./scripts/init.sh --yes` (unprivileged — generates `.env`/certs)
  2. `sudo ./scripts/setup-host.sh` (the one privileged step — VoIP network interfaces, DNS)
  3. `./scripts/start.sh` (brings up all services, runs migrations)
  4. `./scripts/check-install.sh` (verifies the result)

  For an operator **migrating an existing `voipbin/sandbox` checkout**, step 1 is replaced: copy
  the full generated-artifact set from the existing checkout into the new location (defined as
  whatever `voipbin-cli.py`'s `clean --purge` command enumerates — see the existing-install
  migration note under Scope item 4 for the full list and why `.env` alone isn't enough) and
  `export COMPOSE_PROJECT_NAME=sandbox`, instead of running `init.sh --yes` — steps 2–4 are
  unchanged.
- **Option B — GCP + Kubernetes (existing, still supported):** the current 3-stage pipeline
  content, moved under this subheading, still linking to `voipbin/install` for full docs.

Other changes:
1. Repositories table: `voipbin/sandbox` row description updated to note the installer's primary
   location moved to `voipbin/voipbin/self-install/`; `voipbin/install` row unchanged.
2. Contributing table: "Deployment / self-hosting (Docker Compose)" → `voipbin/voipbin`; a
   second row "Deployment / self-hosting (GCP/K8s)" keeps pointing at `voipbin/install`.
3. "This repo is the project hub with no code of its own" line: rewritten to name the one
   exception (`self-install/`) and note that this repo now carries CI and code-review scope for
   that directory, alongside its existing docs-only role for everything else.

### Bootstrap mechanism

No new bootstrap script. Phase 1 uses sandbox's existing documented flow: `git clone` the
`voipbin/voipbin` repo, then the 4-step sequence above from `self-install/`. A full clone also
pulls `docs/images/` (several MB of PNGs/GIFs) — a minor, non-blocking rough edge, not worth a
sparse-checkout instruction for Phase 1.

## Verification Plan

`./scripts/doctor.sh` (the read-only health-check entrypoint) confirms relocation didn't break
its own path resolution — it does **not** exercise `init.sh`/`setup-host.sh`/`start.sh`'s path
handling. Since those are unsafe to run against a host with an existing install (see below), the
actual regression net for this move is sandbox's existing test suites, run from the new
`self-install/` location — but the two suites are not equivalent and shouldn't be run the same
way:

- The `tests/*.bats` suite (12 files) is self-contained and safe to run immediately after the
  move, no live stack required.
- The `scripts/tests/*.py` suite (3 files) is a **live-stack integration test**, not a unit
  test — e.g. `test_backup_restore_live.py` runs against an actual Compose project
  (`COMPOSE_PROJECT_NAME="voipbin-test"`). It requires the same disposable/clean-host
  precondition as a full `init` run, not a quick post-move check.

- Run the full `tests/*.bats` suite from `self-install/` post-move immediately; confirm no
  path-resolution regressions. Run `scripts/tests/*.py` only on a disposable host, alongside the
  full-stack check below.
- `init.sh`/`setup-host.sh` make real host changes (CoreDNS Corefile generation, replacing
  `/etc/resolv.conf` via `setup-dns.sh -y`, installing an mkcert CA into the system trust store)
  — not dry-runs. Any full `init`→`setup-host`→`start` check runs only on a disposable/clean
  host or VM, never on a host with an existing sandbox install (macvlan interface and DNS
  collision risk).
- Verify the Compose-project-name migration path directly: with `COMPOSE_PROJECT_NAME=sandbox`
  exported, run `setup-host.sh`'s network-derivation logic (or the equivalent doctor check) and
  confirm it resolves to `sandbox_default`, not `self-install_default` — checking
  `docker compose config` alone doesn't exercise this path, since project-name derivation lives
  in `setup-host.sh`, not in Compose itself.
- Manual read-through of the rewritten README sections for broken links/anchors, covering every
  reference point listed above.
- Confirm the moved CI workflow triggers correctly from repo root with its secret provisioned.
- Confirm the credential-hardening change (Scope item 4): a fresh `init` produces non-default
  MySQL/RabbitMQ/AMI values (compose, `start.sh`, `init_database.sh`, `migrate.sh`,
  `voipbin-cli.py` all consistent — `doctor.sh`'s own `${MYSQL_ROOT_PASSWORD:-root_password}`
  fallback is fine as-is since it's a container-internal default that receives the real value
  from the environment, not a literal that needs removing), `JWT_KEY` stays generated
  (regression check only — this already works today), and the `admin@localhost` test account is
  not auto-created outside of an explicit dev-mode flag.
- Confirm Scope item 7: `init_database.sh` no longer references the personal `/home/pchero/...`
  path.
- Confirm Scope item 8: a fresh `self-install/` install (no `COMPOSE_PROJECT_NAME` override)
  succeeds through `setup-voip-network.sh` instead of failing on a hardcoded `sandbox_default`
  lookup; also confirm the `start.sh` first-run heuristic and `voipbin-cli.py` diagnostics
  resolve correctly under both a fresh (`self-install`) and migrated (`sandbox`) project name.
- Migration path, end to end (this class of gap has been found twice by review, so it gets its
  own explicit check rather than relying on the file list being complete): reproduce an existing
  sandbox install on a disposable host, migrate it following only the documented copy-set
  instructions, and confirm afterward that (a) the admin account password was *not* reset to
  `admin@localhost`, (b) existing image pins / rollback history in `docker-compose.override.yml`
  / `.voipbin-versions/` are still present and honored, and (c) all services start successfully
  against the preserved volumes.

## Open Questions (flagged, not blocking Phase 1 engineering review — the commercial-positioning
sign-off in Motivation is a separate, blocking precondition on shipping)

- Fate of the standalone `voipbin/sandbox` repo post-merge — deferred per explicit decision.
- Whether `voipbin/sandbox`'s own CI/Discord notifications should be disabled once
  `voipbin/voipbin` takes over as the canonical location, to avoid duplicate notifications
  during the coexistence window.
- Whether the credential-hardening code change (Scope item 4) lands upstream in
  `voipbin/sandbox` first (then carried over) or directly in `self-install/` as part of this
  PR — an implementation-sequencing call for the write-up plan, not this design doc.
