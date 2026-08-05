# Sandbox Install Doctor (VOIP-1280) - Design

Date: 2026-08-02
Ticket: VOIP-1280 "Sandbox install doctor: diagnose-and-prescribe helper for install and runtime state"
Predecessors: VOIP-1275 (dual-mode DNS, merged), VOIP-1278 / VOIP-1279 (backlog; doctor covers their failure modes as detection/mitigation until they land).

## 1. Problem and scope

`check-install.sh` answers "is the install good?" as a CI-style pass/fail gate. It assumes an
`.env` exists and a stack is (mostly) up, and it exits early otherwise. When something is wrong,
the operator (human or AI agent) still has to figure out *what* is wrong and *which command*
fixes it, across three very different stages: before `init.sh` has ever run, mid-install, and on
a running stack that developed a runtime pathology (the VOIP-1278 plugin failure, the VOIP-1279
silent proxy death).

The doctor closes that gap: one unprivileged command that runs at any stage, diagnoses
everything it can, and prints the exact recovery command for every failure, in wording
consistent with README.md / CLAUDE.md.

Non-goals:
- Doctor does not fix anything. It only diagnoses and prescribes (no `--fix` mode in this
  ticket; keeping it read-only makes it always safe to run, including for AI agents).
- Doctor does not replace `check-install.sh`. The gate stays as-is; doctor is the diagnostic
  superset. No behavior change to any existing script except additive `common.sh` reuse.
