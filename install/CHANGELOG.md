# Changelog

Release notes for the self-hosted install (`install/`), newest first. Each
release gets its own `## [vX.Y.Z]` section; changes that are not released yet
are collected under `## [Unreleased]`. (`HISTORY.md` is a different file: it
records where this directory came from.)

## [Unreleased]

### Added

- timeline-manager now gets `DATABASE_DSN`, so its analysis store is initialized and the analysis endpoints are served, and it waits for the database to be healthy before starting.
- ai-manager gets `MCP_SECRET_ENCRYPTION_KEYS` and `ENGINE_BASE_KEY_ENCRYPTION_KEYS`. `init.sh` generates both on install (a different random key each, written to `.env` only) and keeps existing valid values when it rewrites `.env` with `--yes` or `--force-reinit`. Without them ai-manager cannot store MCP server secrets or the API keys customers keep for their own LLM endpoints.
- `LOCAL_LLM_MODELS` (ai-manager, pipecat-manager) and `LOCAL_LLM_ENDPOINTS` (pipecat-script-runner) for self-hosted OpenAI-compatible LLM servers. Both are off by default; `.env.template` explains the format and the order to enable them.
- pipecat-script-runner now receives `GOOGLE_APPLICATION_CREDENTIALS` and the credentials file mount, like the other services that use Google APIs. The default is the dummy file `init.sh` creates, which is not a valid credential; set the variable in `.env` to a real service account file to use Google speech services.
- `sudo ./voipbin install`: a guided install that runs host checks, `init.sh`, `setup-host.sh` (after showing the host changes and asking), `start.sh`, a health verification and the first admin account in one command, and ends with the admin console URL and a one-time random password. `--yes --admin-email <e> --admin-password-file <path>` runs it without questions, the last line is `VOIPBIN_INSTALL: status=ok|error ...`, and `--resume` continues an install that stopped after the configuration step. `sudo ./voipbin` on a new checkout offers it.
- `scripts/create-admin.sh` creates or resets a customer's admin login (password from an environment variable, never on a command line) and sets its billing plan to unlimited. The dev seed now uses it too.
- `sudo ./voipbin` on a terminal now opens a start screen: a header with the install state, mode and directory, an input box between two rules, and a status bar (`Env`, `Services`, `DNS`, `Net`, `Mode`) refreshed after commands that change or report the state. Slash commands `/help`, `/install`, `/status`, `/start`, `/stop`, `/restart`, `/logs`, `/doctor`, `/update`, `/version`, `/clear` and `/exit` run the matching commands, and Tab completes them. The plain words keep working. Pipes, scripts, `NO_COLOR`, `TERM=dumb` and non-UTF-8 terminals keep the previous prompt and output, and commands run with arguments are unchanged.

### Changed

- `sudo ./voipbin init` passes its options to `init.sh`, and `init`, `start` and `clean` now exit with a non-zero code when they fail.
- `sudo ./voipbin clean --volumes|--images|--purge|--all` lists what it removes and asks for `yes` first; scripts must add `--force`. `clean --purge` now also removes `docker-compose.yml`, `versions.lock` and the install marker, like `scripts/clean.sh --purge`. When `clean --volumes` keeps the VoIP network interfaces, `clean` and `scripts/clean.sh` say to remove them (`--network`) before installing again, and `install` stops at the host check while they are still there: a new install next to them loses container network access.
- `check-install.sh`, `doctor.sh` and the install verification treat an "unhealthy" container whose image has no healthcheck binary as a known non-fault, and tts-manager restarting because no TTS credentials are configured as an optional feature that is not set up. Other unhealthy, restarting or exited containers still fail.
- `sudo ./voipbin start` no longer resolves image tags on installs with `versions.lock`, whose images are already pinned by digest.
- `help` and `--help` show the same grouped command list (resource commands moved to `help api`), and `help`, `--help` and `version` work without sudo. Output has no color when `NO_COLOR` is set or stdout is not a terminal.
- `status` shows which optional provider keys are set and no longer prints MySQL or RabbitMQ credentials; the start summary no longer prints the RabbitMQ password.
- User-facing text no longer calls the install a sandbox, and `.env.template` labels secrets and certificates as generated and provider keys as optional. New installs use `voipbin-placeholder` for the GCP placeholder values; existing `.env` values keep working.

### Fixed

- `doctor.sh` no longer warns that `start.sh` will re-seed test data on an install that has the dev seed off (the default). Without `VOIPBIN_SANDBOX_DEV_SEED=true` in `.env` the missing `.test_data_initialized` marker is expected, so the `test-data` check now passes there. With the seed on it still warns.

### Upgrade notes

- Your live `docker-compose.yml` is not updated by `git pull`. Merge the new settings by hand and add the two encryption keys to `.env` before `sudo ./voipbin update all`; the steps are in `README.md` under "Adopting the encryption keys, local LLM and timeline analysis settings". Do not run `init.sh --yes` or `--force-reinit` for this: they regenerate the database and RabbitMQ passwords.
- Until the merge, nothing else changes: ai-manager cannot store the secrets above and timeline-manager serves no analysis endpoints.
- Back up `.env`. A lost encryption key cannot be recovered, and whatever was stored under it has to be entered again.
- Scripts that call `sudo ./voipbin clean --volumes`, `--images`, `--purge` or `--all` must add `--force`; without it (and without a terminal to answer `yes`) `clean` now removes nothing and exits with code 1.
