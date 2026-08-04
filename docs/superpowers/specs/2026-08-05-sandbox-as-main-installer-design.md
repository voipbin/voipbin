# Sandbox as the Main Installer — Design

_Revision 2 — incorporates round-1 review findings (see PR discussion for the round-1 report)._

## Motivation

Today `voipbin/voipbin` positions `voipbin/install` (GCP + Terraform + Ansible + Kubernetes)
as *the* self-install path, and lists `voipbin/sandbox` (Docker Compose, single-host) only as
"Examples & sandbox" — a dev/test toy, not an install method.

Decision (confirmed with the CEO/CTO): flip this. The Docker Compose stack in
`voipbin/sandbox` becomes the **primary, documented self-install method** for VoIPBin,
including production use on a single server. `voipbin/install` (GCP/K8s) stays available as a
secondary option; its long-term fate is a separate future decision, out of scope here.

## Scope

**Phase 1 (this spec, this PR):**
- Bring the sandbox codebase into `voipbin/voipbin` as an in-repo directory (`self-install/`).
- Rewrite the root README so both self-install paths are documented side by side, with the
  Docker Compose path as primary.
- Rewrite `self-install/README.md`'s security section: replace "local dev only, don't expose"
  with "these are the defaults, here is what to change before exposing this to the internet."
- Add a default-credential rotation step to the installer's `init` flow (or, if that's too large
  a code change for this PR, a documented manual procedure) so "production" isn't shipped with
  `root`/`root_password` and `guest`/`guest` reachable from the internet on day one. **This is
  a Phase 1 blocker, not a Phase 2 nice-to-have** — see round-1 review finding B1.
- Carry the sandbox repo's CI workflow over, adjusted to live at repo root.
- Add a minimal, explicit-scope exception to "no changes to `voipbin/install` itself": a single
  README banner in `voipbin/install` pointing existing GCP users at the new primary path while
  confirming their setup keeps working. This is a stakeholder-communication necessity, not a
  functional change to the installer — see round-1 finding I5.

**Phase 2 (tracked separately, not implemented in this PR):**
- Remove/replace the macvlan networking requirement (or clearly document it as a hard
  prerequisite with a guided setup path).
