# VoIPBin Sandbox Tests

Unit tests for infrastructure scripts using [BATS](https://github.com/bats-core/bats-core) (Bash Automated Testing System).

## Prerequisites

Install BATS using one of these methods:

```bash
# Ubuntu/Debian (system-wide)
sudo apt install bats

# macOS (system-wide)
brew install bats-core

# Without sudo (local installation)
git clone --depth 1 https://github.com/bats-core/bats-core.git /tmp/bats-core
# Then run tests with: /tmp/bats-core/bin/bats tests/

# From source with sudo (system-wide, latest version)
git clone https://github.com/bats-core/bats-core.git
cd bats-core
sudo ./install.sh /usr/local
```

## Running Tests

```bash
# Run all tests
bats tests/

# Run specific test file
bats tests/common.bats
bats tests/init.bats
bats tests/setup-voip-network.bats

# Verbose output (shows each test)
bats --verbose-run tests/

# TAP output format (for CI)
bats --tap tests/
```

## Test Structure

```
tests/
├── README.md                     # This file
├── test_helper.bash              # Shared utilities and mocking functions
├── common.bats                   # Tests for scripts/common.sh
├── config.bats                   # Configuration validation tests
├── doctor.bats                   # Tests for scripts/doctor.sh (install doctor, VOIP-1280)
├── init.bats                     # Tests for scripts/init.sh
├── setup-dns.bats                # Tests for scripts/setup-dns.sh
└── setup-voip-network.bats       # Tests for scripts/setup-voip-network.sh
```

## What's Tested (120 tests total)

### common.sh (20 tests)
- `detect_host_ip()` - IP detection with fallback chain (.env → ip route → hostname → fallback)
- `generate_coredns_config()` - CoreDNS Corefile generation with correct domain mappings
- `detect_os()` - OS detection (Linux/macOS/unknown)

### init.sh (19 tests)
- `generate_service_ips()` - Sequential IP allocation for Kamailio/RTPEngine
- `generate_random_key()` - JWT key generation (64-char hex)
- `check_mkcert()` - Certificate tool detection
- `generate_cert()` - SIP TLS certificate generation
- `generate_api_cert()` - API certificate with base64 encoding

### setup-voip-network.sh (20 tests)
- `detect_physical_interface()` - Network interface detection
- `get_interface_ip()` - Get IP of interface
- `load_external_ips()` - Load IPs from .env file
- `parse_args()` - Command-line argument parsing
- `INTERNAL_INTERFACES` - Verify interface configuration

### setup-dns.sh (24 tests)
- `check_coredns()` - CoreDNS container detection
- `regenerate_corefile()` - Corefile generation with IP configuration
- Linux DNS configuration (resolv.conf, backup/restore)
- macOS DNS configuration (/etc/resolver)
- Command-line argument parsing (--uninstall, --test, --regenerate, -y)
- `test_dns()` - DNS resolution testing

### config.bats (37 tests)
- **docker-compose.yml validation**
  - YAML syntax validity
  - Required services exist (db, redis, rabbitmq, kamailio, api-manager, frontends)
  - Port mappings (3003, 3004, 3005, 8443)
  - Network configuration (10.100.0.0/16)
  - Fixed container IPs
  - No duplicate port conflicts
- **.env.template validation**
  - No duplicate keys
  - Required variables present
- **Cross-file consistency**
  - Container IPs match between common.sh and docker-compose.yml
  - Internal network IPs consistent across scripts

### doctor.bats (40 tests)
- `scripts/doctor.sh` output grammar: `DOCTOR <name>: pass|fail|warn|skip` lines, `FIX <name>:` prescriptions, `VOIPBIN_DOCTOR:` result line, exit codes
- Stage awareness (pre-install, pre-start, running), docker-availability gating, macOS gating
- Prerequisite, install-state and runtime-pathology checks with seeded failures and exact prescription assertions

## Writing New Tests

1. Create a new `.bats` file or add to existing one
2. Load the test helper: `load 'test_helper'`
3. Use `setup()` and `teardown()` for test isolation
4. Use mocking functions from `test_helper.bash`

Example:

```bash
#!/usr/bin/env bats

load 'test_helper'

setup() {
    setup_test_env
}

teardown() {
    teardown_test_env
}

@test "my function does something" {
    # Arrange
    create_env_file "KEY=value"
    mock_ip_route "192.168.1.100"
    load_common

    # Act
    result=$(my_function)

    # Assert
    assert_equal "$result" "expected"
}
```

## Mocking Functions

The test helper provides these mocking utilities:

- `mock_command "name" "output" [exit_code]` - Create mock command
- `mock_ip_route "ip"` - Mock `ip route get` output
- `mock_hostname "ip"` - Mock `hostname -I` output
- `mock_uname "os"` - Mock `uname -s` output
- `mock_openssl_rand "hex"` - Mock `openssl rand -hex` output
- `create_env_file "KEY=val" ...` - Create .env file with content

## Assertion Functions

- `assert_equal "actual" "expected"` - Values are equal
- `assert_not_equal "val1" "val2"` - Values are different
- `assert_valid_ip "ip"` - Valid IPv4 format
- `assert_valid_base64 "str"` - Valid base64 encoding
- `assert_file_contains "file" "string"` - File contains string
- `assert_file_not_contains "file" "string"` - File doesn't contain string
- `assert_matches "string" "pattern"` - String matches regex
- `assert_length "string" N` - String has length N
