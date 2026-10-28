# Model output Markdown sanitisation

Detail behind the Markdown rule in the root `CLAUDE.md` / `AGENTS.md`. Read it before changing `formatting.py`, the `formatting_guidance` hook, `DEFAULT_SYSTEM_PROMPT` or the Telegram formatter.

Models drift into Markdown (`**bold**`, `[label](url)`, `#` headings) despite the prompt, and WhatsApp renders it literally. `formatting.markdown_to_whatsapp` (pure, idempotent, never touches code spans or URLs, leaves `---` burst lines byte-for-byte) converts every model-authored text:
- in `streams/processor.py` before delivery/embedding/save, and on the `[Partial - Error]` history row (saved Markdown would be replayed as history and reinforce the habit);
- in the sync `/chat` route;
- in the `send_whatsapp_message` tool.

The formatting *rule* lives in the `formatting_guidance` instructions hook (`agent/core.py`), sent on every run and to every client, because a `bot_prompt` DB override replaces `DEFAULT_SYSTEM_PROMPT` wholesale. Keep `**` out of the default prompt itself (pinned by `tests/mocked/test_formatting_guidance.py`), because it would demonstrate the syntax it forbids.

Telegram composes on top: model Markdown → WhatsApp markup (ai-api) → `---` bursts + Telegram HTML (client). `packages/ai-api/tests/fixtures/markdown_pipeline.json` pins both hops and is asserted from both sides (`tests/unit/test_formatting.py`, telegram-client `tests/unit/markdown-pipeline.test.ts`). Regenerate the expected columns if either converter changes on purpose.
