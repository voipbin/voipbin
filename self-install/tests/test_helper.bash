#!/bin/bash
# VoIPBin Sandbox - BATS Test Helper
# Shared utilities for all test files

# =============================================================================
# Test Directory Setup
# =============================================================================

# Get the directory where tests are located
TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$TEST_DIR")"
SCRIPTS_DIR="$PROJECT_ROOT/scripts"

# Temp directory for test artifacts (created per test)
TEST_TEMP_DIR=""

# Original PATH (saved for restoration)
ORIGINAL_PATH="$PATH"

# =============================================================================
# Setup/Teardown Functions
# =============================================================================

# Call this in setup() to create temp directories
setup_test_env() {
    TEST_TEMP_DIR="$(mktemp -d)"
    export PROJECT_DIR="$TEST_TEMP_DIR"
    export CERTS_DIR="$TEST_TEMP_DIR/certs"
    export MOCK_BIN_DIR="$TEST_TEMP_DIR/mock_bin"

    mkdir -p "$PROJECT_DIR"
    mkdir -p "$CERTS_DIR"
    mkdir -p "$MOCK_BIN_DIR"

    # Prepend mock bin to PATH so mocked commands take precedence
    export PATH="$MOCK_BIN_DIR:$ORIGINAL_PATH"
}

# Call this in teardown() to clean up
teardown_test_env() {
    if [[ -n "$TEST_TEMP_DIR" && -d "$TEST_TEMP_DIR" ]]; then
        rm -rf "$TEST_TEMP_DIR"
    fi
    export PATH="$ORIGINAL_PATH"
}

# =============================================================================
# Command Mocking
# =============================================================================

# Create a mock command that outputs specified text
# Usage: mock_command "command_name" "output_text" [exit_code]
mock_command() {
    local cmd_name="$1"
    local output="$2"
    local exit_code="${3:-0}"

    cat > "$MOCK_BIN_DIR/$cmd_name" << EOF
#!/bin/bash
echo "$output"
exit $exit_code
EOF
    chmod +x "$MOCK_BIN_DIR/$cmd_name"
}

# Create a mock command that runs custom script
# Usage: mock_command_script "command_name" "script_content"
mock_command_script() {
    local cmd_name="$1"
    local script="$2"

    cat > "$MOCK_BIN_DIR/$cmd_name" << EOF
#!/bin/bash
$script
EOF
    chmod +x "$MOCK_BIN_DIR/$cmd_name"
}

