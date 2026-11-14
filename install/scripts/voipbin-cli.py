#!/usr/bin/env python3
"""
VoIPBin Install Interactive CLI

An interactive command-line interface for installing and managing VoIPBin,
inspired by Asterisk CLI and Claude Code.

This CLI requires sudo for full functionality (network setup, DNS config, etc.)
"""

import atexit
import errno
import getpass
import hashlib
import json
import os
import pwd
import readline
import re
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


def check_root():
    """Check if running as root, exit if not"""
    if os.geteuid() != 0:
        print(f"{red('[ERROR]')} This CLI requires sudo for full functionality.")
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
    "backups/",
    "docker-compose.yml.tmp",
    ".env.pre-merge",
    "docker-compose.yml.pre-merge",
]

# Exit codes of `update` (non-interactive). 0 = success (including --check).
RC_UNEXPECTED = 1
RC_PRECONDITION = 2
RC_BACKUP_FAILED = 10
RC_SCRIPTS_FAILED = 11
RC_UNPINNED = 12
RC_SYNC_FAILED = 13
RC_PULL_FAILED = 14
RC_MIGRATE_FAILED = 15
RC_UP_FAILED = 16
RC_VERIFY_CONTAINERS = 17
RC_VERIFY_PING = 18
RC_VERIFY_BOTH = 19
RC_KEYS_FAILED = 20
RC_USAGE = 64
RC_INTERRUPTED = 130

# Files/directories that are tracked for script updates
# NOTE: docker-compose.yml/versions.lock (no suffix) are the untracked,
# locally-generated live copies (see docker-compose.yml.dist's/
# versions.lock.dist's header comment/"_comment" field) - gitignored and
# would never show a diff anyway. The .dist files are the committed copies
# that actually change upstream.
TRACKED_PATHS = [
    "scripts/",
    "docker-compose.yml.dist",
    "versions.lock.dist",
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


def color_wanted(stream=None):
    """False when NO_COLOR is set (any non-empty value) or stdout is not a
    terminal, so piped and logged output carries no escape codes."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


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
    ("  ██    ██   ██   ██    ██  ", ""),
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


def _logo_row(icon_part, text_part):
    """One colored logo line: white icon, bold wordmark, gray tagline."""
    if "Connect & Collaborate" in text_part:
        return f"{white(icon_part)}{gray(text_part)}"
    if text_part:
        return f"{white(icon_part)}{bold(text_part)}"
    return f"{white(icon_part)}"


def get_logo():
    """Return the combined ASCII logo with colors"""
    return '\n'.join(_logo_row(icon_part, text_part) for icon_part, text_part in LOGO_LINES)


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

    # Check network status. VOIP-1331: also reject a still-legacy macvlan
    # interface, not just presence+IP - otherwise a host that hasn't
    # migrated yet renders a green "configured" indicator here even though
    # `voipbin network doctor`/doctor.sh correctly flags it as broken. Also
    # reject an orphaned veth peer (bridge-side "-br" end with no `master`)
    # - unlike macvlan, a veth pair survives `docker compose down`/
    # `voipbin> clean` with its bridge enslavement cleared, so presence +
    # correct IP alone isn't sufficient once the bridge has been recreated.
    kamailio_int = run_cmd("ip addr show kamailio-int 2>/dev/null | grep -oP 'inet [\\d./]+' | head -1")
    kamailio_int_is_macvlan = "macvlan" in run_cmd("ip -d link show kamailio-int 2>/dev/null")
    kamailio_peer_enslaved = "master" in run_cmd("ip link show kamailio-br 2>/dev/null")
    network_configured = bool(kamailio_int) and not kamailio_int_is_macvlan and kamailio_peer_enslaved

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
{yellow("     /  \\")}       No installation found in this directory.
{yellow("    / !! \\")}
{yellow("   /______\\")}        {bold("install")}       Guided install (checks, configuration, host setup,
                                 services, admin account). Nothing to edit first.

                AI provider keys are optional: add them to .env later, then 'update keys'."""
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
# Interactive TUI (start screen, input frame, status bar, slash commands)
# =============================================================================
#
# Used only by the no-argument REPL, and only when stdin and stdout are both
# terminals, NO_COLOR is unset, TERM is not "dumb" and stdout is UTF-8. In every
# other case the REPL keeps its plain prompt and welcome dashboard. The pure
# render/parse functions below take plain data so tests can call them directly.

TUI_MIN_HEIGHT = 8
# The header is pinned to the top of the terminal (a scroll region below it
# holds the command output and the input frame), so it needs the room for
# itself, the 5-line input frame and a few lines of output. It also needs a
# terminal at least TUI_FULL_MIN_WIDTH wide; narrower or shorter terminals
# keep the header printed once at start, as before. The icon is the only
# place that decides the header height: TUI_ICON rows with the info lines
# beside them (the logo header) or the three info lines alone (compact).
TUI_ICON = (
    " ▗▄▄▄████▄▄▄▖ ",
    "▗█▄▄▄▄▄▄▄▄▄▄█▖",
    "▝▙  ▄ ▗▖ ▄  ▟▘",
    " █  █ ▐▌ █  █ ",
    " ▜▖ █ ▐▌ █ ▗▛ ",
    " ▝▀▀▀▀▀▀▀▀▀▀▘ ",
)
# Every row is padded to the same width, so the info lines beside it line up.
TUI_ICON_WIDTH = max(len(row) for row in TUI_ICON)
TUI_ICON_GAP = 6
TUI_TAGLINE = "Connect & Collaborate for all"
TUI_FULL_MIN_WIDTH = 60
TUI_FRAME_ROWS = 5
TUI_MIN_OUTPUT_ROWS = 5
TUI_SLASH_HELP = (
    ("help", "Show commands"),
    ("install", "Guided install"),
    ("status", "Show service status"),
    ("start", "Start services"),
    ("stop", "Stop app services"),
    ("restart", "Restart services"),
    ("logs", "View logs (/logs <service>)"),
    ("doctor", "Diagnose (read-only)"),
    ("update", "Update the install"),
    ("version", "Show pinned image versions"),
    ("clear", "Clear the screen"),
    ("exit", "Quit"),
)
TUI_SLASH_COMMANDS = tuple(name for name, _ in TUI_SLASH_HELP)
# Commands after which the status bar data is refreshed once (status/ps show
# the live state; the others change it). doctor is read-only and not listed.
TUI_STATE_CHANGING = frozenset((
    "install", "init", "start", "stop", "restart", "clean", "update",
    "backup", "restore", "rollback", "network", "dns", "certs", "status", "ps",
))
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def tui_wanted(stdin=None, stdout=None, environ=None, lines=None):
    """True only on a UTF-8 terminal with color allowed (see block comment)."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    try:
        if not (stdin.isatty() and stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    encoding = (getattr(stdout, "encoding", None) or "").lower().replace("_", "-")
    if encoding not in ("utf-8", "utf8"):
        return False
    if lines is None:
        lines = shutil.get_terminal_size().lines
    return lines >= TUI_MIN_HEIGHT


def tui_width():
    """Current terminal width, read at every draw so a resize is picked up."""
    return max(20, shutil.get_terminal_size().columns)


def _char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(text):
    """Columns the text occupies once ANSI escape sequences are removed."""
    return sum(_char_width(c) for c in _ANSI_RE.sub("", text))


def clip(text, width, keep="head"):
    """Cut text to at most `width` columns, marking the cut with an ellipsis.

    keep="head" keeps the start (ANSI sequences stay intact and a reset is
    appended); keep="tail" keeps the end of a plain string (long paths).
    """
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    budget = width - 1
    if keep == "tail":
        out, used = [], 0
        for ch in reversed(_ANSI_RE.sub("", text)):
            w = _char_width(ch)
            if used + w > budget:
                break
            out.append(ch)
            used += w
        return "…" + "".join(reversed(out))
    out, used, has_ansi = [], 0, False
    for token in re.split(r"(\x1b\[[0-9;?]*[A-Za-z])", text):
        if _ANSI_RE.fullmatch(token):
            out.append(token)
            has_ansi = True
            continue
        for ch in token:
            w = _char_width(ch)
            if used + w > budget:
                return "".join(out) + "…" + ("\x1b[0m" if has_ansi else "")
            out.append(ch)
            used += w
    return "".join(out)


def read_release_version(path=None):
    """Latest released version from install/CHANGELOG.md ("## [vX.Y.Z]"), or
    None when there is no released section yet or the file is unreadable."""
    path = path or (Path(__file__).resolve().parent.parent / "CHANGELOG.md")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"## \[(v\d+\.\d+\.\d+)\]", line)
                if m:
                    return m.group(1)
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _printable(text, limit=64):
    """Strip control characters from values shown on screen."""
    return "".join(c if c.isprintable() else "?" for c in str(text))[:max(limit, 1)]


def tui_collect_info(cli):
    """Snapshot for the header and status bar. Reuses detect_dashboard_state and
    get_quick_status (no new probes); the only .env value it adds is DOMAIN_MODE,
    and only the state, counts, mode and directory are ever displayed."""
    state = detect_dashboard_state()
    info = {
        "version": read_release_version(),
        "state": state,
        "running": None, "total": None,
        "dns": None, "net": None,      # True | False | None (unknown) | "n/a"
        "mode": None,
        "cwd": "?",
    }
    try:
        info["cwd"] = _printable(os.getcwd(), 4096)
    except OSError:
        pass  # the working directory was removed
    if state != "not_initialized":
        info["mode"] = re.sub(r"[^A-Za-z0-9_.-]", "", cli._domain_mode())[:16] or None
    if state == "running":
        quick = get_quick_status()
        if quick["running_count"] == 0:
            info["state"] = "stopped"
        else:
            info["running"] = quick["running_count"]
            info["total"] = quick["total_count"]
            info["dns"] = "n/a" if info["mode"] == "external" else bool(quick["dns_active"])
            info["net"] = bool(quick["network_configured"])
    return info


def _state_label(info):
    return {"not_initialized": "not installed", "stopped": "stopped",
            "running": "running"}.get(info["state"], info["state"])


def _services_text(info):
    if info["state"] != "running" or info["running"] is None:
        return "-"
    return f"{info['running']}/{info['total']}" if info["total"] else str(info["running"])


def _dot(value):
    if value == "n/a":
        return "n/a"
    if value is None:
        return "-"
    return green("●") if value else gray("○")


def render_header(info, width, height=None):
    """The header lines: the icon with four info lines beside it when the
    terminal is wide and tall enough to pin it (tui_pin_rows), else only the
    three info lines. Returns a list of lines, each at most width-1 columns.
    height=None reads the terminal height."""
    limit = max(1, width - 1)
    if height is None:
        height = shutil.get_terminal_size().lines
    title = "VoIPBin Install CLI" + (f" {info['version']}" if info.get("version") else "")
    status = _state_label(info)
    if info["state"] == "running":
        status += f" {_services_text(info)}"
    if info.get("mode"):
        status += f" · mode {info['mode']}"
    logo = (width >= TUI_FULL_MIN_WIDTH
            and height >= len(TUI_ICON) + 1 + TUI_FRAME_ROWS + TUI_MIN_OUTPUT_ROWS)
    avail = limit - (TUI_ICON_WIDTH + TUI_ICON_GAP if logo else 0)
    right = [
        bold(clip(title, avail)),
        clip(status, avail),
        gray(clip(info["cwd"], avail, keep="tail")),
    ]
    if not logo:
        return right
    right.append(gray(clip(TUI_TAGLINE, avail)))
    gap = " " * TUI_ICON_GAP
    return [white(icon) + (gap + right[i] if i < len(right) else "")
            for i, icon in enumerate(TUI_ICON)]


def render_rule(width):
    return gray("─" * max(1, width - 1))


def render_notice(info, width):
    if info["state"] == "not_initialized":
        text = "No installation found · /install"
    else:
        text = f"Using .env (mode={info.get('mode') or 'internal'}) · /status"
    return clip(text, max(1, width - 1))


def render_statusbar(info, width):
    """Two lines: the state bar and the hint line, narrowed in three steps."""
    limit = max(1, width - 1)
    env = _state_label(info)
    mode = info.get("mode") or "-"
    short_env = f"{env} {_services_text(info)}" if info["state"] == "running" else env
    candidates = [
        f"Env: {env} | Services: {_services_text(info)} | DNS: {_dot(info['dns'])}"
        f" | Net: {_dot(info['net'])} | Mode: {mode}",
        f"{short_env} · DNS {_dot(info['dns'])} Net {_dot(info['net'])} · {mode}",
        f"{short_env} · {mode}",
    ]
    bar = next((c for c in candidates if visible_len(c) <= limit), None)
    if bar is None:
        bar = clip(candidates[-1], limit)
    first = 'Try "/install"' if info["state"] == "not_initialized" else 'Try "/status" or "/logs <service>"'
    hints = [f"{first} · /help for commands · /exit to quit",
             "/help for commands · /exit to quit", "/help · /exit"]
    hint = next((h for h in hints if len(h) <= limit), hints[-1])
    return [bar, dim(clip(hint, limit))]


def render_frame(info, width):
    """Lines of the input frame: top rule, (empty) input line, bottom rule and
    the two status lines."""
    rule = gray("─" * max(1, width - 1))
    return [rule, ""] + [rule] + render_statusbar(info, width)


def render_slash_list():
    rows = ["Slash commands:"]
    rows += [f"  /{name:<9} {desc}" for name, desc in TUI_SLASH_HELP]
    rows.append("Plain commands work too; 'help' lists all.")
    return "\n".join(rows)


def parse_slash(line):
    """Translate a slash command into its plain command line.

    Returns (line, None) for a line that is not a slash command, (translated,
    None) for a known slash command, and (None, message) for "/" alone or an
    unknown slash command.
    """
    text = line.strip()
    if not text.startswith("/"):
        return line, None
    if text == "/":
        return None, render_slash_list()
    m = re.match(r"/(\S+)(?:\s+(.*))?$", text, re.S)
    name = m.group(1).lower() if m else ""
    if name in TUI_SLASH_COMMANDS:
        args = (m.group(2) or "").strip()
        return (f"{name} {args}" if args else name), None
    return None, f"Unknown command: {text.split()[0]}. Type /help for available commands."


def tui_pin_rows(info, width, height):
    """Header lines plus the closing rule when the header can be pinned at
    this size, else None (the header is then printed once, unpinned)."""
    if width < TUI_FULL_MIN_WIDTH:
        return None
    rows = render_header(info, width, height)
    if height < len(rows) + 1 + TUI_FRAME_ROWS + TUI_MIN_OUTPUT_ROWS:
        return None
    return rows + [render_rule(width)]


def tui_pin_sequence(rows, height, mode):
    """Escape sequences for the pinned header. The scroll region is DECSTBM
    (rows below the header); setting or resetting it homes the cursor, so
    every mode puts the cursor back where it belongs.
    init: clear, draw, set the region, cursor on the first region row.
    repaint: draw and set the region, cursor returns where it was.
    clear: erase the region only. release: reset the region, cursor kept."""
    top = len(rows) + 1
    if mode == "release":
        return "\x1b7\x1b[r\x1b8"
    if mode == "clear":
        return f"\x1b[{top};1H\x1b[J"
    draw = "".join(f"\x1b[{i + 1};1H\x1b[2K{row}" for i, row in enumerate(rows))
    region = f"\x1b[{top};{height}r"
    if mode == "repaint":
        return "\x1b7" + draw + region + "\x1b8"
    return "\x1b[r\x1b[2J" + draw + region + f"\x1b[{top};1H"


# State of the pinned header. Nothing here writes to the terminal unless
# tui_pin_start() was called (only the terminal REPL does), so the plain
# prompt, pipes and commands run with arguments never see these sequences.
# enabled: signal handlers installed (start..close); active: the region is set;
# waiting: the REPL is blocked reading a line (the only time the SIGWINCH
# handler may draw); geom: (width, height, header rows) of the last full draw.
_PIN = {"enabled": False, "active": False, "waiting": False, "geom": None,
        "info": None, "saved": {}, "atexit": False}


def _pin_write(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def _pin_state(info):
    width = tui_width()
    height = shutil.get_terminal_size().lines
    rows = tui_pin_rows(info, width, height) if height >= TUI_MIN_HEIGHT else None
    return width, height, rows


def _pin_on_winch(signum, frame):
    """Terminal resized while the prompt waits: redraw the header and the
    region edge in place. The input line, the cursor and the saved geometry
    are left alone; the next prompt loop (tui_pin_refresh) cleans up."""
    if not (_PIN["active"] and _PIN["waiting"]):
        return
    try:
        width, height, rows = _pin_state(_PIN["info"])
        if rows is None or len(rows) != _PIN["geom"][2]:
            return
        _pin_write(tui_pin_sequence(rows, height, "repaint"))
    except Exception:
        pass


def _pin_on_term(signum, frame):
    """SIGTERM/SIGHUP: leave through SystemExit so the REPL's finally releases
    the region. At the prompt, first erase the open input frame (the typed
    line and the status lines below the cursor)."""
    signal.signal(signum, signal.SIG_DFL)
    if _PIN["active"] and _PIN["waiting"]:
        try:
            _pin_write("\r\x1b[J")
        except (OSError, ValueError):
            pass
    sys.exit(128 + signum)


def _pin_install_handlers():
    for name, handler, only_default in (("SIGWINCH", _pin_on_winch, False),
                                        ("SIGTERM", _pin_on_term, True),
                                        ("SIGHUP", _pin_on_term, True)):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
            if only_default and previous != signal.SIG_DFL:
                continue  # keep an ignored signal ignored (nohup)
            signal.signal(sig, handler)
            _PIN["saved"][sig] = previous
        except (ValueError, OSError):
            pass


def tui_pin_start(info):
    """Pin the header and put the cursor under it, or print it once when it
    cannot be pinned. Returns True when pinned."""
    if not _PIN["enabled"]:
        _PIN["enabled"] = True
        _pin_install_handlers()
        if not _PIN["atexit"]:
            _PIN["atexit"] = True
            atexit.register(tui_pin_close)
    _PIN["info"] = info
    width, height, rows = _pin_state(info)
    if rows is not None:
        try:
            # Push what is on the screen into the scrollback first.
            _pin_write("\n" * height + tui_pin_sequence(rows, height, "init"))
        except (OSError, ValueError):
            try:
                _pin_write(tui_pin_sequence([], height, "release"))
            except (OSError, ValueError):
                pass
        else:
            _PIN["active"] = True
            _PIN["geom"] = (width, height, len(rows))
            print(render_notice(info, width))
            return True
    tui_print_intro(info)
    return False


def tui_pin_refresh(info):
    """Before every prompt: follow a resize, pick up a changed status line
    and heal a region that an external tool reset. A new geometry clears the
    region (like /clear); the same geometry only repaints."""
    if not _PIN["enabled"]:
        return
    try:
        _PIN["info"] = info
        width, height, rows = _pin_state(info)
        if rows is None:
            tui_pin_release()
            return
        geom = (width, height, len(rows))
        mode = "repaint" if _PIN["active"] and geom == _PIN["geom"] else "init"
        _pin_write(tui_pin_sequence(rows, height, mode))
        _PIN["active"] = True
        _PIN["geom"] = geom
    except (OSError, ValueError):
        pass


def tui_pin_clear():
    """/clear: erase the region below the pinned header. False when the
    header is not pinned (the caller clears the whole screen)."""
    if not _PIN["active"]:
        return False
    try:
        _pin_write(tui_pin_sequence([""] * _PIN["geom"][2], 0, "clear"))
    except (OSError, ValueError):
        return False
    return True


def tui_pin_release():
    """Reset the scroll region, leaving the header and the cursor in place."""
    if not _PIN["active"]:
        return
    _PIN["active"] = False
    try:
        _pin_write(tui_pin_sequence([], 0, "release"))
    except (OSError, ValueError):
        pass


def tui_pin_close():
    """Release the region and give the signal handlers back. Idempotent; runs
    from the REPL's finally, atexit and before the update re-exec."""
    tui_pin_release()
    for sig, previous in _PIN["saved"].items():
        try:
            signal.signal(sig, previous if previous is not None else signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    _PIN["saved"] = {}
    _PIN["enabled"] = False


def tui_print_intro(info):
    width = tui_width()
    print()
    for row in render_header(info, width):
        print(row)
    print(render_notice(info, width))
    print()


def tui_read_line(cli, info):
    """Draw the input frame, read one line below the top rule, then leave the
    rule, the typed line and a closing rule in the scrollback.

    Rows: top rule 0, input 1, bottom rule 2. Ctrl+C returns "" and EOF
    re-raises EOFError (the caller's loop ends on it) after the frame is tidied.
    """
    out = sys.stdout
    if shutil.get_terminal_size().lines < TUI_MIN_HEIGHT:
        return input(cli.get_prompt())
    frame = render_frame(info, tui_width())
    try:
        out.write("\r" + "\n".join(frame) + "\x1b[3A\r")
        out.flush()
    except (OSError, ValueError):
        return input(cli.get_prompt())
    prompt = f"\001{Colors.BOLD}\002❯\001{Colors.RESET}\002 "
    previous, swapped = None, False
    try:
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        swapped = True
    except (ValueError, OSError):
        pass
    try:
        line = input(prompt)
    except KeyboardInterrupt:
        out.write("\x1b[1A\r\x1b[J")
        out.flush()
        return ""
    except EOFError:
        out.write("\n\r\x1b[J" + frame[2] + "\n")
        out.flush()
        raise
    finally:
        if swapped:
            signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)
    if line.strip():
        out.write("\r\x1b[J" + frame[2] + "\n")
    else:
        out.write("\x1b[2A\r\x1b[J")
    out.flush()
    return line


def tui_after_command(cli, info, cmd):
    """Redraw the header after `clear`, or refresh the snapshot once after a
    command that changes or reports the state. Never raises."""
    if not cli.running:
        return
    try:
        if cmd == "clear":
            if not _PIN["active"]:
                tui_print_intro(info)
        elif cmd in TUI_STATE_CHANGING:
            info.update(tui_collect_info(cli))
    except Exception:
        pass


# Errors a read raises once the terminal is gone; retrying them can only spin.
TERMINAL_GONE_ERRNOS = (errno.EIO, errno.EBADF)
_HANGUP_WATCH = {"started": False}


def _hangup_watch(fd):
    # With a libedit-backed readline (python builds that ship editline instead
    # of GNU readline) input() keeps retrying a hung-up terminal in C and never
    # lets the Python SIGHUP handler run (or SIGHUP be ignored), so wait for the
    # hangup in a thread. poll() with no requested events wakes only on
    # POLLHUP/POLLERR/POLLNVAL, so it costs no CPU while idle. POLLNVAL alone
    # (a platform that cannot poll a terminal) is not a hangup: stop watching.
    poller = select.poll()
    poller.register(fd, 0)
    while True:
        try:
            events = poller.poll()
        except InterruptedError:
            continue
        except (OSError, ValueError):
            return
        if any(mask & (select.POLLHUP | select.POLLERR) for _, mask in events):
            # The main thread is stuck inside readline, so a normal exit cannot
            # run: do what it would (release the op lock, save the history),
            # then leave. (_run_exitfuncs is private API, hence the broad except.)
            try:
                atexit._run_exitfuncs()
            except BaseException:
                pass
            os._exit(128 + signal.SIGHUP)
        if events:
            return


def start_hangup_watch():
    """Exit when the controlling terminal hangs up, wherever the CLI is waiting
    for input. A no-op unless stdin is a terminal, and started once."""
    if _HANGUP_WATCH["started"] or not hasattr(select, "poll"):
        return
    try:
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return
    _HANGUP_WATCH["started"] = True
    threading.Thread(target=_hangup_watch, args=(fd,), daemon=True).start()


def repl_loop(cli, info=None):
    """The interactive command loop. info is the TUI snapshot, or None for the
    plain `voipbin> ` prompt (non-terminal, NO_COLOR, TERM=dumb)."""
    while cli.running:
        if info is not None:
            tui_pin_refresh(info)
        try:
            tui = info is not None and not cli.context
            _PIN["waiting"] = True
            try:
                line = tui_read_line(cli, info) if tui else input(cli.get_prompt())
            except OSError as e:
                if e.errno in TERMINAL_GONE_ERRNOS:
                    raise EOFError  # terminal gone: leave like Ctrl+D
                raise
            finally:
                _PIN["waiting"] = False

            if cli.context:
                cli.run_in_context(line)
            else:
                if tui:
                    line, message = parse_slash(line)
                    if message:
                        print(message)
                        continue
                cmd, args = cli.parse_input(line)
                if cmd:
                    if cmd in cli.commands:
                        try:
                            cli.commands[cmd](args)
                        finally:
                            if info is not None:
                                tui_after_command(cli, info, cmd)
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


# Optional provider keys `status` reports as set/empty (from .env.template's
# OPTIONAL sections). Grouped keys count as set only when all are set.
OPTIONAL_PROVIDER_KEYS = (
    ("OPENAI_API_KEY",), ("GOOGLE_API_KEY",), ("XAI_API_KEY",), ("OPENROUTER_API_KEY",),
    ("ELEVENLABS_API_KEY",), ("CARTESIA_API_KEY",), ("DEEPGRAM_API_KEY",), ("LOCAL_LLM_ENDPOINTS",),
    ("AWS_ACCESS_KEY", "AWS_SECRET_KEY"), ("TWILIO_SID", "TWILIO_API_KEY"),
    ("TELNYX_API_KEY",), ("SENDGRID_API_KEY",), ("MAILGUN_API_KEY",),
)

# Provider secrets of six services are read from the stored configs (database)
# only; the services no longer read the environment variables. These rows tell
# the installer how to copy the value the container used to receive (the compose
# environment name, filled from .env) into the stored config of the service:
# (compose service, control binary under /app/bin, master key variable,
#  compose environment name, .env variable, stored config key, atomic group).
# Both keys of an atomic group must come from the same source, so they are
# stored together or not at all.
PROVIDER_CONFIG_KEYS = (
    ("ai-manager", "ai-control", "AI_CONFIG_ENCRYPTION_KEYS", "ENGINE_KEY_CHATGPT", "OPENAI_API_KEY", "llm.openai_compat.api_key", None),
    ("ai-manager", "ai-control", "AI_CONFIG_ENCRYPTION_KEYS", "GOOGLE_API_KEY", "GOOGLE_API_KEY", "llm.gemini.api_key", None),
    ("email-manager", "email-control", "EMAIL_CONFIG_ENCRYPTION_KEYS", "SENDGRID_API_KEY", "SENDGRID_API_KEY", "sendgrid.api_key", None),
    ("email-manager", "email-control", "EMAIL_CONFIG_ENCRYPTION_KEYS", "MAILGUN_API_KEY", "MAILGUN_API_KEY", "mailgun.api_key", None),
    ("message-manager", "message-control", "MESSAGE_CONFIG_ENCRYPTION_KEYS", "AUTHTOKEN_MESSAGEBIRD", "MESSAGEBIRD_API_KEY", "provider.messagebird.auth_token", None),
    ("message-manager", "message-control", "MESSAGE_CONFIG_ENCRYPTION_KEYS", "AUTHTOKEN_TELNYX", "TELNYX_API_KEY", "provider.telnyx.auth_token", None),
    ("transcribe-manager", "transcribe-control", "TRANSCRIBE_CONFIG_ENCRYPTION_KEYS", "AWS_ACCESS_KEY", "AWS_ACCESS_KEY", "aws.access_key", "aws"),
    ("transcribe-manager", "transcribe-control", "TRANSCRIBE_CONFIG_ENCRYPTION_KEYS", "AWS_SECRET_KEY", "AWS_SECRET_KEY", "aws.secret_key", "aws"),
    ("tts-manager", "tts-control", "TTS_CONFIG_ENCRYPTION_KEYS", "AWS_ACCESS_KEY", "AWS_ACCESS_KEY", "aws.access_key", "aws"),
    ("tts-manager", "tts-control", "TTS_CONFIG_ENCRYPTION_KEYS", "AWS_SECRET_KEY", "AWS_SECRET_KEY", "aws.secret_key", "aws"),
    ("tts-manager", "tts-control", "TTS_CONFIG_ENCRYPTION_KEYS", "ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY", "elevenlabs.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "OPENAI_API_KEY", "OPENAI_API_KEY", "openai.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "XAI_API_KEY", "XAI_API_KEY", "xai.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "GOOGLE_API_KEY", "GOOGLE_API_KEY", "google.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "OPENROUTER_API_KEY", "OPENROUTER_API_KEY", "openrouter.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "DEEPGRAM_API_KEY", "DEEPGRAM_API_KEY", "deepgram.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "CARTESIA_API_KEY", "CARTESIA_API_KEY", "cartesia.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY", "elevenlabs.api_key", None),
    ("pipecat-manager", "pipecat-control", "PIPECAT_CONFIG_ENCRYPTION_KEYS", "LOCAL_LLM_ENDPOINTS", "LOCAL_LLM_ENDPOINTS", "local_llm.endpoints", None),
)
# Recorded as the person who made the change in the history of a stored key.
# Not a secret; any non-zero UUID is accepted by the control binaries.
PROVIDER_ACTOR_ID = "00000000-0000-4000-8000-00000000a001"
PROVIDER_CALL_TIMEOUT = 120      # seconds for one control binary call
PROVIDER_TOTAL_BUDGET = 600      # seconds for all calls of one import
PROVIDER_STATUS_TIMEOUT = 20     # seconds for one call made only to show `status`
PROVIDER_MAX_VALUE_LEN = 512     # longest value the services accept
# Stored keys that accept a longer value (bytes): the local LLM endpoints of
# pipecat-manager are one line of JSON.
PROVIDER_MAX_VALUE_LEN_BY_KEY = {"local_llm.endpoints": 2020}
# Stored config sources that mean "no value stored yet" (an old image can still
# report env or default) and the one that means "stored and readable".
PROVIDER_SOURCES_EMPTY = ("none", "env", "default")
PROVIDER_SOURCE_STORED = "db"

# number-manager keeps its provider credentials in the rows of number_providers
# (an encrypted secret plus non-secret config), written with
# `number-control provider ...`; it has no `config` commands, so it is handled
# next to the table above, not in it. The names are the compose environment names
# (what the container used to receive, filled from .env).
NUMBER_SVC = "number-manager"
NUMBER_CTL = "number-control"
NUMBER_MASTER = "NUMBER_SECRET_ENCRYPTION_KEYS"
NUMBER_TELNYX_SECRET = "TELNYX_TOKEN"
NUMBER_TELNYX_CONFIG = (("connection_id", "TELNYX_CONNECTION_ID"), ("profile_id", "TELNYX_PROFILE_ID"))
NUMBER_TWILIO_VARS = ("TWILIO_SID", "TWILIO_TOKEN")
NUMBER_CONFIG_MAX_LEN = 256      # longest config value the service accepts
# Only Telnyx is stored from .env. Twilio needs a trunk SID that .env cannot carry.
NUMBER_ADVICE = {
    "telnyx": "TELNYX_API_KEY is set but TELNYX_CONNECTION_ID is empty, so number search, release and update "
              "cannot use Telnyx; put the connection id of your Telnyx account in .env and run "
              "'sudo ./voipbin update keys' (ignore this if Telnyx is only used for SMS)",
    "twilio": "TWILIO_SID / TWILIO_API_KEY in .env are not stored: Twilio also needs a trunk SID and the "
              "account auth token (an API key SID starting with SK is not supported); see README.md "
              "\"Provider keys\" to enter it",
}


def provider_max_len(key):
    """Longest value (bytes) the service accepts for a stored config key."""
    return PROVIDER_MAX_VALUE_LEN_BY_KEY.get(key, PROVIDER_MAX_VALUE_LEN)


def provider_mask(text, values):
    """Hide secret values (and anything longer than a short line) in text."""
    text = text or ""
    for v in values:
        if v:
            text = text.replace(v, "***")
    return text.strip()[:200]


def provider_last_line(text, values=()):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return provider_mask(lines[-1], values) if lines else ""


def provider_classify(rc, err, ctl):
    """ok | unsupported (an old image without the command) | failed.

    Only the two messages an old image really produces count as unsupported:
    cobra's unknown command for config/migrate, and the docker error for a
    missing /app/bin/<ctl>. Everything else (database refused, image pull
    failed, a "not found" of some other kind) is a failure."""
    if rc == 0:
        return "ok"
    err = err or ""
    if re.search(r'unknown command "(config|migrate)"', err):
        return "unsupported"
    if ctl == NUMBER_CTL and re.search(r'unknown command "provider"', err):
        return "unsupported"
    if f"/app/bin/{ctl}" in err and re.search(r"no such file or directory|executable file not found", err):
        return "unsupported"
    return "failed"


def provider_parse_list(out):
    """The rows of `config list` ({key: source}, {key: has_secret}) or None.

    The output is a JSON array that starts at a line beginning with "[";
    lines before it (logs) are ignored, and every such line is tried."""
    lines = (out or "").splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode("\n".join(lines[i:]).lstrip())
        except ValueError:
            continue
        if isinstance(data, list) and all(isinstance(r, dict) and "key" in r and "source" in r for r in data):
            return ({r["key"]: r["source"] for r in data}, {r["key"]: bool(r.get("has_secret")) for r in data})
    return None


def provider_compose_services(data):
    """Reduce the parsed `docker compose config --format json` to
    {service: {variable: value}} for the provider rows only.

    `compose config` writes every "$" of a value as "$$"; the container gets
    the single form, so undo that. Values stay in memory."""
    wanted = {}
    for svc, _ctl, master, cname, _env, _key, _grp in PROVIDER_CONFIG_KEYS:
        wanted.setdefault(svc, set()).update((master, cname))
    wanted[NUMBER_SVC] = {NUMBER_MASTER, NUMBER_TELNYX_SECRET, *(c for _k, c in NUMBER_TELNYX_CONFIG), *NUMBER_TWILIO_VARS}
    out = {}
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return None
    for svc, body in services.items():
        if svc not in wanted or not isinstance(body, dict):
            continue
        env = body.get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) if "=" in item else (item, None) for item in env if isinstance(item, str))
        if not isinstance(env, dict):
            env = {}
        out[svc] = {k: (v.replace("$$", "$") if isinstance(v, str) else "")
                    for k, v in env.items() if k in wanted[svc]}
    return out


