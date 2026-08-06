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

# Strip CR/LF from user-pasted input (common when copying tokens from the clipboard).
sanitize() { printf '%s' "${1//$'\r'/}" | tr -d '\n'; }

# Return 0 if a TCP port is currently bound on the host.
# ss/lsof check all interfaces; the bash /dev/tcp fallback only probes 127.0.0.1
# (best-effort on minimal hosts that have neither tool — may miss a port bound to
# a non-loopback interface). A probe that errors out is treated as "free".
port_in_use() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -Hltn "( sport = :$port )" 2>/dev/null | grep -q . && return 0
    return 1
  elif command -v lsof &>/dev/null; then
    lsof -iTCP:"$port" -sTCP:LISTEN -t &>/dev/null && return 0
    return 1
  else
    # Fallback: a successful connect means something is listening.
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
    return 1
  fi
}

# Find the first free port at/above $1 (scans up to +100), skipping ports already
# claimed this run (tracked in $CHOSEN_PORTS). On success sets the global FREE_PORT
# and returns 0. Sets a global (not stdout) so calls aren't run in a subshell —
# otherwise CHOSEN_PORTS would not accumulate and two services could be assigned
# the same bumped port.
CHOSEN_PORTS=""
FREE_PORT=""
find_free_port() {
  local port="$1" max=$(( $1 + 100 ))
  while [ "$port" -le "$max" ]; do
    if [[ " $CHOSEN_PORTS " != *" $port "* ]] && ! port_in_use "$port"; then
      CHOSEN_PORTS="$CHOSEN_PORTS $port"
      FREE_PORT="$port"
      return 0
    fi
    port=$(( port + 1 ))
  done
  return 1
}

# Compare semantic versions with sort -V; returns 0 if $2 >= $3.
check_min_version() {
  local name="$1" current="$2" minimum="$3"
  if [ "$(printf '%s\n%s\n' "$minimum" "$current" | sort -V | head -n1)" != "$minimum" ]; then
    print_error "$name $current found, but $minimum+ required"
    MISSING+=("$name $minimum+")
    return 1
  fi
  return 0
}

# ── Banner ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}AI WhatsApp Agent — Setup${NC}"
echo "This script will configure your environment and install dependencies."
echo "Note: targets Linux (uses GNU sed). On macOS, configure .env manually."
echo ""

if [[ "$(uname)" == "Darwin" ]]; then
  print_error "macOS is not supported (script requires GNU sed). Configure .env manually."
  exit 1
fi

# ── 1. Check prerequisites ──────────────────────────────
print_header "Checking prerequisites"

MISSING=()

if command -v node &>/dev/null; then
  NODE_VER=$(node --version 2>&1 | sed 's/^v//')
  print_success "Node.js $(node --version)"
  check_min_version "Node.js" "$NODE_VER" "18.0.0" || true
else
  print_error "Node.js not found"
  MISSING+=("Node.js 18+ (https://nodejs.org)")
fi

if command -v pnpm &>/dev/null; then
  print_success "pnpm $(pnpm --version)"
else
  print_error "pnpm not found"
  MISSING+=("pnpm (npm install -g pnpm)")
fi

if command -v python3 &>/dev/null; then
  PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
  print_success "Python $PY_VER"
  check_min_version "Python" "$PY_VER" "3.11.0" || true
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

if command -v openssl &>/dev/null; then
  print_success "openssl $(openssl version 2>&1 | awk '{print $2}')"
else
  print_error "openssl not found"
  MISSING+=("openssl (required for secret generation)")
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
# Ports we couldn't free (declared here so the always-run final summary can read it).
PORT_ERRORS=()

