#!/usr/bin/env bash
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

print_header() {
  echo ""
  echo -e "${BLUE}${BOLD}$1${NC}"
  echo -e "${BLUE}$(printf '=%.0s' $(seq 1 ${#1}))${NC}"
}

print_success() { echo -e "  ${GREEN}✓${NC} $1"; }
print_warning() { echo -e "  ${YELLOW}!${NC} $1"; }
print_error()   { echo -e "  ${RED}✗${NC} $1"; }

generate_password() {
  openssl rand -base64 24 | tr -d '/+=' | head -c 32
}

generate_hex_key() {
  openssl rand -hex 32
}

escape_sed() { printf '%s' "$1" | sed -e 's/[&/|\\]/\\&/g'; }

# ── Banner ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}AI WhatsApp Agent — Setup${NC}"
echo "This script will configure your environment and install dependencies."
echo ""

# ── 1. Check prerequisites ──────────────────────────────
print_header "Checking prerequisites"

MISSING=()

if command -v node &>/dev/null; then
  print_success "Node.js $(node --version)"
else
  print_error "Node.js not found"
  MISSING+=("Node.js (https://nodejs.org)")
fi

if command -v pnpm &>/dev/null; then
  print_success "pnpm $(pnpm --version)"
else
  print_error "pnpm not found"
  MISSING+=("pnpm (npm install -g pnpm)")
fi

if command -v python3 &>/dev/null; then
  print_success "Python $(python3 --version 2>&1 | awk '{print $2}')"
else
  print_error "Python 3 not found"
  MISSING+=("Python 3.11+ (https://python.org)")
fi

if command -v uv &>/dev/null; then
  print_success "uv $(uv --version 2>&1 | awk '{print $2}')"
else
  print_error "uv not found"
  MISSING+=("uv (curl -LsSf https://astral.sh/uv/install.sh | sh)")
fi

if command -v docker &>/dev/null; then
  print_success "Docker $(docker --version 2>&1 | awk '{print $3}' | tr -d ',')"
else
  print_warning "Docker not found (needed for production, not required for dev)"
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  echo ""
  print_error "Missing required tools:"
  for tool in "${MISSING[@]}"; do
    echo "    - $tool"
  done
  echo ""
  echo "Install the missing tools and re-run this script."
  exit 1
fi

# ── 2. Create .env from template ────────────────────────
print_header "Environment configuration"

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

if [ -f "$ENV_FILE" ]; then
  echo ""
  echo -e "  ${YELLOW}A .env file already exists.${NC}"
  read -rp "  Overwrite it? (y/N): " OVERWRITE
  if [[ ! "$OVERWRITE" =~ ^[Yy]$ ]]; then
    print_warning "Keeping existing .env — skipping configuration"
    SKIP_ENV=true
  else
    SKIP_ENV=false
  fi
else
  SKIP_ENV=false
fi