def provider_plan(compose):
    """What to store: ({service: [entry]}, notes).

    entry = dict(key, env_var, value, group, ctl, master). A group (AWS pair)
    is planned only when every member has a value and each value can be
    stored; otherwise notes tell why it is skipped. Notes are
    (kind, service, stored key, reason) with kind skipped (incomplete pair) or
    invalid (a value that cannot be stored)."""
    plan, notes = {}, []
    members = {}
    for svc, ctl, master, cname, env_var, key, grp in PROVIDER_CONFIG_KEYS:
        if svc not in compose:
            continue
        value = compose[svc].get(cname) or ""
        entry = dict(key=key, env_var=env_var, value=value, group=grp, ctl=ctl, master=master, svc=svc)
        members.setdefault((svc, grp or key), []).append(entry)
    for (svc, _g), ents in members.items():
        have = [e for e in ents if e["value"]]
        if not have:
            continue
        missing = [e["env_var"] for e in ents if not e["value"]]
        if missing:
            for e in have:
                notes.append(("skipped", svc, e["key"], "incomplete pair, no value for " + ", ".join(missing)))
            continue
        bad = [e for e in ents if "\n" in e["value"] or "\r" in e["value"]
               or len(e["value"].encode()) > provider_max_len(e["key"])]
        if bad:
            for e in ents:
                notes.append(("invalid", svc, e["key"],
                              "the value of " + ", ".join(b["env_var"] for b in bad) +
                              " cannot be stored (more than one line or too long); fix or empty it in .env"))
            continue
        plan.setdefault(svc, []).extend(ents)
    return plan, notes


def number_plan(compose):
    """What to do for number-manager: (store, advice, invalid).

    store: None or dict(secret, config) for the Telnyx provider (a token and a
    connection id; the profile id when set). advice: provider names that .env
    cannot store but the operator should hear about (a Telnyx token without a
    connection id; any Twilio value). invalid: notes for values that cannot be
    stored. Values are stripped; blank means empty."""
    env = compose.get(NUMBER_SVC)
    if env is None:
        return None, [], []

    def val(name):
        return (env.get(name) or "").strip()

    token = val(NUMBER_TELNYX_SECRET)
    conf = {k: val(c) for k, c in NUMBER_TELNYX_CONFIG if val(c)}
    store, advice, invalid = None, [], []
    if token and conf.get("connection_id"):
        values = [token] + list(conf.values())
        if (any("\n" in v or "\r" in v for v in values) or len(token.encode()) > PROVIDER_MAX_VALUE_LEN
                or any(len(v.encode()) > NUMBER_CONFIG_MAX_LEN for v in conf.values())):
            invalid.append(("invalid", NUMBER_SVC, "telnyx",
                            "a value of TELNYX_API_KEY, TELNYX_CONNECTION_ID or TELNYX_PROFILE_ID cannot be stored "
                            "(more than one line or too long); fix or empty it in .env"))
        else:
            store = dict(secret=token, config=conf)
    elif token:
        advice.append("telnyx")
    if any(val(v) for v in NUMBER_TWILIO_VARS):
        advice.append("twilio")
    return store, advice, invalid


