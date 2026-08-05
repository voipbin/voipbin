# Implementation Plan: Sandbox Dual-Mode DNS (VOIP-1275)

- **Date:** 2026-07-31
- **Design:** `2026-07-31-sandbox-dual-mode-dns-design.md` (APPROVED rev 4) — this plan implements it; design decisions are not re-litigated here. Section references (§) point at the design doc.
- **Status:** APPROVED (plan review: round 1 Request Changes, rounds 2–3 Approve; rev 3 applies round 3's non-blocking suggestions)
- **Branch:** `VOIP-1275-Sandbox-dual-mode-dns` (single PR)

Execution rules: phases land as separate commits in design order; the bats suite, `bash -n` on touched scripts, `check-env-template-sync.sh`, and `sync-compose-images.sh` must be green at every commit. Existing function/variable naming conventions of each script are followed.

## Phase 1 — Domain derivation + init contract (§2.1, §2.2, §2.7)

**`scripts/common.sh`**
1. Add `get_env_var <env-file> <var-name>`: prints the value of `VAR=` from the file (last occurrence wins, no sourcing — must be safe on untrusted content); empty output if absent. Used by every mode gate in later phases.
2. Add `derive_domain_env <base_domain>`: sets `DERIVED_API_URL`, `DERIVED_WEBSOCKET_URL`, `DERIVED_REGISTRAR_URL`, `DERIVED_REGISTRAR_DOMAIN`, `DERIVED_CONFERENCE_URL`, `DERIVED_CONFERENCE_DOMAIN`, `DERIVED_DOMAIN_NAME_EXTENSION`, `DERIVED_DOMAIN_NAME_TRUNK`, `DERIVED_EMAIL_VERIFY_BASE_URL`, `DERIVED_BASE_DOMAIN`, `DERIVED_BASE_HOSTNAME` exactly per the §2.1 table (trailing slash on `API_URL`, none on `EMAIL_VERIFY_BASE_URL`).
3. Add `get_domain_mode <env-file>`: wraps `get_env_var DOMAIN_MODE`, maps empty → `internal` (§2.5 legacy rule). All later gates call this, never `get_env_var DOMAIN_MODE` directly.
3a. `check_compose_profiles_conflict` is **defined in `common.sh`** (next to the two functions above), because two entry points consume it: `start.sh` (Phase 2 task 0) and `check-install.sh` (Phase 5) — the latter cannot source `start.sh` (it ends with `main "$@"`). Defining it at the choke point avoids a Phase-5 refactor.

**`scripts/init.sh`**
4. Add `parse_args "$@"` called from inside `main()` (test_helper strips only `main "$@"` — §2.2). Flags: `--mode`, `--domain`, `--tls`, `--cert`, `--key`, `--yes`, `--force-reinit`, `--help`. Defaults: `mode=internal`, `tls` auto (mkcert if available else selfsigned — current behavior) **in internal mode only**; `--mode external` with no `--tls` is rejected outright with "external mode requires `--tls byo --cert ... --key ...`" (the auto-default must not fall through into the mkcert-rejection message). Other rejections per §2.2: `--mode external` without `--domain`; `--domain` with `--mode internal`; `--tls mkcert|selfsigned` with `--mode external`; `--tls byo` without both `--cert` and `--key`; unknown flag. Domain validation: lowercase RFC-1123 labels, ≥2 labels, no scheme/port/trailing dot.
5. Existing-`.env` compatibility as a **named function** `check_existing_env_compat` (unit-testable; called from `main()` before any generation): read existing `DOMAIN_MODE`(via `get_domain_mode`)/`BASE_DOMAIN`; same mode+domain → current prompt semantics (`--yes` auto-confirms); different → refuse (exit 1) with the §2.7 message naming `clean.sh --volumes --purge` and `--force-reinit`.
5a. `--force-reinit` as a named function pair: (a) internal→external precondition check (`check_force_reinit_preconditions`): coredns container exists? `/etc/resolv.conf.voipbin-backup` exists? → refuse with the exact teardown commands while any remain; (b) success path: proceed with `.env`/certs/Corefile rewrite for the new domain and, on completion, print the §2.7 live-state follow-up block verbatim (delete + recreate extensions via API / `setup_test_customer.sh`; recreate `registrar-manager`, `api-manager`, `hook-manager`, `customer-manager`, `square-*`), never touching the DB. Both the refusal and the follow-up message are bats-tested.
6. Replace the 11 heredoc literals with `API_URL=$DERIVED_API_URL` etc. (checker-compatible, §2.1). Call `derive_domain_env "$TARGET_DOMAIN"` before the heredoc where `TARGET_DOMAIN` is `voipbin.test` (internal) or `--domain` (external).
7. Add to the heredoc: `DOMAIN_MODE=$INIT_MODE`, `TLS_MODE=$INIT_TLS_MODE`, and a single `COMPOSE_PROFILES=$INIT_COMPOSE_PROFILES` line (`internal-dns` in internal mode, empty string in external) — key always present, checker-visible (§2.1).
7a. **External-mode gate on `setup_dns()` (`init.sh:32` Corefile write + `setup-dns.sh -y` call), effective on both the root and unprivileged paths** — §2.3/§2.4 commit that external init never generates a Corefile; without this gate `sudo ./scripts/init.sh --mode external` would still write `.test` zones.
8. Result lines (§2.2): a single `emit_result`/`die` helper plus an `EXIT` trap — **registered inside `main()`, not at top level** (test_helper sources the whole file minus `main "$@"`, so a top-level trap would fire at every bats test-shell exit), with an already-emitted flag so the success path doesn't double-print — guarantees every exit path — including `set -e` aborts — ends with `VOIPBIN_INIT: status=ok|error ...` on stdout. Exit codes: 0 success; 1 validation/user error (bad flags, refuse-on-switch, bad domain, failed cert pre-flight); 2 environment error (missing `.env.template`, missing openssl, unwritable paths). The success line's `next=` is `"sudo ./scripts/setup-host.sh"` when unprivileged, `"./scripts/start.sh"` when root (Phase 3 activates the unprivileged path; the line format lands here). The same helper+trap pattern is reused for the other four result-line prefixes.

**`.env.template`**
9. Add `DOMAIN_MODE`, `COMPOSE_PROFILES`, `TLS_MODE` with comments; annotate the 11 derived vars with "derived from BASE_DOMAIN by init.sh — edit BASE_DOMAIN and re-run init, do not edit individually".

**Test-isolation enabler (prerequisite for every new bats test in this plan)**
10. All path constants in touched/new scripts become override-friendly: `PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"`, same for `ENV_FILE`, `CERTS_DIR` (existing scripts get the pattern as they are touched; all three new scripts ship with it). Blast-radius note at each override site (one-line comment): an operator's exported `PROJECT_DIR` now redirects destructive scripts (`clean.sh --volumes`) — low probability, but documented deliberately rather than discovered. `tests/test_helper.bash`: `load_init_functions` re-asserts `PROJECT_DIR`/`ENV_FILE`/`CERTS_DIR` to `$TEST_TEMP_DIR` (today it only re-fixes `SCRIPT_DIR`, and since `init.sh:12` recomputes `PROJECT_DIR` from `SCRIPT_DIR`, existing `generate_cert` tests actually write into the **real repository's** `certs/` tree — the isolation work is load-bearing, not hygiene); add analogous loaders for `start.sh`, `setup-host.sh`, `install-certs.sh`, `check-install.sh`. Any bats test invoking a script that can reach `docker`/`docker compose` runs with the suite's `MOCK_BIN_DIR` docker stub **and** an explicit `PROJECT_DIR=$TEST_TEMP_DIR` — nothing may touch the real tree or daemon (the `clean.sh --volumes` test would otherwise wipe live volumes).