if [ "$SKIP_ENV" = false ]; then
  if [ ! -f "$ENV_EXAMPLE" ]; then
    print_error ".env.example not found. Are you in the project root?"
    exit 1
  fi

  cp "$ENV_EXAMPLE" "$ENV_FILE"
  print_success "Copied .env.example → .env"

  # ── Required secrets ──────────────────────────────────
  echo ""
  echo -e "  ${BOLD}Required secrets${NC}"
  echo "  These are needed to run the project. Press Enter to auto-generate where possible."
  echo ""

  # Postgres password
  DEFAULT_PG_PASS=$(generate_password)
  read -rp "  POSTGRES_PASSWORD (Enter to auto-generate): " PG_PASS
  PG_PASS=${PG_PASS:-$DEFAULT_PG_PASS}

  # Redis password
  DEFAULT_REDIS_PASS=$(generate_password)
  read -rp "  REDIS_PASSWORD (Enter to auto-generate): " REDIS_PASS
  REDIS_PASS=${REDIS_PASS:-$DEFAULT_REDIS_PASS}

  # Gemini API key
  echo ""
  echo -e "  ${YELLOW}Get your Gemini API key at: https://aistudio.google.com/apikey${NC}"
  while true; do
    read -rsp "  GEMINI_API_KEY: " GEMINI_KEY
    echo
    if [ -n "$GEMINI_KEY" ]; then
      break
    fi
    print_error "Gemini API key is required"
  done

  # Inter-service auth keys
  echo ""
  echo "  Inter-service authentication keys (used internally between services)."
  DEFAULT_AI_KEY=$(generate_hex_key)
  read -rp "  AI_API_KEY (Enter to auto-generate): " AI_KEY
  AI_KEY=${AI_KEY:-$DEFAULT_AI_KEY}

  DEFAULT_WA_KEY=$(generate_hex_key)
  read -rp "  WHATSAPP_API_KEY (Enter to auto-generate): " WA_KEY
  WA_KEY=${WA_KEY:-$DEFAULT_WA_KEY}

  # Construct DATABASE_URL
  PG_USER="aiagent"
  PG_DB="aiagent"
  DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@localhost:5432/${PG_DB}"

  # Write values to .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(escape_sed "$PG_PASS")|" "$ENV_FILE"
  sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(escape_sed "$REDIS_PASS")|" "$ENV_FILE"
  sed -i "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=$(escape_sed "$GEMINI_KEY")|" "$ENV_FILE"
  sed -i "s|^AI_API_KEY=.*|AI_API_KEY=$(escape_sed "$AI_KEY")|" "$ENV_FILE"
  sed -i "s|^WHATSAPP_API_KEY=.*|WHATSAPP_API_KEY=$(escape_sed "$WA_KEY")|" "$ENV_FILE"
  sed -i "s|^DATABASE_URL=.*|DATABASE_URL=$(escape_sed "$DATABASE_URL")|" "$ENV_FILE"

  print_success "Required secrets configured"

  # ── Optional: Cloud API ───────────────────────────────
  echo ""
  read -rp "  Set up WhatsApp Cloud API (Meta)? (y/N): " SETUP_CLOUD
  if [[ "$SETUP_CLOUD" =~ ^[Yy]$ ]]; then
    echo ""
    echo -e "  ${YELLOW}Get these from the Meta Developer Console: https://developers.facebook.com${NC}"
    read -rp "  META_PHONE_NUMBER_ID: " META_PHONE
    read -rsp "  META_ACCESS_TOKEN: " META_TOKEN
    echo
    read -rsp "  META_APP_SECRET: " META_SECRET
    echo
    read -rp "  META_WEBHOOK_VERIFY_TOKEN (any string you choose): " META_WEBHOOK

    [ -n "$META_PHONE" ]   && sed -i "s|^META_PHONE_NUMBER_ID=.*|META_PHONE_NUMBER_ID=$(escape_sed "$META_PHONE")|" "$ENV_FILE"
    [ -n "$META_TOKEN" ]   && sed -i "s|^META_ACCESS_TOKEN=.*|META_ACCESS_TOKEN=$(escape_sed "$META_TOKEN")|" "$ENV_FILE"
    [ -n "$META_SECRET" ]  && sed -i "s|^META_APP_SECRET=.*|META_APP_SECRET=$(escape_sed "$META_SECRET")|" "$ENV_FILE"
    [ -n "$META_WEBHOOK" ] && sed -i "s|^META_WEBHOOK_VERIFY_TOKEN=.*|META_WEBHOOK_VERIFY_TOKEN=$(escape_sed "$META_WEBHOOK")|" "$ENV_FILE"

    print_success "Cloud API configured"
  else
    print_warning "Skipped Cloud API — you can set META_* vars in .env later"
  fi

  # ── Optional: Groq ────────────────────────────────────
  echo ""
  read -rp "  Set up Groq API for speech-to-text? (y/N): " SETUP_GROQ
  if [[ "$SETUP_GROQ" =~ ^[Yy]$ ]]; then
    echo -e "  ${YELLOW}Get your key at: https://console.groq.com/keys${NC}"
    read -rsp "  GROQ_API_KEY: " GROQ_KEY
    echo
    [ -n "$GROQ_KEY" ] && sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=$(escape_sed "$GROQ_KEY")|" "$ENV_FILE"
    print_success "Groq API configured"
  else
    print_warning "Skipped Groq — audio transcription will be unavailable"
  fi
fi

# ── 3. Install dependencies ─────────────────────────────
print_header "Installing dependencies"

echo "  Running pnpm install:all (Node + Python)..."
pnpm install:all

print_success "All dependencies installed"

# ── 4. Next steps ───────────────────────────────────────
print_header "Setup complete!"

echo ""
echo -e "  ${BOLD}Option A: Docker (recommended for production)${NC}"
echo "    docker compose up -d                               # core stack"
echo "    docker compose --profile dev up -d                 # + Adminer (DB GUI)"
echo "    docker compose --profile cloud up -d               # + WhatsApp Cloud API"
echo "    docker compose --profile dev --profile cloud up -d # everything"
echo ""
echo -e "  ${BOLD}Option B: Local development${NC}"
echo "    1. Start infrastructure:  docker compose up -d postgres redis"
echo "    2. Start AI API:          pnpm dev:server"
echo "    3. Start WhatsApp client: pnpm dev:whatsapp"
echo "    4. Start worker:          pnpm dev:queue"
echo ""
echo -e "  ${BOLD}Useful commands${NC}"
echo "    pnpm test          Run all tests"
echo "    pnpm lint          Check code style"
echo "    pnpm format        Format code"
echo ""
echo -e "  ${BOLD}Documentation${NC}"
echo "    AI API:     http://localhost:8000/docs"
echo "    WhatsApp:   http://localhost:3001/docs"
echo "    Cloud API:  http://localhost:3002/docs"
echo "    DB Admin:   http://localhost:8080 (requires --profile dev)"
echo ""
