#!/usr/bin/env python3
"""PreToolUse hook: stop agents from hand-editing secrets and lockfiles.

Shared by Claude Code (`.claude/settings.json`) and Antigravity (`.agents/hooks.json`).
The payload shape tells them apart: agy sends `toolCall.args`, Claude `tool_input`.

- `.env`, `.env.local`, `.env.<anything>` except `.env.example`: ask the user first,
  because they hold the shared secrets (`./setup.sh --update` adds new keys).
- `pnpm-lock.yaml`, `uv.lock`: deny, because only `pnpm add` / `uv add` may change them.

Stdlib only, and it never fails the tool call on its own errors.
"""

from __future__ import annotations

import json
import sys
from pathlib import PurePath

LOCKFILES = {"pnpm-lock.yaml", "uv.lock"}


def edited_path(payload: dict) -> tuple[str, bool]:
    """Return (path, is_agy) for the file the tool call is about to write."""
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args") or {}
        for key in ("Path", "AbsolutePath", "TargetFile", "path", "file_path"):
            if args.get(key):
                return str(args[key]), True
        return "", True
    tool_input = payload.get("tool_input") or {}
    return str(tool_input.get("file_path") or ""), False


def verdict(path: str) -> tuple[str, str] | None:
    """Return (decision, reason) for a guarded path, or None to let it through."""
    name = PurePath(path).name
    if name in LOCKFILES:
        return (
            "deny",
            f"{name} is generated: change dependencies with `pnpm add` / `uv add` "
            "(never by editing package.json, pyproject.toml or the lockfile).",
        )
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return (
            "ask",
            f"{name} holds this deployment's secrets. New keys belong in .env.example "
            "(picked up with `./setup.sh --update`); edit the real file only if the user wants it.",
        )
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    path, is_agy = edited_path(payload if isinstance(payload, dict) else {})
    result = verdict(path) if path else None

    if is_agy:
        # agy requires a decision on every PreToolUse reply; "allow" is what
        # Google's own plugins return to pass a call through.
        decision, reason = result or ("allow", "")
        out = {"decision": decision}
        if reason:
            out["reason"] = reason
        print(json.dumps(out))
        return

    if result:
        decision, reason = result
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": decision,
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
