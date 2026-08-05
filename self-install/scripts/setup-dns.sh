#!/bin/bash
# VoIPBin Sandbox - DNS Setup Script
# Configures OS to use CoreDNS (port 53) for all DNS queries
#
# Linux: Points /etc/resolv.conf to CoreDNS (127.0.0.1)
# macOS: Uses /etc/resolver/ directory for .voipbin.test

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects which tree this script operates on — deliberate.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

# Check if CoreDNS container is running
check_coredns() {
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "voipbin-dns"; then
        return 0
    else
        return 1
    fi
}

# ============================================================================
# Linux (CoreDNS on port 53) configuration
# ============================================================================

# Backup file for original resolv.conf (overridable for bats — same pattern
# as $RESOLV_CONF/$RESOLV_UPSTREAMS in common.sh; needed so setup_linux()/
# uninstall_linux() can be exercised behaviorally in tests without touching
# real /etc)
RESOLV_BACKUP="${RESOLV_BACKUP:-/etc/resolv.conf.voipbin-backup}"

setup_linux() {
    local host_ip="$1"

    log_step "Configuring Linux DNS (CoreDNS on port 53)..."

    # Capture upstream fallback nameservers before anything else — must
    # happen before the cleanup/restart block below, which can itself
    # leave /run/systemd/resolve/resolv.conf momentarily stale if
    # systemd-resolved is mid-restart.
    #
    # $RESOLV_UPSTREAMS stores bare IPs, one per line (no "nameserver "
    # prefix — that's added later by write_resolv_conf_with_fallback).
    # The validity check below must match that format: any non-blank
    # line counts as a captured upstream. (An earlier version of this
    # guard checked for a "nameserver " prefix, which $RESOLV_UPSTREAMS
    # never contains — that guard was always true, so `refresh` mode was
    # unreachable and every re-run silently re-ran `initial`, which can
    # read source 2 (this script's own previously-written $RESOLV_CONF)
    # and recover a stale upstream instead of detecting a genuine change.)
    if [[ ! -s "$RESOLV_UPSTREAMS" ]] || ! grep -q '[^[:space:]]' "$RESOLV_UPSTREAMS"; then
        capture_dns_upstreams initial
    elif check_ip_changed; then
        capture_dns_upstreams refresh
    fi

    # Backup current resolv.conf if not already backed up. Must happen
    # before the cleanup block below so it captures pre-sandbox state, not
    # state a systemd-resolved/NetworkManager restart may have altered.
    if [[ ! -f "$RESOLV_BACKUP" ]]; then
        if [[ -L "$RESOLV_CONF" ]]; then
            # It's a symlink, save the target
            readlink "$RESOLV_CONF" > "$RESOLV_BACKUP"
            echo "symlink" >> "$RESOLV_BACKUP"
        elif [[ -f "$RESOLV_CONF" ]]; then
            # It's a file, copy it
            cp "$RESOLV_CONF" "$RESOLV_BACKUP"
        fi
        log_info "Backed up original resolv.conf"
    fi

    # Clean up old configurations — moved before the resolv.conf write
    # below. systemctl restart systemd-resolved/NetworkManager can
    # recreate $RESOLV_CONF as a symlink; running this first means our
    # write (which unlinks unconditionally) always wins the race instead
    # of possibly running before a restart that reverts it.
    if [[ -f /etc/systemd/resolved.conf.d/voipbin-sandbox.conf ]]; then
        rm -f /etc/systemd/resolved.conf.d/voipbin-sandbox.conf
        systemctl restart systemd-resolved 2>/dev/null || true
        log_info "Removed old systemd-resolved config"
    fi
    if [[ -f /etc/NetworkManager/dnsmasq.d/voipbin-sandbox.conf ]]; then
        rm -f /etc/NetworkManager/dnsmasq.d/voipbin-sandbox.conf
    fi
    if [[ -f /etc/NetworkManager/conf.d/dns-dnsmasq.conf ]]; then
        rm -f /etc/NetworkManager/conf.d/dns-dnsmasq.conf
        systemctl restart NetworkManager 2>/dev/null || true
    fi

    # Create resolv.conf pointing to CoreDNS, with fallback nameservers
    write_resolv_conf_with_fallback

    log_info "Configured $RESOLV_CONF → CoreDNS (127.0.0.1:53) with fallback"

    if systemctl is-active systemd-resolved &>/dev/null; then
        log_warn "systemd-resolved is active but no longer manages $RESOLV_CONF until 'sudo ./scripts/setup-dns.sh --uninstall' runs — a future systemd-resolved restart (unrelated to this script) may revert this file"
    fi

    log_info "DNS configured: all queries → CoreDNS (127.0.0.1:53), fallback on CoreDNS failure"
    log_info "CoreDNS forwards *.voipbin.test locally, others to fallback upstreams"
}

