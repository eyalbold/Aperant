#!/usr/bin/env python3
"""
test_docker_task.py — Run a single headless task inside the Auto-Claude image.

Auto-resolves the image: looks for a running auto-claude container first,
otherwise falls back to a local `auto-claude-*autoclaude*` image. The user's
running app is left alone — this always launches a NEW one-shot container
under its own name.

Usage:
    python scripts/test_docker_task.py --task "create hello world"
    python scripts/test_docker_task.py --task "..." --name my-test --keep
    python scripts/test_docker_task.py --task "..." --image custom:tag
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NAME = "auto-claude-task-test"
RUNNING_NAME_PATTERNS = ("auto-claude-autoclaude", "auto-claude")
IMAGE_NAME_FALLBACKS = ("auto-claude-autoclaude:latest", "auto-claude:latest")
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _docker(cmd: list[str], check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *cmd], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def _read_keychain_creds() -> str:
    """Read raw JSON from macOS Keychain entry for Claude Code. Empty string on miss."""
    if platform.system() != "Darwin":
        return ""
    try:
        account = subprocess.run(["id", "-un"], text=True, capture_output=True).stdout.strip()
    except FileNotFoundError:
        account = ""
    for args in (
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
    ):
        if not account and "-a" in args:
            continue
        res = subprocess.run(args, text=True, capture_output=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return ""


def _extract_access_token(creds_json: str) -> str:
    try:
        data = json.loads(creds_json)
        return (data.get("claudeAiOauth") or {}).get("accessToken", "").strip()
    except json.JSONDecodeError:
        return ""


def _run_claude_login() -> bool:
    """Invoke `claude /login`. Returns True if the command was found and exited cleanly."""
    print("  Running 'claude /login' — complete the OAuth flow in your browser...")
    try:
        rc = subprocess.run(["claude", "/login"]).returncode
    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found on PATH. Install Claude Code, then retry.")
        return False
    if rc != 0:
        print(f"WARNING: 'claude /login' exited with code {rc}.")
    return rc == 0


def _read_creds_file() -> str:
    """Read accessToken from ~/.claude/.credentials.json (Windows/Linux path). Empty on miss."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return ""
    try:
        tok = _extract_access_token(creds_path.read_text(encoding="utf-8"))
        if tok:
            print(f"  OAuth token from {creds_path}: {tok[:16]}...")
            return tok
    except OSError as e:
        print(f"WARNING: failed to read {creds_path}: {e}")
    return ""


def load_oauth_token(reauth: bool = False) -> str:
    """Resolve a Claude OAuth token, dispatching by OS.

    - $CLAUDE_CODE_OAUTH_TOKEN wins on every platform.
    - macOS: try Keychain (`Claude Code-credentials`), then ~/.claude/.credentials.json.
    - Windows / Linux: read ~/.claude/.credentials.json (same path docker-start.ps1 uses).
    If `reauth` or all sources are empty, run `claude /login` and re-resolve.
    """
    def _resolve_once() -> str:
        tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if tok:
            print("  OAuth token from $CLAUDE_CODE_OAUTH_TOKEN")
            return tok
        system = platform.system()
        if system == "Darwin":
            kc = _read_keychain_creds()
            if kc:
                tok = _extract_access_token(kc)
                if tok:
                    print(f"  OAuth token from macOS Keychain ({KEYCHAIN_SERVICE}): {tok[:16]}...")
                    return tok
            return _read_creds_file()
        # Windows / Linux: file-based, same as before.
        return _read_creds_file()

    if reauth:
        _run_claude_login()
        return _resolve_once()

    tok = _resolve_once()
    if tok:
        return tok
    sources = "env, credentials file, or Keychain" if platform.system() == "Darwin" else "env or credentials file"
    print(f"  No token found in {sources}.")
    if _run_claude_login():
        return _resolve_once()
    return ""


def validate_token(token: str) -> int:
    """Return HTTP status code from a sanity GET to /v1/models. 0 on transport error."""
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def _build_image() -> str:
    print("  Running 'docker compose build'...")
    build = subprocess.run(["docker", "compose", "build"], cwd=REPO_ROOT)
    if build.returncode != 0:
        sys.exit("\nERROR: 'docker compose build' failed.\n")
    for candidate in IMAGE_NAME_FALLBACKS:
        if _docker(["image", "inspect", candidate]).returncode == 0:
            print(f"  Built image: {candidate}")
            return candidate
    sys.exit("\nERROR: Build succeeded but image not found.\n")


def resolve_image(rebuild: bool = False, allow_build: bool = True) -> str:
    """Find the Auto-Claude image. Order:
    1. A running container whose name contains 'auto-claude' — use its image.
    2. A local image matching one of IMAGE_NAME_FALLBACKS.
    3. A local image whose repo contains 'auto-claude' (best-effort match).

    If `rebuild`, skip discovery and always build. If no image is found and
    `allow_build` is False, exit with an error instead of building.
    """
    if rebuild:
        print("  --rebuild: forcing 'docker compose build'.")
        return _build_image()

    res = _docker(["ps", "--format", "{{.Names}}\t{{.Image}}"])
    for line in (res.stdout or "").splitlines():
        name, _, image = line.partition("\t")
        if any(p in name for p in RUNNING_NAME_PATTERNS) and image:
            print(f"  Resolved image from running container '{name}': {image}")
            return image

    for candidate in IMAGE_NAME_FALLBACKS:
        if _docker(["image", "inspect", candidate]).returncode == 0:
            print(f"  Resolved image: {candidate}")
            return candidate

    res = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}"])
    for line in (res.stdout or "").splitlines():
        if "auto-claude" in line and not line.endswith(":<none>"):
            print(f"  Resolved image (fuzzy match): {line}")
            return line

    if not allow_build:
        sys.exit("\nERROR: No Auto-Claude image found locally and --no-build was set. "
                 "Omit --no-build to build it via 'docker compose build'.\n")

    print("  No Auto-Claude image found locally — building via 'docker compose build'...")
    return _build_image()