def provider_parse_number_list(out):
    """{provider: dict(source, has_secret, enabled, usable, config)} from the
    JSON array `number-control provider list` prints, or None.

    Lines before the array (logs) are ignored. Every item must be an object with
    a string provider_name and credential_source; config may be null."""
    lines = (out or "").splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode("\n".join(lines[i:]).lstrip())
        except ValueError:
            continue
        if not (isinstance(data, list) and data and all(
                isinstance(r, dict) and isinstance(r.get("provider_name"), str)
                and isinstance(r.get("credential_source"), str) for r in data)):
            continue
        rows = {}
        for r in data:
            cfg = r.get("config")
            rows[r["provider_name"]] = dict(
                source=r["credential_source"], has_secret=bool(r.get("has_secret")),
                enabled=bool(r.get("enabled")), usable=bool(r.get("usable")),
                config={k: v for k, v in cfg.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(cfg, dict) else {})
        return rows
    return None


def number_row_stored(row):
    """A provider row the installer treats as stored: a secret in the database."""
    return bool(row) and row["source"] == PROVIDER_SOURCE_STORED and row["has_secret"]


def provider_number_row_state(provider, env_set, stored, down):
    """State of a number-manager status row: set | pending | incomplete | empty.

    env_set: the .env variables with a value; stored: {(service, name)}
    readable in the database; down: services that could not be asked (Twilio is
    then empty: the installer never stores it; Telnyx falls back to .env)."""
    if provider == "twilio":
        if NUMBER_SVC in down:
            return "empty"
        if (NUMBER_SVC, "twilio") in stored:
            return "set"
        return "incomplete" if env_set & {"TWILIO_SID", "TWILIO_API_KEY"} else "empty"
    token, conn = "TELNYX_API_KEY" in env_set, "TELNYX_CONNECTION_ID" in env_set
    if NUMBER_SVC in down:
        return "set" if token and conn else "empty"
    if (NUMBER_SVC, "telnyx") in stored:
        return "set"
    if token and conn:
        return "pending"
    return "incomplete" if conn else "empty"


def provider_row_state(group_vars, env_set, stored, down):
    """State of one status row: set | pending | incomplete | empty.

    group_vars: the .env variables of the row; env_set: those with a value in
    .env; stored: {(service, stored key)} readable in the database; down: the
    services that could not be asked (the row then falls back to .env)."""
    mapped = {v: [(s, k) for s, _c, _m, _n, e, k, _g in PROVIDER_CONFIG_KEYS if e == v] for v in group_vars}
    legacy = all(v in env_set for v in group_vars)
    if not all(mapped.values()):
        return "set" if legacy else "empty"
    if any(s in down for v in group_vars for s, _k in mapped[v]):
        return "set" if legacy else "empty"
    if len(group_vars) > 1:
        # A pair is usable only when one service holds every key of it.
        svcs = {s for v in group_vars for s, _k in mapped[v]}
        complete = any(all((s, k) in stored for v in group_vars for s2, k in mapped[v] if s2 == s) and
                       all(any(s2 == s for s2, _k in mapped[v]) for v in group_vars) for s in svcs)
    else:
        # A variable that feeds several services (OPENAI_API_KEY, GOOGLE_API_KEY,
        # ELEVENLABS_API_KEY) is set only when every one of them holds it.
        complete = all(all(t in stored for t in mapped[v]) for v in group_vars)
    if complete:
        return "set"
    if legacy:
        return "pending"
    if len(group_vars) > 1 and any(v in env_set for v in group_vars):
        return "incomplete"
    return "empty"

# =============================================================================
# Guided install helpers (module level so tests can call them directly)
# =============================================================================

INSTALL_MARKER = ".voipbin-installed"
INSTALL_STEPS = 6
INSTALL_INIT_VALUE_FLAGS = ("--mode", "--domain", "--tls", "--cert", "--key",
                            "--kamailio-ip", "--rtpengine-ip")
INSTALL_INIT_BOOL_FLAGS = ("--web-reverse-proxy",)
# doctor.sh checks that block the install when they fail. Every other
# doctor check describes something a later install step creates.
INSTALL_PREFLIGHT_BLOCKING = ("docker", "compose-version", "tools", "ports", "disk-space")
INSTALL_MIN_MEMORY_KIB = 4 * 1024 * 1024
INSTALL_REACHABILITY_URLS = (
    ("Docker Hub", "https://registry-1.docker.io/v2/"),
    ("GitHub", "https://github.com"),
)
INSTALL_TTS_CONTAINER = "voipbin-tts-mgr"
INSTALL_VOIP_INTERFACES = ("kamailio-int", "rtpengine-int")  # scripts/setup-voip-network.sh
INSTALL_TTS_CREDENTIALS_MISSING_LOG = "Check TTS provider credentials"
INSTALL_HEALTHCHECK_BINARY_MISSING = "executable file not found"
INSTALL_EMAIL_RE = re.compile(r"^[^@\s\"']+@[^@\s\"']+\.[^@\s\"']+$|^[^@\s\"']+@localhost$")
# Files init/start/install create; fix_install_ownership() hands them to the
# invoking user (directories recursively).
INSTALL_OWNED_PATHS = (
    ".env", "certs", os.path.join("config", "coredns"), os.path.join("config", "caddy"),
    os.path.join("config", "dummy-gcp-credentials.json"), "docker-compose.yml",
    "versions.lock", "docker-compose.override.yml", ".voipbin-versions",
    ".test_data_initialized", INSTALL_MARKER, "tmp",
)


class InstallAbort(Exception):
    """Stops `install` with an exit code and the three-part error message."""

    def __init__(self, rc, step, reason, next_cmd="", why="", quiet=False):
        super().__init__(reason)
        self.quiet = quiet
        self.rc = rc
        self.step = step
        self.reason = reason
        self.next_cmd = next_cmd
        self.why = why


def clamp_script_rc(rc):
    """init/start/setup-host use 0/1/2; anything else (signals, set -e
    aborts with other codes) is reported as an environment error."""
    return rc if rc in (1, 2) else 2


def resolve_install_owner(project_dir):
    """(uid, gid, name) that generated files should belong to, or None.
    Order: SUDO_USER (when not root), then the owner of project_dir (when
    not root); None when both are root."""
    sudo_user = os.environ.get("SUDO_USER", "")
    if sudo_user:
        try:
            pw = pwd.getpwnam(sudo_user)
            if pw.pw_uid != 0:
                return (pw.pw_uid, pw.pw_gid, sudo_user)
        except KeyError:
            pass
    try:
        st = os.stat(project_dir)
    except OSError:
        return None
    if st.st_uid != 0:
        try:
            name = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            name = str(st.st_uid)
        return (st.st_uid, st.st_gid, name)
    return None


def install_user_caroot(run=subprocess.run):
    """mkcert CA directory of the sudo-invoking user, created by that user, or None.

    Under sudo, init.sh and start.sh would otherwise create and use a CA in
    root's home while setup-host.sh trusts the invoking user's CA directory
    (as the unprivileged flow expects), so the certificates and the trusted
    CA would not match. Passing this directory as CAROOT to init.sh and
    start.sh keeps one CA. mkcert may not be installed yet (setup-host.sh
    installs it); its Linux default directory is used then.
    """
    sudo_user = os.environ.get("SUDO_USER", "")
    if os.geteuid() != 0 or not sudo_user:
        return None
    try:
        pw = pwd.getpwnam(sudo_user)
    except KeyError:
        return None
    if pw.pw_uid == 0:
        return None
    as_user = ["sudo", "-u", sudo_user, "-H"]
    try:
        caroot = os.path.join(pw.pw_dir, ".local", "share", "mkcert")
        if shutil.which("mkcert"):
            out = run(as_user + ["mkcert", "-CAROOT"], capture_output=True, text=True, timeout=30)
            caroot = out.stdout.strip()
            if out.returncode != 0 or not os.path.isabs(caroot):
                return None
        if run(as_user + ["mkdir", "-p", caroot], timeout=30).returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return caroot


def fix_caroot_ownership(caroot, owner, chown=None):
    """Give CA files a root run of mkcert wrote back to their owner."""
    if not caroot or owner is None or not os.path.isdir(caroot) or os.path.islink(caroot):
        return
    if chown is None:
        if os.geteuid() != 0:
            return
        chown = lambda p, u, g: os.chown(p, u, g, follow_symlinks=False)
    for name in os.listdir(caroot):
        path = os.path.join(caroot, name)
        if not os.path.islink(path):
            try:
                chown(path, owner[0], owner[1])
            except OSError:
                pass


def fix_install_ownership(project_dir, owner=None, chown=None):
    """Hand the files init/start/install generate to the invoking user, so a
    `sudo ./voipbin ...` run does not leave root-owned files in the checkout.
    Symlinks are never followed. Returns the number of paths changed."""
    if chown is None:
        if os.geteuid() != 0:
            return 0
        chown = lambda p, u, g: os.chown(p, u, g, follow_symlinks=False)
    owner = owner or resolve_install_owner(project_dir)
    if owner is None:
        # stderr: callers' last stdout line is a VOIPBIN_* result line
        print("  ! file ownership not changed: neither SUDO_USER nor the install directory owner is a regular user",
              file=sys.stderr)
        return 0
    uid, gid = owner[0], owner[1]
    changed = 0
    real_root = os.path.realpath(project_dir)
    for rel in INSTALL_OWNED_PATHS:
        path = os.path.join(project_dir, rel)
        if not os.path.lexists(path) or os.path.islink(path):
            continue
        # A symlinked parent (e.g. config -> elsewhere) would lead outside
        # the checkout: skip unless the parent resolves to where it should.
        if os.path.realpath(os.path.dirname(path)) != os.path.normpath(os.path.join(real_root, os.path.dirname(rel))):
            continue
        targets = [path]
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path, followlinks=False):
                targets += [os.path.join(root, n) for n in dirs + files]
        for t in targets:
            try:
                chown(t, uid, gid)
                changed += 1
            except OSError:
                pass
    return changed