uninstall_linux() {
    log_step "Removing Linux DNS configuration..."

    # Restore original resolv.conf
    if [[ -f "$RESOLV_BACKUP" ]]; then
        local last_line=$(tail -1 "$RESOLV_BACKUP")
        if [[ "$last_line" == "symlink" ]]; then
            # Restore symlink
            local target=$(head -1 "$RESOLV_BACKUP")
            rm -f "$RESOLV_CONF"
            ln -s "$target" "$RESOLV_CONF"
            log_info "Restored resolv.conf symlink → $target"
        else
            # Restore file
            rm -f "$RESOLV_CONF"
            cp "$RESOLV_BACKUP" "$RESOLV_CONF"
            log_info "Restored original resolv.conf"
        fi
        rm -f "$RESOLV_BACKUP"
    else
        # No backup, create a sensible default
        rm -f "$RESOLV_CONF"
        cat > "$RESOLV_CONF" << 'EOF'
# Default DNS configuration
nameserver 8.8.8.8
nameserver 8.8.4.4
EOF
        log_info "Created default resolv.conf (no backup found)"
    fi

    rm -f "$RESOLV_UPSTREAMS"

    # Clean up any old configs
    if [[ -f /etc/systemd/resolved.conf.d/voipbin-sandbox.conf ]]; then
        rm -f /etc/systemd/resolved.conf.d/voipbin-sandbox.conf
        systemctl restart systemd-resolved 2>/dev/null || true
    fi
    if [[ -f /etc/NetworkManager/dnsmasq.d/voipbin-sandbox.conf ]]; then
        rm -f /etc/NetworkManager/dnsmasq.d/voipbin-sandbox.conf
    fi
    if [[ -f /etc/NetworkManager/conf.d/dns-dnsmasq.conf ]]; then
        rm -f /etc/NetworkManager/conf.d/dns-dnsmasq.conf
        systemctl restart NetworkManager 2>/dev/null || true
    fi

    log_info "DNS configuration removed"
}

# ============================================================================
# macOS (/etc/resolver/) configuration
# ============================================================================

setup_macos() {
    local host_ip="$1"

    log_step "Configuring macOS DNS forwarding..."

    # Create resolver directory if it doesn't exist
    mkdir -p /etc/resolver

    # Create resolver file for voipbin.test
    cat > /etc/resolver/voipbin.test << 'EOF'
# VoIPBin Sandbox - Forward .voipbin.test to CoreDNS
nameserver 127.0.0.1
port 53
EOF

    log_info "Created /etc/resolver/voipbin.test"

    # Flush DNS cache
    dscacheutil -flushcache 2>/dev/null || true
    killall -HUP mDNSResponder 2>/dev/null || true
    log_info "Flushed DNS cache"

    log_info "DNS forwarding configured: *.voipbin.test → CoreDNS (port 53)"
}

uninstall_macos() {
    log_step "Removing macOS DNS configuration..."

    if [[ -f /etc/resolver/voipbin.test ]]; then
        rm -f /etc/resolver/voipbin.test
        dscacheutil -flushcache 2>/dev/null || true
        killall -HUP mDNSResponder 2>/dev/null || true
        log_info "Removed /etc/resolver/voipbin.test"
    else
        log_info "No configuration found to remove"
    fi
}

# ============================================================================
# Test DNS resolution
# ============================================================================