- Fix the internal ↔ external DNS mode switch currently requiring a clean host.
- Live E2E re-verification of the `confbridge_join` call-bridge fix (monorepo `bin-call-manager`,
  commit `f1dd2687a`, PR #1033, merged 2026-07-01). The bug itself is **already fixed** — this
  item is re-verification only, currently blocked by an unrelated Kamailio external-IP drift
  issue, not a bug fix (round-1 review corrected this; the original draft mis-stated it as an
  open bug).
- Full security review of the Compose stack for internet-facing, single-server production use.
- Upgrade / backup / rollback story for the installed stack (version pin/bump via
  `versions.lock`, backup procedure, rollback procedure). Flagged in round-1 review as missing
  from any phase; a production installer needs this eventually, but it is not required to ship
  Phase 1 since GCP install remains available for operators who need it today.
- Each item above gets its own follow-up ticket/PR; this spec only lists them so they aren't
  lost, it does not implement them.

**Explicitly out of scope for Phase 1:**
- Any *functional* change to `voipbin/install` (GCP repo) — the one README banner above is
  communication-only, not a behavior change.
- Deciding whether `voipbin/sandbox` (the standalone repo) gets archived, kept as a mirror, or
  deleted — deferred per the CEO/CTO's "decide later" call. Its existing open issues/PRs stay
  there until that decision is made; the Contributing table routes *new* self-install issues to
  `voipbin/voipbin` going forward.
- Any of the Phase 2 hardening/upgrade-story work above.
- **Business/commercial positioning:** `sandbox/README.md` currently tells production users to
  "use the official VoIPBin cloud service or contact us for on-premise licensing." Making
  self-hosted production a first-class, unrestricted, documented path is a commercial
  positioning change, not just a docs change. **This needs explicit CEO sign-off before the
  README rewrite ships**, separate from the engineering design approval this spec is asking
  for. Flagging here so it isn't missed; not resolving it in this document.

## Repository Structure Change

### Directory name: `self-install/`, not `sandbox/` or `install/`

- `install/` would collide in meaning with the separate `voipbin/install` repo — confusing.
- `sandbox/` undersells its new role as the primary install method.
- `self-install/` matches the README's existing `#-self-install-guide` anchor.

### Merge mechanism: plain copy + `HISTORY.md`, not `git subtree`

Round-1 review flagged a real conflict: the org's mandatory squash-merge policy collapses any
subtree-imported history into a single commit at merge time, which defeats the entire reason to
use `git subtree` (`git log --follow` becomes meaningless post-merge). Requesting a squash
exception for one PR is a bigger ask than this integration warrants.

Revised approach: copy the current `voipbin/sandbox` tree into `self-install/` as a normal
`git add`, and add `self-install/HISTORY.md` with a link to `https://github.com/voipbin/sandbox`
and a note that full commit history for everything under this directory prior to the move lives
in that repo (which is not deleted — see Scope). This is simpler, has no policy conflict, and
loses nothing a reader can't already get from the linked repo.

### Directory rename breaks existing installs' Docker volumes — needs a migration note

Round-1 review caught this: `scripts/setup-host.sh` derives the Compose project name from
`basename "$PROJECT_DIR"` when `COMPOSE_PROJECT_NAME` isn't set. Renaming the checkout directory
from `sandbox` to `self-install` changes the project name, which changes the network
(`sandbox_default` → `self-install_default`) and **volume names**
(`sandbox_db_data` → `self-install_db_data`). An existing sandbox user who pulls the new location
and re-runs `init`/`start` would appear to lose their database and recordings.

**Mitigation for Phase 1:** document that anyone migrating an existing sandbox checkout must set
`COMPOSE_PROJECT_NAME=sandbox` in their `.env` before first run at the new location, to keep
resolving to the pre-existing volumes. New installs from `voipbin/voipbin` get no such
instruction and default to `self-install` naturally.

### Path portability check (done, with one caveat)

Sandbox's scripts derive `PROJECT_DIR` from `SCRIPT_DIR` (`dirname` of the script's own
location), not from a hardcoded repo name — confirmed via grep across `scripts/*.sh`. The one
exception is the Compose project name behavior above, which is handled via the migration note,
not a code change. (`migrate.sh` and `generate-versions-lock.sh` also contain a
`$HOME/gitvoipbin/monorepo` default for a developer-only cross-repo lookup, overridable via env
var; this is pre-existing sandbox-repo behavior, unrelated to the directory move, left as-is.)

### CI

