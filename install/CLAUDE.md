# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the **VoIPBin sandbox** - a Docker Compose development environment for running the complete VoIPBin CPaaS (Communications Platform as a Service) stack locally. It orchestrates 25+ microservices along with supporting infrastructure.

## Quick Start

```bash
# One command to start everything
./scripts/start.sh
```

The `start.sh` script handles everything:
- Environment initialization (generates .env and certificates if missing)
- Database startup and schema migration
- Starting all 25+ services
- VoIP network interface setup (prompts for sudo password)
- Creating test account and extensions (opt-in, see [Test Data Setup](#test-data-setup))

### Step-by-Step Alternative

```bash
./scripts/init.sh              # 1. Generate .env, certificates, docker-compose.yml
nano .env                      # 2. Add your API keys (GCP, OpenAI, etc.)
./scripts/start.sh             # 3. Start all services (test data seeding is opt-in)
```

### docker-compose.yml.dist vs docker-compose.yml

`docker-compose.yml` is copied once from the committed `docker-compose.yml.dist`
on first `init.sh` run and is then untracked (`install/.gitignore`) — a later
`git pull` updates `docker-compose.yml.dist` but never rewrites the live
`docker-compose.yml`, so a repo update never silently changes a running
install's compose config. `init.sh` never overwrites an existing
`docker-compose.yml`, not even on `--force-reinit`; `clean.sh --purge`
removes it so the next `init.sh` re-copies a fresh one from `.dist`. To
adopt upstream changes (new services, image digest bumps), diff the two
files and merge deliberately, or run `scripts/sync-compose-images.sh` with
`COMPOSE_FILE=docker-compose.yml` to pull in just the image digest updates
from `versions.lock.dist`. See `docker-compose.yml.dist`'s own header comment
for the full rationale.

**Upgrading an install that predates this split (IMPORTANT, one-time):**
before this change `docker-compose.yml` was git-tracked. The first
`git pull` that introduces `docker-compose.yml.dist` deletes the tracked
`docker-compose.yml` from the working tree as an ordinary rename — no
warning, no conflict — and `docker compose` then fails ("no configuration
file provided") until it's recovered. Re-running `init.sh` is NOT the fix:
it copies whatever `docker-compose.yml.dist` is at the CURRENT commit,
which may differ from what was actually running. Recover the real
pre-pull file instead (run these from this `install/` directory):
`git log --all --oneline --diff-filter=D -- docker-compose.yml` finds the
commit that DELETED it (the split commit) - the file lived in that
commit's PARENT, so use `git show <commit>^:./docker-compose.yml >
docker-compose.yml`. Both the `^` and the leading `./` matter: `git show
<commit>:docker-compose.yml` without the `^` fails with "exists on disk,
but not in '<commit>'" (already gone there), and without the `./`, `git
show <rev>:<path>` resolves `<path>` from the repo ROOT rather than the
cwd (unlike `git log`/`git diff`'s pathspecs), so it fails from inside
`install/` with "exists, but not '<bare-name>'".
`init.sh` also detects this itself (existing `.env` + missing `docker-compose.yml`)
and prints this same guidance instead of silently copying `.dist`, but
doing it proactively is safer than relying on that reminder firing at the
right time.

### versions.lock.dist vs versions.lock

Same split, same rationale, applied to `versions.lock`: `versions.lock` is
copied once from the committed `versions.lock.dist` on first `init.sh` run
and is then untracked (`install/.gitignore`) — a later `git pull` updates
`versions.lock.dist` but never rewrites the live `versions.lock`. The live
file is meant to be operator-owned from here on, updated on its own
schedule and independently of any automated deploy path (deploying a new
image and updating `versions.lock.dist` are two deliberately decoupled
things — see `versions.lock.dist`'s own `"_comment"` field). `init.sh`
never overwrites an existing `versions.lock`, not even on `--force-reinit`;
`clean.sh --purge` removes it so the next `init.sh` re-copies a fresh one
from `.dist`. To adopt upstream pin changes, diff the two files and merge
deliberately, or run `scripts/generate-versions-lock.sh`/
`scripts/bump-image-digest.sh` with `LOCK_FILE=versions.lock` to update the
live copy directly.

**Upgrading an install that predates this split (IMPORTANT, one-time):**
identical migration transition to `docker-compose.yml.dist` above, and for
the identical reason - `versions.lock` was git-tracked before this change.
Recover the real pre-pull file (run these from this `install/` directory):
`git log --all --oneline --diff-filter=D -- versions.lock` finds the
commit that DELETED it, then `git show <commit>^:./versions.lock >
versions.lock` (same `^`/`./` caveats as the `docker-compose.yml` recovery
above apply here too). `init.sh` also detects this itself (existing `.env`
+ missing `versions.lock`) and prints this same guidance.

### Other Useful Commands

```bash
# Start everything
./scripts/start.sh

# Stop all services (preserves data)
./scripts/stop.sh

# Stop and remove volumes (full reset)
./scripts/stop.sh --clean

# Stop and teardown VoIP network interfaces
./scripts/stop.sh --network

# Stop and remove DNS configuration
sudo ./scripts/stop.sh --dns

# Full cleanup: volumes + network + DNS
sudo ./scripts/stop.sh --all

# View logs for a specific service
docker compose logs -f api-manager

# Start specific services only
docker compose up -d db redis rabbitmq
```

### Database Management

The database uses vanilla MySQL with schema managed by alembic migrations from the monorepo.

```bash
# Initialize database (first time or after volume reset)
docker compose up -d db
./scripts/init_database.sh

# Re-run migrations (after schema updates)
./scripts/init_database.sh

# Access MySQL directly (for debugging only)
docker exec -it voipbin-db mysql -uroot -p"$(grep MYSQL_ROOT_PASSWORD .env | cut -d= -f2)" bin_manager
```

The `init_database.sh` script's `main()`:
1. Provisions the dedicated `asterisk-registrar` realtime DB user (VOIP-1328, see below) — runs before the "already initialized?" check, so it's not skipped by answering "N" to the re-run-migrations prompt
2. Creates `bin_manager` and `asterisk` databases
3. Downloads schema from monorepo's `bin-dbscheme-manager`
4. Runs alembic migrations to create all tables

**Note:** Migrations run inside a container (`scripts/migrate.sh`, python:3.11-slim
on the compose network). No host `alembic`/`mysqlclient` installation is needed.
`scripts/migrate.sh` and `scripts/start.sh` (Step 6.5, unconditional — not
gated on whether the DB was already initialized) provision the same
dedicated DB user independently via the shared `provision_asterisk_db_user()`
in `scripts/common.sh`, so all three entrypoints keep `asterisk-registrar`'s
realtime auth working.

**`DATABASE_ASTERISK_USERNAME`/`DATABASE_ASTERISK_PASSWORD` (VOIP-1328,
password-length fix VOIP-1332):**
`asterisk-registrar`'s `res_config_mysql` realtime client previously
authenticated as `root` (`DATABASE_ASTERISK_USERNAME=root` in
`docker-compose.yml`), which was observed to intermittently fail MySQL 8
auth. `init.sh` now generates a dedicated least-privilege user (`asterisk_rt`
by default, `SELECT/INSERT/UPDATE/DELETE` on `asterisk.*` only) and writes
it to `.env`. `docker-compose.yml` falls back to `root`/`MYSQL_ROOT_PASSWORD`
when these two vars are absent, so installs created before VOIP-1328 keep
the same fallback behavior until `.env` is regenerated - which, per the
VOIP-1332 note below, is itself broken by the same 49-char truncation
(`MYSQL_ROOT_PASSWORD` is also a 64-char `generate_random_key()` value).
There is no automatic self-heal for this fallback path specifically:
`provision_asterisk_db_user()` returns early for `DATABASE_ASTERISK_USERNAME=root`,
so a pre-VOIP-1328 install stays broken until it opts in. To opt an
EXISTING install in: add `DATABASE_ASTERISK_USERNAME=asterisk_rt` and a
`DATABASE_ASTERISK_PASSWORD` under 50 characters to `.env`, then simply
re-run `voipbin> start` — Step 6.5 provisions the user unconditionally, no
need to manually invoke `init_database.sh`/`migrate.sh`.

**Root cause finally confirmed (VOIP-1332, 2026-08-12):** the actual
failure, on VOIP-1328 and on any fresh MySQL data volume since, was the
generated `DATABASE_ASTERISK_PASSWORD` itself. `generate_random_key()`
produces a 64-char hex string, but Asterisk's `res_config_mysql.c` stores
the password in a fixed `char pass[50]` buffer and copies into it with
`ast_copy_string()`, which **silently truncates** at 49 usable chars
instead of erroring. The DB then holds the full 64-char password while
Asterisk only ever sends the truncated 49-char prefix — a genuine
credential mismatch, reported by MySQL as `err 1045` (Access Denied), which
is accurate but easy to misread as an account/permission problem since
nothing about the password itself looks wrong at a glance. (An earlier
version of this note attributed the symptom to MySQL 8's self-signed TLS
certificate; that theory did not survive reproducing `res_config_mysql`'s
actual `mysql_real_connect()` call path and is wrong — the raw C API
doesn't negotiate TLS the way the `mariadb` CLI client does by default.)

**Fix:** `init.sh` now generates `DATABASE_ASTERISK_PASSWORD` with
`generate_random_key_short()` (32 hex chars, well under the 49-char limit).
`provision_asterisk_db_user()` in `common.sh` additionally truncates
whatever value it reads from `.env` to 49 chars before granting it in
MySQL, so pre-VOIP-1332 installs (still holding a 64-char value) self-heal
on the next `start.sh` run without needing to touch `.env` by hand.

## Install Modes and AI-Install Contract (VOIP-1275)

The sandbox has two install modes, selected at init time and recorded in
`.env` as `DOMAIN_MODE`:

- **internal (default):** base domain `voipbin.test`, CoreDNS + resolv.conf
  managed by the sandbox, mkcert/self-signed TLS. Byte-for-byte the classic
  behavior.
- **external:** a real operator-owned domain, operator-managed DNS records,
  bring-your-own certificate (`install-certs.sh`). The sandbox never
  touches DNS or the trust store in this mode.

**Mode detection:** read `DOMAIN_MODE` from `.env`. A missing key (or a
missing `.env`) means internal; that is the legacy rule every mode gate
follows (`get_domain_mode` in `scripts/common.sh`).

### The 4-command unprivileged flow

An AI agent installs the sandbox with exactly one sudo command:

```bash
# internal mode
./scripts/init.sh --yes
sudo ./scripts/setup-host.sh     # the single sudo command
./scripts/start.sh
./scripts/check-install.sh

# external mode (real domain + BYO certificate)
./scripts/init.sh --mode external --domain example.com --tls byo \
  --cert fullchain.pem --key privkey.pem --yes
sudo ./scripts/setup-host.sh
./scripts/start.sh
./scripts/check-install.sh
```

If anything fails at any of these steps, run `./scripts/doctor.sh` as the
optional diagnostic step: it is read-only, works at any stage, and prints
the exact recovery command for every failure (see "Install doctor" below).

`setup-host.sh` owns every host mutation: mkcert package + CA trust
(internal only, with the two-pass CAROOT handoff), Corefile + DNS setup
(internal only), the compose default docker network on fresh hosts, and
the VoIP network interfaces (both modes — internal veth pairs, VOIP-1331,
plus external macvlan for pinned hosting-provider IPs). It is idempotent;
each step probes current state and logs a skip.

### Result-line grammar

Every entry-point script ends with a machine-parseable result line, the
last line on stdout, on success and on failure (including `set -e` aborts):

| Script | Prefix | Success shape |
|--------|--------|---------------|
| `init.sh` | `VOIPBIN_INIT:` | `status=ok mode=<m> domain=<d> tls=<t> next="<command>"` |
| `setup-host.sh` | `VOIPBIN_SETUP_HOST:` | `status=ok mode=<m> steps=<name:done\|skipped,...>` |
| `start.sh` | `VOIPBIN_START:` | `status=ok services=<running>/<total>` |
| `check-install.sh` | `VOIPBIN_CHECK:` | `status=pass\|fail passed=N failed=M mode=<m>` |
| `install-certs.sh` | `VOIPBIN_CERTS:` | `status=ok domain=<d> expires=<date>` |
| `doctor.sh` | `VOIPBIN_DOCTOR:` | `status=pass\|fail passed=N failed=M warned=K mode=<internal\|external\|unknown>` |

Failure shape for the first, second, third and fifth:
`status=error reason="..."` (plus `next="..."` where a remedy exists).
`check-install.sh` uses `status=fail` with per-check
`CHECK <name>: pass|fail|skip <detail>` lines above it.

Exit codes: `0` success; `1` validation/user error; `2` environment error.
`check-install.sh` exits `0` only when every check passes.

### Install doctor (diagnose and prescribe)

`./scripts/doctor.sh` (or `sudo ./voipbin doctor`) is the read-only
diagnostic superset of `check-install.sh`. It runs at any stage
(pre-install, mid-install, running stack), never mutates anything, and
auto-skips whatever cannot be probed at the current stage.

Output grammar: one `DOCTOR <name>: pass|fail|warn|skip <detail>` line per
check; every fail (and every warn that has a remedy) is followed
immediately by a `FIX <name>: <exact command or action>` line. The agent
contract: `grep '^FIX '` on the output yields the ordered recovery
commands, each exactly once (the summary re-lists them indented, without
the FIX prefix). The last line is the `VOIPBIN_DOCTOR:` result line from
the table above; `mode=unknown` appears before an `.env` exists and when
`DOMAIN_MODE` holds an invalid value.

Exit codes: `0` only when `failed=0` (warns and skips never fail the
run); `1` when any check failed; `2` when the doctor cannot even start.
In that last case the result line takes the early-abort shape
`VOIPBIN_DOCTOR: status=error reason="..."` instead of the pass/fail
shape, so parsers must handle all three status values.

### Caveat: mode/domain switching

The base domain is baked into database state (extension SIP realms are
`{customer_id}.registrar.<domain>`), so `init.sh` refuses a mode or domain
switch on an existing `.env`. Escape hatches: full reset
(`./scripts/clean.sh --volumes --purge`, always the combined form — this also
removes the live `docker-compose.yml`/`versions.lock`, re-copied fresh from
`docker-compose.yml.dist`/`versions.lock.dist` on the next `init.sh` run) or
`init.sh --force-reinit` (rewrites `.env`/certs/Corefile, never the database
and never `docker-compose.yml`/`versions.lock` — see "docker-compose.yml.dist
vs docker-compose.yml"/"versions.lock.dist vs versions.lock" above — and
prints the live-state follow-up: recreate extensions via the
API and recreate `registrar-manager`, `api-manager`, `hook-manager`,
`customer-manager`, `square-*`). `--force-reinit` without an explicit
`--mode` is refused when it would silently target a different mode or
domain than the existing install. An internal-to-external `--force-reinit`
first requires a clean host (stack down under the old `.env`, then
`sudo ./scripts/setup-dns.sh --uninstall`).

### External-mode DNS records (operator runbook)

Mirrored from README.md "External Mode (Real Domain)". `<d>` is the base
domain; `voipbin dns` prints this table with actual values substituted:

| Record | Type | Target | Purpose |
|---|---|---|---|
| `api.<d>` | A | Host IP | REST API + WebSocket (:8443) |
| `admin.<d>` / `meet.<d>` / `talk.<d>` | A | Host IP | Web UIs (:3003/:3004/:3005) |
| `sip.<d>` | A | Kamailio external IP | SIP signaling / WSS (:5060/:5066) |
| `sip-service.<d>`, `conference.<d>`, `trunk.<d>`, `pstn.<d>` | A | Kamailio external IP | SIP surfaces |
| `registrar.<d>` | A | Kamailio external IP | Apex registrar name, **not** covered by the wildcard below |
| `*.registrar.<d>` | A | Kamailio external IP | Per-customer SIP realm resolution |

Host IP and Kamailio IP are two distinct addresses on the same subnet;
external mode targets directly-routable hosts (single-public-IP NAT
environments are a documented limitation). The `admin`/`meet`/`talk` UIs
are plain HTTP on :3003-:3005; on a routable domain front them with a TLS
reverse proxy or restrict them to trusted networks — or use
`--web-reverse-proxy` (below) for a built-in one.

### Web reverse proxy: `--web-reverse-proxy` (VOIP-1325)

`init.sh --web-reverse-proxy` (external + `--tls byo` only) runs a Caddy
container that terminates TLS with the same BYO certificate and routes
`api`/`admin`/`meet`/`talk.<d>` by Host header to the internal service —
the standard 443 port, no `:8443`/`:3003`/`:3004`/`:3005` suffix. DNS
targets in the table above are unchanged (still the host IP); Caddy just
listens on that same host's 80/443.

- Written to `.env` as `WEB_REVERSE_PROXY=true` and
  `COMPOSE_PROFILES=web-proxy` (mirrors internal mode's `internal-dns`
  profile for CoreDNS — see `generate_caddy_config()`/`step_setup_web_proxy()`
  in `scripts/common.sh`/`scripts/setup-host.sh`).
- The certificate must cover `admin`/`meet`/`talk.<d>` in addition to the
  six names `install-certs.sh` already requires (`REQUIRED_NAMES`); a
  wildcard satisfies both automatically. `install-certs.sh --web-reverse-proxy`
  (or `WEB_REVERSE_PROXY=true` already in `.env`, e.g. on a certbot
  `--deploy-hook` renewal) enforces the extra three names.
- The Caddyfile (`config/caddy/Caddyfile`) is generated once by
  `setup-host.sh`, not regenerated by `start.sh` — it depends only on
  `BASE_DOMAIN`, which `init.sh` refuses to change on an existing install
  without `--force-reinit` (which reruns `setup-host.sh`), unlike the
  CoreDNS Corefile's dependency on `HOST_EXTERNAL_IP`.
- Inside the Caddy container the BYO certificate is read from `/certs`
  (the container's mount point for `./certs`), not the host path — see the
  comment on `generate_caddy_config()`'s `certs_dir` parameter.

## Environment Setup

**Prerequisites:**
- Docker + Docker Compose v2 (2.24.4+ recommended; the test override
  `docker-compose.test.yml` uses `!reset`/`!override` merge tags)
- `mkcert` (recommended) - for browser-trusted SSL certificates
  ```bash
  sudo apt install mkcert && mkcert -install
  ```

The `./scripts/init.sh` script auto-generates `.env` with detected values. If `mkcert` is installed, it creates browser-trusted certificates. Otherwise, it falls back to self-signed certificates (browser will show warnings).

### `AMI_USERNAME`/`AMI_PASSWORD` are fixed, not random (VOIP-1329)

Unlike every other secret `init.sh` generates, `AMI_USERNAME`/`AMI_PASSWORD`
are always `asterisk`/`asterisk` — they must match the static account baked
into `voip-asterisk-call`/`-conference`/`-registrar`'s `manager.conf` (AMI)
and `ari.conf` (ARI). Nothing in those images' `k8s_start.sh` entrypoint or
their k8s deployment manifests templates AMI credentials from an env var, so
a randomly generated value here would authenticate as a user that doesn't
exist — breaking call origination/control via the `asterisk-*-proxy`
sidecars (AMI/ARI-to-RabbitMQ bridge) while SIP registration keeps working
fine (that path is pjsip + realtime MySQL, not AMI/ARI — `asterisk-registrar-proxy`
is just as broken by a credential mismatch as the other two).

Security note: AMI (port 5038) IS bound to `127.0.0.1` inside each asterisk
container's own namespace (`network_mode: "service:asterisk-*"` for the
proxy), but the same account is also the ARI credential, and ARI (port
8088, HTTP+websocket) listens on `0.0.0.0` in these images — reachable from
any other container on the internal `10.100.0.0/16` compose network, and
from the host. That exposure is a property of the images, not of this
value; randomizing `AMI_USERNAME`/`AMI_PASSWORD` cannot mitigate it and
only re-breaks authentication. Do not change these two values unless the
images themselves change what account they ship. Fixing the actual
exposure would mean the images shipping `http.conf` with
`bindaddr=127.0.0.1` (the proxy only ever reaches it via
`ARI_ADDRESS=localhost:8088` inside the shared network namespace, so this
would have zero functional impact) — out of scope for this repo (the
images are built and digest-pinned from `monorepo-voip`); track as a
follow-up ticket there rather than treating it as accepted forever.

**Existing installs**: an install created before VOIP-1329 has
`AMI_USERNAME=voipbin` plus a random `AMI_PASSWORD` sitting in `.env` and
stays silently broken (SIP registration works, calls fail) until manually
fixed — there is no automatic migration path like VOIP-1328's Step 6.5 (no
consumer of these two vars has a `${VAR:-asterisk}` fallback). To fix an
existing install: set `AMI_USERNAME=asterisk` and `AMI_PASSWORD=asterisk`
in `.env`, then `docker compose up -d --force-recreate asterisk-call-proxy
asterisk-conference-proxy asterisk-registrar-proxy` — only the three proxy
sidecars actually consume these vars (`asterisk-call`/`-conference`/`-registrar`
themselves don't), so recreating just the proxies is both sufficient and
avoids dropping active calls. The reverse direction is unsafe, though:
never recreate `asterisk-call`/`-conference`/`-registrar` alone without
also recreating its paired proxy — see the `network_mode: "service:"`
warning on `asterisk-call-proxy` in `docker-compose.yml`.

## DNS Configuration

> **Scope: internal mode (default).** This section describes the automatic
> `.voipbin.test` DNS the sandbox manages itself. In external mode DNS is
> operator-managed; see "Install Modes and AI-Install Contract" above.

VoIPBin uses the `.voipbin.test` domain (IANA reserved TLD per RFC 2606) for SIP routing. The setup scripts automatically configure DNS forwarding.

### Architecture

**Linux (CoreDNS on port 53, with fallback — VOIP-1275, VOIP-1285):**
```
Application / SIP Client
    ↓ DNS query
/etc/resolv.conf → 127.0.0.1 (primary), captured upstream(s) (fallback)
    ↓ (CoreDNS reachable)         ↓ (CoreDNS unreachable)
CoreDNS (Docker container,    original upstream DNS
port 53)                      (e.g. router/ISP resolver)
    ↓
*.voipbin.test → host IP (from Corefile)
other queries  → 8.8.8.8 (forwarded)
```
`/etc/resolv.conf` is not a bare `nameserver 127.0.0.1` — a downed/
crashed CoreDNS container previously meant *all* host DNS resolution
failed, not just `*.voipbin.test` (VOIP-1285). It now also carries up to
two upstream nameservers captured at install time (from
`/run/systemd/resolve/resolv.conf` when systemd-resolved is active, else
the pre-existing `/etc/resolv.conf`, else a hardcoded `8.8.8.8`/`8.8.4.4`
fallback) plus `options timeout:1 attempts:2` to bound worst-case latency
if CoreDNS is up but unresponsive. **Known residual limitation**: on
systemd-resolved hosts, this script takes over `/etc/resolv.conf` from
systemd-resolved directly (not a scoped/cooperative handoff); a
systemd-resolved restart triggered by something unrelated to this script
(netplan/NetworkManager reconnect, suspend/resume) can still revert the
file. `sudo ./scripts/setup-dns.sh --uninstall` is the supported way back
to systemd-resolved-managed DNS.

**macOS (/etc/resolver):**
```
Application / SIP Client
    ↓ DNS query for *.voipbin.test
macOS resolver
    ↓ routes .voipbin.test queries (config: /etc/resolver/voipbin.test)
CoreDNS (Docker container, port 53)
    ↓ wildcard response
Returns host IP (from config/coredns/Corefile)
```

### Key Files

| File | Purpose |
|------|---------|
| `/etc/resolv.conf` | Linux: Points to 127.0.0.1 (CoreDNS), plus fallback nameservers |
| `/etc/resolv.conf.voipbin-backup` | Linux: Backup of original resolv.conf |
| `/etc/resolv.conf.voipbin-upstreams` | Linux: Captured fallback upstream nameservers (VOIP-1285) |
| `/etc/resolver/voipbin.test` | macOS: Routes .voipbin.test to CoreDNS |
| `config/coredns/Corefile` | CoreDNS config (wildcard + forwarding) |

### Troubleshooting DNS

```bash
# Check CoreDNS is running
docker ps | grep voipbin-dns

# Test CoreDNS directly
dig @127.0.0.1 voipbin.test

# Test system DNS resolution
dig voipbin.test
ping registrar.voipbin.test

# Linux: Check resolv.conf
cat /etc/resolv.conf  # Should show nameserver 127.0.0.1 first, then fallback upstream(s)

# Linux: Check backup exists
cat /etc/resolv.conf.voipbin-backup

# Linux: Check captured fallback upstreams
cat /etc/resolv.conf.voipbin-upstreams

# macOS: Check config
cat /etc/resolver/voipbin.test

# Re-run DNS setup
sudo ./scripts/setup-dns.sh
```

### Configuring SIP Devices on Your Network

SIP phones and softphones on your LAN can use the sandbox's DNS server to resolve `*.voipbin.test` domains:

1. **Find your host IP** (shown in `.env` as `HOST_EXTERNAL_IP`, e.g., `192.168.45.152`)

2. **Configure your SIP device's DNS** to point to that IP:
   - DNS Server: `192.168.45.152` (your host IP)

3. **Configure SIP registration:**
   - SIP Server: `sip.voipbin.test` or `{customer_id}.registrar.voipbin.test`
   - The device will resolve this to Kamailio's IP automatically

This works because CoreDNS listens on the host's LAN IP, not just localhost.

### Removing DNS Configuration

To completely remove the DNS configuration:
```bash
sudo ./scripts/setup-dns.sh --uninstall
# or
voipbin> clean --dns
```

This restores:
- Linux: Original `/etc/resolv.conf` from backup
- macOS: Removes `/etc/resolver/voipbin.test`

Key variables:

| Variable | Purpose |
|----------|---------|
| `DOMAIN_MODE` | Install mode: `internal` (default) or `external`. Written by init.sh; a missing key means internal (legacy rule) |
| `COMPOSE_PROFILES` | Docker compose profile set read from `.env`: `internal-dns` in internal mode (enables coredns), `web-proxy` when `--web-reverse-proxy` was used (enables Caddy, VOIP-1325), empty otherwise. Always present; a shell-exported value that contradicts `.env` fails fast |
| `TLS_MODE` | `mkcert`, `selfsigned` or `byo`. Set by init.sh (`--tls`); `byo` certificates are managed only by `install-certs.sh` and never auto-regenerated |
| `WEB_REVERSE_PROXY` | `true` when `init.sh --web-reverse-proxy` was used (external + byo only, VOIP-1325); `false` otherwise. Gates the Caddy `web-proxy` compose profile and `install-certs.sh`'s admin/meet/talk SAN requirement — see "Web reverse proxy" above |
| `BASE_DOMAIN` | The base domain all 11 derived domain values are composed from. Edit it and re-run init; do not edit the derived vars individually |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON |
| `API_SSL_CERT_BASE64` / `API_SSL_PRIVKEY_BASE64` | Base64-encoded TLS certs for API |
| `OPENAI_API_KEY` | AI/chatbot features |
| `TWILIO_SID` / `TWILIO_API_KEY` | Phone number provisioning |
| `TELNYX_API_KEY` / `TELNYX_CONNECTION_ID` | Telnyx telephony |
| `SENDGRID_API_KEY` / `MAILGUN_API_KEY` | Email delivery |
| `AWS_ACCESS_KEY` / `AWS_SECRET_KEY` | Transcription (AWS Transcribe) |
| `DOMAIN_NAME_EXTENSION` | SIP domain suffix for extensions (default: `registrar.voipbin.test`) |
| `HOST_EXTERNAL_IP` | Host's LAN IP (auto-detected); web-facing domains (api/admin/meet/talk.voipbin.test) resolve here, Docker's published host ports route to the right service |
| `KAMAILIO_EXTERNAL_IP` | Kamailio's dedicated external IP for SIP signaling (auto-generated, MUST differ from host) |
| `RTPENGINE_EXTERNAL_IP` | RTPEngine's dedicated external IP for RTP media (auto-generated) |
| `EXTERNAL_IP_PINNED` | `true` when `KAMAILIO_EXTERNAL_IP`/`RTPENGINE_EXTERNAL_IP` were set explicitly via `init.sh --kamailio-ip`/`--rtpengine-ip` (hosting-provider-registered routed IPs — see README.md "Hosting-provider routed IPs"). `false` (default) means the classic host+8-offset auto-generation applies and is safe to recompute on host-IP change |
| `BASE_HOSTNAME` | Base hostname for frontend apps (default: `voipbin.test`) |
| `API_URL` | API endpoint URL for admin/talk (default: `https://api.voipbin.test:8443/`) |
| `WEBSOCKET_URL` | WebSocket URL for admin/talk (default: `wss://api.voipbin.test:8443/v1.0/ws`) |
| `REGISTRAR_URL` | SIP registrar WebSocket URL for talk (default: `wss://sip.voipbin.test:5066`) |
| `REGISTRAR_DOMAIN` | SIP registrar domain for admin/talk (default: `registrar.voipbin.test`) |
| `CONFERENCE_URL` | Conference WebSocket URL for meet (default: `wss://conference.voipbin.test`) |
| `CONFERENCE_DOMAIN` | Conference domain for meet (default: `conference.voipbin.test`) |

## VoIP Network Configuration

The VoIP stack uses a combination of Docker networks and host network interfaces for SIP/RTP traffic.

### Why Kamailio Needs a Different IP

**Important:** Kamailio MUST use a different IP than the host machine to avoid SIP loop detection. When you test SIP calls from the host:

- If Kamailio uses the same IP as the host → Kamailio sees the source IP matches its own → Drops the request as a "loop"
- If Kamailio uses a different IP → Request is processed normally

The `init` script automatically finds an available IP in your subnet for Kamailio.

### Network Architecture

```
Docker Network:
└── default (10.100.0.0/16)         # All services
    ├── api-manager: service-name DNS (no fixed IP — removed Phase 1,
    │                 see docs/plans/2026-07-05-production-grade-horizontal-scale-design.md)
    ├── square-admin: service-name DNS (no fixed IP — removed Phase 1)
    ├── square-meet: service-name DNS (no fixed IP — removed Phase 1)
    ├── square-talk: service-name DNS (no fixed IP — removed Phase 1)
    ├── kamailio-int: 10.100.0.200
    ├── rtpengine-int: 10.100.0.201
    ├── ast-call: 10.100.0.210
    ├── ast-registrar: 10.100.0.211
    └── ast-conf: 10.100.0.212

Host Network (veth pairs, VOIP-1331 - see "Internal Interfaces" below):
├── kamailio-int (10.100.0.200)     # Kamailio internal communication
└── rtpengine-int (10.100.0.201)    # RTPEngine internal communication

External (host's physical interface):
├── HOST_EXTERNAL_IP (e.g., 192.168.45.152)        # Host's primary IP
├── KAMAILIO_EXTERNAL_IP (e.g., 192.168.45.252)    # Kamailio's dedicated IP (SIP signaling)
└── RTPENGINE_EXTERNAL_IP (e.g., 192.168.45.253)   # RTPEngine's dedicated IP (RTP media)

NOTE: api-manager/admin/meet/talk no longer have dedicated per-service
EXTERNAL_IP/iptables forwarding — they are reached directly via Docker's
published host ports (3003/3004/3005/8443) and resolve to HOST_EXTERNAL_IP
via CoreDNS, same as before this change but without the intermediate fixed
internal IP hop.
```

### DNS Resolution

| Domain | Resolves To | Purpose |
|--------|-------------|---------|
| api.voipbin.test | HOST_EXTERNAL_IP | API Manager (published host port 8443 → api-manager) |
| admin.voipbin.test | HOST_EXTERNAL_IP | Admin Console (published host port 3003 → square-admin) |
| meet.voipbin.test | HOST_EXTERNAL_IP | Meet (published host port 3004 → square-meet) |
| talk.voipbin.test | HOST_EXTERNAL_IP | Talk (published host port 3005 → square-talk) |
| sip.voipbin.test | KAMAILIO_EXTERNAL_IP | SIP proxy |

| pstn.voipbin.test | KAMAILIO_EXTERNAL_IP | PSTN gateway |
| trunk.voipbin.test | KAMAILIO_EXTERNAL_IP | SIP trunking |
| *.registrar.voipbin.test | KAMAILIO_EXTERNAL_IP | SIP registration |

**External IP Architecture:**
```
External Client → EXTERNAL_IP:port
                      ↓ iptables DNAT
                  Container IP:port

Examples:
  api.voipbin.test:443   → 10.100.0.100:443 (api-manager)
  admin.voipbin.test:80  → 10.100.0.101:80 (square-admin)
  meet.voipbin.test:80   → 10.100.0.102:80 (square-meet)
  talk.voipbin.test:80   → 10.100.0.103:80 (square-talk)
```

### Internal Interfaces

The `setup-voip-network.sh` script creates **veth pairs** (VOIP-1331) that
bridge Docker's internal network to the host, one end enslaved directly to
the compose bridge (`master` set to the bridge, same as any container's own
veth attachment) and the other kept in the host namespace with the static
IP:

- **kamailio-int** (10.100.0.200): Allows Kamailio to communicate with containerized services
- **rtpengine-int** (10.100.0.201): Allows RTPEngine to communicate with containerized services

**Not macvlan** (pre-VOIP-1331 design): these were originally macvlan
interfaces with the bridge itself as their parent. That has a kernel-level
asymmetry — new inbound TCP connections from other bridge ports (i.e. from
containers) to the macvlan child are silently dropped (frame reaches the
bridge, never arrives at the macvlan child; confirmed with tcpdump), while
macvlan-initiated traffic works fine. This silently broke `asterisk-call`'s
outbound INVITE to Kamailio: call-manager could create the ARI channel, but
the SIP packet never left the host, and the call would time out with no
error on the Asterisk side. A veth pair has no such asymmetry — from the
bridge's perspective it's indistinguishable from any other container's own
network attachment.

### CLI Commands

```bash
# Show network configuration status
sudo ./voipbin network status

# Setup VoIP network interfaces
sudo ./voipbin network setup

# Setup with specific external IP (if auto-detected one doesn't work)
sudo ./voipbin network setup --external-ip 192.168.45.160

# Teardown network interfaces
sudo ./voipbin network teardown

# Check DNS resolution
sudo ./voipbin dns status
sudo ./voipbin dns test
```

### Manual IP Configuration

If you need to change external IPs after initialization:

```bash
# Edit .env
nano .env
# Change any of:
#   KAMAILIO_EXTERNAL_IP=192.168.45.xxx
#   RTPENGINE_EXTERNAL_IP=192.168.45.xxx
#   API_EXTERNAL_IP=192.168.45.xxx
#   ADMIN_EXTERNAL_IP=192.168.45.xxx
#   MEET_EXTERNAL_IP=192.168.45.xxx
#   TALK_EXTERNAL_IP=192.168.45.xxx

# Regenerate DNS and network configuration
sudo ./voipbin dns regenerate
sudo ./voipbin network setup
sudo ./voipbin restart kamailio
```

### Dynamic IP Detection (After Reboot/Network Change)

The sandbox automatically detects when your host IP changes (e.g., after reboot, hibernate, or network switch) and regenerates all necessary configurations.

**What gets updated automatically:**
- `.env` file: `HOST_EXTERNAL_IP`, `KAMAILIO_EXTERNAL_IP`, `RTPENGINE_EXTERNAL_IP`
- CoreDNS Corefile with new IP addresses
- SSL certificates with new IP in SAN (Subject Alternative Name)
- Base64-encoded certificates in `.env` (`API_SSL_CERT_BASE64`, etc.)
- api-manager container is recreated (not just restarted) to pick up new certificates

**When automatic detection happens:**
- `./scripts/start.sh` - checks at startup
- `sudo ./voipbin dns regenerate` - forces regeneration
- `sudo ./voipbin network setup` - checks during network setup

**Manual verification:**
```bash
# Check current vs configured IP
ip route get 8.8.8.8 | grep -oP 'src \K\S+'  # Current IP
grep HOST_EXTERNAL_IP .env                   # Configured IP

# Force regeneration if needed
sudo ./scripts/setup-dns.sh --regenerate
```

**Note:** If you see `ERR_CERT_AUTHORITY_INVALID` after IP change, the certificate was regenerated correctly but your browser may need a hard refresh (Ctrl+Shift+R) or you may need to use a non-incognito window (incognito doesn't trust user-installed CAs).

## Architecture

### Infrastructure Services

| Service | Container | Ports | Purpose |
|---------|-----------|-------|---------|
| `db` | voipbin-db | 3306 | MySQL with pre-seeded schema |
| `redis` | voipbin-redis | - | Cache and session storage |
| `rabbitmq` | (no container_name — use `docker compose ps rabbitmq`) | 5672, 15672 | Message broker with delayed exchange plugin |
| `coredns` | voipbin-dns | 53 | DNS server (*.voipbin.test + forwarding) |
| `postgres` | voipbin-postgres | - (not published) | PostgreSQL + pgvector, rag-manager's vector store |
| `clickhouse` | voipbin-clickhouse | - (not published) | ClickHouse, timeline-manager's analytics backend |

`postgres` and `clickhouse` are deliberately not published to the host: each has
a single in-stack consumer, so keeping them internal shrinks the surface. Debug
them with `docker exec` instead:

```bash
docker exec -it voipbin-postgres psql -U voipbin -d rag
docker exec -it voipbin-clickhouse clickhouse-client
```

Both services run their consumer's migrations at that consumer's startup
(rag-manager and timeline-manager each embed golang-migrate), so there is no
sandbox-side migration step for either.

### VoIP Stack

| Service | Container | Ports | Purpose |
|---------|-----------|-------|---------|
| `kamailio` | voipbin-kamailio | 5060/udp+tcp | SIP proxy and routing |
| `asterisk-registrar` | voipbin-ast-registrar | 5082/udp | SIP registration (realtime DB) |
| `asterisk-call` | voipbin-ast-call | 5080/udp+tcp, 10000-10050/udp | Call server |
| `asterisk-call-proxy` | (no container_name — use `docker compose ps asterisk-call-proxy`) | - | ARI/AMI bridge to RabbitMQ |

### Manager Services

All manager services follow this pattern:
- Connect to MySQL via `DATABASE_DSN`
- Connect to RabbitMQ via `RABBITMQ_ADDRESS`
- Connect to Redis via `REDIS_ADDRESS`
- Expose Prometheus metrics on `:2112/metrics`

Key services:
- `api-manager` (port 8443) - External REST API gateway
- `customer-manager` - Customer and extension management
- `call-manager` - Call routing and control
- `flow-manager` - Workflow execution engine
- `billing-manager` - Usage tracking
- `registrar-manager` - SIP registration
- `contact-manager` - Contact management
- `direct-manager` - Direct (per-entity addressable endpoint) records; agent-manager
  RPCs it during agent creation, so admin-agent bootstrap depends on it
- `webchat-manager` - Webchat widgets and sessions (the user-facing websocket
  lives in api-manager)
- `rag-manager` - RAG (Retrieval Augmented Generation) knowledge base, backed by
  the local `postgres` (pgvector) service. **It runs with the placeholder GCP
  values init.sh writes, but RAG ingestion/query requires a real GCP project and
  service-account key** — embedding calls fail at Vertex AI / GCS until you
  replace `GCP_PROJECT_ID` / `GCP_REGION` / `GCP_BUCKET_NAME_MEDIA` and
  `GOOGLE_APPLICATION_CREDENTIALS` in `.env`
- `timeline-manager` - Call timeline analytics, backed by the local `clickhouse`
  service
- `schedule-manager` - Platform-internal cron (VOIP-1281): number renewal,
  execution-audit retention, and a nightly in-stack DB backup, all DB-scheduled
  and dispatched via RabbitMQ RPC like every other manager — no external
  CronJob or host crontab. See README.md "Scheduled Jobs (VOIP-1281)" for the
  seeded schedules, `schedule-control` CLI, and the host-side gaps (offsite
  backup copy, host OS maintenance) that stay operator-owned by design.

### Frontend

| Service | Container | Access | Purpose |
|---------|-----------|--------|---------|
| `square-admin` | (no container_name — use `docker compose ps square-admin`) | published host port 3003 | Admin dashboard UI |
| `square-meet` | (no container_name — use `docker compose ps square-meet`) | published host port 3004 | Video conferencing |
| `square-talk` | (no container_name — use `docker compose ps square-talk`) | published host port 3005 | Voice client |

**Note:** container_name pins for these 4 services and the frontend/proxy
services above were removed in the horizontal-scale-architecture Phase 1
change (2026-07-05) — they're only reachable via published host ports, not
by another service's hardcoded reference, so a fixed name adds nothing.
Use `docker compose logs -f <service>` / `docker compose ps <service>`
(service name, not container name) for these going forward. Services that
ARE still referenced by ops scripts (e.g. `voipbin-db`, `voipbin-customer-mgr`)
keep their container_name unchanged.

Each frontend service is reached via its published Docker host port and
resolves through CoreDNS to HOST_EXTERNAL_IP (see DNS Resolution section).

## Web Access

Web services resolve to HOST_EXTERNAL_IP via CoreDNS; Docker's published
host ports route to the correct container (no per-service dedicated
external IP or iptables forwarding — removed in Phase 1, see DNS
Resolution section above):

| Service | URL | Published Host Port |
|---------|-----|---------------------|
| API Manager | https://api.voipbin.test:8443 | 8443 → api-manager:443 |
| Admin Console | http://admin.voipbin.test:3003 | 3003 → square-admin:80 |
| Meet | http://meet.voipbin.test:3004 | 3004 → square-meet:80 |
| Talk | http://talk.voipbin.test:3005 | 3005 → square-talk:80 |

SIP services (sip.voipbin.test, pstn.voipbin.test, etc.) resolve to KAMAILIO_EXTERNAL_IP.

**Check configured IPs:**
```bash
sudo ./voipbin network status
sudo ./voipbin dns status
```

### SSL Certificate Trust (Important!)

When using self-signed certificates, browsers block API requests from the Admin Console because the certificate is not trusted. **You must manually accept the API certificate first:**

1. Open a new browser tab and navigate to: `https://api.voipbin.test`
2. You'll see a "Your connection is not private" warning
3. Click **"Advanced"** → **"Proceed to api.voipbin.test (unsafe)"**
4. You should see a JSON response or error page from the API
5. Now go to `http://admin.voipbin.test` and login will work

**Note:** This step is required because browser fetch/XHR requests don't show certificate acceptance prompts - they fail silently with `ERR_CERT_AUTHORITY_INVALID`.

### Browser-Trusted Certificates (Recommended)

To avoid the manual certificate acceptance step, use `mkcert`:

```bash
# Install mkcert
sudo apt install mkcert   # Ubuntu/Debian
brew install mkcert       # macOS

# Install the CA (makes certificates browser-trusted)
mkcert -install

# Regenerate certificates
rm -rf certs/
sudo ./voipbin init

# Restart browser
```

After this, `https://api.voipbin.test` will be trusted automatically.

## Test Data Setup

The `start.sh` script creates a test account on first run when opted in via `VOIPBIN_SANDBOX_DEV_SEED=true` (off by default):
- **Customer:** admin@localhost (login: admin@localhost / admin@localhost)
- **Extensions:** 1000, 2000, 3000 (passwords: pass1000, pass2000, pass3000)

**IMPORTANT: Never modify the database directly. Always use CLIs and APIs.**

### Manual Setup (if needed)

If you need to recreate test data after a reset:

```bash
./scripts/setup_test_customer.sh
```

### Manual Setup Steps

#### 1. Create Customer

```bash
# List existing customers
docker exec voipbin-customer-mgr /app/bin/customer-control customer list

# Create new customer (agent-manager auto-creates admin agent with random password)
docker exec voipbin-customer-mgr /app/bin/customer-control customer create \
  --name "Test Customer" \
  --email "admin@localhost"
```

#### 2. Get Customer ID and Set Admin Password

The admin agent is created automatically by agent-manager with a random unusable password.
You must set the password explicitly using `agent-control`.

```bash
# Get customer ID
CUSTOMER_ID=$(docker exec voipbin-customer-mgr /app/bin/customer-control customer list 2>/dev/null \
  | jq -r '.[] | select(.email == "admin@localhost") | .id')

# Wait for agent-manager to process the event (takes a few seconds via RabbitMQ)
sleep 5

# Get admin agent ID
AGENT_ID=$(docker exec voipbin-agent-mgr /app/bin/agent-control agent list \
  --customer-id "$CUSTOMER_ID" 2>/dev/null | jq -r '.[0].id')

# Set admin password
docker exec voipbin-agent-mgr /app/bin/agent-control agent update-password \
  --id "$AGENT_ID" \
  --password "admin@localhost"
```

#### 3. Login to Get JWT Token

```bash
# Login with admin credentials
curl -sk -X POST https://localhost:8443/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin@localhost", "password": "admin@localhost"}'

# Response contains: {"username": "...", "token": "JWT_TOKEN_HERE"}
```

#### 4. Create Extensions via API

```bash
TOKEN="<jwt_token_from_step_3>"

# Create extension
curl -sk -X POST https://localhost:8443/v1.0/extensions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"extension": "2000", "password": "pass2000", "name": "Extension 2000"}'
```

#### 5. Create API Key via CLI

```bash
docker exec voipbin-customer-mgr /app/bin/customer-control accesskey create \
  --customer-id "$CUSTOMER_ID" \
  --name "API Key" \
  --detail "For testing" \
  --expire 87600h
```

#### 6. Get Customer ID

```bash
curl -sk -X GET https://localhost:8443/v1.0/customer \
  -H "Authorization: Bearer $TOKEN"

# Response contains customer_id needed for SIP domain
```

### SIP Domain Format

All SIP requests from external clients must use the correct domain for Kamailio routing:

```
{customer_id}.registrar.voipbin.test
```

Example: `9e75d9a8-c289-4104-9ea6-8f6e238501f4.registrar.voipbin.test`

**Important**: Configure `DOMAIN_NAME_EXTENSION=registrar.voipbin.test` in `.env` for correct domain format. The registrar-manager uses this to generate the full domain `{customer_id}.registrar.voipbin.test`.

**Troubleshooting**: If extensions are created with wrong domain (e.g., `.voipbin.test` instead of `.registrar.voipbin.test`):
1. Check for shell environment variables overriding `.env`: `env | grep DOMAIN_NAME`
2. Unset any conflicting vars: `unset DOMAIN_NAME_EXTENSION DOMAIN_NAME_TRUNK`
3. Verify docker compose sees correct values: `docker compose config | grep DOMAIN`
4. Recreate registrar-manager: `docker compose rm -fsv registrar-manager && docker compose up -d registrar-manager`
5. Delete and recreate extensions via API to regenerate with correct domain

## Common Operations

```bash
# Rebuild a specific service after code changes
docker compose build api-manager
docker compose up -d api-manager

# Access MySQL (for debugging only - don't modify data directly)
docker exec -it voipbin-db mysql -uroot -p"$(grep MYSQL_ROOT_PASSWORD .env | cut -d= -f2)" bin_manager

# Access RabbitMQ management UI
# Open http://localhost:15672 (credentials are in your .env file: RABBITMQ_DEFAULT_USER/RABBITMQ_DEFAULT_PASS)

# Check service health
docker compose ps
docker exec voipbin-ast-call asterisk -rx "core show help"

# View Asterisk CLI
docker exec -it voipbin-ast-call asterisk -rvvv

# View Kamailio logs
docker logs -f voipbin-kamailio

# Check PJSIP endpoints (asterisk-registrar uses realtime DB - no reload needed)
docker exec voipbin-ast-registrar asterisk -rx "pjsip show endpoints"
```

## Service Dependencies

```
db, redis, rabbitmq (infrastructure)
    └── All manager services depend on these
        └── asterisk-call-proxy depends on asterisk-call
            └── call-manager depends on asterisk-call-proxy
                └── square-admin depends on api-manager
```

## Volumes

| Volume | Purpose |
|--------|---------|
| `db_data` | MySQL data persistence |
| `postgres_data` | PostgreSQL/pgvector data persistence (rag-manager) |
| `clickhouse_data` | ClickHouse data persistence (timeline-manager) |
| `asterisk-call-recording` | Call recordings |

## Testing Extension-to-Extension Calls

### SIP Registration Test

```bash
# Register extension (use customer_id from setup)
python3 scripts/softphone.py 2000 pass2000 \
  --server 192.168.45.152 \
  --customer-id <customer_id>

# Verify registration in Asterisk
docker exec voipbin-ast-registrar asterisk -rx "pjsip show contacts"
```

### Test Call Script

```bash
# Run test call from 2000 to 3000
python3 scripts/test_call.py
```

### SIP Routing Architecture

```
External SIP Client
    ↓
Kamailio (192.168.45.152:5060)
    ↓ filters by domain: {customer_id}.registrar.voipbin.test
Asterisk Registrar (5082) - for REGISTER
Asterisk Call (5080) - for INVITE
    ↓
Back through Kamailio to destination
```

### Known Limitations

**~~Flow Execution Timing Issue~~ (fixed 2026-07-01):** This section previously
documented the A-leg terminating a `confbridge_join`/`connect` call flow with
BYE/CANCEL before the B-leg could fully answer. Root cause was call-manager
never answering the master call-in channel while its peer joined the
conference bridge. Fixed in monorepo `bin-call-manager` by
`NOJIRA-Fix-call-bridge-auto-answer` (commit `f1dd2687a`, PR #1033): the join
channel's `ChannelStateChange` to `Up` now auto-answers non-Up peer channels
in the same call bridge. Covered by
`pkg/confbridgehandler/ari_event_test.go` (`Test_ARIChannelStateChangeTypeJoin`,
`Test_answerCallBridgePeers`). No further changes to this area since. Live
E2E re-verification with an actual conference call is still pending in this
sandbox (blocked on an unrelated Kamailio external-IP drift, see "Dynamic IP
Detection" above) — remove this note entirely once that's confirmed.

## API Reference

Base URL: `https://localhost:8443`

### Authentication
- `POST /auth/login` - Get JWT token (body: `{"username": "email", "password": "email"}`)

### Extensions
- `GET /v1.0/extensions` - List extensions
- `POST /v1.0/extensions` - Create extension
- `GET /v1.0/extensions/:id` - Get extension
- `DELETE /v1.0/extensions/:id` - Delete extension

### Access Keys
- `GET /v1.0/accesskeys` - List API keys
- `POST /v1.0/accesskeys` - Create API key (requires `expire` timestamp)
- `DELETE /v1.0/accesskeys/:id` - Delete API key

### Customer
- `GET /v1.0/customer` - Get current customer info

## Commit Message Format

This directory now lives inside the `voipbin/voipbin` repo. Use project prefix `voipbin:` for
changes (not `sandbox:` — that prefix belonged to the standalone `voipbin/sandbox` repo this
directory was copied from):

```
Summary of changes (max 72 chars)

- voipbin: Specific change description
```
