# VOIP-1285: Sandbox internal-mode DNS SPOF/conflict fix — Design

## Problem

Internal-mode install (`sudo ./scripts/setup-host.sh` → `setup-dns.sh` →
`setup_linux()`) deletes `/etc/resolv.conf` and replaces it with exactly one
line:

```
nameserver 127.0.0.1
```

This makes **all** host DNS resolution (not just `*.voipbin.test`) depend on
a single Docker container (`voipbin-dns`, CoreDNS). Two failure modes:

1. **SPOF**: whenever CoreDNS is not answering on 127.0.0.1:53, the host has
   zero working DNS resolution, not just for voipbin domains. `docker-
   compose.yml:171` already sets `restart: always` on the `coredns` service,
   so a plain container crash or a `dockerd` restart self-heals. The
   symptom still reproduces via: `docker compose down` (explicit stop,
   `restart: always` does not undo an explicit stop), Docker daemon itself
   not running/not enabled at boot, `COMPOSE_PROFILES` not including
   `internal-dns` (stale `.env`, see `common.sh` profile-conflict guard),
   or simply the window between reboot and the operator re-running
   `start.sh`. Operator-visible symptom: "internet is down."
2. **Fights systemd-resolved**: on distros where `/etc/resolv.conf` is
   normally a symlink to `/run/systemd/resolve/stub-resolv.conf`, deleting
   that symlink and writing a plain file bypasses systemd-resolved instead
   of configuring it. `setup_linux()` (lines 64-68, 72-75) also calls
   `systemctl restart systemd-resolved` / `systemctl restart
   NetworkManager` when cleaning up old drop-ins — either restart can
   rewrite `/etc/resolv.conf` back to a stub symlink, racing the file this
   same function just wrote. This is one plausible, narrow trigger for the
   "nameserver keeps changing" symptom; the broader conflict (systemd-
   resolved no longer owns `/etc/resolv.conf` after this script runs, so
   *any* later restart of it — unrelated to this script — can also revert
   the file) is the more likely general cause and is not eliminated by
   this ticket (see §2).

## Non-goals

- Rearchitecting DNS to a fully scoped, per-domain resolver split
  (`resolvectl domain ~voipbin.test`) is **out of scope for this ticket**.
  It's a larger blast-radius change: `check-install.sh`'s `check_resolv_conf`
  (`RESOLV_CONF` must show `nameserver 127.0.0.1` in internal mode),
  `README.md`'s troubleshooting section, and the existing DNS bats tests all
  encode "resolv.conf points at CoreDNS" as the internal-mode contract. This
  ticket keeps that contract and makes it resilient instead of replacing it,
  per the "smallest change that works" principle — a resolver-split redesign
  can be a follow-up if this turns out to be insufficient.
- macOS is unaffected (`/etc/resolver/voipbin.test` is additive, not a
  wholesale resolver replacement) — no changes proposed there.
- `coredns` compose `restart` policy: already `restart: always`
  (`docker-compose.yml:171`). No change — an earlier draft of this doc
  incorrectly claimed no policy was set and proposed downgrading it; that
  was wrong and has been removed.

## Fix

### 1. Fallback nameserver chain (fixes symptom 1 for the cases restart:always doesn't cover)

`setup_linux()` currently discards the pre-existing resolver entirely. Change
it to write a resilient chain into the generated `/etc/resolv.conf`:

```
# VoIPBin Sandbox - DNS via CoreDNS
# CoreDNS handles *.voipbin.test locally and forwards others upstream.
# Fallback nameservers below are used if CoreDNS is unreachable.
# To restore: sudo ./scripts/setup-dns.sh --uninstall
nameserver 127.0.0.1
nameserver <upstream-1>
nameserver <upstream-2>
options timeout:1 attempts:2
```

glibc's resolver (`MAXNS=3`) tries nameservers in listed order per query,
and a closed UDP port on 127.0.0.1 gets an immediate ICMP port-unreachable
(not a multi-second hang), so a downed (not-running) CoreDNS container
degrades to "voipbin.test domains stop resolving, everything else keeps
working" instead of "all DNS resolution stops."

