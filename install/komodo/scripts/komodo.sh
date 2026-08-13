#!/usr/bin/env bash
# komodo.sh - the only supported way to operate the Komodo stack.
#
# Always pins BOTH the compose project name and the compose file explicitly
# (`-p komodo -f docker-compose.yml`), regardless of what's exported in the
# calling shell. This matters because Compose's project-name precedence is
# `-p` flag > `COMPOSE_PROJECT_NAME` env var > docker-compose.yml.dist's
# top-level `name:` field > directory name - and this repo's own README.md
# tells operators to `export COMPOSE_PROJECT_NAME=sandbox` for the main
# stack. Without this script forcing `-p komodo` on every invocation, a
# shell with that variable set would silently fold Komodo's 3 containers
# into the main `sandbox` project/network, exposing komodo-periphery's
# docker.sock (host-root-equivalent) to the 30+ containers on that network.
# Similarly, `-f docker-compose.yml` guards against a stray `COMPOSE_FILE`
# env var pointing compose at the wrong file.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."   # install/komodo/

# Intentionally a plain variable (word-split on call, not an array) - the
# value is a fixed literal with no spaces/globs to mis-split, and this keeps
# every subcommand below a one-liner instead of repeating the same 5 tokens
# 6 times.
# shellcheck disable=SC2086
COMPOSE="docker compose -p komodo -f docker-compose.yml"

# Diagnostic only - the -p/-f flags above are what actually enforces
# isolation; this is just a heads-up for anyone running raw `docker compose`
# commands in this directory by hand instead of through this script.
if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "${COMPOSE_PROJECT_NAME}" != "komodo" ]]; then
    echo "[komodo] Note: \$COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME} is set in your shell." >&2
    echo "[komodo] This script always uses -p komodo regardless, but if you run" >&2
    echo "[komodo] 'docker compose' directly in this directory, add -p komodo" >&2
    echo "[komodo] yourself or 'unset COMPOSE_PROJECT_NAME' first." >&2
fi

case "${1:-}" in
    init)
        ./scripts/init-komodo.sh
        ;;
    up)
        $COMPOSE up -d
        ;;
    down)
        $COMPOSE down
        ;;
    restart)
        $COMPOSE restart "${@:2}"
        ;;
    logs)
        $COMPOSE logs -f "${@:2}"
        ;;
    ps|status)
        $COMPOSE ps
        ;;
    *)
        echo "Usage: $0 {init|up|down|restart [service...]|logs [service...]|ps|status}" >&2
        exit 1
        ;;
esac
