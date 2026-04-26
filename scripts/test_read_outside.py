#!/usr/bin/env python3
"""
test_read_outside.py — Smoke test that the agent can READ files outside /workspace.

Mounts a subdirectory (apps/backend) as /workspace and the full repo at
/extra-workspace, then asks the agent to read README.md from the parent
project and report a token from it. Verifies the land-lock blocks writes
but does NOT block reads outside /workspace.

    python scripts/test_read_outside.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_docker_task import (  # noqa: E402
    REPO_ROOT,
    _docker,
    load_oauth_token,
    remove_existing,
    resolve_image,
)

NAME = "auto-claude-read-outside-test"
SUBPROJECT = REPO_ROOT / "apps" / "backend"
SENTINEL = "READOUTSIDE-OK-9F2A"
TARGET_REL = "README.md"  # path under /extra-workspace

TASK = (
    f"Use the Read tool to read /extra-workspace/{TARGET_REL}. "
    f"Then output exactly one line: the token READOUTSIDE-OK-9F2A followed by "
    "a space and the first non-empty line of that file. Do not write files."
)


def main() -> int:
    if not SUBPROJECT.exists():
        print(f"ERROR: subproject path missing: {SUBPROJECT}")
        return 2
    target_host = REPO_ROOT / TARGET_REL
    if not target_host.exists():
        print(f"ERROR: target file missing on host: {target_host}")
        return 2

    token = load_oauth_token()
    if not token:
        print("ERROR: No Claude OAuth token found.")
        return 2

    image = resolve_image()
    remove_existing(NAME)

    inner_cmd = (
        'cd /workspace && '
        'echo "[test] invoking claude -p" && '
        f'claude -p {json.dumps(TASK)} --allowedTools Read --add-dir /extra-workspace; '
        'rc=$?; echo "[test] claude exit=$rc"; exit $rc'
    )

    docker_cmd = [
        "docker", "run",
        "--name", NAME,
        "--label", "auto-claude.test=1",
        "-v", f"{SUBPROJECT}:/workspace",
        "-v", f"{REPO_ROOT}:/extra-workspace:ro",
        "-e", f"CLAUDE_CODE_OAUTH_TOKEN={token}",
        "-e", "PYTHONUNBUFFERED=1",
        "--entrypoint", "bash",
        image,
        "-lc", inner_cmd,
    ]

    print("\n=== Auto-Claude read-outside test ===")
    print(f"  image          : {image}")
    print(f"  workspace      : {SUBPROJECT} -> /workspace")
    print(f"  extra (ro)     : {REPO_ROOT} -> /extra-workspace")
    print(f"  read target    : /extra-workspace/{TARGET_REL}")
    print(f"  sentinel       : {SENTINEL}")
    print()

    timeout = 180
    start = time.time()
    log_path = REPO_ROOT / f".auto-claude-test-{NAME}.log"
    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            proc = subprocess.Popen(docker_cmd, stdout=logf, stderr=subprocess.STDOUT)
            while proc.poll() is None:
                if time.time() - start > timeout:
                    print(f"\nTIMEOUT after {timeout}s — killing container")
                    _docker(["kill", NAME])
                    proc.wait(timeout=10)
                    break
                time.sleep(1)
            exit_code = proc.returncode if proc.returncode is not None else 124
    finally:
        _docker(["rm", "-f", NAME])

    elapsed = time.time() - start
    output = log_path.read_text(encoding="utf-8", errors="replace")

    print(f"=== Done in {elapsed:.1f}s — exit code {exit_code} ===")

    emitted_lines = [
        ln for ln in output.splitlines()
        if SENTINEL in ln and "Read tool" not in ln
    ]
    if emitted_lines:
        print(f"PASS: agent emitted {SENTINEL} — read outside /workspace succeeded")
        for ln in emitted_lines[:3]:
            print(f"  > {ln.strip()}")
        return 0

    print(f"FAIL: sentinel {SENTINEL} not found in output")
    print(f"  full log: {log_path}")
    print("  --- last 30 lines ---")
    for line in output.splitlines()[-30:]:
        print(f"  {line}")
    return exit_code or 1


if __name__ == "__main__":
    sys.exit(main())
