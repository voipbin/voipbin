# CLI Welcome Dashboard Design

**Date:** 2026-01-26
**Status:** Approved

## Overview

Redesign the VoIPBin CLI welcome screen to show a user-friendly dashboard with ASCII logo, status at a glance, and context-aware guidance.

## Current State

```
VoIPBin Sandbox CLI
Type 'help' for available commands, 'exit' to quit.

voipbin>
```

Minimal, no branding, no status information.

## New Design

### Logo + Branding

ASCII art combining phone icon with text:

```
          ████████
   ██████████████████████           __     __   ___ ____  ____  _
  ██                    ██          \ \   / /  / _ \_ _||  _ \| |__  _ _ __
 ██████████████████████████          \ \ / / | | | | | | |_) | '_ \| | '_ \
 ██                      ██           \ V /  | |_| | | |  __/| |_) | | | | |
  ██    ██   ██   ██    ██             \_/    \___/___|_|   |____/|_|_| |_|
  ██    ██   ██   ██    ██                  Connect & Collaborate for all
  ██    ██   ██   ██    ██                        S A N D B O X
  ██    ██   ██   ██    ██
   ██   ██   ██   ██   ██
   ██████████████████████
```

**Colors:**
- Phone icon: White
- "VoIPBin" ASCII text: Bold white
- "Connect & Collaborate for all": Gray
- "SANDBOX": Bold gray

### Context-Aware States

The dashboard adapts based on system state:

#### State 1: Not Initialized (no .env file)

```
          ████████
   ██████████████████████           __     __   ___ ____  ____  _
  ██                    ██          \ \   / /  / _ \_ _||  _ \| |__  _ _ __
 ██████████████████████████          \ \ / / | | | | | | |_) | '_ \| | '_ \
 ██                      ██           \ V /  | |_| | | |  __/| |_) | | | | |
  ██    ██   ██   ██    ██             \_/    \___/___|_|   |____/|_|_| |_|
  ██    ██   ██   ██    ██                  Connect & Collaborate for all
  ██    ██   ██   ██    ██                        S A N D B O X
  ██    ██   ██   ██    ██
   ██   ██   ██   ██   ██
   ██████████████████████

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

      /\
     /  \       Not initialized. Let's get started:
    / !! \
   /______\        1. init          Generate .env and SSL certificates
                   2. nano .env     Add your API keys (GCP, OpenAI, etc.)
                   3. start         Launch all services

                Quick start: init → nano .env → start

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type 'help' for all commands, 'exit' to quit.
```

**Detection:** `.env` file does not exist
**Warning triangle:** Yellow color

#### State 2: Initialized but Stopped

```
          ████████
   ██████████████████████           __     __   ___ ____  ____  _
  ██                    ██          \ \   / /  / _ \_ _||  _ \| |__  _ _ __
 ██████████████████████████          \ \ / / | | | | | | |_) | '_ \| | '_ \
 ██                      ██           \ V /  | |_| | | |  __/| |_) | | | | |
  ██    ██   ██   ██    ██             \_/    \___/___|_|   |____/|_|_| |_|
  ██    ██   ██   ██    ██                  Connect & Collaborate for all
  ██    ██   ██   ██    ██                        S A N D B O X
  ██    ██   ██   ██    ██
   ██   ██   ██   ██   ██
   ██████████████████████

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

     ○○○        Services stopped. Ready to start.
    ○   ○
     ○○○           Type 'start' to launch all services
                   Type 'status' for detailed configuration

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type 'help' for all commands, 'exit' to quit.
```

**Detection:** `.env` exists AND no running containers
**Circle icon:** Gray color

#### State 3: Running (Full Dashboard)

```
          ████████
   ██████████████████████           __     __   ___ ____  ____  _
  ██                    ██          \ \   / /  / _ \_ _||  _ \| |__  _ _ __
 ██████████████████████████          \ \ / / | | | | | | |_) | '_ \| | '_ \
 ██                      ██           \ V /  | |_| | | |  __/| |_) | | | | |
  ██    ██   ██   ██    ██             \_/    \___/___|_|   |____/|_|_| |_|
  ██    ██   ██   ██    ██                  Connect & Collaborate for all
  ██    ██   ██   ██    ██                        S A N D B O X
  ██    ██   ██   ██    ██
   ██   ██   ██   ██   ██
   ██████████████████████

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Services                              Endpoints
  ─────────────────────────────         ─────────────────────────────────────
  ● 22/25 running                       Admin    http://admin.voipbin.test
  ○ coredns, rtpengine, ast-proxy       API      https://api.voipbin.test
                                        Meet     http://meet.voipbin.test
  DNS: ● active                         Talk     http://talk.voipbin.test
  Network: ● configured                 SIP      sip.voipbin.test:5060

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type 'help' for all commands, 'status' for details, 'exit' to quit.
```

**Detection:** Running containers exist
**Status indicators:**
- Green ● for running/active
- Gray ○ for stopped services

## State Detection Logic

```python
def detect_state():
    env_exists = os.path.exists(".env")
    running_containers = get_running_containers()

    if not env_exists:
        return "not_initialized"
    elif not running_containers:
        return "stopped"
    else:
        return "running"
```

## Implementation Notes

1. **File to modify:** `scripts/voipbin-cli.py`

2. **New function:** `show_welcome_dashboard()` - replaces the simple print statement in `main()`

3. **Helper functions needed:**
   - `get_logo()` - returns the ASCII logo string
   - `detect_state()` - returns "not_initialized", "stopped", or "running"
   - `get_quick_status()` - returns running count, stopped services, DNS/network status

4. **Color usage:**
   - Use existing `blue()`, `green()`, `yellow()`, `gray()`, `bold()` functions
   - Logo icon: `blue()`
   - Warning triangle: `yellow()`
   - Running indicators: `green("●")`
   - Stopped indicators: `gray("○")`

5. **Performance consideration:**
   - Status checks should be fast (single docker command)
   - Don't run full `cmd_status()` - just get counts
