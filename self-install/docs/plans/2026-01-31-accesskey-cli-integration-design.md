# Accesskey CLI Integration Design

## Overview

Add `accesskey` subcommand to the existing `customer` service in the voipbin CLI. This enables management of API access keys directly from the command line.

## Command Structure

```bash
# List accesskeys
customer accesskey list
customer accesskey list --customer-id <id>
customer accesskey list --size 20

# Create accesskey
customer accesskey create --customer-id <id> --name "API Key"
customer accesskey create --customer-id <id> --name "Production" --detail "For prod env" --expire 720h

# Get accesskey details
customer accesskey get --id <key-id>

# Update accesskey
customer accesskey update --id <key-id> --name "New Name"

# Delete accesskey
customer accesskey delete --id <key-id>
```

## Required Arguments

| Command | Required Args |
|---------|---------------|
| `list` | (none) |
| `create` | `customer-id`, `name` |
| `get` | `id` |
| `update` | `id` |
| `delete` | `id` |

## Output Formatting

### List (Table)

| Column | Field | Width |
|--------|-------|-------|
| ID | `id` | 36 |
| Name | `name` | 20 |
| Customer ID | `customer_id` | 36 |
| Expire | `tm_expire` | 19 |

### Get (Details)

Fields: ID, Name, Detail, Customer ID, Token, Expire, Created, Updated

## Delete Confirmation

Delete operations display accesskey details and require typing `yes` to confirm.

## Implementation

Update `scripts/voipbin-cli.py`:

1. **SIDECAR_COMMANDS** - Add `accesskey` subcommand under `customer`
2. **SIDECAR_REQUIRED_ARGS** - Add required args for create, get, update, delete
3. **SIDECAR_DELETE_COMMANDS** - Add delete confirmation tuple
4. **SIDECAR_TABLE_COLUMNS** - Add list formatting
5. **SIDECAR_DETAIL_FIELDS** - Add get formatting
6. **cmd_customer()** - Update valid actions and help text
