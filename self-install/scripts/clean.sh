#!/bin/bash
# VoIPBin Sandbox - Cleanup Script
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

main() {
    echo ""
    echo "=============================================="
    echo "  VoIPBin Sandbox - Cleanup"
    echo "=============================================="

    cd "$PROJECT_DIR"

    # Step 1: Stop services and optionally remove volumes
    if docker compose ps -q 2>/dev/null | grep -q .; then
        log_step "Stopping running services..."
        if [ "$CLEAN_VOLUMES" = true ]; then
            docker compose down -v
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

        # Remove generated CoreDNS config
        if [ -d "$PROJECT_DIR/config/coredns" ]; then
            log_info "Removing CoreDNS config..."
            rm -rf "$PROJECT_DIR/config/coredns"
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
    [ "$PURGE" = true ] && actions+=("Generated files purged (.env, certs, configs)")

    for action in "${actions[@]}"; do
        log_info "$action"
    done

    echo ""
    log_info "Run 'init' to initialize, then 'start' to begin."
    echo ""
}

main "$@"