**Tests (Phase 1)**
11. `tests/common.bats`: `derive_domain_env voipbin.test` — resulting `DERIVED_*` variable values byte-compare against the 11 current literal values; `derive_domain_env example.com` spot-checks; `get_env_var` (absent var, last-wins, no code execution from `$(...)` content); `get_domain_mode` empty→internal.
12. `tests/init.bats`: each parser rejection (incl. external-without-`--tls`); `check_existing_env_compat` matrix (same/same, same/diff-domain, diff-mode) against fixture `.env` files in `$TEST_TEMP_DIR`; result-line grammar (`grep -E '^VOIPBIN_INIT: status=(ok|error)'`); `check_force_reinit_preconditions` refusal with stubbed host probes; `--force-reinit` success-path follow-up message.
13. `check-env-template-sync.sh` green (new vars in both template and heredoc).

## Phase 2 — CoreDNS profile + mode gating (§2.3, §2.4)

**Ordering note (load-bearing):** the stale-`.env` guard (task 0) lands **before** the compose profile (task 1) — otherwise, between the two commits, a pre-cycle `.env` (no `COMPOSE_PROFILES`) silently loses coredns while resolv.conf still points at 127.0.0.1: host-wide DNS breakage on exactly the machines with existing installs.

**`scripts/start.sh`**
0. Stale-`.env` + `COMPOSE_PROFILES` conflict guard as a named function `check_compose_profiles_conflict` called at the top of `main()` (before `validate_env`'s `set -a; source .env` at `:240` — §6), **only when `$PROJECT_DIR/.env` exists** (a missing `.env` keeps today's `start.sh:586` "run init first" message): (a) shell-exported `COMPOSE_PROFILES` contradicting `.env` → fail fast; (b) internal mode (`get_domain_mode`) with no `COMPOSE_PROFILES` key in `.env` → `VOIPBIN_START: status=error reason="stale .env, re-run ./scripts/init.sh --yes"`. (Moved up from Phase 3 per the ordering note.)

