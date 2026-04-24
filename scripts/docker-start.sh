#!/usr/bin/env bash
# docker-start.sh
# Reads the Claude OAuth token from ~/.claude/.credentials.json, then launches Auto-Claude via Docker Compose.
# Use --reauth to run `claude /login` and refresh the token first.
# Usage: ./scripts/docker-start.sh [--project-path <path>] [--extra-mount-path <path>] [--build] [--reauth] [--dont-remove]

set -euo pipefail

PROJECT_PATH=""
EXTRA_MOUNT_PATH=""
BUILD_FLAG=""
REAUTH=false
DONT_REMOVE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-path)     PROJECT_PATH="$2"; shift 2 ;;
    --extra-mount-path) EXTRA_MOUNT_PATH="$2"; shift 2 ;;
    --build)            BUILD_FLAG="--build"; shift ;;
    --reauth)           REAUTH=true; shift ;;
    --dont-remove)      DONT_REMOVE=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo "================================================="
echo "  Auto-Claude Docker Launcher"
echo "================================================="
echo ""

# ── Step 1: Read token from ~/.claude/.credentials.json ───────────────────────
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  CREDS_PATH="$HOME/.claude/.credentials.json"

  if $REAUTH || [[ ! -f "$CREDS_PATH" ]]; then
    echo "[1/2] Running 'claude /login'..."
    echo "      A browser window will open — complete the OAuth flow there."
    echo ""
    claude /login
  fi

  if [[ ! -f "$CREDS_PATH" ]]; then
    echo "ERROR: Credentials file not found at $CREDS_PATH. Make sure 'claude /login' completed successfully."
    exit 1
  fi

  TOKEN="$(python3 -c "import json; print(json.load(open('$CREDS_PATH'))['claudeAiOauth']['accessToken'])" 2>/dev/null || true)"

  if [[ -z "$TOKEN" ]]; then
    # Fallback: try jq
    TOKEN="$(jq -r '.claudeAiOauth.accessToken // empty' "$CREDS_PATH" 2>/dev/null || true)"
  fi

  if [[ -z "$TOKEN" ]]; then
    echo "ERROR: Could not find accessToken in $CREDS_PATH. Run with --reauth to re-authenticate."
    exit 1
  fi

  echo "  ✓  Token loaded from credentials file: ${TOKEN:0:16}..."
  echo ""

  export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
fi

# ── Validate OAuth token ──────────────────────────────────────────────────────
echo "  Validating token..."
HTTP_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $CLAUDE_CODE_OAUTH_TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  https://api.anthropic.com/v1/models || echo "000")"

if [[ "$HTTP_STATUS" == "200" ]]; then
  echo "  Token valid."
  echo ""
elif [[ "$HTTP_STATUS" == "401" ]]; then
  echo "ERROR: Token is invalid or expired. Re-run with --reauth to get a new token."
  exit 1
else
  echo "WARNING: Token check returned HTTP $HTTP_STATUS — continuing anyway."
  echo ""
fi

# ── Step 2: docker compose down (if running) + up ────────────────────────────
RUNNING="$(docker compose ps --quiet 2>/dev/null || true)"
if [[ -n "$RUNNING" ]]; then
  echo "[2/3] Stopping existing containers..."
  docker compose down
  echo ""
  STEP="3/3"
else
  STEP="2/2"
fi

echo "[$STEP] Starting Docker..."

if [[ -n "$PROJECT_PATH" ]]; then
  export PROJECT_PATH
fi
if [[ -n "$EXTRA_MOUNT_PATH" ]]; then
  export EXTRA_MOUNT_PATH
fi

# ── Validate GH_TOKEN ────────────────────────────────────────────────────────
if [[ -z "${GH_TOKEN:-}" ]]; then
  ENV_FILE="$(dirname "$0")/../docker/.env"
  if [[ -f "$ENV_FILE" ]]; then
    GH_LINE="$(grep -E '^GH_TOKEN=(.+)' "$ENV_FILE" | head -1 || true)"
    if [[ -n "$GH_LINE" ]]; then
      export GH_TOKEN="${GH_LINE#GH_TOKEN=}"
    fi
  fi
fi

if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "WARNING: GH_TOKEN is not set. GitHub features will be unavailable."
  echo "  Set GH_TOKEN in docker/.env or as an environment variable."
  echo "  Create a fine-grained token at: https://github.com/settings/tokens?type=beta"
  echo ""
fi

# ── Git identity — read from local git config if not already set ───────────────
if [[ -z "${GIT_USER_NAME:-}" ]]; then
  GIT_USER_NAME="$(git config --global user.name 2>/dev/null || true)"
  export GIT_USER_NAME
fi
if [[ -z "${GIT_USER_EMAIL:-}" ]]; then
  GIT_USER_EMAIL="$(git config --global user.email 2>/dev/null || true)"
  export GIT_USER_EMAIL
fi

if [[ -n "${GIT_USER_NAME:-}" || -n "${GIT_USER_EMAIL:-}" ]]; then
  echo "  Git identity: $GIT_USER_NAME <$GIT_USER_EMAIL>"
  echo ""
else
  echo "WARNING: Git user.name / user.email not found in git config. Commits inside the container will have no identity."
  echo ""
fi

# ── Remove system volumes on rebuild (unless --dont-remove) ──────────────────
if [[ -n "$BUILD_FLAG" ]] && ! $DONT_REMOVE; then
  echo "  Removing persistent system volumes (usr, etc) for clean rebuild..."
  docker volume rm auto-claude_autoclaude-usr auto-claude_autoclaude-etc 2>/dev/null || true
  echo ""
fi

COMPOSE_ARGS=("compose" "up" "-d")
if [[ -n "$BUILD_FLAG" ]]; then
  COMPOSE_ARGS+=("--build")
fi

docker "${COMPOSE_ARGS[@]}"

echo ""
echo "  Auto-Claude is running. Open in your browser:"
echo "  http://localhost:6080/vnc.html"
echo ""
echo "  To view logs: docker compose logs -f"
echo "  To stop:      docker compose down"