def remove_existing(name: str) -> None:
    res = _docker(["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"])
    if res.stdout.strip():
        _docker(["rm", "-f", name])


def run_task(task: str, *, name: str = DEFAULT_NAME, image: str | None = None,
             project: Path = REPO_ROOT, keep: bool = False, timeout: int = 900,
             reauth: bool = False, validate: bool = True,
             rebuild: bool = False, allow_build: bool = True,
             dry_run: bool = False) -> int:
    project = Path(project).resolve()
    if not project.exists():
        print(f"ERROR: project path does not exist: {project}")
        return 2

    token = load_oauth_token(reauth=reauth)
    if not token:
        print("ERROR: No Claude OAuth token found.")
        print("  Set CLAUDE_CODE_OAUTH_TOKEN, or run 'claude /login' to populate")
        print(f"  {Path.home() / '.claude' / '.credentials.json'}")
        return 2

    if validate:
        print("  Validating token...")
        status = validate_token(token)
        if status == 200:
            print("  Token valid.")
        elif status == 401:
            print("ERROR: Token is invalid or expired. Re-run with --reauth to refresh.")
            return 2
        elif status == 0:
            print("WARNING: Could not reach api.anthropic.com to validate token — continuing.")
        else:
            print(f"WARNING: Unexpected validation status {status} — continuing.")

    if image is None:
        image = resolve_image(rebuild=rebuild, allow_build=allow_build)

    remove_existing(name)

    # Run apps/backend/runners/spec_runner.py from /app/apps/backend so its
    # relative imports (cli.utils, core.platform, ...) resolve. --auto-approve
    # skips the human review gate; --no-build stops after spec creation so the
    # test verifies "a spec was produced" without chaining into the full build.
    inner_cmd = (
        'cd /app/apps/backend && '
        'PY=/app/apps/backend/.venv/bin/python; '
        '[ -x "$PY" ] || PY=python; '
        'echo "[test] starting spec_runner.py with $PY" && '
        f'"$PY" runners/spec_runner.py --task {shlex.quote(task)} '
        '--project-dir /workspace --auto-approve --no-build; '
        'rc=$?; echo "[test] spec_runner.py exit=$rc"; exit $rc'
    )

    docker_cmd = [
        "docker", "run",
        "--name", name,
        "--label", "auto-claude.test=1",
        "-v", f"{project}:/workspace",
        "-e", f"CLAUDE_CODE_OAUTH_TOKEN={token}",
        "-e", "PYTHONUNBUFFERED=1",
        "--entrypoint", "bash",
        image,
        "-lc", inner_cmd,
    ]

    print(f"\n=== Auto-Claude Docker task ===")
    print(f"  image  : {image}")
    print(f"  name   : {name}")
    print(f"  task   : {task}")
    print(f"  mount  : {project} -> /workspace")
    print(f"  timeout: {timeout}s")
    print(f"  token  : {token[:16]}... ({len(token)} chars)")
    print()

    if dry_run:
        masked = list(docker_cmd)
        for i, arg in enumerate(masked):
            if arg.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                masked[i] = "CLAUDE_CODE_OAUTH_TOKEN=<redacted>"
        print("--dry-run: would execute:")
        print("  " + " ".join(shlex.quote(a) for a in masked))
        return 0

    start = time.time()
    try:
        proc = subprocess.Popen(docker_cmd)
        while proc.poll() is None:
            if time.time() - start > timeout:
                print(f"\nTIMEOUT after {timeout}s — killing container")
                _docker(["kill", name])
                proc.wait(timeout=10)
                return 124
            time.sleep(1)
        exit_code = proc.returncode
    finally:
        if not keep:
            _docker(["rm", "-f", name])

    elapsed = time.time() - start
    print(f"\n=== Done in {elapsed:.1f}s — exit code {exit_code} ===")
    if exit_code == 0:
        specs_dir = project / ".auto-claude" / "specs"
        if specs_dir.exists():
            recent = sorted(specs_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:3]
            if recent:
                print("Most recent specs:")
                for s in recent:
                    print(f"  {s.name}")
    return exit_code


def add_runtime_args(p: argparse.ArgumentParser) -> None:
    """Flags shared between this script and test_hello_world.py."""
    p.add_argument("--reauth", action="store_true",
                   help="Run 'claude /login' before resolving the token (refresh path).")
    p.add_argument("--no-validate", dest="validate", action="store_false",
                   help="Skip the GET /v1/models token sanity check.")
    p.add_argument("--rebuild", action="store_true",
                   help="Force 'docker compose build' even if an image already resolves.")
    p.add_argument("--no-build", dest="allow_build", action="store_false",
                   help="Fail instead of building when no image is found.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print resolved image, masked token, and the docker run command, then exit.")
    p.add_argument("--keep", action="store_true", help="Don't remove container after exit")
    p.add_argument("--timeout", type=int, default=900, help="Max seconds to wait")
    p.add_argument("--image", default=None, help="Override image (default: auto-resolved)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--task", required=True, help="Task description for run_spec.py")
    p.add_argument("--name", default=DEFAULT_NAME, help="Container name")
    p.add_argument("--project", default=str(REPO_ROOT), help="Host path mounted at /workspace")
    add_runtime_args(p)
    args = p.parse_args()
    return run_task(args.task, name=args.name, image=args.image,
                    project=Path(args.project), keep=args.keep, timeout=args.timeout,
                    reauth=args.reauth, validate=args.validate,
                    rebuild=args.rebuild, allow_build=args.allow_build,
                    dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
