#!/bin/bash
# init_local.sh - Bootstrap script for VoIPBin sandbox local development
# Creates test customer, access key, and extensions for SIP testing
# Uses only CLIs and APIs - NO direct database access

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
CUSTOMER_NAME="Test Customer"
CUSTOMER_EMAIL="test@example.com"
EXTENSION_2000_PASS="pass2000"
EXTENSION_3000_PASS="pass3000"
API_HOST="${API_HOST:-localhost}"
API_PORT="${API_PORT:-8443}"

echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}VoIPBin Sandbox Bootstrap Script${NC}"
echo -e "${GREEN}================================${NC}"
echo ""

# Check if docker compose is available
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: docker command not found${NC}"
    exit 1
fi

# Check if jq is available
if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq command not found. Please install jq.${NC}"
    exit 1
fi

# Check if services are running
echo -e "${YELLOW}Checking if services are running...${NC}"
if ! docker compose ps --format "{{.Name}}" | grep -q voipbin-db; then
    echo -e "${RED}Error: Services are not running. Please run 'docker compose up -d' first${NC}"
    exit 1
fi

# Wait for services to be healthy
echo -e "${YELLOW}Waiting for services to be healthy...${NC}"
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    DB_HEALTH=$(docker compose ps --format "{{.Status}}" db 2>/dev/null | grep -c "healthy" || echo "0")
    MQ_HEALTH=$(docker compose ps --format "{{.Status}}" rabbitmq 2>/dev/null | grep -c "healthy" || echo "0")
    REDIS_HEALTH=$(docker compose ps --format "{{.Status}}" redis 2>/dev/null | grep -c "healthy" || echo "0")

    if [ "$DB_HEALTH" -gt 0 ] && [ "$MQ_HEALTH" -gt 0 ] && [ "$REDIS_HEALTH" -gt 0 ]; then
        echo -e "${GREEN}All infrastructure services are healthy${NC}"
        break
    fi

    echo "Waiting for services... ($WAITED/$MAX_WAIT seconds)"
    sleep 5
    WAITED=$((WAITED + 5))
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo -e "${RED}Timeout waiting for services to be healthy${NC}"
    exit 1
fi

# Wait for API manager to be ready
echo -e "${YELLOW}Waiting for API manager to be ready...${NC}"
MAX_API_WAIT=30
API_WAITED=0
while [ $API_WAITED -lt $MAX_API_WAIT ]; do
    if curl -sk "https://${API_HOST}:${API_PORT}/docs/" > /dev/null 2>&1; then
        echo -e "${GREEN}API manager is ready${NC}"
        break
    fi
    echo "Waiting for API manager... ($API_WAITED/$MAX_API_WAIT seconds)"
    sleep 2
    API_WAITED=$((API_WAITED + 2))
done

if [ $API_WAITED -ge $MAX_API_WAIT ]; then
    echo -e "${RED}Timeout waiting for API manager${NC}"
    exit 1
fi

# Step 1: Check/Create test customer using customer-control CLI
echo ""
echo -e "${YELLOW}Step 1: Checking/Creating test customer...${NC}"

# Check if customer already exists using customer-control list
EXISTING_CUSTOMER=$(docker exec voipbin-customer-mgr /app/bin/customer-control customer list 2>&1 | grep -c "$CUSTOMER_EMAIL" || true)

if [ "$EXISTING_CUSTOMER" -gt 0 ]; then
    echo -e "${GREEN}Customer $CUSTOMER_EMAIL already exists${NC}"
else
    echo "Creating customer: $CUSTOMER_EMAIL"
    CREATE_RESULT=$(docker exec voipbin-customer-mgr /app/bin/customer-control customer create \
        --name "$CUSTOMER_NAME" \
        --email "$CUSTOMER_EMAIL" 2>&1)

    if echo "$CREATE_RESULT" | grep -q "Success"; then
        echo -e "${GREEN}Customer created successfully${NC}"
    else
        # Check if it's an "already exists" error
        if echo "$CREATE_RESULT" | grep -q "already exist"; then
            echo -e "${GREEN}Customer already exists${NC}"
        else
            echo -e "${RED}Failed to create customer:${NC}"
            echo "$CREATE_RESULT"
            exit 1
        fi
    fi
fi

# Step 2: Login to get JWT token
echo ""
echo -e "${YELLOW}Step 2: Logging in to get JWT token...${NC}"

LOGIN_RESPONSE=$(curl -sk -X POST "https://${API_HOST}:${API_PORT}/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\": \"$CUSTOMER_EMAIL\", \"password\": \"$CUSTOMER_EMAIL\"}" 2>&1)

TOKEN=$(echo "$LOGIN_RESPONSE" | jq -r '.token' 2>/dev/null)

if [ "$TOKEN" == "null" ] || [ -z "$TOKEN" ]; then
    echo -e "${RED}Failed to login. Response: $LOGIN_RESPONSE${NC}"
    exit 1
fi

echo -e "${GREEN}Login successful${NC}"

# Step 3: Get customer ID
echo ""
echo -e "${YELLOW}Step 3: Getting customer ID...${NC}"

CUSTOMER_INFO=$(curl -sk -X GET "https://${API_HOST}:${API_PORT}/v1.0/customer" \
    -H "Authorization: Bearer $TOKEN" 2>&1)

CUSTOMER_ID=$(echo "$CUSTOMER_INFO" | jq -r '.id' 2>/dev/null)

if [ "$CUSTOMER_ID" == "null" ] || [ -z "$CUSTOMER_ID" ]; then
    echo -e "${RED}Failed to get customer info${NC}"
    exit 1
