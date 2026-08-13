# Komodo - Web UI Container Management (VOIP-1339)

Komodo gives you a browser UI for restarting/stopping/starting containers,
tailing logs, and viewing CPU/memory/network/disk usage - without SSH+CLI.
It is a standalone [core/periphery](https://komo.do) deployment, entirely
separate from the main voipbin stack in `../`.

**Role division:** metrics and dashboards live in Grafana (VOIP-1336, see
`../README.md` "Monitoring"). Restarting containers, reading logs, and other
hands-on actions live in Komodo. The two tools don't overlap in purpose.

## What this does NOT do

Komodo is not a deployment tool here. The main voipbin stack's only
supported deploy path is still CircleCI -> SSH. No Komodo "Stack" resource
is configured for that stack in this first pass, so the Deploy/Destroy UI
and git-sync auto-update don't exist in this setup - don't add one for the
main stack without a separate design/review (see
`../docs/plans/2026-08-14-komodo-container-management-design.md`).

## Setup

```bash
cd install/komodo
./scripts/komodo.sh init   # generates .env + docker-compose.yml (idempotent)
./scripts/komodo.sh up
```

**Always use `./scripts/komodo.sh`, never call `docker compose` directly in
this directory.** The wrapper pins both `-p komodo` and `-f
docker-compose.yml` on every invocation, which is what keeps this stack
isolated from the main one even if your shell has `COMPOSE_PROJECT_NAME`
exported (this repo's own README recommends exporting
`COMPOSE_PROJECT_NAME=sandbox` for the main stack - see `../README.md`). If
you must run a raw `docker compose` command here, add `-p komodo -f
docker-compose.yml` yourself, or `unset COMPOSE_PROJECT_NAME
COMPOSE_FILE` first. Skipping this lets Komodo's 3 containers join the main
voipbin network, exposing `komodo-periphery`'s Docker-socket access
(effectively host-root-equivalent) to that network's 30+ containers.

## Access

Komodo's web UI binds to `127.0.0.1:9120` only - reach it over an SSH local
port-forward, same pattern as Grafana/Prometheus/RabbitMQ's management UI:

```bash
ssh -L 9120:127.0.0.1:9120 root@<host>
```

Then open `http://localhost:9120` in your browser and log in with the admin
username/password from `.env` (`KOMODO_ADMIN_USERNAME`/`KOMODO_ADMIN_PASSWORD`).

## Operating

```bash
./scripts/komodo.sh ps                    # container status
./scripts/komodo.sh logs [service]        # tail logs (mongo|core|periphery)
./scripts/komodo.sh restart [service]     # restart one or all
./scripts/komodo.sh down                  # stop everything
```

Stopping/cleaning the main stack (`../scripts/stop.sh`, `../scripts/clean.sh`)
never touches this stack, and vice versa - they are separate compose
projects with separate volumes.

## Secrets

`.env` holds 4 auto-generated secrets (`KOMODO_ADMIN_PASSWORD`,
`KOMODO_DATABASE_PASSWORD`, `KOMODO_JWT_SECRET`, `KOMODO_WEBHOOK_SECRET`).
There is no `--force-reinit` for this stack: regenerating
`KOMODO_DATABASE_PASSWORD` without also wiping the `komodo_mongo_data`
volume leaves MongoDB's existing root account out of sync with the new
value and Komodo falls into an auth-failure loop. To rotate secrets:

```bash
./scripts/komodo.sh down
docker volume rm komodo_komodo_mongo_data komodo_komodo_mongo_config komodo_komodo_keys
rm .env
./scripts/komodo.sh init
./scripts/komodo.sh up
```

This resets all Komodo state (server registration, any saved views) - it
does not touch the main voipbin stack.

## Image updates

The 3 images here (`mongo`, `ghcr.io/moghtech/komodo-core`,
`ghcr.io/moghtech/komodo-periphery`) are digest-pinned in
`docker-compose.yml.dist` but are **not** part of the main stack's
`versions.lock`/`sync-compose-images.sh` pipeline (tracked as
[VOIP-1340](https://voipbin.atlassian.net/browse/VOIP-1340) for future
consideration). Update by diffing `docker-compose.yml.dist` against your
live `docker-compose.yml` and merging the new digests deliberately.