if [ -f "$ENV_FILE" ]; then
  echo ""
  echo -e "  ${YELLOW}A .env file already exists.${NC}"
  read -rp "  Overwrite it? (y/N): " OVERWRITE
  if [[ ! "$OVERWRITE" =~ ^[Yy]$ ]]; then
    print_warning "Keeping existing .env — skipping configuration"
    print_warning "Verify your .env contains all required keys (diff against .env.example)"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
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
  chmod 600 "$ENV_FILE"
  print_success "Copied .env.example → .env (mode 600)"

  # Fail fast if .env.example is missing any key this script writes to —
  # otherwise sed substitutions silently no-op and the user ends up with a
  # half-configured .env only discovered at runtime.
  REQUIRED_KEYS=(
    POSTGRES_PASSWORD REDIS_PASSWORD GEMINI_API_KEY AI_API_KEY
    WHATSAPP_API_KEY DATABASE_URL GROQ_API_KEY LLAMA_CLOUD_API_KEY
    META_PHONE_NUMBER_ID META_ACCESS_TOKEN META_APP_SECRET META_WEBHOOK_VERIFY_TOKEN
    STT_PROVIDER WHISPER_MODEL WHISPER_TIMEOUT_SECONDS INSTALL_DOCLING
    TELEGRAM_BOT_TOKEN TELEGRAM_WEBHOOK_SECRET
    SERVICE_NAME COMPOSE_PROJECT_NAME
    POSTGRES_PORT REDIS_PORT ADMINER_PORT AI_API_PORT WHISPER_PORT
    WHATSAPP_API_PORT WHATSAPP_CLOUD_PORT TELEGRAM_PORT
    WHATSAPP_CLIENT_URL WHATSAPP_CLOUD_CLIENT_URL TELEGRAM_CLIENT_URL
  )
  MISSING_KEYS=()
  for key in "${REQUIRED_KEYS[@]}"; do
    grep -q "^${key}=" "$ENV_EXAMPLE" || MISSING_KEYS+=("$key")
  done
  if [ ${#MISSING_KEYS[@]} -gt 0 ]; then
    print_error ".env.example is missing expected keys: ${MISSING_KEYS[*]}"
    print_error "Template drift detected — update .env.example or setup.sh"
    exit 1
  fi

  # ── Project identity & ports ──────────────────────────
  echo ""
  echo -e "  ${BOLD}Project identity${NC}"
  echo "  Names every Docker container (NAME-postgres, …), the network, and the"
  echo "  API image tag. Use lowercase letters, digits, '-' or '_'."
  echo ""
  while true; do
    read -rp "  SERVICE_NAME (Enter for 'aiagent'): " SERVICE_NAME
    SERVICE_NAME=$(sanitize "${SERVICE_NAME:-aiagent}")
    SERVICE_NAME=$(printf '%s' "$SERVICE_NAME" | tr '[:upper:]' '[:lower:]')
    if [[ "$SERVICE_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
      break
    fi
    print_error "Invalid name — must start with a letter/digit and contain only a-z 0-9 _ -"
  done
  sed -i "s|^SERVICE_NAME=.*|SERVICE_NAME=$(escape_sed "$SERVICE_NAME")|" "$ENV_FILE"
  sed -i "s|^COMPOSE_PROJECT_NAME=.*|COMPOSE_PROJECT_NAME=$(escape_sed "$SERVICE_NAME")|" "$ENV_FILE"
  print_success "Service name set to '$SERVICE_NAME'"

  # ── Port conflict detection ───────────────────────────
  # Each entry is VAR:DEFAULT. The .env value is the Docker host-published port
  # (and, for the client *_PORT vars, the local-dev bind port); containers stay
  # pinned to canonical internal ports in docker-compose.yml. We auto-bump any
  # port already in use on the host.
  echo ""
  echo -e "  ${BOLD}Port check${NC}"
  if ! command -v ss &>/dev/null && ! command -v lsof &>/dev/null; then
    print_warning "Neither 'ss' nor 'lsof' found — port probing is best-effort"
    print_warning "(loopback only); a conflict on a non-loopback interface may be missed"
  fi
  PORT_SPECS=(
    "POSTGRES_PORT:5432"
    "REDIS_PORT:6379"
    "ADMINER_PORT:8080"
    "AI_API_PORT:8000"
    "WHATSAPP_API_PORT:3001"
    "WHATSAPP_CLOUD_PORT:3002"
    "TELEGRAM_PORT:3003"
    "WHISPER_PORT:8771"
  )
  PORT_CHANGES=0
  # Captured to rebuild DATABASE_URL and the client callback URLs below.
  PG_PORT=5432
  WA_PORT=3001
  WA_CLOUD_PORT=3002
  TG_PORT=3003
  for spec in "${PORT_SPECS[@]}"; do
    var="${spec%%:*}"
    default="${spec##*:}"
    if find_free_port "$default"; then
      chosen="$FREE_PORT"
    else
      print_error "Could not find a free port near $default for $var — set $var manually in .env"
      PORT_ERRORS+=("$var (default $default)")
      chosen="$default"
    fi
    sed -i "s|^${var}=.*|${var}=${chosen}|" "$ENV_FILE"
    if [ "$chosen" != "$default" ]; then
      print_warning "Port $default in use → ${var}=${chosen}"
      PORT_CHANGES=$((PORT_CHANGES + 1))
    fi
    case "$var" in
      POSTGRES_PORT) PG_PORT="$chosen" ;;
      WHATSAPP_API_PORT) WA_PORT="$chosen" ;;
      WHATSAPP_CLOUD_PORT) WA_CLOUD_PORT="$chosen" ;;
      TELEGRAM_PORT) TG_PORT="$chosen" ;;
    esac
  done

  # Keep the inter-service callback URLs the AI API reads in sync with the chosen
  # client ports, so local (non-Docker) dev works after a bump. In Docker these
  # are overridden in docker-compose.yml, so this only affects local dev.
  # AI_API_URL is intentionally left at :8000 — the local API always binds 8000.
  sed -i "s|^WHATSAPP_CLIENT_URL=.*|WHATSAPP_CLIENT_URL=$(escape_sed "http://localhost:${WA_PORT}")|" "$ENV_FILE"
  sed -i "s|^WHATSAPP_CLOUD_CLIENT_URL=.*|WHATSAPP_CLOUD_CLIENT_URL=$(escape_sed "http://localhost:${WA_CLOUD_PORT}")|" "$ENV_FILE"
  sed -i "s|^TELEGRAM_CLIENT_URL=.*|TELEGRAM_CLIENT_URL=$(escape_sed "http://localhost:${TG_PORT}")|" "$ENV_FILE"

  if [ "$PORT_CHANGES" -eq 0 ]; then
    print_success "All default ports are free"
  else
    print_success "Reassigned $PORT_CHANGES port(s) to avoid conflicts"
  fi

  # ── Required secrets ──────────────────────────────────
  echo ""
  echo -e "  ${BOLD}Required secrets${NC}"
  echo "  These are needed to run the project. Press Enter to auto-generate where possible."
  echo ""

  # Postgres password
  DEFAULT_PG_PASS=$(generate_password)
  read -rp "  POSTGRES_PASSWORD (Enter to auto-generate): " PG_PASS
  PG_PASS=$(sanitize "${PG_PASS:-$DEFAULT_PG_PASS}")

  # Redis password
  DEFAULT_REDIS_PASS=$(generate_password)
  read -rp "  REDIS_PASSWORD (Enter to auto-generate): " REDIS_PASS
  REDIS_PASS=$(sanitize "${REDIS_PASS:-$DEFAULT_REDIS_PASS}")

  # Gemini API key
  echo ""
  echo -e "  ${YELLOW}Get your Gemini API key at: https://aistudio.google.com/apikey${NC}"
  while true; do
    read -rsp "  GEMINI_API_KEY: " GEMINI_KEY
    echo
    GEMINI_KEY=$(sanitize "$GEMINI_KEY")
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
  AI_KEY=$(sanitize "${AI_KEY:-$DEFAULT_AI_KEY}")

  DEFAULT_WA_KEY=$(generate_hex_key)
  read -rp "  WHATSAPP_API_KEY (Enter to auto-generate): " WA_KEY
  WA_KEY=$(sanitize "${WA_KEY:-$DEFAULT_WA_KEY}")

  # Construct DATABASE_URL — read defaults from .env.example so customizing
  # POSTGRES_USER/POSTGRES_DB there stays in sync.
  PG_USER=$(grep -E '^POSTGRES_USER=' "$ENV_EXAMPLE" | cut -d= -f2-)
  PG_DB=$(grep -E '^POSTGRES_DB=' "$ENV_EXAMPLE" | cut -d= -f2-)
  PG_USER="${PG_USER:-aiagent}"
  PG_DB="${PG_DB:-aiagent}"
  # Use the chosen host port (PG_PORT) so local (non-Docker) dev connects to the
  # published postgres. In Docker, the api/worker override DATABASE_URL to
  # postgres:5432 internally, so this only affects host-side connections.
  DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@localhost:${PG_PORT}/${PG_DB}"

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
    META_PHONE=$(sanitize "$META_PHONE")
    read -rsp "  META_ACCESS_TOKEN: " META_TOKEN
    echo
    META_TOKEN=$(sanitize "$META_TOKEN")
    read -rsp "  META_APP_SECRET: " META_SECRET
    echo
    META_SECRET=$(sanitize "$META_SECRET")
    DEFAULT_META_WEBHOOK=$(generate_hex_key)
    read -rp "  META_WEBHOOK_VERIFY_TOKEN (Enter to auto-generate; paste this into the Meta dashboard): " META_WEBHOOK
    META_WEBHOOK=$(sanitize "${META_WEBHOOK:-$DEFAULT_META_WEBHOOK}")

    META_WRITTEN=0
    write_if_set() {
      local key="$1" value="$2"
      if [ -n "$value" ]; then
        sed -i "s|^${key}=.*|${key}=$(escape_sed "$value")|" "$ENV_FILE"
        META_WRITTEN=$((META_WRITTEN + 1))
      else
        print_warning "$key left empty — Cloud API will not be fully functional"
      fi
    }
    write_if_set META_PHONE_NUMBER_ID     "$META_PHONE"
    write_if_set META_ACCESS_TOKEN        "$META_TOKEN"
    write_if_set META_APP_SECRET          "$META_SECRET"
    write_if_set META_WEBHOOK_VERIFY_TOKEN "$META_WEBHOOK"

    if [ "$META_WRITTEN" -eq 4 ]; then
      print_success "Cloud API configured"
    else
      print_warning "Cloud API partially configured ($META_WRITTEN/4 fields set)"
    fi
  else
    print_warning "Skipped Cloud API — you can set META_* vars in .env later"
  fi

  # ── Optional: Telegram ────────────────────────────────
  echo ""
  read -rp "  Set up Telegram Bot? (y/N): " SETUP_TG
  if [[ "$SETUP_TG" =~ ^[Yy]$ ]]; then
    echo ""
    echo -e "  ${YELLOW}Create a bot with @BotFather on Telegram; it will give you a token like 123:ABC.${NC}"
    read -rsp "  TELEGRAM_BOT_TOKEN: " TG_TOKEN
    echo
    TG_TOKEN=$(sanitize "$TG_TOKEN")
    DEFAULT_TG_SECRET=$(generate_hex_key)
    read -rp "  TELEGRAM_WEBHOOK_SECRET (Enter to auto-generate): " TG_SECRET
    TG_SECRET=$(sanitize "${TG_SECRET:-$DEFAULT_TG_SECRET}")
    read -rp "  TELEGRAM_PUBLIC_WEBHOOK_URL (public https URL ending in /webhook, leave empty to skip setWebhook): " TG_URL
    TG_URL=$(sanitize "$TG_URL")

    if [ -n "$TG_TOKEN" ]; then
      sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$(escape_sed "$TG_TOKEN")|" "$ENV_FILE"
      sed -i "s|^TELEGRAM_WEBHOOK_SECRET=.*|TELEGRAM_WEBHOOK_SECRET=$(escape_sed "$TG_SECRET")|" "$ENV_FILE"
      if [ -n "$TG_URL" ]; then
        sed -i "s|^TELEGRAM_PUBLIC_WEBHOOK_URL=.*|TELEGRAM_PUBLIC_WEBHOOK_URL=$(escape_sed "$TG_URL")|" "$ENV_FILE"
      fi
      print_success "Telegram configured"
      print_warning "Start the container with: docker compose --profile telegram up -d"
      print_warning "In @BotFather: /setprivacy → Disable, so the bot can see group messages"
    else
      print_warning "TELEGRAM_BOT_TOKEN left empty — Telegram client will not start"
    fi
  else
    print_warning "Skipped Telegram — set TELEGRAM_* vars in .env when ready"
  fi

  # ── Optional: LlamaParse ──────────────────────────────
  LLAMA_CONFIGURED=0
  USE_DOCLING=0
  echo ""
  read -rp "  Set up LlamaParse for PDF parsing (primary parser)? (y/N): " SETUP_LLAMA
  if [[ "$SETUP_LLAMA" =~ ^[Yy]$ ]]; then
    echo -e "  ${YELLOW}Get your key at: https://cloud.llamaindex.ai (free tier: 1000 pages/day)${NC}"
    read -rsp "  LLAMA_CLOUD_API_KEY: " LLAMA_KEY
    echo
    LLAMA_KEY=$(sanitize "$LLAMA_KEY")
    if [ -n "$LLAMA_KEY" ]; then
      sed -i "s|^LLAMA_CLOUD_API_KEY=.*|LLAMA_CLOUD_API_KEY=$(escape_sed "$LLAMA_KEY")|" "$ENV_FILE"
      print_success "LlamaParse configured"
      LLAMA_CONFIGURED=1
    else
      print_warning "LLAMA_CLOUD_API_KEY left empty"
    fi
  else
    print_warning "Skipped LlamaParse"
  fi

  # ── Optional: Docling (local PDF parser) ──────────────
  echo ""
  if [ "$LLAMA_CONFIGURED" -eq 1 ]; then
    echo "  Docling is a local PDF parser — slower than LlamaParse but no API key, no network."
    echo "  With both configured, PDF_PARSER=auto falls back to Docling on LlamaParse errors."
    read -rp "  Also set up Docling as a local fallback? (y/N): " SETUP_DOCLING
  else
    echo "  No LlamaParse key set. Docling is a local PDF parser (no API key, no network)."
    echo "  Trade-offs: ~1 GB larger Docker image, slower first build, CPU-heavy parsing."
    read -rp "  Set up Docling as your PDF parser? (y/N): " SETUP_DOCLING
  fi
  if [[ "$SETUP_DOCLING" =~ ^[Yy]$ ]]; then
    USE_DOCLING=1
    sed -i "s|^INSTALL_DOCLING=.*|INSTALL_DOCLING=true|" "$ENV_FILE"
    if [ "$LLAMA_CONFIGURED" -eq 0 ]; then
      sed -i "s|^PDF_PARSER=.*|PDF_PARSER=docling|" "$ENV_FILE"
      print_success "Docling enabled as sole PDF parser (PDF_PARSER=docling)"
    else
      print_success "Docling enabled as auto-fallback (PDF_PARSER=auto)"
    fi
    echo ""
    echo -e "  ${YELLOW}Docling needs system packages for local (non-Docker) dev:${NC}"
    if command -v apt-get &>/dev/null; then
      echo "    sudo apt-get install -y poppler-utils tesseract-ocr libmagic1"
    elif command -v dnf &>/dev/null; then
      echo "    sudo dnf install -y poppler-utils tesseract file-libs"
    elif command -v pacman &>/dev/null; then
      echo "    sudo pacman -S --needed poppler tesseract file"
    elif command -v brew &>/dev/null; then
      echo "    brew install poppler tesseract libmagic"
    else
      echo "    Install poppler-utils, tesseract-ocr, and libmagic via your package manager."
    fi
    print_warning "Docker builds install these automatically; only needed for 'pnpm dev:server'."
  else
    if [ "$LLAMA_CONFIGURED" -eq 0 ]; then
      print_warning "No PDF parser configured — PDF uploads will fail until LLAMA_CLOUD_API_KEY is set or Docling is enabled"
    else
      print_warning "Skipped Docling — install later with: (cd packages/ai-api && uv sync --extra docling)"
    fi
  fi

  # ── Optional: Groq ────────────────────────────────────
  echo ""
  read -rp "  Set up Groq API for speech-to-text? (y/N): " SETUP_GROQ
  GROQ_CONFIGURED=0
  if [[ "$SETUP_GROQ" =~ ^[Yy]$ ]]; then
    echo -e "  ${YELLOW}Get your key at: https://console.groq.com/keys${NC}"
    read -rsp "  GROQ_API_KEY: " GROQ_KEY
    echo
    GROQ_KEY=$(sanitize "$GROQ_KEY")
    if [ -n "$GROQ_KEY" ]; then
      sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=$(escape_sed "$GROQ_KEY")|" "$ENV_FILE"
      print_success "Groq API configured"
      GROQ_CONFIGURED=1
    else
      print_warning "GROQ_API_KEY left empty"
    fi
  else
    print_warning "Skipped Groq"
  fi

  # ── Optional: Self-hosted Whisper ─────────────────────
  echo ""
  echo "  Self-hosted Whisper runs in an opt-in Docker container (profile: whisper)."
  echo "  Not enabling it here means zero extra images/RAM — you can turn it on later."
  read -rp "  Enable self-hosted Whisper for speech-to-text? (y/N): " SETUP_WHISPER
  if [[ "$SETUP_WHISPER" =~ ^[Yy]$ ]]; then
    # Uncomment the in-cluster default. Host-side dev can switch to http://127.0.0.1:8771.
    sed -i "s|^# WHISPER_BASE_URL=.*|WHISPER_BASE_URL=http://whisper:8000|" "$ENV_FILE"
    print_success "Self-hosted Whisper enabled (WHISPER_BASE_URL=http://whisper:8000)"
    print_warning "Start the container with: docker compose --profile whisper up -d"
    echo "  First startup blocks ~1–2 min while the whisper-init sidecar pulls the model."
    if [ "$GROQ_CONFIGURED" -eq 0 ]; then
      echo "  With only self-hosted Whisper configured, STT_PROVIDER=auto will use it."
    else
      echo "  With both providers set, STT_PROVIDER=auto prefers Groq and falls back to self-hosted."
    fi
  else
    if [ "$GROQ_CONFIGURED" -eq 0 ]; then
      print_warning "No STT provider configured — audio transcription will return 503 until you set GROQ_API_KEY or start the whisper profile"
    else
      print_warning "Skipped self-hosted Whisper — enable later by uncommenting WHISPER_BASE_URL in .env"
    fi
  fi