test_dns() {
    local host_ip="$1"

    log_step "Testing DNS resolution..."

    sleep 2

    # Read external IPs from .env file
    local kamailio_ip=""

    if [[ -f "$PROJECT_DIR/.env" ]]; then
        kamailio_ip=$(grep '^KAMAILIO_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
    fi

    # Use defaults if not found
    [ -z "$kamailio_ip" ] && kamailio_ip="$host_ip"

    # Test domains with expected IPs
    # Web services resolve to HOST_IP (Docker port mapping handles routing)
    # SIP services resolve to KAMAILIO_IP
    local test_cases=(
        "api.voipbin.test:$host_ip"
        "admin.voipbin.test:$host_ip"
        "meet.voipbin.test:$host_ip"
        "talk.voipbin.test:$host_ip"
        "sip.voipbin.test:$kamailio_ip"
        "pstn.voipbin.test:$kamailio_ip"
        "registrar.voipbin.test:$kamailio_ip"
        "test.registrar.voipbin.test:$kamailio_ip"
    )

    local all_ok=true

    for test_case in "${test_cases[@]}"; do
        local domain="${test_case%%:*}"
        local expected="${test_case##*:}"
        local result=""

        # Test system DNS resolution
        if command -v dig &> /dev/null; then
            result=$(dig +short "$domain" 2>/dev/null | head -1)
        elif command -v nslookup &> /dev/null; then
            result=$(nslookup "$domain" 2>/dev/null | grep -A1 "Name:" | grep "Address" | awk '{print $2}' | head -1)
        fi

        if [[ "$result" == "$expected" ]]; then
            echo -e "  ${GREEN}✓${NC} $domain → $result"
        elif [[ -n "$result" ]]; then
            echo -e "  ${YELLOW}!${NC} $domain → $result (expected: $expected)"
            all_ok=false
        else
            echo -e "  ${RED}✗${NC} $domain → (no resolution)"
            all_ok=false
        fi
    done

    if [[ "$all_ok" == "true" ]]; then
        echo ""
        log_info "All DNS tests passed!"
    else
        echo ""
        log_warn "Some DNS tests failed. Make sure CoreDNS is running:"
        echo "  docker compose up -d coredns"
    fi
}

# ============================================================================
# Show status
# ============================================================================

show_status() {
    local host_ip="$1"
    local os="$2"

    echo ""
    echo "=============================================="
    echo "  DNS Setup Complete!"
    echo "=============================================="
    echo ""
    echo "Configuration:"
    echo "  OS:          $os"
    echo "  DNS Server:  CoreDNS (Docker, port 53)"
    echo "  Domain:      *.voipbin.test → $host_ip"
    echo ""
    echo "How it works:"
    if [[ "$os" == "linux" ]]; then
        echo "  CoreDNS runs on 127.0.0.1:53 (Docker container)"
        echo "  /etc/resolv.conf points to 127.0.0.1"
        echo "  CoreDNS: *.voipbin.test → $host_ip, others → 8.8.8.8"
        echo "  Original resolv.conf backed up to $RESOLV_BACKUP"
    else
        echo "  macOS resolver forwards .voipbin.test to CoreDNS"
        echo "  Config: /etc/resolver/voipbin.test"
    fi
    echo ""
    echo "Test with:"
    echo "  dig voipbin.test"
    echo "  ping registrar.voipbin.test"
    echo ""
    echo "To remove this configuration:"
    echo "  sudo $0 --uninstall"
    echo ""
}

# ============================================================================
# Main
# ============================================================================

regenerate_corefile() {
    local host_ip="$1"
    local force_update="${2:-false}"
    local ip_changed=false

    log_step "Regenerating CoreDNS configuration..."

    # Check if IP has changed and update .env if needed
    if check_ip_changed || [[ "$force_update" == "true" ]]; then
        local configured_ip=$(get_configured_host_ip)
        if [[ -n "$configured_ip" && "$configured_ip" != "$host_ip" ]]; then
            log_warn "Host IP changed: $configured_ip -> $host_ip"
            log_info "Updating .env with new IPs..."
            update_env_ips "$host_ip"
            ip_changed=true

            # Host IP actually changed — refresh the DNS fallback
            # upstreams too (a new gateway likely means a new upstream
            # DNS server). Deliberately scoped to this real-IP-change
            # branch, not the outer force_update guard: refreshing on
            # every manual --regenerate would downgrade a good captured
            # LAN upstream to the hardcoded 8.8.8.8/8.8.4.4 fallback for
            # no reason.
            capture_dns_upstreams refresh
            write_resolv_conf_with_fallback
        fi
    fi

    # Read external IPs from .env file (may have just been updated)
    local kamailio_ip=""
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        kamailio_ip=$(grep '^KAMAILIO_EXTERNAL_IP=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | head -1)
    fi

    # Use defaults if not found
    [ -z "$kamailio_ip" ] && kamailio_ip="$host_ip"

    log_info "Using IPs:"
    log_info "  Host IP:     $host_ip (web services)"
    log_info "  Kamailio IP: $kamailio_ip (SIP services)"

    # Generate Corefile using common function
    generate_coredns_config "$host_ip" "$PROJECT_DIR/config/coredns" "$kamailio_ip"

    log_info "Corefile regenerated"

    # Regenerate SSL certificate if IP changed
    if [[ "$ip_changed" == "true" ]]; then
        regenerate_ssl_certs "$host_ip"

        # Restart api-manager to pick up new certificate
        if docker ps --format '{{.Names}}' | grep -q "voipbin-api-mgr"; then
            log_info "Restarting api-manager to apply new certificate..."
            cd "$PROJECT_DIR" && docker compose rm -sf api-manager && docker compose up -d api-manager
        fi
    fi

    # Restart CoreDNS if running
    if check_coredns; then
        log_info "Restarting CoreDNS..."
        cd "$PROJECT_DIR" && docker compose restart coredns
        sleep 2
    fi
}

main() {
    local os=$(detect_os)
    local non_interactive=false

    # External-mode gate (§2.3): DNS is operator-managed, nothing to configure.
    # --uninstall stays mode-independent: leftover resolv.conf hijack state
    # (e.g. clean.sh --purge then init --mode external) must remain removable,
    # otherwise check-install.sh's external resolv.conf check fails with no
    # remedy.
    local want_uninstall=false
    local arg
    for arg in "$@"; do
        case "$arg" in
            --uninstall|-u) want_uninstall=true ;;
        esac
    done
    if [[ "$want_uninstall" != "true" && -f "$PROJECT_DIR/.env" ]]; then
        if [[ "$(get_domain_mode "$PROJECT_DIR/.env")" == "external" ]]; then
            log_info "external mode: DNS is operator-managed, skipping"
            exit 0
        fi
    fi

    # Parse arguments
    for arg in "$@"; do
        case "$arg" in
            --yes|-y)
                non_interactive=true
                ;;
            --uninstall|-u)
                check_root
                if [[ "$os" == "linux" ]]; then
                    uninstall_linux
                else
                    uninstall_macos
                fi
                exit 0
                ;;
            --test|-t)
                local host_ip=$(detect_host_ip)
                test_dns "$host_ip"
                exit 0
                ;;
            --regenerate|-r)
                check_root
                # Use fresh IP detection, not from .env
                local host_ip=$(detect_current_host_ip)
                regenerate_corefile "$host_ip" "true"
                test_dns "$host_ip"
                exit 0
                ;;
        esac
    done

    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - DNS Setup"
    echo "=============================================="

    if [[ "$os" == "unknown" ]]; then
        log_error "Unsupported operating system"
        exit 1
    fi

    check_root

    # Get host IP
    local host_ip=$(detect_host_ip)
    log_info "Detected OS: $os"
    log_info "Detected host IP: $host_ip"

    # Check if CoreDNS is running
    if ! check_coredns; then
        log_warn "CoreDNS container (voipbin-dns) is not running"
        if [[ "$non_interactive" == "true" ]]; then
            log_info "Starting CoreDNS..."
            cd "$PROJECT_DIR" && docker compose up -d coredns
            sleep 2
        else
            echo ""
            read -p "Start CoreDNS now? (Y/n): " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                log_info "Starting CoreDNS..."
                cd "$PROJECT_DIR" && docker compose up -d coredns
                sleep 2
            fi
        fi
    else
        log_info "CoreDNS is running"
    fi

    # Confirm with user (skip in non-interactive mode)
    if [[ "$non_interactive" != "true" ]]; then
        echo ""
        read -p "Configure *.voipbin.test DNS forwarding? (Y/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Nn]$ ]]; then
            log_info "Cancelled"
            exit 0
        fi
    fi

    # Setup based on OS
    if [[ "$os" == "linux" ]]; then
        setup_linux "$host_ip"
    else
        setup_macos "$host_ip"
    fi

    # Test and show status
    test_dns "$host_ip"
    show_status "$host_ip" "$os"
}

main "$@"