# Mock 'ip route get' to return specific source IP
# Usage: mock_ip_route "192.168.1.100"
mock_ip_route() {
    local ip="$1"
    mock_command_script "ip" "
if [[ \"\$1\" == \"route\" && \"\$2\" == \"get\" ]]; then
    echo \"8.8.8.8 via 192.168.1.1 dev eth0 src $ip uid 1000\"
elif [[ \"\$1\" == \"addr\" && \"\$2\" == \"show\" ]]; then
    echo \"    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0\"
elif [[ \"\$1\" == \"link\" && \"\$2\" == \"show\" ]]; then
    exit 0
else
    exit 1
fi
"
}

# Mock 'hostname -I' to return specific IP
# Usage: mock_hostname "192.168.1.100"
mock_hostname() {
    local ip="$1"
    mock_command_script "hostname" "
if [[ \"\$1\" == \"-I\" ]]; then
    echo \"$ip \"
else
    # Use command -v for portable path resolution (works on Linux and macOS)
    \$(command -v hostname 2>/dev/null || echo /usr/bin/hostname) \"\$@\"
fi
"
}

# Mock 'uname -s' to return specific OS
# Usage: mock_uname "Linux" or mock_uname "Darwin"
mock_uname() {
    local os="$1"
    mock_command_script "uname" "
if [[ \"\$1\" == \"-s\" ]]; then
    echo \"$os\"
else
    # Use command -v for portable path resolution (works on Linux and macOS)
    \$(command -v uname 2>/dev/null || echo /usr/bin/uname) \"\$@\"
fi
"
}

# Mock 'openssl rand' to return predictable output
# Usage: mock_openssl_rand "abc123..."
mock_openssl_rand() {
    local hex_output="$1"
    mock_command_script "openssl" "
if [[ \"\$1\" == \"rand\" && \"\$2\" == \"-hex\" ]]; then
    echo \"$hex_output\"
elif [[ \"\$1\" == \"req\" ]]; then
    # For certificate generation, create dummy files
    shift  # skip 'req'
    while [[ \$# -gt 0 ]]; do
        case \"\$1\" in
            -keyout) touch \"\$2\"; shift 2 ;;
            -out) touch \"\$2\"; shift 2 ;;
            *) shift ;;
        esac
    done
else
    # Use command -v for portable path resolution (works on Linux and macOS)
    \$(command -v openssl 2>/dev/null || echo /usr/bin/openssl) \"\$@\"
fi
"
}

# Mock 'docker' command
# Usage: mock_docker_network "network_id"
mock_docker_network() {
    local network_id="$1"
    mock_command_script "docker" "
if [[ \"\$1\" == \"network\" && \"\$2\" == \"inspect\" ]]; then
    if [[ \"\$4\" == \"-f\" ]]; then
        echo \"$network_id\"
    else
        echo '{}'
    fi
    exit 0
else
    exit 1
fi
"
}

# =============================================================================
# .env File Helpers
# =============================================================================

# Create a .env file with specified content
# Usage: create_env_file "KEY1=value1" "KEY2=value2" ...
create_env_file() {
    local env_file="$PROJECT_DIR/.env"
    > "$env_file"  # Create/truncate file
    for line in "$@"; do
        echo "$line" >> "$env_file"
    done
}

# =============================================================================
# Assertion Helpers
# =============================================================================

# Assert file contains a string (fixed-string matching)
# Usage: assert_file_contains "/path/to/file" "expected string"
assert_file_contains() {
    local file="$1"
    local expected="$2"

    if [[ ! -f "$file" ]]; then
        echo "File not found: $file" >&2
        return 1
    fi

    # Use -F for fixed string matching (no regex interpretation)
    # Use -- to separate options from pattern (handles patterns starting with -)
    if ! grep -qF -- "$expected" "$file"; then
        echo "Expected '$expected' not found in $file" >&2
        echo "File contents:" >&2
        cat "$file" >&2
        return 1
    fi
}

# Assert file does not contain a string (fixed-string matching)
# Usage: assert_file_not_contains "/path/to/file" "unexpected string"
assert_file_not_contains() {
    local file="$1"
    local unexpected="$2"

    if [[ ! -f "$file" ]]; then
        echo "File not found: $file" >&2
        return 1
    fi

    # Use -F for fixed string matching (no regex interpretation)
    # Use -- to separate options from pattern (handles patterns starting with -)
    if grep -qF -- "$unexpected" "$file"; then
        echo "Unexpected '$unexpected' found in $file" >&2
        return 1
    fi
}

# Assert string is a valid IPv4 address
# Usage: assert_valid_ip "192.168.1.100"
assert_valid_ip() {
    local ip="$1"

    if [[ ! "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Invalid IP format: $ip" >&2
        return 1
    fi

    # Check each octet is 0-255
    IFS='.' read -ra octets <<< "$ip"
    for octet in "${octets[@]}"; do
        if [[ "$octet" -lt 0 || "$octet" -gt 255 ]]; then
            echo "Invalid octet in IP: $ip" >&2
            return 1
        fi
    done
}

# Assert string is valid base64
# Usage: assert_valid_base64 "SGVsbG8gV29ybGQ="
assert_valid_base64() {
    local str="$1"

    if [[ -z "$str" ]]; then
        echo "Empty string is not valid base64" >&2
        return 1
    fi

    # Try to decode - if it fails, it's not valid base64
    if ! echo "$str" | base64 -d &>/dev/null; then
        echo "Invalid base64: $str" >&2
        return 1
    fi
}

# Assert two values are equal
# Usage: assert_equal "actual" "expected"
assert_equal() {
    local actual="$1"
    local expected="$2"

    if [[ "$actual" != "$expected" ]]; then
        echo "Expected: $expected" >&2
        echo "Actual:   $actual" >&2
        return 1
    fi
}

# Assert two values are not equal
# Usage: assert_not_equal "value1" "value2"
assert_not_equal() {
    local val1="$1"
    local val2="$2"

    if [[ "$val1" == "$val2" ]]; then
        echo "Values should not be equal: $val1" >&2
        return 1
    fi
}

# Assert string matches regex
# Usage: assert_matches "string" "^pattern$"
assert_matches() {
    local str="$1"
    local pattern="$2"

    if [[ ! "$str" =~ $pattern ]]; then
        echo "String '$str' does not match pattern '$pattern'" >&2
        return 1
    fi
}

# Assert string length
# Usage: assert_length "string" 64
assert_length() {
    local str="$1"
    local expected_len="$2"
    local actual_len="${#str}"

    if [[ "$actual_len" -ne "$expected_len" ]]; then
        echo "Expected length $expected_len, got $actual_len" >&2
        return 1
    fi
}

# =============================================================================
# Script Loading Helpers
# =============================================================================

# Load common.sh functions (safe - no auto-execution)
load_common() {
    source "$SCRIPTS_DIR/common.sh"
}

# Load init.sh functions without running main()
# This sources the file but redefines main to be a no-op first
load_init_functions() {
    # Source common.sh first (init.sh depends on it)
    source "$SCRIPTS_DIR/common.sh"

    # Create a temporary file that sources init.sh but doesn't call main
    local temp_init="$TEST_TEMP_DIR/init_functions.sh"

    # Extract only the functions from init.sh:
    # 1. Remove the main "$@" call at the end
    # 2. Replace the source common.sh line since we already sourced it
    sed -e '/^main "\$@"$/d' \
        -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
        "$SCRIPTS_DIR/init.sh" > "$temp_init"

    # Set SCRIPT_DIR for the extracted functions
    source "$temp_init"

    # Re-assert AFTER sourcing: the extracted snippet's own top-level
    # SCRIPT_DIR=$(dirname BASH_SOURCE) resolves to the temp dir, which would
    # break "$SCRIPT_DIR/<sub-script>" subprocess calls under test.
    SCRIPT_DIR="$SCRIPTS_DIR"

    # Re-assert test isolation: init.sh recomputes these from SCRIPT_DIR when
    # not already set, and a stale value here would make generate_cert & co.
    # write into the REAL repository tree instead of the test sandbox.
    PROJECT_DIR="$TEST_TEMP_DIR"
    ENV_FILE="$TEST_TEMP_DIR/.env"
    ENV_TEMPLATE="$TEST_TEMP_DIR/.env.template"
    CERTS_DIR="$TEST_TEMP_DIR/certs"
}

# Load start.sh functions without running main()
# Same pattern as load_init_functions: strip the trailing main "$@" call and
# skip re-sourcing common.sh.
load_start_functions() {
    source "$SCRIPTS_DIR/common.sh"

    local temp_start="$TEST_TEMP_DIR/start_functions.sh"

    sed -e '/^main "\$@"$/d' \
        -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
        -e 's/^set -e$/# set -e  # disabled for testing/' \
        "$SCRIPTS_DIR/start.sh" > "$temp_start"

    source "$temp_start"

    # Re-assert AFTER sourcing: the extracted snippet's own top-level
    # SCRIPT_DIR=$(dirname BASH_SOURCE) resolves to the temp dir, which would
    # break "$SCRIPT_DIR/<sub-script>" subprocess calls under test.
    SCRIPT_DIR="$SCRIPTS_DIR"

    # Re-assert test isolation
    PROJECT_DIR="$TEST_TEMP_DIR"
}

# Load setup-host.sh functions without running main()
# Same pattern as load_start_functions: strip the trailing main "$@" call,
# skip re-sourcing common.sh, and disable set -e.
load_setup_host_functions() {
    source "$SCRIPTS_DIR/common.sh"

    local temp_setup_host="$TEST_TEMP_DIR/setup_host_functions.sh"

    sed -e '/^main "\$@"$/d' \
        -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
        -e 's/^set -e$/# set -e  # disabled for testing/' \
        "$SCRIPTS_DIR/setup-host.sh" > "$temp_setup_host"

    source "$temp_setup_host"

    # Re-assert AFTER sourcing: the extracted snippet's own top-level
    # SCRIPT_DIR=$(dirname BASH_SOURCE) resolves to the temp dir, which would
    # break "$SCRIPT_DIR/<sub-script>" subprocess calls under test.
    SCRIPT_DIR="$SCRIPTS_DIR"

    # Re-assert test isolation
    PROJECT_DIR="$TEST_TEMP_DIR"
    ENV_FILE="$TEST_TEMP_DIR/.env"
}

# Load install-certs.sh functions without running main()
# Same pattern as load_start_functions: strip the trailing main "$@" call,
# skip re-sourcing common.sh, and disable set -e.
load_install_certs_functions() {
    source "$SCRIPTS_DIR/common.sh"

    local temp_install_certs="$TEST_TEMP_DIR/install_certs_functions.sh"

    sed -e '/^main "\$@"$/d' \
        -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
        -e 's/^set -e$/# set -e  # disabled for testing/' \
        "$SCRIPTS_DIR/install-certs.sh" > "$temp_install_certs"

    source "$temp_install_certs"

    # Re-assert AFTER sourcing: the extracted snippet's own top-level
    # SCRIPT_DIR=$(dirname BASH_SOURCE) resolves to the temp dir, which would
    # break "$SCRIPT_DIR/<sub-script>" subprocess calls under test.
    SCRIPT_DIR="$SCRIPTS_DIR"

    # Re-assert test isolation
    PROJECT_DIR="$TEST_TEMP_DIR"
    ENV_FILE="$TEST_TEMP_DIR/.env"
    CERTS_DIR="$TEST_TEMP_DIR/certs"
}

# Load check-install.sh functions without running main()
# Same pattern as load_start_functions: strip the trailing main "$@" call and
# skip re-sourcing common.sh (check-install.sh has no set -e by design).
load_check_install_functions() {
    source "$SCRIPTS_DIR/common.sh"

    local temp_check_install="$TEST_TEMP_DIR/check_install_functions.sh"

    sed -e '/^main "\$@"$/d' \
        -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
        "$SCRIPTS_DIR/check-install.sh" > "$temp_check_install"

    source "$temp_check_install"

    # Re-assert AFTER sourcing: the extracted snippet's own top-level
    # SCRIPT_DIR=$(dirname BASH_SOURCE) resolves to the temp dir, which would
    # break "$SCRIPT_DIR/<sub-script>" subprocess calls under test.
    SCRIPT_DIR="$SCRIPTS_DIR"

    # Re-assert test isolation
    PROJECT_DIR="$TEST_TEMP_DIR"
    ENV_FILE="$TEST_TEMP_DIR/.env"
    CERTS_DIR="$TEST_TEMP_DIR/certs"
}

# Load doctor.sh functions without running main()
# Same pattern as load_check_install_functions: strip the trailing main "$@"
# call and skip re-sourcing common.sh (doctor.sh has no set -e by design).
# The source-substring substitution's leading # comments out the whole guarded
# source line, exit-2 guard suffix included.
load_doctor_functions() {
    source "$SCRIPTS_DIR/common.sh"

    local temp_doctor="$TEST_TEMP_DIR/doctor_functions.sh"

    sed -e '/^main "\$@"$/d' \
        -e 's|source "\$SCRIPT_DIR/common.sh"|# common.sh already sourced|' \
        "$SCRIPTS_DIR/doctor.sh" > "$temp_doctor"

    source "$temp_doctor"

    # Re-assert AFTER sourcing: the extracted snippet's own top-level
    # SCRIPT_DIR=$(dirname BASH_SOURCE) resolves to the temp dir, which would
    # break "$SCRIPT_DIR/<sub-script>" subprocess calls under test.
    SCRIPT_DIR="$SCRIPTS_DIR"

    # Re-assert test isolation
    PROJECT_DIR="$TEST_TEMP_DIR"
    ENV_FILE="$TEST_TEMP_DIR/.env"
    CERTS_DIR="$TEST_TEMP_DIR/certs"
}

# Load setup-voip-network.sh functions
# This script has inline code, so we need to be careful
load_network_functions() {
    # Source common.sh directly (the extracted snippet's own source line is
    # neutralized below — its SCRIPT_DIR would resolve to the temp dir)
    source "$SCRIPTS_DIR/common.sh"

    local temp_network="$TEST_TEMP_DIR/network_functions.sh"

    # Extract everything up to "parse_args "$@"" (marker-based, not line-number)
    # This is more robust than hardcoded line numbers
    # Remove 'set -e' to prevent premature test exits
    sed -n '1,/^parse_args "\$@"/p' "$SCRIPTS_DIR/setup-voip-network.sh" | \
        sed -e '$d' \
            -e 's/^set -e$/# set -e  # disabled for testing/' \
            -e 's|^source "\$SCRIPT_DIR/common.sh"$|# common.sh already sourced|' > "$temp_network"

    source "$temp_network"

    # Override paths to use test environment
    PROJECT_DIR="$TEST_TEMP_DIR"
    SCRIPT_DIR="$SCRIPTS_DIR"
}

# Load migrate.sh's fetch_dbscheme() and write_alembic_ini() functions
# without running the rest of the script (dbscheme pin resolution via
# versions.lock/python3, docker container checks, actual migrations).
# migrate.sh has no main() wrapper — it is top-to-bottom procedural — so
# instead of stripping a trailing invocation line, we extract each target
# function verbatim by its "name() {" ... "^}" boundaries. This fails loudly
# (empty extraction) if either function is ever renamed or restructured so
# its closing brace no longer sits at column 0.
load_migrate_functions() {
    local temp_migrate="$TEST_TEMP_DIR/migrate_functions.sh"

    {
        echo 'log()  { echo "[migrate] $1"; }'
        echo 'err()  { echo "[migrate:ERROR] $1" >&2; }'
        awk '/^fetch_dbscheme\(\) \{/,/^}/' "$SCRIPTS_DIR/migrate.sh"
        awk '/^write_alembic_ini\(\) \{/,/^}/' "$SCRIPTS_DIR/migrate.sh"
    } > "$temp_migrate"

    if ! grep -q '^fetch_dbscheme() {' "$temp_migrate" || ! grep -q '^write_alembic_ini() {' "$temp_migrate"; then
        echo "load_migrate_functions: failed to extract fetch_dbscheme/write_alembic_ini from migrate.sh" >&2
        return 1
    fi

    source "$temp_migrate"
}