fi

# ── 3. Install dependencies ─────────────────────────────
print_header "Installing dependencies"

echo "  Running pnpm install:all (Node + Python)..."
if ! pnpm install:all; then
  echo ""
  print_error "pnpm install:all failed."
  if [ "$SKIP_ENV" = false ]; then
    echo "  Your .env has been saved. To retry dependencies without reconfiguring:"
  else
    echo "  To retry dependency installation:"
  fi
  echo "    pnpm install:all"
  echo "  If the issue persists, inspect the output above and report it."
  exit 1
fi

print_success "All dependencies installed"

if [ "${USE_DOCLING:-0}" -eq 1 ]; then
  echo ""
  echo "  Installing Docling Python extra (this can take a minute)..."
  if ! (cd packages/ai-api && uv sync --extra docling); then
    echo ""
    print_error "Failed to install the [docling] extra."
    print_error "Retry manually: (cd packages/ai-api && uv sync --extra docling)"
    exit 1
  fi
  print_success "Docling Python extra installed"
fi

# ── 4. Next steps ───────────────────────────────────────
print_header "Setup complete!"

if [ "${#PORT_ERRORS[@]}" -gt 0 ]; then
  echo ""
  print_warning "Could not auto-assign a free port for: ${PORT_ERRORS[*]}"
  print_warning "Set these manually in .env before starting the stack."
