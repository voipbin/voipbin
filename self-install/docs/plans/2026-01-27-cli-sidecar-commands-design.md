# CLI Sidecar Commands Design

**Date:** 2026-01-27
**Status:** Approved

## Overview

Integrate sidecar control binaries (billing-control, customer-control, number-control) into the VoIPBin CLI with human-friendly input/output, interactive prompts, and confirmation dialogs.

## Command Structure

### Billing Commands

```
billing account list [--customer-id ID] [--limit N] [--verbose]
billing account create --customer-id ID [--name NAME] [--detail TEXT] [--payment-type TYPE]
billing account get --id ID [--verbose]
billing account delete --id ID
billing account add-balance --id ID --amount AMOUNT
billing account subtract-balance --id ID --amount AMOUNT

billing billing list [--customer-id ID] [--account-id ID] [--limit N] [--verbose]
billing billing get --id ID [--verbose]
```

### Customer Commands

```
customer list [--limit N] [--verbose]
customer create --email EMAIL [--name NAME] [--detail TEXT] [--address ADDR] [--phone_number PHONE]
customer get --id ID [--verbose]
customer delete --id ID
```

### Number Commands

```
number list [--customer-id ID] [--limit N] [--verbose]
number create --number +15551234567 [--customer-id ID] [--name NAME] [--detail TEXT]
number get --id ID [--verbose]
number delete --id ID
number register --number +15551234567 [--customer-id ID] [--name NAME] [--detail TEXT]
```

## Output Formatting

### Table Format (for list commands)

```
voipbin> billing account list

Billing Accounts (1 found)
──────────────────────────────────────────────────────────────────────────────
ID                                    Name                   Balance  Payment
──────────────────────────────────────────────────────────────────────────────
9fbe7f10-354f-4c8f-9b0a-c782b5de8fe9  basic billing account     0.00  prepaid
──────────────────────────────────────────────────────────────────────────────
```

### Key-Value Format (for get commands)

```
voipbin> billing account get --id 9fbe7f10-354f-4c8f-9b0a-c782b5de8fe9

Billing Account
────────────────────────────────────
  ID:             9fbe7f10-354f-4c8f-9b0a-c782b5de8fe9
  Customer ID:    9924a2b6-181b-4cc7-ab9c-327f5569b0db
  Name:           basic billing account
  Detail:         billing account for default use
  Balance:        0.00
  Payment Type:   prepaid
  Payment Method: -
  Created:        2026-01-26 12:55:51
```

### Success Messages

```
voipbin> billing account add-balance --id abc123 --amount 100

✓ Added 100.00 to account "basic billing account"
  New balance: 100.00
```

### Empty Results

```
voipbin> number list

No numbers found.
```

## Interactive Features

### Delete Confirmation

```
voipbin> customer delete --id 9924a2b6-181b-4cc7-ab9c-327f5569b0db

⚠ Delete customer?
  Name:  Test Customer
  Email: admin@localhost
  ID:    9924a2b6-181b-4cc7-ab9c-327f5569b0db

  This action cannot be undone.

Type 'yes' to confirm: yes
✓ Customer deleted.
```

Typing anything other than 'yes' cancels the operation.

### Missing Required Arguments

Prompts one by one:

```
voipbin> billing account add-balance

Missing required argument: --id

Enter id: 9fbe7f10-354f-4c8f-9b0a-c782b5de8fe9

Missing required argument: --amount

Enter amount: 50
Adding balance...
✓ Added 50.00 to account "basic billing account"
```

Ctrl+C cancels the prompt.

## Error Handling

### Log Filtering

By default, DEBUG/ERROR logs from docker exec are filtered out. Use `--verbose` to see them:

```
voipbin> billing account list --verbose

[DEBUG] Configuration has been loaded and locked.
[ERROR] Could not start prometheus listener
[DEBUG] Connecting to rabbitmq
[DEBUG] Connection established to rabbitmq.

Billing Accounts (1 found)
...
```

### Error Messages

```
voipbin> billing account get --id nonexistent

✗ Account not found: nonexistent
```

