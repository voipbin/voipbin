# Sandbox as the Main Installer — Design

_Revision 4 — incorporates round-1, round-2, and round-3 review findings (see PR discussion for
all three reports)._

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
   - test fixtures (`scripts/tests/test_backup_restore_live.py`, `tests/config.bats`)

   Phase 1 must parameterize **all of the above together**, not just the compose file, plus:
   - Add the corresponding keys to `.env.template` (required — `check-env-template-sync.sh`
     enforces `.env`/`.env.template` key parity and will fail CI otherwise), with `init`
     generating random values by default instead of shipping `root_password`/`guest`/`guest`/
     `asterisk` as literals.
   - `JWT_KEY` generation is **already handled** — `init.sh` already calls
     `generate_random_key()` and compose already defaults to an empty string, not a shipped
     secret (round-3 review found this is not new work). The only loose end is
     `.env.template`'s `JWT_KEY=your-random-jwt-secret-key` placeholder, which should be cleaned
     up for consistency with the other rotated keys, plus a regression check that this stays
     true (see Verification Plan).
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
8. Remove the developer-only hardcoded path `/home/pchero/gitvoipbin/monorepo/bin-dbscheme-manager`
   in `scripts/init_database.sh` (a fallback branch, not on the primary path, but inappropriate
   to carry into a public repo's primary installer as-is) — either delete the fallback or make it
   derive from `$HOME` generically.
7. A single, minimal README banner in `voipbin/install` pointing existing GCP users at the new
   primary path while confirming their setup keeps working. **This lands as a separate PR in the
   `voipbin/install` repo**, not bundled into this PR (different repo, different review queue) —
   noted here so the rollout isn't announced with only one half of the pair merged.

**Phase 2 (tracked separately, not implemented in this PR):**
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
- Any *functional* change to `voipbin/install` (GCP repo) beyond the one README banner (item 7
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
the new location — this is a documentation instruction, not a `.env` edit. New installs default
to `self-install` naturally and need no such instruction.

### Path portability check

Sandbox's scripts derive `PROJECT_DIR` from `SCRIPT_DIR`, not from a hardcoded repo name —
confirmed via grep across `scripts/*.sh`, with the Compose-project-name caveat handled above.
`migrate.sh` / `generate-versions-lock.sh` default to `$HOME/gitvoipbin/monorepo` for a
developer-only cross-repo lookup (overridable via env var) — pre-existing, left as-is. One item
does need cleanup before this becomes the repo's public primary installer:
`scripts/init_database.sh` hardcodes the author's personal absolute path
(`/home/pchero/gitvoipbin/monorepo/bin-dbscheme-manager`) in a fallback branch — see Scope item 8.

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
  Entrypoint, matching sandbox's actual documented flow (round-2 review corrected an earlier
  draft that collapsed this to a single `init` command):
  1. `./scripts/init.sh --yes` (unprivileged — generates `.env`/certs)
  2. `sudo ./scripts/setup-host.sh` (the one privileged step — VoIP network interfaces, DNS)
  3. `./scripts/start.sh` (brings up all services, runs migrations)
  4. `./scripts/check-install.sh` (verifies the result)
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
  `voipbin-cli.py` all consistent, no residual literal fallback), `JWT_KEY` stays generated
  (regression check only — this already works today), and the `admin@localhost` test account is
  not auto-created outside of an explicit dev-mode flag.
- Confirm Scope item 8: `init_database.sh` no longer references the personal `/home/pchero/...`
  path.

## Open Questions (flagged, not blocking Phase 1 engineering review — the commercial-positioning
sign-off in Motivation is a separate, blocking precondition on shipping)

- Fate of the standalone `voipbin/sandbox` repo post-merge — deferred per explicit decision.
- Whether `voipbin/sandbox`'s own CI/Discord notifications should be disabled once
  `voipbin/voipbin` takes over as the canonical location, to avoid duplicate notifications
  during the coexistence window.
- Whether the credential-hardening code change (Scope item 4) lands upstream in
  `voipbin/sandbox` first (then carried over) or directly in `self-install/` as part of this
  PR — an implementation-sequencing call for the write-up plan, not this design doc.