fi

echo ""
echo -e "  ${BOLD}Option A: Docker (recommended for production)${NC}"
echo "    docker compose up -d                               # core stack"
echo "    docker compose --profile dev up -d                 # + Adminer (DB GUI)"
echo "    docker compose --profile cloud up -d               # + WhatsApp Cloud API"
echo "    docker compose --profile telegram up -d            # + Telegram client"
echo "    docker compose --profile whisper up -d             # + self-hosted Whisper (STT)"
echo "    docker compose --profile dev --profile cloud up -d # everything"

if [ "${USE_DOCLING:-0}" -eq 1 ]; then
  echo ""
  echo -e "    ${YELLOW}Note: INSTALL_DOCLING=true is set in .env.${NC}"
  echo "    Docker builds will include Docling + OCR system deps (~1 GB larger image, slower first build)."
fi
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
# Read host ports back from .env (fall back to defaults) so the links below
# reflect any conflict-driven reassignment.
env_port() {
  local val=""
  [ -f "$ENV_FILE" ] && val=$(grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2-)
  printf '%s' "${val:-$2}"
}
DOC_API_PORT=$(env_port AI_API_PORT 8000)
DOC_WA_PORT=$(env_port WHATSAPP_API_PORT 3001)
DOC_CLOUD_PORT=$(env_port WHATSAPP_CLOUD_PORT 3002)
DOC_TG_PORT=$(env_port TELEGRAM_PORT 3003)
DOC_ADMINER_PORT=$(env_port ADMINER_PORT 8080)

echo -e "  ${BOLD}Documentation${NC}"
echo "    AI API:     http://localhost:${DOC_API_PORT}/docs"
echo "    WhatsApp:   http://localhost:${DOC_WA_PORT}/docs"
echo "    Cloud API:  http://localhost:${DOC_CLOUD_PORT}/docs"
echo "    Telegram:   http://localhost:${DOC_TG_PORT}/docs (requires --profile telegram)"
echo "    DB Admin:   http://localhost:${DOC_ADMINER_PORT} (requires --profile dev)"
echo ""
