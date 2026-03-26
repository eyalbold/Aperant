# Auto-Claude Docker Image
# Runs the full Electron desktop app inside a virtual display (Xvfb).
# Access the UI via VNC in your browser at http://localhost:6080/vnc.html

FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1
# Electron sandbox disabled — required inside Docker (no user namespaces)
ENV ELECTRON_NO_SANDBOX=1

# ── Node.js 24 ─────────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── System dependencies ─────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    # Virtual framebuffer
    xvfb \
    # Electron / Chromium required libraries
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libnspr4 \
    libnss3 \
    libxss1 \
    libxtst6 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxi6 \
    libglib2.0-0 \
    libgtk-3-0 \
    libgdk-pixbuf2.0-0 \
    libdbus-glib-1-2 \
    libdbus-1-3 \
    # Git — required for worktree operations
    git \
    # VNC — remote desktop access via browser
    x11vnc \
    novnc \
    # Clipboard sharing between host and VNC
    autocutsel \
    xclip \
    # Utilities
    procps \
    && rm -rf /var/lib/apt/lists/*

# ── GitHub CLI ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Claude Code CLI ──────────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Dependency manifests only (cached unless lockfiles change) ───────────────
COPY package.json package-lock.json ./
COPY scripts/install-backend.js scripts/
COPY apps/frontend/package.json apps/frontend/
COPY apps/frontend/scripts/postinstall.cjs apps/frontend/scripts/
COPY apps/backend/requirements.txt apps/backend/
COPY tests/requirements-test.txt tests/

# ── Install all dependencies (Python venv + Node modules) ────────────────────
RUN npm run install:all

# ── Application source ───────────────────────────────────────────────────────
COPY . .

# Mount point for the user's project directory
RUN mkdir -p /workspace

# noVNC web UI (VNC over WebSocket)
EXPOSE 6080

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