`voipbin/sandbox` carries one workflow, `.github/workflows/discord-merge-notify.yml`. A plain
copy (not subtree) means this file needs to be **explicitly moved** to
`voipbin/voipbin/.github/workflows/` at repo root — GitHub Actions only runs workflows from the
root `.github/workflows/`, not from a nested directory's copy of that path. Its Discord webhook
secret must also be provisioned on `voipbin/voipbin` (it currently lives on `voipbin/sandbox`
only). `voipbin/voipbin` has no other CI today (it's been a docs-only repo) — this is the first
CI this repo runs, called out so it isn't a surprise in review.

## README Changes

Full list of `voipbin/install` references in the current root `README.md` that need updating
(round-1 review found the original draft only caught one of these):
- Badge at line 33 (`installer` label pointing at `voipbin/install` releases)
- "Self-Install Guide" section, lines 378–481 (the GCP 3-stage pipeline walkthrough)
- Repositories table row (line ~536)
- Documentation section, line ~548 ("Examples & Sandbox" bullet)
- Contributing table row (line ~564)

Rather than deleting the GCP path to make room for the Compose path (which would contradict the
"install stays available" scope decision), the Self-Install Guide section is restructured into
two documented options, following the same two-column pattern the README already uses for
"Cloud vs Self-host":

- **Option A — Single-Server Docker Compose (primary, recommended):** points at
  `self-install/`, `git clone` + `cd voipbin/self-install && sudo ./voipbin init` as the
  entrypoint (see bootstrap note below — no `curl | bash` for this option, unlike the current
  GCP flow).
- **Option B — GCP + Kubernetes (existing, still supported):** the current 3-stage pipeline
  content, moved under this subheading largely as-is, still linking to `voipbin/install` for
  full docs.

Other changes:
1. Repositories table: `voipbin/sandbox` row description updated to note the installer's primary
   location moved to `voipbin/voipbin/self-install/`; `voipbin/install` row description
   unchanged (still a valid, supported option).
2. Contributing table: "Deployment / self-hosting (Docker Compose)" row points at
   `voipbin/voipbin`; a second row, "Deployment / self-hosting (GCP/K8s)", keeps pointing at
   `voipbin/install`.
3. "This repo is the project hub with no code of its own" line (Contributing section): rewritten
   to name the one exception (`self-install/`) explicitly, along with a short note on what that
   means going forward — this repo now carries its own CI and code-review burden for that one
   directory, alongside its existing docs-only workflow for everything else.

### Bootstrap mechanism (resolved — no new script)

Round-1 review flagged that the original draft implied a `curl | bash` bootstrap analogous to
`voipbin/install`'s, which doesn't exist for sandbox and isn't being written in this PR. Phase 1
uses the same entrypoint sandbox already documents: `git clone`, then run `./voipbin init` /
`./voipbin start` from the `self-install/` subdirectory. No new bootstrap script is in scope.

A full clone of `voipbin/voipbin` also pulls `docs/images/` (several MB of PNGs/GIFs). This is a
minor, non-blocking rough edge — not worth a sparse-checkout instruction for Phase 1 given the
repo is already meant to be cloned by contributors — but noted here so it isn't rediscovered
later as a surprise.

## Verification Plan

Round-1 review corrected a factual error in the original draft: `self-install/voipbin init`
is **not** a dry-run. It writes a CoreDNS Corefile, calls `setup-dns.sh -y` which replaces
`/etc/resolv.conf`, and installs an mkcert CA into the system trust store — all host-level
changes, not read-only checks. Running it against a host that already has a sandbox install
active risks colliding with the existing macvlan interfaces and DNS config.

Revised plan:
- Run `self-install/voipbin doctor` (read-only, safe at any point) from the new path to confirm
  the relocation didn't break script path resolution.
- If an end-to-end `init`/`start` check is wanted, run it only on a disposable/clean host or VM,
  never on a host with an existing sandbox install.
- Manually verify the migration note above by simulating an existing install: set
  `COMPOSE_PROJECT_NAME=sandbox`, confirm `docker compose config` resolves to the pre-existing
  network/volume names.
- Manual read-through of the rewritten README sections for broken links/anchors (including the
  five reference points listed above).
- Confirm the moved CI workflow (`.github/workflows/discord-merge-notify.yml`) triggers correctly
  from repo root with its secret provisioned.

## Open Questions (flagged, not blocking Phase 1 engineering review — see Scope for the
commercial-positioning sign-off, which does block shipping)

- Fate of the standalone `voipbin/sandbox` repo post-merge — deferred per explicit decision.
- Whether `voipbin/sandbox`'s own CI/Discord notifications should be disabled once
  `voipbin/voipbin` takes over as the canonical location, to avoid duplicate notifications
  during the coexistence window.
- Whether default-credential rotation (Scope, Phase 1 blocker) is implemented as an `init`-time
  prompt/generator or a documented manual step — an implementation-level call for the write-up
  plan, not this design doc.
