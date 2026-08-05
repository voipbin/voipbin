# Sandbox as the Main Installer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `voipbin/sandbox`'s Docker Compose installer into `voipbin/voipbin` as
`self-install/`, hardened enough (no default credentials, no personal paths, working
project-name derivation) to be documented as VoIPBin's primary, production-capable self-install
method — while `voipbin/install` (GCP/K8s) stays available as a secondary option.

**Architecture:** Two work surfaces. (1) `voipbin/sandbox` — apply the credential-hardening and
project-name-derivation code fixes there first, so the artifact copied into `voipbin/voipbin` is
already correct; this repo keeps existing separately per the design doc's "fate deferred"
decision. (2) `voipbin/voipbin` — copy the hardened sandbox tree into `self-install/`, rewrite the
root README (Option A/B), rewrite `self-install/README.md`'s security section, move the CI
workflow, and document backup/version-pin capabilities.

**Tech Stack:** Bash (sandbox scripts), Python 3 (`voipbin-cli.py`), Docker Compose, `bats` (shell
test framework, existing suite), `pytest` (existing live-stack test suite), Markdown (README).

**Design doc:** `docs/superpowers/specs/2026-08-05-sandbox-as-main-installer-design.md`
(Revision 12, 2 consecutive design-review approvals + CEO/CTO business sign-off — see that doc for
full rationale and the code line numbers each finding is based on).

---

## Repo layout for this plan

Two working directories, both git worktrees per org convention:

- `~/gitvoipbin/sandbox-worktrees/NOJIRA-Credential-hardening-and-project-name-fix/` — new worktree
  off `voipbin/sandbox`, created in Task 1.
- `~/gitvoipbin/voipbin-worktrees/NOJIRA-Integrate-sandbox-as-main-installer/` — already exists
  (created during design phase), branch `NOJIRA-Integrate-sandbox-as-main-installer`, holds the
  design doc and will hold the README rewrites + `self-install/` copy.

This produces **two separate PRs** (one per repo, per org convention for cross-repo changes) plus
a third, smaller PR in `voipbin/install` for the README banner (Task 13) — noted explicitly to the
user before opening any of them, since the default is one PR per task/session and this is the
structurally-necessary exception the org's rules call out.

---

## Task 1: Create the sandbox worktree and confirm current state

**Files:** none yet — setup only.

- [ ] **Step 1: Create the worktree**

```bash
cd ~/gitvoipbin/sandbox
git fetch origin main
mkdir -p ~/gitvoipbin/sandbox-worktrees
git worktree add ~/gitvoipbin/sandbox-worktrees/NOJIRA-Credential-hardening-and-project-name-fix \
  -b NOJIRA-Credential-hardening-and-project-name-fix origin/main
```

Expected: `Preparing worktree ... HEAD is now at <sha>`.

- [ ] **Step 2: Confirm the bats suite passes before touching anything (baseline)**

```bash
cd ~/gitvoipbin/sandbox-worktrees/NOJIRA-Credential-hardening-and-project-name-fix
bats tests/*.bats
```

Expected: all tests pass (this is the regression baseline every later step in this repo must not
break). If any test fails here, stop and report — it means `main` is already red, which changes
the plan (fix that first, separately, before proceeding).

---

## Task 2: Add hardened credential variables to `.env.template` and `init.sh` generation

**Files:**
- Modify: `.env.template`
- Modify: `scripts/init.sh:584` (near `JWT_KEY=$(generate_random_key)`) and the `.env` heredoc
  section around line 737 (`JWT_KEY=$JWT_KEY`)
- Modify: `scripts/init_no_sudo.sh` (same `.env`-generation responsibility as `init.sh` — check
  for an equivalent JWT_KEY-generation block and heredoc, mirror the same additions there)

- [ ] **Step 1: Add the new generated variables next to `JWT_KEY` in `init.sh`**

Find (around line 584):
```bash
JWT_KEY=$(generate_random_key)
log_info "  Generated JWT_KEY"
```

Replace with:
```bash
JWT_KEY=$(generate_random_key)
log_info "  Generated JWT_KEY"

MYSQL_ROOT_PASSWORD=$(generate_random_key)
log_info "  Generated MYSQL_ROOT_PASSWORD"

RABBITMQ_DEFAULT_USER="voipbin"
RABBITMQ_DEFAULT_PASS=$(generate_random_key)
log_info "  Generated RABBITMQ_DEFAULT_PASS"

AMI_USERNAME="voipbin"
AMI_PASSWORD=$(generate_random_key)
log_info "  Generated AMI_PASSWORD"
```

- [ ] **Step 2: Write the new variables into the generated `.env` file**

Find (around line 737, in the `.env` heredoc, "Security & Storage" section):
```
JWT_KEY=$JWT_KEY
EMAIL_VERIFY_BASE_URL=$DERIVED_EMAIL_VERIFY_BASE_URL
```

Replace with:
```
JWT_KEY=$JWT_KEY
MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD
RABBITMQ_DEFAULT_USER=$RABBITMQ_DEFAULT_USER
RABBITMQ_DEFAULT_PASS=$RABBITMQ_DEFAULT_PASS
AMI_USERNAME=$AMI_USERNAME
AMI_PASSWORD=$AMI_PASSWORD
EMAIL_VERIFY_BASE_URL=$DERIVED_EMAIL_VERIFY_BASE_URL
```

- [ ] **Step 3: Mirror both changes in `init_no_sudo.sh`**

Read `scripts/init_no_sudo.sh` in full first (it's the unprivileged variant of `init.sh` and
round-2/round-6 review confirmed it independently generates `.env`/certs — do not assume it
`source`s `init.sh`). Apply the same two additions (key generation + heredoc lines) at the
equivalent points. If its `.env` heredoc structure differs from `init.sh`'s, match its existing
section layout rather than copy-pasting verbatim.

- [ ] **Step 4: Add the same keys to `.env.template`, with placeholder comments, not values**

Find in `.env.template` (around the existing `JWT_KEY=your-random-jwt-secret-key` line — **keep
this key line**, per the design doc's finding that removing it registers as
`.env`/`.env.template` drift):

```
JWT_KEY=your-random-jwt-secret-key
```

Replace with:
```
JWT_KEY=your-random-jwt-secret-key
MYSQL_ROOT_PASSWORD=your-random-mysql-root-password
RABBITMQ_DEFAULT_USER=voipbin
RABBITMQ_DEFAULT_PASS=your-random-rabbitmq-password
AMI_USERNAME=voipbin
AMI_PASSWORD=your-random-ami-password
```

