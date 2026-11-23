#!/bin/bash
# VoIPBin Install - Cleanup Script
# Removes volumes, network interfaces, DNS config, and generated files
#
# Usage: sudo ./voipbin clean [OPTIONS]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Override-friendly for test isolation. Blast radius: an operator's exported
# PROJECT_DIR redirects this DESTRUCTIVE script (clean.sh --volumes) to a
# different tree — low probability, documented deliberately.
PROJECT_DIR="${PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"

# Source common functions
source "$SCRIPT_DIR/common.sh"

show_usage() {
    echo "Usage: voipbin> clean [OPTIONS]"
    echo ""
    echo "Cleanup options (can be combined):"
    echo "  --volumes   Remove docker volumes (database, recordings)"
    echo "  --images    Remove docker images"
    echo "  --network   Teardown VoIP network interfaces"
    echo "  --dns       Remove DNS configuration"
    echo "  --purge     Remove generated files (.env, certs, configs)"
    echo "  --all       All of the above (full reset to pre-init state)"
    echo "  -h, --help  Show this help message"
    echo ""
    echo "Examples:"
    echo "  voipbin> clean --volumes            # Remove only docker volumes"
    echo "  voipbin> clean --images             # Remove only docker images"
    echo "  voipbin> clean --purge              # Remove only .env/certs/configs"
    echo "  voipbin> clean --volumes --purge    # Remove volumes and generated files"
    echo "  voipbin> clean --all                # Full reset to pre-init state"
    echo ""
    echo "Note: Services will be stopped automatically if running."
    echo ""
}

CLEAN_VOLUMES=false
CLEAN_IMAGES=false
TEARDOWN_NETWORK=false
TEARDOWN_DNS=false
PURGE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --volumes)
            CLEAN_VOLUMES=true
            shift
            ;;
        --images)
            CLEAN_IMAGES=true
            shift
            ;;
        --network)
            TEARDOWN_NETWORK=true
            shift
            ;;
        --dns)
            TEARDOWN_DNS=true
            shift
            ;;
        --purge)
            PURGE=true
            shift
            ;;
        --all)
            CLEAN_VOLUMES=true
            CLEAN_IMAGES=true
            TEARDOWN_NETWORK=true
            TEARDOWN_DNS=true
            PURGE=true
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Check if at least one option was specified
if [ "$CLEAN_VOLUMES" = false ] && [ "$CLEAN_IMAGES" = false ] && [ "$TEARDOWN_NETWORK" = false ] && [ "$TEARDOWN_DNS" = false ] && [ "$PURGE" = false ]; then
    log_error "No cleanup option specified."
    echo ""
    show_usage
    exit 1
fi

# Removing the stack through Komodo (the plain "down" of an adopted install).
# Only a Komodo that cannot be reached falls back to removing the containers
# directly; any other failure is reported as it is.
clean_down_adopted() {
    local rc=0
    compose_exec down || rc=$?
    if [ "$rc" -eq 21 ]; then
        log_warn "Komodo is not available, removing the containers directly"
        docker compose down
    elif [ "$rc" -ne 0 ]; then
        return "$rc"
    fi
}

# Komodo teardown for --purge: the periphery service, its files, the hook and
# the Komodo containers and data (komodo-setup.sh --teardown does the work).
clean_komodo_teardown() {
    local setup="$SCRIPT_DIR/komodo-setup.sh"
    if [ ! -f "$setup" ]; then
        log_warn "komodo-setup.sh not found, Komodo was not removed"
        return 0
    fi
    if [ "$EUID" -eq 0 ]; then
        "$setup" --teardown || log_warn "Komodo teardown failed. Run: sudo ./scripts/komodo-setup.sh --teardown"
    elif sudo -n true 2>/dev/null; then
        sudo "$setup" --teardown || log_warn "Komodo teardown failed. Run: sudo ./scripts/komodo-setup.sh --teardown"
    else
        log_warn "Requires sudo. Run:"
        echo "    sudo ./scripts/komodo-setup.sh --teardown"
    fi
}

