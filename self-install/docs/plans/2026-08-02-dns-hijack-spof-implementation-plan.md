# VOIP-1285: Implementation Plan

Implements the approved design at
`docs/plans/2026-08-02-dns-hijack-spof-design.md`. Steps are ordered so
each is independently testable before the next begins.

## Step 1 — `common.sh`: shared capture/refresh helper

Add to `scripts/common.sh` (near the other DNS/env helpers):

- `RESOLV_UPSTREAMS="${RESOLV_UPSTREAMS:-/etc/resolv.conf.voipbin-upstreams}"`
  **and** `RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"`, both in
  `common.sh`. `RESOLV_CONF` doesn't currently exist anywhere —
  `setup-dns.sh` hardcodes the literal path `/etc/resolv.conf` at lines
  41-47, 53-59, 90-96, **and also at 102-107** (`uninstall_linux`'s
  no-backup-found branch, easy to miss since it's a separate `else` far
  from the other three) — all four sites move to `$RESOLV_CONF` in Steps
  2 and 3 below, not just the first three. The new helpers below read/
  write it, and Step 1's own bats coverage requires overriding it to avoid
  touching
  real `/etc` unprivileged. Introducing it is in scope here: replace
  those hardcoded literals in `setup-dns.sh` with `$RESOLV_CONF` as part
  of Step 2, not just inside the new helpers, so there's one source of
  truth for the path, not a mix of `$RESOLV_CONF` in new code and a
  literal in old code. (`RESOLV_BACKUP` stays a `setup-dns.sh`-local
  hardcoded var, unchanged, per design §1 "Testability" — only the
  *new* file plus this now-necessary `RESOLV_CONF` need to be
  overridable.)
- `capture_dns_upstreams(mode)`, `mode` is `initial` or `refresh` —
  **not a single unparameterized function**. This directly implements
  design §1's bolded requirement: "refresh must use only capture source 1
  or 3, never source 2."
  - `initial`: full three-source priority list (systemd-resolved live
    file → plain resolv.conf → Corefile forward targets). Used only for
    the very first capture, before `/etc/resolv.conf` has ever been
    rewritten by this script.
  - `refresh`: **skips source 2 entirely** — systemd-resolved live file,
    else straight to Corefile forward targets. At refresh time
    `/etc/resolv.conf` is already our own generated file, so source 2
    would just recover the previous (possibly stale) fallback instead of
    finding a new upstream.
  - Source 3 ("Corefile forward targets") is a **hardcoded literal pair,
    not a file read**: `common.sh:471`'s Corefile-generation heredoc
    itself hardcodes `forward . 8.8.8.8 8.8.4.4` — there is no existing
    "read the forward targets out of the generated Corefile" capability
    to reuse, and building one to read back a value this same codebase
    just hardcoded would be needless indirection. Source 3 is simply:
    write `8.8.8.8` and `8.8.4.4` to `$RESOLV_UPSTREAMS`, literally,
    matching that constant. No "Corefile absent" case exists because
    nothing is read from it.
  - Both modes apply the same loopback/link-local/IPv6-loopback filter +
    dedupe uniformly to whichever source supplied lines, cap at 2 entries
    (so `127.0.0.1` + these fits `MAXNS=3`), and write the result one
    `nameserver`-per-line to `$RESOLV_UPSTREAMS`, overwriting
    unconditionally — the caller decides *whether* to call it (write-once
    vs. refresh gating lives in each call site, per Steps 2/4/5), the
    helper itself doesn't guard on file existence.
