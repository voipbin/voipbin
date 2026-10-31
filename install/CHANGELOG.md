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

### Upgrade notes

- Your live `docker-compose.yml` is not updated by `git pull`. Merge the new settings by hand and add the two encryption keys to `.env` before `sudo ./voipbin update all`; the steps are in `README.md` under "Adopting the encryption keys, local LLM and timeline analysis settings". Do not run `init.sh --yes` or `--force-reinit` for this: they regenerate the database and RabbitMQ passwords.
- Until the merge, nothing else changes: ai-manager cannot store the secrets above and timeline-manager serves no analysis endpoints.
- Back up `.env`. A lost encryption key cannot be recovered, and whatever was stored under it has to be entered again.