main() {
    echo ""
    echo "=============================================="
    echo "  VoIPBin Install - Cleanup"
    echo "=============================================="

    cd "$PROJECT_DIR"

    # Decided once: --purge removes the files this decision is read from.
    local clean_mode
    clean_mode=$(komodo_mode)
    if [ "$clean_mode" != "none" ]; then
        # Direct compose calls below must see the same inputs as Komodo does.
        compose_env_setup --readonly || true
    fi
    if [ "$clean_mode" = "adopted" ] && [ "$PURGE" = true ]; then
        log_warn "--purge also removes .env.komodo and the Komodo variables."
        log_warn "Without --volumes the database volume stays, but its password and keys are gone."
    fi

    # Step 1: Stop services and optionally remove volumes
    if docker compose ps -q 2>/dev/null | grep -q .; then
        log_step "Stopping running services..."
        if [ "$CLEAN_VOLUMES" = true ]; then
            docker compose down -v
        elif [ "$clean_mode" = "adopted" ]; then
            clean_down_adopted
        else
            docker compose down
        fi
    elif [ "$CLEAN_VOLUMES" = true ]; then
        # Services not running but we need to remove volumes
        log_step "Removing docker volumes..."
        docker compose down -v 2>/dev/null || true
    fi

    # The test-data marker mirrors DB state, which lives in the volumes — so
    # --volumes removes it too, letting start.sh re-seed the test customer
    # against a fresh database (§2.7).
    if [ "$CLEAN_VOLUMES" = true ] && [ -f "$PROJECT_DIR/.test_data_initialized" ]; then
        log_info "Removing test data marker (.test_data_initialized)..."
        rm -f "$PROJECT_DIR/.test_data_initialized"
    fi
    # The guided-install completion marker also describes DB state (the
    # admin account), so it goes with --volumes and with --purge.
    if { [ "$CLEAN_VOLUMES" = true ] || [ "$PURGE" = true ]; } && [ -f "$PROJECT_DIR/.voipbin-installed" ]; then
        log_info "Removing install marker (.voipbin-installed)..."
        rm -f "$PROJECT_DIR/.voipbin-installed"
    fi

    # Step 2: Remove docker images if requested
    if [ "$CLEAN_IMAGES" = true ]; then
        log_step "Removing docker images..."

        # Get images from docker compose config (if compose file exists)
        local compose_images=""
        if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
            compose_images=$(docker compose config --images 2>/dev/null || true)
        fi

        # Also look for voipbin images directly
        local voipbin_images=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^voipbin/' 2>/dev/null || true)

        # Look for other sandbox-related images
        local other_images=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^(mysql|redis|rabbitmq|coredns/)' 2>/dev/null || true)

        # Combine and deduplicate
        local all_images=$(echo -e "${compose_images}\n${voipbin_images}\n${other_images}" | sort -u | grep -v '^$' | grep -v '<none>')

        local removed_count=0
        if [ -n "$all_images" ]; then
            for image in $all_images; do
                if docker image inspect "$image" &>/dev/null; then
                    log_info "Removing image: $image"
                    docker rmi -f "$image" 2>/dev/null || true
                    ((removed_count++)) || true
                fi
            done
        fi

        # Also try docker compose --rmi for any built images
        docker compose down --rmi all 2>/dev/null || true

        log_info "Removed $removed_count docker images"
    fi

    # Step 3: Teardown VoIP network interfaces if requested
    if [ "$TEARDOWN_NETWORK" = true ]; then
        log_step "Tearing down VoIP network interfaces..."
        if [ -f "$SCRIPT_DIR/teardown-voip-network.sh" ]; then
            if sudo -n true 2>/dev/null || [ "$EUID" -eq 0 ]; then
                "$SCRIPT_DIR/teardown-voip-network.sh" || true
            else
                log_warn "Requires sudo. Run:"
                echo "    voipbin> clean --network"
            fi
        else
            log_warn "teardown-voip-network.sh not found"
        fi
    fi

    # Step 4: Remove DNS configuration if requested
    if [ "$TEARDOWN_DNS" = true ]; then
        log_step "Removing DNS configuration..."
        if [ "$(get_domain_mode "$PROJECT_DIR/.env")" = "external" ]; then
            # External mode: DNS was never hijacked, nothing to remove (§2.3).
            log_info "External mode: DNS is operator-managed, skipping"
        elif [ -f "$SCRIPT_DIR/setup-dns.sh" ]; then
            if sudo -n true 2>/dev/null || [ "$EUID" -eq 0 ]; then
                "$SCRIPT_DIR/setup-dns.sh" --uninstall || true
            else
                log_warn "Requires sudo. Run:"
                echo "    voipbin> clean --dns"
            fi
        else
            log_warn "setup-dns.sh not found"
        fi
    fi

    # Step 5: Purge all generated files if requested
    if [ "$PURGE" = true ]; then
        log_step "Purging generated files..."

        # Remove certificates
        if [ -d "$PROJECT_DIR/certs" ]; then
            log_info "Removing certificates directory..."
            rm -rf "$PROJECT_DIR/certs"
        fi

        # Remove .env file
        if [ -f "$PROJECT_DIR/.env" ]; then
            log_info "Removing .env file..."
            rm -f "$PROJECT_DIR/.env"
        fi

        # Komodo leftovers: the rendered copy and the certificate state files
        if [ "$clean_mode" != "none" ]; then
            log_info "Removing .env.komodo and certificate state files..."
            rm -f "$PROJECT_DIR/.env.komodo" "$PROJECT_DIR/.certs-pending" \
                "$PROJECT_DIR/.certs-sync-pending" "$PROJECT_DIR/.voipbin-hook-token"
        fi

        # Remove live docker-compose.yml (generated from docker-compose.yml.dist
        # by init.sh; re-copied fresh on the next init.sh run), plus any
        # concurrency lock file (VOIP-1334, acquire_file_lock() in
        # common.sh) left next to it OR docker-compose.yml.dist -
        # bump-image-digest.sh/sync-compose-images.sh/generate-versions-lock.sh
        # default to the .dist targets, so a plain local/dev invocation (no
        # LOCK_FILE/COMPOSE_FILE/OUTPUT_FILE override) creates
        # docker-compose.yml.dist.flock, not docker-compose.yml.flock. purge
        # is meant to leave no generated artifact behind, and a leftover
        # zero-byte .flock is still a generated artifact even though it's
        # harmless to reuse across runs.
        if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
            log_info "Removing docker-compose.yml..."
            rm -f "$PROJECT_DIR/docker-compose.yml"
        fi
        rm -f "$PROJECT_DIR/docker-compose.yml.flock" "$PROJECT_DIR/docker-compose.yml.dist.flock"

        # Remove live versions.lock (generated from versions.lock.dist by
        # init.sh; re-copied fresh on the next init.sh run), plus any
        # concurrency lock file (VOIP-1334) left next to it OR
        # versions.lock.dist - same defaults-to-.dist reasoning as above.
        if [ -f "$PROJECT_DIR/versions.lock" ]; then
            log_info "Removing versions.lock..."
            rm -f "$PROJECT_DIR/versions.lock"
        fi
        rm -f "$PROJECT_DIR/versions.lock.flock" "$PROJECT_DIR/versions.lock.dist.flock"

        # Remove generated CoreDNS config
        if [ -d "$PROJECT_DIR/config/coredns" ]; then
            log_info "Removing CoreDNS config..."
            rm -rf "$PROJECT_DIR/config/coredns"
        fi

        # Remove generated Caddy config (VOIP-1325, --web-reverse-proxy)
        if [ -d "$PROJECT_DIR/config/caddy" ]; then
            log_info "Removing Caddy reverse proxy config..."
            rm -rf "$PROJECT_DIR/config/caddy"
        fi

        # Remove dummy GCP credentials
        if [ -f "$PROJECT_DIR/config/dummy-gcp-credentials.json" ]; then
            log_info "Removing dummy GCP credentials..."
            rm -f "$PROJECT_DIR/config/dummy-gcp-credentials.json"
        fi

        # Remove tmp directory contents
        if [ -d "$PROJECT_DIR/tmp" ]; then
            log_info "Removing tmp directory..."
            rm -rf "$PROJECT_DIR/tmp"
        fi

        # Komodo itself goes last: nothing above needs it any more
        if [ "$clean_mode" != "none" ]; then
            log_info "Removing Komodo..."
            clean_komodo_teardown
        fi
    fi

    echo ""
    echo "=============================================="
    echo "  Cleanup Complete!"
    echo "=============================================="
    echo ""

    # Build summary of what was done
    local actions=()
    [ "$CLEAN_VOLUMES" = true ] && actions+=("Docker volumes removed")
    [ "$CLEAN_IMAGES" = true ] && actions+=("Docker images removed")
    [ "$TEARDOWN_NETWORK" = true ] && actions+=("Network interfaces removed")
    [ "$TEARDOWN_DNS" = true ] && actions+=("DNS config removed")
    [ "$PURGE" = true ] && actions+=("Generated files purged (.env, docker-compose.yml, versions.lock, certs, configs)")

    for action in "${actions[@]}"; do
        log_info "$action"
    done

    echo ""
    if [ "$CLEAN_VOLUMES" = true ] && [ "$TEARDOWN_NETWORK" = false ] && \
       { [ -e /sys/class/net/kamailio-int ] || [ -e /sys/class/net/rtpengine-int ]; }; then
        log_warn "The VoIP network interfaces were kept. Before installing again, remove them: $0 --network"
    fi
    log_info "Run 'init' to initialize, then 'start' to begin."
    echo ""
}

main "$@"
