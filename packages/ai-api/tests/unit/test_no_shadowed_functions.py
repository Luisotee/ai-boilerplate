"""A module-level function defined twice silently replaces the first one.

broadcast_consumer.py (curupira-bot and here) once had two ``_has_pending`` functions with different
signatures: the second one won, and every call to the first raised TypeError in
production. The mocked tests patched the name away, so nothing caught it.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "ai_api"


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_level_function_is_defined_twice(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = Counter(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    assert [n for n, c in names.items() if c > 1] == []