- [ ] **Step 5: Run the drift checker manually (it's not wired into CI — see design doc)**

```bash
./scripts/check-env-template-sync.sh
```

Expected: no drift reported for the six new keys. If it reports drift, read its output — it
checks both directions (`.env.template` keys missing from what `init.sh`/`init_no_sudo.sh` write,
and vice versa) — and fix whichever side is missing a key.

- [ ] **Step 6: Commit**

```bash
git add .env.template scripts/init.sh scripts/init_no_sudo.sh
git commit -m "Generate random MySQL/RabbitMQ/AMI credentials on init

- sandbox: Add MYSQL_ROOT_PASSWORD, RABBITMQ_DEFAULT_USER/PASS, AMI_USERNAME/PASSWORD generation to init.sh and init_no_sudo.sh, matching the existing JWT_KEY pattern"
```

---

## Task 3: Parameterize `docker-compose.yml`'s hardcoded credentials

**Files:**
- Modify: `docker-compose.yml` (compose service definitions — MySQL, RabbitMQ, Asterisk/AMI
  services, and every service's DSN/AMQP env var)

- [ ] **Step 1: Replace the MySQL root password service definition**

Find:
```yaml
    environment:
      - MYSQL_ROOT_PASSWORD=root_password
```
Replace with:
```yaml
    environment:
      - MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
```

Also update the MySQL healthcheck in the same service block. Find:
```yaml
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-uroot", "-proot_password"]
```
Replace with:
```yaml
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-uroot", "-p${MYSQL_ROOT_PASSWORD}"]
```

- [ ] **Step 2: Replace every service's MySQL DSN**

Every backend service's `environment:` block has a line shaped like:
```yaml
      - DATABASE_DSN=root:root_password@tcp(${DB_HOST:-db}:${DB_PORT:-3306})/bin_manager
```
(the DB name after the final `/` varies per service — `bin_manager`, `asterisk`, etc. — keep that
part unchanged). Run a scoped substitution across the file rather than editing each service by
hand — this pattern is mechanical and repeats ~25+ times, one per backend service:

```bash
sed -i 's/root:root_password@tcp/root:${MYSQL_ROOT_PASSWORD}@tcp/g' docker-compose.yml
```

- [ ] **Step 3: Replace RabbitMQ credentials**

Find:
```yaml
      - RABBITMQ_DEFAULT_USER=guest
      - RABBITMQ_DEFAULT_PASS=guest
```
Replace with:
```yaml
      - RABBITMQ_DEFAULT_USER=${RABBITMQ_DEFAULT_USER}
      - RABBITMQ_DEFAULT_PASS=${RABBITMQ_DEFAULT_PASS}
```

Then replace every service's AMQP URL:
```bash
sed -i 's/amqp:\/\/guest:guest@/amqp:\/\/${RABBITMQ_DEFAULT_USER}:${RABBITMQ_DEFAULT_PASS}@/g' docker-compose.yml
```

- [ ] **Step 4: Replace Asterisk AMI credentials**

Find each occurrence (there are 3, per the design doc's earlier line-number audit — asterisk-call,
asterisk-conference, and their proxy services) of:
```yaml
      - AMI_PASSWORD=asterisk
      - AMI_USERNAME=asterisk
```
Replace with:
```yaml
      - AMI_PASSWORD=${AMI_PASSWORD}
      - AMI_USERNAME=${AMI_USERNAME}
```
And any `ARI_ACCOUNT=asterisk:asterisk`-shaped line:
```bash
sed -i 's/ARI_ACCOUNT=asterisk:asterisk/ARI_ACCOUNT=${AMI_USERNAME}:${AMI_PASSWORD}/g' docker-compose.yml
```

- [ ] **Step 5: Verify no literal credentials remain**

```bash
grep -n "root_password\|guest:guest\|RABBITMQ_DEFAULT_USER=guest\|AMI_PASSWORD=asterisk\|ARI_ACCOUNT=asterisk:asterisk" docker-compose.yml
```

Expected: **no output**. If anything prints, it's a missed occurrence — fix it before continuing.

- [ ] **Step 6: Validate the compose file parses**

```bash
export MYSQL_ROOT_PASSWORD=test RABBITMQ_DEFAULT_USER=test RABBITMQ_DEFAULT_PASS=test AMI_USERNAME=test AMI_PASSWORD=test
docker compose config --quiet
```

Expected: no error output (empty on success). This does not start containers, only validates
YAML + interpolation.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml
git commit -m "Parameterize MySQL/RabbitMQ/AMI credentials in docker-compose.yml

- sandbox: Replace hardcoded root_password/guest:guest/asterisk literals with env var interpolation across all services"
```

---

## Task 4: Update consumer scripts that read the same literals directly

**Files:**
- Modify: `scripts/start.sh:388,390,392,409,253`
- Modify: `scripts/init_database.sh:15` and its 7 call sites
- Modify: `scripts/migrate.sh:27`
- Modify: `scripts/voipbin-cli.py` (6 call sites without fallback: lines 58, 2004, 4296, 5378,
  5532, 5863 — the two that already fall back, 5418/5644, are left as-is)

- [ ] **Step 1: Fix `start.sh`'s `check_database_initialized()` and `wait_for_database()`**

Find (four occurrences of `mysql -u root -proot_password` in this region):
```bash
    tables=$(docker exec voipbin-db mysql -u root -proot_password -N -e \
        "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'bin_manager';" 2>/dev/null || echo "0")
    alembic_bin=$(docker exec voipbin-db mysql -u root -proot_password -N -e \
        "SELECT version_num FROM bin_manager.alembic_version LIMIT 1;" 2>/dev/null)
    alembic_ast=$(docker exec voipbin-db mysql -u root -proot_password -N -e \
        "SELECT version_num FROM asterisk.alembic_version LIMIT 1;" 2>/dev/null)
```
and
```bash
        if docker exec voipbin-db mysql -u root -proot_password -e "SELECT 1" &>/dev/null; then
```

Run this substitution across the whole file (safe here because `start.sh` sources `.env` via
`set -a; source "$PROJECT_DIR/.env"` before any of these functions run, so
`$MYSQL_ROOT_PASSWORD` is populated by the time they execute):

```bash
sed -i 's/-proot_password/-p"${MYSQL_ROOT_PASSWORD:-root_password}"/g' scripts/start.sh
```

- [ ] **Step 2: Fix `start.sh`'s Compose-project-name first-run heuristic (defer full fix to Task 6)**

Leave `scripts/start.sh:253`'s `grep -q 'sandbox_db_data'` untouched in this task — it's part of
the project-name centralization work, handled together with `setup-voip-network.sh` and
`voipbin-cli.py`'s equivalents in Task 6, not here. (Fixing it here in isolation would leave the
other two hardcoded spots broken and split one logical change across two commits.)

- [ ] **Step 3: Fix `init_database.sh`**

Find:
```bash
DB_ROOT_PASSWORD="root_password"
```
Replace with:
```bash
DB_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root_password}"
```

Confirm this covers all 7 call sites (they all reference the `$DB_ROOT_PASSWORD` variable, not
the literal, per the design doc's audit):
```bash
grep -n "DB_ROOT_PASSWORD" scripts/init_database.sh
```
Expected: the assignment line plus 7 usage lines, none containing the literal `root_password`
directly.

- [ ] **Step 4: Fix `migrate.sh`**

Find:
```bash
DB_PASSWORD="root_password"
```
Replace with:
```bash
DB_PASSWORD="${MYSQL_ROOT_PASSWORD:-root_password}"
```

- [ ] **Step 5: Fix `voipbin-cli.py`'s 6 unguarded call sites**

Read the file at each of these line numbers first (line numbers shift once earlier edits in this
task land — re-`grep` before each edit rather than trusting the original numbers):

```bash
grep -n "root_password" scripts/voipbin-cli.py
```

For each match that is a plain literal (not already `${MYSQL_ROOT_PASSWORD:-root_password}`),
change it to use `os.environ.get("MYSQL_ROOT_PASSWORD", "root_password")` if it's a Python
f-string/variable context, or the shell-style `${MYSQL_ROOT_PASSWORD:-root_password}` if it's a
string being handed to a subprocess/shell call — match whichever pattern the surrounding code
already uses at the two existing guarded call sites (5418, 5644 pre-edit) for consistency.

- [ ] **Step 6: Verify no unguarded literal remains outside test fixtures**

```bash
grep -rn "root_password" scripts/ --include="*.sh" --include="*.py" | grep -v "MYSQL_ROOT_PASSWORD:-root_password\|MYSQL_ROOT_PASSWORD.*root_password"
```

Expected: only matches inside `scripts/tests/` (the fixtures, handled in Task 5) or none at all.

- [ ] **Step 7: Commit**

```bash
git add scripts/start.sh scripts/init_database.sh scripts/migrate.sh scripts/voipbin-cli.py
git commit -m "Read MySQL root password from env instead of hardcoded literal

- sandbox: start.sh/init_database.sh/migrate.sh/voipbin-cli.py all read MYSQL_ROOT_PASSWORD from the environment (with the same value as a fallback default, matching doctor.sh's existing pattern) instead of the root_password literal"
```

---

## Task 5: Update test fixtures that assert the old literals

**Files:**
- Modify: `tests/config.bats:158,163,168`
- Modify: `scripts/tests/test_backup_restore_live.py:41,51`

- [ ] **Step 1: Read `tests/config.bats` around the flagged lines**

```bash
sed -n '145,175p' tests/config.bats
```

These assert that `docker-compose.yml`/`.env.template` contain the literal `root_password`
string. Update each assertion to check for the **variable reference** instead (e.g.
`grep -q 'MYSQL_ROOT_PASSWORD' docker-compose.yml` instead of
`grep -q 'root_password' docker-compose.yml`), matching whatever assertion style the surrounding
`.bats` file already uses (likely `run grep ...` + `[ "$status" -eq 0 ]`).

- [ ] **Step 2: Run the updated bats file in isolation**

```bash
bats tests/config.bats
```

Expected: all tests pass.

- [ ] **Step 3: Update `scripts/tests/test_backup_restore_live.py`**

Read lines 30–60 to see how `root_password` is used (round-3 review found it's a live-stack
fixture using `COMPOSE_PROJECT_NAME="voipbin-test"`). Update it to read
`os.environ.get("MYSQL_ROOT_PASSWORD", "root_password")` the same way, so the live test still
works against a freshly-generated `.env`.

- [ ] **Step 4: Commit**

```bash
git add tests/config.bats scripts/tests/test_backup_restore_live.py
git commit -m "Update test fixtures for parameterized MySQL credentials

- sandbox: config.bats and test_backup_restore_live.py assert on the env var reference instead of the retired root_password literal"
```

---

## Task 6: Gate `start.sh`'s test-account auto-creation behind a dev-mode flag

**Files:**
- Modify: `scripts/start.sh` (`check_test_data_initialized()` around line 421, and the
  `setup_test_customer()` call site)
- Modify: `.env.template` (document the new flag)

- [ ] **Step 1: Read the current gating logic**

```bash
sed -n '420,435p' scripts/start.sh
grep -n "setup_test_customer" scripts/start.sh
```

- [ ] **Step 2: Add an explicit dev-mode flag, defaulting off, that additionally gates seeding**

Find:
```bash
check_test_data_initialized() {
    [ -f "$PROJECT_DIR/.test_data_initialized" ]
}
```

Replace with:
```bash
# Test/dev seed data (admin@localhost account, extensions 1000/2000/3000 with
# fixed passwords) is only created when explicitly opted into via
# VOIPBIN_SANDBOX_DEV_SEED=true in .env. This is now the primary, documented
# self-install path, including production use — auto-seeding known credentials
# by default is not acceptable there. See design doc Scope item 4.
check_test_data_initialized() {
    [ -f "$PROJECT_DIR/.test_data_initialized" ]
}

dev_seed_enabled() {
    [ "${VOIPBIN_SANDBOX_DEV_SEED:-false}" = "true" ]
}
```

- [ ] **Step 3: Gate the call site**

Find the line that calls `setup_test_customer` (unconditionally, or guarded only by
`check_test_data_initialized`) and wrap it:

```bash
if dev_seed_enabled && ! check_test_data_initialized; then
    setup_test_customer
elif ! check_test_data_initialized; then
    log_info "Skipping dev seed data (VOIPBIN_SANDBOX_DEV_SEED not set to true) — no test customer/extensions created."
fi
```

(Adjust to match the exact existing conditional structure found in Step 1 — the point is
`setup_test_customer` must not run unless `dev_seed_enabled` is true, in addition to the existing
marker check.)

- [ ] **Step 4: Document the flag in `.env.template`**

Add near the other optional flags:
```
# Set to "true" to auto-create a test customer/admin account and extensions
# 1000/2000/3000 with well-known passwords, for local development only.
# Leave unset/false for any install exposed beyond localhost.
VOIPBIN_SANDBOX_DEV_SEED=false
```

- [ ] **Step 5: Run the bats suite covering `start.sh`**

```bash
bats tests/start.bats
```

Expected: passes. If `start.bats` currently asserts the test customer *is* created, that
assertion needs updating to set `VOIPBIN_SANDBOX_DEV_SEED=true` in its test fixture `.env` first
— read the test file, don't guess; update only if it currently relies on default seeding.

- [ ] **Step 6: Commit**

```bash
git add scripts/start.sh .env.template tests/start.bats
git commit -m "Gate test-account auto-seeding behind VOIPBIN_SANDBOX_DEV_SEED flag

- sandbox: start.sh no longer creates admin@localhost/pass1000-3000 test accounts unless VOIPBIN_SANDBOX_DEV_SEED=true is set, defaulting off"
```

---

## Task 7: Remove the personal hardcoded path in `init_database.sh`

**Files:**
- Modify: `scripts/init_database.sh` (around lines 138-140)

- [ ] **Step 1: Read the fallback branch**

```bash
sed -n '130,145p' scripts/init_database.sh
```

- [ ] **Step 2: Replace the personal path with a `$HOME`-derived one**

Find:
```bash
    if [ -d "/home/pchero/gitvoipbin/monorepo/bin-dbscheme-manager" ]; then
        cp -r /home/pchero/gitvoipbin/monorepo/bin-dbscheme-manager "$DBSCHEME_DIR"
```
Replace with:
```bash
    if [ -d "$HOME/gitvoipbin/monorepo/bin-dbscheme-manager" ]; then
        cp -r "$HOME/gitvoipbin/monorepo/bin-dbscheme-manager" "$DBSCHEME_DIR"
```

- [ ] **Step 3: Verify no personal path remains anywhere in the repo**

```bash
grep -rn "/home/pchero" . --include="*.sh" --include="*.py" 2>/dev/null
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add scripts/init_database.sh
git commit -m "Replace personal hardcoded path with \$HOME-derived fallback

- sandbox: init_database.sh's dev-shortcut fallback branch no longer hardcodes /home/pchero"
```

---

## Task 8: Centralize Compose project-name derivation

**Files:**
- Modify: `scripts/common.sh` (add the shared function)
- Modify: `scripts/setup-host.sh:44-54` (use the shared function instead of its own copy)
- Modify: `scripts/setup-voip-network.sh:21` (use the shared function instead of the hardcoded
  literal)
- Modify: `scripts/start.sh:253` (use the shared function's result instead of the hardcoded
  `sandbox_db_data` grep)
- Modify: `scripts/doctor.sh:107-112` (use the shared function instead of its own separate copy)
- Modify: `scripts/voipbin-cli.py:4553,4564,5050-5057` (bring `_compose_project_name()`'s
  normalization rule in line with the bash version; use it at the two hardcoded call sites)
- Test: `tests/compose-project-name.bats` (new)

- [ ] **Step 1: Write the failing test first**

Create `tests/compose-project-name.bats`:
```bash
#!/usr/bin/env bats

setup() {
    load 'test_helper'
    SCRIPT_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../scripts" && pwd)"
}

@test "derive_compose_project_name: uses COMPOSE_PROJECT_NAME env var when set" {
    source "$SCRIPT_DIR/common.sh"
    COMPOSE_PROJECT_NAME="sandbox"
    result=$(derive_compose_project_name)
    [ "$result" = "sandbox" ]
}

@test "derive_compose_project_name: derives from PROJECT_DIR basename when unset" {
    source "$SCRIPT_DIR/common.sh"
    unset COMPOSE_PROJECT_NAME
    PROJECT_DIR="/tmp/self-install"
    result=$(derive_compose_project_name)
    [ "$result" = "self-install" ]
}

@test "derive_compose_project_name: normalizes uppercase and strips leading dash" {
    source "$SCRIPT_DIR/common.sh"
    unset COMPOSE_PROJECT_NAME
    PROJECT_DIR="/tmp/-Self-Install"
    result=$(derive_compose_project_name)
    [ "$result" = "self-install" ]
}
```

- [ ] **Step 2: Run it to confirm it fails (function doesn't exist in `common.sh` yet)**

```bash
bats tests/compose-project-name.bats
```

Expected: FAIL — `derive_compose_project_name: command not found` or similar.

- [ ] **Step 3: Move `derive_compose_project_name()` into `common.sh`**

Read the current implementation in `setup-host.sh:44-54` first, then add it to `common.sh` (near
the other shared functions), keeping its exact validation/normalization behavior (strips leading
`[-_]`, validates against `^[a-z0-9][a-z0-9_-]*$`):

```bash
# =============================================================================
# Compose Project Name Derivation
# =============================================================================
# Shared by setup-host.sh, setup-voip-network.sh, start.sh, and doctor.sh so a
# renamed checkout directory (e.g. sandbox -> self-install) derives a
# consistent Compose project name everywhere, instead of some places
# hardcoding the literal "sandbox".
derive_compose_project_name() {
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        [[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || return 1
        printf '%s' "$COMPOSE_PROJECT_NAME"
        return 0
    fi
    basename "$PROJECT_DIR" \
        | tr '[:upper:]' '[:lower:]' \
        | tr -cd 'a-z0-9_-' \
        | sed 's/^[-_]*//'
}
```

- [ ] **Step 4: Remove the duplicate from `setup-host.sh`, source `common.sh` for it instead**

Delete `setup-host.sh`'s own `derive_compose_project_name()` definition (lines 44-54 in the
original). Confirm `setup-host.sh` already sources `common.sh` before this point (it does, per
the design doc's audit) — no new `source` line needed, just delete the duplicate function body.

- [ ] **Step 5: Run the new test — it should pass now**

```bash
bats tests/compose-project-name.bats
```

Expected: PASS (all 3 tests).

- [ ] **Step 6: Fix `setup-voip-network.sh`**

Find:
```bash
NETWORK_NAME="sandbox_default"
```
Replace with:
```bash
NETWORK_NAME="$(derive_compose_project_name)_default"
```
Confirm `setup-voip-network.sh` sources `common.sh` and sets `PROJECT_DIR` before this line (per
the design doc's audit, it already does at line 16/19) — if not, add
`source "$SCRIPT_DIR/common.sh"` before this line.

- [ ] **Step 7: Fix `start.sh`'s first-run heuristic**

Find:
```bash
    if docker volume ls --format '{{.Name}}' | grep -q 'sandbox_db_data'; then
```
Replace with:
```bash
    if docker volume ls --format '{{.Name}}' | grep -q "$(derive_compose_project_name)_db_data"; then
```

- [ ] **Step 8: Fold `doctor.sh`'s separate copy into the shared function**

Read `doctor.sh:107-112`'s `doctor_compose_project_name()`. Delete it, and replace its call sites
with `derive_compose_project_name()` (confirm `doctor.sh` sources `common.sh` — it does, per the
design doc's audit — and note `doctor.sh`'s version had no input validation, unlike the shared
one; this is an intentional behavior tightening, not a regression, since `doctor.sh` is read-only
and a validation failure there should surface as an error, not silently degrade).

- [ ] **Step 9: Bring `voipbin-cli.py`'s normalization in line, fix its hardcoded call sites**

Read `_compose_project_name()` at line 5050. It currently skips leading-char stripping and has a
`"voipbin"` fallback that the bash version doesn't. Update it to match the bash version's rule
(strip leading `[-_]` after lowercasing/filtering) while keeping its existing
`COMPOSE_PROJECT_NAME` env var precedence, so both implementations agree on the same input:

```python
def _compose_project_name(self):
    env_name = os.environ.get("COMPOSE_PROJECT_NAME")
    if env_name:
        if not re.match(r'^[a-z0-9][a-z0-9_-]*$', env_name):
            raise ValueError(f"Invalid COMPOSE_PROJECT_NAME: {env_name!r}")
        return env_name
    raw = os.path.basename(self.project_dir).lower()
    filtered = re.sub(r'[^a-z0-9_-]', '', raw)
    stripped = filtered.lstrip('-_')
    return stripped or "voipbin"
```

Then fix the two hardcoded lookups (originally 4553, 4564 — re-`grep` first, line numbers shift):
```bash
grep -n "sandbox_voip-internal\|sandbox_default" scripts/voipbin-cli.py
```
Replace each `"sandbox_voip-internal"` / `"sandbox_default"` literal with an f-string using
`self._compose_project_name()`, e.g. `f"{self._compose_project_name()}_voip-internal"` /
`f"{self._compose_project_name()}_default"` — match whichever variable name the surrounding method
already uses to call this helper.

- [ ] **Step 10: Add a cross-language pinning test**

Add to `scripts/tests/` (Python, since it's testing the Python helper against fixed inputs that
mirror the bats cases above):

```python
def test_compose_project_name_matches_bash_derivation():
    """Pin voipbin-cli.py's project-name rule to match common.sh's, so the two
    implementations can't silently drift apart again."""
    cli = VoipbinCLI(project_dir="/tmp/-Self-Install")
    assert cli._compose_project_name() == "self-install"

    cli2 = VoipbinCLI(project_dir="/tmp/sandbox")
    assert cli2._compose_project_name() == "sandbox"
```

(Adjust the constructor call to however `voipbin-cli.py`'s test suite already instantiates the
CLI class elsewhere in `scripts/tests/` — read an existing test file first for the pattern.)

- [ ] **Step 11: Run the full bats suite plus the new Python test**

```bash
bats tests/*.bats
python3 -m pytest scripts/tests/ -k "compose_project_name" -v
```

Expected: all pass.

- [ ] **Step 12: Verify a fresh install under a renamed directory now works end-to-end for the network step**

```bash
mkdir -p /tmp/self-install-check && cp -r . /tmp/self-install-check/ 2>/dev/null || true
cd /tmp/self-install-check
unset COMPOSE_PROJECT_NAME
bash -c 'SCRIPT_DIR="$(pwd)/scripts"; PROJECT_DIR="$(pwd)"; source scripts/common.sh; derive_compose_project_name'
```

Expected: prints `self-install-check` (or whatever the temp dir's sanitized basename is) — confirms
the derivation works from a directory that isn't literally named `sandbox`.

- [ ] **Step 13: Commit**

```bash
git add scripts/common.sh scripts/setup-host.sh scripts/setup-voip-network.sh scripts/start.sh scripts/doctor.sh scripts/voipbin-cli.py tests/compose-project-name.bats scripts/tests/
git commit -m "Centralize Compose project-name derivation

- sandbox: Move derive_compose_project_name() into common.sh, shared by setup-host.sh/setup-voip-network.sh/start.sh/doctor.sh; align voipbin-cli.py's Python implementation with the same rule and fix its hardcoded sandbox_default/sandbox_voip-internal lookups"
```

---

## Task 9: Final sandbox-repo verification and PR

**Files:** none new — verification only.

- [ ] **Step 1: Run the full bats suite**

```bash
bats tests/*.bats
```

Expected: all pass (this is the same suite from Task 1's baseline — confirm nothing regressed).

- [ ] **Step 2: Run a full fresh-install smoke test on a disposable host/VM**

This cannot run safely on a host with an existing sandbox install (per the design doc — `init`/
`setup-host.sh` make real host changes: `/etc/resolv.conf`, mkcert CA, macvlan interfaces). On a
disposable host or VM:

```bash
./scripts/init.sh --yes
sudo ./scripts/setup-host.sh
./scripts/start.sh
./scripts/check-install.sh
```

Expected: `check-install.sh` reports success, and:
```bash
grep -c "root_password\|guest:guest\|AMI_PASSWORD=asterisk" .env
```
prints `0` — i.e. the generated `.env` contains randomized values, not the old defaults.

- [ ] **Step 3: Confirm no test account was created without the dev flag**

```bash
grep VOIPBIN_SANDBOX_DEV_SEED .env  # should show =false or be absent (defaults false)
# then check no admin@localhost customer exists via the CLI or API — exact check
# depends on which is available in this environment; at minimum confirm
# .test_data_initialized was not created by this run.
ls .test_data_initialized 2>/dev/null && echo "UNEXPECTED: seed data ran" || echo "OK: no seed data"
```

Expected: `OK: no seed data`.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin NOJIRA-Credential-hardening-and-project-name-fix
gh pr create --repo voipbin/sandbox \
  --title "NOJIRA-Credential-hardening-and-project-name-fix" \
  --body "$(cat <<'EOF'
Harden default credentials and centralize Compose project-name derivation ahead of
sandbox becoming voipbin/voipbin's primary self-install method.

- sandbox: Generate random MySQL/RabbitMQ/AMI credentials on init instead of shipping root_password/guest:guest/asterisk literals
- sandbox: Gate test-account auto-seeding behind VOIPBIN_SANDBOX_DEV_SEED, defaulting off
- sandbox: Remove personal hardcoded path from init_database.sh's fallback branch
- sandbox: Centralize Compose project-name derivation in common.sh, shared across setup-host.sh/setup-voip-network.sh/start.sh/doctor.sh/voipbin-cli.py, fixing a renamed-checkout install failure
EOF
)"
```

**Do not merge this PR without the CEO/CTO's explicit go-ahead — wait for instruction before
merging, per org policy.**

---

## Task 10: Create the `self-install/` copy in `voipbin/voipbin`

**Files:**
- Create: `self-install/` (entire tree, copied from the now-hardened `voipbin/sandbox`)
- Create: `self-install/HISTORY.md`

- [ ] **Step 1: Confirm Task 9's sandbox PR has merged before starting this task**

```bash
cd ~/gitvoipbin/sandbox && git fetch origin main && git log origin/main -1 --oneline
```

Confirm the commit from Task 8 (project-name centralization) is present on `origin/main`. If not
yet merged, wait — copying pre-hardening sandbox code into `voipbin/voipbin` would ship the exact
defaults this plan exists to remove.

- [ ] **Step 2: Copy via a clean clone, not the local working directory**

```bash
cd /tmp
git clone https://github.com/voipbin/sandbox.git sandbox-clean-clone
cd ~/gitvoipbin/voipbin-worktrees/NOJIRA-Integrate-sandbox-as-main-installer
SANDBOX_COMMIT=$(cd /tmp/sandbox-clean-clone && git rev-parse HEAD)
rsync -a --exclude='.git' /tmp/sandbox-clean-clone/ self-install/
rm -rf /tmp/sandbox-clean-clone
```

This deliberately does **not** copy from `~/gitvoipbin/sandbox`, which may contain a stray
`.worktrees/` checkout not excluded by `.gitignore` (per the design doc's finding).

- [ ] **Step 3: Write `self-install/HISTORY.md`**

```markdown
# History

This directory was copied from [voipbin/sandbox](https://github.com/voipbin/sandbox) at commit
`<SANDBOX_COMMIT>`. For the full commit history of everything under this directory prior to the
move, see that repository.
```

(Fill in `<SANDBOX_COMMIT>` with the actual value captured in Step 2.)

- [ ] **Step 4: Verify no gitignored artifacts leaked in**

```bash
cat self-install/.gitignore
find self-install -maxdepth 2 -name ".worktrees" -o -name "tmp" | grep -v node_modules
```

Expected: no `.worktrees/` directory present (the clean clone never had one).

- [ ] **Step 5: Commit**

```bash
git add self-install/
git commit -m "Copy hardened voipbin/sandbox into self-install/

- voipbin: Clean-clone copy of voipbin/sandbox at <SANDBOX_COMMIT>, post credential-hardening and project-name-derivation fixes; see self-install/HISTORY.md"
```

---

## Task 11: Move the CI workflow to repo root

**Files:**
- Create: `.github/workflows/discord-merge-notify.yml` (moved from `self-install/.github/workflows/`)
- Delete: `self-install/.github/workflows/discord-merge-notify.yml`, `self-install/.github/` if
  now empty

- [ ] **Step 1: Move the file**

```bash
mkdir -p .github/workflows
git mv self-install/.github/workflows/discord-merge-notify.yml .github/workflows/discord-merge-notify.yml
rmdir self-install/.github/workflows self-install/.github 2>/dev/null || true
```

- [ ] **Step 2: Read the moved workflow and check for path-relative assumptions**

```bash
cat .github/workflows/discord-merge-notify.yml
```

If it references any path relative to the old `self-install/` location (e.g. a script it invokes),
update those paths to be relative to the new repo root. If it only reacts to push/merge events and
posts a fixed message, no path changes are needed.

- [ ] **Step 3: Note the secret provisioning requirement (cannot be done from this session)**

This workflow needs `secrets.DISCORD_WEBHOOK_URL` provisioned on `voipbin/voipbin` (currently only
set on `voipbin/sandbox`). **Flag this to the user explicitly before merging** — it requires
repository-settings access this plan can't exercise.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "Move CI workflow to repo root

- voipbin: discord-merge-notify.yml moved from self-install/.github/workflows/ to .github/workflows/ — GitHub Actions doesn't run workflows from a nested path. Requires DISCORD_WEBHOOK_URL secret provisioned on this repo before it will fire."
```

---

## Task 12: Rewrite `self-install/README.md`'s security section

**Files:**
- Modify: `self-install/README.md` (the `⚠️ SECURITY WARNING` block, originally around line 456)

- [ ] **Step 1: Find the current block**

```bash
grep -n "SECURITY WARNING" self-install/README.md
```

- [ ] **Step 2: Replace it**

Find (the block starting with `> **⚠️ SECURITY WARNING: Local Development Only**` through the
`on-premise licensing` sentence):

```
> **⚠️ SECURITY WARNING: Local Development Only**
>
> This sandbox uses **default credentials** for ease of development:
>
> | Service | Credentials |
> |---------|-------------|
> | MySQL | `root` / `root_password` |
> | RabbitMQ | `guest` / `guest` |
> | Admin Account | `admin@localhost` / `admin@localhost` |
> | Extensions | `1000` / `pass1000`, `2000` / `pass2000`, `3000` / `pass3000` |
> | JWT Secret | Auto-generated in `.env` |
>
> **DO NOT expose this sandbox to the public internet.** All ports, credentials, and secrets are meant for local development only. For production deployments, use the official VoIPBin cloud service or contact us for on-premise licensing.
```

Replace with:
```
> **🔒 Security: credentials are generated per-install, not shipped as defaults**
>
> As of this install method, `MYSQL_ROOT_PASSWORD`, `RABBITMQ_DEFAULT_PASS`, and `AMI_PASSWORD`
> are randomly generated by `init.sh`/`init_no_sudo.sh` and written to your local `.env` — they
> are not fixed, shared defaults. `JWT_KEY` has always worked this way.
>
> A dev/test seed account (`admin@localhost` / extensions `1000`/`2000`/`3000` with well-known
> passwords) is available for local development, but is **off by default**. It only gets created
> if you explicitly set `VOIPBIN_SANDBOX_DEV_SEED=true` in `.env` — never set this on an install
> reachable from the public internet.
>
> Before exposing this install beyond localhost, also review: TLS certificate mode
> (`TLS_MODE`), firewall/network exposure of the ports this stack opens, and that
> `VOIPBIN_SANDBOX_DEV_SEED` is unset or `false`.
```

- [ ] **Step 3: Verify the on-premise-licensing sentence is gone from the whole file**

```bash
grep -n "on-premise licensing" self-install/README.md
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add self-install/README.md
git commit -m "Rewrite self-install/README.md security section

- voipbin: Replace 'local dev only, default credentials, don't expose' warning with an accurate description of the now-randomized credentials and the opt-in dev-seed flag. Delete the 'contact us for on-premise licensing' sentence per CEO/CTO decision (no real license exists behind it)."
```

---

## Task 13: Rewrite the root `README.md`

**Files:**
- Modify: `README.md` at all reference points identified in the design doc: line 33 (badge), the
  Self-Install summary card (~299-312), the Self-Install Guide section (378-481), the
  Repositories table (~536, ~540), the Documentation section bullet (~548), and the Contributing
  table (~564-565).

- [ ] **Step 1: Re-locate each reference point (line numbers may have shifted since the design doc was written)**

```bash
grep -n "voipbin/install\|Self-Install\|Examples & Sandbox\|Sandbox & examples" README.md
```

- [ ] **Step 2: Fix the Self-Install summary card**

Find the card text:
```
### 🏠 Self-Install

**Full control over your infrastructure.**

Deploy VoIPBin on your own cloud infrastructure. Own your data, customize everything, and run it wherever you want.
```
Replace with:
```
### 🏠 Self-Install

**Full control over your infrastructure.**

Deploy VoIPBin on your own server or cloud infrastructure. Own your data, customize everything, and run it wherever you want.
```

- [ ] **Step 3: Restructure the "Self-Install Guide" section into Option A / Option B**

Replace the section starting at `## 🏠 Self-Install Guide` through the Prerequisites table with:

```markdown
## 🏠 Self-Install Guide

VoIPBin can be self-hosted two ways. Option A is the primary, recommended path for most
operators; Option B is for teams that specifically need GCP/Kubernetes-scale infrastructure.

### Option A — Single-Server Docker Compose (recommended)

Deploy the full stack on one server with Docker Compose. Lives in this repo's
[`self-install/`](self-install/) directory.

```bash
git clone https://github.com/voipbin/voipbin.git
cd voipbin/self-install

# Fresh install:
./scripts/init.sh --yes
sudo ./scripts/setup-host.sh
./scripts/start.sh
./scripts/check-install.sh
```

Migrating an existing `voipbin/sandbox` checkout instead of starting fresh? See
[`self-install/HISTORY.md`](self-install/HISTORY.md) and the migration notes in
[`self-install/README.md`](self-install/README.md) — the copy/skip file list and
`COMPOSE_PROJECT_NAME` handling matter for preserving your existing data.

Full docs, including backup/restore, version pinning/rollback, and troubleshooting: see
[`self-install/README.md`](self-install/README.md).

### Option B — GCP + Kubernetes (existing, still supported)

Deploy on your own cloud with a single CLI command. The
[**voipbin/install**](https://github.com/voipbin/install) repo handles everything: infrastructure
provisioning, VM configuration, and full Kubernetes deployment.

```bash
# Step 0: Install voipbin-install (requires git, python3, pip)
curl -fsSL https://raw.githubusercontent.com/voipbin/install/main/install.sh | bash -s -- --auto-approve
cd ~/voipbin-install

gcloud auth login
gcloud auth application-default login

./voipbin-install init
./voipbin-install apply
./voipbin-install verify
```

> 📖 **Full documentation**: See the [**voipbin/install**](https://github.com/voipbin/install)
> repo for detailed architecture, configuration reference, day-to-day operations, and cost
> breakdowns.
```

(Keep the existing "Day-to-Day Operations" code block and "Prerequisites" table for Option B —
move them under this Option B subsection rather than deleting them, since GCP install still needs
that reference material.)

- [ ] **Step 4: Fix the Repositories table**

Find:
```
| **[voipbin/sandbox](https://github.com/voipbin/sandbox)** | Sandbox & examples | ... |
```
Replace with:
```
| **[voipbin/sandbox](https://github.com/voipbin/sandbox)** | Legacy standalone location of the Docker Compose installer — now primarily developed as [`self-install/`](self-install/) in this repo | ... |
```

(`voipbin/install` row stays unchanged, per the design doc.)

- [ ] **Step 5: Fix the Documentation section bullet**

Find:
```
- 🐍 **[Examples & Sandbox](https://github.com/voipbin/sandbox)**. Sample applications and integrations
```
Replace with:
```
- 🏠 **[Self-Install](self-install/)**. Docker Compose installer — the primary self-hosting path
```

- [ ] **Step 6: Fix the Contributing table**

Find:
```
| Deployment / self-hosting | **[voipbin/install](https://github.com/voipbin/install)** |
| Examples & sandbox | **[voipbin/sandbox](https://github.com/voipbin/sandbox)** |
```
Replace with:
```
| Deployment / self-hosting (Docker Compose) | **This repo** — [`self-install/`](self-install/) |
| Deployment / self-hosting (GCP/K8s) | **[voipbin/install](https://github.com/voipbin/install)** |
```

- [ ] **Step 7: Fix the "no code of its own" claim**

Find (in the Contributing section, preceding text):
```
All source code lives in the individual repositories. This repo is the project hub with no code of its own.
```
Replace with:
```
Most source code lives in the individual repositories linked below. The one exception is
[`self-install/`](self-install/), the Docker Compose installer, which lives directly in this
repo.
```

- [ ] **Step 8: Fix the installer version badge (line ~33)**

Find:
```
  <a href="https://github.com/voipbin/install/releases"><img src="https://img.shields.io/github/v/tag/voipbin/install?label=installer&color=blueviolet" alt="Installer Version" /></a>
```

This badge specifically tracks `voipbin/install`'s GCP-installer version tags — leave it as-is
(it's accurate for Option B), but do not let it stand alone as if it's the only installer. No code
change needed here beyond what Steps 2-7 already did to give Option A equal visibility.

- [ ] **Step 9: Read through the whole file once for consistency**

```bash
grep -n "voipbin/sandbox\|Sandbox & examples\|no code of its own" README.md
```

Expected: no remaining references describing `voipbin/sandbox` as "the" installer location, and
no lingering "no code of its own" claim.

- [ ] **Step 10: Commit**

```bash
git add README.md
git commit -m "Document self-install/ as the primary self-hosting path

- voipbin: Restructure Self-Install Guide into Option A (Docker Compose, primary) / Option B (GCP+K8s, existing); update Repositories table, Documentation section, and Contributing table accordingly"
```

---

## Task 14: Document backup/version-pin capabilities honestly

**Files:**
- Modify: `self-install/README.md` (confirm the existing backup/versions sections are still
  accurate post-copy — likely no change needed, just verification)

- [ ] **Step 1: Confirm the existing sections are intact after the copy**

```bash
grep -n "database-backup\|versions.lock\|manifest.json" self-install/README.md
```

- [ ] **Step 2: Add the two honesty caveats the design doc calls for, if not already present**

Near the backup section, add (if this caveat isn't already stated):
```
> Note: the `database-backup` scheduler ships **disabled** by default and is enabled by
> `start.sh` on first run. Backups are retained locally (the newest 7 snapshots) — there is no
> automatic offsite/remote copy today.
```

- [ ] **Step 3: Commit (only if Step 2 made a change)**

```bash
git add self-install/README.md
git commit -m "Add honesty caveats to backup documentation

- voipbin: Note that the backup scheduler ships disabled by default and has no offsite copy, per design doc Scope item 6"
```

---

## Task 15: Final `voipbin/voipbin` verification and PR

**Files:** none new — verification only.

- [ ] **Step 1: Run the bats suite from the new location**

```bash
cd self-install
bats tests/*.bats
```

Expected: all pass — confirms the relocation into `self-install/` didn't break path resolution.

- [ ] **Step 2: Read through the rewritten README sections for broken links/anchors**

```bash
grep -n "\](#\|\](self-install" README.md | head -30
```

Manually confirm each anchor link (`#self-install-guide`, etc.) still matches an existing heading,
and each `self-install/...` relative link points at a file that exists.

- [ ] **Step 3: Confirm the CI workflow is at repo root and lints as valid YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/discord-merge-notify.yml'))" && echo "valid YAML"
```

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin NOJIRA-Integrate-sandbox-as-main-installer
gh pr create --repo voipbin/voipbin \
  --title "NOJIRA-Integrate-sandbox-as-main-installer" \
  --body "$(cat <<'EOF'
Bring voipbin/sandbox's Docker Compose installer into this repo as self-install/ and document
it as the primary, production-capable self-hosting path, alongside the existing voipbin/install
(GCP/K8s) option.

- voipbin: Copy hardened voipbin/sandbox into self-install/ (post credential-hardening PR)
- voipbin: Move discord-merge-notify.yml CI workflow to repo root
- voipbin: Rewrite self-install/README.md security section, remove inaccurate on-premise-licensing wording
- voipbin: Restructure root README's Self-Install Guide into Option A (Docker Compose, primary) / Option B (GCP+K8s, existing)
- voipbin: Document existing backup/version-pin capabilities in the primary install guide

Requires: DISCORD_WEBHOOK_URL secret provisioned on this repo (see .github/workflows/ commit).
Design doc: docs/superpowers/specs/2026-08-05-sandbox-as-main-installer-design.md
EOF
)"
```

**Do not merge this PR without the CEO/CTO's explicit go-ahead — wait for instruction before
merging, per org policy.**

---

## Task 16: (Separate, smaller PR) `voipbin/install` README banner

Per the design doc, this is explicitly a separate PR in a third repo — flag it to the user as
such before starting, since it's an exception to "one PR per task."

**Files:**
- Modify: `voipbin/install`'s `README.md` (add a banner near the top)

- [ ] **Step 1: Create the worktree**

```bash
cd ~/gitvoipbin/install
git fetch origin main
git worktree add ~/gitvoipbin/install/.worktrees/NOJIRA-Add-self-install-banner -b NOJIRA-Add-self-install-banner origin/main
cd ~/gitvoipbin/install/.worktrees/NOJIRA-Add-self-install-banner
```

- [ ] **Step 2: Add the banner near the top of `README.md`**

Insert after the title/badges section:
```markdown
> **Looking for the primary self-install path?** VoIPBin's main documented self-hosting method is
> now the single-server Docker Compose installer at
> [voipbin/voipbin's `self-install/`](https://github.com/voipbin/voipbin/tree/main/self-install).
> This GCP/Kubernetes installer remains fully supported for teams that need it — nothing here
> changes for existing installs.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Add banner pointing to voipbin/voipbin's primary self-install path

- install: Note that self-install/ (Docker Compose) is now the primary documented path, while confirming this GCP installer remains fully supported"
```

- [ ] **Step 4: Push and open the PR (only after Task 15's PR has merged, so the link resolves)**

```bash
git push -u origin NOJIRA-Add-self-install-banner
gh pr create --repo voipbin/install \
  --title "NOJIRA-Add-self-install-banner" \
  --body "Add a README banner pointing existing GCP-install users at voipbin/voipbin's new primary self-install path (self-install/), confirming this installer remains supported. See voipbin/voipbin design doc: docs/superpowers/specs/2026-08-05-sandbox-as-main-installer-design.md"
```

**Do not merge without explicit go-ahead.**

---

## Self-Review Notes (completed during plan writing)

- **Spec coverage:** All 9 numbered Scope items from the design doc map to tasks: item 1→Task 10,
  item 2→Task 13, item 3→Task 12, item 4→Tasks 2-6, item 5→Task 11, item 6→Task 14, item 7→Task 7,
  item 8→Task 8, item 9→Task 16. Phase 2 items are intentionally **not** planned here — they're
  follow-up tickets per the design doc, out of this plan's scope.
- **Placeholder scan:** No TBD/TODO markers; every code-editing step shows the actual before/after
  text. The one deliberately-open item (Task 11 Step 3, Discord secret provisioning) is flagged as
  a manual action outside this session's reach, not a placeholder.
- **Type/name consistency:** `derive_compose_project_name` (bash) and `_compose_project_name`
  (Python) are used consistently by that exact name across Tasks 8-9; `VOIPBIN_SANDBOX_DEV_SEED`
  is the same flag name in Task 6's code and Task 12's README rewrite.
