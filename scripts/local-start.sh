#!/usr/bin/env bash
# local-start.sh
# Run Auto-Claude locally (no Docker) with credentials sourced from Claude Code.
#
# On macOS the token is read from the login Keychain (same place Claude Code stores it).
# On Linux/other the token is read from ~/.claude/.credentials.json.
#
# Usage: ./scripts/local-start.sh [--reauth] [--prod]
#   --reauth   Force re-login via `claude /login` before starting
#   --prod     Build and run the production Electron bundle (default: dev mode with HMR)

set -euo pipefail

REAUTH=false
PROD=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reauth) REAUTH=true; shift ;;
    --prod)   PROD=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "================================================="
echo "  Auto-Claude Local Launcher"
echo "================================================="
echo ""

# ── Step 1: Claude OAuth token ────────────────────────────────────────────────
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then

  read_credentials_file() {
    local creds="$HOME/.claude/.credentials.json"
    [[ -f "$creds" ]] || return 0
    python3 -c "import json; print(json.load(open('$creds'))['claudeAiOauth']['accessToken'])" 2>/dev/null \
      || jq -r '.claudeAiOauth.accessToken // empty' "$creds" 2>/dev/null \
      || true
  }

  if [[ "$(uname)" == "Darwin" ]]; then
    # macOS — prefer Keychain, fall back to credentials file
    KC_SERVICE="Claude Code-credentials"
    KC_ACCOUNT="$(id -un)"

    read_keychain_creds() {
      security find-generic-password -s "$KC_SERVICE" -a "$KC_ACCOUNT" -w 2>/dev/null \
        || security find-generic-password -s "$KC_SERVICE" -w 2>/dev/null \
        || true
    }

    CREDS_JSON="$(read_keychain_creds)"

    if $REAUTH || [[ -z "$CREDS_JSON" ]]; then
      echo "[1/2] Running 'claude /login'..."
      echo "      A browser window will open — complete the OAuth flow there."
      echo ""
      claude /login
      CREDS_JSON="$(read_keychain_creds)"
    fi

    if [[ -n "$CREDS_JSON" ]]; then
      TOKEN="$(printf '%s' "$CREDS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['claudeAiOauth']['accessToken'])" 2>/dev/null \
        || printf '%s' "$CREDS_JSON" | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null \
        || true)"
    else
      TOKEN="$(read_credentials_file)"
    fi

    SOURCE="Keychain"
  else
    # Linux / other — credentials file
    if $REAUTH || [[ ! -f "$HOME/.claude/.credentials.json" ]]; then
      echo "[1/2] Running 'claude /login'..."
      echo "      A browser window will open — complete the OAuth flow there."
      echo ""
      claude /login
    fi
    TOKEN="$(read_credentials_file)"
    SOURCE="~/.claude/.credentials.json"
  fi

  if [[ -z "${TOKEN:-}" ]]; then
    echo "ERROR: Could not read Claude OAuth token from $SOURCE."
    echo "       Run with --reauth to re-authenticate."
    exit 1
  fi

  echo "  ✓  Token loaded from $SOURCE: ${TOKEN:0:16}..."
  echo ""
  export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
else
  echo "  ✓  CLAUDE_CODE_OAUTH_TOKEN already set: ${CLAUDE_CODE_OAUTH_TOKEN:0:16}..."
  echo ""
fi

# ── Step 2: GH_TOKEN (optional) ───────────────────────────────────────────────
if [[ -z "${GH_TOKEN:-}" ]]; then
  ENV_FILE="$REPO_ROOT/docker/.env"
  if [[ -f "$ENV_FILE" ]]; then
    GH_LINE="$(grep -E '^GH_TOKEN=(.+)' "$ENV_FILE" | head -1 || true)"
    [[ -n "$GH_LINE" ]] && export GH_TOKEN="${GH_LINE#GH_TOKEN=}"
  fi
  # Also try `gh auth token` if the GitHub CLI is available
  if [[ -z "${GH_TOKEN:-}" ]] && command -v gh &>/dev/null; then
    GH_TOKEN="$(gh auth token 2>/dev/null || true)"
    [[ -n "$GH_TOKEN" ]] && export GH_TOKEN
  fi
fi

if [[ -n "${GH_TOKEN:-}" ]]; then
  echo "  ✓  GH_TOKEN present: ${GH_TOKEN:0:8}..."
  echo ""
else
  echo "  ⚠  GH_TOKEN not set — GitHub features will be unavailable."
  echo "     Set GH_TOKEN in docker/.env or run: gh auth login"
  echo ""
fi

# ── Step 3: Git identity ───────────────────────────────────────────────────────
if [[ -z "${GIT_USER_NAME:-}" ]]; then
  GIT_USER_NAME="$(git -C "$REPO_ROOT" config --global user.name 2>/dev/null || true)"
  export GIT_USER_NAME
fi
if [[ -z "${GIT_USER_EMAIL:-}" ]]; then
  GIT_USER_EMAIL="$(git -C "$REPO_ROOT" config --global user.email 2>/dev/null || true)"
  export GIT_USER_EMAIL
fi

if [[ -n "${GIT_USER_NAME:-}" || -n "${GIT_USER_EMAIL:-}" ]]; then
  echo "  ✓  Git identity: ${GIT_USER_NAME:-<unset>} <${GIT_USER_EMAIL:-<unset>}>"
  echo ""
fi

# ── Step 4: Start the app ──────────────────────────────────────────────────────
cd "$REPO_ROOT"

if $PROD; then
  echo "[2/2] Building and starting Auto-Claude (production)..."
  echo ""
  npm start
else
  echo "[2/2] Starting Auto-Claude in dev mode (HMR enabled)..."
  echo ""
  npm run dev
fi
