#!/usr/bin/env bash
# docker-start.sh
# Runs `claude setup-token` to obtain an OAuth token, then launches Auto-Claude via Docker Compose.
# Usage: ./scripts/docker-start.sh [--project-path <path>] [--build]

set -euo pipefail

PROJECT_PATH=""
BUILD_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-path) PROJECT_PATH="$2"; shift 2 ;;
    --build)        BUILD_FLAG="--build"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo "================================================="
echo "  Auto-Claude Docker Launcher"
echo "================================================="
echo ""

# ── Step 1: claude setup-token ────────────────────────────────────────────────
echo "[1/2] Running 'claude setup-token'..."
echo "      A browser window will open — complete the OAuth flow there."
echo ""

# Tee so the user sees output AND we capture it
SETUP_OUTPUT="$(claude setup-token 2>&1 | tee /dev/tty)"

# Extract token (sk-ant-oat01-... pattern)
TOKEN="$(echo "$SETUP_OUTPUT" | grep -oE 'sk-ant-oat01-[A-Za-z0-9_-]+' | head -1)"

if [[ -z "$TOKEN" ]]; then
  echo ""
  echo "ERROR: Could not find a Claude OAuth token in the output above."
  echo "Make sure 'claude setup-token' completed successfully."
  exit 1
fi

echo ""
echo "  Token captured: ${TOKEN:0:24}..."
echo ""

# ── Step 2: docker compose down (if running) + up ────────────────────────────
if [[ -n "$(docker compose ps --quiet 2>/dev/null)" ]]; then
  echo "[2/3] Stopping existing containers..."
  docker compose down
  echo ""
  STEP="3/3"
else
  STEP="2/2"
fi

echo "[$STEP] Starting Docker..."

export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
[[ -n "$PROJECT_PATH" ]] && export PROJECT_PATH

docker compose up $BUILD_FLAG
