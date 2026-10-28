"""Ensure all AGENTS.md rule files remain <= 24,000 bytes.

Antigravity (agy) truncates rule files silently at 24,000 bytes on line
boundaries. This test prevents regressions from pushing documentation over
the budget.
"""

import subprocess
from pathlib import Path

AGY_RULE_LIMIT = 24_000  # bytes; agy truncates a rule file past this


def _repo_root() -> Path:
    # packages/ai-api/tests/unit -> repo root is 4 levels up
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def test_root_agents_md_size():
    root_agents = _repo_root() / "AGENTS.md"
    assert root_agents.is_file(), "Root AGENTS.md must exist"
    size = root_agents.stat().st_size
    assert size <= 24_000, f"Root AGENTS.md size ({size} bytes) exceeds the 24,000-byte limit"
    assert size > 0, "Root AGENTS.md must not be empty"


def test_all_agents_rule_files_within_budget():
    repo_root = _repo_root()
    excluded = {".git", "node_modules", ".venv", "dist", ".next", "build"}

    rule_files = [
        p
        for p in repo_root.rglob("AGENTS.md")
        if not any(part in excluded for part in p.relative_to(repo_root).parts)
    ]

    assert len(rule_files) >= 8, f"Expected at least 8 AGENTS.md files, found {len(rule_files)}"

    for rule_file in rule_files:
        resolved = rule_file.resolve()
        assert resolved.is_file(), f"Rule file {rule_file} must resolve to an existing file"
        size = resolved.stat().st_size
        assert size <= 24_000, f"Rule file {rule_file} ({size} bytes) exceeds the 24,000-byte limit"


def test_tracked_rules_files_fit_agy():
    """CLAUDE.md and GEMINI.md count too: the root CLAUDE.md is a real file, not a symlink."""
    root = _repo_root()
    names = subprocess.run(
        ["git", "ls-files", "-z", "*CLAUDE.md", "*AGENTS.md", "*GEMINI.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    for name in filter(None, names):
        path = root / name
        if not path.is_file():  # deleted but not yet committed
            continue
        size = len(path.read_bytes())
        assert size <= AGY_RULE_LIMIT, (
            f"{name} is {size} bytes, over agy's {AGY_RULE_LIMIT}-byte limit"
        )