That ICMP-unreachable behavior only covers "container not running." A
CoreDNS that's up but hung (not refusing connections, just not answering)
instead falls back on glibc's default `timeout:5 attempts:2` per
nameserver, i.e. up to ~20s of stall before the resolver gives up on it
and moves on — a milder, slower version of symptom 1. `options
timeout:1 attempts:2` bounds that to ~1s per attempt (worst case ~6s
across all 3 nameservers), while keeping `attempts:2`'s retry so one
dropped UDP packet against a legitimately slow-but-working upstream
(VPN/remote DNS) doesn't hard-fail on the first try. **Caveat, stated
explicitly**: this `options` line applies to *all* host DNS resolution via
`/etc/resolv.conf`, not just voipbin.test lookups — a deliberate,
disclosed trade-off (bounded worst-case latency for every query) rather
than a targeted fix, since resolv.conf has no mechanism to scope `options`
per-nameserver.

Concretely:

- **`MAXNS` cap**: total nameserver lines written must not exceed 3
  (127.0.0.1 + at most 2 upstreams). Truncate, don't silently rely on glibc
  to ignore the rest — make the cap explicit in code and in a bats test.
- **Upstream source, in priority order**, captured into a new **durable
  state file** `/etc/resolv.conf.voipbin-upstreams` (sibling to
  `RESOLV_BACKUP`, removed by `uninstall_linux` alongside it):
  1. If `/run/systemd/resolve/resolv.conf` exists (present whenever
     systemd-resolved is active, even though `/etc/resolv.conf` itself
     only shows the `127.0.0.53` stub), read the real upstream
     `nameserver` lines from there. This is the common case on the
     project's primary target (Ubuntu).
  2. Else, if the current `/etc/resolv.conf` is a plain file (no
     systemd-resolved), read its `nameserver` lines directly.
  3. Else (nothing usable found), fall back to the same targets CoreDNS's
     own Corefile forwards to (`8.8.8.8`/`8.8.4.4` — see Corefile
     generation in `common.sh`). Same trust boundary already in place, no
     new upstream introduced.

  Whichever of 1/2 supplied lines (3 needs no filtering — it's already a
  trusted, hardcoded list), apply the same filter uniformly before use:
  exclude any loopback (`127.0.0.0/8`), link-local (`169.254.0.0/16`,
  `fe80::/10`), or IPv6 loopback (`::1`) entries — self-referential or
  link-local stub addresses, meaningless to a client resolver once CoreDNS
  is gone — and dedupe before applying the `MAXNS` cap below. Source 1's
  file is not normally expected to contain such entries, but filtering it
  the same way source 2 is filtered, instead of trusting it unfiltered,
  removes a class of edge case for free.
- **Testability**: `check-install.sh:22-24` already makes `RESOLV_CONF`/
  `RESOLV_BACKUP` overridable via env var for unprivileged bats runs
  (`RESOLV_CONF="${RESOLV_CONF:-/etc/resolv.conf}"`). Give the new state
  file the same treatment (`RESOLV_UPSTREAMS="${RESOLV_UPSTREAMS:-/etc/
  resolv.conf.voipbin-upstreams}"` in both `setup-dns.sh` and
  `check-install.sh`) so the bats coverage in the Testing section can run
  without real root/`/etc` access.
- **Why a separate state file, not `RESOLV_BACKUP`**: `RESOLV_BACKUP`
  stores "how to restore on `--uninstall`" — for the symlink case that's
  just `<target>\nsymlink`, which has no `nameserver` lines to re-read on
  a later run. Upstream capture is a different concern (what to fall back
  to *while installed*) and needs its own durable, independently-gated
  file: `/etc/resolv.conf.voipbin-upstreams` is written once, iff it does
  not already exist, using the same "only if absent" guard `RESOLV_BACKUP`
  uses today (`[[ ! -f ... ]]`) — but as its own file/guard, not a reuse
  of `RESOLV_BACKUP`'s. This makes re-runs of `setup_linux()` itself (the
  plain `sudo ./scripts/setup-host.sh` re-run path, documented as
  idempotent) read upstreams back from this state file instead of
  re-deriving them from (by then) our own `127.0.0.1`-first
  `/etc/resolv.conf` — closing the gap where a second run would otherwise
  capture nothing. Note `--regenerate` does **not** go through
  `setup_linux()` (it calls `regenerate_corefile` + `test_dns` directly,
  see `setup-dns.sh:379-386`) — see the stale-upstream refresh item below
  for how that path stays current instead.