**`docker-compose.yml`**
1. `coredns` service: add `profiles: ["internal-dns"]`. No other compose change.

**`scripts/common.sh`**
2. `update_env_ips()`: build the two URLs from `BASE_DOMAIN` read out of the target `.env` (fallback `voipbin.test` if absent); in external mode (`get_domain_mode`) skip the URL rewrites entirely; IP vars update in both modes.
3. `regenerate_ssl_certs()`: first line — if `TLS_MODE` from `.env` is `byo`, log a skip and `return 0` (**not** `return 1`: all three call sites — `common.sh:258` inside `regenerate_ip_config` reached from `start.sh:617`, `setup-voip-network.sh:146`, and `setup-dns.sh:317` — are bare commands under `set -e`, so a non-zero return would abort the caller mid-run on every external-mode IP change). The same return-0-on-refuse rule applies to every new gate at a shared choke point.
4. `regenerate_ip_config()`: gate the `generate_coredns_config` call (`common.sh:254`) on internal mode; external logs "operator DNS may need updating (HOST_EXTERNAL_IP changed)" and continues (§2.4).

**`scripts/start.sh` (continued)**
5. Step 7 (`:648-656`) and Step 8 (`:661-668`): wrap in internal-mode gate; external logs one line "DNS is operator-managed (external mode)" (§2.4 fourth clobber site).
6. Issuer-check/regen branch (`:135-149`): skip when `TLS_MODE=byo`; missing/expired cert in byo mode → fail fast naming `install-certs.sh` (§2.4).
6a. Final summary block (`:735-771`, incl. `SIP Domain: ${CUSTOMER_ID}.registrar.voipbin.test` at `:771`): derive printed URLs/domains from `BASE_DOMAIN`/`DOMAIN_NAME_EXTENSION` in `.env` — in external mode the hardcoded summary would actively misinform the operator.

**`scripts/setup-dns.sh`**
7. Top of `main()`: if external mode (read from `$PROJECT_DIR/.env` when it exists), print "external mode: DNS is operator-managed, skipping" and exit 0 — all subcommands **except `--uninstall`**, which stays mode-independent: leftover resolv.conf hijack state (e.g. `clean.sh --purge` → `init --mode external` sequence) must remain removable, otherwise `check-install.sh`'s external resolv.conf check fails with no remedy. (Deviation from design §2.3's blanket wording, justified here; the design's intent — external mode never *creates* DNS state — is preserved.)

