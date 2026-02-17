#!/bin/bash
# Auto-format files after Claude edits them.
# Runs prettier for TS/JS files and ruff for Python files.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0

case "$FILE_PATH" in
  *.ts|*.tsx|*.js|*.jsx|*.json|*.md|*.yaml|*.yml)
    npx prettier --write "$FILE_PATH" 2>/dev/null || true
    ;;
  *.py)
    uvx ruff format "$FILE_PATH" 2>/dev/null || true
    ;;
esac

exit 0
