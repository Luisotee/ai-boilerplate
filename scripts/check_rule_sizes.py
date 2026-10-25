#!/usr/bin/env python3
"""Check that all AGENTS.md rule files in the repository are <= 24,000 bytes.

Antigravity (agy) enforces a strict 24 KB (24,000 bytes) limit per rule file.
Files exceeding this limit are truncated on line boundaries.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_RULE_SIZE_BYTES = 24_000
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "dist", ".next", "build"}


def find_agent_rule_files(repo_root: Path) -> list[Path]:
    """Find all AGENTS.md files in the repository, excluding build/env dirs."""
    rule_files: list[Path] = []
    for path in repo_root.rglob("AGENTS.md"):
        # Check if any parent part is in excluded dirs
        rel_parts = path.relative_to(repo_root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        rule_files.append(path)
    return sorted(rule_files)


def check_rule_sizes(repo_root: Path) -> bool:
    """Verify all AGENTS.md files satisfy the 24,000-byte budget."""
    rule_files = find_agent_rule_files(repo_root)
    if not rule_files:
        print("Error: No AGENTS.md files found in repository.", file=sys.stderr)
        return False

    print(f"Checking {len(rule_files)} AGENTS.md rule file(s) for 24,000-byte limit:\n")

    failed = False
    for path in rule_files:
        rel_path = path.relative_to(repo_root)
        resolved = path.resolve()
        size = resolved.stat().st_size
        symlink_info = f" -> {resolved.relative_to(repo_root)}" if path.is_symlink() else ""

        if size > MAX_RULE_SIZE_BYTES:
            print(
                f"  FAIL: {rel_path}{symlink_info} ({size:,} bytes > {MAX_RULE_SIZE_BYTES:,} bytes)"
            )
            failed = True
        else:
            remaining = MAX_RULE_SIZE_BYTES - size
            print(
                f"  PASS: {rel_path}{symlink_info} ({size:,} bytes, {remaining:,} bytes headroom)"
            )

    if failed:
        print(
            "\nOne or more rule files exceed the 24,000-byte Antigravity limit!",
            file=sys.stderr,
        )
        return False

    print("\nAll AGENTS.md rule files are within the 24,000-byte budget.")
    return True


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    success = check_rule_sizes(repo_root)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