- **Stale upstream refresh**: the state file is captured once and is not
  self-refreshing. `check_ip_changed()` (`common.sh:224-238`, a pure
  predicate comparing the current host IP against `.env`'s
  `HOST_EXTERNAL_IP`) is checked at three call sites, but they are **not
  equally suitable places to write host files**:
  - `start.sh:716` is documented as the *unprivileged* step of the
    4-command install flow. It must **not** attempt to refresh
    `/etc/resolv.conf*` — it doesn't run as root, and shouldn't need to.
  - `setup-voip-network.sh:140` (root-checked at line 133, internal-mode
    gated at line 152) and `setup-dns.sh:289` inside `regenerate_corefile()`
    (root via `check_root` at line 380, external-mode gated at lines
    352-357) both already run as root and internal-mode-gated — but
    `regenerate_corefile()` itself is called from exactly **one** place,
    the `--regenerate` flag (`setup-dns.sh:383`), not from plain runs.
    Plain runs (`setup-host.sh`, `init.sh`, `start.sh` — all invoking
    `setup-dns.sh -y`, i.e. `main()` → `setup_linux()`) never call
    `regenerate_corefile()`, so refresh must **also** live in
    `setup_linux()` itself (already root-checked via `check_root` in
    `main()`, already internal-mode-gated by the external-mode early-exit
    before `main()`'s body runs). Concretely: **all three** —
    `setup_linux()`, `setup-voip-network.sh`'s IP-changed block, and
    `regenerate_corefile()` — perform the same refresh check (`if
    check_ip_changed || force_update; then` re-run capture and
    **overwrite** `/etc/resolv.conf.voipbin-upstreams`, else read the
    existing state file), not just the latter two. Without `setup_linux()`
    also refreshing, the documented remediation "run `sudo
    ./scripts/setup-host.sh` again" after a network change would rewrite
    `/etc/resolv.conf` with stale fallback entries, with correctness then
    silently depending on `setup-voip-network.sh` happening to run
    afterward in the same `setup-host.sh` invocation and overwriting them
    — too fragile to leave implicit.
  - **Refresh must use only capture source 1 or 3, never source 2.**
    Source 2 (§ above, "read the current `/etc/resolv.conf`'s own
    `nameserver` lines") is only meaningful *before* this script has ever
    run — at refresh time `/etc/resolv.conf` is already our own
    generated file, so re-reading it after excluding `127.0.0.1` just
    recovers the previous (possibly now-stale) fallback entries, never a
    new upstream. On a non-systemd-resolved host with no other capture
    source, refresh has no way to learn a new upstream automatically and
    must fall through to source 3 (Corefile forward targets) rather than
    silently keeping stale addresses.
  Without this refresh path, a host that changes networks (new
  gateway/upstream DNS) keeps a permanently stale, now-unreachable
  fallback chain — same failure class as symptom 1, just delayed instead
  of immediate.
- **Sequencing**: capture (write the state file, step 1/2/3 above) must
  happen strictly before *any* of the cleanup/restart blocks in §2 — both
  the systemd-resolved block (`setup-dns.sh:64-68`) and the NetworkManager
  block (`setup-dns.sh:72-75`) — which must happen strictly before this
  function writes the new `/etc/resolv.conf`. Three ordered steps, not
  two: capture → cleanup (both blocks) → write. Reversing capture and
  cleanup risks reading a momentarily stale/absent
  `/run/systemd/resolve/resolv.conf` if systemd-resolved is mid-restart.
- **Docker's own DNS inheritance**: today, with `/etc/resolv.conf`
  containing only `nameserver 127.0.0.1`, the Docker daemon detects a
  loopback-only host resolver (an address it cannot bind-mount into a
  container's netns unchanged) and falls back to `8.8.8.8` for every
  container's embedded DNS resolver — including `coredns` itself, though
  that container ignores its own resolv.conf and forwards purely per
  Corefile config. Adding real non-loopback upstream addresses to the
  host's `/etc/resolv.conf` changes this: Docker will now copy those
  upstreams into every container's resolv.conf instead of defaulting to
  `8.8.8.8`. This is a behavior change worth a deliberate check, not an
  oversight — add a bats/manual check that `docker compose up` and image
  builds still succeed unchanged after this fix (container-internal DNS
  resolution for apt/npm/go-module fetches during builds, etc.), since
  those now resolve via whatever upstream was captured rather than a
  fixed `8.8.8.8`.

### 2. Work with systemd-resolved instead of racing it

`setup_linux()`'s cleanup blocks (lines 64-68 and 72-75) call `systemctl
restart systemd-resolved` / `systemctl restart NetworkManager` after
removing stale legacy config (`/etc/systemd/resolved.conf.d/voipbin-
sandbox.conf`, `/etc/NetworkManager/conf.d/dns-dnsmasq.conf`), both
sequenced *after* this function already wrote our static
`/etc/resolv.conf`. That specific ordering is a real, narrow bug (a
leftover legacy drop-in triggers a
resolved restart right after we've written our file, and a
resolved restart is not guaranteed to leave an externally-managed
`/etc/resolv.conf` alone) and its fix is a plain reorder: cleanup/restart
moves *before* the write (see the three-step sequencing in §1), not after.
This is a **defensive fix for one specific, narrow trigger** — it is not
established, and this doc does not claim, that this is *the* mechanism
behind every "nameserver keeps changing" report. It only applies when the
legacy drop-in exists, which is not the common case.

The broader, unresolved conflict: systemd-resolved keeps running and is
not the thing managing `/etc/resolv.conf` after this script runs (we
deleted the symlink it expects to own). *Any* systemd-resolved restart,
triggered by anything else on the host — a netplan/NetworkManager
reconnect, a manual `systemctl restart systemd-resolved`, a suspend/resume
— can independently recreate the stub symlink and silently undo our file,
with no involvement from this script at all. This ticket does **not**
eliminate that conflict; §2 only removes one specific self-inflicted
trigger. Log an explicit, disclosed warning at install time when
`systemctl is-active systemd-resolved` is true: resolv.conf is now
unmanaged by systemd-resolved until `--uninstall` runs, and any future
systemd-resolved restart (host-triggered, not just ours) may silently
revert it. Actually preventing that (masking the symlink, disabling
`resolvconf.conf` management) is host-configuration surgery beyond this
script's blast radius and is deferred to the scoped-resolver follow-up in
Non-goals. **VOIP-1285 closes with symptom 2 diagnosed and its known
self-inflicted trigger fixed, not the underlying conflict eliminated** —
this is a decision the repo owner should make knowingly before merge, not
discover afterward.

### 3. `check-install.sh` awareness

`check_result` only supports `pass|fail|skip` (no `warn` — see
`check-install.sh:37-52`, and CLAUDE.md's `CHECK <name>: pass|fail|skip`
contract). Extending that contract is out of scope here. Instead:
`check_resolv_conf` (internal mode) stays `pass`/`fail` exactly as today
(hijack detected via the `nameserver 127.0.0.1` line — unchanged), and its
`pass` detail string additionally reports whether fallback lines are
present, e.g. `pass "... points at CoreDNS (127.0.0.1), fallback: 2
upstream(s)"` vs `pass "... points at CoreDNS (127.0.0.1), no fallback
configured"`. Both remain a `pass` (fallback presence is not part of the
install-correctness contract, just visibility) — no bats test changes
needed beyond asserting the detail string shape.