```
voipbin> billing account list

✗ Service unavailable: voipbin-billing-mgr is not running.
  Run 'start' to launch services.
```

## Help System

### Top-level Help

```
voipbin> help

VoIPBin Sandbox CLI

Service Management:
  start                  Start all services
  stop                   Stop all services
  restart                Restart services
  status                 Show service status
  logs                   View service logs

Data Management:
  billing                Billing and account management
  customer               Customer management
  number                 Phone number management
  extension              Extension management

Configuration:
  init                   Initialize environment
  config                 View/set configuration
  dns                    DNS configuration
  network                Network configuration

Other:
  help                   Show this help
  exit                   Exit CLI

Type '<command> help' for details on a specific command.
```

### Command Group Help

```
voipbin> billing help

Billing Management

Available Commands:
  billing account        Manage billing accounts
  billing billing        View billing records

Type 'billing <subcommand> help' for more details.
```

### Subcommand Help

```
voipbin> billing account help

Billing Account Management

Available Commands:
  list             List billing accounts
  create           Create a new billing account
  get              Get account details by ID
  delete           Delete an account
  add-balance      Add balance to an account
  subtract-balance Subtract balance from an account

Usage: billing account <command> [options]

Examples:
  billing account list
  billing account list --customer-id abc123
  billing account create --customer-id abc123 --name "Main Account"
  billing account get --id xyz789
  billing account add-balance --id xyz789 --amount 100
```

### Specific Command Help

```
voipbin> billing account create help

Create a new billing account

Usage: billing account create [options]

Required:
  --customer-id ID       Customer ID

Optional:
  --name NAME            Account name
  --detail TEXT          Account description
  --payment-type TYPE    Payment type (default: prepaid)
  --payment-method METHOD Payment method

Examples:
  billing account create --customer-id abc123
  billing account create --customer-id abc123 --name "Main Account" --detail "Primary billing"
```

## Implementation

### Sidecar Command Registry

```python
SIDECAR_COMMANDS = {
    "billing": {
        "container": "voipbin-billing-mgr",
        "binary": "/app/bin/billing-control",
        "subcommands": {
            "account": ["list", "create", "get", "delete", "add-balance", "subtract-balance"],
            "billing": ["list", "get"],
        }
    },
    "customer": {
        "container": "voipbin-customer-mgr",
        "binary": "/app/bin/customer-control",
        "subcommands": {
            "customer": ["list", "create", "get", "delete"],
        }
    },
    "number": {
        "container": "voipbin-number-mgr",
        "binary": "/app/bin/number-control",
        "subcommands": {
            "number": ["list", "create", "get", "delete", "register"],
        }
    },
}
```

### Required Arguments by Command

```python
REQUIRED_ARGS = {
    "billing account create": ["customer-id"],
    "billing account get": ["id"],
    "billing account delete": ["id"],
    "billing account add-balance": ["id", "amount"],
    "billing account subtract-balance": ["id", "amount"],
    "billing billing get": ["id"],
    "customer create": ["email"],
    "customer get": ["id"],
    "customer delete": ["id"],
    "number get": ["id"],
    "number delete": ["id"],
    "number create": ["number"],
    "number register": ["number"],
}
```

### Delete Commands (require confirmation)

- `billing account delete`
- `customer delete`
- `number delete`

### Key Helper Functions

1. **`run_sidecar_command(container, binary, args, verbose=False)`**
   - Execute docker exec
   - Filter log lines (JSON with "severity" key)
   - Return parsed JSON or error

2. **`format_table(data, columns)`**
   - Render list of dicts as aligned table
   - Handle missing keys gracefully

3. **`format_details(data, field_map)`**
   - Render single dict as key-value pairs
   - Format dates, handle None values

4. **`prompt_missing_args(command, provided_args)`**
   - Check required args against provided
   - Prompt for each missing arg
   - Support Ctrl+C to cancel

5. **`confirm_delete(resource_type, resource_data)`**
   - Show resource details
   - Require typing 'yes' to confirm

6. **`parse_sidecar_args(args)`**
   - Parse `--flag value` style arguments
   - Return dict of flag->value
