#!/usr/bin/env python3
"""
VoIPBin Sandbox Interactive CLI

An interactive command-line interface for managing VoIPBin sandbox,
inspired by Asterisk CLI and Claude Code.

This CLI requires sudo for full functionality (network setup, DNS config, etc.)
"""

import atexit
import getpass
import hashlib
import json
import os
import readline
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


def check_root():
    """Check if running as root, exit if not"""
    if os.geteuid() != 0:
        print("\033[91m[ERROR]\033[0m This CLI requires sudo for full functionality.")
        print("")
        print("  Usage: sudo ./voipbin")
        print("")
        sys.exit(1)

# =============================================================================
# Configuration
# =============================================================================

CONFIG_FILE = Path.home() / ".voipbin-cli.conf"
HISTORY_FILE = Path.home() / ".voipbin-cli-history"

DEFAULT_CONFIG = {
    "api_host": "localhost",
    "api_port": 8443,
    "log_lines": 50,
    "history_size": 1000,
    "colors": True,
    "asterisk_container": "voipbin-ast-call",
    "registrar_container": "voipbin-ast-registrar",
    "kamailio_container": "voipbin-kamailio",
    "db_container": "voipbin-db",
    "db_password": os.environ.get("MYSQL_ROOT_PASSWORD", "root_password"),
    "project_dir": str(Path(__file__).parent.parent),
}

# =============================================================================
# Backup Configuration (for update scripts)
# =============================================================================

BACKUP_DIR = ".backup"
MAX_BACKUPS = 5  # Keep last 5 backups

# Files/directories that should never be touched during script updates
PROTECTED_PATHS = [
    ".env",
    "certs/",
    "tmp/",
    ".backup/",
    ".git/",
    "__pycache__/",
]

# Files/directories that are tracked for script updates
TRACKED_PATHS = [
    "scripts/",
    "docker-compose.yml",
    ".env.template",
    "config/",
    "tests/",
    "CLAUDE.md",
    "README.md",
]

# =============================================================================
# Colors
# =============================================================================

class Colors:
    """ANSI color codes"""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls):
        cls.GREEN = cls.RED = cls.YELLOW = cls.BLUE = ""
        cls.GRAY = cls.WHITE = cls.BOLD = cls.DIM = cls.RESET = ""


def green(text):
    return f"{Colors.GREEN}{text}{Colors.RESET}"

def red(text):
    return f"{Colors.RED}{text}{Colors.RESET}"

def yellow(text):
    return f"{Colors.YELLOW}{text}{Colors.RESET}"

def blue(text):
    return f"{Colors.BLUE}{text}{Colors.RESET}"

def gray(text):
    return f"{Colors.GRAY}{text}{Colors.RESET}"

def white(text):
    return f"{Colors.WHITE}{text}{Colors.RESET}"

def bold(text):
    return f"{Colors.BOLD}{text}{Colors.RESET}"

def dim(text):
    return f"{Colors.DIM}{text}{Colors.RESET}"


# =============================================================================
# Welcome Dashboard
# =============================================================================

# ASCII Logo - Phone icon + VoIPBin text
# Each icon line is padded to 28 characters for consistent alignment
LOGO_LINES = [
    ("          ████████          ", ""),
    ("   ██████████████████████   ", " __     __   ___ ____  ____  _"),
    ("  ██                    ██  ", " \\ \\   / /__|_ _|  _ \\| __ )(_)_ __"),
    (" ██████████████████████████ ", "  \\ \\ / / _ \\| || |_) |  _ \\| | '_ \\"),
    (" ██                      ██ ", "   \\ V / (_) | ||  __/| |_) | | | | |"),
    ("  ██    ██   ██   ██    ██  ", "    \\_/ \\___/___|_|   |____/|_|_| |_|"),
    ("  ██    ██   ██   ██    ██  ", "        Connect & Collaborate for all"),
    ("  ██    ██   ██   ██    ██  ", "              S A N D B O X"),
    ("  ██    ██   ██   ██    ██  ", ""),
    ("   ██   ██   ██   ██   ██   ", ""),
    ("   ██████████████████████   ", ""),
]

# =============================================================================
# Sidecar Command Registry
# =============================================================================

SIDECAR_COMMANDS = {
    "billing": {
        "container": "voipbin-billing-mgr",
        "binary": "/app/bin/billing-control",
        "subcommands": {
            "account": {
                "commands": ["list", "create", "get", "delete", "add-balance", "subtract-balance", "update", "update-payment-info"],
                "description": "Manage billing accounts",
            },
            "billing": {
                "commands": ["list", "get"],
                "description": "View billing records",
            },
        },
    },
    "customer": {
        "container": "voipbin-customer-mgr",
        "binary": "/app/bin/customer-control",
        "subcommands": {
            "customer": {
                "commands": ["list", "create", "get", "delete", "update", "update-billing-account"],
                "description": "Manage customers",
            },
            "accesskey": {
                "commands": ["list", "create", "get", "update", "delete"],
                "description": "Manage API access keys",
            },
        },
    },
    "number": {
        "container": "voipbin-number-mgr",
        "binary": "/app/bin/number-control",
        "subcommands": {
            "number": {
                "commands": ["list", "create", "get", "delete", "register", "update", "get-available"],
                "description": "Manage phone numbers",
            },
        },
    },
    "registrar": {
        "container": "voipbin-registrar-mgr",
        "binary": "/app/bin/registrar-control",
        "subcommands": {
            "extension": {
                "commands": ["list", "create", "get", "delete", "update"],
                "description": "Manage SIP extensions",
            },
            "trunk": {
                "commands": ["list", "create", "get", "delete", "update"],
                "description": "Manage SIP trunks",
            },
        },
    },
    "agent": {
        "container": "voipbin-agent-mgr",
        "binary": "/app/bin/agent-control",
        "subcommands": {
            "agent": {
                "commands": ["list", "create", "get", "delete", "login", "update-addresses", "update-basic-info", "update-password", "update-permission", "update-status", "update-tag-ids"],
                "description": "Manage agents",
            },
        },
    },
    "call": {
        "container": "voipbin-call-mgr",
        "binary": "/app/bin/call-control",
        "subcommands": {
            "call": {
                "commands": ["list", "get", "delete", "hangup", "update-status"],
                "description": "Manage calls",
            },
        },
    },
    "campaign": {
        "container": "voipbin-campaign-mgr",
        "binary": "/app/bin/campaign-control",
        "subcommands": {
            "campaign": {
                "commands": ["list", "create", "get", "delete", "update-basic-info", "update-status"],
                "description": "Manage campaigns",
            },
        },
    },
    "chat": {
        "container": "voipbin-chat-mgr",
        "binary": "/app/bin/chat-control",
        "subcommands": {
            "chat": {
                "commands": ["list", "create", "get", "delete", "add-participant", "remove-participant", "update-basic-info", "update-room-owner"],
                "description": "Manage chat rooms",
            },
        },
    },
    "conference": {
        "container": "voipbin-conference-mgr",
        "binary": "/app/bin/conference-control",
        "subcommands": {
            "conference": {
                "commands": ["list", "create", "get", "delete", "get-by-confbridge", "recording-start", "recording-stop", "terminating", "transcribe-start", "transcribe-stop", "update", "update-recording-id"],
                "description": "Manage conferences",
            },
        },
    },
    "conversation": {
        "container": "voipbin-conversation-mgr",
        "binary": "/app/bin/conversation-control",
        "subcommands": {
            "account": {
                "commands": ["list", "create", "get", "delete", "update"],
                "description": "Manage conversation accounts",
            },
            "conversation": {
                "commands": ["list", "get"],
                "description": "View conversations",
            },
            "message": {
                "commands": ["list", "get", "delete"],
                "description": "Manage conversation messages",
            },
        },
    },
    "flow": {
        "container": "voipbin-flow-mgr",
        "binary": "/app/bin/flow-control",
        "subcommands": {
            "flow": {
                "commands": ["list", "create", "get", "delete", "update", "update-actions", "action-get"],
                "description": "Manage flows",
            },
        },
    },
    "outdial": {
        "container": "voipbin-outdial-mgr",
        "binary": "/app/bin/outdial-control",
        "subcommands": {
            "outdial": {
                "commands": ["list", "create", "get", "delete", "update-basic-info", "update-campaign-id", "update-data"],
                "description": "Manage outdials",
            },
        },
    },
    "queue": {
        "container": "voipbin-queue-mgr",
        "binary": "/app/bin/queue-control",
        "subcommands": {
            "queue": {
                "commands": ["list", "create", "get", "delete", "update", "update-execute", "update-routing-method", "update-tag-ids"],
                "description": "Manage queues",
            },
            "queuecall": {
                "commands": ["list", "get", "delete", "get-by-reference"],
                "description": "Manage queue calls",
            },
        },
    },
    "route": {
        "container": "voipbin-route-mgr",
        "binary": "/app/bin/route-control",
        "subcommands": {
            "route": {
                "commands": ["list", "create", "get", "delete", "update", "dialroute-list", "list-by-target"],
                "description": "Manage routes",
            },
        },
    },
    "storage": {
        "container": "voipbin-storage-mgr",
        "binary": "/app/bin/storage-control",
        "subcommands": {
            "account": {
                "commands": ["list", "create", "get", "delete"],
                "description": "Manage storage accounts",
            },
            "file": {
                "commands": ["list", "create", "get", "delete"],
                "description": "Manage files",
            },
            "recording": {
                "commands": ["get", "delete"],
                "description": "Manage recordings",
            },
        },
    },
    "tag": {
        "container": "voipbin-tag-mgr",
        "binary": "/app/bin/tag-control",
        "subcommands": {
            "tag": {
                "commands": ["list", "create", "get", "delete", "update"],
                "description": "Manage tags",
            },
        },
    },
    "talk": {
        "container": "voipbin-talk-mgr",
        "binary": "/app/bin/talk-control",
        "subcommands": {
            "chat": {
                "commands": ["list", "create", "get", "delete", "update"],
                "description": "Manage talk chats",
            },
            "message": {
                "commands": ["list", "create", "get", "delete"],
                "description": "Manage talk messages",
            },
            "participant": {
                "commands": ["list", "add", "remove"],
                "description": "Manage chat participants",
            },
            "reaction": {
                "commands": ["add", "remove"],
                "description": "Manage message reactions",
            },
        },
    },
    "transfer": {
        "container": "voipbin-transfer-mgr",
        "binary": "/app/bin/transfer-control",
        "subcommands": {
            "transfer": {
                "commands": ["get-by-call", "get-by-groupcall", "service-start"],
                "description": "Manage transfers",
            },
        },
    },
    "tts": {
        "container": "voipbin-tts-mgr",
        "binary": "/app/bin/tts-control",
        "subcommands": {
            "tts": {
                "commands": ["create"],
                "description": "Text-to-speech operations",
            },
        },
    },
    "webhook": {
        "container": "voipbin-webhook-mgr",
        "binary": "/app/bin/webhook-control",
        "subcommands": {
            "webhook": {
                "commands": ["send-to-customer", "send-to-uri"],
                "description": "Webhook operations",
            },
        },
    },
    "hook": {
        "container": "voipbin-hook-mgr",
        "binary": "/app/bin/hook-control",
        "subcommands": {
            "hook": {
                "commands": ["send-conversation", "send-email", "send-message"],
                "description": "Test webhook operations",
            },
        },
    },
}

# Required arguments for sidecar commands
SIDECAR_REQUIRED_ARGS = {
    # billing commands
    ("billing", "account", "create"): ["customer-id"],
    ("billing", "account", "get"): ["id"],
    ("billing", "account", "delete"): ["id"],
    ("billing", "account", "add-balance"): ["id", "amount"],
    ("billing", "account", "subtract-balance"): ["id", "amount"],
    ("billing", "account", "update"): ["id", "name"],
    ("billing", "account", "update-payment-info"): ["id", "payment-method", "payment-type"],
    ("billing", "billing", "get"): ["id"],
    # customer commands
    ("customer", "customer", "create"): ["email"],
    ("customer", "customer", "get"): ["id"],
    ("customer", "customer", "delete"): ["id"],
    ("customer", "customer", "update"): ["id"],
    ("customer", "customer", "update-billing-account"): ["id", "billing-account-id"],
    # accesskey commands
    ("customer", "accesskey", "create"): ["customer-id", "name"],
    ("customer", "accesskey", "get"): ["id"],
    ("customer", "accesskey", "update"): ["id"],
    ("customer", "accesskey", "delete"): ["id"],
    # number commands
    ("number", "number", "list"): ["customer-id"],
    ("number", "number", "create"): ["customer-id", "number"],
    ("number", "number", "get"): ["id"],
    ("number", "number", "delete"): ["id"],
    ("number", "number", "register"): ["customer-id", "number"],
    ("number", "number", "update"): ["id"],
    # registrar extension commands
    ("registrar", "extension", "list"): ["customer-id"],
    ("registrar", "extension", "create"): ["customer-id", "username", "password"],
    ("registrar", "extension", "get"): ["id"],
    ("registrar", "extension", "delete"): ["id"],
    ("registrar", "extension", "update"): ["id"],
    # registrar trunk commands
    ("registrar", "trunk", "list"): ["customer-id"],
    ("registrar", "trunk", "create"): ["customer-id", "domain"],
    ("registrar", "trunk", "get"): ["id"],
    ("registrar", "trunk", "delete"): ["id"],
    ("registrar", "trunk", "update"): ["id"],
    # agent commands
    ("agent", "agent", "list"): ["customer-id"],
    ("agent", "agent", "create"): ["customer-id", "username", "password"],
    ("agent", "agent", "get"): ["id"],
    ("agent", "agent", "delete"): ["id"],
    ("agent", "agent", "login"): ["username", "password"],
    ("agent", "agent", "update-addresses"): ["id"],
    ("agent", "agent", "update-basic-info"): ["id"],
    ("agent", "agent", "update-password"): ["id", "password"],
    ("agent", "agent", "update-permission"): ["id"],
    ("agent", "agent", "update-status"): ["id", "status"],
    ("agent", "agent", "update-tag-ids"): ["id"],
    # call commands
    ("call", "call", "list"): ["customer-id"],
    ("call", "call", "get"): ["id"],
    ("call", "call", "delete"): ["id"],
    ("call", "call", "hangup"): ["id"],
    ("call", "call", "update-status"): ["id", "status"],
    # campaign commands
    ("campaign", "campaign", "list"): ["customer-id"],
    ("campaign", "campaign", "create"): ["customer-id", "name"],
    ("campaign", "campaign", "get"): ["id"],
    ("campaign", "campaign", "delete"): ["id"],
    ("campaign", "campaign", "update-basic-info"): ["id"],
    ("campaign", "campaign", "update-status"): ["id", "status"],
    # chat commands
    ("chat", "chat", "list"): ["customer-id"],
    ("chat", "chat", "create"): ["customer-id", "name"],
    ("chat", "chat", "get"): ["id"],
    ("chat", "chat", "delete"): ["id"],
    ("chat", "chat", "add-participant"): ["id", "agent-id"],
    ("chat", "chat", "remove-participant"): ["id", "agent-id"],
    ("chat", "chat", "update-basic-info"): ["id"],
    ("chat", "chat", "update-room-owner"): ["id", "owner-id"],
    # conference commands
    ("conference", "conference", "list"): ["customer-id"],
    ("conference", "conference", "create"): ["customer-id"],
    ("conference", "conference", "get"): ["id"],
    ("conference", "conference", "delete"): ["id"],
    ("conference", "conference", "get-by-confbridge"): ["confbridge-id"],
    ("conference", "conference", "recording-start"): ["id"],
    ("conference", "conference", "recording-stop"): ["id"],
    ("conference", "conference", "terminating"): ["id"],
    ("conference", "conference", "transcribe-start"): ["id"],
    ("conference", "conference", "transcribe-stop"): ["id"],
    ("conference", "conference", "update"): ["id"],
    ("conference", "conference", "update-recording-id"): ["id", "recording-id"],
    # conversation account commands
    ("conversation", "account", "list"): ["customer-id"],
    ("conversation", "account", "create"): ["customer-id"],
    ("conversation", "account", "get"): ["id"],
    ("conversation", "account", "delete"): ["id"],
    ("conversation", "account", "update"): ["id"],
    # conversation conversation commands
    ("conversation", "conversation", "list"): ["customer-id"],
    ("conversation", "conversation", "get"): ["id"],
    # conversation message commands
    ("conversation", "message", "list"): ["customer-id"],
    ("conversation", "message", "get"): ["id"],
    ("conversation", "message", "delete"): ["id"],
    # flow commands
    ("flow", "flow", "list"): ["customer-id"],
    ("flow", "flow", "create"): ["customer-id", "name"],
    ("flow", "flow", "get"): ["id"],
    ("flow", "flow", "delete"): ["id"],
    ("flow", "flow", "update"): ["id"],
    ("flow", "flow", "update-actions"): ["id"],
    ("flow", "flow", "action-get"): ["id", "action-id"],
    # outdial commands
    ("outdial", "outdial", "list"): ["customer-id"],
    ("outdial", "outdial", "create"): ["customer-id"],
    ("outdial", "outdial", "get"): ["id"],
    ("outdial", "outdial", "delete"): ["id"],
    ("outdial", "outdial", "update-basic-info"): ["id"],
    ("outdial", "outdial", "update-campaign-id"): ["id", "campaign-id"],
    ("outdial", "outdial", "update-data"): ["id"],
    # queue commands
    ("queue", "queue", "list"): ["customer-id"],
    ("queue", "queue", "create"): ["customer-id", "name"],
    ("queue", "queue", "get"): ["id"],
    ("queue", "queue", "delete"): ["id"],
    ("queue", "queue", "update"): ["id"],
    ("queue", "queue", "update-execute"): ["id"],
    ("queue", "queue", "update-routing-method"): ["id", "routing-method"],
    ("queue", "queue", "update-tag-ids"): ["id"],
    # queuecall commands
    ("queue", "queuecall", "list"): ["customer-id"],
    ("queue", "queuecall", "get"): ["id"],
    ("queue", "queuecall", "delete"): ["id"],
    ("queue", "queuecall", "get-by-reference"): ["reference-id"],
    # route commands
    ("route", "route", "list"): ["customer-id"],
    ("route", "route", "create"): ["customer-id"],
    ("route", "route", "get"): ["id"],
    ("route", "route", "delete"): ["id"],
    ("route", "route", "update"): ["id"],
    ("route", "route", "dialroute-list"): ["customer-id"],
    ("route", "route", "list-by-target"): ["target"],
    # storage account commands
    ("storage", "account", "list"): ["customer-id"],
    ("storage", "account", "create"): ["customer-id"],
    ("storage", "account", "get"): ["id"],
    ("storage", "account", "delete"): ["id"],
    # storage file commands
    ("storage", "file", "list"): ["customer-id"],
    ("storage", "file", "create"): ["customer-id"],
    ("storage", "file", "get"): ["id"],
    ("storage", "file", "delete"): ["id"],
    # storage recording commands
    ("storage", "recording", "get"): ["reference-id"],
    ("storage", "recording", "delete"): ["reference-id"],
    # tag commands
    ("tag", "tag", "list"): ["customer-id"],
    ("tag", "tag", "create"): ["customer-id", "name"],
    ("tag", "tag", "get"): ["id"],
    ("tag", "tag", "delete"): ["id"],
    ("tag", "tag", "update"): ["id"],
    # talk chat commands
    ("talk", "chat", "list"): ["customer-id"],
    ("talk", "chat", "create"): ["customer-id"],
    ("talk", "chat", "get"): ["id"],
    ("talk", "chat", "delete"): ["id"],
    ("talk", "chat", "update"): ["id"],
    # talk message commands
    ("talk", "message", "list"): ["chat-id"],
    ("talk", "message", "create"): ["chat-id"],
    ("talk", "message", "get"): ["id"],
    ("talk", "message", "delete"): ["id"],
    # talk participant commands
    ("talk", "participant", "list"): ["chat-id"],
    ("talk", "participant", "add"): ["chat-id", "agent-id"],
    ("talk", "participant", "remove"): ["chat-id", "agent-id"],
    # talk reaction commands
    ("talk", "reaction", "add"): ["message-id", "agent-id", "reaction"],
    ("talk", "reaction", "remove"): ["message-id", "agent-id", "reaction"],
    # transfer commands
    ("transfer", "transfer", "get-by-call"): ["call-id"],
    ("transfer", "transfer", "get-by-groupcall"): ["groupcall-id"],
    # tts commands
    ("tts", "tts", "create"): ["text"],
    # webhook commands
    ("webhook", "webhook", "send-to-customer"): ["customer-id"],
    ("webhook", "webhook", "send-to-uri"): ["uri"],
    # hook commands (test webhooks)
    ("hook", "hook", "send-conversation"): ["uri"],
    ("hook", "hook", "send-email"): ["uri"],
    ("hook", "hook", "send-message"): ["uri"],
}

# Commands that require delete confirmation
SIDECAR_DELETE_COMMANDS = [
    ("billing", "account", "delete"),
    ("customer", "customer", "delete"),
    ("customer", "accesskey", "delete"),
    ("number", "number", "delete"),
    ("registrar", "extension", "delete"),
    ("registrar", "trunk", "delete"),
    ("agent", "agent", "delete"),
    ("call", "call", "delete"),
    ("campaign", "campaign", "delete"),
    ("chat", "chat", "delete"),
    ("conference", "conference", "delete"),
    ("conversation", "account", "delete"),
    ("conversation", "message", "delete"),
    ("flow", "flow", "delete"),
    ("outdial", "outdial", "delete"),
    ("queue", "queue", "delete"),
    ("queue", "queuecall", "delete"),
    ("route", "route", "delete"),
    ("storage", "account", "delete"),
    ("storage", "file", "delete"),
    ("storage", "recording", "delete"),
    ("tag", "tag", "delete"),
    ("talk", "chat", "delete"),
    ("talk", "message", "delete"),
]

# Table columns for list commands (command_key -> [(display_name, json_key, width)])
SIDECAR_TABLE_COLUMNS = {
    ("billing", "account", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Balance", "balance", 10),
        ("Payment", "payment_type", 10),
    ],
    ("billing", "billing", "list"): [
        ("ID", "id", 36),
        ("Account ID", "account_id", 36),
        ("Type", "type", 15),
        ("Cost", "cost", 10),
    ],
    ("customer", "customer", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Email", "email", 30),
    ],
    ("customer", "accesskey", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 20),
        ("Customer ID", "customer_id", 36),
        ("Expire", "tm_expire", 19),
    ],
    ("number", "number", "list"): [
        ("ID", "id", 36),
        ("Number", "number", 16),
        ("Name", "name", 25),
    ],
    ("registrar", "extension", "list"): [
        ("ID", "id", 36),
        ("Extension", "extension", 12),
        ("Username", "username", 20),
        ("Realm", "realm", 40),
    ],
    ("registrar", "trunk", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 20),
        ("Username", "username", 20),
        ("Domain", "domain", 30),
    ],
    ("agent", "agent", "list"): [
        ("ID", "id", 36),
        ("Username", "username", 25),
        ("Name", "name", 25),
    ],
    ("call", "call", "list"): [
        ("ID", "id", 36),
        ("Direction", "direction", 10),
        ("Status", "status", 12),
        ("Source", "source", 20),
        ("Destination", "destination", 20),
    ],
    ("campaign", "campaign", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Status", "status", 12),
        ("Type", "type", 12),
    ],
    ("chat", "chat", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Type", "type", 12),
        ("Owner ID", "owner_id", 36),
    ],
    ("conference", "conference", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Status", "status", 12),
        ("Participants", "participant_count", 12),
    ],
    ("conversation", "account", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Type", "type", 12),
    ],
    ("conversation", "conversation", "list"): [
        ("ID", "id", 36),
        ("Account ID", "account_id", 36),
        ("Status", "status", 12),
    ],
    ("conversation", "message", "list"): [
        ("ID", "id", 36),
        ("Direction", "direction", 10),
        ("Status", "status", 12),
    ],
    ("flow", "flow", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Type", "type", 12),
    ],
    ("outdial", "outdial", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Status", "status", 12),
        ("Campaign ID", "campaign_id", 36),
    ],
    ("queue", "queue", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Routing", "routing_method", 15),
        ("Waiting", "waiting_count", 10),
    ],
    ("queue", "queuecall", "list"): [
        ("ID", "id", 36),
        ("Queue ID", "queue_id", 36),
        ("Status", "status", 12),
    ],
    ("route", "route", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Target", "target", 25),
        ("Priority", "priority", 10),
    ],
    ("storage", "account", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Type", "type", 12),
    ],
    ("storage", "file", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Type", "type", 12),
        ("Size", "size", 10),
    ],
    ("tag", "tag", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Color", "color", 10),
    ],
    ("talk", "chat", "list"): [
        ("ID", "id", 36),
        ("Name", "name", 25),
        ("Type", "type", 12),
    ],
    ("talk", "message", "list"): [
        ("ID", "id", 36),
        ("Sender ID", "sender_id", 36),
        ("Type", "type", 12),
    ],
    ("talk", "participant", "list"): [
        ("Agent ID", "agent_id", 36),
        ("Role", "role", 12),
        ("Joined", "tm_join", 20),
    ],
}