- No CI wiring (bats runs stay manual, consistent with the repo's current state).

## 2. Deliverables

1. `scripts/doctor.sh` - the doctor (unprivileged, read-only, no `set -e`).
2. `tests/doctor.bats` + `load_doctor_functions` in `tests/test_helper.bash`.
3. `voipbin doctor` CLI command (5 registration points in `scripts/voipbin-cli.py`).
4. Documentation: CLAUDE.md result-line table row + doctor subsection, README.md
   (unprivileged flow + Quick Diagnostics), `tests/README.md` suite entry.

## 3. Output contract

Follows the ticket and the existing `VOIPBIN_*` grammar family exactly.

Per-check line (machine-parseable, one per check):

```
DOCTOR <name>: pass|fail|warn|skip <detail>
```

Per-fail prescription line, printed immediately after its DOCTOR fail line:

```
FIX <name>: <exact command or action>
```

`warn` may also carry a FIX line when a remedy exists (e.g. near-expiry cert).

Human summary at the end: a short banner, counts, and the collected prescriptions in order.
The summary re-lists prescriptions as *indented, non-FIX-prefixed* lines (`  [<name>] <command>`)
so that `grep '^FIX '` yields each prescription exactly once, in check order - that grep is
the documented agent contract for extracting the recovery script.

Final machine-parseable line (last line of output, same shape family as `VOIPBIN_CHECK:`):

```
VOIPBIN_DOCTOR: status=pass|fail passed=N failed=M warned=K mode=<internal|external|unknown>
```

Early-abort shape (only for truly unrecoverable runs, e.g. `common.sh` missing):
`VOIPBIN_DOCTOR: status=error reason="..."` - mirrors the documented failure shape of the
other scripts.

Exit codes: `0` only when `failed=0` (warns and skips do not fail the run); `1` when any check
failed; `2` for environment errors (cannot even start). This matches the documented 0/1/2 rule.

Counter semantics (identical to check-install.sh): `pass` and `fail` increment their counters,
`skip` increments nothing; doctor adds `warn` with its own counter, which never affects the
exit code.

Rationale for `FIX` as its own grammar rather than appending to the DOCTOR detail: prescriptions
are the product here. A dedicated prefix keeps DOCTOR lines short and lets an agent do
`grep '^FIX '` to get the ordered recovery script. check-install.sh keeps its inline
"— run: ..." style; the two contracts stay independent.

## 4. Stage awareness

Doctor auto-detects the stage and skips what cannot be probed, instead of aborting:

| Stage | Detection | Behavior |
|---|---|---|
| pre-install | no `.env` | run prerequisites; `DOCTOR env-file: fail no .env` + `FIX env-file: ./scripts/init.sh --yes`; all install-state/runtime checks after the env checks emit `skip no .env` |
| pre-start | `.env` exists, compose has no running containers | prerequisites + install-state run; runtime checks emit `skip stack not running — run ./scripts/start.sh` |
| running | running containers exist | everything runs |

`mode=unknown` in the final line only in pre-install stage; otherwise `get_domain_mode` decides.

Docker-availability gating: when the `docker` prerequisite check fails (binary missing or
daemon unreachable), it sets a `DOCTOR_DOCKER_OK=false` flag and every docker-dependent
check afterwards (compose-version daemon probe, compose-network, the coredns clause of
dns-internal, all of category 3) emits `skip docker unavailable` instead of cascade-failing.

OS scope: doctor targets Linux. On macOS (`detect_os`), the Linux-only probes emit
`skip unsupported on macos` instead of failing: `ports` (`ss`), `voip-interfaces`
(`ip link`, macvlan is Linux-only anyway), and `proxy-netns` (GNU `date -d`). Everything
else runs on both, with one known parity caveat: `dns-internal` probes resolv.conf, while
macOS internal-mode installs use `/etc/resolver/voipbin.test` - this is exact parity with
check-install.sh's existing `check_resolv_conf` behavior, deliberately not improved here.

## 5. Check inventory

Order below is output order. Names are the `DOCTOR <name>` keys. All docker compose
invocations follow the identical-environment rule from check-install.sh:
`cd "$PROJECT_DIR" && COMPOSE_PROFILES="$DOCTOR_PROFILES" docker compose ...`.

### Category 1 - prerequisites (always run, no `.env` needed)

| name | probe | fail/warn condition | prescription (FIX line) |
|---|---|---|---|
| `docker` | `command -v docker`; `docker info` | missing → fail; daemon unreachable → fail | install docker / `sudo systemctl start docker` (linux) |
| `docker-group` | `id -nG` contains `docker`, or `docker info` already succeeded unprivileged | not in group and daemon only reachable via sudo → warn | `sudo usermod -aG docker $USER` + re-login |
| `compose-version` | `docker compose version --short` | absent → fail; `< 2.24.4` → fail (merge-tag support the test override relies on) | upgrade docker compose to >= 2.24.4 |
| `tools` | `command -v` for `openssl`, `curl`, `dig`, `mkcert` | openssl/curl missing → fail; dig missing → warn (dns checks will skip); mkcert missing → fail only when mode=internal and TLS_MODE=mkcert, else skip mention | distro install commands; mkcert: `sudo ./scripts/setup-host.sh` (installs/uses mkcert path) |
| `ports` | `ss -tuln` scan of 53, 5060/5061/5066, 8443, 3003-3005, **bind-address-aware** (see policy below); best-effort process attribution via `ss -tulnp` (works unprivileged for own processes; otherwise name unknown) | a port+address our stack needs is bound by a process that is not part of our stack → fail; bound but attribution unavailable and our stack is down → fail (something else holds it); our own stack holding them while running → pass | `sudo lsof -i :<port>` to identify, then stop the conflicting service |
| `disk-space` | `df -P` on docker root dir (`docker info -f '{{.DockerRootDir}}'`, fallback `/var/lib/docker`, fallback `$PROJECT_DIR`) | free < 3 GiB → fail; < 15 GiB → warn | free disk space / `docker system prune` |

Port check policy detail - bind-address semantics (pinned here, not left to implementation):

- Port 53: coredns publishes only `127.0.0.1:53` and `${HOST_EXTERNAL_IP}:53`. The check
  compares against those two addresses only. The systemd-resolved stub listeners
  (`127.0.0.53:53`, `127.0.0.54:53`) are explicitly whitelisted; a wildcard `0.0.0.0:53`
  bind by a foreign process is a conflict. Without this, every stock Ubuntu host false-fails.
  External mode deploys no coredns, so port 53 is not ours to claim there: the doctor
  exempts port 53 from the conflict scan entirely in external mode.
- SIP ports 5060/5061/5066: kamailio runs `network_mode: host` and binds specific IPs
  (internal LB addresses / KAMAILIO_EXTERNAL_IP). A foreign listener on the wildcard address
  or on the addresses kamailio needs is a conflict; unrelated addresses are ignored.
- Compose-mapped ports (8443, 3003-3005): conflict only for the published bind address
  (`0.0.0.0`) when held by a non-stack process.

Attribution strategy: when the stack is running, ports held by our own containers
(docker-proxy for compose-mapped ports, kamailio/rtpengine on host network) are a pass -
for compose-mapped ports ask `docker compose ps` first; for host-network SIP ports check
whether kamailio/rtpengine services are running. Only unattributable binds on needed
addresses fail. In pre-install stage every non-whitelisted bind on a needed address is a
fail (name the process when `ss -tulnp` reveals it). Pre-install (no `.env`), the
needed-address set for port 53 is `127.0.0.1` plus `detect_current_host_ip` from common.sh
(there is no `HOST_EXTERNAL_IP` source yet).

### Category 2 - install state (needs `.env`; otherwise each emits `skip`)

| name | probe | prescription |
|---|---|---|
| `env-file` | `.env` exists. Exception to this category's skip rule: this check itself emits `fail` (not skip) in the pre-install stage - it is the check that *defines* that stage | `./scripts/init.sh --yes` (internal) - the external variant is referenced in the summary text |
| `domain-mode` | `DOMAIN_MODE` valid (`internal`/`external`); `COMPOSE_PROFILES` key present (absent → stale pre-dual-mode `.env`) | `stale .env, re-run ./scripts/init.sh --yes` (exact common.sh wording) |
| `compose-profiles` | `check_compose_profiles_conflict` from common.sh | its `COMPOSE_PROFILES_CONFLICT_REASON` verbatim (`unset COMPOSE_PROFILES` / stale .env) |
| `dns-internal` (internal only) | resolv.conf has an *active* `nameserver 127.0.0.1` line (anchored regex, commented lines don't count); coredns container up (`docker compose ps coredns`); `dig @127.0.0.1 api.<d>` and `sip.<d>` match `HOST_EXTERNAL_IP` / `KAMAILIO_EXTERNAL_IP` | `sudo ./scripts/setup-host.sh` (resolv.conf / dns); `docker compose up -d coredns` when only the container is down |
| `dns-external` (external only) | every runbook record resolved via system resolver and diffed against `.env`: `api`, `admin`, `meet`, `talk` → `HOST_EXTERNAL_IP`; `sip`, `sip-service`, `conference`, `trunk`, `pstn`, `registrar`, and a wildcard probe `doctor-probe.registrar` → `KAMAILIO_EXTERNAL_IP`. Per-record diff output: one indented line per mismatch/missing record `<fqdn> expected <ip> got <ip|NXDOMAIN>`; single DOCTOR line fails with the mismatch count | `see the DNS runbook in README.md` + `sudo ./voipbin dns` (prints the table with real values) |
| `certs` | `certs/api/cert.pem` + `privkey.pem` exist; not expired (`-checkend 0` → fail) and not near expiry (`-checkend 1209600`, 14 days in seconds - `-checkend` takes seconds → warn) - these clauses run for every TLS_MODE. SAN and issuer clauses are **TLS_MODE-gated** (the check-install `cert-trust` skip precedent): `TLS_MODE=selfsigned` → both emit no fail (the init.sh selfsigned fallback generates a CN-only cert with no subjectAltName at all; the check notes this in the detail and skips those clauses); mkcert (internal) → SAN covers `api.<d>` with **wildcard-aware matching** (mkcert certs carry `voipbin.test` + `*.voipbin.test`, never a literal `api.voipbin.test`; a SAN entry `*.<d>` satisfies any single-label name under `<d>`) and issuer matches mkcert CAROOT rootCA subject; external/byo → SAN covers `api.<d>` (wildcard-aware) and issuer != subject (not self-signed) | internal: `sudo ./scripts/setup-host.sh` / re-run `./scripts/init.sh`; external: `./scripts/install-certs.sh <fullchain.pem> <privkey.pem>` / `renew and re-run ./scripts/install-certs.sh` |
| `certs-env-sync` | `API_SSL_CERT_BASE64`/`API_SSL_PRIVKEY_BASE64`/`HOOK_SSL_CERT_BASE64`/`HOOK_SSL_PRIVKEY_BASE64` in `.env` match `base64 -w0` of `certs/api/{cert,privkey}.pem` | `./scripts/install-certs.sh ...` (external) / `./scripts/init.sh` (internal) - install-certs rewrites the four vars |
| `voip-interfaces` | `ip link show kamailio-int` and `rtpengine-int` (same probe as start.sh `check_voip_interfaces`) | `sudo ./scripts/setup-voip-network.sh` |
| `compose-network` | `docker network inspect` of `<project>_default` exists with label `com.docker.compose.project` | `sudo ./scripts/setup-host.sh` (its `step_ensure_docker_network` owns network creation with compose-compatible labels per CLAUDE.md) |

### Category 3 - runtime pathology (needs running stack; otherwise `skip`)

| name | probe | prescription |
|---|---|---|
| `containers` | `docker compose ps -a --format '{{.Name}}\t{{.State}}\t{{.Status}}'` - the `-a` is mandatory: since compose v2.21, plain `ps` hides exited containers, which are this check's primary target (stage detection elsewhere keeps the running-only default deliberately); group `restarting` / `unhealthy` / `exited` services; for each, best-effort last error line: `docker logs --tail 20 <name> 2>&1 \| grep -iE 'error\|fatal\|panic' \| tail -1` (truncated to one line) | `docker compose logs --tail 50 <service>` (the `_verify_stack` wording) then `docker compose restart <service>` |
| `rabbitmq-plugin` | `docker compose exec -T rabbitmq rabbitmq-plugins list -e 2>/dev/null \| grep -q delayed_message` (mirrors t_infra T4b) | `docker compose restart rabbitmq` and check `docker compose logs rabbitmq` for the plugin download failure (VOIP-1278 failure mode: first boot needs network to fetch the .ez) |
| `queue-consumers` | `docker compose exec -T rabbitmq rabbitmqctl list_queues name consumers --no-table-headers`; required: `asterisk.call.request`, `asterisk.conference.request`, `asterisk.registrar.request` each with `>0` consumers; additionally every discovered `asterisk.*.request` queue with 0 consumers is reported. Zero consumers while the matching `asterisk-<target>-proxy` service is Up = the VOIP-1279 silent-death signature | `docker compose restart asterisk-call-proxy asterisk-conference-proxy asterisk-registrar-proxy` (restarting the proxy alone is safe; the namespace hazard is owner-alone restarts) |
| `proxy-netns` | for each pair in {call, conference, registrar}: (a) proxy's `docker inspect -f '{{.HostConfig.NetworkMode}}'` yields `container:<owner-id>`; strip the prefix and compare against the current `docker compose ps -q <owner-service>` id - mismatch = owner was *recreated* under the proxy; (b) compare `.State.StartedAt`: the owner starting *after* the proxy = owner-alone restart, proxy attached to the orphaned old netns (compare as epoch seconds via `date -d ... +%s`, never lexically - RFC3339Nano formatting varies). Either condition = fail. Ids always resolved via `docker compose ps -q <service>`, never by name (the proxies have no container_name). Note: `NetworkSettings.SandboxKey` is empty on `network_mode: service:` containers and must NOT be used (empirically verified) | `sudo ./voipbin restart asterisk-<target>` (pair-ordered restart) |
| `database` | container id via `docker compose ps -q db`; `mysqladmin ping` inside the container using the container's own env creds (the migrate.sh MYSQL_IN_DB idiom, password never on host argv); then `bin_manager` schema table count > 0 and `alembic_version` non-empty in both `bin_manager` and `asterisk` | db down: `docker compose up -d db`; empty/no-migrations: `./scripts/migrate.sh` (invoked by start.sh) or `./scripts/start.sh` |
| `realm` | same probe as check-install `check_realm`: `docker inspect voipbin-registrar-mgr` env `DOMAIN_NAME_EXTENSION` vs `.env` | `docker compose rm -fsv registrar-manager && docker compose up -d registrar-manager` (exact check-install wording) |
| `test-data` | marker `.test_data_initialized` vs DB reality. The probe is deliberately the schema-level `information_schema.TABLES` count for `bin_manager` (the `check_database_initialized` probe), NOT a data-row query: marker present + zero schema tables → fail (marker lies, the volume-wipe-with-stale-marker case); marker absent + schema tables present → warn (informational only). Marker present + schema present + zero customer rows is explicitly out of scope | `rm .test_data_initialized` then `./scripts/start.sh` re-seeds; warn case has no FIX |

### Category 4 - prescriptions

Every FIX line reuses the exact wording already present in check-install.sh, common.sh,
README.md, CLAUDE.md, or `_verify_stack` (inventoried during exploration). Where a
domain-in-state reset is the remedy the wording is always the combined
`./scripts/clean.sh --volumes --purge` form. New wording is introduced only where no
documented precedent exists (ports, disk-space, docker-group), and README's troubleshooting
section gains the same wording so docs and doctor stay consistent.

## 6. Script structure

`scripts/doctor.sh` mirrors check-install.sh conventions exactly:

- No `set -e` (same rationale comment: probes are expected to fail on a broken install).
- Same override-friendly path block with blast-radius comments: `SCRIPT_DIR`, `PROJECT_DIR`,
  `ENV_FILE`, `CERTS_DIR`, `RESOLV_CONF` (+ `DOCTOR_DISK_MIN_GB` / `DOCTOR_DISK_WARN_GB`
  for test override of thresholds).
- `source "$SCRIPT_DIR/common.sh"` appears exactly once, in the guarded single-line form
  shown below (the bats loader sed anchor substitutes the source substring, and its leading
  `#` comments out the whole line including the guard suffix).
- Bookkeeping: `CHECKS_PASSED` / `CHECKS_FAILED` / `CHECKS_WARNED`, a `PRESCRIPTIONS` array,
  and two emitters:
  - `doctor_result <name> <pass|fail|warn|skip> [detail...]` → `DOCTOR` line + counters
  - `doctor_fix <name> <command...>` → `FIX` line + append to `PRESCRIPTIONS`
- `load_doctor_env` mirroring `load_check_env` (`DOCTOR_MODE` via `get_domain_mode`,
  `DOCTOR_TLS_MODE`, `DOCTOR_BASE_DOMAIN`, `DOCTOR_HOST_IP`, `DOCTOR_KAMAILIO_IP`,
  `DOCTOR_PROFILES`, ...). Stage detection helper `detect_stage` → `preinstall|prestart|running`.
- One `check_<name>()` function per row above, invoked as a flat ordered list in `main()`.
- `main()` prints the banner, runs categories 1→3, prints the summary (counts + collected
  `PRESCRIPTIONS`), emits the final `VOIPBIN_DOCTOR:` line, exits 0/1. The only exit-2 path
  is the explicit source guard before any helper exists:
  `source "$SCRIPT_DIR/common.sh" || { echo 'VOIPBIN_DOCTOR: status=error reason="common.sh missing"'; exit 2; }`.
- Last line is exactly `main "$@"` (bats loader contract).
- No changes to `common.sh` required by this design; if implementation finds a probe both
  check-install.sh and doctor.sh need, it moves to common.sh following the
  `check_compose_profiles_conflict` precedent (reason-global + caller emits result line).

Estimated size: ~500-600 lines. Within the 800-line file guideline; if it overflows during
implementation, category groups split into sourced `scripts/doctor.d/*.sh` is the escape
hatch (not the default, to keep the single-file convention of the suite).

## 7. CLI integration

`voipbin doctor` follows the existing shell-script wrapper pattern (the `cmd_init` /
`dns_setup` precedent): existence guard on `scripts/doctor.sh` under the configured
project dir, then shell out to it with inherited stdio. One deliberate deviation from the
precedent: the wrapper must propagate doctor.sh's exit code to the CLI process exit status
(existing wrappers discard it), because the 0/1 exit code is part of the machine contract
for agents scripting `sudo ./voipbin doctor`. Mechanism, pinned: `cmd_doctor` runs the script via `subprocess.call` (a real exit code;
the `cmd_init` precedent's shell-out returns a wait status, not an exit code, and must not
be propagated raw), stores it on the instance (e.g. `self.last_doctor_rc`), and only the
*non-interactive* dispatch path in `main()` exits with it; a bare `sys.exit` inside the
command would kill the interactive REPL when a user types `doctor` at the prompt, so the
REPL path just returns to the prompt.

Registration in all five places: `self.commands["doctor"]`, `self.help_text["doctor"]`,
`cmd_help` block (in the Service Commands block next to `status`), `show_cli_usage()`, completer
(no subcommands → automatic first-word completion suffices). The known `cmd_hook`
dict-registration gap is the anti-pattern to avoid.

The CLI runs under sudo (its own `check_root`); doctor.sh itself stays unprivileged-capable
when invoked directly (`./scripts/doctor.sh`), which is the documented agent path, while
`sudo ./voipbin doctor` is the human convenience path. Running under root skews two
user-context probes, so doctor.sh must handle `EUID=0` explicitly (the setup-host.sh
`SUDO_USER` precedent):

- `certs` issuer check: mkcert CAROOT is resolved for the *invoking user* - when `EUID=0`
  and `SUDO_USER` is set, run `mkcert -CAROOT` as `SUDO_USER`
  (`sudo -u "$SUDO_USER" mkcert -CAROOT`); root's own CAROOT
  (`/root/.local/share/mkcert`) would false-fail every healthy mkcert install.
- `docker-group`: when `EUID=0`, evaluate group membership for `SUDO_USER` (`id -nG
  "$SUDO_USER"`); if `SUDO_USER` is unset (real root login), emit `skip running as root`.
- `certs` issuer clause mirrors the same rule: `EUID=0` with `SUDO_USER` unset → skip the
  issuer clause only (presence/expiry/SAN are user-independent and still run).

## 8. Testing

`tests/doctor.bats`, cloned from the check-install.bats template:

- Full isolation: docker / dig / curl / mkcert / ss / ip / df stubbed via `MOCK_BIN_DIR`
  PATH stubs; `PROJECT_DIR=$TEST_TEMP_DIR`; `RESOLV_CONF` fixture override; real openssl for
  cert fixtures (fall-through stub pattern where needed).
- `load_doctor_functions` added to `test_helper.bash` (standard sed shape; doctor has no
  `set -e` so no set-e strip, mirroring the check-install loader comment).
- `run_doctor()` wrapper with `env -u COMPOSE_PROFILES` + fixture paths.
- Scenario coverage (acceptance criteria mapped 1:1):
  1. healthy internal-mode install (all stubs healthy) → every check pass, exit 0, final
     line matches `^VOIPBIN_DOCTOR: status=pass passed=[0-9]+ failed=0 warned=0 mode=internal$`.
  2. stopped docker daemon (docker stub exits 1 on `info`) → `DOCTOR docker: fail` +
     FIX line, and dependent checks skip rather than cascade-fail.
  3. commented-out resolv.conf nameserver line → `DOCTOR dns-internal: fail` + setup-host FIX.
  4. expired cert fixture → `DOCTOR certs: fail`; near-expiry (real openssl `-days 5`) →
     `warn`. Expired fixture strategy: `openssl req -x509 -not_after` when the local
     openssl supports it (capability-probed via `openssl req -help 2>&1 | grep -q not_after`;
     the option landed in OpenSSL 3.4, so never version-gate it), else a PATH-stubbed
     `openssl` that fails `-checkend` for that one test.
  5. disabled rabbitmq plugin (docker stub: `rabbitmq-plugins list -e` without
     delayed_message) → `DOCTOR rabbitmq-plugin: fail` + restart FIX.
  6. zero-consumer queue with Up proxy (docker stub: ps says running,
     `rabbitmqctl list_queues` says `asterisk.call.request 0`) → fail + the exact
     `docker compose restart asterisk-call-proxy ...` FIX (VOIP-1279 signature test).
  7. external mode: dig stub returns wrong IP for two records and NXDOMAIN for one →
     `DOCTOR dns-external: fail` and the per-record diff lists every missing/mismatched
     record (assert all three lines).
  7b. healthy external-mode install (external stubs all healthy, the check-install.bats
     "external all-pass" precedent) → every check pass, exit 0, final line
     `mode=external`.
  8. pre-install stage (no .env) → prerequisites still run, env-file fail + init FIX,
     install-state/runtime checks skip, exit 1.
  9. grammar tests: every emitted line matches the DOCTOR/FIX grammar; final-line regex;
     skip increments nothing; warn does not affect exit code.
  10. identical-environment rule: compose_env.log `sort -u` assertion (same as
      check-install's).
  11. CLI wiring (the `tests/test_cli_mode.py` precedent): `doctor` registered in
      `cli.commands` (guards against the `cmd_hook` gap), and non-interactive dispatch
      propagates the exit code while the REPL path does not exit.
- Prescription strings asserted as substrings (the suite's load-bearing convention).
- `tests/README.md` gains the doctor.bats entry (and while touching it, the stale suite
  list note is corrected only for the doctor addition - full inventory refresh is out of
  scope).

## 9. Documentation

- CLAUDE.md "Install Modes and AI-Install Contract": add `doctor.sh` row to the result-line
  table (`VOIPBIN_DOCTOR:` / `status=pass|fail passed=N failed=M warned=K mode=<m>`), a short
  subsection describing DOCTOR/FIX grammar and the run-at-any-stage property, and mention as
  the optional diagnostic step in the 4-command flow ("if anything fails, run
  ./scripts/doctor.sh").
- README.md: Quick Diagnostics gains `./scripts/doctor.sh` (or `sudo ./voipbin doctor`) as
  the first thing to run; unprivileged flow section mentions it as the recovery entry point.
- Both keep the existing wording style; no em dashes in new prose.

## 10. Risks / open points

1. Port attribution without root is best-effort; the design accepts "bound but unknown
   process" as fail-with-generic-prescription in pre-install stage. Documented in the check
   detail.
2. `rabbitmqctl list_queues` output format differences across rabbitmq versions: pinned
   image `rabbitmq:3-management-alpine` keeps this stable; `--no-table-headers` guarded with
   a fallback parse (skip header lines).
3. The wildcard DNS probe name `doctor-probe.registrar.<d>` is synthetic; DNS providers
   that answer wildcards differently per-label could false-negative. Acceptable: the runbook
   requires `*.registrar`, and a synthetic label is the only way to test a wildcard.
4. `docker logs --tail 20` on 48 services could be slow if many are broken; bounded by only
   querying the (usually few) not-running services.
5. Hosts whose openssl lacks `req -not_after` cannot generate the expired fixture; the
   bats test falls back to a stubbed openssl for that single scenario (documented in the
   test file).
