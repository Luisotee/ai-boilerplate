#!/usr/bin/env python3
"""Claude Code PostToolUse hook: cheap repo checks right after an Edit/Write.

- A rules file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) over 24,000 bytes: Antigravity
  truncates it, so the edit is reported back to Claude as a problem to fix now.
- A `packages/*/src/utils/whitelist.ts` copy that no longer matches the reference
  (`whatsapp-client`): the same check as `tests/unit/whitelist-copies.test.ts`, without
  starting vitest, in the checkout that holds the edited file (a mesh worktree, say).
  Reported as context only, since it is expected mid-sync.

Claude-only: agy's PostToolUse output is `{}` and can't feed anything back to the model.
Stdlib only, and silent on its own errors (any exception exits 0 with no output).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_RULE_SIZE_BYTES = 24_000
RULE_FILES = {"CLAUDE.md", "AGENTS.md", "GEMINI.md"}
WHITELIST_REFERENCE = "packages/whatsapp-client/src/utils/whitelist.ts"
WHITELIST_COPIES = (
    "packages/whatsapp-cloud/src/utils/whitelist.ts",
    "packages/telegram-client/src/utils/whitelist.ts",
)


def rule_size_problem(path: Path) -> str | None:
    if path.name not in RULE_FILES or not path.is_file():
        return None
    size = path.stat().st_size
    if size <= MAX_RULE_SIZE_BYTES:
        return None
    return (
        f"{path.name} is now {size:,} bytes, over the {MAX_RULE_SIZE_BYTES:,}-byte limit: "
        "agy truncates it. Move subsystem detail to a folder CLAUDE.md or a linked doc, "
        f"then check it with `wc -c {path}` (`pnpm check:rules` scans AGENTS.md files only)."
    )


def checkout_root(path: Path) -> Path | None:
    """The checkout holding `packages/<pkg>/src/utils/whitelist.ts`, from the path itself."""
    parts = path.parts
    if len(parts) < 6 or parts[-5] != "packages" or parts[-3:-1] != ("src", "utils"):
        return None
    return path.parents[4]


def whitelist_drift(path: Path) -> str | None:
    if path.name != "whitelist.ts":
        return None
    root = checkout_root(path)
    if root is None:
        return None
    try:
        reference = (root / WHITELIST_REFERENCE).read_bytes()
        drifted = [c for c in WHITELIST_COPIES if (root / c).read_bytes() != reference]
    except OSError:
        return None
    if not drifted:
        return None
    return (
        "The whitelist.ts copies differ from the reference "
        f"({WHITELIST_REFERENCE}): {', '.join(drifted)}. They must stay byte-identical "
        "(tests/unit/whitelist-copies.test.ts). Make the change in the reference and `cp` it "
        "over the copies; never hand-merge. Mirror any matching change in "
        "packages/ai-api/src/ai_api/whitelist.py, which must never be more permissive."
    )


def run() -> None:
    payload = json.load(sys.stdin)
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not isinstance(file_path, str) or not file_path:
        return
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(payload.get("cwd") or os.getcwd()) / path

    problem = rule_size_problem(path)
    if problem:
        print(json.dumps({"decision": "block", "reason": problem}))
        return

    drift = whitelist_drift(path)
    if drift:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": drift,
                    }
                }
            )
        )


def main() -> None:
    try:
        run()
    except Exception:  # noqa: BLE001 - a hook must never fail the tool call
        return


if __name__ == "__main__":
    main()
