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
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NAME = "auto-claude-task-test"
RUNNING_NAME_PATTERNS = ("auto-claude-autoclaude", "auto-claude")
IMAGE_NAME_FALLBACKS = ("auto-claude-autoclaude:latest", "auto-claude:latest")


def _docker(cmd: list[str], check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *cmd], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def load_oauth_token() -> str:
    """Same resolution as scripts/docker-start.ps1."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if tok:
        return tok
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text(encoding="utf-8"))
            tok = (data.get("claudeAiOauth") or {}).get("accessToken", "").strip()
            if tok:
                print(f"  OAuth token from {creds_path}: {tok[:16]}...")
                return tok
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARNING: failed to read {creds_path}: {e}")
    return ""


def resolve_image() -> str:
    """Find the Auto-Claude image. Order:
    1. A running container whose name contains 'auto-claude' — use its image.
    2. A local image matching one of IMAGE_NAME_FALLBACKS.
    3. A local image whose repo contains 'auto-claude' (best-effort match).
    """
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

    print("  No Auto-Claude image found locally — building via 'docker compose build'...")
    build = subprocess.run(["docker", "compose", "build"], cwd=REPO_ROOT)
    if build.returncode != 0:
        sys.exit("\nERROR: 'docker compose build' failed.\n")
    for candidate in IMAGE_NAME_FALLBACKS:
        if _docker(["image", "inspect", candidate]).returncode == 0:
            print(f"  Built image: {candidate}")
            return candidate
    sys.exit("\nERROR: Build succeeded but image not found.\n")


def remove_existing(name: str) -> None:
    res = _docker(["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"])
    if res.stdout.strip():
        _docker(["rm", "-f", name])


def run_task(task: str, *, name: str = DEFAULT_NAME, image: str | None = None,
             project: Path = REPO_ROOT, keep: bool = False, timeout: int = 900) -> int:
    project = Path(project).resolve()
    if not project.exists():
        print(f"ERROR: project path does not exist: {project}")
        return 2

    token = load_oauth_token()
    if not token:
        print("ERROR: No Claude OAuth token found.")
        print("  Set CLAUDE_CODE_OAUTH_TOKEN, or run 'claude /login' to populate")
        print(f"  {Path.home() / '.claude' / '.credentials.json'}")
        return 2

    if image is None:
        image = resolve_image()

    remove_existing(name)

    inner_cmd = (
        'cd /workspace && '
        'PY=/app/apps/backend/.venv/bin/python; '
        '[ -x "$PY" ] || PY=python; '
        'echo "[test] starting run_spec.py with $PY" && '
        f'"$PY" /app/run_spec.py {json.dumps(task)} --project /workspace; '
        'rc=$?; echo "[test] run_spec.py exit=$rc"; exit $rc'
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
    print()

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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--task", required=True, help="Task description for run_spec.py")
    p.add_argument("--name", default=DEFAULT_NAME, help="Container name")
    p.add_argument("--image", default=None, help="Override image (default: auto-resolved)")
    p.add_argument("--project", default=str(REPO_ROOT), help="Host path mounted at /workspace")
    p.add_argument("--keep", action="store_true", help="Don't remove container after exit")
    p.add_argument("--timeout", type=int, default=900, help="Max seconds to wait")
    args = p.parse_args()
    return run_task(args.task, name=args.name, image=args.image,
                    project=Path(args.project), keep=args.keep, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