fi

echo -e "${GREEN}Customer ID: $CUSTOMER_ID${NC}"

# Step 4: Create extensions via API
echo ""
echo -e "${YELLOW}Step 4: Creating SIP extensions...${NC}"

# Check existing extensions
EXISTING_EXTENSIONS=$(curl -sk -X GET "https://${API_HOST}:${API_PORT}/v1.0/extensions" \
    -H "Authorization: Bearer $TOKEN" 2>&1)

# Create extension 2000 if not exists
if echo "$EXISTING_EXTENSIONS" | jq -e '.result[] | select(.extension == "2000")' > /dev/null 2>&1; then
    echo -e "${GREEN}Extension 2000 already exists${NC}"
else
    RESULT=$(curl -sk -X POST "https://${API_HOST}:${API_PORT}/v1.0/extensions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $TOKEN" \
        -d "{\"extension\": \"2000\", \"password\": \"${EXTENSION_2000_PASS}\", \"name\": \"Extension 2000\"}" 2>&1)

    if echo "$RESULT" | jq -e '.id' > /dev/null 2>&1; then
        echo -e "${GREEN}Created extension 2000${NC}"
    else
        echo -e "${YELLOW}Extension 2000: $(echo "$RESULT" | head -c 100)${NC}"
    fi
fi

# Create extension 3000 if not exists
if echo "$EXISTING_EXTENSIONS" | jq -e '.result[] | select(.extension == "3000")' > /dev/null 2>&1; then
    echo -e "${GREEN}Extension 3000 already exists${NC}"
else
    RESULT=$(curl -sk -X POST "https://${API_HOST}:${API_PORT}/v1.0/extensions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $TOKEN" \
        -d "{\"extension\": \"3000\", \"password\": \"${EXTENSION_3000_PASS}\", \"name\": \"Extension 3000\"}" 2>&1)

    if echo "$RESULT" | jq -e '.id' > /dev/null 2>&1; then
        echo -e "${GREEN}Created extension 3000${NC}"
    else
        echo -e "${YELLOW}Extension 3000: $(echo "$RESULT" | head -c 100)${NC}"
    fi
fi

# Step 5: Create API key via API
echo ""
echo -e "${YELLOW}Step 5: Creating API access key...${NC}"

# Check existing access keys
EXISTING_KEYS=$(curl -sk -X GET "https://${API_HOST}:${API_PORT}/v1.0/accesskeys" \
    -H "Authorization: Bearer $TOKEN" 2>&1)

# Check if we already have an access key
EXISTING_KEY_TOKEN=$(echo "$EXISTING_KEYS" | jq -r '.result[0].token // empty' 2>/dev/null)

if [ -n "$EXISTING_KEY_TOKEN" ]; then
    API_TOKEN="$EXISTING_KEY_TOKEN"
    echo -e "${GREEN}Access key already exists${NC}"
else
    # Create new access key (expire = 1 year from now)
    EXPIRE=$(($(date +%s) + 31536000))

    ACCESSKEY_RESPONSE=$(curl -sk -X POST "https://${API_HOST}:${API_PORT}/v1.0/accesskeys" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $TOKEN" \
        -d "{\"name\": \"Sandbox API Key\", \"detail\": \"Auto-generated for local development\", \"expire\": $EXPIRE}" 2>&1)

    API_TOKEN=$(echo "$ACCESSKEY_RESPONSE" | jq -r '.token' 2>/dev/null)

    if [ "$API_TOKEN" == "null" ] || [ -z "$API_TOKEN" ]; then
        echo -e "${YELLOW}Failed to create API key (may already exist)${NC}"
        API_TOKEN="(use JWT token or check existing keys)"
    else
        echo -e "${GREEN}Created API access key${NC}"
    fi
fi

# Print summary
echo ""
echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}Bootstrap Complete!${NC}"
echo -e "${GREEN}================================${NC}"
echo ""
echo "Customer ID: ${CUSTOMER_ID}"
echo "Admin Login: ${CUSTOMER_EMAIL} / ${CUSTOMER_EMAIL}"
echo "API Token:   ${API_TOKEN}"
echo ""
echo "SIP Registration Details:"
echo "========================="
echo "SIP Domain: ${CUSTOMER_ID}.registrar.localhost"
echo ""
echo "Extension 2000:"
echo "  Username: 2000"
echo "  Password: ${EXTENSION_2000_PASS}"
echo "  SIP URI:  sip:2000@${CUSTOMER_ID}.registrar.localhost"
echo ""
echo "Extension 3000:"
echo "  Username: 3000"
echo "  Password: ${EXTENSION_3000_PASS}"
echo "  SIP URI:  sip:3000@${CUSTOMER_ID}.registrar.localhost"
echo ""
echo "To register your softphone:"
echo "  Server:   192.168.45.152:5060 (or your host IP)"
echo "  Domain:   ${CUSTOMER_ID}.registrar.localhost"
echo "  Username: 2000 (or 3000)"
echo "  Password: pass2000 (or pass3000)"
echo ""
echo "Test with softphone.py:"
echo "  python3 scripts/softphone.py 2000 pass2000 --customer-id ${CUSTOMER_ID}"
echo ""
echo "API Usage:"
echo "  curl -sk 'https://localhost:8443/v1.0/extensions' -H 'Authorization: Bearer <jwt_token>'"
echo "  curl -sk 'https://localhost:8443/v1.0/extensions?token=${API_TOKEN}'"
echo ""
