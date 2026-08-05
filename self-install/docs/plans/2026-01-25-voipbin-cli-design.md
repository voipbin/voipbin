# VoIPBin Interactive CLI Design

**Date:** 2026-01-25
**Status:** Approved

## Overview

An interactive command-line interface for managing VoIPBin sandbox, inspired by Asterisk CLI and Claude Code. Provides unified access to service management, debugging, and API operations.

## Requirements

- **Language:** Python (stdlib only, optional `requests`)
- **Interface:** Hybrid - flat commands with optional sub-contexts
- **Features:** Command history, tab completion, colored output, help system, config file

## Architecture

**File:** `scripts/voipbin-cli.py` (single file, ~500-700 lines)

```
┌─────────────────────────────────────────────┐
│              VoIPBin CLI                    │
├─────────────────────────────────────────────┤
│  readline (history, line editing)           │
│  completer (tab completion)                 │
├─────────────────────────────────────────────┤
│  CommandRouter                              │
│  ├── service commands (start/stop/status)   │
│  ├── logs commands                          │
│  ├── contexts (ast, kam, db, api)           │
│  └── shortcuts (ext, customer)              │
├─────────────────────────────────────────────┤
│  Config (~/.voipbin-cli.conf)               │
│  History (~/.voipbin-cli-history)           │
└─────────────────────────────────────────────┘
```

**Entry point:**
```bash
./scripts/voipbin-cli.py
```

## Command Structure

### Top-level Commands

| Command | Description | Example |
|---------|-------------|---------|
| `status` | Show all services status | `status` |
| `ps` | Alias for status | `ps` |
| `start [service]` | Start all or specific service | `start api-manager` |
| `stop [service]` | Stop all or specific service | `stop` |
| `restart [service]` | Restart service(s) | `restart kamailio` |
| `logs <service>` | Show last N lines | `logs api-manager` |
| `logs -f <service>` | Follow logs (Ctrl+C to stop) | `logs -f call-manager` |
| `help [command]` | Show help | `help logs` |
| `config [key] [val]` | View/set configuration | `config log_lines 100` |
| `exit` / `quit` | Exit CLI | `exit` |

### Context Entry Commands

| Command | Context | Pass-through to |
|---------|---------|-----------------|
| `ast [cmd]` | asterisk | `docker exec voipbin-ast-call asterisk -rx "cmd"` |
| `kam [cmd]` | kamailio | `docker exec voipbin-kamailio kamcmd cmd` |
| `db [query]` | database | `docker exec voipbin-db mysql -e "query"` |
| `api [method] [path]` | api | curl to https://localhost:8443 |

### Shortcut Commands

| Command | Action |
|---------|--------|
| `ext list` | List extensions |
| `ext create <num> <pass>` | Create extension |
| `ext delete <id>` | Delete extension |
| `customer info` | Show customer info |
| `customer create <email>` | Create customer |

## Context Behavior

**Entering a context:**
```
voipbin> ast
voipbin(asterisk)> pjsip show endpoints
voipbin(asterisk)> core show channels
voipbin(asterisk)> exit
voipbin>
```

**One-off commands (without entering context):**
```
voipbin> ast pjsip show endpoints
voipbin> kam ul.dump
voipbin> db SELECT COUNT(*) FROM extensions
```

**API context special handling:**
- Auto-login on first API call if no token
- Store token in memory for session
- Commands: `login`, `get /path`, `post /path {...}`

```
voipbin> api
voipbin(api)> login admin@localhost
Password: ********
Logged in successfully.
voipbin(api)> get /v1.0/extensions
[{"id": "...", "extension": "1000", ...}]
```

## Tab Completion

1. **Commands** - Complete top-level commands
2. **Service names** - Complete running container names
3. **Subcommands** - Complete known subcommands (ext list/create/delete, etc.)

## History

- **File:** `~/.voipbin-cli-history`
- **Size:** Last 1000 commands (configurable)
- **Navigation:** Up/Down arrows, Ctrl+R for search

## Colored Output

| Element | Color | Usage |
|---------|-------|-------|
| Running/Success | Green | `● running`, `✓ Created` |
| Stopped/Error | Red | `○ stopped`, `✗ Failed` |
| Warning/Partial | Yellow | `⚠ restarting` |
| Info/Headers | Blue | Section headers |
| Muted | Gray | Timestamps, IDs |

**Status output example:**
```
voipbin> status
┌──────────────────────┬────────────┐
│ Service              │ Status     │
├──────────────────────┼────────────┤
│ api-manager          │ ● running  │
│ call-manager         │ ● running  │
│ kamailio             │ ⚠ restart  │
│ ai-manager           │ ○ stopped  │
└──────────────────────┴────────────┘
```

## Configuration

**Location:** `~/.voipbin-cli.conf` (JSON)

```json
{
  "api_host": "localhost",
  "api_port": 8443,
  "log_lines": 50,
  "history_size": 1000,
  "colors": true,
  "asterisk_container": "voipbin-ast-call",
  "registrar_container": "voipbin-ast-registrar",
  "db_container": "voipbin-db",
  "db_password": "root_password"
}
```

Auto-created on first run. Environment variables can override config values.

## Implementation Notes

- Single Python file for simplicity
- Only stdlib dependencies (readline, subprocess, json, os, atexit)
- Optional: `requests` for cleaner API calls (fallback to curl)
- Graceful handling of missing containers
- Ctrl+C handling for log following and long operations