**`scripts/stop.sh`** — 8. `restore_dns()`: external mode → no-op with log line. (Already a de-facto no-op — it is guarded by the backup file's existence — so this is an explicit-logging change, not a correctness fix.)

**`scripts/clean.sh`**
9. `--dns` path: external mode → no-op with log line.
10. `--volumes` path: also remove `$PROJECT_DIR/.test_data_initialized` (§2.7).

**`scripts/setup-voip-network.sh`** — 11. Gate the `generate_coredns_config` call (`:150`) on internal mode.

**`scripts/_gen_test_override.py`** — 12. Emit `profiles: !override ["host-mutating"]` for **all** EXCLUDE services (uniform — semantically identical for the ones with no base profile, and avoids special-casing coredns); regenerate `docker-compose.test.yml` and commit both (§2.3).

**`scripts/voipbin-cli.py`** — 13. Move `.test_data_initialized` from the purge-gated `files_to_remove` list to the `--volumes` branch (one entry lifted out, §2.10).

**Tests (Phase 2)**
14. `tests/common.bats`: `update_env_ips` against an external-mode fixture `.env` (URLs untouched, IPs updated) and an internal fixture with non-default `BASE_DOMAIN`; `regenerate_ssl_certs` byo skip (returns 0, no regeneration).
15. New `tests/mode-gates.bats`: `setup-dns.sh` external no-op (exit 0 + message, `PROJECT_DIR=$TEST_TEMP_DIR` so it reads the fixture `.env`, sudo-free because it exits before root checks); `clean.sh --volumes` removes marker — **run with the docker stub on PATH and `PROJECT_DIR=$TEST_TEMP_DIR`** (per the Phase-1 isolation rule; without both, this test would wipe the developer's live volumes); `check_compose_profiles_conflict` matrix (shell-conflict, stale-`.env`, clean cases).
16. Compose fixtures: `docker compose --env-file <mode1.env> config --services` lists coredns; mode-2 fixture omits it; counts 48/47. Run `scripts/tests/t_infra.sh`'s config check (`config -q`) with the regenerated override.
17. `sync-compose-images.sh` green (profiles addition must not disturb image-line parsing).

## Phase 3 — Sudo isolation (§2.5)

**New `scripts/setup-host.sh`**
1. Root-required (`check_root`). Reads `.env` (must exist — else error result line pointing at init). Mode via `get_domain_mode`.
2. Internal: (a) install mkcert package if missing (apt/brew, the logic removed from `start.sh:97-129`); (b) CAROOT two-pass trust install per §2.5 — `CAROOT="$(sudo -u "$SUDO_USER" -H mkcert -CAROOT)" mkcert -install` for the system store, then `sudo -u "$SUDO_USER" -H env CAROOT=... mkcert -install` for the user NSS store; when `SUDO_USER` is unset (direct root login) skip the user pass **and** the user-CAROOT resolution entirely (plain `mkcert -install` with root's default CAROOT — never execute `sudo -u ""`); (c) Corefile generation + `setup-dns.sh -y`; (d) `setup-voip-network.sh`. External: (d) only. (Traceability note: design §2.5's prose lists Corefile generation among init's unprivileged steps while its table assigns DNS setup to `setup-host.sh`; this plan follows the table — Corefile generation is coupled to `setup-dns.sh` inside `setup_dns()`, and `start.sh` Step 7 regenerates it each internal-mode start anyway.)
3. Idempotence: each step probes current state (mkcert present? CA installed per `mkcert -check` semantics/issuer probe? resolv.conf already 127.0.0.1? interfaces exist?) and logs skip. Result line `VOIPBIN_SETUP_HOST: status=ok mode=<m> steps=<comma-list>`.

**`scripts/init.sh`**
4. Remove `check_root` (`:161`). Root path: unchanged behavior (mkcert CA install via `setup_mkcert_ca`, DNS setup at the end) **plus** chown of `.env`, `certs/`, `config/` to `$SUDO_USER` when set (§2.5). Unprivileged path: skip `setup_mkcert_ca` (`:186`) and `setup_dns` (`:387`); cert generation still runs (mkcert creates CA implicitly); result line `next="sudo ./scripts/setup-host.sh"`.

**`scripts/start.sh`**
5. (Conflict/stale guard already landed in Phase 2 task 0.)
6. Replace `check_root` (`:577`) with a named prerequisite check `check_host_prereqs` (unit-testable): VoIP interfaces present — **reusing `check_voip_interfaces` (`start.sh:304-309`), which probes both `kamailio-int` and `rtpengine-int`**, making the Step-9 `sudo` at `:677` provably unreachable on the unprivileged path (a narrower probe would reintroduce a sudo prompt into the AI flow); internal mode additionally resolv.conf → 127.0.0.1 (or CoreDNS answering). Root + missing prereqs → run host setup inline, **by invoking `setup-host.sh` as a subprocess** (single implementation; no sourcing). Unprivileged + missing → fail fast, `next="sudo ./scripts/setup-host.sh"`. Unprivileged + satisfied → proceed.
7. `setup_mkcert()`: remove the two escalation sites (`:106`, `:124-126`); keep issuer/regen branch (Phase 2 already TLS-gated it); missing mkcert in internal mode → fail fast pointing at `setup-host.sh`.
8. Step-9 `setup-voip-network` call (`:677`): only reached on the root path (prereq check guarantees it); add comment.
9. Result line `VOIPBIN_START: status=ok services=<running>/<total>` / `status=error reason=...`.

**Tests (Phase 3)**
10. `tests/init.bats`: unprivileged run (mock `EUID`/stub `setup_mkcert_ca`, `setup_dns`) produces `.env` + certs in `$TEST_TEMP_DIR`, never calls the stubs, result line `next=setup-host`.
11. New `tests/setup-host.bats` (new `load_setup_host_functions` loader): mode step-selection with stubbed sub-scripts; CAROOT handoff command shape; idempotent skip logging; refuses without `.env`.
12. New `tests/start.bats` (using the `load_start_functions` loader from Phase 1 task 10): `check_host_prereqs` branch matrix (root/non-root × prereqs present/absent) with stubs.

## Phase 4 — External TLS (§2.6)

1. **New `scripts/install-certs.sh`**: per §2.6 numbered list — validation (key-matches-cert; SAN covers `api. sip. sip-service. conference. trunk. registrar.` of `BASE_DOMAIN`, hard fail; expiry <30d warn; `*.registrar.` warn), layout install (`certs/api/{cert,privkey}.pem` + five `certs/<name>.<d>/{fullchain,privkey}.pem`), `.env` base64 rewrite (temp file + atomic move, owner/group/mode captured before and restored after; new cert dirs inherit `certs/` owner), conditional service recreate (`api-manager`, `hook-manager` recreate; `kamailio` restart) only when running, result line `VOIPBIN_CERTS:`. Also `--check-only` and `--domain <d>` flags (the latter overrides the `.env` lookup) for init's pre-`.env` pre-flight.
2. **`scripts/init.sh`**: external mode runs `install-certs.sh --check-only --domain "$INIT_DOMAIN" <cert> <key>` **before** writing `.env` (§2.6 pre-flight), then full install after `.env` exists; skips `generate_cert` loop (`:208`) and `generate_api_cert` (`:215`).
3. **Delete `scripts/generate-certs.sh`** (§1.10).
4. **Tests:** new `tests/install-certs.bats` with openssl fixture certs (wildcard covering all names; non-wildcard missing one name → hard fail; expired → warn; base64 values land in fixture `.env`; ownership restore logic with stubbed stat where root isn't available).

## Phase 5 — `check-install.sh` (§2.8)

1. **New `scripts/check-install.sh`**: unprivileged; reads `.env`; the §2.8 check table exactly — derived service count (`docker compose config --services` vs `ps`, identical env, **and none unhealthy/restarting/exited**), cert-trust-chain check (internal: issuer of `certs/api/cert.pem` vs `mkcert -CAROOT` CA; external: `curl` without `-k` + expiry >14d), DNS checks (internal `dig @127.0.0.1`; external system resolver, mismatch lists expected records), API `/ping`, registrar-manager `DOMAIN_NAME_EXTENSION` via `docker inspect`, resolv.conf-matches-mode. Also runs `check_compose_profiles_conflict` (shell-vs-`.env`, §6 names both scripts) and the stale-`.env` rule (§2.5). Per-check `CHECK <name>: pass|fail|skip <detail>` lines; final `VOIPBIN_CHECK: status=... passed=N failed=M mode=<m>`; exit 0 only on all-pass.
2. **Tests:** new `tests/check-install.bats`: mode-aware check selection and result grammar with stubbed `docker`/`dig`/`curl` (the suite's existing stub pattern); both fixture `.env`s.

## Phase 6 — CLI + SIP tooling (§2.10)

1. `scripts/voipbin-cli.py`: `status`/`info`/`open` compose URLs from `.env` `BASE_DOMAIN`; `dns` subcommands external-mode message + §2.9 record table (domain/IPs substituted) exit 0; `certs status`/`certs trust` external-mode: report expiry/SANs, point at `install-certs.sh`, never advise `rm -rf certs/`.
2. `scripts/test_call.py`: defaults from `.env` when present — server `sip.<BASE_DOMAIN>`, domain `{cid}.<DOMAIN_NAME_EXTENSION>`; explicit args/env still win. Same for `scripts/softphone.py`.
3. **Tests:** CLI is not covered by bats today, and direct invocation is not testable (`voipbin-cli.py:main()` calls `check_root()` → `sys.exit(1)` unprivileged, then `os.chdir`s to the configured project dir). Instead: a small `tests/test_cli_mode.py` imports the file as a module (`importlib.util.spec_from_file_location` — import does not call `check_root`) and exercises the dns/certs handlers directly against both fixture `.env`s, pointing the CLI at the fixture tree via the existing `VOIPBIN_PROJECT_DIR` env override (`Config.get` honors `VOIPBIN_<KEY>` — never `Config.set`, which writes the developer's real `~/.voipbin-cli.conf`); guard for the `yaml` import dependency. Run from the DoD list alongside `py_compile`.

## Phase 7 — Documentation (§2.9, §2.11)

(Note: design §3 places the LE recipe docs in its Phase 4; this plan consolidates all documentation here — a deliberate mapping change so docs land once, complete.)

1. **README.md:** "Install modes" section (mode table + decision guidance); external-mode walkthrough (prereqs → DNS records → obtain cert → `init --mode external` → `setup-host` → `start` → `check-install`); the §2.9 DNS record runbook incl. apex `registrar.<d>`, the directly-routable-hosts amendment, and the cleartext-UI warning + reverse-proxy recommendation; LE recipe + renewal deploy-hook; domain-in-state caveat; existing `.voipbin.test` sections get "internal mode (default)" scoping notes.
2. **CLAUDE.md:** AI-install contract (§2.11): the 4-command unprivileged flow, the single sudo command, result-line grammar for all five prefixes, mode detection, caveat; env-var table additions.
3. `tasks/lessons.md`: entry for the CAROOT-under-sudo and clobber-site findings (per repo lessons policy).

## Verification (Definition of Done for the PR)

1. `bats tests/` fully green (existing 126 + new).
2. `bash -n` all touched shell scripts; `python3 -m py_compile` for touched Python; `python3 tests/test_cli_mode.py` (plain asserts, no pytest dependency).
3. `./scripts/check-env-template-sync.sh` green; `./scripts/sync-compose-images.sh` green (no drift).
4. `docker compose --env-file` mode-1/mode-2 fixtures: `config -q` passes; service lists 48/47; coredns present/absent.
5. **Live internal-mode verification on this machine** (§4): `./scripts/init.sh --yes` (unprivileged) → `sudo ./scripts/setup-host.sh` → `./scripts/start.sh` → `./scripts/check-install.sh` all-pass → `test_call.py` SIP register/call. This is the mode-1 no-regression gate and discharges the VOIP-1274 deferred verification for the new flow.
6. Live external-mode verification: requires a directly-routable host + real domain (§4); tracked as the ticket's Done gate. Not blocking the PR; recorded in the PR body and the ticket.

## Explicitly not in this PR

Everything in design §5 (Non-goals) and §7 (Follow-ups). No monorepo changes (§1.9 probe-confirmed).