`check_resolv_conf`'s external-mode branch (`check-install.sh:274-278`)
already fails on leftover internal-mode state, checking `hijacked` and
`$RESOLV_BACKUP`. The new `/etc/resolv.conf.voipbin-upstreams` state file
is the same class of leftover state (written only by internal-mode
`setup_linux()`, meaningless and stale in external mode) and must be added
to that same leftover check — as its **own** third local variable (e.g.
`upstreams_exists`), reported as its own field in the failure detail
string, not folded into `backup_exists`. Two independently-toggleable
leftover files collapsed into one flag would make a future "backup was
cleaned up but the upstreams file wasn't" case indistinguishable from
"both are clean" in the check output — keep them separately diagnosable.
This keeps an internal→external mode switch (via `clean.sh --volumes
--purge` + reinit, per the mode-switch caveat in CLAUDE.md) fully detected
as clean by `check-install.sh`.

### 4. Divergent DNS test behavior (documentation only, no code change)

`setup-dns.sh`'s own `test_dns` function resolves against the *system*
resolver, so once fallbacks exist, killing CoreDNS makes voipbin.test
lookups return NXDOMAIN from the fallback upstream (a clear failure) rather
than hanging — `test_dns`'s existing pass/fail logic already handles a
non-matching result correctly, no change needed. `check-install.sh`'s
`check_dns` already pins `@127.0.0.1` explicitly in internal mode and is
unaffected either way. Documented here so the difference isn't rediscovered
as a bug later.