- `write_resolv_conf_with_fallback()`: reads `$RESOLV_UPSTREAMS` (must
  exist and contain at least one `nameserver` line — see the guard note
  in Step 2, this function does not itself validate that, it's the
  caller's contract), then **`rm -f "$RESOLV_CONF"` before writing** —
  this is not optional. Because cleanup/restart now runs *before* this
  write (Step 2.3), `/etc/resolv.conf` may at this point be a
  systemd-resolved-recreated symlink to `/run/systemd/resolve/
  stub-resolv.conf`; writing through a symlink with `cat > "$RESOLV_CONF"`
  without unlinking first would silently write into that stub target
  instead of replacing it with our static file — exactly the class of
  bug this whole reorder exists to avoid. Then write the full
  `/etc/resolv.conf` content (comment header + `nameserver 127.0.0.1` +
  up to 2 lines from `$RESOLV_UPSTREAMS` + `options timeout:1
  attempts:2`). No parameters — the content doesn't depend on `host_ip`.

Rationale for putting these in `common.sh` rather than `setup-dns.sh`:
design's round-5 finding requires the same capture+refresh logic
reachable from `setup_linux()` (in `setup-dns.sh`),
`setup-voip-network.sh`'s IP-changed block, and `regenerate_corefile()`
(also in `setup-dns.sh`) — `common.sh` is the shared file all three
already source.

**Test**: new `tests/common-dns-upstreams.bats` — `initial` capture from a
stubbed `/run/systemd/resolve/resolv.conf`; `initial` capture from a
stubbed plain resolv.conf with loopback/link-local lines present (verify
excluded); `initial` capture with nothing usable (verify
Corefile-forward-target fallback); `refresh` mode never reads a stubbed
plain `/etc/resolv.conf` even when one is present (proves source 2 is
skipped); `refresh` falls through to Corefile targets when systemd-resolved
is absent; `MAXNS` cap at 3 total lines; dedupe. Use
`RESOLV_CONF`/`RESOLV_UPSTREAMS` env overrides to avoid touching real
`/etc`.

## Step 2 — `setup-dns.sh`: `setup_linux()` capture-then-write, reordered cleanup

In `setup_linux()`:

1. **Capture** (before anything else in the function), condition stated
   plainly, exactly per design — this *is* the stale-refresh mechanism,
   not an optional nicety. The existence check is deliberately **not**
   a bare `[[ ! -f ]]` — an existing-but-empty-or-truncated state file
   (partial write from a killed process, manual tampering, etc.) must be
   treated the same as absent, otherwise the write step below silently
   emits `nameserver 127.0.0.1` with zero fallback lines — reinstating
   the exact SPOF this ticket exists to fix, with no path to recovery
   short of a manual `rm` or full `--uninstall`:
   ```
   if [[ ! -s "$RESOLV_UPSTREAMS" ]] || ! grep -q '[^[:space:]]' "$RESOLV_UPSTREAMS"; then
       capture_dns_upstreams initial
   elif check_ip_changed; then
       capture_dns_upstreams refresh
   fi
   ```
   **Note the validity check matches `$RESOLV_UPSTREAMS`'s actual format**
   — Step 1 defines this file as bare IPs, one per line, with no
   `nameserver ` prefix (that prefix is added later, only when
   `write_resolv_conf_with_fallback` composes `$RESOLV_CONF`). A guard
   checking for a `nameserver ` prefix here would never match — the
   `initial` branch would always fire, on every single re-run, making the
   `elif check_ip_changed; then capture_dns_upstreams refresh` branch
   permanently unreachable and defeating the stale-upstream-refresh
   mechanism entirely (an earlier draft of this plan had exactly this
   mismatch; caught in code review, not by this doc).
   (If neither branch fires — file exists, non-empty, has at least one
   non-blank line, and IP unchanged — the existing state file is left
   as-is and read by the write step below.)
2. Existing `RESOLV_BACKUP` logic — unchanged in behavior; its
   `/etc/resolv.conf` literals become `$RESOLV_CONF` per Step 1, same as
   everywhere else in this file.
3. **Cleanup, moved earlier**: the systemd-resolved drop-in removal +
   `systemctl restart systemd-resolved` block (current lines 64-68) and
   the NetworkManager drop-in removal + `systemctl restart
   NetworkManager` block (current lines 72-75) both move to *before* the
   `/etc/resolv.conf` write (current lines 53-59). The other NM cleanup
   line (current lines 69-71, removing
   `/etc/NetworkManager/dnsmasq.d/voipbin-sandbox.conf`, no restart
   attached) is not part of the race this reorder fixes and can stay
   wherever's convenient — note this in the diff/PR description so a
   reviewer doesn't wonder why only part of the NM cleanup moved.
4. **Write**: replace the current `rm -f /etc/resolv.conf; cat > ...`
   heredoc with a call to `write_resolv_conf_with_fallback` (no
   arguments — see Step 1, content doesn't depend on `host_ip`).
5. **Disclosure warning**: after the write, if `systemctl is-active
   systemd-resolved &>/dev/null`, `log_warn` the resolv.conf-now-unmanaged
   message from design §2.

**Test**: `tests/setup-dns.bats` already exists — extend it. Cases: fresh
install (no prior state file) produces non-empty fallback lines via
`capture_dns_upstreams initial`; a second `setup_linux()` run with an
unchanged IP and an existing state file does **not** call
`capture_dns_upstreams` again and still produces the same non-empty
fallback lines (the idempotent-re-run case design §1 explicitly requires,
proving the write step correctly reads the existing file rather than
needing a fresh capture); a re-run with `check_ip_changed` stubbed true
calls `capture_dns_upstreams refresh` and the fallback lines change to
match; an existing-but-empty (or `nameserver`-line-less) state file is
treated as absent and triggers `initial` capture, not silently written
through as zero fallback lines; cleanup blocks run before the write is
observable (assert via a call-order-recording stub, not just call count);
the write step unlinks `$RESOLV_CONF` before writing even when it's a
symlink (stub `$RESOLV_CONF` as a symlink in the bats case, assert the
post-write file is a regular file, not a write-through); disclosure
warning fires iff the `systemctl is-active systemd-resolved` stub returns
0.

## Step 3 — `setup-dns.sh`: `uninstall_linux()` removes the state file

- Add `rm -f "$RESOLV_UPSTREAMS"` alongside the existing `rm -f
  "$RESOLV_BACKUP"` in `uninstall_linux()`.
- Replace the hardcoded `/etc/resolv.conf` literals in this same function
  (both the symlink/file-restore branch and the no-backup-found default
  branch at lines 102-107, per Step 1's note) with `$RESOLV_CONF`.

**Test**: extend the existing uninstall bats coverage to assert
`$RESOLV_UPSTREAMS` is gone after `--uninstall`, alongside the existing
backup-restore assertion; add a case for the no-backup-found default
branch writing to `$RESOLV_CONF` (not the real `/etc/resolv.conf`) so
this path is unprivileged-testable too — it wasn't necessarily exercised
via `$RESOLV_CONF` before this change.

## Step 4 — `setup-dns.sh`: `regenerate_corefile()` refresh

Placement is specific, not "somewhere in the outer block" — there are two
nested conditions here and they mean different things:

- **Outer**: `if check_ip_changed || force_update` — true on *every*
  `--regenerate` call, since `--regenerate` always passes
  `force_update=true` regardless of whether the IP actually changed
  (e.g. an operator re-running it to fix an unrelated cert problem).
- **Inner**: `if [[ -n "$configured_ip" && "$configured_ip" !=
  "$host_ip" ]]` — true only when the host IP *actually* changed vs. the
  previously configured one. This is the real "network changed" signal.

Add the upstream refresh (`capture_dns_upstreams refresh` then
`write_resolv_conf_with_fallback`) **inside the inner block**, alongside
the existing `.env` IP update — not the outer one. Refreshing on every
manual `--regenerate` (outer) would, on a non-systemd-resolved host,
silently downgrade a good previously-captured LAN upstream to the
source-3 hardcoded `8.8.8.8`/`8.8.4.4` every time someone runs
`--regenerate` for an unrelated reason, since `refresh` mode has no
source 2 to fall back on. Scoping to the inner block means the upstream
chain only changes when there's an actual reason to believe the old one
might be stale. (This function already runs under `check_root` via
`main()`'s `--regenerate` branch and is already internal-mode gated by
the earlier external-mode early-exit at the top of `main()`.) Always
`refresh` mode here, never `initial` — this code path only runs when
`$RESOLV_UPSTREAMS` is expected to already exist (install already
happened via `setup_linux()`); if it somehow doesn't yet, `refresh`
mode's behavior (systemd-resolved live file → Corefile targets, skipping
source 2) is still the correct fallback since `/etc/resolv.conf` at this
point is not guaranteed to be pre-sandbox state either.

**Test**: bats case simulating an IP change through `--regenerate`,
asserting `$RESOLV_UPSTREAMS` content changes to reflect a different
stubbed upstream.

## Step 5 — `setup-voip-network.sh`: refresh in the IP-changed block

**This step requires a small refactor, not just an insertion.** The
existing `if check_ip_changed; then ... fi` block (current lines 138-161)
is top-level inline code, executed unconditionally when the script runs,
not a callable function. `tests/test_helper.bash:465`'s
`load_network_functions()` extracts this script only up to the
`parse_args "$@"` call line (the marker it sed's on) — the IP-changed
block sits *after* that marker and is never sourced into the test
environment. None of this file's existing bats cases touch it; there is
nothing to "extend the existing stubbing pattern" onto, because the
target code isn't reachable from a test today.

Refactor: extract the block's body into a function —
`handle_ip_change()` — defined among the script's other function
definitions (i.e., *above* the `parse_args "$@"` line, alongside
`detect_physical_interface`, `load_external_ips`, etc., so it's included
in the marker-based extraction), setting the same `IP_CHANGED` global the
inline block currently sets (read later at lines 268-273 for the
api-manager cert-restart check — preserve that behavior exactly, plain
global assignment, no `local`/subshell). **`IP_CHANGED=false` moves inside
the function too**, as its first line — not left behind at the original
call site — so that a bats case exercising `handle_ip_change()` directly
(with `check_ip_changed` stubbed false) observes the correct `IP_CHANGED`
value without depending on init code outside the function. Replace the original inline
block at its current call site (after `parse_args "$@"`, before "Load
external IPs from .env") with a single `handle_ip_change` call — runtime
behavior and execution order are unchanged, only the code is now also
independently callable. Add the new `capture_dns_upstreams refresh` +
`write_resolv_conf_with_fallback` calls inside `handle_ip_change()`,
alongside the existing CoreDNS Corefile regeneration (same internal-mode
`if` as today).

**Test**: new bats cases in `tests/setup-voip-network.bats`, using
`load_network_functions` (now able to reach `handle_ip_change` since it's
defined before the extraction marker) plus stubbed
`capture_dns_upstreams`/`write_resolv_conf_with_fallback` mock functions
that record invocation. Assert both are called exactly once, in that
order, when `check_ip_changed` is stubbed true and domain mode is
internal; assert neither is called when domain mode is external (matching
this file's existing internal-mode-gate coverage for the Corefile
regen); assert neither is called when `check_ip_changed` is stubbed
false; assert `IP_CHANGED` is still set correctly in all three cases
(regression coverage for the refactor itself, independent of the new DNS
calls).

## Step 6 — `check-install.sh`: detail string + external-mode leftover field

In `check_resolv_conf()`:

- Internal-mode branch: after the existing hijack-detected `pass`, append
  a fallback-count detail (count `nameserver` lines in `$RESOLV_UPSTREAMS`
  if it exists, else 0) to the existing `pass` message — still a `pass`,
  richer detail string only.
- External-mode branch: add a third local `upstreams_exists` (checks
  `$RESOLV_UPSTREAMS`), and **change the existing `if` condition itself**
  (`check-install.sh:274`, currently `if [[ "$hijacked" == "true" ||
  "$backup_exists" == "true" ]]`) to `if [[ "$hijacked" == "true" ||
  "$backup_exists" == "true" || "$upstreams_exists" == "true" ]]`. Adding
  the local without changing this condition would leave a
  leftover-upstreams-only host silently reporting `pass "resolv.conf
  untouched"` — the whole point of this change is to make that case a
  `fail`. Report `upstreams_exists` as its own field in the `fail` detail
  string alongside the existing `hijacked=`/`backup=` fields (not folded
  into `backup_exists`). **Do not re-declare `RESOLV_UPSTREAMS`
  here** — `check-install.sh` sources `common.sh` at line 27, *after*
  its own `RESOLV_CONF`/`RESOLV_BACKUP` defaults at lines 23-24, so the
  single `RESOLV_UPSTREAMS="${RESOLV_UPSTREAMS:-...}"` definition from
  Step 1's `common.sh` change is already in scope by the time
  `check_resolv_conf()` runs (well after sourcing completes) and is
  already override-safe via its own `:-` pattern regardless of source
  order. A second declaration here would just be redundant, not
  incorrect, but adds a second place to keep in sync for no benefit —
  one definition, in `common.sh`, is enough.

**Test**: extend `tests/check-install.bats` — internal-mode `pass` detail
reflects 0 vs N fallback lines; external-mode `fail` detail includes all
three fields independently when only the upstreams file is leftover
(backup absent, upstreams present).

## Step 7 — `docker-compose.yml`

No change (design §"Non-goals" — `restart: always` already correct).
Confirm no diff needed here as part of PR review, don't silently skip.

## Step 8 — Docker DNS-inheritance verification (manual, not code)

After steps 1-6 are implemented and merged locally in the worktree, run
`sudo ./scripts/setup-host.sh` on a real (or CI-equivalent) host, then
`docker compose up -d`, and confirm the stack starts as it does today.
Spot-check a container's `/etc/resolv.conf` to confirm it now shows the
captured upstream instead of the previous default `8.8.8.8`. This is a
one-time manual verification per design §1's Docker-inheritance item —
no automated test is proposed for it (would require a running Docker
daemon in CI, out of proportion to the risk).

## Step 9 — README.md / CLAUDE.md documentation touch-ups

- `README.md` DNS troubleshooting section and `CLAUDE.md`'s "DNS
  Configuration" section both currently document `/etc/resolv.conf` as
  containing exactly `nameserver 127.0.0.1`. Update both to show the
  fallback-chain example from design §1, and mention
  `/etc/resolv.conf.voipbin-upstreams` alongside the existing
  `/etc/resolv.conf.voipbin-backup` row in the "Key Files" table.
- Add one line to `CLAUDE.md`'s DNS section disclosing the residual
  systemd-resolved conflict (design §2's explicit scope-down), so it's
  discoverable without reading this plan doc.

## Sequencing / dependency notes

- Step 1 must land before 2, 4, 5 (they call its helpers).
- Steps 2-6 are otherwise independent of each other and can be implemented
  in any order, but land in one PR (design's explicit scope: "keep this
  PR reviewable," not split further).
- Step 8 runs after 1-6 are code-complete, before the PR is opened, as
  local verification evidence (per CLAUDE.md's "prove it works").
- Step 9 lands in the same PR (docs describing behavior the PR changes
  belong with the change, not a follow-up).

## Out of scope (unchanged from design doc)

- `setup-voip-network.sh`'s secondary-IP-on-physical-interface risk —
  separate ticket.
- Full systemd-resolved scoped-resolver redesign — separate ticket if
  needed later.
- VOIP-1280 (install doctor) — separate, in progress.
