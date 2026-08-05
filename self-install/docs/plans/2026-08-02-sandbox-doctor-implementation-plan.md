# Sandbox Install Doctor (VOIP-1280) - Implementation Plan

Design: docs/plans/2026-08-02-sandbox-doctor-design.md (approved after 5 review rounds).
Branch: VOIP-1280-Add-sandbox-install-doctor. All work TDD: each step writes/extends bats
tests first (RED), implements to green, then moves on. The design doc is the single source
of truth for probe semantics; this plan only sequences the work and pins file-level tasks.

## Step 1 - test scaffolding

Files: `tests/test_helper.bash`, `tests/doctor.bats` (new).

1.1 Add `load_doctor_functions` to test_helper.bash: standard sed shape (strip trailing
`main "$@"`, substitute the `source "$SCRIPT_DIR/common.sh"` substring); no set-e strip
(doctor has none, add the same explanatory comment as the check-install loader). Re-assert
`SCRIPT_DIR`/`PROJECT_DIR`/`ENV_FILE`/`CERTS_DIR` after sourcing.

1.2 Create `tests/doctor.bats` with: header isolation comment (check-install.bats wording
adapted), `setup()`/`teardown()` using `setup_test_env` + `RESOLV_FIXTURE`, `run_doctor()`
wrapper (`env -u COMPOSE_PROFILES RESOLV_CONF=... bash "$SCRIPTS_DIR/doctor.sh"`), fixture
builders `make_internal_env`/`make_external_env` (copy from check-install.bats), and stub
builders. New stubs beyond the check-install set:

- `stub_ss <spec>`: emits `ss -tuln`/`-tulnp` fixture lines (LISTEN rows with configurable
  address:port and optional process attribution).
- `stub_ip_ok` / `stub_ip_missing`: answers `ip link show kamailio-int|rtpengine-int`.
- `stub_df <free_kb>`: `df -P` output for the disk-space thresholds.
- `stub_id <groups>`: answers `id -nG`.
- extended `stub_docker_*` variants covering: `info` ok/fail, `compose version --short`,
  `compose ps -a` with State/Status columns, `compose ps -q <service>`, `inspect -f` for
  HostConfig.NetworkMode / State.StartedAt / registrar env, `compose exec -T rabbitmq
  rabbitmq-plugins list -e`, `compose exec -T rabbitmq rabbitmqctl list_queues`,
  `compose exec -T db ...` (mysqladmin ping / table counts), `network inspect`, `logs`.
  One parameterized builder (`stub_docker_doctor <scenario>`) with per-scenario case
  branches, logging `COMPOSE_PROFILES=${COMPOSE_PROFILES-<unset>}` to compose_env.log
  (identical-environment assertion).

Deliverable: helpers in place, `bats tests/doctor.bats` runs 0 tests green.

## Step 2 - doctor.sh skeleton (RED then GREEN)

Files: `scripts/doctor.sh` (new).

2.1 Tests first: grammar tests (final-line regex for pre-install stage, exit codes,
DOCTOR/FIX line shape), pre-install scenario 8 (no .env → prerequisites run, env-file
fail + FIX, later checks skip, exit 1).

2.2 Implement: header contract comment, path/override block with blast-radius comments
(incl. `DOCTOR_DISK_MIN_GB`/`DOCTOR_DISK_WARN_GB`), guarded single-line source of
common.sh (exit 2 path), counters + `doctor_result` + `doctor_fix` + `PRESCRIPTIONS`,
`load_doctor_env`, `detect_stage`, `DOCTOR_DOCKER_OK` flag, banner, summary
(indented `  [<name>] <command>` re-list), final `VOIPBIN_DOCTOR:` line, `main "$@"`
last line. Stub check functions returning skip so the skeleton runs end-to-end.

## Step 3 - category 1 checks (prerequisites)

Order: `docker`, `docker-group`, `compose-version`, `tools`, `ports`, `disk-space`.
Tests per check: healthy pass + the design's fail/warn/skip cases. Scenario 2 (stopped
daemon → dependent checks skip) lands here - note it passes vacuously while categories
2-3 are still skeleton stubs; it must be consciously re-verified after step 5. Port policy
exactly as pinned in design §5 (bind-address-aware, systemd-resolved whitelist,
pre-install needed-address set via `detect_current_host_ip`). EUID=0/SUDO_USER handling
for docker-group. macOS gating test (design §4): starts in step 3 asserting only `ports`
(`mock_uname "Darwin"`); the `voip-interfaces` assertion is added in step 4 and
`proxy-netns` in step 5, when those checks exist - keeps every step green.
Early-abort test: doctor.sh copied next to a missing common.sh → `status=error` line,
exit 2.