## Explicitly out of scope for this ticket

- `setup-voip-network.sh`'s `ip addr add` on the host's physical interface
  (secondary IP for Kamailio/RTPEngine) — flagged in VOIP-1285's
  description as a related risk, but it's a distinct failure mode (NM
  reverting a manually-added secondary IP) with a different fix shape
  (NetworkManager-aware IP assignment). Tracking as a fast-follow, not
  bundling into this DNS fix to keep this PR reviewable.
- Fully eliminating the systemd-resolved conflict (see §2) — deferred to a
  scoped-resolver follow-up.
- VOIP-1280 (install doctor) — separate, in progress.

## Testing

- bats coverage for `setup-dns.sh`: upstream capture from
  `/run/systemd/resolve/resolv.conf` when present; exclusion of
  loopback/link-local/IPv6-loopback addresses when reading a plain
  resolv.conf; dedupe before the `MAXNS` cap; truncation to 3 total lines;
  fallback to Corefile forward targets when no usable upstream is found;
  state file (`/etc/resolv.conf.voipbin-upstreams`) written exactly once
  and re-read (not re-derived) on a second `setup_linux()` run, proving
  idempotent re-runs still produce non-empty fallbacks; `check_ip_changed()`
  firing overwrites the state file and regenerates the fallback lines
  (stale-upstream refresh); uninstall removes the state file alongside
  `RESOLV_BACKUP` and still restores symlink/file byte-for-byte as today
  (unchanged path, regression-only test); the install-time systemd-resolved
  disclosure warning is logged exactly when `systemctl is-active
  systemd-resolved` is true (stub the command result in bats, don't require
  a real systemd-resolved instance in CI).
- `check-install.sh` bats coverage: `check_resolv_conf` still `pass`/`fail`
  identically to today on the hijack detection (no new "warn" state);
  detail string reflects fallback line count, and a bats case asserts a
  zero-fallback regression is visible in that string (not silently a
  plain `pass` indistinguishable from the healthy case).
- Docker inheritance check: after install, `docker compose up -d` and at
  least one image build/pull path complete unchanged; spot-check a
  container's `/etc/resolv.conf` reflects the captured upstream instead of
  the previous hardcoded `8.8.8.8` default, and that this doesn't break
  anything (informational, not a hard pass/fail gate — flag if it does).
- Manual verification: stop `voipbin-dns` container after install (`docker
  compose stop coredns` — bypassing `restart: always` deliberately, since
  `stop` is an explicit action, not a crash), confirm `dig google.com`
  still resolves via fallback while `dig api.voipbin.test` returns
  NXDOMAIN cleanly (not a hang).
