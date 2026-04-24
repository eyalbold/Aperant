#!/bin/bash
set -e

RESOLUTION="${RESOLUTION:-1280x800x24}"

echo "================================================="
echo "  Auto-Claude"
echo "================================================="
echo ""

# ── Auth check ─────────────────────────────────────────────────────────────────
# Docker has no browser or system keychain, so the interactive OAuth flow (claude /login)
# cannot complete. Pass CLAUDE_CODE_OAUTH_TOKEN as an env var instead.
# The app detects this and skips the onboarding auth step automatically.
#
# How to get your token:  claude setup-token  (run once on a machine with a browser)
# Then pass it here:
#   docker run -e CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-... auto-claude
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
  echo "  ✓  CLAUDE_CODE_OAUTH_TOKEN present: ${CLAUDE_CODE_OAUTH_TOKEN:0:16}..."
  echo ""
elif [ -n "$ANTHROPIC_AUTH_TOKEN" ]; then
  echo "  ✓  ANTHROPIC_AUTH_TOKEN present: ${ANTHROPIC_AUTH_TOKEN:0:16}..."
  echo ""
else
  echo "  ⚠  WARNING: No auth token provided."
  echo "     Set CLAUDE_CODE_OAUTH_TOKEN to skip the OAuth wizard."
  echo "     Example: docker run -e CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-... ..."
  echo ""
fi

# ── GitHub CLI auth ──────────────────────────────────────────────────────────
if [ -n "$GH_TOKEN" ]; then
  echo "  ✓  GitHub CLI authenticated (via GH_TOKEN env var)"
  # Rewrite github.com HTTPS URLs to embed the token inline, so git push/pull
  # works without any credential prompt or keychain.
  git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"
  echo "  ✓  Git HTTPS auth configured via GH_TOKEN"
  echo ""
fi

# ── Git identity ──────────────────────────────────────────────────────────────
if [ -n "$GIT_USER_NAME" ]; then
  git config --global user.name "$GIT_USER_NAME"
  echo "  ✓  Git user.name set to: $GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
  git config --global user.email "$GIT_USER_EMAIL"
  echo "  ✓  Git user.email set to: $GIT_USER_EMAIL"
fi
if [ -n "$GIT_USER_NAME" ] || [ -n "$GIT_USER_EMAIL" ]; then
  echo ""
fi

# ── 1. Virtual display ────────────────────────────────────────────────────────
# Clean up stale X lock/socket from a prior container run. Without this, a
# restart finds the old /tmp/.X99-lock still in place and Xvfb bails out with
# "Server is already active for display 99", which cascades into x11vnc failing
# and the container entering a restart loop.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
Xvfb :99 -screen 0 "$RESOLUTION" -ac +extension GLX +render -noreset &
XVFB_PID=$!
echo "[1/3] Virtual display started  (Xvfb :99  ${RESOLUTION})"
sleep 1

# ── 2. VNC server + noVNC web proxy ──────────────────────────────────────────
# x11vnc attaches to the Xvfb display; noVNC proxies VNC over WebSocket
x11vnc -display :99 -nopw -forever -shared -quiet -bg
# Sync X clipboard ↔ VNC clipboard for copy/paste support (non-fatal)
autocutsel -fork &
autocutsel -selection PRIMARY -fork &
websockify --web /usr/share/novnc/ 6080 localhost:5900 &
echo "[2/3] VNC started  — open http://localhost:6080/vnc.html in your browser"

echo ""
echo "  ┌──────────────────────────────────────────────┐"
echo "  │  Open in your browser:                       │"
echo "  │  http://localhost:6080/vnc.html               │"
echo "  └──────────────────────────────────────────────┘"
echo ""

# ── 3. Electron app ───────────────────────────────────────────────────────────
export DISPLAY=:99
echo "[3/3] Starting Auto-Claude Electron app..."
cd /app/apps/frontend
# GPU/compositor flags: Xvfb has no real GPU, so Chromium's GPU process can
# crash the renderer — producing cascades of "Render frame was disposed before
# WebFrameMain could be accessed" errors. Force software rendering and disable
# /dev/shm usage (Docker's default /dev/shm is small even with shm_size bumps).
exec /app/node_modules/.bin/electron-vite dev -- \
  --no-sandbox \
  --disable-gpu \
  --disable-gpu-compositing \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --in-process-gpu