# Detail field mappings for get commands (command_key -> [(display_name, json_key)])
SIDECAR_DETAIL_FIELDS = {
    ("billing", "account", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Balance", "balance"),
        ("Payment Type", "payment_type"),
        ("Payment Method", "payment_method"),
        ("Created", "tm_create"),
    ],
    ("billing", "billing", "get"): [
        ("ID", "id"),
        ("Account ID", "account_id"),
        ("Customer ID", "customer_id"),
        ("Type", "type"),
        ("Cost", "cost"),
        ("Created", "tm_create"),
    ],
    ("customer", "customer", "get"): [
        ("ID", "id"),
        ("Name", "name"),
        ("Email", "email"),
        ("Detail", "detail"),
        ("Phone", "phone_number"),
        ("Address", "address"),
        ("Created", "tm_create"),
    ],
    ("customer", "accesskey", "get"): [
        ("ID", "id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Customer ID", "customer_id"),
        ("Token", "token"),
        ("Expire", "tm_expire"),
        ("Created", "tm_create"),
        ("Updated", "tm_update"),
    ],
    ("number", "number", "get"): [
        ("ID", "id"),
        ("Number", "number"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Customer ID", "customer_id"),
        ("Call Flow ID", "call_flow_id"),
        ("Message Flow ID", "message_flow_id"),
        ("Created", "tm_create"),
    ],
    ("registrar", "extension", "get"): [
        ("ID", "id"),
        ("Name", "name"),
        ("Extension", "extension"),
        ("Username", "username"),
        ("Realm", "realm"),
        ("Customer ID", "customer_id"),
        ("Created", "tm_create"),
    ],
    ("registrar", "trunk", "get"): [
        ("ID", "id"),
        ("Name", "name"),
        ("Username", "username"),
        ("Domain", "domain"),
        ("Customer ID", "customer_id"),
        ("Allowed IPs", "allowed_ips"),
        ("Created", "tm_create"),
    ],
    ("agent", "agent", "get"): [
        ("ID", "id"),
        ("Username", "username"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Customer ID", "customer_id"),
        ("Permission", "permission"),
        ("Status", "status"),
        ("Created", "tm_create"),
    ],
    ("call", "call", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Direction", "direction"),
        ("Status", "status"),
        ("Source", "source"),
        ("Destination", "destination"),
        ("Created", "tm_create"),
    ],
    ("campaign", "campaign", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Status", "status"),
        ("Type", "type"),
        ("Created", "tm_create"),
    ],
    ("chat", "chat", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Type", "type"),
        ("Owner ID", "owner_id"),
        ("Created", "tm_create"),
    ],
    ("conference", "conference", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Status", "status"),
        ("Confbridge ID", "confbridge_id"),
        ("Recording ID", "recording_id"),
        ("Created", "tm_create"),
    ],
    ("conversation", "account", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Type", "type"),
        ("Created", "tm_create"),
    ],
    ("conversation", "conversation", "get"): [
        ("ID", "id"),
        ("Account ID", "account_id"),
        ("Customer ID", "customer_id"),
        ("Status", "status"),
        ("Created", "tm_create"),
    ],
    ("conversation", "message", "get"): [
        ("ID", "id"),
        ("Conversation ID", "conversation_id"),
        ("Direction", "direction"),
        ("Status", "status"),
        ("Content", "content"),
        ("Created", "tm_create"),
    ],
    ("flow", "flow", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Type", "type"),
        ("Created", "tm_create"),
    ],
    ("outdial", "outdial", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Status", "status"),
        ("Campaign ID", "campaign_id"),
        ("Created", "tm_create"),
    ],
    ("queue", "queue", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Routing Method", "routing_method"),
        ("Waiting Count", "waiting_count"),
        ("Created", "tm_create"),
    ],
    ("queue", "queuecall", "get"): [
        ("ID", "id"),
        ("Queue ID", "queue_id"),
        ("Customer ID", "customer_id"),
        ("Status", "status"),
        ("Reference ID", "reference_id"),
        ("Created", "tm_create"),
    ],
    ("route", "route", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Target", "target"),
        ("Priority", "priority"),
        ("Created", "tm_create"),
    ],
    ("storage", "account", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Type", "type"),
        ("Created", "tm_create"),
    ],
    ("storage", "file", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Type", "type"),
        ("Size", "size"),
        ("URI", "uri"),
        ("Created", "tm_create"),
    ],
    ("tag", "tag", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Detail", "detail"),
        ("Color", "color"),
        ("Created", "tm_create"),
    ],
    ("talk", "chat", "get"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Name", "name"),
        ("Type", "type"),
        ("Created", "tm_create"),
    ],
    ("talk", "message", "get"): [
        ("ID", "id"),
        ("Chat ID", "chat_id"),
        ("Sender ID", "sender_id"),
        ("Type", "type"),
        ("Content", "content"),
        ("Created", "tm_create"),
    ],
    ("transfer", "transfer", "get-by-call"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Transferer Call ID", "transferer_call_id"),
        ("Groupcall ID", "groupcall_id"),
        ("Status", "status"),
        ("Created", "tm_create"),
    ],
    ("transfer", "transfer", "get-by-groupcall"): [
        ("ID", "id"),
        ("Customer ID", "customer_id"),
        ("Transferer Call ID", "transferer_call_id"),
        ("Groupcall ID", "groupcall_id"),
        ("Status", "status"),
        ("Created", "tm_create"),
    ],
}


def get_logo():
    """Return the combined ASCII logo with colors"""
    combined = []
    for icon_part, text_part in LOGO_LINES:
        if "Connect & Collaborate" in text_part:
            combined.append(f"{white(icon_part)}{gray(text_part)}")
        elif "S A N D B O X" in text_part:
            combined.append(f"{white(icon_part)}{bold(gray(text_part))}")
        elif text_part:
            combined.append(f"{white(icon_part)}{bold(text_part)}")
        else:
            combined.append(f"{white(icon_part)}")

    return '\n'.join(combined)


def detect_dashboard_state():
    """Detect the current system state for dashboard display"""
    env_exists = os.path.exists(".env")

    if not env_exists:
        return "not_initialized"

    # Check for running containers
    output = run_cmd("docker compose ps --format '{{.Name}}' 2>/dev/null")
    if output and output.strip():
        return "running"

    return "stopped"


def get_quick_status():
    """Get quick status for running state dashboard"""
    # Get running containers
    output = run_cmd("docker compose ps --format '{{.Name}}\t{{.Status}}' 2>/dev/null")

    running = []
    stopped = []

    if output:
        for line in output.strip().split('\n'):
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) >= 2:
                name = parts[0].replace("voipbin-", "")
                status = parts[1]
                if "up" in status.lower():
                    running.append(name)
                else:
                    stopped.append(name)

    # Get total services
    total_output = run_cmd("docker compose config --services 2>/dev/null")
    total = len(total_output.strip().split('\n')) if total_output else 0

    # Check DNS status
    coredns_running = "dns" in running or "voipbin-dns" in run_cmd("docker ps --format '{{.Names}}' 2>/dev/null")
    resolv_configured = "nameserver 127.0.0.1" in run_cmd("cat /etc/resolv.conf 2>/dev/null")
    dns_active = coredns_running and resolv_configured

    # Check network status
    kamailio_int = run_cmd("ip addr show kamailio-int 2>/dev/null | grep -oP 'inet [\\d./]+' | head -1")
    network_configured = bool(kamailio_int)

    # Get host IP for endpoints
    host_ip = run_cmd("grep '^HOST_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or "localhost"

    return {
        "running_count": len(running),
        "total_count": total,
        "stopped_services": stopped[:3],  # Show max 3
        "dns_active": dns_active,
        "network_configured": network_configured,
        "host_ip": host_ip,
    }


def show_welcome_dashboard():
    """Display the welcome dashboard based on system state"""
    state = detect_dashboard_state()

    # Print logo
    print()
    print(get_logo())
    print()

    # Separator
    separator = "━" * 75
    print(separator)
    print()

    if state == "not_initialized":
        # Warning triangle in yellow
        warning = f"""{yellow("      /\\")}
{yellow("     /  \\")}       Not initialized. Let's get started:
{yellow("    / !! \\")}
{yellow("   /______\\")}        1. {bold("init")}          Generate .env and SSL certificates
                   2. {bold("nano .env")}     Add your API keys (GCP, OpenAI, etc.)
                   3. {bold("start")}         Launch all services

                Quick start: {green("init")} → {green("nano .env")} → {green("start")}"""
        print(warning)
        print()
        print(separator)
        print()
        print(f"Type '{bold('help')}' for all commands, '{bold('exit')}' to quit.")

    elif state == "stopped":
        # Circle icon in gray
        stopped_msg = f"""{gray("     ○○○")}        Services stopped. Ready to start.
{gray("    ○   ○")}
{gray("     ○○○")}           Type '{green("start")}' to launch all services
                   Type '{blue("status")}' for detailed configuration"""
        print(stopped_msg)
        print()
        print(separator)
        print()
        print(f"Type '{bold('help')}' for all commands, '{bold('exit')}' to quit.")

    else:  # running
        status = get_quick_status()

        # Status indicators
        running_indicator = green("●") if status["running_count"] > 0 else gray("○")
        dns_indicator = green("●") if status["dns_active"] else gray("○")
        dns_status = "active" if status["dns_active"] else "not configured"
        network_indicator = green("●") if status["network_configured"] else gray("○")
        network_status = "configured" if status["network_configured"] else "not configured"

        # Helper to pad string accounting for ANSI codes
        def pad(text, width, colored_text=None):
            """Pad text to width, using colored_text for display if provided"""
            display = colored_text if colored_text else text
            padding = width - len(text)
            return display + (' ' * max(0, padding))

        # Build content (plain text for width calc, colored for display)
        svc1_plain = f"* {status['running_count']}/{status['total_count']} running"
        svc1_color = f"{running_indicator} {status['running_count']}/{status['total_count']} running"

        svc2_plain = ""
        svc2_color = ""
        if status["stopped_services"]:
            stopped_str = ', '.join(status['stopped_services'][:3])
            svc2_plain = f"* {stopped_str}"
            svc2_color = f"{gray('○')} {gray(stopped_str)}"

        svc4_plain = f"DNS: * {dns_status}"
        svc4_color = f"DNS: {dns_indicator} {dns_status}"

        svc5_plain = f"Network: * {network_status}"
        svc5_color = f"Network: {network_indicator} {network_status}"

        col_width = 36

        print(f"  {'Services':<34}  {'Endpoints'}")
        print(f"  {'─' * 34}  {'─' * 33}")
        print(f"  {pad(svc1_plain, col_width, svc1_color)}  Admin   {blue('http://localhost:3003')}")
        print(f"  {pad(svc2_plain, col_width, svc2_color)}  API     {blue('https://localhost:8443')}")
        print(f"  {'':<{col_width}}  Meet    {blue('http://localhost:3004')}")
        print(f"  {pad(svc4_plain, col_width, svc4_color)}  Talk    {blue('http://localhost:3005')}")
        print(f"  {pad(svc5_plain, col_width, svc5_color)}  SIP     {blue('sip.voipbin.test:5060')}")
        print()
        print(separator)
        print()
        print(f"Type '{bold('help')}' for all commands, '{bold('status')}' for details, '{bold('exit')}' to quit.")

    print()


# =============================================================================
# Configuration Management
# =============================================================================

class Config:
    def __init__(self):
        self.data = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    saved = json.load(f)
                    self.data.update(saved)
            except (json.JSONDecodeError, IOError):
                pass

    def save(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except IOError as e:
            print(red(f"Error saving config: {e}"))

    def get(self, key, default=None):
        # Check environment variable first
        env_key = f"VOIPBIN_{key.upper()}"
        if env_key in os.environ:
            return os.environ[env_key]
        return self.data.get(key, default)

    def set(self, key, value):
        # Try to convert to appropriate type
        if key in self.data:
            orig_type = type(self.data[key])
            try:
                if orig_type == bool:
                    value = value.lower() in ("true", "1", "yes")
                elif orig_type == int:
                    value = int(value)
            except (ValueError, AttributeError):
                pass
        self.data[key] = value
        self.save()

    def reset(self):
        self.data = DEFAULT_CONFIG.copy()
        self.save()


# =============================================================================
# Command Execution Helpers
# =============================================================================

def run_cmd(cmd, capture=True, check=False):
    """Run a shell command and return output"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=capture,
            text=True,
            check=check
        )
        return result.stdout.strip() if capture else ""
    except subprocess.CalledProcessError as e:
        return e.stderr.strip() if e.stderr else str(e)


def docker_exec(container, cmd, interactive=False):
    """Execute command in a docker container"""
    if interactive:
        os.system(f"docker exec -it {container} {cmd}")
        return ""
    return run_cmd(f"docker exec {container} {cmd}")


def get_running_containers():
    """Get list of running voipbin containers"""
    output = run_cmd("docker compose ps --format '{{.Name}}' 2>/dev/null")
    if output:
        return [c.replace("voipbin-", "") for c in output.split("\n") if c.startswith("voipbin-")]
    return []


def get_all_services():
    """Get list of all services from docker-compose"""
    output = run_cmd("docker compose config --services 2>/dev/null")
    return output.split("\n") if output else []


def read_env_file_var(project_dir, name, default=""):
    """Read VAR= from <project_dir>/.env (last occurrence wins).

    Pure text processing - the file is never sourced, mirroring
    common.sh:get_env_var. Returns default when the file or key is absent.
    """
    env_path = os.path.join(project_dir, ".env")
    value = default
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith(f"{name}="):
                    # .strip() drops \r\n (CRLF-saved .env) and surrounding
                    # whitespace, mirroring common.sh:get_env_var.
                    value = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return value


# =============================================================================
# Docker Hub API Helpers
# =============================================================================

DOCKERHUB_API_BASE = "https://hub.docker.com/v2/repositories"
DOCKERHUB_MAX_WORKERS = 10
DOCKERHUB_RETRY_COUNT = 3
DOCKERHUB_RETRY_DELAY = 2


def dockerhub_get(url, retries=DOCKERHUB_RETRY_COUNT):
    """Make a GET request to Docker Hub API with retry logic"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate limited
                wait_time = DOCKERHUB_RETRY_DELAY * (attempt + 1)
                time.sleep(wait_time)
                continue
            elif e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(DOCKERHUB_RETRY_DELAY)
                continue
            raise
    return None


def get_image_latest_digest(image_name):
    """Get the digest of the 'latest' tag for an image"""
    # image_name: voipbin/bin-api-manager
    url = f"{DOCKERHUB_API_BASE}/{image_name}/tags/latest"
    try:
        data = dockerhub_get(url)
        if data and "digest" in data:
            return data["digest"]
    except Exception:
        pass
    return None


def get_image_tags(image_name):
    """Get all tags for an image from Docker Hub"""
    url = f"{DOCKERHUB_API_BASE}/{image_name}/tags?page_size=100"
    try:
        data = dockerhub_get(url)
        if data and "results" in data:
            return data["results"]
    except Exception:
        pass
    return []


def find_commit_sha_tag(image_name, target_digest):
    """Find the commit-SHA tag that matches the given digest"""
    tags = get_image_tags(image_name)
    for tag in tags:
        tag_name = tag.get("name", "")
        tag_digest = tag.get("digest", "")
        # Skip 'latest' and find commit-SHA tag with matching digest
        if tag_name != "latest" and tag_digest == target_digest:
            return tag_name
    return None


def resolve_image_tag(image_name):
    """Resolve an image's latest tag to its commit-SHA tag

    Returns: dict with image, tag, digest, error
    """
    result = {"image": image_name, "tag": None, "digest": None, "error": None}

    try:
        # Get latest digest
        digest = get_image_latest_digest(image_name)
        if not digest:
            result["error"] = "Could not get latest digest"
            return result

        result["digest"] = digest

        # Find matching commit-SHA tag
        tag = find_commit_sha_tag(image_name, digest)
        if tag:
            result["tag"] = tag
        else:
            result["error"] = "No commit-SHA tag found"
    except Exception as e:
        result["error"] = str(e)

    return result


def resolve_image_tags_parallel(images, progress_callback=None):
    """Resolve multiple images' tags in parallel

    Args:
        images: list of image names (e.g., ['voipbin/bin-api-manager', ...])
        progress_callback: optional function(current, total, image_name) for progress

    Returns: list of results from resolve_image_tag
    """
    results = []
    total = len(images)
    completed = 0

    with ThreadPoolExecutor(max_workers=DOCKERHUB_MAX_WORKERS) as executor:
        futures = {executor.submit(resolve_image_tag, img): img for img in images}

        for future in as_completed(futures):
            image = futures[future]
            completed += 1

            if progress_callback:
                progress_callback(completed, total, image)

            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append({
                    "image": image,
                    "tag": None,
                    "digest": None,
                    "error": str(e)
                })

    return results


def get_voipbin_images_from_compose(project_dir="."):
    """Extract voipbin/* images and their service names from docker-compose.yml

    Returns:
        tuple: (list of unique image names, dict mapping image -> list of service names)
    """
    compose_file = os.path.join(project_dir, "docker-compose.yml")
    images = []
    image_to_services = {}  # image -> list of service names

    if not os.path.exists(compose_file):
        print(f"{yellow(f'Warning: docker-compose.yml not found at {compose_file}')}")
        return images, image_to_services

    try:
        with open(compose_file, "r") as f:
            compose = yaml.safe_load(f)

        services = compose.get("services", {})
        for service_name, config in services.items():
            image = config.get("image", "")
            if image.startswith("voipbin/"):
                # Remove tag if present
                image_base = image.split(":")[0]
                if image_base not in images:
                    images.append(image_base)
                if image_base not in image_to_services:
                    image_to_services[image_base] = []
                image_to_services[image_base].append(service_name)
    except Exception as e:
        print(f"{yellow(f'Warning: Error reading docker-compose.yml: {e}')}")

    return images, image_to_services


# =============================================================================
# Sidecar Command Helpers
# =============================================================================

def check_container_running(container):
    """Check if a container is running"""
    output = run_cmd(f"docker ps --filter 'name={container}' --format '{{{{.Names}}}}'")
    return container in output if output else False


def run_sidecar_command(container, binary, args, verbose=False):
    """
    Execute a sidecar command via docker exec.
    Returns (success, data_or_error) tuple.
    data_or_error is parsed JSON on success, error message on failure.
    """
    # Check container is running
    if not check_container_running(container):
        return False, f"Service unavailable: {container} is not running.\n  Run 'start' to launch services."

    # Build command
    args_str = " ".join(f'--{k} "{v}"' if isinstance(v, str) and " " in v else f"--{k} {v}"
                        for k, v in args.items() if v is not None)
    cmd = f'docker exec {container} {binary} {args_str} 2>&1'

    output = run_cmd(cmd)
    if not output:
        return True, []

    # Parse output - filter log lines, info lines, and extract JSON
    lines = output.split("\n")
    json_lines = []
    log_lines = []
    info_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Check if it's a log line (JSON with severity field)
        if stripped.startswith("{") and '"severity"' in stripped:
            log_lines.append(stripped)
        # Check if it's JSON data (starts with { or [ or is part of JSON array/object)
        elif stripped.startswith("{") or stripped.startswith("[") or stripped.startswith('"') or stripped == "]" or stripped == "}," or stripped == "}":
            json_lines.append(line)  # Keep original indentation for JSON
        # Check if it's an indented JSON line (part of pretty-printed JSON)
        elif line.startswith("  ") and ('"' in stripped or stripped in ["{", "}", "},", "]", "],"]):
            json_lines.append(line)
        else:
            # Info/status line (e.g., "Retrieving extensions...")
            info_lines.append(stripped)

    # Show logs and info if verbose
    if verbose:
        for log_line in log_lines:
            try:
                log = json.loads(log_line)
                severity = log.get("severity", "INFO")
                message = log.get("message", "")
                if severity == "ERROR":
                    print(f"{red('[ERROR]')} {message}")
                elif severity == "DEBUG":
                    print(f"{gray('[DEBUG]')} {message}")
                else:
                    print(f"[{severity}] {message}")
            except json.JSONDecodeError:
                print(log_line)
        for info_line in info_lines:
            print(f"{gray('[INFO]')} {info_line}")
        if log_lines or info_lines:
            print()

    # Parse JSON result
    json_str = "\n".join(json_lines)
    if not json_str:
        return True, []

    try:
        data = json.loads(json_str)
        return True, data
    except json.JSONDecodeError:
        # Check for common error patterns
        if "not found" in json_str.lower():
            return False, "Resource not found."
        if "error" in json_str.lower():
            return False, json_str
        return False, f"Invalid response: {json_str}"


def parse_sidecar_args(args):
    """
    Parse --flag value style arguments from a list.
    Returns dict of flag -> value.
    """
    result = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:]
            # Check if next arg is a value (not another flag)
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def prompt_missing_args(command_key, provided_args):
    """
    Check for missing required args and prompt for them.
    Returns updated args dict or None if cancelled.
    """
    required = SIDECAR_REQUIRED_ARGS.get(command_key, [])
    updated_args = provided_args.copy()

    for arg in required:
        if arg not in updated_args or updated_args[arg] is None:
            print(f"\nMissing required argument: {yellow('--' + arg)}\n")
            try:
                value = input(f"Enter {arg}: ").strip()
                if not value:
                    print("Cancelled.")
                    return None

                # Check if user entered flags (e.g., "--customer-id abc123" or full command)
                if "--" in value:
                    # Parse as flag arguments and merge into updated_args
                    parsed = parse_sidecar_args(value.split())
                    updated_args.update(parsed)
                else:
                    updated_args[arg] = value
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                return None

    return updated_args


def confirm_delete(resource_type, resource_data):
    """
    Show delete confirmation dialog.
    Returns True if confirmed, False otherwise.
    """
    print(f"\n{yellow('⚠')} Delete {resource_type}?")

    # Show resource details
    if isinstance(resource_data, dict):
        for key in ["name", "email", "number", "id"]:
            if key in resource_data and resource_data[key]:
                display_key = key.replace("_", " ").title()
                print(f"  {display_key}:  {resource_data[key]}")

    print(f"\n  {red('This action cannot be undone.')}\n")

    try:
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm == "yes":
            return True
        print("Cancelled.")
        return False
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return False


def format_table(data, columns):
    """
    Format list of dicts as a table.
    columns: list of (display_name, json_key, width)
    """
    if not data:
        return

    # Calculate column widths (at least header width)
    col_widths = []
    for display_name, json_key, default_width in columns:
        max_val_width = max((len(str(row.get(json_key, ""))) for row in data), default=0)
        col_widths.append(max(len(display_name), min(max_val_width, default_width)))

    # Print header
    header = "  ".join(display_name.ljust(col_widths[i])
                       for i, (display_name, _, _) in enumerate(columns))
    separator = "  ".join("─" * w for w in col_widths)

    print(separator)
    print(header)
    print(separator)

    # Print rows
    for row in data:
        values = []
        for i, (_, json_key, _) in enumerate(columns):
            val = row.get(json_key, "")
            if val is None:
                val = "-"
            elif isinstance(val, float):
                val = f"{val:.2f}"
            val_str = str(val)[:col_widths[i]].ljust(col_widths[i])
            values.append(val_str)
        print("  ".join(values))

    print(separator)


def format_details(data, fields):
    """
    Format a single dict as key-value pairs.
    fields: list of (display_name, json_key)
    """
    if not data:
        return

    # Find max label width
    max_label = max(len(display_name) for display_name, _ in fields)

    print("─" * 40)
    for display_name, json_key in fields:
        val = data.get(json_key, "")
        if val is None or val == "":
            val = "-"
        elif isinstance(val, float):
            val = f"{val:.2f}"
        # Truncate timestamps
        if "tm_" in json_key and isinstance(val, str) and len(val) > 19:
            val = val[:19]
        print(f"  {display_name}:{' ' * (max_label - len(display_name) + 2)}{val}")
    print()


# =============================================================================
# CLI Commands
# =============================================================================

class VoIPBinCLI:
    def __init__(self):
        self.config = Config()
        self.context = None  # None = top-level, or "asterisk", "kamailio", "db", "api"
        self.api_token = None
        self.running = True
        # Exit code of the last `doctor` run; the non-interactive dispatch in
        # main() propagates it (the 0/1 exit code is part of the machine
        # contract for agents scripting `sudo ./voipbin doctor`, design §7).
        self.last_doctor_rc = 0

        # Disable colors if configured
        if not self.config.get("colors", True):
            Colors.disable()

        # Command handlers
        self.commands = {
            "help": self.cmd_help,
            "?": self.cmd_help,
            "status": self.cmd_status,
            "ps": self.cmd_status,
            "doctor": self.cmd_doctor,
            "start": self.cmd_start,
            "stop": self.cmd_stop,
            "restart": self.cmd_restart,
            "logs": self.cmd_logs,
            "ast": self.cmd_ast,
            "asterisk": self.cmd_ast,
            "kam": self.cmd_kam,
            "kamailio": self.cmd_kam,
            "db": self.cmd_db,
            "mysql": self.cmd_db,
            "api": self.cmd_api,
            "ext": self.cmd_ext,
            "extension": self.cmd_ext,
            "billing": self.cmd_billing,
            "customer": self.cmd_customer,
            "number": self.cmd_number,
            "registrar": self.cmd_registrar,
            "agent": self.cmd_agent,
            "call": self.cmd_call,
            "campaign": self.cmd_campaign,
            "chat": self.cmd_chat,
            "conference": self.cmd_conference,
            "conversation": self.cmd_conversation,
            "flow": self.cmd_flow,
            "outdial": self.cmd_outdial,
            "queue": self.cmd_queue,
            "route": self.cmd_route,
            "storage": self.cmd_storage,
            "tag": self.cmd_tag,
            "talk": self.cmd_talk,
            "transfer": self.cmd_transfer,
            "tts": self.cmd_tts,
            "webhook": self.cmd_webhook,
            "config": self.cmd_config,
            "dns": self.cmd_dns,
            "certs": self.cmd_certs,
            "network": self.cmd_network,
            "init": self.cmd_init,
            "clean": self.cmd_clean,
            "update": self.cmd_update,
            "backup": self.cmd_backup,
            "restore": self.cmd_restore,
            "rollback": self.cmd_rollback,
            "version": self.cmd_version,
            "exit": self.cmd_exit,
            "quit": self.cmd_exit,
            "clear": self.cmd_clear,
        }

        # Help text for commands
        self.help_text = {
            "status": ("Show service status", "status"),
            "ps": ("Alias for status", "ps"),
            "doctor": ("Diagnose install and runtime state (read-only)", "doctor\n  Runs scripts/doctor.sh at any stage (pre-install, pre-start, running).\n  Prints one 'DOCTOR <name>: pass|fail|warn|skip' line per check and a\n  'FIX <name>: <command>' prescription for every failure.\n  Exit code 0 only when no check failed."),
            "start": ("Start services", "start [service] [--no-pin]\n  start              Start all services (pins versions on first run)\n  start --no-pin     Start without version pinning\n  start api-manager  Start specific service"),
            "stop": ("Stop services", "stop [service] [--all]\n  stop            Stop app services (keeps db/redis/mq/dns running)\n  stop kamailio   Stop specific service\n  stop --all      Stop all services including infrastructure"),
            "restart": ("Restart services (Asterisk owner+proxy sidecars always paired)", "restart [service]"),
            "logs": ("View service logs", "logs [-f] <service>\n  logs api-manager     Last 50 lines\n  logs -f api-manager  Follow logs (Ctrl+C to stop)"),
            "ast": ("Asterisk CLI", "ast [command]\n  ast                          Enter Asterisk context\n  ast pjsip show endpoints     Run single command"),
            "kam": ("Kamailio kamcmd", "kam [command]\n  kam              Enter Kamailio context\n  kam ul.dump      Run single command"),
            "db": ("MySQL queries", "db [query]\n  db                                    Enter database context\n  db SELECT * FROM extensions LIMIT 5   Run single query"),
            "api": ("REST API client", "api [method] [path] [data]\n  api                        Enter API context\n  api get /v1.0/extensions   Run single API call"),
            "extension": ("Manage extensions", "extension <command>\n  extension list                       List all extensions\n  extension create <ext> <pass> [name] Create extension\n  extension delete <id>                Delete extension"),
            "billing": ("Billing management", "billing <subcommand> <action> [options]\n  Type 'billing help' for more details"),
            "customer": ("Customer management", "customer <action> [options]\n  Type 'customer help' for more details"),
            "number": ("Phone number management", "number <action> [options]\n  Type 'number help' for more details"),
            "registrar": ("Registrar management", "registrar <subcommand> <action> [options]\n  Type 'registrar help' for more details"),
            "agent": ("Agent management", "agent <action> [options]\n  Type 'agent help' for more details"),
            "call": ("Call management", "call <action> [options]\n  Type 'call help' for more details"),
            "campaign": ("Campaign management", "campaign <action> [options]\n  Type 'campaign help' for more details"),
            "chat": ("Chat room management", "chat <action> [options]\n  Type 'chat help' for more details"),
            "conference": ("Conference management", "conference <action> [options]\n  Type 'conference help' for more details"),
            "conversation": ("Conversation management", "conversation <subcommand> <action> [options]\n  Type 'conversation help' for more details"),
            "flow": ("Flow management", "flow <action> [options]\n  Type 'flow help' for more details"),
            "outdial": ("Outdial management", "outdial <action> [options]\n  Type 'outdial help' for more details"),
            "queue": ("Queue management", "queue <subcommand> <action> [options]\n  Type 'queue help' for more details"),
            "route": ("Route management", "route <action> [options]\n  Type 'route help' for more details"),
            "storage": ("Storage management", "storage <subcommand> <action> [options]\n  Type 'storage help' for more details"),
            "tag": ("Tag management", "tag <action> [options]\n  Type 'tag help' for more details"),
            "talk": ("Talk management", "talk <subcommand> <action> [options]\n  Type 'talk help' for more details"),
            "transfer": ("Transfer management", "transfer <action> [options]\n  Type 'transfer help' for more details"),
            "tts": ("Text-to-speech", "tts <action> [options]\n  Type 'tts help' for more details"),
            "webhook": ("Webhook operations", "webhook <action> [options]\n  Type 'webhook help' for more details"),
            "hook": ("Test webhook operations", "hook <action> [options]\n  Type 'hook help' for more details"),
            "config": ("View/set configuration", "config [key] [value]\n  config                Show all settings\n  config log_lines 100  Set value\n  config reset          Reset to defaults"),
            "dns": ("DNS setup for SIP domains", "dns [status|list|setup|regenerate|test]\n  dns status       Check DNS configuration\n  dns list         List all DNS domains and their purposes\n  dns setup        Setup DNS forwarding to CoreDNS (requires sudo)\n  dns regenerate   Regenerate Corefile and restart CoreDNS (requires sudo)\n  dns test         Test domain resolution"),
            "certs": ("Manage SSL certificates", "certs [status|trust]\n  certs status   Check certificate configuration\n  certs trust    Install mkcert CA for browser-trusted certificates"),
            "network": ("Manage VoIP network interfaces", "network [status|setup|teardown]\n  network status                       Show current network configuration\n  network setup                        Setup VoIP network interfaces\n  network setup --external-ip X.X.X.X  Setup with fixed external IP\n  network teardown                     Remove VoIP network interfaces"),
            "init": ("Initialize sandbox", "init\n  Runs initialization script to generate .env and certificates"),
            "clean": ("Cleanup sandbox", "clean [options]\n  clean --containers  Remove app containers (keeps db/redis/mq/dns)\n  clean --volumes     Remove docker volumes (database, recordings, redis, rabbitmq)\n  clean --images      Remove docker images\n  clean --network     Teardown VoIP network interfaces\n  clean --dns         Remove DNS configuration\n  clean --purge       Remove generated files (.env, certs, configs)\n  clean --all         All of the above (full reset)"),
            "update": ("Update sandbox", "update [subcommand] [--check] [--skip-backup]\n  update               Pull latest Docker images + restart services\n                       (pinned repo: pulls pinned digests only; real upgrades = 'update all')\n  update --check       Dry-run: show available image updates\n  update scripts       Update scripts/configs from GitHub (with backup)\n  update scripts --check  Dry-run: show what would change\n  update all           Full upgrade. Pinned repo: backup -> git pull -> compose pull\n                       -> migrate -> up -d -> verify (safe-ordered)\n  update all --skip-backup  Skip the automatic pre-upgrade backup (pinned repo)\n  update all --check   Dry-run: describe the upgrade plan, change nothing"),
            "backup": ("Create a full data backup", "backup\n  Dumps MySQL (all databases), archives recording volumes, and copies\n  .env/certs/versions.lock into backups/<timestamp>/ (chmod 700/600).\n  Runs while services are up (mysqldump --single-transaction).\n  Retention: last 7 backups are kept.\n  NOTE: backups contain your full .env secrets in plaintext - copy them\n  off-host (rsync/rclone) for real disaster recovery."),
            "restore": ("Restore data from a backup (DESTRUCTIVE)", "restore <timestamp> --force\n  restore              List available backups\n  restore <ts> --force Restore MySQL dump + recordings + config from backups/<ts>/\n  DESTRUCTIVE: overwrites current DATA. Requires --force AND all services\n  stopped except db and redis. Flushes Redis after import.\n  (restore = DATA from backups/. rollback = image override history, unpinned repos only.)"),
            "rollback": ("Rollback to previous version", "rollback [N]\n  rollback             Interactive version selection\n  rollback N           Restore version by number (e.g., rollback 2)\n  rollback --list      Show available versions\n  NOTE: rollback replays docker-compose.override.yml IMAGE snapshots and only\n  applies to UNPINNED repos (refused when versions.lock exists).\n  For DATA restore use 'restore <ts>' instead."),
            "version": ("Show pinned image versions", "version [--json]\n  version              Show version table\n  version --json       Output as JSON for scripting"),
            "exit": ("Exit CLI", "exit"),
            "clear": ("Clear screen", "clear"),
        }

    def get_prompt(self):
        """Get the current prompt string

        Note: Uses \001 and \002 (RL_PROMPT_START_IGNORE/END_IGNORE) to wrap
        ANSI escape codes so readline correctly calculates prompt length.
        Without these markers, cursor movement breaks after history lookup.
        """
        # Readline-safe escape sequences
        rl_bold = f"\001{Colors.BOLD}\002"
        rl_blue = f"\001{Colors.BLUE}\002"
        rl_reset = f"\001{Colors.RESET}\002"

        if self.context:
            ctx_name = {
                "asterisk": "asterisk",
                "kamailio": "kam",
                "db": "db",
                "api": "api"
            }.get(self.context, self.context)
            return f"{rl_bold}voipbin{rl_reset}({rl_blue}{ctx_name}{rl_reset})> "
        return f"{rl_bold}voipbin{rl_reset}> "

    def parse_input(self, line):
        """Parse input line into command and arguments"""
        line = line.strip()
        if not line:
            return None, []

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()

        return parts[0].lower(), parts[1:]

    def run_in_context(self, line):
        """Handle input when in a context"""
        if line.lower() in ("exit", "quit", ".."):
            self.context = None
            return

        if self.context == "asterisk":
            self.asterisk_cmd(line)
        elif self.context == "kamailio":
            self.kamailio_cmd(line)
        elif self.context == "db":
            self.db_cmd(line)
        elif self.context == "api":
            self.api_cmd(line)

    # -------------------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------------------

    def cmd_help(self, args):
        """Show help"""
        if args:
            cmd = args[0].lower()
            if cmd in self.help_text:
                desc, usage = self.help_text[cmd]
                print(f"\n{bold(cmd)} - {desc}\n")
                print(f"Usage:\n  {usage}\n")
            else:
                print(f"Unknown command: {cmd}")
            return

        print(f"""
{bold('VoIPBin Sandbox CLI')}

{blue('Service Commands:')}
  status, ps        Show service status
  doctor            Diagnose install and runtime state (read-only)
  start [service]   Start services
  stop [--all]      Stop app services (--all for everything)
  restart [service] Restart services
  logs <service>    View logs (-f to follow)

{blue('Setup & Cleanup:')}
  init              Initialize sandbox (.env, certs)
  update [options]  Update (scripts, all, --check) - 'update all' = full pinned upgrade
  backup            Full data backup (MySQL + recordings + config) to backups/<ts>/
  restore <ts>      Restore DATA from a backup (DESTRUCTIVE, requires --force + stopped services)
  rollback          Rollback image override history (UNPINNED repos only, --list)
  clean [options]   Cleanup (--containers, --volumes, --images, --network, --dns, --purge, --all)

{blue('Contexts:')}
  ast [cmd]         Asterisk CLI
  kam [cmd]         Kamailio kamcmd
  db [query]        MySQL queries
  api               REST API client

{blue('Data Management:')}
  extension         Extension management
  agent             Agent management
  billing           Billing and account management
  call              Call management
  campaign          Campaign management
  chat              Chat room management
  conference        Conference management
  conversation      Conversation and messaging
  customer          Customer management
  flow              Flow management
  number            Phone number management
  outdial           Outdial management
  queue             Queue management
  registrar         Extension and trunk management
  route             Route management
  storage           Storage and file management
  tag               Tag management
  talk              Talk chat management
  transfer          Transfer management
  tts               Text-to-speech
  webhook           Webhook operations
  hook              Test webhook operations

{blue('Infrastructure:')}
  dns               DNS setup for SIP domains
  certs             Manage SSL certificates
  network           Manage VoIP network interfaces

{blue('Other:')}
  config            View/set configuration
  clear             Clear screen
  help [command]    Show help
  exit, quit        Exit CLI

Type 'help <command>' for detailed usage.
""")

    # -------------------------------------------------------------------------
    # Mode awareness (dual-mode DNS, VOIP-1275 design §2.10). URLs and mode
    # gates derive from .env; the base domain is never hardcoded here.
    # -------------------------------------------------------------------------

    def _project_dir(self):
        return self.config.get("project_dir", ".")

    def _base_domain(self):
        return read_env_file_var(self._project_dir(), "BASE_DOMAIN") or "voipbin.test"

    def _domain_mode(self):
        """internal | external; a missing key maps to internal (legacy rule)."""
        return read_env_file_var(self._project_dir(), "DOMAIN_MODE") or "internal"

    def cmd_status(self, args):
        """Show service status"""
        output = run_cmd("docker compose ps --format '{{.Name}}\t{{.Status}}' 2>/dev/null")
        if not output:
            print(yellow("No services running. Run 'start' to start services."))
            return

        # Get host IP for endpoints; base domain drives all composed URLs
        host_ip = run_cmd("grep '^HOST_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or "localhost"
        base_domain = self._base_domain()

        # Parse services into a dict
        services = {}
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0].replace("voipbin-", "")
                status = parts[1]
                services[name] = status

        # Key services with endpoints (service_name: (label, endpoint, credentials))
        # Web services use Docker port mapping on HOST_IP
        endpoint_services = {
            "admin": ("Admin Console", f"http://admin.{base_domain}:3003", None),
            "api-mgr": ("API Manager", f"https://api.{base_domain}:8443", None),
            "mq": ("RabbitMQ", "http://localhost:15672", f"{os.environ.get('RABBITMQ_DEFAULT_USER', 'guest')} / {os.environ.get('RABBITMQ_DEFAULT_PASS', 'guest')}"),
            "db": ("MySQL", "localhost:3306", f"root / {os.environ.get('MYSQL_ROOT_PASSWORD', 'root_password')}"),
        }

        # SIP/VoIP endpoints (shown separately)
        voip_endpoints = {
            "kamailio": [
                ("SIP (UDP/TCP)", f"{host_ip}:5060"),
                ("SIP (TLS)", f"{host_ip}:5061"),
                ("SIP (WSS)", f"wss://{host_ip}:443"),
            ],
            "rtpengine": [("RTPEngine", f"{host_ip}:22222")],
            "ast-registrar": [("Asterisk Registrar", f"{host_ip}:5082")],
            "ast-call": [("Asterisk Call", f"{host_ip}:5080")],
        }

        # Count running services
        running = sum(1 for s in services.values() if "up" in s.lower())
        total = len(services)

        # Helper to get status icon
        def get_status_icon(svc_name):
            status = services.get(svc_name, "")
            if "up" in status.lower():
                return green("●")
            elif "restarting" in status.lower():
                return yellow("◐")
            elif status:
                return red("○")
            return gray("○")

        # Print Web/Management endpoints
        print(f"\n{bold('Web Interfaces')}")
        print("-" * 70)
        for svc_name, (label, endpoint, creds) in endpoint_services.items():
            line = f"  {get_status_icon(svc_name)} {label:<20} {blue(endpoint)}"
            if creds:
                line += f"  {gray('(' + creds + ')')}"
            print(line)

        # Print VoIP endpoints
        print(f"\n{bold('VoIP Endpoints')}")
        print("-" * 70)
        for svc_name, endpoints in voip_endpoints.items():
            status_icon = get_status_icon(svc_name)
            for i, (label, endpoint) in enumerate(endpoints):
                if i == 0:
                    print(f"  {status_icon} {label:<20} {blue(endpoint)}")
                else:
                    print(f"    {label:<20} {blue(endpoint)}")

        # Print services summary
        print(f"\n{bold('Services')} ({running}/{total} running)")
        print("-" * 60)

        # Group by status
        running_svcs = []
        warning_svcs = []
        stopped_svcs = []

        # Services already shown in endpoints sections
        shown_services = set(endpoint_services.keys()) | set(voip_endpoints.keys())

        for name, status in sorted(services.items()):
            if name in shown_services:
                continue  # Already shown above
            if "up" in status.lower():
                running_svcs.append(name)
            elif "restarting" in status.lower():
                warning_svcs.append((name, status))
            else:
                stopped_svcs.append((name, status))

        # Show running services compactly (3 per line)
        if running_svcs:
            print(f"  {green('●')} Running: ", end="")
            for i, name in enumerate(running_svcs):
                if i > 0 and i % 3 == 0:
                    print(f"\n             ", end="")
                print(f"{name:<20}", end="")
            print()

        # Show warning services
        for name, status in warning_svcs:
            print(f"  {yellow('◐')} {name}: {yellow(status)}")

        # Show stopped services
        for name, status in stopped_svcs:
            print(f"  {red('○')} {name}: {red(status)}")

        # Show configuration status
        print(f"\n{bold('Configuration')}")
        print("-" * 60)

        # Helper to check env var
        def get_env_var(var_name):
            result = run_cmd(f"grep '^{var_name}=' .env 2>/dev/null | cut -d'=' -f2 | head -1")
            return result.strip() if result else ""

        # Check GCP credentials
        gcp_creds_path = get_env_var("GOOGLE_APPLICATION_CREDENTIALS")
        if gcp_creds_path:
            # Check if it's the dummy file
            is_dummy = False
            if "dummy" in gcp_creds_path.lower():
                is_dummy = True
            elif os.path.exists(gcp_creds_path):
                # Check file contents for dummy marker
                try:
                    with open(gcp_creds_path, 'r') as f:
                        content = f.read()
                        if "dummy-project" in content:
                            is_dummy = True
                except:
                    pass

            if is_dummy:
                print(f"  {yellow('!')} GCP Credentials:    {yellow('dummy')} {gray('(TTS/storage disabled)')}")
            else:
                print(f"  {green('●')} GCP Credentials:    {green('configured')}")
        else:
            print(f"  {gray('○')} GCP Credentials:    {gray('not set')}")

        # AI Services
        openai_key = get_env_var("OPENAI_API_KEY")
        if openai_key:
            print(f"  {green('●')} OpenAI:             {green('configured')}")
        else:
            print(f"  {gray('○')} OpenAI:             {gray('not set')}")

        # Check other AI services (group them)
        ai_services = []
        if get_env_var("DEEPGRAM_API_KEY"):
            ai_services.append("Deepgram")
        if get_env_var("CARTESIA_API_KEY"):
            ai_services.append("Cartesia")
        if get_env_var("ELEVENLABS_API_KEY"):
            ai_services.append("ElevenLabs")

        if ai_services:
            print(f"  {green('●')} AI Services:        {green(', '.join(ai_services))}")

        # Telephony Providers
        print(f"\n  {bold('Telephony')}")

        twilio_sid = get_env_var("TWILIO_SID")
        twilio_key = get_env_var("TWILIO_API_KEY")
        if twilio_sid and twilio_key:
            print(f"  {green('●')} Twilio:             {green('configured')}")
        else:
            print(f"  {gray('○')} Twilio:             {gray('not set')}")

        telnyx_key = get_env_var("TELNYX_API_KEY")
        if telnyx_key:
            print(f"  {green('●')} Telnyx:             {green('configured')}")
        else:
            print(f"  {gray('○')} Telnyx:             {gray('not set')}")

        # Email Providers
        print(f"\n  {bold('Email')}")

        sendgrid_key = get_env_var("SENDGRID_API_KEY")
        if sendgrid_key:
            print(f"  {green('●')} SendGrid:           {green('configured')}")
        else:
            print(f"  {gray('○')} SendGrid:           {gray('not set')}")

        mailgun_key = get_env_var("MAILGUN_API_KEY")
        if mailgun_key:
            print(f"  {green('●')} Mailgun:            {green('configured')}")
        else:
            print(f"  {gray('○')} Mailgun:            {gray('not set')}")

        # AWS (transcription)
        print(f"\n  {bold('Cloud Services')}")

        aws_access = get_env_var("AWS_ACCESS_KEY")
        aws_secret = get_env_var("AWS_SECRET_KEY")
        if aws_access and aws_secret:
            print(f"  {green('●')} AWS:                {green('configured')} {gray('(transcription)')}")
        else:
            print(f"  {gray('○')} AWS:                {gray('not set')}")

        # DNS Configuration
        print(f"\n{bold('DNS Domains')} (*.{base_domain} → {host_ip})")
        print("-" * 60)

        # Check if CoreDNS is running and DNS is configured
        coredns_running = "voipbin-dns" in run_cmd("docker ps --format '{{.Names}}' 2>/dev/null")
        dns_configured = "nameserver 127.0.0.1" in run_cmd("cat /etc/resolv.conf 2>/dev/null")

        if coredns_running and dns_configured:
            print(f"  {green('●')} DNS Status: {green('active')}")
        elif coredns_running:
            print(f"  {yellow('!')} DNS Status: {yellow('CoreDNS running, but resolv.conf not configured')}")
        else:
            print(f"  {red('○')} DNS Status: {red('CoreDNS not running')}")

        print(f"\n  {bold('Web Services')} (Docker port mapping on {host_ip})")
        print(f"    {'https://api.' + base_domain + ':8443':<34}API Manager")
        print(f"    {'http://admin.' + base_domain + ':3003':<34}Admin Console")
        print(f"    {'http://meet.' + base_domain + ':3004':<34}Meet")
        print(f"    {'http://talk.' + base_domain + ':3005':<34}Talk")

        print(f"\n  {bold('SIP Services')} (Kamailio: {host_ip})")
        print(f"    {'sip.' + base_domain:<34}SIP proxy")
        print(f"    {'sip-service.' + base_domain:<34}SIP proxy (alias)")
        print(f"    {'pstn.' + base_domain:<34}PSTN gateway")
        print(f"    {'trunk.' + base_domain:<34}SIP trunking")
        print(f"    {'*.registrar.' + base_domain:<34}SIP registration")

        print(f"\n  Run 'dns list' for full domain reference.")
        print()

    def _create_initial_version_pins(self, project_dir):
        """Create initial version pins on first start"""
        override_file = os.path.join(project_dir, "docker-compose.override.yml")
        versions_dir = os.path.join(project_dir, ".voipbin-versions")

        # Get list of voipbin images and their service mappings
        images, image_to_services = get_voipbin_images_from_compose(project_dir)
        if not images:
            print(yellow(f"  No voipbin images found. Skipping version pinning."))
            print(gray(f"  (project_dir: {project_dir})"))
            return

        print(f"  Found {len(images)} voipbin images")
        print(f"  Resolving tags from Docker Hub...")

        # Progress callback
        def progress(current, total, image):
            short_name = image.split("/")[-1] if "/" in image else image
            print(f"\r  Resolving... [{current}/{total}] {short_name:<30}", end="", flush=True)

        # Resolve tags in parallel
        results = resolve_image_tags_parallel(images, progress_callback=progress)
        print()  # New line after progress

        # Separate successful and failed resolutions
        resolved = []
        warnings = []
        for r in results:
            if r["tag"]:
                resolved.append(r)
            else:
                warnings.append(r)

        if warnings:
            print(yellow(f"  {len(warnings)} images could not be resolved (will use :latest)"))

        if not resolved:
            print(yellow("  No images resolved. Will use :latest tags."))
            return

        print(f"  {green('✓')} Resolved {len(resolved)}/{len(images)} images")

        # Generate override file
        override_content = self._generate_override_content(resolved, warnings, image_to_services)
        with open(override_file, "w") as f:
            f.write(override_content)

        # Save to history as first version
        os.makedirs(versions_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        history_file = os.path.join(versions_dir, f"{timestamp}.yml")
        shutil.copy2(override_file, history_file)

        print(f"  {green('✓')} Version pins created")
        print(f"  {green('✓')} Saved to rollback history")

    def _check_ticker_replica_guard(self):
        """Phase 1 (address externalization) guard: bin-billing-manager and
        bin-ai-manager run time.Ticker-based periodic jobs (monthly token
        top-up, failed-event retry, hourly batch job) with NO leader election
        or distributed lock (verified against monorepo main.go). Running N>1
        replicas of either service today duplicate-executes those jobs. This
        is an ordinary application of the no-premature-hardening policy: we
        warn loudly rather than build a distributed lock nobody has asked for
        yet. See docs/plans/2026-07-05-production-grade-horizontal-scale-design.md
        §1.4 for the full analysis.
        """
        RISKY_SERVICES = ("billing-manager", "ai-manager")
        output = run_cmd("docker compose ps --format json 2>/dev/null")
        if not output:
            return
        entries = []
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                entries.extend(parsed)
            else:
                entries.append(parsed)
        counts = {}
        for e in entries:
            svc = e.get("Service") or ""
            if svc:
                counts[svc] = counts.get(svc, 0) + 1
        for svc in RISKY_SERVICES:
            n = counts.get(svc, 0)
            if n > 1:
                print(
                    f"{yellow('WARNING:')} {n} replicas of '{svc}' are running. "
                    f"This service runs a time.Ticker-based periodic job "
                    f"(e.g. monthly billing top-up / failed-event retry) with "
                    f"NO leader election or distributed lock. Each replica will "
                    f"independently fire the same job on its own schedule, "
                    f"duplicating the work (e.g. duplicate billing top-ups). "
                    f"Do not scale '{svc}' above 1 replica until this is fixed "
                    f"in the monorepo (bin-billing-manager/bin-ai-manager)."
                )

    def cmd_start(self, args):
        """Start services"""
        # Check for --no-pin flag
        no_pin = "--no-pin" in args
        args = [a for a in args if a != "--no-pin"]

        service = args[0] if args else ""

        if service:
            # Start specific service
            print(f"Starting {service}...")
            result = run_cmd(f"docker compose up -d {service} 2>&1")
            if result:
                print(result)
            print(green("✓ Done"))
            self._check_ticker_replica_guard()
        else:
            # Full startup
            script_dir = self.config.get("project_dir", ".")

            # Ensure version pinning on first start
            override_file = os.path.join(script_dir, "docker-compose.override.yml")
            if not os.path.exists(override_file) and not no_pin:
                print(f"\n{blue('==>')} First start detected - pinning image versions...")
                try:
                    self._create_initial_version_pins(script_dir)
                except Exception as e:
                    print(f"{red('Error creating version pins:')} {e}")
                    print(yellow("Continuing without version pinning..."))
            elif no_pin:
                print(f"\n{yellow('Skipping version pinning (--no-pin flag)')}")

            # Use start.sh for all setup (network, DNS, etc.)
            script_path = os.path.join(script_dir, "scripts", "start.sh")

            if os.path.exists(script_path):
                print("Running full startup (network, DNS, services)...")
                os.system(script_path)
            else:
                # Fallback to docker compose
                print("Starting all services...")
                result = run_cmd("docker compose up -d 2>&1")
                if result:
                    print(result)
                print(green("✓ Done"))
            self._check_ticker_replica_guard()

    def cmd_stop(self, args):
        """Stop services"""
        # Infrastructure services to keep running by default
        INFRA_SERVICES = {"coredns", "redis", "rabbitmq", "db"}

        stop_all = "--all" in args
        args = [a for a in args if a != "--all"]
        service = args[0] if args else ""

        if service:
            # Stop specific service
            if service == "coredns" or service == "dns":
                print(yellow("Warning: Stopping CoreDNS may cause DNS resolution to fail."))
                print(yellow("         Run 'dns setup' after restarting to restore DNS."))
            print(f"Stopping {service}...")
            result = run_cmd(f"docker compose stop {service} 2>&1")
            if result:
                print(result)
            print(green("✓ Done"))
        elif stop_all:
            # Stop all containers including infrastructure
            print("Stopping all services (including infrastructure)...")
            result = run_cmd("docker compose stop 2>&1")
            if result:
                print(result)
            print(green("✓ All services stopped"))
        else:
            # Stop only app containers, keep infrastructure running
            print("Stopping app services (keeping infrastructure)...")
            all_services = run_cmd("docker compose ps --services 2>/dev/null") or ""
            if all_services:
                services_to_stop = []
                for svc in all_services.split('\n'):
                    svc = svc.strip()
                    if svc and svc not in INFRA_SERVICES:
                        services_to_stop.append(svc)

                if services_to_stop:
                    result = run_cmd(f"docker compose stop {' '.join(services_to_stop)} 2>&1")
                    if result:
                        print(result)
                    print(green(f"✓ Stopped {len(services_to_stop)} app services"))
                    print(gray(f"  Infrastructure still running: {', '.join(sorted(INFRA_SERVICES))}"))
                else:
                    print("No app services to stop")
            else:
                print("No services running")

    # asterisk-*-proxy sidecars run with `network_mode: "service:<owner>"`,
    # sharing the owner's kernel network namespace so ARI/AMI resolve over
    # localhost. `docker restart <owner>` gives the owner a NEW namespace but
    # does NOT restart the sidecar, which stays attached to the old, now-dead
    # namespace forever (ARI connect: connection refused). See VOIP-1237.
    # Owner and sidecar MUST always be restarted together.
    ASTERISK_SIDECAR_PAIRS = {
        "asterisk-call": "asterisk-call-proxy",
        "asterisk-conference": "asterisk-conference-proxy",
        "asterisk-registrar": "asterisk-registrar-proxy",
    }

    def _restart_asterisk_pair(self, owner, proxy, health_timeout=60):
        """Restart an Asterisk owner container, wait for it to report
        healthy, THEN restart its network-namespace-sharing proxy sidecar.

        Order matters (VOIP-1237, live-verified): `docker compose restart
        owner proxy` in one call does NOT guarantee owner-before-proxy
        ordering — Compose can (and in testing did) restart the proxy up to
        ~13s before the owner container finishes coming back up, so the
        proxy re-attaches to the OLD namespace and the bug reproduces anyway.
        Restarting the owner first, waiting for its healthcheck, and only
        then restarting the proxy is the ordering that actually fixes ARI
        registration (confirmed via `ari show apps` showing 'voipbin' and
        matching /proc/self/ns/net inodes on both containers).

        IMPORTANT: `owner`/`proxy` here are docker-compose SERVICE names
        (e.g. "asterisk-call"), which are not necessarily the actual
        container name — some of these services set an explicit
        `container_name:` (e.g. "voipbin-ast-call") that `docker inspect`
        needs verbatim; others have no override and get Compose's default
        `<project>-<service>-<n>` name. `docker inspect <service-name>`
        would silently fail against either of those in most projects. Always
        resolve the real container ID via `docker compose ps -q <service>`
        first, which understands compose service names regardless of any
        `container_name:` override.
        """
        result = run_cmd(f"docker compose restart {owner} 2>&1")
        if result:
            print(result)

        container_id = (run_cmd(f"docker compose ps -q {owner} 2>/dev/null") or "").strip()
        # Defensive: if a service were ever scaled to multiple replicas,
        # `docker compose ps -q` would return multiple newline-separated
        # IDs. That raw string is interpolated into a shell command below,
        # so an embedded newline could be misread as a command separator.
        # None of the three Asterisk services are scaled today, but take
        # only the first ID to stay safe regardless.
        container_id = container_id.split()[0] if container_id else ""
        if not container_id:
            print(
                yellow(
                    f"  Warning: could not resolve a container ID for service "
                    f"'{owner}' via 'docker compose ps -q'; skipping healthcheck "
                    f"wait and restarting {proxy} after a short settle delay."
                )
            )
            time.sleep(5)
            result = run_cmd(f"docker compose restart {proxy} 2>&1")
            if result:
                print(result)
            return

        print(f"  Waiting for {owner} to become healthy...")
        waited = 0
        while waited < health_timeout:
            status = run_cmd(
                f"docker inspect {container_id} --format '{{{{.State.Health.Status}}}}' 2>&1"
            )
            status = (status or "").strip()
            if status == "healthy":
                break
            # Two legitimate "no real health status" cases collapse into the
            # same fallback: (a) older Docker versions print the literal
            # string "<no value>" when a container has no HEALTHCHECK; (b)
            # current Docker (verified on 29.x) instead errors out of the Go
            # template with "template parsing error: ... map has no entry
            # for key \"Health\"", which is a genuine inspect failure message
            # but means the same thing here — there is no Health status to
            # poll. Either way we cannot wait on a real health transition,
            # so fall back to a short fixed settle time instead of spinning
            # for the full timeout.
            no_health_signal = (
                status == ""
                or status.startswith("<no value>")
                or "no entry for key" in status.lower()
            )
            if no_health_signal:
                time.sleep(5)
                break
            if "error" in status.lower():
                # A different, unexpected inspect failure (e.g. the
                # container disappeared mid-poll) — surface it rather than
                # silently treating it as "no healthcheck defined".
                print(yellow(f"  Note: unexpected 'docker inspect' output for {owner} ({container_id}): {status!r}"))
                time.sleep(5)
                break
            time.sleep(2)
            waited += 2
        else:
            print(
                yellow(
                    f"  Warning: {owner} did not report healthy within "
                    f"{health_timeout}s, restarting {proxy} anyway."
                )
            )

        result = run_cmd(f"docker compose restart {proxy} 2>&1")
        if result:
            print(result)

    def cmd_restart(self, args):
        """Restart services (Asterisk owner containers auto-pull in their
        network-namespace-sharing proxy sidecar, see VOIP-1237)"""
        service = args[0] if args else ""

        if service:
            # If the requested service is an Asterisk owner container, force
            # its network-namespace-sharing proxy sidecar to restart AFTER it
            # comes back healthy, so the sidecar never survives into an
            # orphaned namespace (VOIP-1237).
            paired_proxy = self.ASTERISK_SIDECAR_PAIRS.get(service)
            if paired_proxy:
                print(
                    f"Restarting {service}, then its sidecar {paired_proxy} "
                    f"once healthy (required: they share a network "
                    f"namespace, see VOIP-1237)..."
                )
                self._restart_asterisk_pair(service, paired_proxy)
            else:
                print(f"Restarting {service}...")
                result = run_cmd(f"docker compose restart {service} 2>&1")
                if result:
                    print(result)

            print(green("✓ Done"))
        else:
            # Full-stack restart: `docker compose restart` (no args) does not
            # guarantee any owner-before-sidecar ordering, so an
            # asterisk-*-proxy can still end up restarted before (or without)
            # its owner and be left in a dead namespace. Restart every
            # Asterisk owner+proxy pair in the correct order first, then
            # restart everything else in one shot.
            print("Restarting all services...")

            paired_services = set()
            for owner, proxy in self.ASTERISK_SIDECAR_PAIRS.items():
                paired_services.add(owner)
                paired_services.add(proxy)
                print(f"  -> {owner}, then {proxy} once healthy (VOIP-1237)")
                self._restart_asterisk_pair(owner, proxy)

            all_services = run_cmd("docker compose ps --services 2>/dev/null") or ""
            remaining = [
                svc.strip() for svc in all_services.split("\n")
                if svc.strip() and svc.strip() not in paired_services
            ]
            if remaining:
                result = run_cmd(f"docker compose restart {' '.join(remaining)} 2>&1")
                if result:
                    print(result)

            print(green("✓ Done"))

    def cmd_logs(self, args):
        """View service logs"""
        if not args:
            print("Usage: logs [-f] <service>")
            print("Services:", ", ".join(get_all_services()[:10]))
            return

        follow = False
        service = args[0]

        if args[0] == "-f":
            follow = True
            if len(args) < 2:
                print("Usage: logs -f <service>")
                return
            service = args[1]

        lines = self.config.get("log_lines", 50)

        if follow:
            print(f"Following logs for {service}... (Ctrl+C to stop)")
            try:
                os.system(f"docker compose logs -f --tail={lines} {service}")
            except KeyboardInterrupt:
                print()
        else:
            result = run_cmd(f"docker compose logs --tail={lines} {service} 2>&1")
            print(result)

    def cmd_ast(self, args):
        """Asterisk CLI"""
        if not args:
            self.context = "asterisk"
            print(f"Entering Asterisk context. Type 'exit' to return.")
            return

        cmd = " ".join(args)
        self.asterisk_cmd(cmd)

    def asterisk_cmd(self, cmd):
        """Execute Asterisk command"""
        container = self.config.get("asterisk_container")
        result = docker_exec(container, f'asterisk -rx "{cmd}"')
        print(result)

    def cmd_kam(self, args):
        """Kamailio kamcmd"""
        if not args:
            self.context = "kamailio"
            print(f"Entering Kamailio context. Type 'exit' to return.")
            return

        cmd = " ".join(args)
        self.kamailio_cmd(cmd)

    def kamailio_cmd(self, cmd):
        """Execute Kamailio command"""
        container = self.config.get("kamailio_container")
        result = docker_exec(container, f'kamcmd {cmd}')
        print(result)

    def cmd_db(self, args):
        """MySQL queries"""
        if not args:
            self.context = "db"
            print(f"Entering database context. Type 'exit' to return.")
            print(f"Database: bin_manager")
            return

        query = " ".join(args)
        self.db_cmd(query)

    def db_cmd(self, query):
        """Run a MySQL query"""
        container = self.config.get("db_container")
        # Password expands INSIDE the container from its own
        # MYSQL_ROOT_PASSWORD env var (injected via docker-compose.yml) via
        # `sh -c`, never built into a host-visible docker exec /
        # subprocess.run(shell=True) argv (where it would be visible to any
        # local user via `ps aux`) -- matches the idiom used by
        # dns_test/run_migrations/_do_backup/cmd_restore elsewhere in this
        # file, and scripts/migrate.sh's MYSQL_IN_DB.
        query_escaped = (
            query.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )
        inner_cmd = (
            'mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-root_password}" '
            f'bin_manager -e "{query_escaped}"'
        )
        result = docker_exec(container, f"sh -c {shlex.quote(inner_cmd)}")
        print(result)

    def cmd_api(self, args):
        """REST API client"""
        if not args:
            self.context = "api"
            print(f"Entering API context. Type 'exit' to return.")
            print(f"Commands: login <email>, get <path>, post <path> <json>")
            if self.api_token:
                print(green("✓ Logged in"))
            return

        cmd = " ".join(args)
        self.api_cmd(cmd)

    def api_cmd(self, line):
        """Execute API command"""
        parts = line.split(None, 2)
        if not parts:
            return

        cmd = parts[0].lower()

        if cmd == "login":
            email = parts[1] if len(parts) > 1 else "admin@localhost"
            password = getpass.getpass(f"Password for {email}: ")
            self.api_login(email, password)
        elif cmd in ("get", "post", "put", "delete"):
            if len(parts) < 2:
                print(f"Usage: {cmd} <path> [data]")
                return
            path = parts[1]
            data = parts[2] if len(parts) > 2 else None
            self.api_request(cmd.upper(), path, data)
        else:
            print(f"Unknown API command: {cmd}")
            print("Commands: login, get, post, put, delete")

    def api_login(self, email, password):
        """Login to API"""
        host = self.config.get("api_host")
        port = self.config.get("api_port")

        data = json.dumps({"username": email, "password": password})
        result = run_cmd(
            f'curl -sk -X POST "https://{host}:{port}/auth/login" '
            f'-H "Content-Type: application/json" '
            f"-d '{data}'"
        )

        try:
            resp = json.loads(result)
            if "token" in resp:
                self.api_token = resp["token"]
                print(green("✓ Login successful"))
            else:
                print(red(f"✗ Login failed: {resp.get('message', result)}"))
        except json.JSONDecodeError:
            print(red(f"✗ Login failed: {result}"))

    def api_request(self, method, path, data=None):
        """Make API request"""
        if not self.api_token:
            print(yellow("Not logged in. Use 'login <email>' first."))
            return

        host = self.config.get("api_host")
        port = self.config.get("api_port")

        if not path.startswith("/"):
            path = "/" + path

        cmd = f'curl -sk -X {method} "https://{host}:{port}{path}" '
        cmd += f'-H "Authorization: Bearer {self.api_token}" '
        cmd += '-H "Content-Type: application/json" '

        if data:
            cmd += f"-d '{data}'"

        result = run_cmd(cmd)

        try:
            parsed = json.loads(result)
            print(json.dumps(parsed, indent=2))
        except json.JSONDecodeError:
            print(result)

    def cmd_ext(self, args):
        """Manage extensions"""
        if not args:
            print("Usage: extension list|create|delete")
            return

        subcmd = args[0].lower()

        if subcmd == "list":
            self.ext_list()
        elif subcmd == "create":
            if len(args) < 3:
                print("Usage: extension create <extension> <password> [name]")
                return
            ext = args[1]
            password = args[2]
            name = args[3] if len(args) > 3 else f"Extension {ext}"
            self.ext_create(ext, password, name)
        elif subcmd == "delete":
            if len(args) < 2:
                print("Usage: extension delete <id>")
                return
            self.ext_delete(args[1])
        else:
            print(f"Unknown subcommand: {subcmd}")

    def ensure_login(self):
        """Ensure we have an API token"""
        if self.api_token:
            return True

        print("Logging in as admin@localhost...")
        self.api_login("admin@localhost", "admin@localhost")
        return self.api_token is not None

    def ext_list(self):
        """List extensions"""
        if not self.ensure_login():
            return

        host = self.config.get("api_host")
        port = self.config.get("api_port")

        result = run_cmd(
            f'curl -sk "https://{host}:{port}/v1.0/extensions" '
            f'-H "Authorization: Bearer {self.api_token}"'
        )

        try:
            data = json.loads(result)
            if isinstance(data, list):
                print(f"\n{'Extension':<12} {'Name':<20} {'ID'}")
                print("-" * 60)
                for ext in data:
                    print(f"{ext.get('extension', 'N/A'):<12} {ext.get('name', ''):<20} {gray(ext.get('id', ''))}")
                print()
            else:
                print(json.dumps(data, indent=2))
        except json.JSONDecodeError:
            print(result)

    def ext_create(self, extension, password, name):
        """Create extension"""
        if not self.ensure_login():
            return

        host = self.config.get("api_host")
        port = self.config.get("api_port")

        data = json.dumps({"extension": extension, "password": password, "name": name})
        result = run_cmd(
            f'curl -sk -X POST "https://{host}:{port}/v1.0/extensions" '
            f'-H "Authorization: Bearer {self.api_token}" '
            f'-H "Content-Type: application/json" '
            f"-d '{data}'"
        )

        try:
            resp = json.loads(result)
            if "id" in resp:
                print(green(f"✓ Created extension {extension}"))
                print(f"  ID: {gray(resp['id'])}")
            else:
                print(red(f"✗ Failed: {resp.get('message', result)}"))
        except json.JSONDecodeError:
            print(result)

    def ext_delete(self, ext_id):
        """Delete extension"""
        if not self.ensure_login():
            return

        host = self.config.get("api_host")
        port = self.config.get("api_port")

        result = run_cmd(
            f'curl -sk -X DELETE "https://{host}:{port}/v1.0/extensions/{ext_id}" '
            f'-H "Authorization: Bearer {self.api_token}"'
        )

        if not result or result == "{}":
            print(green(f"✓ Deleted extension {ext_id}"))
        else:
            print(result)

    # -------------------------------------------------------------------------
    # Sidecar Commands (billing, customer, number, registrar, agent)
    # -------------------------------------------------------------------------

    def cmd_billing(self, args):
        """Billing management (accounts and billing records)"""
        if not args or args[0] in ("help", "-h", "--help"):
            self._show_billing_help(args[1:] if len(args) > 1 else [])
            return

        subcmd = args[0].lower()

        if subcmd not in ("account", "billing"):
            print(f"{red('✗')} Unknown subcommand: {subcmd}")
            print("  Available: account, billing")
            print("  Type 'billing help' for usage.")
            return

        if len(args) < 2 or args[1] in ("help", "-h", "--help"):
            self._show_billing_subcommand_help(subcmd, args[2:] if len(args) > 2 else [])
            return

        action = args[1].lower()
        cmd_args = parse_sidecar_args(args[2:])
        verbose = cmd_args.pop("verbose", False)

        self._run_billing_command(subcmd, action, cmd_args, verbose)

    def _show_billing_help(self, args):
        """Show billing command help"""
        print(f"""
{bold('Billing Management')}

{blue('Available Commands:')}
  billing account        Manage billing accounts
  billing billing        View billing records

Type 'billing <subcommand> help' for more details.
""")

    def _show_billing_subcommand_help(self, subcmd, args):
        """Show help for billing subcommand"""
        if subcmd == "account":
            if args and args[0] not in ("help", "-h", "--help"):
                self._show_billing_action_help(subcmd, args[0])
                return
            print(f"""
{bold('Billing Account Management')}

{blue('Available Commands:')}
  list                List billing accounts
  create              Create a new billing account
  get                 Get account details by ID
  delete              Delete an account
  add-balance         Add balance to an account
  subtract-balance    Subtract balance from an account
  update              Update account basic info
  update-payment-info Update account payment info

{blue('Usage:')} billing account <command> [options]

{blue('Examples:')}
  billing account list
  billing account list --customer-id abc123
  billing account create --customer-id abc123 --name "Main Account"
  billing account get --id xyz789
  billing account add-balance --id xyz789 --amount 100
  billing account update --id xyz789 --name "Updated Name"
  billing account update-payment-info --id xyz789 --payment-type prepaid --payment-method "credit card"
""")
        elif subcmd == "billing":
            if args and args[0] not in ("help", "-h", "--help"):
                self._show_billing_action_help(subcmd, args[0])
                return
            print(f"""
{bold('Billing Records')}

{blue('Available Commands:')}
  list             List billing records
  get              Get billing record by ID

{blue('Usage:')} billing billing <command> [options]

{blue('Examples:')}
  billing billing list
  billing billing list --customer-id abc123 --account-id xyz789
  billing billing get --id record123
""")

    def _show_billing_action_help(self, subcmd, action):
        """Show help for specific billing action"""
        help_info = {
            ("account", "list"): ("List billing accounts", [], [("customer-id", "Filter by customer ID"), ("limit", "Max results (default: 100)")]),
            ("account", "create"): ("Create a new billing account", [("customer-id", "Customer ID")], [("name", "Account name"), ("detail", "Description"), ("payment-type", "Payment type (default: prepaid)")]),
            ("account", "get"): ("Get account details", [("id", "Account ID")], []),
            ("account", "delete"): ("Delete an account", [("id", "Account ID")], []),
            ("account", "add-balance"): ("Add balance to an account", [("id", "Account ID"), ("amount", "Amount to add")], []),
            ("account", "subtract-balance"): ("Subtract balance from an account", [("id", "Account ID"), ("amount", "Amount to subtract")], []),
            ("account", "update"): ("Update account basic info", [("id", "Account ID"), ("name", "Account name")], [("detail", "Description")]),
            ("account", "update-payment-info"): ("Update account payment info", [("id", "Account ID"), ("payment-method", "Payment method"), ("payment-type", "Payment type")], []),
            ("billing", "list"): ("List billing records", [], [("customer-id", "Filter by customer ID"), ("account-id", "Filter by account ID"), ("limit", "Max results (default: 100)")]),
            ("billing", "get"): ("Get billing record details", [("id", "Billing record ID")], []),
        }

        key = (subcmd, action)
        if key not in help_info:
            print(f"{red('✗')} Unknown command: billing {subcmd} {action}")
            return

        desc, required, optional = help_info[key]
        print(f"\n{bold(desc)}\n")
        print(f"{blue('Usage:')} billing {subcmd} {action} [options]\n")

        if required:
            print(f"{blue('Required:')}")
            for arg, desc in required:
                print(f"  --{arg:<20} {desc}")
            print()

        if optional:
            print(f"{blue('Optional:')}")
            for arg, desc in optional:
                print(f"  --{arg:<20} {desc}")
            print()

    def _run_billing_command(self, subcmd, action, args, verbose):
        """Execute a billing command"""
        config = SIDECAR_COMMANDS["billing"]
        container = config["container"]
        binary = config["binary"]
        command_key = ("billing", subcmd, action)

        # Check if action is valid
        valid_actions = config["subcommands"].get(subcmd, {}).get("commands", [])
        if action not in valid_actions:
            print(f"{red('✗')} Unknown command: billing {subcmd} {action}")
            print(f"  Available: {', '.join(valid_actions)}")
            return

        # Prompt for missing required args
        args = prompt_missing_args(command_key, args)
        if args is None:
            return

        # Confirm delete
        if command_key in SIDECAR_DELETE_COMMANDS:
            # First get the resource to show details
            get_args = {"id": args.get("id")}
            success, data = run_sidecar_command(container, f"{binary} {subcmd} get", get_args, verbose=False)
            if success and data:
                if not confirm_delete("billing account", data):
                    return

        # Build and run command
        cmd_args = {subcmd: None}  # Add subcommand
        cmd_args[action] = None  # Add action
        cmd_args.update(args)

        # Remove subcommand and action from args, build proper command
        actual_args = {k: v for k, v in args.items()}
        success, data = run_sidecar_command(container, f"{binary} {subcmd} {action}", actual_args, verbose)

        if not success:
            print(f"{red('✗')} {data}")
            return

        # Format output
        self._format_billing_output(subcmd, action, data, command_key)

    def _format_billing_output(self, subcmd, action, data, command_key):
        """Format and display billing command output"""
        if action == "list":
            if not data:
                entity = "accounts" if subcmd == "account" else "billing records"
                print(f"\nNo {entity} found.\n")
                return
            columns = SIDECAR_TABLE_COLUMNS.get(command_key)
            if columns:
                entity = "Billing Accounts" if subcmd == "account" else "Billing Records"
                print(f"\n{bold(entity)} ({len(data)} found)\n")
                format_table(data, columns)
                print()

        elif action == "get":
            if not data:
                print(f"{red('✗')} Not found.")
                return
            fields = SIDECAR_DETAIL_FIELDS.get(command_key)
            if fields:
                entity = "Billing Account" if subcmd == "account" else "Billing Record"
                print(f"\n{bold(entity)}")
                format_details(data, fields)

        elif action == "create":
            if data:
                item_id = data.get("id", "unknown")
                print(f"{green('✓')} Created: {item_id}")

        elif action == "delete":
            print(f"{green('✓')} Deleted.")

        elif action in ("add-balance", "subtract-balance"):
            if data:
                new_balance = data.get("balance", "unknown")
                name = data.get("name", "account")
                print(f"{green('✓')} Balance updated for \"{name}\"")
                print(f"  New balance: {new_balance}")

    def cmd_customer(self, args):
        """Customer management (customers and access keys)"""
        if not args or args[0] in ("help", "-h", "--help"):
            self._show_customer_help(args[1:] if len(args) > 1 else [])
            return

        subcmd = args[0].lower()

        # Check if it's a subcommand (customer or accesskey) or a direct action
        if subcmd in ("customer", "accesskey"):
            if len(args) < 2 or args[1] in ("help", "-h", "--help"):
                self._show_customer_subcommand_help(subcmd, args[2:] if len(args) > 2 else [])
                return

            action = args[1].lower()

            # Check for help on specific action
            if len(args) > 2 and args[2] in ("help", "-h", "--help"):
                self._show_customer_action_help(subcmd, action)
                return

            cmd_args = parse_sidecar_args(args[2:])
            verbose = cmd_args.pop("verbose", False)

            self._run_customer_command(subcmd, action, cmd_args, verbose)
        else:
            # Backward compatibility: treat as direct customer action
            action = subcmd
            valid_actions = ["list", "create", "get", "delete", "update", "update-billing-account"]
            if action not in valid_actions:
                print(f"{red('✗')} Unknown subcommand: {action}")
                print("  Available: customer, accesskey")
                print("  Type 'customer help' for usage.")
                return

            # Check for help on specific action
            if len(args) > 1 and args[1] in ("help", "-h", "--help"):
                self._show_customer_action_help("customer", action)
                return

            cmd_args = parse_sidecar_args(args[1:])
            verbose = cmd_args.pop("verbose", False)

            self._run_customer_command("customer", action, cmd_args, verbose)

    def _show_customer_help(self, args):
        """Show customer command help"""
        print(f"""
{bold('Customer Management')}

{blue('Available Commands:')}
  customer customer      Manage customers
  customer accesskey     Manage API access keys

Type 'customer <subcommand> help' for more details.
""")

    def _show_customer_subcommand_help(self, subcmd, args):
        """Show help for customer subcommand"""
        if subcmd == "customer":
            if args and args[0] not in ("help", "-h", "--help"):
                self._show_customer_action_help(subcmd, args[0])
                return
            print(f"""
{bold('Customer Management')}

{blue('Available Commands:')}
  list                    List all customers
  create                  Create a new customer
  get                     Get customer details by ID
  delete                  Delete a customer
  update                  Update customer basic info
  update-billing-account  Update customer billing account ID

{blue('Usage:')} customer customer <command> [options]

{blue('Examples:')}
  customer customer list
  customer customer create --email user@example.com --name "John Doe"
  customer customer get --id abc123
  customer customer delete --id abc123
  customer customer update --id abc123 --name "New Name"
""")
        elif subcmd == "accesskey":
            if args and args[0] not in ("help", "-h", "--help"):
                self._show_customer_action_help(subcmd, args[0])
                return
            print(f"""
{bold('Accesskey Management')}

{blue('Available Commands:')}
  list     List access keys
  create   Create a new access key
  get      Get access key details by ID
  update   Update access key info
  delete   Delete an access key

{blue('Usage:')} customer accesskey <command> [options]

{blue('Examples:')}
  customer accesskey list
  customer accesskey list --customer-id abc123
  customer accesskey create --customer-id abc123 --name "API Key"
  customer accesskey create --customer-id abc123 --name "Production" --expire 720h
  customer accesskey get --id key123
  customer accesskey update --id key123 --name "New Name"
  customer accesskey delete --id key123
""")

    def _show_customer_action_help(self, subcmd, action):
        """Show help for specific customer action"""
        help_info = {
            ("customer", "list"): ("List all customers", [], [("limit", "Max results (default: 100)")]),
            ("customer", "create"): ("Create a new customer", [("email", "Customer email")], [("name", "Customer name"), ("detail", "Description"), ("address", "Physical address"), ("phone_number", "Phone number")]),
            ("customer", "get"): ("Get customer details", [("id", "Customer ID")], []),
            ("customer", "delete"): ("Delete a customer", [("id", "Customer ID")], []),
            ("customer", "update"): ("Update customer basic info", [("id", "Customer ID")], [("name", "Customer name"), ("detail", "Description"), ("address", "Physical address"), ("phone_number", "Phone number")]),
            ("customer", "update-billing-account"): ("Update customer billing account ID", [("id", "Customer ID"), ("billing-account-id", "Billing account ID")], []),
            ("accesskey", "list"): ("List access keys", [], [("customer-id", "Filter by customer ID"), ("size", "Number of results (default: 10)"), ("token", "Pagination token")]),
            ("accesskey", "create"): ("Create a new access key", [("customer-id", "Customer ID"), ("name", "Access key name")], [("detail", "Description"), ("expire", "Expiration duration (e.g., 720h for 30 days)")]),
            ("accesskey", "get"): ("Get access key details", [("id", "Access key ID")], []),
            ("accesskey", "update"): ("Update access key info", [("id", "Access key ID")], [("name", "Access key name"), ("detail", "Description")]),
            ("accesskey", "delete"): ("Delete an access key", [("id", "Access key ID")], []),
        }

        key = (subcmd, action)
        if key not in help_info:
            print(f"{red('✗')} Unknown command: customer {subcmd} {action}")
            return

        desc, required, optional = help_info[key]
        print(f"\n{bold(desc)}\n")
        print(f"{blue('Usage:')} customer {subcmd} {action} [options]\n")

        if required:
            print(f"{blue('Required:')}")
            for arg, arg_desc in required:
                print(f"  --{arg:<20} {arg_desc}")
            print()

        if optional:
            print(f"{blue('Optional:')}")
            for arg, arg_desc in optional:
                print(f"  --{arg:<20} {arg_desc}")
            print()

    def _run_customer_command(self, subcmd, action, args, verbose):
        """Execute a customer command"""
        config = SIDECAR_COMMANDS["customer"]
        container = config["container"]
        binary = config["binary"]
        command_key = ("customer", subcmd, action)

        # Check if action is valid
        valid_actions = config["subcommands"].get(subcmd, {}).get("commands", [])
        if action not in valid_actions:
            print(f"{red('✗')} Unknown command: customer {subcmd} {action}")
            print(f"  Available: {', '.join(valid_actions)}")
            return

        # Prompt for missing required args
        args = prompt_missing_args(command_key, args)
        if args is None:
            return

        # Confirm delete
        if command_key in SIDECAR_DELETE_COMMANDS:
            # First get the resource to show details
            get_args = {"id": args.get("id")}
            success, data = run_sidecar_command(container, f"{binary} {subcmd} get", get_args, verbose=False)
            if success and data:
                entity = "customer" if subcmd == "customer" else "accesskey"
                if not confirm_delete(entity, data):
                    return

        success, data = run_sidecar_command(container, f"{binary} {subcmd} {action}", args, verbose)

        if not success:
            print(f"{red('✗')} {data}")
            return

        # Format output
        self._format_customer_output(subcmd, action, data, command_key)

    def _format_customer_output(self, subcmd, action, data, command_key):
        """Format and display customer command output"""
        entity = "Customers" if subcmd == "customer" else "Access Keys"
        entity_singular = "Customer" if subcmd == "customer" else "Access Key"

        if action == "list":
            if not data:
                print(f"\nNo {entity.lower()} found.\n")
                return
            columns = SIDECAR_TABLE_COLUMNS.get(command_key)
            if columns:
                print(f"\n{bold(entity)} ({len(data)} found)\n")
                format_table(data, columns)
                print()

        elif action == "get":
            if not data:
                print(f"{red('✗')} {entity_singular} not found.")
                return
            fields = SIDECAR_DETAIL_FIELDS.get(command_key)
            if fields:
                print(f"\n{bold(entity_singular)}")
                format_details(data, fields)

        elif action == "create":
            if data:
                item_id = data.get("id", "unknown")
                if subcmd == "customer":
                    email = data.get("email", "")
                    print(f"{green('✓')} {entity_singular} created: {email}")
                else:
                    name = data.get("name", "")
                    print(f"{green('✓')} {entity_singular} created: {name}")
                print(f"  ID: {item_id}")

        elif action == "delete":
            print(f"{green('✓')} {entity_singular} deleted.")

        elif action.startswith("update"):
            print(f"{green('✓')} {entity_singular} updated.")

    def cmd_number(self, args):
        """Phone number management"""
        if not args or args[0] in ("help", "-h", "--help"):
            self._show_number_help(args[1:] if len(args) > 1 else [])
            return

        action = args[0].lower()

        valid_actions = ["list", "create", "get", "delete", "register", "update", "get-available"]
        if action not in valid_actions:
            print(f"{red('✗')} Unknown subcommand: {action}")
            print(f"  Available: {', '.join(valid_actions)}")
            print("  Type 'number help' for usage.")
            return

        cmd_args = parse_sidecar_args(args[1:])
        verbose = cmd_args.pop("verbose", False)

        self._run_number_command(action, cmd_args, verbose)

    def _show_number_help(self, args):
        """Show number command help"""
        if args and args[0] not in ("help", "-h", "--help"):
            self._show_number_action_help(args[0])
            return
        print(f"""
{bold('Phone Number Management')}

{blue('Available Commands:')}
  list             List all phone numbers
  create           Create a new number entry
  get              Get number details by ID
  delete           Delete a number
  register         Register a new number
  update           Update a number
  get-available    Get available numbers for purchase

{blue('Usage:')} number <command> [options]

{blue('Examples:')}
  number list --customer-id abc123
  number create --customer-id abc123 --number +15551234567 --name "Main Line"
  number get --id xyz789
  number delete --id xyz789
  number register --customer-id abc123 --number +15551234567
  number update --id xyz789 --name "New Name"
""")

    def _show_number_action_help(self, action):
        """Show help for specific number action"""
        help_info = {
            "list": ("List all phone numbers", [("customer-id", "Customer ID")], [("limit", "Max results (default: 100)")]),
            "create": ("Create a new number entry", [("customer-id", "Customer ID"), ("number", "Phone number (e.g., +15551234567)")], [("name", "Number name"), ("detail", "Description"), ("call_flow_id", "Call flow ID"), ("message_flow_id", "Message flow ID")]),
            "get": ("Get number details", [("id", "Number ID")], []),
            "delete": ("Delete a number", [("id", "Number ID")], []),
            "register": ("Register a new number", [("customer-id", "Customer ID"), ("number", "Phone number (e.g., +15551234567)")], [("name", "Number name"), ("detail", "Description"), ("call_flow_id", "Call flow ID"), ("message_flow_id", "Message flow ID")]),
            "update": ("Update a number", [("id", "Number ID")], [("name", "Number name"), ("detail", "Description"), ("call_flow_id", "Call flow ID"), ("message_flow_id", "Message flow ID")]),
            "get-available": ("Get available numbers for purchase", [], [("country_code", "Country code"), ("type", "Number type")]),
        }

        if action not in help_info:
            self._show_number_help([])
            return

        desc, required, optional = help_info[action]
        print(f"\n{bold(desc)}\n")
        print(f"{blue('Usage:')} number {action} [options]\n")

        if required:
            print(f"{blue('Required:')}")
            for arg, desc in required:
                print(f"  --{arg:<20} {desc}")
            print()

        if optional:
            print(f"{blue('Optional:')}")
            for arg, desc in optional:
                print(f"  --{arg:<20} {desc}")
            print()

    def _run_number_command(self, action, args, verbose):
        """Execute a number command"""
        config = SIDECAR_COMMANDS["number"]
        container = config["container"]
        binary = config["binary"]
        command_key = ("number", "number", action)

        # Prompt for missing required args
        args = prompt_missing_args(command_key, args)
        if args is None:
            return

        # Confirm delete
        if command_key in SIDECAR_DELETE_COMMANDS:
            # First get the resource to show details
            get_args = {"id": args.get("id")}
            success, data = run_sidecar_command(container, f"{binary} number get", get_args, verbose=False)
            if success and data:
                if not confirm_delete("number", data):
                    return

        success, data = run_sidecar_command(container, f"{binary} number {action}", args, verbose)

        if not success:
            print(f"{red('✗')} {data}")
            return

        # Format output
        self._format_number_output(action, data, command_key)

    def _format_number_output(self, action, data, command_key):
        """Format and display number command output"""
        if action == "list":
            if not data:
                print("\nNo numbers found.\n")
                return
            columns = SIDECAR_TABLE_COLUMNS.get(command_key)
            if columns:
                print(f"\n{bold('Phone Numbers')} ({len(data)} found)\n")
                format_table(data, columns)
                print()

        elif action == "get":
            if not data:
                print(f"{red('✗')} Number not found.")
                return
            fields = SIDECAR_DETAIL_FIELDS.get(command_key)
            if fields:
                print(f"\n{bold('Phone Number')}")
                format_details(data, fields)

        elif action in ("create", "register"):
            if data:
                number = data.get("number", "unknown")
                item_id = data.get("id", "unknown")
                print(f"{green('✓')} Number {'registered' if action == 'register' else 'created'}: {number}")
                print(f"  ID: {item_id}")

        elif action == "delete":
            print(f"{green('✓')} Number deleted.")

        elif action == "update":
            print(f"{green('✓')} Number updated.")

        elif action == "get-available":
            if not data:
                print("\nNo available numbers found.\n")
                return
            print(f"\n{bold('Available Numbers')} ({len(data)} found)\n")
            for num in data:
                print(f"  {num.get('number', 'N/A')}")
            print()

    def cmd_registrar(self, args):
        """Registrar management (extensions and trunks)"""
        if not args or args[0] in ("help", "-h", "--help"):
            self._show_registrar_help(args[1:] if len(args) > 1 else [])
            return

        subcmd = args[0].lower()

        if subcmd not in ("extension", "trunk"):
            print(f"{red('✗')} Unknown subcommand: {subcmd}")
            print("  Available: extension, trunk")
            print("  Type 'registrar help' for usage.")
            return

        if len(args) < 2 or args[1] in ("help", "-h", "--help"):
            self._show_registrar_subcommand_help(subcmd, args[2:] if len(args) > 2 else [])
            return

        action = args[1].lower()
        cmd_args = parse_sidecar_args(args[2:])
        verbose = cmd_args.pop("verbose", False)

        self._run_registrar_command(subcmd, action, cmd_args, verbose)

    def _show_registrar_help(self, args):
        """Show registrar command help"""
        print(f"""
{bold('Registrar Management')}

{blue('Available Commands:')}
  registrar extension    Manage SIP extensions
  registrar trunk        Manage SIP trunks

Type 'registrar <subcommand> help' for more details.
""")

    def _show_registrar_subcommand_help(self, subcmd, args):
        """Show help for registrar subcommand"""
        if subcmd == "extension":
            if args and args[0] not in ("help", "-h", "--help"):
                self._show_registrar_action_help(subcmd, args[0])
                return
            print(f"""
{bold('Extension Management')}

{blue('Available Commands:')}
  list             List extensions
  create           Create a new extension
  get              Get extension details by ID
  delete           Delete an extension
  update           Update an extension

{blue('Usage:')} registrar extension <command> [options]

{blue('Examples:')}
  registrar extension list --customer-id abc123
  registrar extension create --customer-id abc123 --username 1000 --password secret
  registrar extension get --id xyz789
  registrar extension delete --id xyz789
""")
        elif subcmd == "trunk":
            if args and args[0] not in ("help", "-h", "--help"):
                self._show_registrar_action_help(subcmd, args[0])
                return
            print(f"""
{bold('Trunk Management')}

{blue('Available Commands:')}
  list             List trunks
  create           Create a new trunk
  get              Get trunk details by ID
  delete           Delete a trunk
  update           Update a trunk

{blue('Usage:')} registrar trunk <command> [options]

{blue('Examples:')}
  registrar trunk list --customer-id abc123
  registrar trunk create --customer-id abc123 --domain sip.example.com --name "Main Trunk"
  registrar trunk get --id xyz789
  registrar trunk delete --id xyz789
""")

    def _show_registrar_action_help(self, subcmd, action):
        """Show help for specific registrar action"""
        help_info = {
            ("extension", "list"): ("List extensions", [("customer-id", "Customer ID")], [("extension_number", "Filter by extension"), ("limit", "Max results (default: 100)")]),
            ("extension", "create"): ("Create a new extension", [("customer-id", "Customer ID"), ("username", "Username"), ("password", "Password")], [("extension_number", "Extension number"), ("domain", "Domain name")]),
            ("extension", "get"): ("Get extension details", [("id", "Extension ID")], []),
            ("extension", "delete"): ("Delete an extension", [("id", "Extension ID")], []),
            ("extension", "update"): ("Update an extension", [("id", "Extension ID")], [("password", "New password"), ("extension_number", "New extension number"), ("username", "New username")]),
            ("trunk", "list"): ("List trunks", [("customer-id", "Customer ID")], [("name", "Filter by name"), ("limit", "Max results (default: 100)")]),
            ("trunk", "create"): ("Create a new trunk", [("customer-id", "Customer ID"), ("domain", "Domain name")], [("name", "Trunk name"), ("username", "Username"), ("password", "Password"), ("allowed_ips", "Allowed IPs (comma-separated)")]),
            ("trunk", "get"): ("Get trunk details", [("id", "Trunk ID")], []),
            ("trunk", "delete"): ("Delete a trunk", [("id", "Trunk ID")], []),
            ("trunk", "update"): ("Update a trunk", [("id", "Trunk ID")], [("password", "New password"), ("allowed_ips", "Allowed IPs"), ("name", "New name"), ("username", "New username")]),
        }

        key = (subcmd, action)
        if key not in help_info:
            print(f"{red('✗')} Unknown command: registrar {subcmd} {action}")
            return

        desc, required, optional = help_info[key]
        print(f"\n{bold(desc)}\n")
        print(f"{blue('Usage:')} registrar {subcmd} {action} [options]\n")

        if required:
            print(f"{blue('Required:')}")
            for arg, desc in required:
                print(f"  --{arg:<20} {desc}")
            print()

        if optional:
            print(f"{blue('Optional:')}")
            for arg, desc in optional:
                print(f"  --{arg:<20} {desc}")
            print()

    def _run_registrar_command(self, subcmd, action, args, verbose):
        """Execute a registrar command"""
        config = SIDECAR_COMMANDS["registrar"]
        container = config["container"]
        binary = config["binary"]
        command_key = ("registrar", subcmd, action)

        # Check if action is valid
        valid_actions = config["subcommands"].get(subcmd, {}).get("commands", [])
        if action not in valid_actions:
            print(f"{red('✗')} Unknown command: registrar {subcmd} {action}")
            print(f"  Available: {', '.join(valid_actions)}")
            return

        # Prompt for missing required args
        args = prompt_missing_args(command_key, args)
        if args is None:
            return

        # Note: registrar-control outputs JSON by default, no --format flag needed

        # Confirm delete
        if command_key in SIDECAR_DELETE_COMMANDS:
            # First get the resource to show details
            get_args = {"id": args.get("id")}
            success, data = run_sidecar_command(container, f"{binary} {subcmd} get", get_args, verbose=False)
            if success and data:
                resource_type = "extension" if subcmd == "extension" else "trunk"
                if not confirm_delete(resource_type, data):
                    return

        success, data = run_sidecar_command(container, f"{binary} {subcmd} {action}", args, verbose)

        if not success:
            print(f"{red('✗')} {data}")
            return

        # Format output
        self._format_registrar_output(subcmd, action, data, command_key)

    def _format_registrar_output(self, subcmd, action, data, command_key):
        """Format and display registrar command output"""
        entity_name = "Extensions" if subcmd == "extension" else "Trunks"
        entity_singular = "Extension" if subcmd == "extension" else "Trunk"

        if action == "list":
            if not data:
                print(f"\nNo {entity_name.lower()} found.\n")
                return
            columns = SIDECAR_TABLE_COLUMNS.get(command_key)
            if columns:
                print(f"\n{bold(entity_name)} ({len(data)} found)\n")
                format_table(data, columns)
                print()

        elif action == "get":
            if not data:
                print(f"{red('✗')} {entity_singular} not found.")
                return
            fields = SIDECAR_DETAIL_FIELDS.get(command_key)
            if fields:
                print(f"\n{bold(entity_singular)}")
                format_details(data, fields)

        elif action == "create":
            if data:
                item_id = data.get("id", "unknown")
                if subcmd == "extension":
                    ext_num = data.get("extension_number", "")
                    print(f"{green('✓')} Extension created: {ext_num}")
                else:
                    name = data.get("name", "")
                    print(f"{green('✓')} Trunk created: {name}")
                print(f"  ID: {item_id}")

        elif action == "delete":
            print(f"{green('✓')} {entity_singular} deleted.")

        elif action == "update":
            print(f"{green('✓')} {entity_singular} updated.")

    def cmd_agent(self, args):
        """Agent management"""
        if not args or args[0] in ("help", "-h", "--help"):
            self._show_agent_help(args[1:] if len(args) > 1 else [])
            return

        action = args[0].lower()

        valid_actions = ["list", "create", "get", "delete", "login", "update-addresses", "update-basic-info", "update-password", "update-permission", "update-status", "update-tag-ids"]
        if action not in valid_actions:
            print(f"{red('✗')} Unknown subcommand: {action}")
            print(f"  Available: {', '.join(valid_actions)}")
            print("  Type 'agent help' for usage.")
            return

        cmd_args = parse_sidecar_args(args[1:])
        verbose = cmd_args.pop("verbose", False)

        self._run_agent_command(action, cmd_args, verbose)

    def _show_agent_help(self, args):
        """Show agent command help"""
        if args and args[0] not in ("help", "-h", "--help"):
            self._show_agent_action_help(args[0])
            return
        print(f"""
{bold('Agent Management')}

{blue('Available Commands:')}
  list               List all agents
  create             Create a new agent
  get                Get agent details by ID
  delete             Delete an agent
  login              Authenticate an agent
  update-addresses   Update agent addresses
  update-basic-info  Update agent basic info
  update-password    Update agent password
  update-permission  Update agent permission
  update-status      Update agent status
  update-tag-ids     Update agent tag IDs

{blue('Usage:')} agent <command> [options]

{blue('Examples:')}
  agent list --customer-id abc123
  agent create --customer-id abc123 --username user1 --password secret
  agent get --id xyz789
  agent delete --id xyz789
  agent update-password --id xyz789 --password newpass
""")

    def _show_agent_action_help(self, action):
        """Show help for specific agent action"""
        help_info = {
            "list": ("List all agents", [("customer-id", "Customer ID")], [("limit", "Max results (default: 100)")]),
            "create": ("Create a new agent", [("customer-id", "Customer ID"), ("username", "Username"), ("password", "Password")], [("name", "Agent name"), ("detail", "Description"), ("permission", "Permission level")]),
            "get": ("Get agent details", [("id", "Agent ID")], []),
            "delete": ("Delete an agent", [("id", "Agent ID")], []),
            "login": ("Authenticate an agent", [("username", "Username"), ("password", "Password")], []),
            "update-addresses": ("Update agent addresses", [("id", "Agent ID")], [("addresses", "Addresses JSON")]),
            "update-basic-info": ("Update agent basic info", [("id", "Agent ID")], [("name", "Name"), ("detail", "Detail")]),
            "update-password": ("Update agent password", [("id", "Agent ID"), ("password", "New password")], []),
            "update-permission": ("Update agent permission", [("id", "Agent ID")], [("permission", "New permission level")]),
            "update-status": ("Update agent status", [("id", "Agent ID"), ("status", "Status")], []),
            "update-tag-ids": ("Update agent tag IDs", [("id", "Agent ID")], [("tag_ids", "Tag IDs JSON")]),
        }

        if action not in help_info:
            self._show_agent_help([])
            return

        desc, required, optional = help_info[action]
        print(f"\n{bold(desc)}\n")
        print(f"{blue('Usage:')} agent {action} [options]\n")

        if required:
            print(f"{blue('Required:')}")
            for arg, desc in required:
                print(f"  --{arg:<20} {desc}")
            print()

        if optional:
            print(f"{blue('Optional:')}")
            for arg, desc in optional:
                print(f"  --{arg:<20} {desc}")
            print()

    def _run_agent_command(self, action, args, verbose):
        """Execute an agent command"""
        config = SIDECAR_COMMANDS["agent"]
        container = config["container"]
        binary = config["binary"]
        command_key = ("agent", "agent", action)

        # Prompt for missing required args
        args = prompt_missing_args(command_key, args)
        if args is None:
            return

        # Confirm delete
        if command_key in SIDECAR_DELETE_COMMANDS:
            # First get the resource to show details
            get_args = {"id": args.get("id")}
            success, data = run_sidecar_command(container, f"{binary} agent get", get_args, verbose=False)
            if success and data:
                if not confirm_delete("agent", data):
                    return

        success, data = run_sidecar_command(container, f"{binary} agent {action}", args, verbose)

        if not success:
            print(f"{red('✗')} {data}")
            return

        # Format output
        self._format_agent_output(action, data, command_key)

    def _format_agent_output(self, action, data, command_key):
        """Format and display agent command output"""
        if action == "list":
            if not data:
                print("\nNo agents found.\n")
                return
            columns = SIDECAR_TABLE_COLUMNS.get(command_key)
            if columns:
                print(f"\n{bold('Agents')} ({len(data)} found)\n")
                format_table(data, columns)
                print()

        elif action == "get":
            if not data:
                print(f"{red('✗')} Agent not found.")
                return
            fields = SIDECAR_DETAIL_FIELDS.get(command_key)
            if fields:
                print(f"\n{bold('Agent')}")
                format_details(data, fields)

        elif action == "create":
            if data:
                username = data.get("username", "unknown")
                item_id = data.get("id", "unknown")
                print(f"{green('✓')} Agent created: {username}")
                print(f"  ID: {item_id}")

        elif action == "delete":
            print(f"{green('✓')} Agent deleted.")

        elif action == "login":
            if data:
                print(f"{green('✓')} Login successful.")
                if "token" in data:
                    print(f"  Token: {data.get('token', '')[:50]}...")

        elif action.startswith("update-"):
            print(f"{green('✓')} Agent updated.")

    # -------------------------------------------------------------------------
    # Generic Sidecar Command Handler
    # -------------------------------------------------------------------------

    def _run_generic_sidecar(self, service_name, args, entity_name=None):
        """
        Generic handler for sidecar commands.
        service_name: key in SIDECAR_COMMANDS (e.g., "call", "campaign")
        entity_name: display name for messages (defaults to service_name)
        """
        if entity_name is None:
            entity_name = service_name.title()

        config = SIDECAR_COMMANDS.get(service_name)
        if not config:
            print(f"{red('✗')} Unknown service: {service_name}")
            return

        subcommands = config.get("subcommands", {})
        container = config["container"]
        binary = config["binary"]

        # Show help if no args
        if not args or args[0] in ("help", "-h", "--help"):
            self._show_generic_help(service_name, entity_name, subcommands)
            return

        # Determine if first arg is a subcommand or action
        first_arg = args[0].lower()

        # Single subcommand services (action is first arg)
        if len(subcommands) == 1:
            subcmd = list(subcommands.keys())[0]
            action = first_arg
            remaining_args = args[1:]
        # Multi-subcommand services (subcommand is first arg)
        elif first_arg in subcommands:
            subcmd = first_arg
            if len(args) < 2 or args[1] in ("help", "-h", "--help"):
                self._show_generic_subcommand_help(service_name, subcmd, entity_name, subcommands[subcmd])
                return
            action = args[1].lower()
            remaining_args = args[2:]
        else:
            # Assume single subcommand with same name as service
            if service_name in subcommands:
                subcmd = service_name
                action = first_arg
                remaining_args = args[1:]
            else:
                print(f"{red('✗')} Unknown subcommand: {first_arg}")
                print(f"  Available: {', '.join(subcommands.keys())}")
                return

        # Check if action is valid
        valid_actions = subcommands.get(subcmd, {}).get("commands", [])
        if action not in valid_actions:
            print(f"{red('✗')} Unknown action: {action}")
            print(f"  Available: {', '.join(valid_actions)}")
            return

        # Parse arguments
        cmd_args = parse_sidecar_args(remaining_args)
        verbose = cmd_args.pop("verbose", False)
        command_key = (service_name, subcmd, action)

        # Prompt for missing required args
        cmd_args = prompt_missing_args(command_key, cmd_args)
        if cmd_args is None:
            return

        # Confirm delete
        if command_key in SIDECAR_DELETE_COMMANDS and "id" in cmd_args:
            get_args = {"id": cmd_args.get("id")}
            success, data = run_sidecar_command(container, f"{binary} {subcmd} get", get_args, verbose=False)
            if success and data:
                if not confirm_delete(entity_name.lower(), data):
                    return

        # Run command
        success, data = run_sidecar_command(container, f"{binary} {subcmd} {action}", cmd_args, verbose)

        if not success:
            print(f"{red('✗')} {data}")
            return

        # Format output
        self._format_generic_output(service_name, subcmd, action, data, command_key, entity_name)

    def _show_generic_help(self, service_name, entity_name, subcommands):
        """Show help for a service"""
        print(f"\n{bold(f'{entity_name} Management')}\n")

        if len(subcommands) == 1:
            # Single subcommand - show actions directly
            subcmd = list(subcommands.keys())[0]
            subcmd_info = subcommands[subcmd]
            print(f"{blue('Available Commands:')}")
            for cmd in subcmd_info.get("commands", []):
                print(f"  {cmd:<25} {cmd.replace('-', ' ').title()}")
            print(f"\n{blue('Usage:')} {service_name} <command> [options]")
        else:
            # Multiple subcommands
            print(f"{blue('Available Subcommands:')}")
            for subcmd, info in subcommands.items():
                print(f"  {subcmd:<20} {info.get('description', '')}")
            print(f"\n{blue('Usage:')} {service_name} <subcommand> <command> [options]")
            print(f"\nType '{service_name} <subcommand> help' for more details.")
        print()

    def _show_generic_subcommand_help(self, service_name, subcmd, entity_name, subcmd_info):
        """Show help for a specific subcommand"""
        print(f"\n{bold(subcmd_info.get('description', subcmd.title()))}\n")
        print(f"{blue('Available Commands:')}")
        for cmd in subcmd_info.get("commands", []):
            print(f"  {cmd:<25} {cmd.replace('-', ' ').title()}")
        print(f"\n{blue('Usage:')} {service_name} {subcmd} <command> [options]\n")

    def _format_generic_output(self, service_name, subcmd, action, data, command_key, entity_name):
        """Format and display sidecar command output"""
        if action == "list":
            if not data:
                print(f"\nNo {entity_name.lower()}s found.\n")
                return
            columns = SIDECAR_TABLE_COLUMNS.get(command_key)
            if columns:
                print(f"\n{bold(f'{entity_name}s')} ({len(data)} found)\n")
                format_table(data, columns)
                print()
            else:
                # Fallback: print JSON
                print(json.dumps(data, indent=2))

        elif action == "get" or action.startswith("get-"):
            if not data:
                print(f"{red('✗')} {entity_name} not found.")
                return
            fields = SIDECAR_DETAIL_FIELDS.get(command_key)
            if fields:
                print(f"\n{bold(entity_name)}")
                format_details(data, fields)
            else:
                # Fallback: print JSON
                print(json.dumps(data, indent=2))

        elif action == "create":
            if data:
                item_id = data.get("id", "unknown")
                name = data.get("name", data.get("username", ""))
                if name:
                    print(f"{green('✓')} {entity_name} created: {name}")
                else:
                    print(f"{green('✓')} {entity_name} created")
                print(f"  ID: {item_id}")

        elif action == "delete":
            print(f"{green('✓')} {entity_name} deleted.")

        elif action.startswith("update") or action.startswith("add") or action.startswith("remove"):
            print(f"{green('✓')} {entity_name} updated.")

        elif action == "hangup":
            print(f"{green('✓')} Call hung up.")

        elif action == "terminating":
            print(f"{green('✓')} Conference terminated.")

        elif action.startswith("recording-") or action.startswith("transcribe-"):
            op = "started" if action.endswith("-start") else "stopped"
            what = "Recording" if "recording" in action else "Transcription"
            print(f"{green('✓')} {what} {op}.")

        elif action.startswith("send-"):
            print(f"{green('✓')} Webhook sent.")

        elif action == "service-start":
            print(f"{green('✓')} Transfer service started.")

        else:
            # Generic success
            if data:
                if isinstance(data, dict):
                    print(json.dumps(data, indent=2))
                elif isinstance(data, list) and data:
                    print(json.dumps(data, indent=2))
                else:
                    print(f"{green('✓')} Operation completed.")
            else:
                print(f"{green('✓')} Operation completed.")

    # -------------------------------------------------------------------------
    # New Sidecar Command Handlers
    # -------------------------------------------------------------------------

    def cmd_call(self, args):
        """Call management"""
        self._run_generic_sidecar("call", args, "Call")

    def cmd_campaign(self, args):
        """Campaign management"""
        self._run_generic_sidecar("campaign", args, "Campaign")

    def cmd_chat(self, args):
        """Chat room management"""
        self._run_generic_sidecar("chat", args, "Chat")

    def cmd_conference(self, args):
        """Conference management"""
        self._run_generic_sidecar("conference", args, "Conference")

    def cmd_conversation(self, args):
        """Conversation management"""
        self._run_generic_sidecar("conversation", args, "Conversation")

    def cmd_flow(self, args):
        """Flow management"""
        self._run_generic_sidecar("flow", args, "Flow")

    def cmd_outdial(self, args):
        """Outdial management"""
        self._run_generic_sidecar("outdial", args, "Outdial")

    def cmd_queue(self, args):
        """Queue management"""
        self._run_generic_sidecar("queue", args, "Queue")

    def cmd_route(self, args):
        """Route management"""
        self._run_generic_sidecar("route", args, "Route")

    def cmd_storage(self, args):
        """Storage management"""
        self._run_generic_sidecar("storage", args, "Storage")

    def cmd_tag(self, args):
        """Tag management"""
        self._run_generic_sidecar("tag", args, "Tag")

    def cmd_talk(self, args):
        """Talk management"""
        self._run_generic_sidecar("talk", args, "Talk")

    def cmd_transfer(self, args):
        """Transfer management"""
        self._run_generic_sidecar("transfer", args, "Transfer")

    def cmd_tts(self, args):
        """Text-to-speech"""
        self._run_generic_sidecar("tts", args, "TTS")

    def cmd_webhook(self, args):
        """Webhook operations"""
        self._run_generic_sidecar("webhook", args, "Webhook")

    def cmd_hook(self, args):
        """Test webhook operations (send test webhooks)"""
        self._run_generic_sidecar("hook", args, "Hook")

    def cmd_config(self, args):
        """View/set configuration"""
        if not args:
            print(f"\n{bold('Configuration')} ({CONFIG_FILE})\n")
            for key, value in self.config.data.items():
                print(f"  {key}: {value}")
            print()
            return

        if args[0] == "reset":
            self.config.reset()
            print(green("✓ Configuration reset to defaults"))
            return

        key = args[0]
        if len(args) > 1:
            value = " ".join(args[1:])
            self.config.set(key, value)
            print(green(f"✓ Set {key} = {value}"))
        else:
            print(f"{key}: {self.config.get(key)}")

    def cmd_dns(self, args):
        """DNS setup for SIP domains"""
        subcmd = args[0].lower() if args else "status"

        # External mode (design §2.10): DNS is operator-managed. Every dns
        # subcommand prints the required record set instead of touching
        # CoreDNS/resolv.conf state, and exits 0.
        if self._domain_mode() == "external":
            self.dns_external_info()
            return

        if subcmd == "status":
            self.dns_status()
        elif subcmd == "list":
            self.dns_list()
        elif subcmd == "setup":
            self.dns_setup()
        elif subcmd == "regenerate":
            self.dns_regenerate()
        elif subcmd == "test":
            self.dns_test()
        else:
            print("Usage: dns [status|list|setup|regenerate|test]")

    def dns_external_info(self):
        """External mode: print the operator DNS runbook (design §2.9 table)
        with the actual domain and IPs substituted."""
        project_dir = self._project_dir()
        d = self._base_domain()
        host_ip = read_env_file_var(project_dir, "HOST_EXTERNAL_IP") or "<host-ip>"
        kamailio_ip = read_env_file_var(project_dir, "KAMAILIO_EXTERNAL_IP") or "<kamailio-ip>"

        print(f"\n{bold('DNS is operator-managed in external mode')}")
        print("-" * 70)
        print("The sandbox does not run CoreDNS or touch resolv.conf in external")
        print("mode. Create these records at your DNS provider:")
        print()
        print(f"  {'Record':<28} {'Type':<6} {'Target':<18} Purpose")
        print(f"  {'-' * 68}")
        rows = [
            (f"api.{d}", host_ip, "REST API + WebSocket (:8443)"),
            (f"admin.{d}", host_ip, "Admin Console (:3003)"),
            (f"meet.{d}", host_ip, "Meet (:3004)"),
            (f"talk.{d}", host_ip, "Talk (:3005)"),
            (f"sip.{d}", kamailio_ip, "SIP signaling / WSS (:5060/:5066)"),
            (f"sip-service.{d}", kamailio_ip, "SIP surface"),
            (f"conference.{d}", kamailio_ip, "SIP surface"),
            (f"trunk.{d}", kamailio_ip, "SIP trunking"),
            (f"pstn.{d}", kamailio_ip, "PSTN gateway"),
            (f"registrar.{d}", kamailio_ip, "Apex registrar name (not covered by the wildcard)"),
            (f"*.registrar.{d}", kamailio_ip, "Per-customer SIP realm resolution"),
        ]
        for record, target, purpose in rows:
            print(f"  {record:<28} {'A':<6} {target:<18} {purpose}")
        print()
        print(f"  Host IP ({host_ip}) and Kamailio IP ({kamailio_ip}) are two")
        print("  distinct addresses on the same subnet; both must be routable.")
        print("  See the external-mode runbook in README.md for TTL guidance")
        print("  and the reverse-proxy recommendation for the web UIs.")
        print()

    def dns_status(self):
        """Check DNS configuration status"""
        host_ip = run_cmd("grep '^HOST_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or "127.0.0.1"
        kamailio_ip = run_cmd("grep '^KAMAILIO_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or host_ip
        rtpengine_ip = run_cmd("grep '^RTPENGINE_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or kamailio_ip

        print(f"\n{bold('DNS Configuration Status')}")
        print("-" * 50)

        # Check if CoreDNS container is running
        coredns_running = "voipbin-dns" in run_cmd("docker ps --format '{{.Names}}' 2>/dev/null")
        if coredns_running:
            print(f"  {green('●')} CoreDNS container: running (port 53)")
        else:
            print(f"  {red('○')} CoreDNS container: not running")

        # Check OS-specific configuration
        import platform
        if platform.system() == "Darwin":
            # macOS
            config_exists = os.path.exists("/etc/resolver/voipbin.test")
            if config_exists:
                print(f"  {green('●')} macOS resolver: configured (/etc/resolver/voipbin.test)")
            else:
                print(f"  {gray('○')} macOS resolver: not configured")
        else:
            # Linux - check resolv.conf points to 127.0.0.1
            resolv_conf = run_cmd("cat /etc/resolv.conf 2>/dev/null") or ""
            config_exists = "nameserver 127.0.0.1" in resolv_conf
            if config_exists:
                print(f"  {green('●')} resolv.conf: configured (nameserver 127.0.0.1)")
            else:
                print(f"  {gray('○')} resolv.conf: not configured")

        # Show IP configuration
        print(f"\n{bold('IP Configuration')}")
        print("-" * 50)
        print(f"  Host IP:      {host_ip} (web services)")
        print(f"  Kamailio IP:  {kamailio_ip} (SIP signaling)")
        print(f"  RTPEngine IP: {rtpengine_ip} (RTP media)")
        if kamailio_ip == host_ip:
            print(f"  {yellow('!')} Warning: Kamailio uses same IP as host (SIP loop issues possible)")

        # Test resolution
        print(f"\n{bold('Resolution Test')}")
        print("-" * 50)

        # Web services resolve to HOST_IP (Docker port mapping)
        # SIP services resolve to KAMAILIO_IP
        test_domains = [
            ("api.voipbin.test", host_ip, "API (:8443)"),
            ("admin.voipbin.test", host_ip, "Admin (:3003)"),
            ("meet.voipbin.test", host_ip, "Meet (:3004)"),
            ("talk.voipbin.test", host_ip, "Talk (:3005)"),
            ("sip.voipbin.test", kamailio_ip, "SIP (Kamailio)"),
            ("pstn.voipbin.test", kamailio_ip, "SIP (Kamailio)"),
            ("test.registrar.voipbin.test", kamailio_ip, "SIP (Kamailio)"),
        ]

        for domain, expected, desc in test_domains:
            # Try CoreDNS directly first (port 53)
            result = run_cmd(f"dig +short {domain} @127.0.0.1 2>/dev/null | head -1")
            if not result:
                # Fallback to system resolver
                result = run_cmd(f"dig +short {domain} 2>/dev/null | head -1") or run_cmd(f"getent hosts {domain} 2>/dev/null | awk '{{print $1}}'")
            if result == expected:
                print(f"  {green('✓')} {domain} → {result} {gray('(' + desc + ')')}")
            elif result:
                print(f"  {yellow('!')} {domain} → {result} (expected: {expected})")
            else:
                print(f"  {red('✗')} {domain} → (no resolution)")

        if not coredns_running:
            print(f"\n{yellow('Hint')}: Start CoreDNS with 'docker compose up -d coredns'")
        elif not config_exists:
            print(f"\n{yellow('Hint')}: Run 'dns setup' to configure DNS forwarding")
        print()

    def dns_list(self):
        """List all DNS domains and their purposes"""
        host_ip = run_cmd("grep '^HOST_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or "localhost"
        kamailio_ip = run_cmd("grep '^KAMAILIO_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or host_ip
        rtpengine_ip = run_cmd("grep '^RTPENGINE_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or kamailio_ip

        print(f"""
{bold('VoIPBin DNS Domains')}
{'=' * 70}

{bold('IP Configuration')}
  Host IP:      {host_ip} (web services)
  Kamailio IP:  {kamailio_ip} (SIP signaling)
  RTPEngine IP: {rtpengine_ip} (RTP media)

{bold('Web Services')} (resolve to Host IP, Docker port mapping)
{'-' * 70}
  {'Domain':<35} {'Resolves To':<18} {'Port'}
  {'api.voipbin.test':<35} {host_ip:<18} :8443
  {'admin.voipbin.test':<35} {host_ip:<18} :3003
  {'meet.voipbin.test':<35} {host_ip:<18} :3004
  {'talk.voipbin.test':<35} {host_ip:<18} :3005

{bold('SIP/VoIP Services')} (resolve to Kamailio IP: {kamailio_ip})
{'-' * 70}
  {'Domain':<35} {'Port':<8} {'Description'}
  {'sip.voipbin.test':<35} {'5060':<8} SIP proxy (UDP/TCP)
  {'sip-service.voipbin.test':<35} {'5060':<8} SIP proxy (alias)
  {'pstn.voipbin.test':<35} {'5060':<8} PSTN gateway
  {'trunk.voipbin.test':<35} {'5060':<8} SIP trunking
  {'*.registrar.voipbin.test':<35} {'5060':<8} SIP registration
                                             (e.g., {{customer_id}}.registrar.voipbin.test)

{bold('Internal Services')} (Docker bridge network)
{'-' * 70}
  {'Service':<35} {'IP':<16} {'Description'}
  {'api-manager':<35} {'172.28.0.100':<16} API Manager container
  {'square-admin':<35} {'172.28.0.101':<16} Admin Console container
  {'square-meet':<35} {'172.28.0.102':<16} Meet container
  {'square-talk':<35} {'172.28.0.103':<16} Talk container

{bold('Example SIP URIs')}
{'-' * 70}
  Extension registration:  sip:1000@{{customer_id}}.registrar.voipbin.test
  Extension call:          sip:2000@{{customer_id}}.registrar.voipbin.test
  PSTN call:               sip:+15551234567@pstn.voipbin.test

{bold('DNS Commands')}
{'-' * 70}
  dns status    Check DNS configuration status
  dns list      Show this domain reference
  dns setup     Configure DNS forwarding to CoreDNS
  dns test      Test domain resolution
""")

    def dns_setup(self):
        """Setup DNS forwarding to CoreDNS"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "setup-dns.sh")

        if not os.path.exists(script_path):
            print(red(f"Setup script not found: {script_path}"))
            return

        print("Configuring DNS forwarding for *.voipbin.test to CoreDNS...\n")
        os.system(script_path)

    def dns_regenerate(self):
        """Regenerate CoreDNS configuration"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "setup-dns.sh")

        if not os.path.exists(script_path):
            print(red(f"Setup script not found: {script_path}"))
            return

        print("Regenerating CoreDNS configuration (Corefile)...\n")
        os.system(f"{script_path} --regenerate")

    def dns_test(self):
        """Test DNS resolution for SIP domains"""
        host_ip = run_cmd("grep '^HOST_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or "localhost"
        kamailio_ip = run_cmd("grep '^KAMAILIO_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or host_ip

        # Get a customer ID if available
        customer_id = run_cmd(
            "docker exec voipbin-db sh -c 'exec mysql -u root -p\"${MYSQL_ROOT_PASSWORD:-root_password}\" -N "
            "-e \"SELECT id FROM bin_manager.customer LIMIT 1\"' 2>/dev/null"
        ) or "f1504bd0-9fd4-495b-a360-a73a6fa088b0"

        print(f"\n{bold('Testing DNS Domain Resolution')}")
        print("-" * 60)
        print(f"Host IP:     {host_ip} (web services)")
        print(f"Kamailio IP: {kamailio_ip} (SIP services)\n")

        all_ok = True

        # Web services (resolve to HOST_IP, Docker port mapping)
        print(f"  {bold('Web Services')} (Docker port mapping)")
        external_tests = [
            ("api.voipbin.test", host_ip),
            ("admin.voipbin.test", host_ip),
            ("meet.voipbin.test", host_ip),
            ("talk.voipbin.test", host_ip),
        ]
        for domain, expected in external_tests:
            result = run_cmd(f"dig +short {domain} @127.0.0.1 2>/dev/null | head -1")
            if not result:
                result = run_cmd(f"dig +short {domain} 2>/dev/null | head -1") or run_cmd(f"getent hosts {domain} 2>/dev/null | awk '{{print $1}}'")
            if result == expected:
                print(f"    {green('✓')} {domain} → {result}")
            elif result:
                print(f"    {yellow('!')} {domain} → {result} (expected: {expected})")
                all_ok = False
            else:
                print(f"    {red('✗')} {domain} → (no resolution)")
                all_ok = False

        # SIP services - resolve to Kamailio IP
        print(f"\n  {bold('SIP Services')} (expect {kamailio_ip})")
        sip_domains = [
            "sip.voipbin.test",
            "sip-service.voipbin.test",
            "pstn.voipbin.test",
            "trunk.voipbin.test",
            "registrar.voipbin.test",
            f"{customer_id[:8]}...registrar.voipbin.test",  # Shortened for display
        ]
        sip_domains_full = [
            "sip.voipbin.test",
            "sip-service.voipbin.test",
            "pstn.voipbin.test",
            "trunk.voipbin.test",
            "registrar.voipbin.test",
            f"{customer_id}.registrar.voipbin.test",
        ]

        for i, domain in enumerate(sip_domains_full):
            display_domain = sip_domains[i]
            result = run_cmd(f"dig +short {domain} @127.0.0.1 2>/dev/null | head -1")
            if not result:
                result = run_cmd(f"dig +short {domain} 2>/dev/null | head -1") or run_cmd(f"getent hosts {domain} 2>/dev/null | awk '{{print $1}}'")

            if result == kamailio_ip:
                print(f"    {green('✓')} {display_domain} → {result}")
            elif result:
                print(f"    {yellow('!')} {display_domain} → {result} (expected: {kamailio_ip})")
                all_ok = False
            else:
                print(f"    {red('✗')} {display_domain} → (no resolution)")
                all_ok = False

        print()
        if all_ok:
            print(green("All domains resolve correctly!"))
        else:
            print(yellow("Some domains are not resolving. Run 'dns setup' or 'dns regenerate' to fix."))
        print()

    def cmd_certs(self, args):
        """Manage SSL certificates"""
        subcmd = args[0].lower() if args else "status"

        # External mode (design §2.10): certificates are BYO. Report expiry
        # and SANs and point at install-certs.sh. NEVER advise rm -rf certs/
        # or mkcert regeneration; that would destroy a real certificate.
        if self._domain_mode() == "external":
            self.certs_external_status()
            return

        if subcmd == "status":
            self.certs_status()
        elif subcmd == "trust":
            self.certs_trust()
        else:
            print("Usage: certs [status|trust]")

    def certs_external_status(self):
        """External mode: report the installed BYO certificate's expiry and
        SANs; renewal goes through install-certs.sh."""
        project_dir = self._project_dir()
        api_cert = os.path.join(project_dir, "certs", "api", "cert.pem")

        print(f"\n{bold('Certificate Status (external mode, BYO)')}")
        print("-" * 60)

        if not os.path.exists(api_cert):
            print(f"  {red('○')} API certificate: not found ({api_cert})")
            print()
            print("  Install your certificate:")
            print("    ./scripts/install-certs.sh <fullchain.pem> <privkey.pem>")
            print()
            return

        expires = run_cmd(f"openssl x509 -in {shlex.quote(api_cert)} -noout -enddate 2>/dev/null")
        expires = expires.split("=", 1)[1] if "=" in expires else expires or "unknown"
        subject = run_cmd(f"openssl x509 -in {shlex.quote(api_cert)} -noout -subject 2>/dev/null")
        sans = run_cmd(
            f"openssl x509 -in {shlex.quote(api_cert)} -noout -ext subjectAltName 2>/dev/null | tail -n +2"
        ).strip()

        print(f"  {green('●')} API certificate: {api_cert}")
        print(f"      {subject}")
        print(f"      Expires: {expires}")
        if sans:
            print(f"      SANs: {sans}")

        not_expiring = run_cmd(
            f"openssl x509 -in {shlex.quote(api_cert)} -noout -checkend {30 * 24 * 3600} >/dev/null 2>&1 && echo ok"
        )
        if not_expiring != "ok":
            print(f"\n  {yellow('!')} Certificate expires within 30 days (or is expired).")

        print()
        print("  To renew or replace the certificate:")
        print("    ./scripts/install-certs.sh <fullchain.pem> <privkey.pem>")
        print("  (certbot users: wire it as a --deploy-hook, see README.md)")
        print()

    def certs_status(self):
        """Check certificate configuration"""
        project_dir = self.config.get("project_dir", ".")
        certs_dir = os.path.join(project_dir, "certs")

        print(f"\n{bold('Certificate Status')}")
        print("-" * 50)

        # Check if mkcert is installed
        mkcert_installed = run_cmd("which mkcert 2>/dev/null")
        if mkcert_installed:
            print(f"  {green('●')} mkcert: installed")

            # Check if CA is installed
            ca_check = run_cmd("mkcert -check 2>&1")
            if "is not installed" in ca_check.lower():
                print(f"  {yellow('!')} mkcert CA: {yellow('not installed')}")
                print(f"      Run 'certs trust' to install CA for browser-trusted certificates")
            else:
                print(f"  {green('●')} mkcert CA: installed (browser-trusted)")
        else:
            print(f"  {gray('○')} mkcert: not installed")
            print(f"      Install with: sudo apt install mkcert  # or: brew install mkcert")

        # Check certificates directory
        print(f"\n  {bold('Certificates')}")
        if os.path.isdir(certs_dir):
            api_cert = os.path.join(certs_dir, "api", "cert.pem")
            if os.path.exists(api_cert):
                print(f"  {green('●')} API certificate: {api_cert}")
            else:
                print(f"  {red('○')} API certificate: not found")

            # List other cert directories
            for item in sorted(os.listdir(certs_dir)):
                item_path = os.path.join(certs_dir, item)
                if os.path.isdir(item_path) and item != "api":
                    cert_file = os.path.join(item_path, "fullchain.pem")
                    if os.path.exists(cert_file):
                        print(f"  {green('●')} {item}: found")
        else:
            print(f"  {red('○')} Certificates directory not found")
            print(f"      Run 'init' to generate certificates")

        print()

    def certs_trust(self):
        """Install mkcert CA for browser-trusted certificates"""
        print(f"\n{bold('Installing mkcert CA')}")
        print("-" * 50)

        # Check if mkcert is installed
        mkcert_installed = run_cmd("which mkcert 2>/dev/null")
        if not mkcert_installed:
            print(red("mkcert is not installed."))
            print("\nInstall mkcert first:")
            print("  Ubuntu/Debian: sudo apt install mkcert")
            print("  macOS:         brew install mkcert")
            print("\nThen run 'certs trust' again.")
            return

        print("Installing mkcert CA (this makes certificates browser-trusted)...\n")
        os.system("mkcert -install")

        print(f"\n{green('✓')} mkcert CA installed!")
        print("\nNext steps:")
        print("  1. Restart your browser")
        print("  2. If certificates were generated before CA install, regenerate them:")
        print("     rm -rf certs/")
        print("     voipbin> init")
        print()

    def cmd_network(self, args):
        """Manage VoIP network interfaces"""
        subcmd = args[0].lower() if args else "status"

        if subcmd == "status":
            self.network_status()
        elif subcmd == "setup":
            # Parse --external-ip if provided
            external_ip = None
            for i, arg in enumerate(args):
                if arg == "--external-ip" and i + 1 < len(args):
                    external_ip = args[i + 1]
                    break
            self.network_setup(external_ip)
        elif subcmd == "teardown":
            self.network_teardown()
        else:
            print("Usage: network [status|setup|teardown]")
            print("       network setup --external-ip X.X.X.X")

    def network_status(self):
        """Show VoIP network configuration status"""
        project_dir = self._project_dir()
        project = self._compose_project_name(project_dir)
        print(f"\n{bold('VoIP Network Configuration')}")
        print("=" * 60)

        # Get IPs from .env
        host_ip = run_cmd("grep '^HOST_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or "not set"
        kamailio_ip = run_cmd("grep '^KAMAILIO_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or ""
        rtpengine_ip = run_cmd("grep '^RTPENGINE_EXTERNAL_IP=' .env 2>/dev/null | cut -d'=' -f2 | head -1") or ""

        # Check internal interfaces
        print(f"\n{bold('Internal Interfaces')} (Docker bridge → host macvlan)")
        print("-" * 60)

        kamailio_int = run_cmd("ip addr show kamailio-int 2>/dev/null | grep -oP 'inet \\K[\\d./]+' | head -1")
        rtpengine_int = run_cmd("ip addr show rtpengine-int 2>/dev/null | grep -oP 'inet \\K[\\d./]+' | head -1")

        if kamailio_int:
            print(f"  {green('●')} kamailio-int:  {kamailio_int}")
        else:
            print(f"  {red('○')} kamailio-int:  not configured")

        if rtpengine_int:
            print(f"  {green('●')} rtpengine-int: {rtpengine_int}")
        else:
            print(f"  {red('○')} rtpengine-int: not configured")

        # Check Docker network
        print(f"\n{bold('Docker Networks')}")
        print("-" * 60)

        # Check if voip-internal network exists
        voip_internal = run_cmd(f"docker network inspect {shlex.quote(project + '_voip-internal')} --format '{{{{.Id}}}}' 2>/dev/null | head -c 12")
        if voip_internal:
            bridge_if = f"br-{voip_internal}"
            bridge_exists = run_cmd(f"ip link show {bridge_if} 2>/dev/null | head -1")
            if bridge_exists:
                print(f"  {green('●')} voip-internal: {bridge_if} (172.29.0.0/16)")
            else:
                print(f"  {yellow('!')} voip-internal: network exists but bridge not found")
        else:
            print(f"  {gray('○')} voip-internal: not created (run 'docker compose up -d' first)")

        default_network = run_cmd(f"docker network inspect {shlex.quote(project + '_default')} --format '{{{{.Id}}}}' 2>/dev/null | head -c 12")
        if default_network:
            print(f"  {green('●')} default:       br-{default_network} (172.28.0.0/16)")
        else:
            print(f"  {gray('○')} default:       not created")

        # External configuration
        print(f"\n{bold('External Configuration')}")
        print("-" * 60)
        print(f"  HOST_EXTERNAL_IP: {blue(host_ip)}")

        physical_iface = run_cmd("ip route get 8.8.8.8 2>/dev/null | grep -oP 'dev \\K\\S+' | head -1") or "eth0"

        if kamailio_ip:
            ip_on_iface = run_cmd(f"ip addr show {physical_iface} 2>/dev/null | grep -oP 'inet \\K{kamailio_ip}'")
            if ip_on_iface:
                print(f"  KAMAILIO_EXTERNAL_IP:   {green(kamailio_ip)} (configured on {physical_iface})")
            else:
                print(f"  KAMAILIO_EXTERNAL_IP:   {yellow(kamailio_ip)} (not yet applied)")
        else:
            print(f"  KAMAILIO_EXTERNAL_IP:   {red('not set')} (required for SIP)")

        if rtpengine_ip:
            ip_on_iface = run_cmd(f"ip addr show {physical_iface} 2>/dev/null | grep -oP 'inet \\K{rtpengine_ip}'")
            if ip_on_iface:
                print(f"  RTPENGINE_EXTERNAL_IP:  {green(rtpengine_ip)} (configured on {physical_iface})")
            else:
                print(f"  RTPENGINE_EXTERNAL_IP:  {yellow(rtpengine_ip)} (not yet applied)")
        else:
            print(f"  RTPENGINE_EXTERNAL_IP:  {red('not set')} (required for RTP media)")

        # Web services info
        print(f"\n{bold('Web Services')} (Docker port mapping)")
        print("-" * 60)
        print(f"  API:   {host_ip}:8443")
        print(f"  Admin: {host_ip}:3003")
        print(f"  Meet:  {host_ip}:3004")
        print(f"  Talk:  {host_ip}:3005")

        # Show physical interface IPs
        print(f"\n{bold('Physical Interface')}")
        print("-" * 60)
        physical_iface = run_cmd("ip route get 8.8.8.8 2>/dev/null | grep -oP 'dev \\K\\S+' | head -1") or "unknown"
        iface_ips = run_cmd(f"ip addr show {physical_iface} 2>/dev/null | grep -oP 'inet \\K[\\d./]+'")
        if iface_ips:
            print(f"  {physical_iface}:")
            for ip in iface_ips.split('\n'):
                if ip:
                    print(f"    {ip}")
        else:
            print(f"  Could not detect physical interface")

        print(f"\n{bold('Usage')}")
        print("-" * 60)
        print("  network setup                     Setup internal interfaces")
        print("  network setup --external-ip X.X.X.X  Add secondary IP for VoIP")
        print("  network teardown                  Remove interfaces")
        print()

    def network_setup(self, external_ip=None):
        """Setup VoIP network interfaces"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "setup-voip-network.sh")

        if not os.path.exists(script_path):
            print(red(f"Setup script not found: {script_path}"))
            return

        print(f"\n{bold('Setting up VoIP network interfaces...')}\n")

        cmd = script_path
        if external_ip:
            cmd += f" --external-ip {external_ip}"

        os.system(cmd)

    def network_teardown(self):
        """Teardown VoIP network interfaces"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "teardown-voip-network.sh")

        if not os.path.exists(script_path):
            print(red(f"Teardown script not found: {script_path}"))
            return

        print(f"\n{bold('Tearing down VoIP network interfaces...')}\n")
        os.system(script_path)

    def cmd_init(self, args):
        """Initialize sandbox"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "init.sh")

        if not os.path.exists(script_path):
            print(red(f"Init script not found: {script_path}"))
            return

        print("Running initialization script...")
        print("This will generate .env and certificates.\n")
        os.system(script_path)

    def cmd_doctor(self, args):
        """Run the install doctor (diagnose and prescribe, read-only)"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "doctor.sh")

        if not os.path.exists(script_path):
            print(red(f"Doctor script not found: {script_path}"))
            self.last_doctor_rc = 2
            return

        # subprocess.call returns a real exit code (the cmd_init precedent's
        # shell-out returns a raw wait status and must not be propagated).
        # Run via bash so a lost exec bit cannot raise PermissionError.
        # Only the non-interactive dispatch in main() exits with this value;
        # a bare sys.exit here would kill the interactive REPL, so the REPL
        # path just returns to the prompt.
        self.last_doctor_rc = subprocess.call(["bash", script_path], cwd=script_dir)

    def cmd_clean(self, args):
        """Cleanup sandbox"""
        if not args:
            print("Usage: clean [--containers] [--volumes] [--images] [--network] [--dns] [--purge] [--all]")
            print("")
            print("Options:")
            print("  --containers  Remove app containers (keeps infrastructure: db, redis, mq, dns)")
            print("  --volumes     Remove docker volumes (database, recordings, redis/rabbitmq state)")
            print("  --images      Remove docker images")
            print("  --network     Teardown VoIP network interfaces")
            print("  --dns         Remove DNS configuration")
            print("  --purge       Remove generated files (.env, certs, configs)")
            print("  --all         All of the above (full reset)")
            return

        # Infrastructure services to keep when using --containers
        INFRA_SERVICES = {"dns", "coredns", "redis", "rabbitmq", "mq", "db"}

        # Parse options
        clean_containers = "--containers" in args
        clean_volumes = "--volumes" in args or "--all" in args
        clean_images = "--images" in args or "--all" in args
        teardown_network = "--network" in args or "--all" in args
        teardown_dns = "--dns" in args or "--all" in args
        purge = "--purge" in args or "--all" in args

        project_dir = self.config.get("project_dir", ".")
        scripts_dir = os.path.join(project_dir, "scripts")

        # Stop and remove containers/volumes
        if clean_volumes:
            print("Stopping all services and removing volumes...")
            run_cmd("docker compose down -v 2>&1")
            # The test-data marker mirrors DB state (which lives in the
            # volumes), so it is removed with --volumes — aligned with
            # clean.sh --volumes (VOIP-1275 §2.10).
            marker_path = os.path.join(project_dir, ".test_data_initialized")
            if os.path.exists(marker_path):
                os.remove(marker_path)
                print("  Removed test data marker")
            print(green("✓ All containers and volumes removed"))
        elif clean_containers:
            # Remove only app containers, keep infrastructure
            print("Removing app containers (keeping infrastructure)...")
            all_services = run_cmd("docker compose ps --services 2>/dev/null") or ""
            if all_services:
                services_to_remove = []
                for svc in all_services.split('\n'):
                    svc = svc.strip()
                    if svc and svc not in INFRA_SERVICES:
                        services_to_remove.append(svc)

                if services_to_remove:
                    run_cmd(f"docker compose rm -fsv {' '.join(services_to_remove)} 2>&1")
                    print(green(f"✓ Removed {len(services_to_remove)} app containers"))
                    running_infra = INFRA_SERVICES & set(all_services.split())
                    if running_infra:
                        print(green(f"✓ Infrastructure still running: {', '.join(sorted(running_infra))}"))
                else:
                    print("No app containers to remove")
            else:
                print("No containers running")
        else:
            print("Stopping services...")
            run_cmd("docker compose down 2>&1")
            print(green("✓ Services stopped"))

        # Remove images
        if clean_images:
            print("\nRemoving docker images...")
            # Get images from docker compose
            compose_images = run_cmd("docker compose config --images 2>/dev/null") or ""
            # Get voipbin images
            voipbin_images = run_cmd("docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^voipbin/' 2>/dev/null") or ""
            # Get other sandbox images
            other_images = run_cmd("docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^(mysql|redis|rabbitmq|coredns/)' 2>/dev/null") or ""

            # Combine and deduplicate
            all_images = set()
            for img_list in [compose_images, voipbin_images, other_images]:
                for img in img_list.strip().split('\n'):
                    if img and '<none>' not in img:
                        all_images.add(img)

            removed = 0
            for image in sorted(all_images):
                result = run_cmd(f"docker rmi -f {image} 2>/dev/null")
                if result is not None:
                    print(f"  Removed: {image}")
                    removed += 1

            # Also try docker compose --rmi
            run_cmd("docker compose down --rmi all 2>/dev/null")
            print(green(f"✓ Removed {removed} docker images"))

        # Teardown network
        if teardown_network:
            print("\nTearing down VoIP network interfaces...")
            script_path = os.path.join(scripts_dir, "teardown-voip-network.sh")
            if os.path.exists(script_path):
                os.system(f"{script_path} 2>/dev/null || true")
                print(green("✓ Network interfaces removed"))
            else:
                print(yellow("! teardown-voip-network.sh not found"))

        # Teardown DNS
        if teardown_dns:
            print("\nRemoving DNS configuration...")
            script_path = os.path.join(scripts_dir, "setup-dns.sh")
            if os.path.exists(script_path):
                os.system(f"{script_path} --uninstall 2>/dev/null || true")
                print(green("✓ DNS configuration removed"))
            else:
                print(yellow("! setup-dns.sh not found"))

        # Purge generated files
        if purge:
            print("\nPurging generated files...")

            files_to_remove = [
                ("certs", "certificates directory"),
                (".env", ".env file"),
                # .test_data_initialized moved to the --volumes branch: the
                # marker mirrors DB state, which lives in the volumes.
                ("config/coredns", "CoreDNS config"),
                ("config/dummy-gcp-credentials.json", "dummy GCP credentials"),
                ("tmp", "tmp directory"),
                ("docker-compose.override.yml", "version pins"),
                (".voipbin-versions", "rollback history"),
            ]

            for path, desc in files_to_remove:
                full_path = os.path.join(project_dir, path)
                if os.path.exists(full_path):
                    if os.path.isdir(full_path):
                        import shutil
                        shutil.rmtree(full_path)
                    else:
                        os.remove(full_path)
                    print(f"  Removed {desc}")

            print(green("✓ Generated files purged"))

        print(f"\n{bold('Cleanup complete!')}")
        print("Run 'init' to initialize, then 'start' to begin.")

    def _show_running_images(self, project_dir, json_output=False):
        """Show currently running container images when no override file exists"""
        # Get running voipbin containers and their images
        result = run_cmd("docker compose ps --format json 2>/dev/null")

        running_images = []
        if result:
            import json as json_module
            for line in result.strip().split('\n'):
                if not line:
                    continue
                try:
                    container = json_module.loads(line)
                    service = container.get('Service', '')
                    image = container.get('Image', '')
                    state = container.get('State', '')

                    if image.startswith('voipbin/') and state == 'running':
                        # Get image ID (short form)
                        image_id_result = run_cmd(f"docker images {image} --format '{{{{.ID}}}}' 2>/dev/null")
                        image_id = image_id_result.strip()[:12] if image_id_result else 'unknown'

                        # Get image created time
                        created_result = run_cmd(f"docker images {image} --format '{{{{.CreatedSince}}}}' 2>/dev/null")
                        created = created_result.strip() if created_result else 'unknown'

                        running_images.append({
                            'service': service,
                            'image': image,
                            'image_id': image_id,
                            'created': created
                        })
                except Exception:
                    continue

        if json_output:
            print(json.dumps({
                "pinned": False,
                "message": "No version pins found. Showing running images.",
                "images": running_images
            }, indent=2))
            return

        print(f"\n{yellow('No version pins found.')} Using :latest tags from docker-compose.yml.")

        if running_images:
            print(f"\n{bold('Currently Running Images')}")
            print("=" * 70)
            print(f"  {'SERVICE':<25} {'IMAGE ID':<15} {'CREATED':<20}")
            print("  " + "-" * 65)

            for img in sorted(running_images, key=lambda x: x['service']):
                service = img['service'][:24]
                image_id = img['image_id'][:14]
                created = img['created'][:19]
                print(f"  {service:<25} {image_id:<15} {created:<20}")

            print(f"\n  Total: {len(running_images)} voipbin containers running")
        else:
            print(f"\n{yellow('No voipbin containers currently running.')}")
            print("Run 'voipbin start' to start services.")

        print(f"\n  Tip: Run '{bold('voipbin update')}' to pin to specific commit-SHA versions")

    def cmd_version(self, args):
        """Show pinned image versions from docker-compose.override.yml"""
        project_dir = self.config.get("project_dir", ".")
        override_file = os.path.join(project_dir, "docker-compose.override.yml")
        json_output = "--json" in args

        if not os.path.exists(override_file):
            # No override file - show currently running images instead
            self._show_running_images(project_dir, json_output)
            return

        # Parse override file
        images = []
        try:
            with open(override_file, "r") as f:
                content = f.read()

            # Get file modification time
            mtime = os.path.getmtime(override_file)
            updated = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

            # Parse services and images
            current_service = None
            for line in content.split("\n"):
                line = line.strip()
                if line.endswith(":") and not line.startswith("#") and not line.startswith("image"):
                    current_service = line[:-1]
                elif line.startswith("image:") and current_service:
                    image_full = line.split(":", 1)[1].strip()
                    if ":" in image_full:
                        image_name, tag = image_full.rsplit(":", 1)
                    else:
                        image_name, tag = image_full, "latest"
                    images.append({
                        "service": current_service,
                        "image": image_name,
                        "tag": tag
                    })
                    current_service = None

        except Exception as e:
            if json_output:
                print(json.dumps({"error": str(e), "images": []}))
            else:
                print(f"{red('Error reading override file:')} {e}")
            return

        # Get image creation times from docker inspect
        for img in images:
            full_image = f"{img['image']}:{img['tag']}"
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Created}}", full_image],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    # Parse ISO format: 2026-01-28T15:30:00.123456789Z
                    created_str = result.stdout.strip()
                    if created_str:
                        # Handle both formats with and without nanoseconds
                        created_str = created_str.split(".")[0]  # Remove nanoseconds
                        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        img["created"] = created_dt.strftime("%Y-%m-%d %H:%M")
                    else:
                        img["created"] = "-"
                else:
                    img["created"] = "-"
            except Exception:
                img["created"] = "-"

        if json_output:
            print(json.dumps({
                "file": override_file,
                "last_pulled": updated,
                "images": images
            }, indent=2))
            return

        # Display table
        print(f"\n{bold('Image Versions')} (from docker-compose.override.yml)")
        print("=" * 70)
        print(f"  {'SERVICE':<25} {'TAG':<12} {'IMAGE CREATED':<18}")
        print("  " + "-" * 65)

        for img in sorted(images, key=lambda x: x["service"]):
            service = img["service"][:24]
            tag = img["tag"][:11]
            created = img.get("created", "-")[:17]
            print(f"  {service:<25} {tag:<12} {created:<18}")

        print(f"\n  Total: {len(images)} services pinned")
        print(f"  Last pulled: {updated}")
        print(f"\n  Tip: Run '{bold('voipbin update')}' to pull latest versions")
        print(f"       Run '{bold('voipbin rollback --list')}' to see history")

    def cmd_update(self, args):
        """Update sandbox - pull images and/or update scripts from GitHub"""
        project_dir = self.config.get("project_dir", ".")
        check_only = "--check" in args
        skip_backup = "--skip-backup" in args
        resume_from = None
        backup_ts = None
        filtered = []
        for a in args:
            if a in ("--check", "--skip-backup"):
                continue
            if a.startswith("--resume-from="):
                resume_from = a.split("=", 1)[1]
                continue
            if a.startswith("--backup-ts="):
                backup_ts = a.split("=", 1)[1]
                continue
            filtered.append(a)
        args = filtered

        # Determine what to update
        subcommand = args[0] if args else ""

        pinned = os.path.exists(os.path.join(project_dir, "versions.lock"))

        if resume_from == "pull" and not pinned:
            # The pulled commit REMOVED versions.lock (unpin release). Falling
            # through to the unpinned path silently would skip migrations -
            # tell the operator what happened instead.
            print(f"{yellow('!')} Resumed after git pull, but versions.lock is GONE in the new")
            print("  commit (the release unpinned this repo). The pinned upgrade flow does")
            print("  not apply anymore - run 'update all' again to use the standard path,")
            print("  and run scripts/migrate.sh manually if the release requires migrations.")
            return

        if subcommand == "scripts":
            self._update_scripts(project_dir, check_only)
        elif subcommand == "all":
            if pinned:
                # Pinned repo: safe-ordered upgrade flow (design §3.5).
                self._upgrade_pinned(
                    project_dir,
                    check_only=check_only,
                    skip_backup=skip_backup,
                    resume_from=resume_from,
                    backup_ts=backup_ts,
                )
            else:
                self._update_scripts(project_dir, check_only)
                if not check_only:
                    print("")
                self._update_images(project_dir, check_only)
        elif subcommand in ("", "images"):
            self._update_images(project_dir, check_only)
        else:
            print(f"{red('Unknown subcommand:')} {subcommand}")
            print("Usage: update [scripts|images|all] [--check] [--skip-backup]")

    # -------------------------------------------------------------------------
    # Pinned-repo upgrade flow (design §3.5)
    # -------------------------------------------------------------------------

    def _compose_project_name(self, project_dir):
        """Derive the docker compose project name (for network naming).

        Mirrors common.sh's derive_compose_project_name(): COMPOSE_PROJECT_NAME
        when set (validated, used as-is — compose itself does not sanitize an
        explicit override), else the project directory's basename, lowercased,
        filtered to [a-z0-9_-], with any leading '-'/'_' stripped. A basename
        that filters down to nothing (e.g. "---" or "!!!") raises the same
        ValueError as an invalid explicit COMPOSE_PROJECT_NAME, rather than
        silently falling back to a fixed name — the bash side
        (derive_compose_project_name) fails the same way, and callers already
        need to handle failure here.
        """
        env_name = os.environ.get("COMPOSE_PROJECT_NAME")
        if env_name:
            if not re.match(r"^[a-z0-9][a-z0-9_-]*$", env_name):
                raise ValueError(f"Invalid COMPOSE_PROJECT_NAME: {env_name!r}")
            return env_name
        base = os.path.basename(os.path.abspath(project_dir)).lower()
        filtered = re.sub(r"[^a-z0-9_-]", "", base)
        stripped = filtered.lstrip("-_")
        if not stripped:
            raise ValueError(
                f"Could not derive a compose project name from {project_dir!r} "
                "(set COMPOSE_PROJECT_NAME)"
            )
        return stripped

    def _upgrade_pinned(self, project_dir, check_only=False, skip_backup=False,
                        resume_from=None, backup_ts=None):
        """Safe-ordered upgrade for a pinned repo:
        backup -> git pull -> re-exec -> compose pull -> migrate -> up -d -> verify

        The git pull in step 2 overwrites THIS running file. To avoid executing
        stale code for steps 3-6, we re-exec the CLI after step 2 with
        --resume-from=pull (which doubles as the re-entry loop guard).
        """
        print(f"\n{bold('Pinned Upgrade (update all)')}")
        print("=" * 50)

        if check_only:
            print(f"\n{yellow('Dry-run:')} pinned repo detected (versions.lock). 'update all' would run:")
            print("  1. backup                  - full data backup into backups/<ts>/ (skip with --skip-backup)")
            print("  2. update scripts          - git pull (new docker-compose.yml + versions.lock + scripts)")
            print("     (the CLI then re-execs itself so steps 3-6 run the NEW code)")
            print("  3. docker compose pull     - fetch the new pinned digests")
            print("  4. scripts/migrate.sh      - alembic migrations at the new dbscheme pin (abort on failure)")
            print("  5. docker compose up -d    - recreate only containers whose digest changed")
            print("  6. verify                  - poll container health (120s) + GET api-manager /ping")
            print(f"\n{yellow('No changes made.')} Run 'update all' without --check to upgrade.")
            return

        if resume_from != "pull":
            # Concurrency lock: held from here THROUGH the execv (the lock file
            # survives the process image swap) and released at the end of the
            # resumed run. Blocks concurrent backup / second update all.
            lock = self._acquire_op_lock(project_dir, "update all")
            if not lock:
                return
            # Any unexpected exception (incl. Ctrl-C during mysqldump) before
            # the execv must not leak the lock: on the success path execv
            # replaces the process so this except never runs, and the resumed
            # process owns/releases the lock via atexit.
            try:
                self._upgrade_pinned_pre_exec(project_dir, lock, skip_backup, backup_ts)
            except BaseException:
                self._release_op_lock(lock)
                raise
            return  # only reached on abort paths inside pre_exec

        # ---- Resumed process (new code) continues from step 3. NEVER re-exec here. ----
        # The op lock acquired before the execv is still on disk; release it on
        # ANY exit path of this process (abort or success) via atexit.
        _lock_path = os.path.join(project_dir, ".voipbin-op.lock")
        atexit.register(self._release_op_lock, _lock_path)
        print(f"\n{blue('==>')} Resumed after script update (steps 1-2 already done).")
        if backup_ts:
            print(f"  Pre-upgrade backup: backups/{backup_ts}/")

        restore_hint = f"voipbin restore {backup_ts} --force" if backup_ts else "voipbin restore <ts> --force"

        # ---- Step 3: docker compose pull (fetch the NEW pinned digests) ----
        print(f"\n{blue('==>')} Step 3/6: pulling new pinned images (docker compose pull)...")
        rc = subprocess.call("docker compose pull", shell=True, cwd=project_dir)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} docker compose pull failed (exit {rc}).")
            print("  Old containers are still running the old digests; safe to re-run 'update all'.")
            return

        # ---- Step 4: migrations (surfaced live, abort on failure) ----
        migrate_script = os.path.join(project_dir, "scripts", "migrate.sh")
        print(f"\n{blue('==>')} Step 4/6: running database migrations (scripts/migrate.sh)...")
        if not os.path.exists(migrate_script):
            print(f"\n{red('Upgrade aborted:')} {migrate_script} not found.")
            print("  The 'update scripts' step may have pulled an incomplete tree -")
            print("  check 'git status' in the repo, or re-clone and retry 'update all'.")
            return
        rc = subprocess.call(["bash", migrate_script], cwd=project_dir)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} migration failed (exit {rc}).")
            print(f"\n{yellow('MySQL DDL is non-transactional - the migration may be PARTIALLY applied.')}")
            print("  Recovery procedure:")
            print("    1. voipbin stop --all")
            print(f"    2. {restore_hint}    (restores pre-upgrade schema + data)")
            print("    3. git checkout <previous commit>   (reverts compose/versions.lock/scripts)")
            print("    4. voipbin start")
            return

        # ---- Step 5: recreate changed containers ----
        print(f"\n{blue('==>')} Step 5/6: recreating services (docker compose up -d)...")
        rc = subprocess.call("docker compose up -d", shell=True, cwd=project_dir)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} docker compose up -d failed (exit {rc}).")
            print(f"  If services are broken: stop, then '{restore_hint}' and git checkout the previous commit.")
            return

        # ---- Step 6: verify ----
        print(f"\n{blue('==>')} Step 6/6: verifying stack health...")
        if self._verify_stack(project_dir):
            print(f"\n{bold(green('Upgrade complete!'))}")
            if backup_ts:
                print(f"  Pre-upgrade backup kept at backups/{backup_ts}/")
            print("  Run 'voipbin status' to inspect services.")
        else:
            print(f"\n{red('Upgrade verification FAILED.')}")
            print("  Recovery options:")
            print("    - Data rollback:  stop services, then run:")
            print(f"        {restore_hint}")
            print("    - Image rollback: git checkout the previous repo commit")
            print("      (docker-compose.yml + versions.lock + scripts are git-versioned), then")
            print("      'docker compose up -d'.")
            print(f"  {yellow('Do NOT use')} 'voipbin rollback' here: it replays docker-compose.override.yml")
            print("  snapshots that only exist on UNPINNED repos and would silently unpin this repo.")

    def _upgrade_pinned_pre_exec(self, project_dir, lock, skip_backup, backup_ts):
        """Steps 1-2 of the pinned upgrade + the re-exec. Runs with the op lock
        held; the CALLER releases the lock if this raises. Returning (instead
        of exec'ing) means the upgrade aborted - the caller just returns."""
        # ---- Step 1: backup (BEFORE git pull - captures the OLD pin state) ----
        if skip_backup:
            print(f"\n{yellow('==>')} Step 1/6: backup SKIPPED (--skip-backup)")
        else:
            print(f"\n{blue('==>')} Step 1/6: creating pre-upgrade backup...")
            backup_ts = self._do_backup(project_dir)
            if not backup_ts:
                print(f"\n{red('Upgrade aborted:')} backup failed. Nothing was changed.")
                print("  Fix the backup problem, or re-run with --skip-backup to opt out (not recommended).")
                self._release_op_lock(lock)
                return

        # ---- Step 2: update scripts (git pull) ----
        print(f"\n{blue('==>')} Step 2/6: updating scripts (git pull)...")
        if not self._update_scripts(project_dir, check_only=False):
            print(f"\n{red('Upgrade aborted:')} git pull failed. The old pin is intact.")
            if backup_ts:
                print(f"  Pre-upgrade backup: backups/{backup_ts}/ (untouched state, nothing to restore)")
            self._release_op_lock(lock)
            return

        # ---- Re-exec: the git pull may have overwritten this very file. ----
        # CPython loaded the OLD code at startup; steps 3-6 must run the NEW
        # code. --resume-from=pull is the loop guard: the resumed process
        # never re-execs again.
        # NOTE for interactive-shell users: execv REPLACES this process, so
        # after the upgrade finishes the CLI exits instead of returning to
        # the voipbin> prompt. Communicate that now.
        print(f"\n{blue('==>')} Re-executing CLI to continue with the updated code...")
        print("  (After the upgrade completes, the CLI will exit - start it again")
        print("   with 'sudo ./voipbin' if you were in the interactive shell.)")
        argv = [sys.executable, os.path.abspath(__file__), "update", "all", "--resume-from=pull"]
        if backup_ts:
            argv.append(f"--backup-ts={backup_ts}")
        if skip_backup:
            argv.append("--skip-backup")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, argv)

    def _verify_stack(self, project_dir, timeout=120):
        """Poll compose health until no container is starting/unhealthy, then
        ping api-manager from a helper container on the compose network."""
        deadline = time.time() + timeout
        bad = []
        while time.time() < deadline:
            output = run_cmd("docker compose ps --format json 2>/dev/null")
            entries = []
            if output:
                for line in output.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, list):
                        entries.extend(parsed)
                    else:
                        entries.append(parsed)
            bad = []
            for e in entries:
                name = e.get("Name") or e.get("Service") or "?"
                # Containers WITHOUT a healthcheck have no/empty Health field -
                # treat absent as OK.
                health = (e.get("Health") or "").lower()
                state = (e.get("State") or "").lower()
                if health in ("starting", "unhealthy"):
                    bad.append((name, health))
                elif state == "restarting":
                    bad.append((name, state))
            if not bad:
                break
            print(f"\r  Waiting for {len(bad)} container(s) to become healthy... ", end="", flush=True)
            time.sleep(5)
        print()

        if bad:
            print(f"  {red('✗')} Timed out after {timeout}s. Problem containers:")
            for name, why in bad:
                print(f"    - {name}: {why}")
            print("  Inspect why with: docker compose logs --tail 50 <service>")
            return False
        print(f"  {green('✓')} All containers healthy (or no healthcheck defined)")

        # API ping from a helper container on the compose network
        # (the bin-api-manager image may lack curl).
        # Resolve the network the SAME way migrate.sh does: from the running db
        # container's attachments (robust against COMPOSE_PROJECT_NAME overrides),
        # falling back to the derived <project>_default name.
        db_container = self.config.get("db_container", "voipbin-db")
        network = run_cmd(
            "docker inspect " + shlex.quote(db_container) +
            " -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' 2>/dev/null"
        )
        network = network.strip().splitlines()[0].strip() if network and network.strip() else \
            f"{self._compose_project_name(project_dir)}_default"
        print(f"  Pinging api-manager via network '{network}'...")
        result = subprocess.run(
            f"docker run --rm --network {shlex.quote(network)} curlimages/curl "
            f"-sk -o /dev/null -w '%{{http_code}}' --max-time 15 https://api-manager:443/ping",
            shell=True, capture_output=True, text=True, cwd=project_dir,
        )
        code = (result.stdout or "").strip()
        if result.returncode == 0 and code and code != "000":
            print(f"  {green('✓')} api-manager /ping responded (HTTP {code})")
            return True
        print(f"  {red('✗')} api-manager /ping failed (curl exit {result.returncode}, HTTP '{code or '-'}')")
        if result.stderr:
            print(f"    {result.stderr.strip().splitlines()[-1]}")
        return False

    # -------------------------------------------------------------------------
    # Backup / Restore (design §3.4)
    # -------------------------------------------------------------------------

    DATA_BACKUP_DIR = "backups"
    DATA_BACKUP_KEEP = 7
    RECORDING_VOLUMES = [
        ("asterisk-call-recording", "recordings-call.tar.gz"),
        ("asterisk-conf-recording", "recordings-conf.tar.gz"),
    ]

    def _resolve_volume(self, project_dir, volume):
        """Resolve a compose volume name to its real docker volume name.

        Compose prefixes named volumes with the project name
        (e.g. sandbox_asterisk-call-recording). Prefer an exact-name match,
        then <project>_<name>, then any single suffix match."""
        out = run_cmd("docker volume ls --format '{{.Name}}'") or ""
        names = [n.strip() for n in out.strip().split("\n") if n.strip()]
        if volume in names:
            return volume
        prefixed = f"{self._compose_project_name(project_dir)}_{volume}"
        if prefixed in names:
            return prefixed
        suffix_matches = [n for n in names if n.endswith(f"_{volume}")]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        return None

    def _acquire_op_lock(self, project_dir, op):
        """Advisory lock so backup/upgrade cannot run concurrently.

        mysqldump --single-transaction is NOT safe against concurrent DDL:
        a backup taken while migrations run could produce a 'valid-looking'
        dump of a half-migrated schema. Returns the lock path on success,
        None if another operation holds the lock.
        """
        lock_path = os.path.join(project_dir, ".voipbin-op.lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{op} pid={os.getpid()} {time.strftime('%F %T')}\n".encode())
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                holder = open(lock_path).read().strip()
            except Exception:
                holder = "unknown"
            print(f"\n{red('Another voipbin operation is in progress:')} {holder or 'unknown (lock just created?)'}")
            print(f"  Refusing to run '{op}' concurrently (backup during a migration")
            print("  would capture a half-migrated schema).")
            # Liveness hint: if the recorded pid is dead, the lock is stale.
            stale_hint = ""
            pid_m = re.search(r"pid=(\d+)", holder or "")
            if pid_m:
                try:
                    os.kill(int(pid_m.group(1)), 0)
                except ProcessLookupError:
                    stale_hint = " (that pid is NOT running - the lock looks stale)"
                except PermissionError:
                    pass  # pid exists under another user
            print(f"  If that operation crashed, remove the stale lock: rm {lock_path}{stale_hint}")
            return None

    def _release_op_lock(self, lock_path):
        try:
            if lock_path and os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass

    def cmd_backup(self, args):
        """Create a full data backup (MySQL + recordings + config)"""
        project_dir = self.config.get("project_dir", ".")
        lock = self._acquire_op_lock(project_dir, "backup")
        if not lock:
            return
        try:
            ts = self._do_backup(project_dir)
        finally:
            self._release_op_lock(lock)
        if ts:
            print(f"\n{bold(green('Backup complete:'))} backups/{ts}/")
            print(f"  {yellow('Reminder:')} this backup contains your full .env secrets in plaintext,")
            print("  and a backup on the same disk dies with the disk. Copy it off-host, e.g.:")
            print(f"    rsync -a {os.path.join(project_dir, self.DATA_BACKUP_DIR, ts)} user@other-host:/backups/voipbin/")
        else:
            print(f"\n{red('Backup FAILED.')}")

    def _do_backup(self, project_dir):
        """Create backups/<ts>/ with mysql dump, recordings tars, config copies
        and a manifest. Returns the timestamp string on success, None on failure
        (callers MUST abort on None)."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backups_base = os.path.join(project_dir, self.DATA_BACKUP_DIR)
        backup_dir = os.path.join(backups_base, ts)

        def fail(msg):
            print(f"  {red('✗')} {msg}")
            # Remove the partial backup so it can never be restored from.
            try:
                if os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir)
            except Exception:
                pass
            return None

        try:
            # exist_ok=False: if a backup with the same second-resolution ts
            # already exists, refuse instead of silently reusing the directory
            # (a later fail()/rmtree would otherwise destroy the earlier,
            # already-reported-successful backup).
            os.makedirs(backup_dir, exist_ok=False)
            os.chmod(backups_base, 0o700)
            os.chmod(backup_dir, 0o700)
        except FileExistsError:
            print(f"{red('✗')} Backup {ts} already exists - retry in 1 second.")
            return None
        except Exception as e:
            return fail(f"Could not create backup directory: {e}")

        print(f"\n{bold('Data Backup')} -> backups/{ts}/")
        print("=" * 50)

        # 1. MySQL dump (consistent for InnoDB while services run)
        print(f"\n{blue('==>')} Dumping MySQL (all databases)...")
        dump_path = os.path.join(backup_dir, "mysql.sql.gz")
        db_container = self.config.get("db_container", "voipbin-db")
        dump_cmd = (
            "set -o pipefail; "
            # password via the container's own env, not host argv: a literal
            # -p<password> on the docker exec command line is visible to every
            # user on the host via 'ps aux' for the whole dump duration.
            f"docker exec {shlex.quote(db_container)} "
            "sh -c 'exec mysqldump --single-transaction --routines "
            "--all-databases -uroot -p\"${MYSQL_ROOT_PASSWORD:-root_password}\"' "
            f"| gzip > {shlex.quote(dump_path)}"
        )
        rc = subprocess.call(["bash", "-c", dump_cmd])
        if rc != 0 or not os.path.exists(dump_path) or os.path.getsize(dump_path) == 0:
            return fail(f"mysqldump failed (exit {rc}). Is voipbin-db running?")
        print(f"  {green('✓')} mysql.sql.gz ({os.path.getsize(dump_path)} bytes)")

        # 2. Recording volumes
        abs_backup_dir = os.path.abspath(backup_dir)
        for volume, archive in self.RECORDING_VOLUMES:
            real_vol = self._resolve_volume(project_dir, volume)
            if not real_vol:
                print(f"  {yellow('!')} volume {volume} not found - skipping (no recordings yet?)")
                continue
            print(f"\n{blue('==>')} Archiving volume {real_vol}...")
            rc = subprocess.call(
                f"docker run --rm -v {shlex.quote(real_vol)}:/src:ro "
                f"-v {shlex.quote(abs_backup_dir)}:/dst alpine "
                f"tar czf /dst/{shlex.quote(archive)} -C /src .",
                shell=True,
            )
            if rc != 0:
                return fail(f"Archiving volume {real_vol} failed (exit {rc}).")
            print(f"  {green('✓')} {archive}")

        # 3. Config copies (.env, certs/, versions.lock)
        print(f"\n{blue('==>')} Copying config (.env, certs/, versions.lock)...")
        config_dir = os.path.join(backup_dir, "config")
        try:
            os.makedirs(config_dir, exist_ok=True)
            env_path = os.path.join(project_dir, ".env")
            if os.path.exists(env_path):
                shutil.copy2(env_path, os.path.join(config_dir, ".env"))
            lock_path = os.path.join(project_dir, "versions.lock")
            if os.path.exists(lock_path):
                shutil.copy2(lock_path, os.path.join(config_dir, "versions.lock"))
            certs_path = os.path.join(project_dir, "certs")
            if os.path.isdir(certs_path):
                shutil.copytree(certs_path, os.path.join(config_dir, "certs"), dirs_exist_ok=True)
            print(f"  {green('✓')} config copied")
        except Exception as e:
            return fail(f"Config copy failed: {e}")

        # 4. Manifest
        try:
            target_commit = ""
            lock_path = os.path.join(project_dir, "versions.lock")
            if os.path.exists(lock_path):
                try:
                    with open(lock_path) as lf:
                        target_commit = json.load(lf).get("target_commit", "")
                except Exception:
                    target_commit = ""
            compose_sha256 = ""
            compose_path = os.path.join(project_dir, "docker-compose.yml")
            if os.path.exists(compose_path):
                h = hashlib.sha256()
                with open(compose_path, "rb") as cf:
                    for chunk in iter(lambda: cf.read(65536), b""):
                        h.update(chunk)
                compose_sha256 = h.hexdigest()

            sizes = {}
            for root, _dirs, files in os.walk(backup_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    sizes[os.path.relpath(fp, backup_dir)] = os.path.getsize(fp)

            manifest = {
                "timestamp": ts,
                "target_commit": target_commit,
                "compose_sha256": compose_sha256,
                "sizes": sizes,
            }
            with open(os.path.join(backup_dir, "manifest.json"), "w") as mf:
                json.dump(manifest, mf, indent=2)
        except Exception as e:
            return fail(f"Manifest write failed: {e}")

        # 5. Tighten permissions (backup contains the full .env secret set)
        try:
            for root, dirs, files in os.walk(backup_dir):
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o700)
                for fn in files:
                    os.chmod(os.path.join(root, fn), 0o600)
        except Exception as e:
            return fail(f"Could not set backup permissions: {e}")

        # 6. Retention: keep the last N TIMESTAMP-SHAPED backups. Non-timestamp
        # directories (e.g. operator's manual copies) are never touched, and
        # the backup just taken can never be pruned.
        try:
            entries = sorted(
                d for d in os.listdir(backups_base)
                if os.path.isdir(os.path.join(backups_base, d))
                and re.fullmatch(r"\d{8}-\d{6}", d)
            )
            for old in entries[:-self.DATA_BACKUP_KEEP]:
                if old == ts:
                    continue
                shutil.rmtree(os.path.join(backups_base, old))
                print(f"  Pruned old backup: {old}")
        except Exception as e:
            print(f"  {yellow('!')} Retention cleanup warning: {e}")

        print(f"\n  {green('✓')} Backup written to backups/{ts}/")
        return ts

    def cmd_restore(self, args):
        """Restore data from a backup (DESTRUCTIVE)"""
        project_dir = self.config.get("project_dir", ".")
        backups_base = os.path.join(project_dir, self.DATA_BACKUP_DIR)

        available = []
        if os.path.isdir(backups_base):
            available = sorted(
                d for d in os.listdir(backups_base)
                if os.path.isdir(os.path.join(backups_base, d))
            )

        positional = [a for a in args if not a.startswith("--")]
        force = "--force" in args

        if not positional:
            print(f"\n{bold('Available backups')} (backups/)")
            print("=" * 50)
            if not available:
                print("  (none - run 'voipbin backup' first)")
            for b in available:
                print(f"  {b}")
            print(f"\nUsage: restore <timestamp> --force")
            print(f"{yellow('DESTRUCTIVE:')} overwrites current MySQL data and recordings.")
            return

        ts = positional[0]
        # ts must be a backup timestamp - rejects path traversal
        # ('restore ../../etc') and anything not produced by _do_backup.
        if not re.fullmatch(r"\d{8}-\d{6}", ts):
            print(f"{red('Invalid backup timestamp:')} {ts}")
            print("  Expected format: YYYYmmdd-HHMMSS (as listed by 'restore' with no args).")
            return
        backup_dir = os.path.join(backups_base, ts)
        if not os.path.isdir(backup_dir):
            print(f"{red('Backup not found:')} backups/{ts}/")
            if available:
                print("Available: " + ", ".join(available))
            return

        if not force:
            print(f"\n{red('Refusing to restore without --force.')}")
            print(f"  'restore {ts}' will OVERWRITE the current MySQL data, recordings and .env")
            print(f"  with the contents of backups/{ts}/. This cannot be undone.")
            print(f"  If you are sure: restore {ts} --force")
            return

        # Guard: all services must be stopped except db and redis.
        # Scope: the compose PROJECT the target db container belongs to
        # (via the com.docker.compose.project label). This is checked against
        # ACTUAL RUNNING CONTAINERS, so a COMPOSE_PROJECT_NAME mismatch cannot
        # let the target stack slip past the guard (Round-1 finding M1), while
        # unrelated stacks on the same host do not block the restore.
        db_container = self.config.get("db_container", "voipbin-db")
        redis_container = self.config.get("redis_container", "voipbin-redis")
        ps_out = run_cmd("docker ps --format '{{.Names}}' 2>/dev/null") or ""
        running_names = [n.strip() for n in ps_out.split("\n") if n.strip()]
        if db_container not in running_names:
            print(f"\n{red('Database is not running.')} Restore needs db (and redis) up:")
            print("  docker compose up -d db redis")
            return
        project = run_cmd(
            f"docker inspect {shlex.quote(db_container)} "
            "-f '{{index .Config.Labels \"com.docker.compose.project\"}}' 2>/dev/null"
        )
        project = (project or "").strip()
        if project:
            stack_out = run_cmd(
                "docker ps --format '{{.Names}}' "
                f"--filter label=com.docker.compose.project={shlex.quote(project)} 2>/dev/null"
            ) or ""
            stack_names = [n.strip() for n in stack_out.split("\n") if n.strip()]
        else:
            # db container not compose-managed: fall back to blocking on any
            # running voipbin-* container other than db/redis (conservative).
            stack_names = [n for n in running_names if "voipbin" in n]
        allowed_names = {db_container, redis_container}
        extra = sorted(n for n in stack_names if n not in allowed_names)
        if extra:
            print(f"\n{red('Refusing to restore while services are running.')}")
            print("  Restore requires ALL services stopped except db and redis. Still running")
            print(f"  in the target stack ({project or 'unlabeled'}):")
            for s in extra:
                print(f"    - {s}")
            print("\n  Stop them first:  voipbin stop --all && docker compose up -d db redis")
            return

        # Backup integrity gate: a partial backup (interrupted mid-write)
        # has no manifest - refuse to restore from it (Round-1 finding M3).
        manifest_path = os.path.join(backup_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            print(f"{red('✗')} manifest.json missing in backups/{ts}/ - backup is incomplete")
            print("  (probably interrupted). Refusing to restore from a partial backup.")
            return
        rc = subprocess.call(["gunzip", "-t", os.path.join(backup_dir, "mysql.sql.gz")]) \
            if os.path.exists(os.path.join(backup_dir, "mysql.sql.gz")) else 1
        if rc != 0:
            print(f"{red('✗')} mysql.sql.gz missing or corrupt in backups/{ts}/ - aborting.")
            return

        print(f"\n{bold('Restoring from')} backups/{ts}/")
        print("=" * 50)

        # 1. MySQL import
        dump_path = os.path.join(backup_dir, "mysql.sql.gz")
        if not os.path.exists(dump_path):
            print(f"{red('✗')} {dump_path} missing - backup is incomplete, aborting.")
            return
        print(f"\n{blue('==>')} Importing MySQL dump...")
        rc = subprocess.call([
            "bash", "-c",
            "set -o pipefail; "
            f"gunzip -c {shlex.quote(dump_path)} "
            # password via container env, not host argv (ps aux exposure)
            f"| docker exec -i {shlex.quote(self.config.get('db_container', 'voipbin-db'))} "
            "sh -c 'exec mysql -uroot -p\"${MYSQL_ROOT_PASSWORD:-root_password}\"'",
        ])
        if rc != 0:
            print(f"{red('✗')} MySQL import failed (exit {rc}). Database may be in a partial state.")
            print(f"  Re-run 'restore {ts} --force' after fixing the problem.")
            return
        print(f"  {green('✓')} MySQL restored")

        # 2. Recordings
        for volume, archive in self.RECORDING_VOLUMES:
            archive_path = os.path.join(backup_dir, archive)
            if not os.path.exists(archive_path):
                print(f"  {yellow('!')} {archive} not in backup - skipping volume {volume}")
                continue
            real_vol = self._resolve_volume(project_dir, volume) or volume
            print(f"\n{blue('==>')} Restoring volume {real_vol}...")
            rc = subprocess.call(
                f"docker run --rm -v {shlex.quote(real_vol)}:/dst "
                f"-v {shlex.quote(os.path.abspath(backup_dir))}:/src:ro alpine "
                "sh -c " + shlex.quote(f"rm -rf /dst/* && tar xzf /src/{archive} -C /dst"),
                shell=True,
            )
            if rc != 0:
                print(f"{red('✗')} Restoring volume {volume} failed (exit {rc}). Aborting.")
                print(f"  {yellow('Note:')} MySQL was already imported from this backup, so the stack")
                print("  is in a PARTIALLY-restored state. Fix the volume issue and re-run")
                print(f"  'restore {ts} --force' before starting services.")
                return
            print(f"  {green('✓')} {volume} restored")

        # 3. Flush Redis AFTER the import: bin-* services read cache-first with
        # 24h TTLs, so stale Redis rows would shadow the restored MySQL state
        # for up to a day (and Redis is AOF-persistent now).
        if redis_container in running_names:
            print(f"\n{blue('==>')} Flushing Redis cache...")
            rc = subprocess.call(f"docker exec {shlex.quote(redis_container)} redis-cli FLUSHALL", shell=True)
            if rc != 0:
                print(f"  {yellow('!')} Redis FLUSHALL failed - flush manually before starting services:")
                print("    docker exec voipbin-redis redis-cli FLUSHALL")
            else:
                print(f"  {green('✓')} Redis flushed")
        else:
            print(f"\n{yellow('!')} Redis is not running - flush it before starting services:")
            print("    docker compose up -d redis && docker exec voipbin-redis redis-cli FLUSHALL")

        # 4. Config: restore .env (keeping a .env.pre-restore copy of the current one)
        backed_env = os.path.join(backup_dir, "config", ".env")
        if os.path.exists(backed_env):
            print(f"\n{blue('==>')} Restoring .env...")
            current_env = os.path.join(project_dir, ".env")
            try:
                if os.path.exists(current_env):
                    pre_path = os.path.join(project_dir, ".env.pre-restore")
                    if os.path.exists(pre_path):
                        # never silently clobber an earlier pre-restore copy
                        pre_path = os.path.join(
                            project_dir,
                            f".env.pre-restore.{time.strftime('%Y%m%d-%H%M%S')}",
                        )
                    shutil.copy2(current_env, pre_path)
                    print(f"  Current .env saved as {os.path.basename(pre_path)}")
                shutil.copy2(backed_env, current_env)
                print(f"  {green('✓')} .env restored")
            except Exception as e:
                print(f"  {red('✗')} .env restore failed: {e}")
                print(f"  Copy it manually: cp backups/{ts}/config/.env .env")
                return
        cfg_dir = os.path.join(backup_dir, "config")
        if os.path.isdir(cfg_dir):
            print(f"  Note: certs/ and versions.lock copies are available in backups/{ts}/config/")
            print("  (not applied automatically - they normally track the git repo state).")

        print(f"\n{bold(green('Restore complete.'))} Next steps:")
        print("  1. voipbin start          (or: docker compose up -d)")
        print("  2. voipbin status         (verify all services are healthy)")

    def _update_images(self, project_dir, check_only=False):
        """Pull Docker images with version pinning to commit-SHA tags"""
        print(f"\n{bold('Docker Image Update')}")
        print("=" * 50)

        override_file = os.path.join(project_dir, "docker-compose.override.yml")
        versions_dir = os.path.join(project_dir, ".voipbin-versions")

        # PIN GUARD: if versions.lock exists, this sandbox is pinned to a
        # verified release snapshot. Re-resolving "latest" from Docker Hub would
        # silently break the pin and reintroduce the image/schema mismatch that
        # versions.lock exists to prevent. Honor the pin: just pull the digests
        # already written in docker-compose.yml. The pin is intentionally sticky;
        # changing it is a deliberate maintainer action, not a routine update.
        lock_file = os.path.join(project_dir, "versions.lock")
        if os.path.exists(lock_file):
            print(f"\n{yellow('==>')} versions.lock found - this sandbox is PINNED to a verified snapshot.")
            print(f"  {blue('Skipping')} 'latest' re-resolution to preserve the pin.")
            print(f"  Pulling the pinned image digests from docker-compose.yml instead.")
            try:
                with open(lock_file) as lf:
                    _lock = json.load(lf)
                tgt = _lock.get("target_commit", "")
                if tgt:
                    print(f"  Pinned snapshot: {tgt[:12]} ({_lock.get('target_commit_desc','')})")
            except Exception:
                pass
            if check_only:
                print(f"\n{yellow('Dry-run:')} pinned - no changes. Pinned digests are in docker-compose.yml.")
                print(f"  Real upgrades on a pinned repo go through '{bold('voipbin update all')}'.")
                return
            print(f"\n{blue('==>')} docker compose pull (pinned digests)...")
            result = run_cmd("docker compose pull 2>&1")
            if result:
                for line in result.strip().split("\n")[-12:]:
                    print(f"  {line}")
            print(f"\n  {green('✓')} Pinned images pulled. To change the pin, edit versions.lock + docker-compose.yml deliberately.")
            print(f"  {yellow('Hint:')} pulling alone is a no-op for upgrades - new digests only arrive via git.")
            print(f"  Real upgrades = '{bold('voipbin update all')}' (backup -> git pull -> pull -> migrate -> up -> verify).")
            return

        # Get list of voipbin images and their service mappings
        images, image_to_services = get_voipbin_images_from_compose(project_dir)
        if not images:
            print(red("No voipbin images found in docker-compose.yml"))
            return

        print(f"\n{blue('==>')} Resolving image tags from Docker Hub...")
        print(f"  Found {len(images)} voipbin images")

        # Progress callback
        def progress(current, total, image):
            # Extract short name for display
            short_name = image.split("/")[-1] if "/" in image else image
            print(f"\r  Resolving... [{current}/{total}] {short_name:<30}", end="", flush=True)

        # Resolve tags in parallel
        results = resolve_image_tags_parallel(images, progress_callback=progress)
        print()  # New line after progress

        # Separate successful and failed resolutions
        resolved = []
        warnings = []
        for r in results:
            if r["tag"]:
                resolved.append(r)
            else:
                warnings.append(r)

        # Show warnings
        if warnings:
            print(f"\n{yellow('Warnings:')}")
            for w in warnings:
                short_name = w["image"].split("/")[-1]
                print(f"  ! {short_name}: {w['error']}")

        print(f"\n  {green('✓')} Resolved {len(resolved)}/{len(images)} images")

        if check_only:
            # Show current vs resolved
            print(f"\n  {'IMAGE':<35} {'RESOLVED TAG':<20}")
            print("  " + "-" * 55)
            for r in sorted(resolved, key=lambda x: x["image"]):
                short_name = r["image"].split("/")[-1][:34]
                tag = r["tag"][:19] if r["tag"] else yellow("(keeping latest)")
                print(f"  {short_name:<35} {tag:<20}")
            print(f"\n{yellow('Dry-run mode:')} No changes made.")
            print("Run 'update' without --check to apply changes.")
            return

        if not resolved:
            print(yellow("No images resolved. Falling back to docker compose pull."))
            result = run_cmd("docker compose pull 2>&1")
            if result:
                for line in result.strip().split('\n')[-10:]:
                    print(f"  {line}")
            return

        # Backup existing override file (if exists)
        os.makedirs(versions_dir, exist_ok=True)
        if os.path.exists(override_file):
            print(f"\n{blue('==>')} Backing up current version...")
            timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            backup_file = os.path.join(versions_dir, f"{timestamp}.yml")
            shutil.copy2(override_file, backup_file)
            print(f"  Saved to .voipbin-versions/{timestamp}.yml")

        # Generate new override file
        print(f"\n{blue('==>')} Generating docker-compose.override.yml...")
        override_content = self._generate_override_content(resolved, warnings, image_to_services)
        with open(override_file, "w") as f:
            f.write(override_content)
        print(f"  {green('✓')} Override file generated")

        # Save new version to history
        timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        history_file = os.path.join(versions_dir, f"{timestamp}.yml")
        shutil.copy2(override_file, history_file)
        print(f"  {green('✓')} Saved to rollback history")

        # Cleanup old backups (keep last 100)
        backups = sorted(Path(versions_dir).glob("*.yml"))
        if len(backups) > 100:
            for old_backup in backups[:-100]:
                old_backup.unlink()

        # Pull images and run migrations in parallel
        print(f"\n{blue('==>')} Pulling images and checking migrations (parallel)...")

        def pull_images():
            """Pull pinned images"""
            result = run_cmd("docker compose pull 2>&1")
            return result

        def run_migrations():
            """Run database migrations via the containerized migrate.sh
            (no host alembic needed; non-interactive by design)."""
            # migrate.sh resolves the dbscheme pin from versions.lock. On a
            # genuinely unpinned repo there is no lock to migrate against -
            # skip with guidance instead of guaranteed failure.
            if not os.path.exists(os.path.join(project_dir, "versions.lock")):
                return "skip", "No versions.lock (unpinned repo) - run scripts/migrate.sh manually if needed"
            db_container = self.config.get("db_container", "voipbin-db")
            db_check = run_cmd(
                f"docker exec {shlex.quote(db_container)} sh -c "
                "'exec mysql -u root -p\"${MYSQL_ROOT_PASSWORD:-root_password}\" -e \"SELECT 1\"' 2>/dev/null"
            )
            if not db_check:
                return "skip", "Database not running"

            script_path = os.path.join(project_dir, "scripts", "migrate.sh")
            if os.path.exists(script_path):
                # Surface migration output and check the exit code (the old
                # `os.system(... > /dev/null 2>&1)` swallow hid real failures).
                mig = subprocess.run(
                    ["bash", script_path], capture_output=True, text=True, cwd=project_dir
                )
                if mig.returncode == 0:
                    return "done", None
                tail = "\n".join((mig.stdout + "\n" + mig.stderr).strip().splitlines()[-15:])
                return "fail", f"migrate.sh exited {mig.returncode}:\n{tail}"

            return "skip", "Migration script not found"

        # Run both tasks in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            pull_future = executor.submit(pull_images)
            migration_future = executor.submit(run_migrations)

            # Wait for both to complete
            pull_result = pull_future.result()
            migration_status, migration_msg = migration_future.result()

        # Show results
        if pull_result:
            lines = pull_result.strip().split('\n')
            for line in lines[-5:]:
                print(f"  {line}")
        print(f"  {green('✓')} Images pulled")

        if migration_status == "done":
            print(f"  {green('✓')} Database migrations complete")
        elif migration_status == "fail":
            print(f"  {red('✗')} Database migrations FAILED: {migration_msg}")
            print(f"    Fix the problem and re-run migrations before relying on the new images.")
        elif migration_status == "skip":
            print(f"  {yellow('!')} Migrations skipped: {migration_msg}")

        # Restart services
        running_services = run_cmd("docker compose ps -q 2>/dev/null")
        if running_services:
            print(f"\n{blue('==>')} Restarting services with new images...")
            run_cmd("docker compose up -d 2>&1")
            print(green("✓ Services restarted"))

        print(f"\n{bold('Image update complete!')}")
        print(f"  Run '{bold('voipbin version')}' to see pinned versions")
        print(f"  Run '{bold('voipbin rollback')}' to restore previous version")

    def _generate_override_content(self, resolved, warnings, image_to_services):
        """Generate docker-compose.override.yml content

        Args:
            resolved: list of resolved image dicts with 'image' and 'tag' keys
            warnings: list of failed image dicts with 'image' and 'error' keys
            image_to_services: dict mapping image name to list of service names
        """
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "# Auto-generated by voipbin CLI",
            f"# Generated: {timestamp}",
            "# Run 'voipbin update' to refresh",
            "",
            "services:",
        ]

        # Build a sorted list of (service_name, image, tag) tuples
        service_entries = []
        for r in resolved:
            image_name = r["image"]
            tag = r["tag"]
            # Get actual service names from docker-compose.yml mapping
            service_names = image_to_services.get(image_name, [])
            for service_name in service_names:
                service_entries.append((service_name, image_name, tag))

        # Sort by service name and add to output
        for service_name, image_name, tag in sorted(service_entries):
            lines.append(f"  {service_name}:")
            lines.append(f"    image: {image_name}:{tag}")

        # Add comments for warnings (images keeping :latest)
        if warnings:
            lines.append("")
            lines.append("  # Images keeping :latest (no commit-SHA tag found):")
            for w in warnings:
                lines.append(f"  # - {w['image']}: {w['error']}")

        lines.append("")
        return "\n".join(lines)

    def _update_scripts(self, project_dir, check_only=False):
        """Update scripts and configs from GitHub.

        Returns True on success (including already-up-to-date), False on failure
        so upgrade callers can abort."""
        print(f"\n{bold('Script Update from GitHub')}")
        print("=" * 50)

        os.chdir(project_dir)

        if not os.path.exists(".git"):
            print(red("Error: Not a git repository."))
            print("This command requires the sandbox to be cloned from GitHub.")
            return False

        print(f"\n{blue('==>')} Fetching from remote...")
        fetch_result = run_cmd("git fetch origin 2>&1")
        if "error" in fetch_result.lower() or "fatal" in fetch_result.lower():
            print(red(f"  Error fetching: {fetch_result}"))
            return False
        print(green("  ✓ Fetched latest"))

        current_branch = run_cmd("git rev-parse --abbrev-ref HEAD")
        print(f"  Current branch: {current_branch}")

        status = run_cmd("git status --porcelain")
        local_changes = [line for line in status.split('\n') if line.strip()] if status else []

        significant_changes = []
        for change in local_changes:
            file_path = change[3:] if len(change) > 3 else change
            is_protected = any(file_path.startswith(p.rstrip('/')) for p in PROTECTED_PATHS)
            if not is_protected:
                significant_changes.append(change)

        if significant_changes:
            print(f"\n{yellow('Local changes detected:')}")
            for change in significant_changes[:10]:
                print(f"  {change}")
            if len(significant_changes) > 10:
                print(f"  ... and {len(significant_changes) - 10} more")

        print(f"\n{blue('==>')} Checking for updates...")
        diff_stat = run_cmd(f"git diff HEAD..origin/{current_branch} --stat 2>/dev/null")

        if not diff_stat:
            print(green("  Already up to date!"))
            return True

        print(f"\n{yellow('Changes available:')}")
        for line in diff_stat.split('\n')[-20:]:
            print(f"  {line}")

        print(f"\n{blue('==>')} Changes in tracked files:")
        for tracked in TRACKED_PATHS:
            diff = run_cmd(f"git diff HEAD..origin/{current_branch} --stat -- {tracked} 2>/dev/null")
            if diff:
                print(f"  {tracked}:")
                for line in diff.split('\n')[:5]:
                    if line.strip():
                        print(f"    {line}")

        if check_only:
            print(f"\n{yellow('Dry-run mode:')} No changes made.")
            print("Run 'update scripts' without --check to apply updates.")
            return True

        backup_path = self._create_backup(project_dir, significant_changes)
        if backup_path:
            print(f"\n{green('✓')} Backup created: {backup_path}")

        stashed = False
        if significant_changes:
            print(f"\n{blue('==>')} Stashing local changes...")
            stash_result = run_cmd("git stash push -m 'voipbin-cli auto-stash before update'")
            if "No local changes" not in stash_result:
                stashed = True
                print(green("  ✓ Changes stashed"))

        print(f"\n{blue('==>')} Pulling updates...")
        pull_result = run_cmd(f"git pull origin {current_branch} 2>&1")
        if "error" in pull_result.lower() or "fatal" in pull_result.lower():
            print(red(f"  Error: {pull_result}"))
            if stashed:
                print(f"\n{blue('==>')} Restoring stashed changes...")
                run_cmd("git stash pop")
            return False

        print(green("  ✓ Updated successfully"))

        if stashed:
            print(f"\n{blue('==>')} Restoring local changes...")
            pop_result = run_cmd("git stash pop 2>&1")
            if "CONFLICT" in pop_result:
                print(yellow("  ! Merge conflicts detected. Please resolve manually."))
                print(f"  Run 'git status' to see conflicts.")
            else:
                print(green("  ✓ Local changes restored"))

        self._cleanup_old_backups(project_dir)

        print(f"\n{bold('Script update complete!')}")
        print("Run 'status' to check service status.")
        return True

    def _create_backup(self, project_dir, changed_files):
        """Create a backup of modified files before updating"""
        if not changed_files:
            return None

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        backup_path = os.path.join(project_dir, BACKUP_DIR, timestamp)

        try:
            os.makedirs(backup_path, exist_ok=True)

            manifest = {
                "timestamp": timestamp,
                "reason": "update scripts",
                "files": []
            }

            for change in changed_files:
                if len(change) < 4:
                    continue
                status = change[:2]
                file_path = change[3:]

                src_path = os.path.join(project_dir, file_path)
                if not os.path.exists(src_path):
                    continue

                dst_path = os.path.join(backup_path, file_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)

                if os.path.isfile(src_path):
                    shutil.copy2(src_path, dst_path)
                    manifest["files"].append({
                        "path": file_path,
                        "status": status.strip(),
                    })

            manifest_path = os.path.join(backup_path, "manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            return backup_path

        except Exception as e:
            print(yellow(f"  Warning: Could not create backup: {e}"))
            return None

    def _cleanup_old_backups(self, project_dir):
        """Keep only the last MAX_BACKUPS backups"""
        backup_base = os.path.join(project_dir, BACKUP_DIR)
        if not os.path.exists(backup_base):
            return

        backups = sorted([
            d for d in os.listdir(backup_base)
            if os.path.isdir(os.path.join(backup_base, d))
        ], reverse=True)

        for old_backup in backups[MAX_BACKUPS:]:
            old_path = os.path.join(backup_base, old_backup)
            try:
                shutil.rmtree(old_path)
                print(f"  Removed old backup: {old_backup}")
            except Exception as e:
                print(yellow(f"  Warning: Could not remove {old_backup}: {e}"))

    def cmd_rollback(self, args):
        """Rollback to a previous image version or script backup"""
        project_dir = self.config.get("project_dir", ".")

        # PINNED-REPO GUARD (design §3.5): rollback replays
        # docker-compose.override.yml snapshots, which are ONLY written on the
        # UNPINNED update path. On a pinned repo there is either nothing to
        # replay, or a stale override that would silently SHADOW the pinned
        # digests in docker-compose.yml - unpinning the repo without telling
        # you. Refuse and point at the pinned-safe alternatives.
        if os.path.exists(os.path.join(project_dir, "versions.lock")):
            print(f"\n{red('rollback is disabled on a pinned repo')} (versions.lock found).")
            print("  'rollback' replays docker-compose.override.yml snapshots, which are only")
            print("  written by the UNPINNED update path. Restoring one here would shadow the")
            print("  pinned digests in docker-compose.yml and silently unpin this repo.")
            print("\n  What you probably want instead:")
            print(f"    - Data restore:   {bold('voipbin restore <ts> --force')}  (from backups/)")
            print("    - Image rollback: git checkout the previous repo commit")
            print("      (docker-compose.yml + versions.lock + scripts are git-versioned),")
            print("      then 'docker compose up -d'.")
            return

        # Check if this is a script rollback
        if args and args[0] == "scripts":
            self._rollback_scripts(project_dir, args[1:])
            return

        # Image version rollback
        self._rollback_versions(project_dir, args)

    def _rollback_versions(self, project_dir, args):
        """Rollback to a previous image version"""
        versions_dir = os.path.join(project_dir, ".voipbin-versions")
        override_file = os.path.join(project_dir, "docker-compose.override.yml")

        # Build version list - include current override and history
        version_list = []

        # Add current override file if it exists
        if os.path.exists(override_file):
            mtime = os.path.getmtime(override_file)
            version_list.append({
                "path": override_file,
                "timestamp": datetime.fromtimestamp(mtime),
                "label": "(current)",
                "is_current": True
            })

        # Add history files if they exist
        versions = []
        if os.path.exists(versions_dir):
            versions = sorted(Path(versions_dir).glob("*.yml"), reverse=True)

        # Check if we have anything to show
        if not version_list and not versions:
            print(f"\n{yellow('No version history found.')}")
            print("Version history is created when running 'voipbin start' or 'voipbin update'.")
            return

        # Get current override timestamp for deduplication
        current_mtime = None
        if version_list:
            current_mtime = version_list[0]["timestamp"]

        for v in versions:
            # Parse timestamp from filename (YYYY-MM-DDTHH-MM-SS.yml)
            try:
                parts = v.stem.split("T")
                date_part = parts[0]
                time_part = parts[1].replace("-", ":")
                ts = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
            except Exception:
                ts = datetime.fromtimestamp(v.stat().st_mtime)

            # Skip if this is essentially the same as current (within 2 seconds)
            if current_mtime and abs((ts - current_mtime).total_seconds()) < 2:
                continue

            version_list.append({
                "path": str(v),
                "timestamp": ts,
                "label": "",
                "is_current": False
            })

        if "--list" in args:
            # Show numbered list
            print(f"\n{bold('Available Versions')}")
            print("=" * 50)
            for i, v in enumerate(version_list):
                ts_str = v["timestamp"].strftime("%Y-%m-%d %H:%M")
                label = v["label"]
                print(f"  [{i + 1}] {ts_str}  {label}")
            print(f"\n  Run 'rollback N' to restore (e.g., 'rollback 2')")
            return

        # Check for number argument
        remaining_args = [a for a in args if a != "--list"]
        if remaining_args:
            try:
                idx = int(remaining_args[0]) - 1
                if 0 <= idx < len(version_list):
                    selected = version_list[idx]
                    if selected["is_current"]:
                        print(yellow("That's the current version. Nothing to restore."))
                        return
                    self._restore_version(project_dir, selected, override_file)
                    return
                else:
                    print(red(f"Invalid version number. Choose 1-{len(version_list)}"))
                    return
            except ValueError:
                print(red(f"Invalid argument: {remaining_args[0]}"))
                print("Use a number (e.g., 'rollback 2') or '--list' to see options.")
                return

        # Interactive selection
        self._interactive_version_select(project_dir, version_list, override_file)

    def _interactive_version_select(self, project_dir, version_list, override_file):
        """Interactive arrow-key selection for version rollback"""
        import termios
        import tty

        if not version_list:
            print(yellow("No versions available."))
            return

        selected_idx = 0
        max_idx = len(version_list) - 1

        def get_key():
            """Read a single keypress"""
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == '\x1b':  # Escape sequence
                    ch += sys.stdin.read(2)
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        def render():
            """Render the selection menu"""
            # Calculate total lines: header(1) + items + hint(1) + blank lines(2)
            total_lines = len(version_list) + 4

            # Clear previous output (move up and clear)
            if hasattr(render, 'rendered'):
                sys.stdout.write(f"\033[{total_lines}A")  # Move up
                sys.stdout.write("\033[J")  # Clear to end
                sys.stdout.flush()

            print(f"\n{bold('Select version to restore:')}")
            for i, v in enumerate(version_list):
                ts_str = v["timestamp"].strftime("%Y-%m-%d %H:%M")
                label = v["label"]
                if i == selected_idx:
                    print(f"  {green('>')} {ts_str}  {label}")
                else:
                    print(f"    {ts_str}  {label}")
            print(f"\n  [{dim('↑/↓ move, Enter select, q cancel')}]")
            sys.stdout.flush()
            render.rendered = True

        print(f"\033[?25l", end="")  # Hide cursor
        try:
            render()
            while True:
                key = get_key()
                if key == '\x1b[A':  # Up arrow
                    selected_idx = max(0, selected_idx - 1)
                    render()
                elif key == '\x1b[B':  # Down arrow
                    selected_idx = min(max_idx, selected_idx + 1)
                    render()
                elif key in ('\r', '\n'):  # Enter
                    print(f"\033[?25h", end="")  # Show cursor
                    selected = version_list[selected_idx]
                    if selected["is_current"]:
                        print(yellow("\nThat's the current version. Nothing to restore."))
                        return
                    self._restore_version(project_dir, selected, override_file)
                    return
                elif key in ('q', 'Q', '\x1b'):  # q or Escape
                    print(f"\033[?25h", end="")  # Show cursor
                    print("\nCancelled.")
                    return
        except Exception as e:
            print(f"\033[?25h", end="")  # Show cursor
            # Fallback to numbered selection if interactive fails
            print(f"\n{yellow('Interactive mode not available. Using numbered selection:')}")
            for i, v in enumerate(version_list):
                ts_str = v["timestamp"].strftime("%Y-%m-%d %H:%M")
                label = v["label"]
                print(f"  [{i + 1}] {ts_str}  {label}")
            try:
                choice = input("\nEnter number to restore (or 'q' to cancel): ").strip()
                if choice.lower() == 'q':
                    print("Cancelled.")
                    return
                idx = int(choice) - 1
                if 0 <= idx < len(version_list):
                    selected = version_list[idx]
                    if selected["is_current"]:
                        print(yellow("That's the current version. Nothing to restore."))
                        return
                    self._restore_version(project_dir, selected, override_file)
                else:
                    print(red("Invalid selection."))
            except ValueError:
                print(red("Invalid input."))

    def _restore_version(self, project_dir, version, override_file):
        """Restore a specific version"""
        print(f"\n{blue('==>')} Restoring version from {version['timestamp'].strftime('%Y-%m-%d %H:%M')}...")

        # Copy the version file to override
        shutil.copy2(version["path"], override_file)
        print(f"  {green('✓')} Restored docker-compose.override.yml")

        # Ask to restart services
        print(f"\n{yellow('Services need to be restarted to use the restored version.')}")
        confirm = input("Restart services now? [Y/n]: ").strip().lower()
        if confirm != 'n':
            print(f"\n{blue('==>')} Pulling images...")
            run_cmd("docker compose pull 2>&1")
            print(f"{blue('==>')} Restarting services...")
            run_cmd("docker compose up -d 2>&1")
            print(green("✓ Services restarted"))

        print(f"\n{bold('Rollback complete!')}")

    def _rollback_scripts(self, project_dir, args):
        """Rollback scripts to a previous backup (legacy)"""
        backup_base = os.path.join(project_dir, BACKUP_DIR)

        if "--list" in args or "list" in args:
            self._list_backups(backup_base)
            return

        if not os.path.exists(backup_base):
            print(red("No script backups found."))
            print("Backups are created automatically when running 'update scripts'.")
            return

        backups = sorted([
            d for d in os.listdir(backup_base)
            if os.path.isdir(os.path.join(backup_base, d))
        ], reverse=True)

        if not backups:
            print(red("No script backups found."))
            return

        target_backup = None
        remaining_args = [a for a in args if a not in ("--list", "list")]

        if remaining_args:
            target = remaining_args[0]
            if target in backups:
                target_backup = target
            else:
                matches = [b for b in backups if b.startswith(target)]
                if len(matches) == 1:
                    target_backup = matches[0]
                elif len(matches) > 1:
                    print(f"{yellow('Multiple matches:')} {', '.join(matches)}")
                    print("Please be more specific.")
                    return
                else:
                    print(f"{red('Backup not found:')} {target}")
                    self._list_backups(backup_base)
                    return
        else:
            target_backup = backups[0]

        backup_path = os.path.join(backup_base, target_backup)
        manifest_path = os.path.join(backup_path, "manifest.json")

        if not os.path.exists(manifest_path):
            print(red(f"Invalid backup (no manifest): {target_backup}"))
            return

        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        print(f"\n{bold('Rollback to:')} {target_backup}")
        print("=" * 50)
        print(f"  Reason: {manifest.get('reason', 'unknown')}")
        print(f"  Files: {len(manifest.get('files', []))}")

        if not manifest.get("files"):
            print(yellow("\nNo files to restore."))
            return

        print(f"\n{yellow('Files to restore:')}")
        for file_info in manifest["files"]:
            print(f"  {file_info['path']}")

        print(f"\n{yellow('This will overwrite current files.')}")
        confirm = input("Continue? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

        print(f"\n{blue('==>')} Restoring files...")
        restored = 0
        for file_info in manifest["files"]:
            src_path = os.path.join(backup_path, file_info["path"])
            dst_path = os.path.join(project_dir, file_info["path"])

            if not os.path.exists(src_path):
                print(yellow(f"  ! Missing: {file_info['path']}"))
                continue

            try:
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)
                print(f"  {green('✓')} {file_info['path']}")
                restored += 1
            except Exception as e:
                print(f"  {red('✗')} {file_info['path']}: {e}")

        print(f"\n{bold('Rollback complete!')} Restored {restored} file(s).")

    def _list_backups(self, backup_base):
        """List available backups"""
        print(f"\n{bold('Available Backups')}")
        print("=" * 50)

        if not os.path.exists(backup_base):
            print("  No backups found.")
            return

        backups = sorted([
            d for d in os.listdir(backup_base)
            if os.path.isdir(os.path.join(backup_base, d))
        ], reverse=True)

        if not backups:
            print("  No backups found.")
            return

        for i, backup in enumerate(backups):
            manifest_path = os.path.join(backup_base, backup, "manifest.json")
            label = " (latest)" if i == 0 else ""

            if os.path.exists(manifest_path):
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
                file_count = len(manifest.get("files", []))
                reason = manifest.get("reason", "")
                print(f"  {backup}{label} - {file_count} file(s) - {reason}")
            else:
                print(f"  {backup}{label} - (no manifest)")

        print(f"\nUsage: rollback [timestamp]")
        print("  rollback             Restore from latest backup")
        print("  rollback 2026-01-27  Restore from specific backup")

    def cmd_exit(self, args):
        """Exit CLI"""
        self.running = False

    def cmd_clear(self, args):
        """Clear screen"""
        os.system("clear" if os.name != "nt" else "cls")


# =============================================================================
# Tab Completion
# =============================================================================

class Completer:
    def __init__(self, cli):
        self.cli = cli
        self.matches = []

    def complete(self, text, state):
        if state == 0:
            line = readline.get_line_buffer()
            self.matches = self.get_matches(line, text)

        try:
            return self.matches[state]
        except IndexError:
            return None

    def get_matches(self, line, text):
        """Get completion matches"""
        parts = line.split()

        # In context mode
        if self.cli.context:
            return self.context_matches(text)

        # Empty line or first word - complete commands
        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            commands = list(self.cli.commands.keys())
            return [c + " " for c in commands if c.startswith(text)]

        # Second word - context-specific completion
        cmd = parts[0].lower()

        if cmd in ("start", "stop", "restart", "logs"):
            # Complete service names
            services = get_all_services()
            if cmd == "logs" and text == "-":
                return ["-f "]
            return [s + " " for s in services if s.startswith(text)]

        if cmd in ("ext", "extension"):
            subcmds = ["list", "create", "delete"]
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "customer":
            subcmds = ["info", "create"]
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "network":
            subcmds = ["status", "setup", "teardown"]
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "dns":
            subcmds = ["status", "list", "setup", "regenerate", "test"]
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "certs":
            subcmds = ["status", "trust"]
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "config":
            if len(parts) == 1 or (len(parts) == 2 and not line.endswith(" ")):
                keys = list(self.cli.config.data.keys()) + ["reset"]
                return [k + " " for k in keys if k.startswith(text)]

        if cmd in ("ast", "asterisk"):
            # Common Asterisk commands
            ast_cmds = [
                "pjsip show endpoints", "pjsip show contacts", "pjsip show aors",
                "core show channels", "core show calls", "core show version",
                "sip show peers", "sip show registry",
                "dialplan show", "module show",
            ]
            return [c for c in ast_cmds if c.startswith(text)]

        if cmd in ("kam", "kamailio"):
            # Common kamcmd commands
            kam_cmds = [
                "ul.dump", "ul.lookup", "stats.get_statistics",
                "tm.stats", "sl.stats", "core.version",
            ]
            return [c for c in kam_cmds if c.startswith(text)]

        if cmd == "update":
            subcmds = ["scripts", "all", "--check"]
            if len(parts) >= 2 and parts[1] in ("scripts", "all"):
                return ["--check "] if "--check".startswith(text) else []
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "rollback":
            subcmds = ["--list"]
            return [s + " " for s in subcmds if s.startswith(text)]

        if cmd == "clean":
            subcmds = ["--containers", "--volumes", "--images", "--network", "--dns", "--purge", "--all"]
            return [s + " " for s in subcmds if s.startswith(text)]

        return []

    def context_matches(self, text):
        """Get matches for context mode"""
        if self.cli.context == "asterisk":
            cmds = [
                "pjsip show endpoints", "pjsip show contacts",
                "core show channels", "core show calls",
                "exit", "quit"
            ]
            return [c for c in cmds if c.startswith(text)]

        if self.cli.context == "api":
            cmds = ["login", "get", "post", "put", "delete", "exit", "quit"]
            return [c + " " for c in cmds if c.startswith(text)]

        return ["exit ", "quit "]


# =============================================================================
# Main Loop
# =============================================================================

def setup_readline(cli):
    """Setup readline with history and completion"""
    # History
    if HISTORY_FILE.exists():
        try:
            readline.read_history_file(HISTORY_FILE)
        except (IOError, OSError):
            pass

    readline.set_history_length(cli.config.get("history_size", 1000))
    atexit.register(lambda: readline.write_history_file(HISTORY_FILE))

    # Tab completion
    completer = Completer(cli)
    readline.set_completer(completer.complete)
    readline.parse_and_bind("tab: complete")

    # macOS compatibility
    if "libedit" in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")


def show_cli_usage():
    """Show usage for command-line mode"""
    print(f"""
{bold('VoIPBin Sandbox CLI')}

Usage:
  sudo ./voipbin                # Start interactive mode
  sudo ./voipbin <command>      # Run command and exit

Commands:
  status              Show service status
  doctor              Diagnose install/runtime state and print fixes (read-only)
  start [service]     Start all services (or specific service)
  stop [--all]        Stop app services (--all for everything)
  restart [service]   Restart services
  logs <service>      View logs
  init                Initialize sandbox (generates .env and certs)
  update [options]    Update sandbox (pinned repos: 'update all' = backup ->
                      git pull -> migrate -> recreate -> verify)
  backup              Full data backup (MySQL + recordings + config)
  restore <ts> --force  Restore data from a backup (DESTRUCTIVE)
  rollback            Image rollback from override history (unpinned repos only)
  dns [subcommand]    DNS configuration (status, list, setup, regenerate, test)
  certs [subcommand]  Certificate management (status, trust)
  network [subcommand] VoIP network management (status, setup, teardown)
  clean [options]     Cleanup sandbox
    --volumes         Remove docker volumes
    --network         Teardown VoIP network interfaces
    --dns             Remove DNS configuration
    --purge           Remove generated files (.env, certs, configs)
    --all             Full reset (all of the above)

Examples:
  sudo ./voipbin status
  sudo ./voipbin start
  sudo ./voipbin network setup --external-ip 192.168.45.160
  sudo ./voipbin clean --all
  sudo ./voipbin logs api-manager

Note: This CLI requires sudo for network and DNS operations.
""")


def main():
    # Require root/sudo
    check_root()

    cli = VoIPBinCLI()

    # Change to project directory
    project_dir = cli.config.get("project_dir")
    if project_dir and os.path.isdir(project_dir):
        os.chdir(project_dir)

    # Check for command-line arguments (non-interactive mode)
    if len(sys.argv) > 1:
        # Handle help
        if sys.argv[1] in ("-h", "--help", "help"):
            show_cli_usage()
            return

        # Parse command and args
        cmd = sys.argv[1].lower()
        args = sys.argv[2:]

        if cmd in cli.commands:
            cli.commands[cmd](args)
            # doctor's 0/1 exit code is part of the machine contract for
            # agents scripting `sudo ./voipbin doctor` (design §7). Only this
            # non-interactive path exits with it; the REPL path returns to
            # the prompt instead.
            if cmd == "doctor" and cli.last_doctor_rc:
                sys.exit(cli.last_doctor_rc)
        else:
            print(red(f"Unknown command: {cmd}"))
            print("Run 'sudo ./voipbin --help' for usage.")
            sys.exit(1)
        return

    # Interactive mode
    setup_readline(cli)

    # Show welcome dashboard
    show_welcome_dashboard()

    # Handle Ctrl+C gracefully
    def sigint_handler(sig, frame):
        print()
        if cli.context:
            cli.context = None
        else:
            print("Type 'exit' to quit")

    signal.signal(signal.SIGINT, sigint_handler)

    while cli.running:
        try:
            line = input(cli.get_prompt())

            if cli.context:
                cli.run_in_context(line)
            else:
                cmd, args = cli.parse_input(line)
                if cmd:
                    if cmd in cli.commands:
                        cli.commands[cmd](args)
                    else:
                        print(f"Unknown command: {cmd}. Type 'help' for available commands.")

        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue
        except Exception as e:
            print(red(f"Error: {e}"))

    print("Goodbye!")


if __name__ == "__main__":
    main()
