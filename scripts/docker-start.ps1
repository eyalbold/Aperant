#!/usr/bin/env pwsh
# docker-start.ps1
# Reads the Claude OAuth token from ~/.claude/.credentials.json, then launches Auto-Claude via Docker Compose.
# Use -Reauth to run `claude /login` and refresh the token first.
# Usage: .\scripts\docker-start.ps1 [-ProjectPath <path>] [-Build] [-Reauth]

param(
    [string]$ProjectPath = "C:/autoproj",
    [switch]$Build,
    [switch]$Reauth,
    [switch]$DontRemove
)

Write-Host "================================================="
Write-Host "  Auto-Claude Docker Launcher"
Write-Host "================================================="
Write-Host ""
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN -or $Reauth) {

# ── Step 1: Read token from ~/.claude/.credentials.json ───────────────────────
$credsPath = Join-Path $env:USERPROFILE ".claude" ".credentials.json"

if ($Reauth -or -not (Test-Path $credsPath)) {
    Write-Host "[1/2] Running 'claude /login'..."
    Write-Host "      A browser window will open — complete the OAuth flow there."
    Write-Host ""
    claude /login
}

if (-not (Test-Path $credsPath)) {
    Write-Error "Credentials file not found at $credsPath. Make sure 'claude /login' completed successfully."
    exit 1
}

$creds = Get-Content $credsPath -Raw | ConvertFrom-Json
$token = $creds.claudeAiOauth.accessToken

if (-not $token) {
    Write-Error "Could not find accessToken in $credsPath. Run with -Reauth to re-authenticate."
    exit 1
}

Write-Host "  ✓  Token loaded from credentials file: $($token.Substring(0, [Math]::Min(16, $token.Length)))..."
Write-Host ""

$env:CLAUDE_CODE_OAUTH_TOKEN = $token

}
# ── Validate OAuth token ──────────────────────────────────────────────────────
Write-Host "  Validating token..."
try {
    $response = Invoke-RestMethod -Uri 'https://api.anthropic.com/v1/models' `
        -Headers @{
            'Authorization' = "Bearer $env:CLAUDE_CODE_OAUTH_TOKEN"
            'anthropic-version' = '2023-06-01'
            'anthropic-beta' = 'oauth-2025-04-20'
        } `
        -Method Get -ErrorAction Stop
    Write-Host "  Token valid." -ForegroundColor Green
    Write-Host ""
} catch {
    $status = $_.Exception.Response.StatusCode.value__
    if ($status -eq 401) {
        Write-Error "Token is invalid or expired. Re-run with -Reauth to get a new token."
        exit 1
    }
    # Non-401 errors (network, rate limit) — warn but continue
    Write-Warning "Token check returned HTTP $status — continuing anyway."
    Write-Host ""
}

# ── Step 2: docker compose down (if running) + up ────────────────────────────
$running = docker compose ps --quiet 2>$null
if ($running) {
    Write-Host "[2/3] Stopping existing containers..."
    docker compose down
    Write-Host ""
}

Write-Host "[$( if ($running) {'3/3'} else {'2/2'} )] Starting Docker..."


if ($ProjectPath -ne "") {
    $env:PROJECT_PATH = $ProjectPath
}

# ── Validate GH_TOKEN ────────────────────────────────────────────────────────
if (-not $env:GH_TOKEN) {
    # Try reading from docker/.env
    $envFile = Join-Path $PSScriptRoot ".." "docker" ".env"
    if (Test-Path $envFile) {
        $ghLine = Select-String -Path $envFile -Pattern '^GH_TOKEN=(.+)' | Select-Object -First 1
        if ($ghLine) {
            $env:GH_TOKEN = $ghLine.Matches[0].Groups[1].Value.Trim()
        }
    }
}

if (-not $env:GH_TOKEN) {
    Write-Warning "GH_TOKEN is not set. GitHub features will be unavailable."
    Write-Host "  Set GH_TOKEN in docker/.env or as an environment variable."
    Write-Host "  Create a fine-grained token at: https://github.com/settings/tokens?type=beta"
    Write-Host ""
}

# ── Git identity — read from local git config if not already set ───────────────
if (-not $env:GIT_USER_NAME) {
    $env:GIT_USER_NAME = (git config --global user.name 2>$null)
}
if (-not $env:GIT_USER_EMAIL) {
    $env:GIT_USER_EMAIL = (git config --global user.email 2>$null)
}

if ($env:GIT_USER_NAME -or $env:GIT_USER_EMAIL) {
    Write-Host "  Git identity: $env:GIT_USER_NAME <$env:GIT_USER_EMAIL>"
    Write-Host ""
} else {
    Write-Warning "Git user.name / user.email not found in git config. Commits inside the container will have no identity."
    Write-Host ""
}

# ── Remove system volumes on rebuild (unless -DontRemove) ──────────────────
if ($Build -and -not $DontRemove) {
    Write-Host "  Removing persistent system volumes (usr, etc) for clean rebuild..."
    docker volume rm auto-claude_autoclaude-usr auto-claude_autoclaude-etc 2>$null
    Write-Host ""
}

$composeArgs = @("compose", "up", "-d")
if ($Build) { $composeArgs += "--build" }

docker @composeArgs

Write-Host ""
Write-Host "  Auto-Claude is running. Open in your browser:"
Write-Host "  http://localhost:6080/vnc.html"
Write-Host ""
Write-Host "  To view logs: docker compose logs -f"
Write-Host "  To stop:      docker compose down"
