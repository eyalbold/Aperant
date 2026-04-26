#!/usr/bin/env python3
"""
test_hello_world.py — Concrete smoke test built on test_docker_task.

Runs the canonical "create hello_world.py" task inside a fresh Auto-Claude
container, then verifies that a spec was produced. Exits non-zero on any
failure so it can be wired into CI / a slash command.

    python scripts/test_hello_world.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_docker_task import REPO_ROOT, run_task  # noqa: E402

TASK = "create a hello_world.py file in /workspace that prints Hello, World"
NAME = "auto-claude-hello-world-test"


def main() -> int:
    specs_dir = REPO_ROOT / ".auto-claude" / "specs"
    before = {p.name for p in specs_dir.iterdir()} if specs_dir.exists() else set()

    started = time.time()
    rc = run_task(TASK, name=NAME, project=REPO_ROOT)
    elapsed = time.time() - started

    if rc != 0:
        print(f"\nFAIL: container exited with code {rc} after {elapsed:.1f}s")
        return rc

    after = {p.name for p in specs_dir.iterdir()} if specs_dir.exists() else set()
    new_specs = sorted(after - before)
    if not new_specs:
        print("\nFAIL: no new spec was created in .auto-claude/specs/")
        return 1

    print(f"\nPASS: hello-world test completed in {elapsed:.1f}s")
    print(f"  new spec(s): {', '.join(new_specs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
