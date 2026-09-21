#!/usr/bin/env bash
#
# Upload every PDF in a directory to the knowledge base (batch endpoint).
#
# Usage:
#   ./upload-kb.sh /path/to/pdf/folder
#   ./upload-kb.sh                         # defaults to ./knowledge_base
#
# Environment:
#   AI_API_URL   AI API base URL (default: http://localhost:8000)
#   AI_API_KEY   API key; read from ./.env when not set in the environment
#
# Files already in the knowledge base (same SHA-256 content) are reported as
# rejected duplicates, so re-running this on the same folder is safe. The batch
# is limited by KB_MAX_BATCH_SIZE_MB (default 500 MB) on the server.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PDF_DIR="${1:-$SCRIPT_DIR/knowledge_base}"
API_URL="${AI_API_URL:-http://localhost:8000}"

API_KEY="${AI_API_KEY:-}"
if [ -z "$API_KEY" ]; then
    ENV_FILE="$SCRIPT_DIR/.env"
    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: AI_API_KEY is not set and no .env found at $ENV_FILE" >&2
        exit 1
    fi
    API_KEY=$(grep -m1 '^AI_API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"'"'"' \r')
fi
if [ -z "$API_KEY" ]; then
    echo "Error: AI_API_KEY not found (environment or .env)" >&2
    exit 1
fi

if [ ! -d "$PDF_DIR" ]; then
    echo "Error: directory not found: $PDF_DIR" >&2
    exit 1
fi

shopt -s nullglob
PDF_FILES=("$PDF_DIR"/*.pdf)
shopt -u nullglob

if [ ${#PDF_FILES[@]} -eq 0 ]; then
    echo "No PDF files found in $PDF_DIR"
    exit 0
fi

echo "Found ${#PDF_FILES[@]} PDF(s) in $PDF_DIR"
echo "Uploading to $API_URL/knowledge-base/upload/batch ..."
echo ""

CURL_ARGS=()
for file in "${PDF_FILES[@]}"; do
    CURL_ARGS+=(-F "files=@$file;type=application/pdf")
done

RESPONSE=$(curl -sS -w "\n%{http_code}" -X POST \
    "$API_URL/knowledge-base/upload/batch" \
    -H "X-API-Key: $API_KEY" \
    "${CURL_ARGS[@]}")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

pretty() { python3 -m json.tool 2>/dev/null || cat; }

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    echo "Upload finished (HTTP $HTTP_CODE)"
    echo "$BODY" | pretty
    echo ""
    echo "Accepted files are processed by the stream worker; check progress with:"
    echo "  curl -H 'X-API-Key: ...' $API_URL/knowledge-base/documents"
else
    echo "Upload failed (HTTP $HTTP_CODE)" >&2
    echo "$BODY" | pretty >&2
    exit 1
fi
