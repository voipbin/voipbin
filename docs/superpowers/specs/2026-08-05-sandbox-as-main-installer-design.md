# Sandbox as the Main Installer — Design

## Motivation

Today `voipbin/voipbin` positions `voipbin/install` (GCP + Terraform + Ansible + Kubernetes)
as *the* self-install path, and lists `voipbin/sandbox` (Docker Compose, single-host) only as
"Examples & sandbox" — a dev/test toy, not an install method.

Decision (confirmed with the CEO/CTO): flip this. The Docker Compose stack in
`voipbin/sandbox` becomes the **primary, documented self-install method** for VoIPBin,
including production use on a single server. `voipbin/install` (GCP/K8s) stays in place
unchanged for now; its long-term fate is a separate future decision, out of scope here.

## Scope

**Phase 1 (this spec, this PR):**
- Bring the sandbox codebase into `voipbin/voipbin` as an in-repo directory (`self-install/`),
  with git history preserved.
- Rewrite the README's "Self-Install Guide" section to document the in-repo installer as the
  main path.
- Update the repositories table / contributing table to reflect the new structure.
- Carry over the sandbox repo's CI workflow that's still relevant.

**Phase 2 (tracked separately, not implemented in this PR):**
Production-hardening backlog for the Docker Compose stack, since it's now the officially
documented production install method, not just a local dev tool:
- Remove/replace the macvlan networking requirement (or clearly document it as a hard
  prerequisite with a guided setup path).
- Fix the internal ↔ external DNS mode switch currently requiring a clean host.
- Fix the `confbridge_join` flow timing bug (calls ending early).
- Security review of the Compose stack for internet-facing, single-server production use
  (previously only reviewed as a local dev environment).
- Each item above gets its own follow-up ticket/PR; this spec only lists them so they aren't
  lost, it does not implement them.

**Explicitly out of scope for Phase 1:**
- Any change to `voipbin/install` (GCP repo) itself.
- Deciding whether `voipbin/sandbox` (the standalone repo) gets archived, kept as a mirror, or
  deleted — deferred per the CEO/CTO's "decide later" call.
- Any of the Phase 2 hardening work.

## Repository Structure Change

### Directory name: `self-install/`, not `sandbox/` or `install/`

- `install/` would collide in meaning with the separate `voipbin/install` repo — confusing.
- `sandbox/` undersells its new role as the primary install method and would read as "just a
  toy" to a reader who hasn't seen this design doc.
- `self-install/` matches the README's existing `#-self-install-guide` anchor and states its
  purpose plainly.

### Merge mechanism: `git subtree`, not a fresh copy

```bash
git remote add sandbox-src https://github.com/voipbin/sandbox.git
git fetch sandbox-src main
git subtree add --prefix=self-install sandbox-src main
```

This preserves full commit history for every file under `self-install/`, visible via
`git log --follow`, instead of landing as one big "add sandbox files" commit. It also means a
future `git subtree pull` can re-sync if `voipbin/sandbox` keeps receiving fixes in parallel
during a transition window (not required by this spec, just kept available).

### Path portability check (done)

Sandbox's scripts derive `PROJECT_DIR` from `SCRIPT_DIR` (`dirname` of the script's own
location), not from a hardcoded repo name or absolute path. Confirmed via grep across
`scripts/*.sh` — no script assumes it lives at repo root under a specific name. Nesting the
tree under `voipbin/voipbin/self-install/` is safe from that angle.

### CI

`voipbin/sandbox` carries one workflow, `.github/workflows/discord-merge-notify.yml`. It gets
folded into `voipbin/voipbin`'s CI config (adjusting any path filters to `self-install/**`).
`voipbin/voipbin` currently has no code-oriented CI (it's a docs-only repo today) — this is the
first CI this repo will run. Called out here so it isn't a surprise during review.

## README Changes

1. **Self-Install Guide section** (currently pipes `curl | bash` against
   `voipbin/install/main/install.sh`): rewritten to bootstrap from
   `voipbin/voipbin/main/self-install/...` instead, using the sandbox's existing
   `./voipbin` entrypoint and `init` / `start` commands in place of the Terraform/Ansible/K8s
   3-stage pipeline description.
2. **Repositories table**: `voipbin/sandbox` row description changes from "Sandbox & examples"
   to reflect that the installer now lives in `voipbin/voipbin`; `voipbin/install` row stays,
   description unchanged (still a valid GCP option).
3. **Contributing table**: "Deployment / self-hosting" row points at `voipbin/voipbin` (this
   repo) instead of `voipbin/install`, since that's now where the primary installer's code and
   issues live.
4. **"This repo is the project hub with no code of its own"** line (Contributing section):
   this claim becomes false the moment `self-install/` lands here. It's rewritten to note the
   one exception explicitly, rather than silently going stale.

## Verification Plan

- Inside the worktree, after the subtree merge: run `self-install/voipbin init` and
  `self-install/voipbin doctor` (or the equivalent existing sandbox smoke checks) from the new
  path to confirm nothing broke from the relocation. Full `start.sh` end-to-end (all 25+
  containers) is expensive — a doctor/init dry-run is the right bar for this PR; full E2E stays
  the responsibility of the sandbox repo's own existing test suite, which moves over intact.
- `git log --follow self-install/README.md` (or similar) to confirm history rode along with the
  subtree merge.
- Manual read-through of the rewritten README sections for broken links/anchors.

## Open Questions (flagged, not blocking Phase 1)

- Fate of the standalone `voipbin/sandbox` repo post-merge — deferred per explicit decision.
- Whether `voipbin/sandbox`'s own CI/Discord notifications should be disabled once
  `voipbin/voipbin` takes over as the canonical location, to avoid duplicate notifications
  during the coexistence window.
