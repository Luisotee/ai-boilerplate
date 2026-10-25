"""Ensure all AGENTS.md rule files remain <= 24,000 bytes.

Antigravity (agy) truncates rule files silently at 24,000 bytes on line
boundaries. This test prevents regressions from pushing documentation over
the budget.
"""

from pathlib import Path


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