def read_install_marker(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_install_marker(path, admin_email):
    with open(path, "w") as f:
        json.dump({"admin_email": admin_email,
                   "installed_at": datetime.now().astimezone().isoformat(timespec="seconds")}, f)
        f.write("\n")


def classify_install_containers(containers, baseline, tts_unconfigured):
    """Step 5 verdict over a docker-inspect snapshot.

    containers: dicts with name, status, restarting, health, last_probe,
    restart_count. baseline: name -> RestartCount at the start of step 5.
    tts_unconfigured(name) -> True when tts-manager is restarting only
    because TTS credentials are not configured.
    Returns {"fail": [(name, why)], "known": [...], "optional": [...],
    "starting": [...]}.
    """
    verdict = {"fail": [], "known": [], "optional": [], "starting": []}
    for c in containers:
        name = c["name"]
        restarted = c["restarting"] or c["restart_count"] > baseline.get(name, c["restart_count"])
        if restarted:
            if tts_unconfigured(name):
                verdict["optional"].append(name)
            else:
                verdict["fail"].append((name, "restarting"))
            continue
        if c["status"] != "running":
            verdict["fail"].append((name, c["status"] or "not running"))
            continue
        if c["health"] == "starting":
            verdict["starting"].append(name)
        elif c["health"] == "unhealthy":
            if INSTALL_HEALTHCHECK_BINARY_MISSING in c["last_probe"]:
                verdict["known"].append(name)
            else:
                verdict["fail"].append((name, "unhealthy"))
    return verdict


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


# =============================================================================
# Git helpers (script update)
# =============================================================================
#
# The CLI runs as root (check_root), but the checkout is normally owned by an
# unprivileged user. Running git as root would leave root-owned objects in
# .git and in the work tree, so every git call is made as the owner of the
# repository's `.git`. The demotion uses preexec_fn, which Python documents as
# unsafe in processes that have threads: these helpers must only be called from
# the single-threaded script-update path, never from the ThreadPoolExecutor
# used by _update_images.

class GitOwnerError(Exception):
    """The repository owner cannot be used to run git (no passwd entry)."""


def _find_git_marker(start):
    """Walk from `start` towards the filesystem root looking for `.git`
    (a directory, or a file for worktrees). Uses os.stat only - no git call,
    so no git is run as root before the owner is known. Returns
    (directory, marker_path, stat_result) or None."""
    cur = os.path.realpath(start)
    while True:
        marker = os.path.join(cur, ".git")
        try:
            return cur, marker, os.stat(marker)
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_owner(marker_stat):
    """(uid, gid) git must run as, or None to run as the current user.
    Only a root process facing a non-root-owned repository demotes."""
    if os.geteuid() == 0 and marker_stat.st_uid != 0:
        return (marker_stat.st_uid, marker_stat.st_gid)
    return None


def _run_as(owner, argv, cwd):
    """Run argv (captured, text) as `owner` ((uid, gid) or None)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    kwargs = {}
    if owner:
        uid, gid = owner
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            raise GitOwnerError(f"uid {uid} has no passwd entry")
        env["HOME"] = pw.pw_dir
        env.pop("XDG_CONFIG_HOME", None)
        groups = os.getgrouplist(pw.pw_name, gid)

        def _demote():
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)

        kwargs["preexec_fn"] = _demote
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                          text=True, **kwargs)


def _git(cwd, *args, owner=None):
    """Run `git <args>` in cwd as `owner`. Argument list, never a shell."""
    return _run_as(owner, ["git"] + list(args), cwd)


def _not_writable_by(owner, paths, cwd):
    """Subset of `paths` the (demoted) owner cannot write. Evaluated in a child
    process running as that owner: os.access() in a root process is always
    True and says nothing about the owner's rights."""
    if not paths:
        return []
    script = 'for p in "$@"; do [ -w "$p" ] || printf "%s\\n" "$p"; done'
    # Hand the paths over in bounded batches so a large checkout cannot
    # exceed the kernel argv limit (E2BIG).
    out = []
    batch, size = [], 0
    for p in list(paths) + [None]:
        if p is None or size + len(p) + 1 > 60000 or len(batch) >= 2000:
            if batch:
                res = _run_as(owner, ["sh", "-c", script, "sh"] + batch, cwd)
                out.extend(line for line in (res.stdout or "").splitlines() if line)
            batch, size = [], 0
        if p is not None:
            batch.append(p)
            size += len(p) + 1
    return out


def _ownership_offenders(owner, git_dir, top, tracked, lstat_fn=None):
    """Paths under .git / the tracked files that a demoted git could not
    update: owned by someone else AND not writable for the owner, or tracked
    files whose directory is not writable. Used before touching the work tree."""
    lstat_fn = lstat_fn or os.lstat
    uid = owner[0]
    candidates = [git_dir]
    foreign = []

    def consider(path):
        try:
            st = lstat_fn(path)
        except OSError:
            return
        if st.st_uid != uid:
            foreign.append(path)

    if os.path.isdir(git_dir):
        for root, dirs, files in os.walk(git_dir):
            for name in dirs + files:
                consider(os.path.join(root, name))
    parents = set()
    for rel in tracked:
        path = os.path.join(top, rel)
        consider(path)
        # A tracked directory the user deleted locally is not an ownership
        # problem; check the nearest ancestor that still exists instead.
        parent = os.path.dirname(path)
        while parent and parent != top and not os.path.isdir(parent):
            parent = os.path.dirname(parent)
        if os.path.isdir(parent):
            parents.add(parent)
    candidates += sorted(parents) + foreign
    seen = set()
    uniq = [c for c in candidates if not (c in seen or seen.add(c))]
    bad = set(_not_writable_by(owner, uniq, top))
    # A foreign-owned entry that the owner can still write (group write) is fine.
    return sorted(p for p in bad if p in foreign or p in parents or p == git_dir)


def _match_parent_owner(path):
    """Give a newly created file the owner of its directory when this process
    runs as root and the directory belongs to someone else."""
    try:
        if os.geteuid() != 0:
            return
        st = os.stat(os.path.dirname(os.path.abspath(path)))
        if st.st_uid != os.geteuid():
            os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _is_protected_path(rel):
    """True when `rel` (relative to the install directory) is one of
    PROTECTED_PATHS. Boundary match: `.env` does not protect `.env.template`."""
    rel = rel.rstrip("/")
    for p in PROTECTED_PATHS:
        base = p.rstrip("/")
        if rel == base or rel.startswith(base + "/"):
            return True
    return False


def _parse_porcelain_z(raw):
    """Parse `git status --porcelain -z` into [(xy, path)]. A rename or copy
    entry carries the destination first and the source as a separate field."""
    entries = []
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        xy, path = f[:2], f[3:]
        if "R" in xy or "C" in xy:
            i += 1  # skip the source path
        entries.append((xy, path))
    return entries


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
        # Exit code of the last init/start/clean/install run, propagated by
        # the non-interactive dispatch in _main() (0 ok, 1 user/validation
        # error, 2 environment error; init/start pass their script's code).
        self.last_rc = 0
        # True only in the terminal REPL started with the TUI (see tui_wanted);
        # help and tab completion add the slash commands then.
        self.tui_on = False

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
            "install": self.cmd_install,
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
            "start": ("Start services", "start [service] [--no-pin]\n  start              Start all services (installs with versions.lock are already pinned)\n  start --no-pin     Start without creating an image override\n  start api-manager  Start specific service"),
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
            "install": ("Guided install (checks, configuration, host setup, start, admin account)", "install [--yes] [--admin-email <email>] [--admin-password-file <path>]\n          [--mode internal|external --domain <d> --tls byo --cert <f> --key <f>]\n          [--web-reverse-proxy] [--resume] [--reset-admin-password]\n  install                     Interactive: asks before changing the host and for the admin email\n  install --yes --admin-email <e> --admin-password-file <path>\n                              Non-interactive; the generated admin password is written to <path> (0600,\n                              must not exist; a relative path is relative to this directory)\n  install --resume            Continue an install that stopped after the configuration step\n  --reset-admin-password      Set a new password when the admin account already exists\n  Last line: VOIPBIN_INSTALL: status=ok|error ...  Exit code 0 ok, 1 user/verification error, 2 environment error"),
            "init": ("Generate configuration only (.env, certificates)", "init [init.sh options]\n  Runs scripts/init.sh with the given options (for example --yes, --mode external ...)\n  'install' runs this for you."),
            "clean": ("Remove containers, data or generated files", "clean [options] [--force]\n  clean --containers  Remove app containers (keeps db/redis/mq/dns)\n  clean --volumes     Remove docker volumes (database, recordings, redis, rabbitmq)\n  clean --images      Remove docker images\n  clean --network     Teardown VoIP network interfaces\n  clean --dns         Remove DNS configuration\n  clean --purge       Remove generated files (.env, certs, configs, docker-compose.yml, versions.lock)\n  clean --all         All of the above (full reset)\n  --volumes/--images/--purge/--all list what will be removed and ask for 'yes';\n  --force skips the question (required when not run from a terminal)"),
            "update": ("Update the install", "update [subcommand] [--check] [--skip-backup]\n  update               Pull latest Docker images + restart services\n                       (pinned repo: pulls pinned digests only; real upgrades = 'update all')\n  update --check       Dry-run: show available image updates\n  update scripts       Update scripts/configs from GitHub (with backup)\n  update scripts --check  Dry-run: show what would change\n  update all           Full upgrade. Pinned repo: backup -> git pull -> compose pull\n                       -> migrate -> store provider keys -> up -d -> verify (safe-ordered)\n  update all --skip-backup  Skip the automatic pre-upgrade backup (pinned repo)\n  update all --check   Dry-run: describe the upgrade plan, change nothing\n  update keys          Store the provider keys of .env that are not stored yet (the services read\n                       them from the database, number-manager its Telnyx account); safe to repeat,\n                       never overwrites a stored key\n  update keys --check  Show which keys would be stored, change nothing"),
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
        """Show help (also used for `--help`, see show_cli_usage)"""
        if args:
            cmd = args[0].lower()
            if cmd == "api":
                print(render_api_help())
                return
            if cmd in self.help_text:
                desc, usage = self.help_text[cmd]
                print(f"\n{bold(cmd)} - {desc}\n")
                print(f"Usage:\n  {usage}\n")
            else:
                print(f"Unknown command: {cmd}")
            return
        print(render_help())
        if self.tui_on:
            print(render_slash_list())

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

    def _web_reverse_proxy(self):
        """True when init.sh --web-reverse-proxy was used (VOIP-1325):
        admin/meet/talk/api are reachable port-less via Caddy, and
        :3003-3005 are bound to 127.0.0.1 only (see SQUARE_BIND_ADDR)."""
        return (read_env_file_var(self._project_dir(), "WEB_REVERSE_PROXY") or "false") == "true"

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
        # Web services use Docker port mapping on HOST_IP, unless the Caddy
        # reverse proxy (VOIP-1325) is active, in which case :3003-3005 are
        # bound to 127.0.0.1 only and the port-less Caddy URL is the real one.
        if self._web_reverse_proxy():
            web_endpoints = {
                "admin": ("Admin Console", f"https://admin.{base_domain}", None),
                "api-mgr": ("API Manager", f"https://api.{base_domain}", None),
            }
        else:
            web_endpoints = {
                "admin": ("Admin Console", f"http://admin.{base_domain}:3003", None),
                "api-mgr": ("API Manager", f"https://api.{base_domain}:8443", None),
            }
        endpoint_services = {
            **web_endpoints,
            # Credentials are never printed here: they live in .env.
            "mq": ("RabbitMQ", "http://localhost:15672", "credentials: see .env"),
            "db": ("MySQL", "localhost:3306", "credentials: see .env"),
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

        # Optional provider keys: whether each one is set, nothing more
        # (none of them is needed for the stack to start).
        print(f"  {gray('Optional provider keys (the services read most of them from the database; pending = in .env, not stored yet: sudo ./voipbin update keys):')}")
        gcp_creds_path = get_env_var("GOOGLE_APPLICATION_CREDENTIALS")
        if not gcp_creds_path:
            gcp_state = "empty"
        elif "dummy" in gcp_creds_path.lower():
            gcp_state = "placeholder"
        else:
            gcp_state = "set"
        rows = [("GOOGLE_APPLICATION_CREDENTIALS", gcp_state)]
        env_set = {v for keys in OPTIONAL_PROVIDER_KEYS for v in keys if get_env_var(v)}
        if get_env_var("TELNYX_CONNECTION_ID"):
            env_set.add("TELNYX_CONNECTION_ID")
        try:
            stored, down = self._provider_stored(self.config.get("project_dir", "."))
        except Exception:
            stored, down = set(), {r[0] for r in PROVIDER_CONFIG_KEYS} | {NUMBER_SVC}
        for keys in OPTIONAL_PROVIDER_KEYS:
            if keys == ("TWILIO_SID", "TWILIO_API_KEY"):
                rows.append((" + ".join(keys), provider_number_row_state("twilio", env_set, stored, down)))
                continue
            rows.append((" + ".join(keys), provider_row_state(keys, env_set, stored, down)))
            if keys == ("TELNYX_API_KEY",):
                rows.append(("TELNYX_CONNECTION_ID (numbers)", provider_number_row_state("telnyx", env_set, stored, down)))
        for label, state in rows:
            icon = green('●') if state == "set" else (yellow('◐') if state in ("pending", "incomplete") else gray('○'))
            shown = green(state) if state == "set" else (yellow(state) if state in ("pending", "incomplete") else gray(state))
            print(f"  {icon} {label:<36} {shown}")

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

        if self._web_reverse_proxy():
            print(f"\n  {bold('Web Services')} (via Caddy reverse proxy, no port suffix)")
            print(f"    {'https://api.' + base_domain:<34}API Manager")
            print(f"    {'https://admin.' + base_domain:<34}Admin Console")
            print(f"    {'https://meet.' + base_domain:<34}Meet")
            print(f"    {'https://talk.' + base_domain:<34}Talk")
        else:
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

        self.last_rc = 0
        if service:
            # Start specific service
            print(f"Starting {service}...")
            proc = subprocess.run(["docker", "compose", "up", "-d", service],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.stdout:
                print(proc.stdout.rstrip())
            if proc.returncode == 0:
                print(green("✓ Done"))
            else:
                print(red(f"✗ docker compose up -d {service} failed (exit {proc.returncode})"))
                self.last_rc = 1
            self._check_ticker_replica_guard()
        else:
            # Full startup
            script_dir = self.config.get("project_dir", ".")

            # Image version pins for unpinned checkouts only. A versions.lock
            # install already runs digest-pinned images (docker-compose.yml
            # references @sha256 digests), so resolving tags here would only
            # make external calls and could shadow those digests.
            override_file = os.path.join(script_dir, "docker-compose.override.yml")
            pinned = os.path.exists(os.path.join(script_dir, "versions.lock"))
            if not os.path.exists(override_file) and not no_pin and not pinned:
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
                # subprocess.call returns the real exit code (os.system's
                # wait status was discarded before); _main() propagates it.
                self.last_rc = subprocess.call(["bash", script_path], cwd=script_dir)
                fix_install_ownership(script_dir)
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

    # network_mode: "service:<owner>" sidecars share the owner's kernel
    # network namespace so their loopback-bound peer resolves over localhost
    # (ARI/AMI for the asterisk-*-proxy trio; pipecat-script-runner's
    # uvicorn for pipecat-manager). `docker restart <owner>` gives the owner
    # a NEW namespace but does NOT restart the sidecar, which stays attached
    # to the old, now-dead namespace forever (connection refused). See
    # VOIP-1237. Owner and sidecar MUST always be restarted together.
    SIDECAR_PAIRS = {
        "asterisk-call": "asterisk-call-proxy",
        "asterisk-conference": "asterisk-conference-proxy",
        "asterisk-registrar": "asterisk-registrar-proxy",
        "pipecat-manager": "pipecat-script-runner",
    }

    def _restart_sidecar_pair(self, owner, proxy, health_timeout=60):
        """Restart a sidecar-pair owner container, wait for it to report
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
        """Restart services (SIDECAR_PAIRS owner containers auto-pull in
        their network-namespace-sharing sidecar, see VOIP-1237)"""
        service = args[0] if args else ""

        if service:
            # If the requested service is a SIDECAR_PAIRS owner, force its
            # network-namespace-sharing sidecar to restart AFTER it comes
            # back healthy, so the sidecar never survives into an orphaned
            # namespace (VOIP-1237).
            paired_proxy = self.SIDECAR_PAIRS.get(service)
            if paired_proxy:
                print(
                    f"Restarting {service}, then its sidecar {paired_proxy} "
                    f"once healthy (required: they share a network "
                    f"namespace, see VOIP-1237)..."
                )
                self._restart_sidecar_pair(service, paired_proxy)
            else:
                print(f"Restarting {service}...")
                result = run_cmd(f"docker compose restart {service} 2>&1")
                if result:
                    print(result)

            print(green("✓ Done"))
        else:
            # Full-stack restart: `docker compose restart` (no args) does not
            # guarantee any owner-before-sidecar ordering, so a sidecar can
            # still end up restarted before (or without) its owner and be
            # left in a dead namespace. Restart every owner+sidecar pair in
            # the correct order first, then restart everything else in one
            # shot.
            print("Restarting all services...")

            paired_services = set()
            for owner, proxy in self.SIDECAR_PAIRS.items():
                paired_services.add(owner)
                paired_services.add(proxy)
                print(f"  -> {owner}, then {proxy} once healthy (VOIP-1237)")
                self._restart_sidecar_pair(owner, proxy)

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
        print("This install does not run CoreDNS or touch resolv.conf in external")
        print("mode. Create these records at your DNS provider:")
        print()
        print(f"  {'Record':<28} {'Type':<6} {'Target':<18} Purpose")
        print(f"  {'-' * 68}")
        # DNS targets are unaffected by --web-reverse-proxy (Caddy listens on
        # the same host IP, just port 443 instead of the individual :3003-
        # :3005/:8443 published ports) — only the purpose annotation changes.
        if self._web_reverse_proxy():
            web_rows = [
                (f"api.{d}", host_ip, "REST API + WebSocket, via Caddy reverse proxy (:443)"),
                (f"admin.{d}", host_ip, "Admin Console, via Caddy reverse proxy (:443)"),
                (f"meet.{d}", host_ip, "Meet, via Caddy reverse proxy (:443)"),
                (f"talk.{d}", host_ip, "Talk, via Caddy reverse proxy (:443)"),
            ]
        else:
            web_rows = [
                (f"api.{d}", host_ip, "REST API + WebSocket (:8443)"),
                (f"admin.{d}", host_ip, "Admin Console (:3003)"),
                (f"meet.{d}", host_ip, "Meet (:3004)"),
                (f"talk.{d}", host_ip, "Talk (:3005)"),
            ]
        rows = [
            *web_rows,
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
        print(f"\n{bold('Internal Interfaces')} (Docker bridge → host veth pair, VOIP-1331)")
        print("-" * 60)

        # VOIP-1331 review round 4, MEDIUM: an IP being present is not
        # sufficient - a legacy macvlan interface or an orphaned veth peer
        # (bridge-side "-br" end with no `master`, e.g. after `docker
        # compose down`/`voipbin> clean` recreates the compose network with
        # a new bridge name) both hold a valid-looking IP while Kamailio/
        # RTPEngine are actually cut off from every container. Matches the
        # same check used for the dashboard indicator and doctor.sh.
        def _voip_interface_status(name):
            ip_addr = run_cmd(f"ip addr show {name} 2>/dev/null | grep -oP 'inet \\K[\\d./]+' | head -1")
            if not ip_addr:
                return None, "not configured"
            if "macvlan" in run_cmd(f"ip -d link show {name} 2>/dev/null"):
                return ip_addr, "legacy macvlan interface, needs migration (VOIP-1331)"
            peer = name.replace("-int", "-br")
            if "master" not in run_cmd(f"ip link show {peer} 2>/dev/null"):
                return ip_addr, "orphaned bridge peer, likely after a network recreate (VOIP-1331)"
            return ip_addr, None

        kamailio_ip_addr, kamailio_problem = _voip_interface_status("kamailio-int")
        rtpengine_ip_addr, rtpengine_problem = _voip_interface_status("rtpengine-int")

        if kamailio_ip_addr and not kamailio_problem:
            print(f"  {green('●')} kamailio-int:  {kamailio_ip_addr}")
        elif kamailio_ip_addr:
            print(f"  {red('○')} kamailio-int:  {kamailio_ip_addr} ({kamailio_problem})")
        else:
            print(f"  {red('○')} kamailio-int:  not configured")

        if rtpengine_ip_addr and not rtpengine_problem:
            print(f"  {green('●')} rtpengine-int: {rtpengine_ip_addr}")
        elif rtpengine_ip_addr:
            print(f"  {red('○')} rtpengine-int: {rtpengine_ip_addr} ({rtpengine_problem})")
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
        if self._web_reverse_proxy():
            print(f"\n{bold('Web Services')} (via Caddy reverse proxy on {host_ip}, no port suffix)")
            print("-" * 60)
            print(f"  API:   {host_ip}:443")
            print(f"  Admin: {host_ip}:443")
            print(f"  Meet:  {host_ip}:443")
            print(f"  Talk:  {host_ip}:443")
        else:
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

    # -------------------------------------------------------------------------
    # Guided install (`sudo ./voipbin install`)
    #
    # Orchestrates the existing scripts (doctor.sh, init.sh, setup-host.sh,
    # start.sh, create-admin.sh) in six steps and ends every path with one
    # machine-parseable line:
    #   VOIPBIN_INSTALL: status=ok state=installed|already-installed ...
    #   VOIPBIN_INSTALL: status=error step=<n> reason="..." next="..."
    # Exit codes: 0 ok; 1 user/validation error or failed verification;
    # 2 environment error; init/start/setup-host failures propagate their own
    # code (anything other than 1/2 maps to 2); 130 interrupted.
    # -------------------------------------------------------------------------

    def _install_parse_args(self, args):
        opts = {
            "yes": False, "resume": False, "admin_email": "", "admin_name": "Admin",
            "password_file": "", "reset": False, "init_args": [],
        }
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--yes", "-y"):
                opts["yes"] = True
            elif a == "--resume":
                opts["resume"] = True
            elif a == "--reset-admin-password":
                opts["reset"] = True
            elif a in INSTALL_INIT_BOOL_FLAGS:
                opts["init_args"].append(a)
            elif a in ("--admin-email", "--admin-name", "--admin-password-file") or a in INSTALL_INIT_VALUE_FLAGS:
                if i + 1 >= len(args) or args[i + 1].startswith("--"):
                    raise InstallAbort(1, 0, f"{a} requires a value", "sudo ./voipbin help install")
                value = args[i + 1]
                i += 1
                if a == "--admin-email":
                    opts["admin_email"] = value
                elif a == "--admin-name":
                    opts["admin_name"] = value
                elif a == "--admin-password-file":
                    opts["password_file"] = value
                else:
                    opts["init_args"] += [a, value]
            else:
                raise InstallAbort(1, 0, f"unknown argument: {a}", "sudo ./voipbin help install")
            i += 1
        if opts["resume"] and opts["init_args"]:
            raise InstallAbort(1, 0, "--resume does not accept --mode/--domain/--tls/--cert/--key (the existing .env is kept)",
                               "sudo ./voipbin install --resume")
        return opts

    def _install_result_line(self, rc, state=None, step=None, reason="", next_cmd="", admin="", url=""):
        if rc == 0:
            line = f"VOIPBIN_INSTALL: status=ok state={state}"
            if admin:
                line += f" admin={admin}"
            if url:
                line += f" url={url}"
        else:
            reason = reason.replace('"', "'")
            line = f'VOIPBIN_INSTALL: status=error step={step} reason="{reason}"'
            if next_cmd:
                line += f' next="{next_cmd.replace(chr(34), chr(39))}"'
        print(line)

    def _install_child_env(self, extra=None):
        env = dict(os.environ)
        if env.get("NO_COLOR"):
            # COMPOSE_PROGRESS only matters on Compose >= 2.36; COMPOSE_ANSI
            # alone already keeps escape codes out of compose output.
            env["COMPOSE_ANSI"] = "never"
            env["COMPOSE_PROGRESS"] = "plain"
        if extra:
            env.update(extra)
        return env

    def _install_step(self, n, title):
        print(f"\n{bold(f'[{n}/{INSTALL_STEPS}]')} {title}")

    def _install_ask(self, interactive, prompt, default_yes=True):
        if not interactive:
            return True
        suffix = "[Y/n]" if default_yes else "[y/N]"
        try:
            answer = input(f"  {prompt} {suffix} ").strip().lower()
        except EOFError:
            return False
        if not answer:
            return default_yes
        return answer in ("y", "yes")

    def _install_db_volume_exists(self, project_dir):
        try:
            name = f"{self._compose_project_name(project_dir)}_db_data"
        except ValueError:
            return False, ""
        out = subprocess.run(["docker", "volume", "ls", "-q"], capture_output=True, text=True)
        return name in out.stdout.split(), name

    def _install_preflight(self, project_dir, resume):
        """Step 1: doctor.sh's prerequisite checks plus memory and network
        reachability. Returns the list of warning lines; raises on a block."""
        warnings = []
        doctor = os.path.join(project_dir, "scripts", "doctor.sh")
        fails = []
        if os.path.exists(doctor):
            out = subprocess.run(["bash", doctor], cwd=project_dir, capture_output=True, text=True,
                                 env=self._install_child_env()).stdout
            fixes = {}
            results = []
            for line in out.splitlines():
                m = re.match(r"^DOCTOR ([a-z0-9-]+): (pass|fail|warn|skip) ?(.*)$", line)
                if m:
                    results.append(m.groups())
                    continue
                m = re.match(r"^FIX ([a-z0-9-]+): (.*)$", line)
                if m:
                    fixes[m.group(1)] = m.group(2)
            soft = ("ports", "tools") if resume else ()
            for name, result, detail in results:
                if name not in INSTALL_PREFLIGHT_BLOCKING:
                    continue
                if result == "fail" and name not in soft:
                    fails.append((name, detail, fixes.get(name, "")))
                elif result in ("fail", "warn"):
                    warnings.append(f"{name}: {detail}")
        else:
            warnings.append("scripts/doctor.sh not found, prerequisite checks skipped")
        if fails:
            for name, detail, fix in fails:
                print(f"  {red('x')} {name}: {detail}")
                if fix:
                    print(f"    FIX {name}: {fix}")
            first = fails[0]
            raise InstallAbort(2, 1, f"host check failed: {first[0]}: {first[1]}",
                               first[2] or "sudo ./voipbin doctor",
                               why="a prerequisite for running the stack is missing")
        if not resume:
            left = [n for n in INSTALL_VOIP_INTERFACES if os.path.exists(f"/sys/class/net/{n}")]
            if left:
                # Left by a removed install: next to a new compose network they
                # cut container traffic (DNS lookups and downloads time out).
                print(f"  {red('x')} VoIP network interfaces from a previous install: {', '.join(left)}")
                raise InstallAbort(2, 1, "VoIP network interfaces from a previous install are still present",
                                   "sudo ./voipbin clean --network --force, then run install again",
                                   why="a new install next to them loses container network access")
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kib = int(line.split()[1])
                        if kib < INSTALL_MIN_MEMORY_KIB:
                            warnings.append(f"memory: {kib // 1024} MiB total, at least 4 GiB is recommended")
                        break
        except (OSError, ValueError, IndexError):
            pass
        for label, url in INSTALL_REACHABILITY_URLS:
            try:
                urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=5).close()
            except urllib.error.HTTPError:
                pass  # an HTTP answer means the host is reachable
            except Exception:
                warnings.append(f"network: {label} ({url}) not reachable; image pulls or downloads may fail")
        return warnings

    def _install_check_init_outputs(self, project_dir, resume_cmd="sudo ./voipbin install --resume"):
        """--resume: init is not re-run, so its outputs must already exist."""
        missing = [p for p in ("docker-compose.yml", "versions.lock",
                               os.path.join("certs", "api", "cert.pem"),
                               os.path.join("certs", "api", "privkey.pem"))
                   if not os.path.exists(os.path.join(project_dir, p))]
        if not missing:
            return
        hint = []
        if "docker-compose.yml" in missing:
            hint.append("cp docker-compose.yml.dist docker-compose.yml")
        if "versions.lock" in missing:
            hint.append("cp versions.lock.dist versions.lock")
        if any(m.startswith("certs") for m in missing):
            if self._domain_mode() == "external":
                hint.append("./scripts/install-certs.sh <fullchain.pem> <privkey.pem>")
            else:
                # not init.sh --force-reinit: it would generate new database
                # passwords that no longer match an existing volume
                hint.append("restore certs/ from backups/<timestamp>/ (or start over: sudo ./voipbin clean --all --force)")
        raise InstallAbort(1, 2, f"files from a previous init are missing: {', '.join(missing)}",
                           "; ".join(hint) + "; then " + resume_cmd,
                           why="--resume does not re-run init")

    def _install_host_notice(self):
        print("  This step changes the host:")
        if self._domain_mode() == "external":
            print("    - creates the VoIP network interfaces (macvlan/veth) and the compose network")
            print("  DNS and certificates stay operator-managed in external mode.")
        else:
            print(f"    - points /etc/resolv.conf at the bundled CoreDNS (backup: /etc/resolv.conf.voipbin-backup)")
            print("      and restarts systemd-resolved / NetworkManager where present")
            print("    - creates the VoIP network interfaces (veth pairs) and the compose network")
            print("    - installs mkcert and trusts its local CA when available")
        print("  To undo later: sudo ./voipbin clean --dns, sudo ./voipbin clean --network,")
        print("                 sudo ./scripts/setup-dns.sh --uninstall")
        print("  The VoIP interfaces do not survive a reboot; 'sudo ./voipbin start' recreates them.")

    def _install_inspect_containers(self, project_dir):
        ids = subprocess.run(["docker", "compose", "ps", "-a", "-q"], cwd=project_dir,
                             capture_output=True, text=True).stdout.split()
        if not ids:
            return []
        out = subprocess.run(["docker", "inspect"] + ids, capture_output=True, text=True).stdout
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError:
            return []
        result = []
        for c in data:
            state = c.get("State") or {}
            health = state.get("Health") or {}
            log = health.get("Log") or []
            result.append({
                "name": (c.get("Name") or "").lstrip("/"),
                "status": state.get("Status", ""),
                "restarting": bool(state.get("Restarting")),
                "health": health.get("Status", ""),
                "last_probe": (log[-1].get("Output") or "") if log else "",
                "restart_count": int(c.get("RestartCount") or 0),
            })
        return result

    def _install_tts_unconfigured(self, name):
        if name != INSTALL_TTS_CONTAINER:
            return False
        out = subprocess.run(["docker", "logs", "--tail", "200", name], capture_output=True, text=True)
        return INSTALL_TTS_CREDENTIALS_MISSING_LOG in (out.stdout + out.stderr)

    def _install_wait_and_verify(self, project_dir, clock=time.monotonic, sleep=time.sleep,
                                 ping=None, min_observe=30, limit=180, interval=5):
        """Step 5. Polls until no container is still starting (at least
        min_observe seconds, at most limit), then classifies. Returns
        (ok, summary, optional_names, failures)."""
        start = clock()
        baseline = {c["name"]: c["restart_count"] for c in self._install_inspect_containers(project_dir)}
        while True:
            snapshot = self._install_inspect_containers(project_dir)
            verdict = classify_install_containers(snapshot, baseline, self._install_tts_unconfigured)
            elapsed = clock() - start
            if (elapsed >= min_observe and not verdict["starting"]) or elapsed >= limit:
                break
            sleep(interval)
        failures = list(verdict["fail"])
        failures += [(n, f"still starting after {limit}s") for n in verdict["starting"]]
        if not snapshot:
            failures.append(("compose", "no containers found (docker compose ps -a)"))
        ping = ping or self._install_ping
        if not ping():
            failures.append(("api-manager", "https://localhost:8443/ping did not answer 200"))
        known = len(verdict["known"])
        summary = f"{len(snapshot)} containers"
        if known:
            summary += f", {known} have no healthcheck binary (known, not a fault)"
        return (not failures), summary, verdict["optional"], failures

    def _install_ping(self, attempts=6, delay=5):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        for i in range(attempts):
            try:
                with urllib.request.urlopen("https://localhost:8443/ping", timeout=5, context=ctx) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            if i + 1 < attempts:
                time.sleep(delay)
        return False

    def _install_admin_console_url(self):
        base = self._base_domain()
        if self._web_reverse_proxy():
            return f"https://admin.{base}"
        return f"http://admin.{base}:3003"

    def _install_open_password_file(self, path):
        if os.path.lexists(path):
            raise InstallAbort(1, 0, f"--admin-password-file {path} already exists (choose a new path)",
                               "sudo ./voipbin install ... --admin-password-file <new path>")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            raise InstallAbort(1, 0, f"cannot create --admin-password-file {path}: {e.strerror}",
                               "pass a path in an existing, writable directory")
        owner = resolve_install_owner(os.path.dirname(os.path.abspath(path)))
        if owner and os.geteuid() == 0:
            try:
                os.fchown(fd, owner[0], owner[1])
            except OSError:
                pass
        return fd

    def _install_create_admin(self, project_dir, email, name, password, reset):
        script = os.path.join(project_dir, "scripts", "create-admin.sh")
        cmd = ["bash", script, "--email", email, "--name", name, "--wait", "60"]
        if reset:
            cmd.append("--reset")
        proc = subprocess.run(cmd, cwd=project_dir, stdout=subprocess.PIPE, text=True,
                              env=self._install_child_env({"VOIPBIN_ADMIN_PASSWORD": password}))
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        return proc.returncode, (lines[-1] if lines else "")

    def cmd_install(self, args):
        """Guided install: preflight, init, host setup, start, verify, admin."""
        self.last_rc = 0
        project_dir = self._project_dir()
        marker = os.path.join(project_dir, INSTALL_MARKER)
        env_path = os.path.join(project_dir, ".env")
        pw_fd = None
        pw_path = ""
        pw_done = False
        touched = False
        step = 0
        resume_cmd = "sudo ./voipbin install --resume"
        try:
            print()
            print(bold("  VoIPBin Installer"))
            print("  " + "─" * 42)
            opts = self._install_parse_args(args)
            interactive = (not opts["yes"]) and sys.stdin.isatty() and sys.stdout.isatty()

            # Entry guards (nothing is changed before these pass)
            if os.path.exists(env_path):
                if opts["resume"]:
                    if os.path.exists(marker):
                        raise InstallAbort(1, 0, "this install already completed; nothing to resume",
                                           "sudo ./voipbin status (or sudo ./voipbin doctor)")
                else:
                    print("  An existing configuration (.env) was found; nothing was changed.")
                    print("  Check it with: sudo ./voipbin status  /  sudo ./voipbin doctor")
                    if os.path.exists(marker):
                        self._install_result_line(0, state="already-installed",
                                                  admin=read_install_marker(marker).get("admin_email", ""),
                                                  url=self._install_admin_console_url())
                        return
                    print("  If a previous 'install' did not finish, continue it with:")
                    print("    sudo ./voipbin install --resume")
                    print("  If this server is already running (installed with init/start), do not")
                    print("  use --resume; use status and doctor instead.")
                    raise InstallAbort(1, 0, "existing .env without a completed install",
                                       "sudo ./voipbin install --resume (only if a previous install did not finish) or sudo ./voipbin status")
            if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                raise InstallAbort(2, 0, "docker is not installed or the daemon is not reachable",
                                   "install docker (https://docs.docker.com/engine/install/) or sudo systemctl start docker")
            if not os.path.exists(env_path):
                exists, volume = self._install_db_volume_exists(project_dir)
                if exists:
                    print(f"  A database volume ({volume}) exists but there is no .env.")
                    print("  Its passwords and encryption keys live in the missing .env, so a new")
                    print("  configuration would not match the data. Choose one:")
                    print("    - restore the .env from a backup: cp backups/<timestamp>/.env .env")
                    print("    - or delete the old data first: sudo ./voipbin clean --volumes --force")
                    raise InstallAbort(1, 0, f"database volume {volume} exists without .env",
                                       "restore .env from backups/<timestamp>/ or sudo ./voipbin clean --volumes --force")
                if opts["resume"]:
                    raise InstallAbort(1, 0, "nothing to resume (no .env)", "sudo ./voipbin install")

            # Argument checks that need no host change
            if not opts["yes"] and not interactive:
                raise InstallAbort(1, 0, "non-interactive run needs --yes",
                                   "sudo ./voipbin install" + (" --resume" if opts["resume"] else "") +
                                   " --yes --admin-email <email> --admin-password-file <path>")
            if opts["admin_email"] and not INSTALL_EMAIL_RE.match(opts["admin_email"]):
                raise InstallAbort(1, 0, f"invalid --admin-email: {opts['admin_email']}", "pass a valid email address")
            if not interactive and not opts["admin_email"]:
                raise InstallAbort(1, 0, "--admin-email is required with --yes",
                                   "sudo ./voipbin install" + (" --resume" if opts["resume"] else "") +
                                   " --yes --admin-email <email> --admin-password-file <path>")
            if opts["password_file"]:
                pw_fd = self._install_open_password_file(opts["password_file"])
                # Set only once this run created the file: the cleanup in
                # `finally` must never remove a file that existed before.
                pw_path = opts["password_file"]
            if not os.path.exists(env_path) and os.path.exists(marker):
                os.remove(marker)  # left over from a removed install
            # What to run after a failure: same mode (interactive or --yes)
            resume_cmd = "sudo ./voipbin install --resume"
            if opts["yes"]:
                resume_cmd += f" --yes --admin-email {opts['admin_email']}"
                if opts["password_file"]:
                    resume_cmd += " --admin-password-file <new path>"

            if interactive and not opts["resume"]:
                print("  No installation found in this directory.")
                if not self._install_ask(True, "Install now?"):
                    raise InstallAbort(1, 0, "cancelled by the user", "sudo ./voipbin install", quiet=True)
                print(gray("  (Everything is automatic. AI provider keys are optional and can be added later.)"))

            owner = resolve_install_owner(project_dir)
            # external mode brings its own certificate: no local CA
            external = "external" in opts["init_args"] or (opts["resume"] and self._domain_mode() == "external")
            caroot = None if external else install_user_caroot()
            caroot_env = {"CAROOT": caroot} if caroot else None

            # [1/6] preflight
            step = 1
            self._install_step(1, "Checking this host")
            for w in self._install_preflight(project_dir, opts["resume"]):
                print(f"  {yellow('!')} {w}")
            print(f"  {green('ok')}")

            # [2/6] configuration
            step = 2
            self._install_step(2, "Generating configuration")
            if opts["resume"]:
                self._install_check_init_outputs(project_dir, resume_cmd)
                print(f"  {green('ok')} (existing configuration kept)")
            else:
                touched = True
                try:
                    rc = subprocess.call(["bash", os.path.join(project_dir, "scripts", "init.sh"), "--yes"] + opts["init_args"],
                                         cwd=project_dir, env=self._install_child_env(caroot_env))
                finally:
                    fix_caroot_ownership(caroot, owner)
                if rc != 0:
                    resume = os.path.exists(env_path)
                    raise InstallAbort(clamp_script_rc(rc), 2, f"init.sh failed (exit {rc})",
                                       "fix the error above, then " + resume_cmd if resume
                                       else "fix the error above, then sudo ./voipbin install",
                                       why="see the VOIPBIN_INIT line above")
                if (read_env_file_var(project_dir, "HOST_EXTERNAL_IP") or "") in ("", "127.0.0.1"):
                    print(f"  {yellow('!')} host IP could not be detected (HOST_EXTERNAL_IP=127.0.0.1); "
                          "SIP clients on other machines will not reach this host")
                print(f"  {green('ok')}")
            touched = True

            # [3/6] host
            step = 3
            self._install_step(3, "Preparing the host (DNS, network, certificates)")
            self._install_host_notice()
            if not self._install_ask(interactive, "Apply these host changes?"):
                raise InstallAbort(1, 3, "host changes not approved", resume_cmd)
            rc = subprocess.call(["bash", os.path.join(project_dir, "scripts", "setup-host.sh")],
                                 cwd=project_dir, env=self._install_child_env())
            if rc != 0:
                raise InstallAbort(clamp_script_rc(rc), 3, f"setup-host.sh failed (exit {rc})",
                                   "fix the error above, then " + resume_cmd,
                                   why="see the VOIPBIN_SETUP_HOST line above")
            print(f"  {green('ok')}")

            # [4/6] start
            step = 4
            services = subprocess.run(["docker", "compose", "config", "--services"], cwd=project_dir,
                                      capture_output=True, text=True).stdout.split()
            self._install_step(4, f"Starting services ({len(services)} services, this can take several minutes)")
            try:
                rc = subprocess.call(["bash", os.path.join(project_dir, "scripts", "start.sh")],
                                     cwd=project_dir, env=self._install_child_env(caroot_env))
            finally:
                fix_caroot_ownership(caroot, owner)
            if rc != 0:
                raise InstallAbort(clamp_script_rc(rc), 4, f"start.sh failed (exit {rc})",
                                   "fix the error above, then " + resume_cmd,
                                   why="see the VOIPBIN_START line above")
            print(f"  {green('ok')}")

            # [5/6] verify
            step = 5
            self._install_step(5, "Verifying the install (waits for health checks, up to 3 minutes)")
            ok, summary, optional, failures = self._install_wait_and_verify(project_dir)
            for name in optional:
                print(f"  {yellow('!')} {name}: optional feature not configured "
                      "(add TTS provider credentials to .env, then 'sudo ./voipbin update keys')")
            if not ok:
                for name, why in failures:
                    print(f"  {red('x')} {name}: {why}")
                raise InstallAbort(1, 5, f"{len(failures)} service problem(s): " + ", ".join(n for n, _ in failures),
                                   "sudo ./voipbin doctor, fix, then " + resume_cmd,
                                   why="a service is not running or not healthy")
            print(f"  {green('ok')} ({summary})")
            if not (self._provider_preflight_step(project_dir, "install")
                    and self._provider_import_step(project_dir, "install")):
                raise InstallAbort(1, 5, "provider keys could not be stored", resume_cmd,
                                   why="see the provider key lines above; the services read these keys from the database only")

            # [6/6] admin account
            step = 6
            self._install_step(6, "Creating the admin account")
            email = opts["admin_email"]
            while not email:
                try:
                    email = input("  email: ").strip()
                except EOFError:
                    raise InstallAbort(1, 6, "no admin email given", resume_cmd)
                if email and not INSTALL_EMAIL_RE.match(email):
                    print(f"  {red('x')} not a valid email address")
                    email = ""
            password = secrets.token_urlsafe(18)
            if pw_fd is not None:
                os.write(pw_fd, (password + "\n").encode())
                os.fsync(pw_fd)
            rc, line = self._install_create_admin(project_dir, email, opts["admin_name"], password, opts["reset"])
            if rc == 3:
                if interactive and self._install_ask(True, f"An account for {email} already exists. Set a new password for it?", default_yes=False):
                    rc, line = self._install_create_admin(project_dir, email, opts["admin_name"], password, True)
                else:
                    raise InstallAbort(1, 6, f"an account for {email} already exists",
                                       resume_cmd + " --reset-admin-password")
            if rc != 0:
                m = re.search(r'reason="([^"]*)"', line)
                raise InstallAbort(1, 6, "admin account setup failed: " + (m.group(1) if m else (line or f"exit {rc}")),
                                   resume_cmd + " (add --reset-admin-password if the account exists)",
                                   why="see the [admin] lines above")
            pw_done = True
            if pw_fd is not None:
                os.close(pw_fd)
                pw_fd = None
            write_install_marker(marker, email)

            url = self._install_admin_console_url()
            print()
            print(f"  {bold('VoIPBin is running.')}")
            print(f"  Admin console   {blue(url)}")
            if pw_path:
                print(f"  Sign in         {email} / password in {pw_path}")
            else:
                print(f"  Sign in         {email} / (shown once below)")
            print(f"  Next steps      add AI provider keys (optional): edit .env, then 'sudo ./voipbin update keys'")
            print(f"                  check health anytime: sudo ./voipbin doctor")
            print(f"                  undo host changes: sudo ./voipbin clean --dns / --network")
            if optional:
                print(f"  TTS             add provider credentials to .env, then 'sudo ./voipbin update keys'")
            if not pw_path:
                print()
                if not interactive:
                    print(yellow("  WARNING: the password below is printed once; it ends up in any log that captures this output."))
                print(f"  Admin password  {password}")
            print()
            self._install_result_line(0, state="installed", admin=email, url=url)
        except InstallAbort as e:
            self.last_rc = e.rc
            print()
            if e.quiet:
                print("  Nothing was changed. Type 'install' (or run 'sudo ./voipbin install') to start it later.")
            else:
                print(f"  {red('What failed:')} {e.reason}")
                if e.why:
                    print(f"  {red('Why:')}         {e.why}")
                if e.next_cmd:
                    print(f"  {red('Next:')}        {e.next_cmd}")
            self._install_result_line(e.rc, step=e.step or step, reason=e.reason, next_cmd=e.next_cmd)
        except KeyboardInterrupt:
            self.last_rc = RC_INTERRUPTED
            print()
            self._install_result_line(RC_INTERRUPTED, step=step, reason="interrupted",
                                      next_cmd=resume_cmd if os.path.exists(env_path) else "sudo ./voipbin install")
        except Exception as e:
            self.last_rc = RC_UNEXPECTED
            traceback.print_exc(file=sys.stderr)
            self._install_result_line(RC_UNEXPECTED, step=step, reason=f"unexpected error: {type(e).__name__}: {e}",
                                      next_cmd="sudo ./voipbin doctor")
        finally:
            if pw_fd is not None:
                os.close(pw_fd)
            if pw_path and not pw_done and os.path.exists(pw_path):
                try:
                    os.remove(pw_path)
                except OSError:
                    pass
            if touched:
                fix_install_ownership(project_dir)

    def cmd_init(self, args):
        """Generate configuration (.env, certificates) via scripts/init.sh"""
        script_dir = self.config.get("project_dir", ".")
        script_path = os.path.join(script_dir, "scripts", "init.sh")

        if not os.path.exists(script_path):
            print(red(f"Init script not found: {script_path}"))
            self.last_rc = 2
            return

        print("Running initialization script...")
        print("This will generate .env and certificates.\n")
        # Arguments go through to init.sh (--yes, --mode, --domain, ...);
        # the exit code is kept for _main() to propagate.
        self.last_rc = subprocess.call(["bash", script_path] + list(args), cwd=script_dir)
        fix_install_ownership(script_dir)

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
        """Remove containers, data or generated files"""
        self.last_rc = 0
        if not [a for a in args if a != "--force"]:
            print("Usage: clean [--containers] [--volumes] [--images] [--network] [--dns] [--purge] [--all] [--force]")
            print("")
            print("Options:")
            print("  --containers  Remove app containers (keeps infrastructure: db, redis, mq, dns)")
            print("  --volumes     Remove docker volumes (database, recordings, redis/rabbitmq state)")
            print("  --images      Remove docker images")
            print("  --network     Teardown VoIP network interfaces")
            print("  --dns         Remove DNS configuration")
            print("  --purge       Remove generated files (.env, certs, configs, docker-compose.yml, versions.lock)")
            print("  --all         All of the above (full reset)")
            print("  --force       Do not ask for confirmation (required when not run from a terminal)")
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

        # Data-destroying options list their targets and need an explicit
        # 'yes' (or --force) before anything runs.
        if clean_volumes or clean_images or purge:
            print(bold("This will permanently remove:"))
            if clean_volumes:
                print("  - all containers and docker volumes: database, recordings, redis and rabbitmq state")
            if clean_images:
                print("  - the docker images of this stack")
            if purge:
                print("  - generated files: .env (passwords and encryption keys), certs/, config/coredns,")
                print("    docker-compose.yml, versions.lock, version pins and the install marker")
            if teardown_network:
                print("  - the VoIP network interfaces")
            if teardown_dns:
                print("  - the DNS configuration (resolv.conf is restored)")
            print("  backups/ is not touched.")
            if purge and not clean_volumes:
                print(yellow("  WARNING: the database volume stays, but its passwords and encryption keys are in .env."))
                print(yellow("           Keep a copy of .env (or use --volumes too), or the data cannot be used again."))
            if "--force" not in args:
                if not sys.stdin.isatty():
                    print(red("Refusing to delete without confirmation: re-run with --force (non-interactive)."))
                    self.last_rc = 1
                    return
                try:
                    answer = input("Type 'yes' to continue: ").strip()
                except EOFError:
                    answer = ""
                if answer != "yes":
                    print("Aborted. Nothing was removed.")
                    self.last_rc = 1
                    return

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
            # Same for the guided-install marker (it records the admin
            # account, which lives in the database).
            install_marker = os.path.join(project_dir, INSTALL_MARKER)
            if os.path.exists(install_marker):
                os.remove(install_marker)
                print("  Removed install marker")
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
            # Get the stack's third-party images
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
                # Same set as scripts/clean.sh --purge: the live copies are
                # re-created from the .dist files by the next init.
                ("docker-compose.yml", "docker-compose.yml"),
                ("docker-compose.yml.flock", "docker-compose.yml lock file"),
                ("docker-compose.yml.dist.flock", "docker-compose.yml.dist lock file"),
                ("versions.lock", "versions.lock"),
                ("versions.lock.flock", "versions.lock lock file"),
                ("versions.lock.dist.flock", "versions.lock.dist lock file"),
                (INSTALL_MARKER, "install marker"),
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
        if clean_volumes and not teardown_network and any(os.path.exists(f"/sys/class/net/{n}") for n in INSTALL_VOIP_INTERFACES):
            # The compose network is recreated on the next start, but the kept
            # VoIP interfaces would then shadow its route and cut container
            # traffic (DNS lookups and image downloads time out).
            print("The VoIP network interfaces were kept. Before installing again, remove them:")
            print("  sudo ./voipbin clean --network --force")
        print("Run 'install' to set up again (or 'init', then 'start').")

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
        """Update the install - pull images and/or update scripts from GitHub"""
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

        # Exit code of this command (see the table in `update --help` / README).
        # main() turns a non-zero value into the process exit status.
        self.last_update_rc = 0
        self._update_scripts_rc = RC_SCRIPTS_FAILED
        self._update_stage = "init"
        self._verify_rc = 0
        self._verify_bad = 0
        self._verify_ping = "skipped"
        self._keys_status = "none"

        if resume_from == "pull":
            # "Did the just-pulled release unpin this product" must be
            # detected from versions.lock.dist (the file `git pull` can
            # actually change), NOT the live `versions.lock` used for
            # `pinned` above - the live file is untracked/operator-owned
            # after the versions.lock.dist split and can never be touched
            # by a pull either way, so checking it here would never fire.
            dist_still_pinned = os.path.exists(os.path.join(project_dir, "versions.lock.dist"))
            if not dist_still_pinned:
                # The pulled commit REMOVED versions.lock.dist (unpin release).
                # Falling through to the unpinned path silently would skip
                # migrations - tell the operator what happened instead.
                print(f"{yellow('!')} Resumed after git pull, but versions.lock.dist is GONE in the new")
                print("  commit (the release unpinned this repo). The pinned upgrade flow does")
                print("  not apply anymore - run 'update all' again to use the standard path,")
                print("  and run scripts/migrate.sh manually if the release requires migrations.")
                # This early return sits before the atexit registration in
                # _upgrade_pinned, so release the op lock held across the
                # re-exec here instead of leaking it.
                self._release_op_lock(os.path.join(project_dir, ".voipbin-op.lock"))
                self.last_update_rc = RC_UNPINNED
                self._update_stage = "resume"
                self._print_update_result()
                return

        if subcommand == "keys":
            self._update_stage = "keys"
            if check_only:
                if self._provider_preflight_step(project_dir, "keys"):
                    self._provider_import_step(project_dir, "keys", check_only=True)
                    self._keys_status = "none"
                    self.last_update_rc = 0
                else:
                    self._update_stage = "keys-preflight"
                    self.last_update_rc = RC_KEYS_FAILED
            else:
                lock = self._acquire_op_lock(project_dir, "update keys")
                if not lock:
                    self._update_stage = "lock"
                    self.last_update_rc = RC_PRECONDITION
                else:
                    try:
                        print(f"\n{bold('Provider keys')}")
                        ok = (self._provider_preflight_step(project_dir, "keys")
                              and self._provider_import_step(project_dir, "keys"))
                        if self._keys_status == "preflight":
                            self._update_stage = "keys-preflight"
                        self.last_update_rc = 0 if ok else RC_KEYS_FAILED
                    finally:
                        self._release_op_lock(lock)
            self._print_update_result()
        elif subcommand == "scripts":
            if not self._update_scripts(project_dir, check_only):
                self.last_update_rc = self._update_scripts_rc or RC_SCRIPTS_FAILED
        elif subcommand == "all":
            if pinned:
                # Pinned repo: safe-ordered upgrade flow (design §3.5).
                rc = self._upgrade_pinned(
                    project_dir,
                    check_only=check_only,
                    skip_backup=skip_backup,
                    resume_from=resume_from,
                    backup_ts=backup_ts,
                )
                self.last_update_rc = rc if isinstance(rc, int) else 0
                if check_only:
                    self._update_stage = "check"
            else:
                self._update_stage = "scripts"
                if not self._update_scripts(project_dir, check_only):
                    # A failed script update must not be followed by an image
                    # update against stale scripts.
                    self.last_update_rc = self._update_scripts_rc or RC_SCRIPTS_FAILED
                else:
                    if not check_only:
                        print("")
                    self._update_stage = "images"
                    self._update_images(project_dir, check_only)
            self._print_update_result()
        elif subcommand in ("", "images"):
            self._update_images(project_dir, check_only)
        else:
            print(f"{red('Unknown subcommand:')} {subcommand}")
            print("Usage: update [scripts|images|keys|all] [--check] [--skip-backup]")
            print("Exit codes of 'update all' are listed in the README (upgrade section).")
            self.last_update_rc = RC_USAGE

    # -------------------------------------------------------------------------
    # Provider keys: copy the secret values the containers used to receive from
    # .env into the stored configs of ai/email/message/transcribe/tts-manager
    # -------------------------------------------------------------------------

    _keys_status = "none"

    def _provider_compose(self, project_dir):
        """({service: {variable: value}}, error). The values come from
        `docker compose config`, i.e. exactly what the container receives
        (.env, quotes and $ already resolved). They stay in memory; compose's
        stderr is dropped because it can quote pieces of a value."""
        try:
            r = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=project_dir,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"docker compose config could not run ({type(e).__name__})"
        if r.returncode != 0:
            return None, f"docker compose config failed (exit {r.returncode})"
        if not (r.stdout or "").strip():
            return None, "docker compose config printed nothing"
        try:
            data = json.loads(r.stdout)
        except ValueError:
            return None, "docker compose config did not print JSON"
        services = provider_compose_services(data)
        if services is None:
            return None, "docker compose config has no services"
        return services, None

    def _provider_run(self, project_dir, svc, ctl, args, value=None, tag="keys", timeout=PROVIDER_CALL_TIMEOUT):
        """Run /app/bin/<ctl> <args> from the NEW image of the service in a
        throwaway container (no start of the service, no dependencies). The
        value, if any, goes to stdin only. Returns (rc, stdout, stderr)."""
        name = f"voipbin-{tag}-{svc}"
        devnull = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=project_dir)

        def drop():
            try:
                subprocess.run(["docker", "rm", "-f", name], timeout=30, **devnull)
            except (OSError, subprocess.TimeoutExpired):
                pass

        drop()
        cmd = ["docker", "compose", "run", "--rm", "-T", "--no-deps", "--name", name,
               "--entrypoint", f"/app/bin/{ctl}", svc] + list(args)
        kw = dict(cwd=project_dir, capture_output=True, text=True, errors="replace", timeout=max(1, timeout))
        if value is None:
            kw["stdin"] = subprocess.DEVNULL
        else:
            kw["input"] = value + "\n"
        try:
            r = subprocess.run(cmd, **kw)
        except subprocess.TimeoutExpired:
            drop()
            return 124, "", "timed out"
        except OSError as e:
            return 127, "", f"docker could not run ({type(e).__name__})"
        return r.returncode, r.stdout or "", r.stderr or ""

    def _provider_preflight_step(self, project_dir, mode):
        """True when every service that has keys to store gets its master key.

        mode: update (before any image changes), install, keys. A compose that
        cannot be read is not a verdict here; the import step reports it."""
        self._keys_status = "none"
        try:
            return self._provider_preflight(project_dir, mode)
        except Exception as e:
            print(f"  {yellow('!')} Provider keys: the pre-flight check failed ({type(e).__name__}); the key step will report it.")
            return True

    def _provider_preflight(self, project_dir, mode):
        compose, err = self._provider_compose(project_dir)
        if compose is None:
            print(f"  {yellow('!')} Provider keys: cannot check the compose file ({err}); the key step will report it.")
            return True
        plan, _notes = provider_plan(compose)
        problems = []
        for svc, ents in plan.items():
            master = ents[0]["master"]
            if not compose.get(svc, {}).get(master):
                in_env = bool(read_env_file_var(project_dir, master))
                problems.append((svc, master, in_env))
        nstore, _nadvice, _ninvalid = number_plan(compose)
        if nstore and not compose.get(NUMBER_SVC, {}).get(NUMBER_MASTER):
            problems.append((NUMBER_SVC, NUMBER_MASTER, bool(read_env_file_var(project_dir, NUMBER_MASTER))))
        if not problems:
            return True
        self._keys_status = "preflight"
        print(f"\n{red('Provider keys cannot be stored yet:')}")
        for svc, master, in_env in problems:
            why = ("docker-compose.yml does not pass it to the service" if in_env
                   else "it is empty or missing in .env")
            print(f"  - {svc}: {master}: {why}")
        print("  The services read their provider keys from the database only, and the master key")
        print("  protects what is stored there.")
        if mode == "update":
            print("  The scripts were updated, but no image was changed and the running containers are untouched.")
        fix = {"update": "sudo ./voipbin update all", "install": "sudo ./voipbin install --resume",
               "keys": "sudo ./voipbin update keys"}.get(mode, "sudo ./voipbin update keys")
        print("  Fix it, then run the command again (safe to repeat):")
        print("    1. docker-compose.yml: copy the encryption key lines (*_CONFIG_ENCRYPTION_KEYS, NUMBER_SECRET_ENCRYPTION_KEYS) from docker-compose.yml.dist")
        print("    2. .env: add the missing keys with the loop in README.md (\"Adopting the encryption keys\")")
        print("       Do not run init.sh --yes or --force-reinit: they regenerate the database passwords.")
        print(f"    3. {fix}")
        return False

    def _provider_import_step(self, project_dir, mode, check_only=False):
        """Store the provider keys that have a value and no stored value yet.

        mode: update (before `up -d`), install, keys, unpinned. Returns True
        when nothing blocks. A key that is already stored is never touched
        (the database is the truth after the first store); a row that is
        unreadable, or half of a pair, is reported with the command to fix it
        and left alone. Values only travel on stdin and are never printed."""
        self._keys_status = "none"
        try:
            return self._provider_import(project_dir, mode, check_only)
        except Exception as e:  # never let an unexpected error print a traceback with a value in it
            self._keys_status = "failed"
            print(f"  {red('x')} Provider keys: unexpected error ({type(e).__name__}); nothing more was stored.")
            print("    Run 'sudo ./voipbin update keys' after fixing the cause.")
            return False

    def _provider_import(self, project_dir, mode, check_only):
        compose, err = self._provider_compose(project_dir)
        if compose is None:
            self._keys_status = "failed"
            print(f"  {red('x')} Provider keys: {err}")
            print("    Fix docker-compose.yml / .env (docker compose config must work), then run again.")
            return False
        plan, notes = provider_plan(compose)
        nstore, nadvice, ninvalid = number_plan(compose)
        notes = list(notes) + ninvalid
        res = dict(stored=[], already=[], skipped=[], unsupported=[], invalid=[], attention=[], failed=[], half=[])
        for kind, svc, key, why in notes:
            res[kind].append((svc, key, why))
        values = {}
        for svc, ents in plan.items():
            values[svc] = [e["value"] for e in ents]
        if not plan and not notes and not nstore and not nadvice:
            print("  No provider keys to store (none of the provider variables in .env has a value).")
            return True
        if check_only:
            for svc, ents in plan.items():
                print(f"  {svc}: would store {', '.join(e['key'] for e in ents)} (when not stored yet)")
            if nstore:
                print(f"  {NUMBER_SVC}: would store telnyx (when not stored yet)")
            for kind, svc, key, why in notes:
                print(f"  {yellow('!')} {svc}: {key}: {why}")
            for a in nadvice:
                print(f"  {yellow('!')} {NUMBER_SVC}: {a}: {NUMBER_ADVICE[a]} (if not stored yet)")
            return True

        deadline = time.monotonic() + PROVIDER_TOTAL_BUDGET
        for svc, ents in plan.items():
            ctl = ents[0]["ctl"]
            vals = values[svc]
            keys = [e["key"] for e in ents]
            if time.monotonic() > deadline:
                res["failed"] += [(svc, k, "time budget exhausted") for k in keys]
                continue
            def left():
                return int(deadline - time.monotonic())

            rc, _out, errtxt = self._provider_run(project_dir, svc, ctl, ["migrate", "up"],
                                                  timeout=min(PROVIDER_CALL_TIMEOUT, left()))
            verdict = provider_classify(rc, errtxt, ctl)
            if verdict == "ok":
                rc, out, errtxt = self._provider_run(project_dir, svc, ctl, ["config", "list"],
                                                     timeout=min(PROVIDER_CALL_TIMEOUT, left()))
                verdict = provider_classify(rc, errtxt, ctl)
            if verdict == "unsupported":
                res["unsupported"] += [(svc, k, "image has no stored config commands (old image)") for k in keys]
                continue
            if verdict == "failed":
                why = provider_last_line(errtxt, vals) or f"exit {rc}"
                res["failed"] += [(svc, k, why) for k in keys]
                continue
            parsed = provider_parse_list(out)
            if parsed is None:
                res["failed"] += [(svc, k, "config list did not print the expected JSON") for k in keys]
                continue
            sources = parsed[0]
            groups = {}
            for e in ents:
                groups.setdefault(e["group"] or e["key"], []).append(e)
            for members in groups.values():
                srcs = [sources.get(m["key"]) for m in members]
                if any(x is None for x in srcs):
                    res["skipped"] += [(svc, m["key"], "this image does not list the key") for m in members]
                elif all(x == PROVIDER_SOURCE_STORED for x in srcs):
                    res["already"] += [(svc, m["key"], "") for m in members]
                elif all(x in PROVIDER_SOURCES_EMPTY for x in srcs):
                    for m in members:
                        rc, _o, errtxt = self._provider_run(project_dir, svc, ctl,
                                                            ["config", "set", "--key", m["key"], "--actor-id", PROVIDER_ACTOR_ID],
                                                            value=m["value"], timeout=min(PROVIDER_CALL_TIMEOUT, left()))
                        if rc == 0:
                            res["stored"].append((svc, m["key"], ""))
                        else:
                            why = provider_last_line(errtxt, vals) or f"exit {rc}"
                            res["failed"].append((svc, m["key"], why))
                            rest = members[members.index(m) + 1:]
                            res["failed"] += [(svc, r_["key"], "not attempted after the failure above") for r_ in rest]
                            res["half"] += [(svc, x_[1], "") for x_ in res["stored"]
                                            if x_[0] == svc and x_[1] in [mm["key"] for mm in members]]
                            break
                else:
                    state = ", ".join(f"{m['key']}={x}" for m, x in zip(members, srcs))
                    res["attention"] += [(svc, m["key"], f"stored state is not usable: {state}") for m in members]

        if nstore or nadvice:
            self._provider_import_number(project_dir, nstore, nadvice, res, deadline)

        self._provider_report(res, mode)
        if res["failed"]:
            self._keys_status = "failed"
        elif res["attention"] or res["invalid"]:
            self._keys_status = "attention"
        elif res["unsupported"]:
            self._keys_status = "skipped"
        else:
            self._keys_status = "ok"
        if res["failed"]:
            return False
        if mode == "keys" and (res["attention"] or res["invalid"] or res["unsupported"]):
            return False
        return True

    def _provider_import_number(self, project_dir, store, advice, res, deadline):
        """number-manager: store the Telnyx token and connection id of .env in the
        `telnyx` provider row when that provider has no stored secret yet.

        The order is fixed by the service: the config first (the row is created
        with its defaults), then the secret, which is refused without the
        required config. Only the non-secret config goes on argv; the token goes
        on stdin. A provider that already has a stored secret is never touched,
        and a row whose config differs from .env is left alone. `advice` names
        providers .env cannot store: they are printed unless stored, and when
        there is nothing to store a failure to ask only says "not verified"."""
        svc, ctl, key = NUMBER_SVC, NUMBER_CTL, "telnyx"
        secrets = ([store["secret"]] + list(store["config"].values())) if store else []

        def left():
            return int(deadline - time.monotonic())

        def advise(rows):
            for a in advice:
                if rows is None:
                    res["skipped"].append((svc, a, NUMBER_ADVICE[a] + " (not verified against the database)"))
                elif not number_row_stored(rows.get(a)):
                    res["skipped"].append((svc, a, NUMBER_ADVICE[a]))

        if time.monotonic() > deadline:
            if store:
                res["failed"].append((svc, key, "time budget exhausted"))
            else:
                advise(None)
            return
        rc, _out, errtxt = self._provider_run(project_dir, svc, ctl, ["migrate", "up"],
                                              timeout=min(PROVIDER_CALL_TIMEOUT, left()))
        verdict = provider_classify(rc, errtxt, ctl)
        out = ""
        if verdict == "ok":
            rc, out, errtxt = self._provider_run(project_dir, svc, ctl, ["provider", "list"],
                                                 timeout=min(PROVIDER_CALL_TIMEOUT, max(1, left())))
            verdict = provider_classify(rc, errtxt, ctl)
        rows = provider_parse_number_list(out) if verdict == "ok" else None
        if not store:
            advise(rows)
            return
        if verdict == "unsupported":
            res["unsupported"].append((svc, key, "image has no provider commands (old image)"))
            advise(None)
            return
        if verdict == "failed":
            res["failed"].append((svc, key, provider_last_line(errtxt, secrets) or f"exit {rc}"))
            return
        if rows is None:
            res["failed"].append((svc, key, "provider list did not print the expected JSON"))
            return
        advise(rows)
        row = rows.get(key)
        if row is None:
            res["skipped"].append((svc, key, "this image does not list the provider"))
        elif number_row_stored(row):
            if row["enabled"] and not row["usable"]:
                res["attention"].append((svc, key, "stored, but not usable (is a required config key missing?)"))
            else:
                res["already"].append((svc, key, ""))
        elif row["source"] not in ("none",):
            res["attention"].append((svc, key, f"stored state is not usable: credential_source={row['source']}"))
        else:
            differ = sorted(k for k, v in store["config"].items() if row["config"].get(k) and row["config"][k] != v)
            if differ:
                res["attention"].append((svc, key, "the row already has a different " + ", ".join(differ) +
                                         " than .env; the .env token was not paired with it"))
                return
            send = {k: v for k, v in store["config"].items() if not row["config"].get(k)}
            if send:
                args = ["provider", "update", "--name", key, "--actor-id", PROVIDER_ACTOR_ID]
                for k, v in send.items():
                    args += ["--config", f"{k}={v}"]
                rc, _o, errtxt = self._provider_run(project_dir, svc, ctl, args,
                                                    timeout=min(PROVIDER_CALL_TIMEOUT, max(1, left())))
                if rc != 0:
                    res["failed"].append((svc, key, provider_last_line(errtxt, secrets) or f"exit {rc}"))
                    return
            rc, _o, errtxt = self._provider_run(project_dir, svc, ctl,
                                                ["provider", "secret", "set", "--name", key, "--actor-id", PROVIDER_ACTOR_ID],
                                                value=store["secret"], timeout=min(PROVIDER_CALL_TIMEOUT, max(1, left())))
            if rc == 0:
                res["stored"].append((svc, key, ""))
            else:
                why = provider_last_line(errtxt, secrets) or f"exit {rc}"
                if send:
                    why = "the config was stored, the secret was not; run 'sudo ./voipbin update keys' again (safe to repeat): " + why
                res["failed"].append((svc, key, why))

    def _provider_report(self, res, mode):
        icons = dict(stored=green("\u2713"), already=green("\u2713"), skipped=yellow("!"), unsupported=yellow("!"),
                     invalid=yellow("!"), attention=yellow("!"), failed=red("x"))
        words = dict(stored="stored", already="already stored, left as is", skipped="skipped", unsupported="skipped",
                     invalid="not stored", attention="needs your attention", failed="FAILED")
        for kind in ("stored", "already", "skipped", "unsupported", "invalid", "attention", "failed"):
            for svc, key, why in res[kind]:
                print(f"  {icons[kind]} {svc}: {key} {words[kind]}" + (f" ({why})" if why else ""))
        print(f"  Provider keys: stored {len(res['stored'])}, already stored {len(res['already'])}, "
              f"skipped {len(res['skipped']) + len(res['unsupported'])}, "
              f"attention {len(res['attention']) + len(res['invalid'])}, failed {len(res['failed'])}")
        ctl_of = {(sv, k): c for sv, c, _m, _n, _e, k, _g in PROVIDER_CONFIG_KEYS}

        def replace_cmds(items):
            print("  Replace it with the new value in NEW_VALUE (keep the value out of your shell history):")
            for svc, key, why in items:
                if svc == NUMBER_SVC:
                    run = f"docker compose run --rm -T --no-deps --entrypoint /app/bin/{NUMBER_CTL} {svc} provider"
                    if "different" in why or "not usable" in why:
                        print(f"    {run} update --name {key} --actor-id {PROVIDER_ACTOR_ID} --config connection_id=<CONNECTION_ID>")
                        print("    (check first that the token belongs to the Telnyx account of that connection id)")
                    print(f"    printf '%s' \"$NEW_VALUE\" | {run} secret set --name {key} --actor-id {PROVIDER_ACTOR_ID}")
                    continue
                print(f"    printf '%s' \"$NEW_VALUE\" | docker compose run --rm -T --no-deps --entrypoint "
                      f"/app/bin/{ctl_of.get((svc, key), '<control>')} {svc} config set --key {key} --actor-id {PROVIDER_ACTOR_ID}")

        if res["attention"]:
            print("  A stored value that cannot be used, or that does not match .env, is never overwritten automatically.")
            replace_cmds(res["attention"])
        if res["invalid"]:
            print("  A value in .env that cannot be stored is skipped; fix or empty it in .env and run 'sudo ./voipbin update keys'.")
        if res["unsupported"]:
            print("  The image of a skipped key has no stored configs yet, so that key was not stored.")
            if mode == "keys":
                print("  Update the image pin ('sudo ./voipbin update all') and run 'sudo ./voipbin update keys' again.")
            else:
                print("  Once those services run an image with stored configs, run 'sudo ./voipbin update keys'.")
        if res["half"]:
            print("  Only part of an AWS pair was stored. The next run reports the pair as needing attention and")
            print("  leaves it alone; store both keys of the pair with these commands:")
            pairs = [(sv, k, "") for sv, k in {(sv, k) for sv, k, _w in res["half"] + res["failed"]
                                               if k.startswith("aws.")}]
            replace_cmds(sorted(pairs))
        if res["failed"]:
            print("  Nothing was lost: keys already stored stay stored. Fix the cause and run the command again.")
        if any(x[0] != NUMBER_SVC for x in res["stored"] + res["already"]):
            print("  From now on the database holds these keys: rotate one with '<service>-control config set' or the")
            print("  admin API; a change in .env alone has no effect, and a key cleared in the database comes back")
            print("  on the next run while .env still has a value. OPENAI_API_KEY, GOOGLE_API_KEY and ELEVENLABS_API_KEY are")
            print("  stored separately for pipecat-manager: rotate them in each service that holds them.")
        if any(x[0] == NUMBER_SVC for x in res["stored"] + res["already"]):
            print("  number-manager now holds the Telnyx account in the database: rotate the token with")
            print("  'number-control provider secret set --name telnyx' (value on stdin) or the admin API; a change in .env")
            print("  alone has no effect. message-manager stores TELNYX_API_KEY separately, so rotate it there too.")
        if any(x[0] == NUMBER_SVC and x[1] == "twilio" for x in res["skipped"]):
            run = f"docker compose run --rm -T --no-deps --entrypoint /app/bin/{NUMBER_CTL} {NUMBER_SVC} provider"
            print("  To use Twilio for phone numbers, enter its account and trunk, then the account auth token (value on stdin):")
            print(f"    {run} update --name twilio --actor-id {PROVIDER_ACTOR_ID} --config sid=<ACCOUNT_SID> --config trunk_sid=<TRUNK_SID>")
            print(f"    printf '%s' \"$AUTH_TOKEN\" | {run} secret set --name twilio --actor-id {PROVIDER_ACTOR_ID}")
            print("  Twilio stays disabled until you enable it in the admin API.")

    def _provider_keys_unpinned(self, project_dir):
        """Key step of the unpinned `update` path (after `up -d`). The new images
        read provider keys from their stored configs only. This path has no
        pre-flight and no exit code, so a failure is a loud warning, not an abort."""
        print(f"\n{blue('==>')} Storing provider keys from .env...")
        if not (self._provider_preflight_step(project_dir, "keys")
                and self._provider_import_step(project_dir, "unpinned")):
            print(f"\n{red('WARNING:')} the provider keys are NOT stored: the updated services cannot see them.")
            print("  Fix the cause above, then run 'sudo ./voipbin update keys'.")

    def _provider_stored(self, project_dir):
        """({(service, key)} stored and readable, {service} that could not be asked).

        For `status`: only asks when the database container runs, one throwaway
        control call per service, in parallel; any failure marks the service
        as down so the row falls back to .env."""
        services = sorted({r[0] for r in PROVIDER_CONFIG_KEYS}) + [NUMBER_SVC]
        db = self.config.get("db_container", "voipbin-db")
        try:
            up = shutil.which("docker") and subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", db], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=15).stdout.strip() == "true"
        except (OSError, subprocess.TimeoutExpired):
            up = False
        if not up:
            return set(), set(services)
        ctl_of = {r[0]: r[1] for r in PROVIDER_CONFIG_KEYS}
        ctl_of[NUMBER_SVC] = NUMBER_CTL

        def ask(svc):
            if svc == NUMBER_SVC:
                rc, out, err = self._provider_run(project_dir, svc, NUMBER_CTL, ["provider", "list"], tag="keys-status",
                                                  timeout=PROVIDER_STATUS_TIMEOUT)
                rows = provider_parse_number_list(out) if rc == 0 else None
                if rows is None:
                    return svc, None
                held = {k: True for k, r in rows.items() if number_row_stored(r)}
                return svc, ({k: PROVIDER_SOURCE_STORED for k in held}, held)
            rc, out, err = self._provider_run(project_dir, svc, ctl_of[svc], ["config", "list"], tag="keys-status",
                                              timeout=PROVIDER_STATUS_TIMEOUT)
            parsed = provider_parse_list(out) if rc == 0 else None
            return svc, parsed

        stored, down = set(), set()
        with ThreadPoolExecutor(max_workers=len(services)) as ex:
            for svc, parsed in ex.map(ask, services):
                if parsed is None:
                    down.add(svc)
                    continue
                for key, src in parsed[0].items():
                    if src == PROVIDER_SOURCE_STORED and parsed[1].get(key):
                        stored.add((svc, key))
        return stored, down

    def _print_update_result(self):
        """One machine-readable line for scripted callers; no secrets."""
        print(f"UPDATE_RESULT rc={self.last_update_rc} stage={self._update_stage} "
              f"containers_bad={self._verify_bad} ping={self._verify_ping} keys={self._keys_status}")

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
            print("  2. update scripts          - git pull (new docker-compose.yml.dist + versions.lock.dist + scripts)")
            print("     (the CLI then re-execs itself so steps 2b-6 run the NEW code)")
            print("  2b. sync compose digests   - scripts/sync-compose-images.sh reconciles the LIVE")
            print("     docker-compose.yml's image: lines against the new versions.lock.dist (both live")
            print("     files are untracked - git pull alone never touches them, see each .dist file's")
            print("     header comment/\"_comment\" field)")
            print("  3. docker compose pull     - fetch the new pinned digests")
            print("  4. scripts/migrate.sh      - database migrations at the pinned schema revision (abort on failure)")
            print("  4b. provider keys          - store the provider keys of .env that are not stored yet into the")
            print("     services' stored configs and the Telnyx account into number-manager (new images, before they")
            print("     start; abort on failure, nothing lost)")
            print("     (before step 2b the live docker-compose.yml is checked for the master keys it needs)")
            print("  Exit codes: 0 ok, 2 busy, 10 backup, 11 scripts, 12 unpinned, 13 sync, 14 pull,")
            print("              15 migrate, 16 up, 17-19 verify (containers / ping / both), 20 provider keys")
            print("  5. docker compose up -d    - recreate only containers whose digest changed")
            print("  6. verify                  - poll container health (120s) + GET api-manager /ping")
            print(f"\n{yellow('No changes made.')} Run 'update all' without --check to upgrade.")
            return 0

        if resume_from != "pull":
            # Concurrency lock: held from here THROUGH the execv (the lock file
            # survives the process image swap) and released at the end of the
            # resumed run. Blocks concurrent backup / second update all.
            lock = self._acquire_op_lock(project_dir, "update all")
            if not lock:
                self._update_stage = "lock"
                return RC_PRECONDITION
            # Any unexpected exception (incl. Ctrl-C during mysqldump) before
            # the execv must not leak the lock: on the success path execv
            # replaces the process so this except never runs, and the resumed
            # process owns/releases the lock via atexit.
            try:
                return self._upgrade_pinned_pre_exec(project_dir, lock, skip_backup, backup_ts)
            except BaseException:
                self._release_op_lock(lock)
                raise
            # pre_exec only returns on abort paths (a success execs)

        # ---- Resumed process (new code) continues from step 3. NEVER re-exec here. ----
        # The op lock acquired before the execv is still on disk; release it on
        # ANY exit path of this process (abort or success) via atexit.
        _lock_path = os.path.join(project_dir, ".voipbin-op.lock")
        atexit.register(self._release_op_lock, _lock_path)
        print(f"\n{blue('==>')} Resumed after script update (steps 1-2 already done).")
        if backup_ts:
            print(f"  Pre-upgrade backup: backups/{backup_ts}/")

        restore_hint = f"voipbin restore {backup_ts} --force" if backup_ts else "voipbin restore <ts> --force"

        # ---- Provider keys preflight: nothing below changes an image until the
        # master keys the services need to store them are in place ----
        self._update_stage = "keys-preflight"
        if not self._provider_preflight_step(project_dir, "update"):
            return RC_KEYS_FAILED

        # ---- Step 2b: reconcile the LIVE docker-compose.yml's image digests ----
        # Both docker-compose.yml and versions.lock are untracked
        # (install/.gitignore) after their respective .dist splits, so
        # step 2's `git pull` updated docker-compose.yml.dist and
        # versions.lock.dist but never touched the live docker-compose.yml
        # `docker compose pull`/`up -d` below actually read (nor the live
        # versions.lock, which stays operator-owned and isn't read by this
        # flow at all). Without this step the pinned upgrade would silently
        # no-op: same digests pulled/recreated, "Upgrade complete!" printed,
        # nothing actually upgraded. sync-compose-images.sh's own default
        # LOCK_FILE (versions.lock.dist) is exactly the file this step just
        # pulled, so no explicit COMPOSE_FILE/LOCK_FILE pairing juggling is
        # needed here beyond pointing COMPOSE_FILE at the live file below.
        sync_script = os.path.join(project_dir, "scripts", "sync-compose-images.sh")
        print(f"\n{blue('==>')} Step 2b/6: syncing docker-compose.yml image digests from versions.lock.dist...")
        self._update_stage = "sync"
        if not os.path.exists(sync_script):
            print(f"\n{red('Upgrade aborted:')} {sync_script} not found.")
            print("  The 'update scripts' step may have pulled an incomplete tree -")
            print("  check 'git status' in the repo, or re-clone and retry 'update all'.")
            return RC_SYNC_FAILED
        sync_env = os.environ.copy()
        sync_env["COMPOSE_FILE"] = os.path.join(project_dir, "docker-compose.yml")
        rc = subprocess.call(["bash", sync_script], cwd=project_dir, env=sync_env)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} sync-compose-images.sh failed (exit {rc}).")
            print("  docker-compose.yml was not modified - the script writes atomically via a")
            print("  temp file + rename, so a mid-run failure leaves the old file intact.")
            print("  Old containers are still running the old digests.")
            return RC_SYNC_FAILED

        # ---- Step 3: docker compose pull (fetch the NEW pinned digests) ----
        print(f"\n{blue('==>')} Step 3/6: pulling new pinned images (docker compose pull)...")
        self._update_stage = "pull"
        rc = subprocess.call("docker compose pull", shell=True, cwd=project_dir)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} docker compose pull failed (exit {rc}).")
            print("  Old containers are still running the old digests; safe to re-run 'update all'.")
            return RC_PULL_FAILED

        # ---- Step 4: migrations (surfaced live, abort on failure) ----
        migrate_script = os.path.join(project_dir, "scripts", "migrate.sh")
        print(f"\n{blue('==>')} Step 4/6: running database migrations (scripts/migrate.sh)...")
        self._update_stage = "migrate"
        if not os.path.exists(migrate_script):
            print(f"\n{red('Upgrade aborted:')} {migrate_script} not found.")
            print("  The 'update scripts' step may have pulled an incomplete tree -")
            print("  check 'git status' in the repo, or re-clone and retry 'update all'.")
            return RC_MIGRATE_FAILED
        rc = subprocess.call(["bash", migrate_script], cwd=project_dir)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} migration failed (exit {rc}).")
            print(f"\n{yellow('MySQL DDL is non-transactional - the migration may be PARTIALLY applied.')}")
            print("  Recovery procedure:")
            print("    1. voipbin stop --all")
            print(f"    2. {restore_hint}    (restores pre-upgrade schema + data)")
            print("    3. git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/")
            print("       (pathspec-scoped: a bare 'git checkout <commit>' would revert this repo's")
            print("       ENTIRE tree and detach HEAD, not just install/'s files)")
            print(f"    4. COMPOSE_FILE={os.path.join(project_dir, 'docker-compose.yml')} bash scripts/sync-compose-images.sh")
            print("    5. voipbin start")
            return RC_MIGRATE_FAILED

        # ---- Step 4b: store the provider keys of .env in the services' stored
        # configs, from the NEW images in throwaway containers. This runs before
        # `up -d` on purpose: the new services must find their keys when they
        # start, and a failure here leaves the running containers untouched. ----
        print(f"\n{blue('==>')} Step 4b/6: storing provider keys from .env (new images, before they start)...")
        self._update_stage = "keys"
        if not self._provider_import_step(project_dir, "update"):
            print(f"\n{red('Upgrade aborted:')} the provider keys could not be stored (see above).")
            print("  The running containers are untouched and still use the old images. The database schema")
            print("  migration (step 4) is already applied and the new images are pulled; no restore is needed.")
            print("  Fix the cause, then run 'sudo ./voipbin update all' again (safe to repeat; stored keys stay).")
            return RC_KEYS_FAILED

        # ---- Step 5: recreate changed containers ----
        print(f"\n{blue('==>')} Step 5/6: recreating services (docker compose up -d)...")
        self._update_stage = "up"
        rc = subprocess.call("docker compose up -d", shell=True, cwd=project_dir)
        if rc != 0:
            print(f"\n{red('Upgrade aborted:')} docker compose up -d failed (exit {rc}).")
            print(f"  If services are broken: stop, then '{restore_hint}', then")
            print("  'git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/'")
            print("  (pathspec-scoped - a bare checkout would revert this repo's entire tree)")
            print(f"  and 'COMPOSE_FILE={os.path.join(project_dir, 'docker-compose.yml')} bash scripts/sync-compose-images.sh'.")
            return RC_UP_FAILED

        # ---- Step 6: verify ----
        print(f"\n{blue('==>')} Step 6/6: verifying stack health...")
        self._update_stage = "verify"
        self._verify_rc = 0
        if self._verify_stack(project_dir):
            self._update_stage = "done"
            print(f"\n{bold(green('Upgrade complete!'))}")
            if backup_ts:
                print(f"  Pre-upgrade backup kept at backups/{backup_ts}/")
            print("  Run 'voipbin status' to inspect services.")
            return 0
        else:
            # A False return with no recorded code (e.g. a replaced
            # _verify_stack) must still fail: derive 17.
            verify_rc = self._verify_rc or RC_VERIFY_CONTAINERS
            print(f"\n{red('Upgrade verification FAILED.')}")
            print("  Recovery options:")
            print("    - Data rollback:  stop services, then run:")
            print(f"        {restore_hint}")
            print("    - Image rollback: restore the previous versions.lock/scripts (docker-compose.yml")
            print("      is not git-versioned, only .dist is - see docker-compose.yml.dist's header):")
            print("        git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/")
            print("      (pathspec-scoped on purpose: a bare 'git checkout <commit>' with no pathspec")
            print("      would revert this repo's ENTIRE tree, not just install/, and leave you in")
            print("      detached HEAD - the form above only restores those files' content, in place,")
            print("      on your current branch)")
            print(f"        COMPOSE_FILE={os.path.join(project_dir, 'docker-compose.yml')} bash scripts/sync-compose-images.sh")
            print("        docker compose up -d")
            print(f"  {yellow('Do NOT use')} 'voipbin rollback' here: it replays docker-compose.override.yml")
            print("  snapshots that only exist on UNPINNED repos and would silently unpin this repo.")
            return verify_rc

    def _upgrade_pinned_pre_exec(self, project_dir, lock, skip_backup, backup_ts):
        """Steps 1-2 of the pinned upgrade + the re-exec. Runs with the op lock
        held; the CALLER releases the lock if this raises. Returning (instead
        of exec'ing) means the upgrade aborted - the caller just returns."""
        # ---- Step 1: backup (BEFORE git pull - captures the OLD pin state) ----
        if skip_backup:
            print(f"\n{yellow('==>')} Step 1/6: backup SKIPPED (--skip-backup)")
        else:
            print(f"\n{blue('==>')} Step 1/6: creating pre-upgrade backup...")
            self._update_stage = "backup"
            backup_ts = self._do_backup(project_dir)
            if not backup_ts:
                print(f"\n{red('Upgrade aborted:')} backup failed. Nothing was changed.")
                print("  Fix the backup problem, or re-run with --skip-backup to opt out (not recommended).")
                self._release_op_lock(lock)
                self._update_stage = "backup"
                return RC_BACKUP_FAILED

        # ---- Step 2: update scripts (git pull) ----
        print(f"\n{blue('==>')} Step 2/6: updating scripts (git pull)...")
        self._update_stage = "scripts"
        self._update_scripts_rc = RC_SCRIPTS_FAILED
        if not self._update_scripts(project_dir, check_only=False):
            print(f"\n{red('Upgrade aborted:')} the script update failed.")
            print("  If the message above says the pull failed, the old pin is intact.")
            if backup_ts:
                print(f"  Pre-upgrade backup: backups/{backup_ts}/ (nothing to restore)")
            self._release_op_lock(lock)
            self._update_stage = "scripts"
            return self._update_scripts_rc or RC_SCRIPTS_FAILED

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
        tui_pin_close()  # the re-executed CLI runs without the TUI
        os.execv(sys.executable, argv)

    def _verify_stack(self, project_dir, timeout=120):
        """Poll compose health until no container is starting/unhealthy, then
        ping api-manager from a helper container on the compose network. The
        ping runs even when containers are unhealthy. Returns True only when
        both are fine; the exit code is recorded in self._verify_rc."""
        self._verify_rc = 0
        self._verify_bad = 0
        self._verify_ping = "skipped"
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

        self._verify_bad = len(bad)
        if bad:
            print(f"  {red('✗')} Timed out after {timeout}s. Problem containers:")
            for name, why in bad:
                print(f"    - {name}: {why}")
            print("  Inspect why with: docker compose logs --tail 50 <service>")
            print("  (The API ping below is still attempted so its result is visible.)")
        else:
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
        ping_ok = result.returncode == 0 and code and code != "000"
        if ping_ok:
            print(f"  {green('✓')} api-manager /ping responded (HTTP {code})")
            self._verify_ping = "ok"
        else:
            print(f"  {red('✗')} api-manager /ping failed (curl exit {result.returncode}, HTTP '{code or '-'}')")
            if result.stderr:
                print(f"    {result.stderr.strip().splitlines()[-1]}")
            self._verify_ping = "fail"
        # 17 containers only, 18 ping only, 19 both (a container problem must
        # not mask an API outage).
        if bad and not ping_ok:
            self._verify_rc = RC_VERIFY_BOTH
        elif bad:
            self._verify_rc = RC_VERIFY_CONTAINERS
        elif not ping_ok:
            self._verify_rc = RC_VERIFY_PING
        else:
            self._verify_rc = 0
        return self._verify_rc == 0

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
            _match_parent_owner(lock_path)
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
            print(f"\n{yellow('==>')} versions.lock found - this install is PINNED to a verified snapshot.")
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
            (no host alembic needed; non-interactive by design). The pinned
            schema revision source is resolved by migrate.sh."""
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
            self._provider_keys_unpinned(project_dir)

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

    # Exit code recorded by _update_scripts on failure (its return value stays a
    # bool so `if not self._update_scripts(...)` keeps its meaning).
    _update_scripts_rc = RC_SCRIPTS_FAILED
    last_update_rc = 0
    _update_stage = "init"
    _verify_rc = 0
    _verify_bad = 0
    _verify_ping = "skipped"

    def _scripts_fail(self, message, *hints):
        print(red(message))
        for h in hints:
            print(h)
        self._update_scripts_rc = RC_SCRIPTS_FAILED
        return False

    def _update_scripts(self, project_dir, check_only=False):
        """Update scripts and configs from the git remote.

        Returns True on success (including already-up-to-date), False on
        failure so upgrade callers can abort. Success is decided by git exit
        codes, never by scanning output text. On failure the exit code is left
        in self._update_scripts_rc (11)."""
        self._update_scripts_rc = RC_SCRIPTS_FAILED
        print(f"\n{bold('Script Update from GitHub')}")
        print("=" * 50)

        os.chdir(project_dir)
        project_dir = os.path.realpath(project_dir)

        found = _find_git_marker(project_dir)
        if not found:
            return self._scripts_fail(
                "Error: Not a git repository.",
                "This command requires the install to be cloned from GitHub "
                "(the checkout may sit in a parent directory of install/).")
        _, git_marker, marker_stat = found
        owner = _git_owner(marker_stat)

        try:
            return self._update_scripts_git(project_dir, git_marker, owner, check_only)
        except GitOwnerError as e:
            return self._scripts_fail(
                f"Error: cannot run git as the repository owner: {e}.",
                "  Fix the ownership of the checkout (sudo chown -R <user> <repo>) and retry.")

    def _update_scripts_git(self, project_dir, git_marker, owner, check_only):
        def git(*args):
            return _git(project_dir, *args, owner=owner)

        top_res = git("rev-parse", "--show-toplevel")
        prefix_res = git("rev-parse", "--show-prefix")
        if top_res.returncode != 0 or prefix_res.returncode != 0:
            return self._scripts_fail(
                "Error: git could not open the repository.",
                f"  {(top_res.stderr or prefix_res.stderr or '').strip()}")
        top = top_res.stdout.strip()
        prefix = prefix_res.stdout.strip()

        # The repository found above must actually track this install directory
        # (guards against an unrelated repository in a parent directory).
        tracked_probe = git("ls-files", "--error-unmatch", "versions.lock.dist")
        if tracked_probe.returncode != 0:
            return self._scripts_fail(
                "Error: the enclosing git repository does not track this install directory.",
                f"  Found a repository at {top}, but it has no versions.lock.dist here.")

        if owner:
            # --full-name: paths relative to the repository root (not to the
            # install directory this command runs in), matching `top`.
            ls = git("ls-files", "-z", "--full-name")
            tracked = [t for t in (ls.stdout or "").split("\0") if t]
            if os.path.isdir(git_marker):
                git_dir = git_marker
                extra_dirs = []
            else:
                # .git is a file (linked worktree): check the real git
                # directory and the shared one it points to.
                gd = git("rev-parse", "--absolute-git-dir")
                cd = git("rev-parse", "--git-common-dir")
                git_dir = (gd.stdout or "").strip() or os.path.join(top, ".git")
                common = (cd.stdout or "").strip()
                if common and not os.path.isabs(common):
                    common = os.path.join(project_dir, common)
                extra_dirs = [os.path.normpath(common)] if common else []
            offenders = _ownership_offenders(owner, git_dir, top, tracked)
            for extra in extra_dirs:
                if extra != git_dir:
                    offenders = sorted(set(offenders) | set(_ownership_offenders(owner, extra, top, [])))
            if offenders:
                print(red("Error: the checkout contains files this tool cannot update as its owner:"))
                for path in offenders[:10]:
                    print(f"  {path}")
                if len(offenders) > 10:
                    print(f"  ... and {len(offenders) - 10} more")
                print("  Nothing was changed. A previous 'sudo git' run probably left them behind;")
                print(f"  repair with: sudo chown -R {owner[0]}:{owner[1]} {top}")
                self._update_scripts_rc = RC_SCRIPTS_FAILED
                return False

        print(f"\n{blue('==>')} Fetching from remote...")
        fetch = git("fetch", "origin")
        if fetch.returncode != 0:
            return self._scripts_fail(f"  Error fetching: {(fetch.stderr or fetch.stdout or '').strip()}")
        print(green("  ✓ Fetched latest"))

        branch_res = git("symbolic-ref", "-q", "--short", "HEAD")
        current_branch = (branch_res.stdout or "").strip()
        if branch_res.returncode != 0 or not current_branch:
            return self._scripts_fail(
                "Error: HEAD is detached.",
                "  Check out a branch (git checkout <branch>) and retry.")
        print(f"  Current branch: {current_branch}")
        remote_ref = f"origin/{current_branch}"
        if git("rev-parse", "-q", "--verify", remote_ref).returncode != 0:
            return self._scripts_fail(
                f"Error: remote branch {remote_ref} does not exist.",
                "  Set the upstream (git branch -u origin/<branch>) or check out a branch that exists on origin.")

        status = git("status", "--porcelain", "-z")
        if status.returncode != 0:
            return self._scripts_fail(f"  Error reading git status: {(status.stderr or '').strip()}")
        significant_changes = []
        for xy, path in _parse_porcelain_z(status.stdout or ""):
            rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
            if prefix and not path.startswith(prefix):
                rel = "../" + path  # outside install/: never protected
            if not _is_protected_path(rel):
                significant_changes.append(f"{xy} {path}")

        if significant_changes:
            print(f"\n{yellow('Local changes detected:')}")
            for change in significant_changes[:10]:
                print(f"  {change}")
            if len(significant_changes) > 10:
                print(f"  ... and {len(significant_changes) - 10} more")

        print(f"\n{blue('==>')} Checking for updates...")
        behind = git("rev-list", "--count", f"HEAD..{remote_ref}")
        try:
            behind_n = int((behind.stdout or "").strip())
        except ValueError:
            behind_n = -1
        if behind.returncode != 0 or behind_n < 0:
            return self._scripts_fail(
                f"  Error comparing HEAD with {remote_ref}: {(behind.stderr or '').strip()}")
        if behind_n == 0:
            print(green("  Already up to date!"))
            return True

        diff_stat = git("diff", f"HEAD..{remote_ref}", "--stat").stdout or ""
        print(f"\n{yellow('Changes available:')}")
        for line in diff_stat.strip().split('\n')[-20:]:
            print(f"  {line}")

        print(f"\n{blue('==>')} Changes in tracked files:")
        for tracked_path in TRACKED_PATHS:
            diff = (git("diff", f"HEAD..{remote_ref}", "--stat", "--", tracked_path).stdout or "").strip()
            if diff:
                print(f"  {tracked_path}:")
                for line in diff.split('\n')[:5]:
                    if line.strip():
                        print(f"    {line}")

        if check_only:
            print(f"\n{yellow('Dry-run mode:')} No changes made.")
            print("Run 'update scripts' without --check to apply updates.")
            return True

        backup_path = self._create_backup(project_dir, significant_changes, repo_root=top)
        if backup_path:
            print(f"\n{green('✓')} Backup created: {backup_path}")

        stashed = False
        if significant_changes:
            print(f"\n{blue('==>')} Stashing local changes...")
            before = (git("rev-parse", "-q", "--verify", "refs/stash").stdout or "").strip()
            stash = git("-c", "user.name=voipbin-cli", "-c", "user.email=voipbin-cli@localhost",
                        "stash", "push", "-m", "voipbin-cli auto-stash before update")
            if stash.returncode != 0:
                return self._scripts_fail(
                    f"  Error stashing local changes: {(stash.stderr or stash.stdout or '').strip()}",
                    "  Nothing was pulled.")
            after = (git("rev-parse", "-q", "--verify", "refs/stash").stdout or "").strip()
            # rc 0 also covers "No local changes to save" (untracked files only);
            # only a changed refs/stash means there is something to pop later.
            stashed = bool(after) and after != before
            if stashed:
                print(green("  ✓ Changes stashed"))

        def restore_stash():
            pop = git("stash", "pop")
            return pop.returncode == 0 and "CONFLICT" not in ((pop.stdout or "") + (pop.stderr or ""))

        def restore_stash_after_failure():
            if not restore_stash():
                print(yellow("  ! Restoring your local changes failed; the stash is still kept."))
                print("  Run 'git stash list' to check it and 'git stash pop' once the problem is resolved.")

        print(f"\n{blue('==>')} Pulling updates...")
        pull = git("pull", "--ff-only", "origin", current_branch)
        if pull.returncode != 0:
            print(red(f"  Error: {(pull.stderr or pull.stdout or '').strip()}"))
            if stashed:
                print(f"\n{blue('==>')} Restoring stashed changes...")
                restore_stash_after_failure()
            self._update_scripts_rc = RC_SCRIPTS_FAILED
            return False

        # Accept a HEAD that already contains the remote branch (local commits
        # ahead); reject one that is behind or diverged.
        if git("merge-base", "--is-ancestor", remote_ref, "HEAD").returncode != 0:
            print(red(f"  Error: after pull, {remote_ref} is not an ancestor of HEAD."))
            if stashed:
                restore_stash_after_failure()
            self._update_scripts_rc = RC_SCRIPTS_FAILED
            return False

        print(green("  ✓ Updated successfully"))

        if stashed:
            print(f"\n{blue('==>')} Restoring local changes...")
            if not restore_stash():
                print(yellow("  ! Scripts were updated, but restoring your local changes conflicted."))
                print("  The conflict is left in the working tree; run 'git status' to resolve it.")
                self._update_scripts_rc = RC_SCRIPTS_FAILED
                return False
            print(green("  ✓ Local changes restored"))

        self._cleanup_old_backups(project_dir)

        print(f"\n{bold('Script update complete!')}")
        print("Run 'status' to check service status.")
        return True

    def _create_backup(self, project_dir, changed_files, repo_root=None):
        """Create a backup of modified files before updating. Paths in
        `changed_files` are relative to the repository root (`repo_root`,
        default project_dir)."""
        if not changed_files:
            return None
        repo_root = repo_root or project_dir

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

                src_path = os.path.join(repo_root, file_path)
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
            print("    - Image rollback: restore the previous versions.lock/scripts (docker-compose.yml")
            print("      is not git-versioned, only .dist is - see docker-compose.yml.dist's header):")
            print("        git checkout <previous commit> -- docker-compose.yml.dist versions.lock.dist scripts/")
            print("      (pathspec-scoped on purpose: a bare 'git checkout <commit>' with no pathspec")
            print("      would revert this repo's ENTIRE tree, not just install/, and leave you in")
            print("      detached HEAD - the form above only restores those files' content, in place,")
            print("      on your current branch)")
            print(f"        COMPOSE_FILE={os.path.join(project_dir, 'docker-compose.yml')} bash scripts/sync-compose-images.sh")
            print("        docker compose up -d")
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
        if tui_pin_clear():
            return  # the pinned header stays; only the region below it is erased
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

        # Slash commands (terminal REPL with the TUI only)
        if self.cli.tui_on and line.lstrip().startswith("/"):
            if len(parts) == 1 and not line.endswith(" "):
                # GNU readline drops "/" from text, libedit keeps it
                prefix = "/" if text.startswith("/") else ""
                word = text.lstrip("/")
                return [f"{prefix}{c} " for c in TUI_SLASH_COMMANDS if c.startswith(word)]
            line = line.lstrip()[1:]
            parts = line.split()

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


def render_help():
    """The command overview shared by `help` (REPL) and `--help`."""
    return f"""
{bold('VoIPBin Install CLI')}

{blue('Install:')}
  install           Guided install: host checks, configuration, host setup,
                    services, verification and the first admin account
  init              Generate configuration only (.env, certificates)

{blue('Operate:')}
  status, ps        Show service status and optional provider keys
  doctor            Diagnose install and runtime state (read-only)
  start [service]   Start services
  stop [--all]      Stop app services (--all for everything)
  restart [service] Restart services
  logs <service>    View logs (-f to follow)
  update [options]  Update (scripts, all, keys, --check) - 'update all' = full pinned upgrade
  backup            Full data backup (MySQL + recordings + config) to backups/<ts>/
  restore <ts>      Restore DATA from a backup (DESTRUCTIVE, requires --force + stopped services)

{blue('Advanced:')}
  dns               DNS setup for SIP domains
  certs             Manage SSL certificates
  network           Manage VoIP network interfaces
  clean [options]   Remove containers/data/generated files (asks first; --force)
  rollback          Rollback image override history (UNPINNED repos only, --list)
  version           Show pinned image versions
  config            View/set CLI configuration
  ast, kam, db, api Asterisk CLI, Kamailio kamcmd, MySQL, REST API contexts

{blue('Other:')}
  help [command]    Show help for a command
  help api          Resource commands (extension, agent, call, flow, ...)
  clear, exit       Clear screen, exit

Undo host changes: clean --dns, clean --network, scripts/setup-dns.sh --uninstall
Type 'help <command>' for detailed usage.
"""


def render_api_help():
    """Resource (sidecar/API) commands, kept out of the default help."""
    return f"""
{blue('Resource commands:')}  (talk to the running services; see 'help <command>')
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
"""


def show_cli_usage():
    """Show usage for command-line mode (same command list as `help`)"""
    print(f"""
Usage:
  sudo ./voipbin                # Interactive mode (offers 'install' on a new checkout)
  sudo ./voipbin <command>      # Run one command and exit""")
    print(render_help())
    print("""Examples:
  sudo ./voipbin install
  sudo ./voipbin install --yes --admin-email admin@example.com --admin-password-file ~/voipbin-admin-password
  sudo ./voipbin status
  sudo ./voipbin logs api-manager

help, --help and version work without sudo; everything else needs it.
""")


def main():
    try:
        _main()
    except KeyboardInterrupt:
        print()
        sys.exit(RC_INTERRUPTED)
    except Exception as e:
        print(red(f"Unexpected error: {type(e).__name__}: {e}"))
        traceback.print_exc(file=sys.stderr)
        sys.exit(RC_UNEXPECTED)


def _main():
    if not color_wanted():
        Colors.disable()

    # help/--help/version need no root (and no docker); everything else does.
    no_root = len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help", "version")
    if not no_root:
        check_root()

    cli = VoIPBinCLI()

    # Change to project directory
    project_dir = cli.config.get("project_dir")
    if project_dir and os.path.isdir(project_dir):
        os.chdir(project_dir)

    # Check for command-line arguments (non-interactive mode)
    if len(sys.argv) > 1:
        # Handle help
        if sys.argv[1] in ("-h", "--help") or (sys.argv[1] == "help" and len(sys.argv) == 2):
            show_cli_usage()
            return

        # Parse command and args
        cmd = sys.argv[1].lower()
        args = sys.argv[2:]

        if cmd in cli.commands:
            cli.commands[cmd](args)
            # Exit codes are part of the machine contract for scripted
            # callers: doctor (0/1), update (0, 11 ... 64), and init, start,
            # clean, install (0 ok, 1 user/validation error, 2 environment
            # error; init/start pass their script's code through). Only this
            # non-interactive path exits with them; the REPL path returns to
            # the prompt instead.
            rc = 0
            if cmd == "doctor":
                rc = cli.last_doctor_rc
            elif cmd == "update":
                rc = cli.last_update_rc
            elif cmd in ("init", "start", "clean", "install"):
                rc = cli.last_rc
            if rc:
                sys.exit(rc if 0 < rc < 256 else 2)
        else:
            print(red(f"Unknown command: {cmd}"))
            print("Run 'sudo ./voipbin --help' for usage.")
            sys.exit(1)
        return

    # Interactive mode
    setup_readline(cli)
    start_hangup_watch()

    try:
        # Start screen: the TUI on a UTF-8 terminal, else the plain dashboard
        cli.tui_on = tui_wanted()
        info = None
        if cli.tui_on:
            info = tui_collect_info(cli)
            tui_pin_start(info)
            state = info["state"]
        else:
            show_welcome_dashboard()
            state = detect_dashboard_state()

        # A checkout without .env: offer the guided install once (it asks
        # "Install now?" itself), then continue with the REPL either way.
        if state == "not_initialized" and sys.stdin.isatty():
            cli.cmd_install([])
            print()
            if info is not None:
                info.update(tui_collect_info(cli))

        # Handle Ctrl+C gracefully
        def sigint_handler(sig, frame):
            print()
            if cli.context:
                cli.context = None
            else:
                print("Type 'exit' to quit")

        signal.signal(signal.SIGINT, sigint_handler)

        repl_loop(cli, info)

        print("Goodbye!")
    finally:
        tui_pin_close()


if __name__ == "__main__":
    main()