## Step 4 - category 2 checks (install state)

Order: `env-file`, `domain-mode`, `compose-profiles`, `dns-internal`/`dns-external`,
`certs`, `certs-env-sync`, `voip-interfaces`, `compose-network`.
Tests: scenarios 3 (commented resolv.conf), 4 (expired cert via capability-probed
`-not_after`, near-expiry warn via real openssl `-days 5`), 7 (external per-record diff,
assert all three seeded records), TLS_MODE-gated SAN/issuer (selfsigned skip case),
base64 sync mismatch, wildcard-aware SAN matching (mkcert-shaped fixture with
`*.voipbin.test` SAN built with real openssl `-addext`). The certs check also implements
the §7 EUID=0 rules: CAROOT resolved via `sudo -u "$SUDO_USER" mkcert -CAROOT` when
EUID=0, issuer clause skipped when EUID=0 with SUDO_USER unset (mirroring docker-group).

## Step 5 - category 3 checks (runtime pathology)

Order: `containers` (`ps -a` pinned), `rabbitmq-plugin`, `queue-consumers`,
`proxy-netns`, `database`, `realm`, `test-data`.
Tests: scenarios 5 (plugin disabled), 6 (zero-consumer + Up proxy → exact proxy-restart
FIX), pre-start stage (stack down → all category 3 skip), proxy-netns both pathologies
(owner recreated: NetworkMode id mismatch; owner-alone restart: StartedAt inversion,
epoch comparison), realm mismatch, marker-vs-schema fail/warn.

## Step 6 - healthy-path scenarios and grammar closure

Scenario 1 (healthy internal all-pass, exit 0, final-line regex `warned=0 mode=internal`),
7b (healthy external all-pass, `mode=external`), 9 (every line matches grammar; skip/warn
counter semantics), 10 (compose_env.log sort -u). Fix any accumulated drift.

## Step 7 - CLI wiring

Files: `scripts/voipbin-cli.py`, `tests/test_cli_mode.py` (extend).

`cmd_doctor` via `subprocess.call` (exit code, not wait status), `self.last_doctor_rc`,
non-interactive dispatch exits with it (REPL path returns). Register in all five places
(`commands` dict, `help_text`, `cmd_help` Service Commands block next to `status`,
`show_cli_usage`, completer no-op). Test (scenario 11): registration present
(anti-`cmd_hook`-gap), non-interactive propagation, REPL non-exit.

## Step 8 - documentation

- CLAUDE.md: `doctor.sh` row in the result-line table, doctor subsection (DOCTOR/FIX
  grammar, any-stage property, `grep '^FIX '` agent contract), optional-5th-step mention
  in the 4-command flow.
- README.md: Quick Diagnostics first entry; unprivileged flow recovery pointer.
- tests/README.md: doctor.bats entry.
- No em dashes in new prose (repo scripts' existing inline "—" in check-install details
  stays untouched; doctor FIX lines use plain wording).

## Step 9 - verification gate

1. `bats tests/` full suite green (all suites, not just doctor.bats).
2. `bash -n scripts/doctor.sh` + `shellcheck scripts/doctor.sh` if shellcheck present
   (advisory; match existing scripts' shellcheck cleanliness level, do not fix pre-existing
   styles elsewhere).
3. `./scripts/check-env-template-sync.sh` still exits 0 (no .env template changes expected).
4. `python3 -m py_compile scripts/voipbin-cli.py` + run `tests/test_cli_mode.py`.
5. Live smoke on this host (stack currently installed): `./scripts/doctor.sh` unprivileged;
   expected all pass or explainable warns; capture output for the PR description.
6. File-size guideline: doctor.sh under 800 lines (design estimate 500-600).

## Acceptance criteria (from ticket, restated)

- [ ] Healthy internal-mode install: all pass, exit 0 (bats scenario 1 + live smoke).
- [ ] Seeded failures each produce fail + correct prescription (scenarios 2-6, bats).
- [ ] Both modes; external DNS diff lists every missing/mismatched record (7, 7b).
- [ ] `voipbin doctor` wired; README/CLAUDE.md document it.

## Commit / PR

Single commit series on the branch, one PR (no splitting). PR body lists changes with the
`sandbox:` project prefix convention of this repo's history (scripts/tests/docs bullets).
Merge only on explicit approval, squash.
