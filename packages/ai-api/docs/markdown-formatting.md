# Model output Markdown sanitisation

Detail behind the Markdown rule in the root `CLAUDE.md` / `AGENTS.md`. Read it before changing `formatting.py`, the `formatting_guidance` hook, `DEFAULT_SYSTEM_PROMPT` or the Telegram formatter.

Models drift into Markdown (`**bold**`, `[label](url)`, `#` headings) despite the prompt, and WhatsApp renders it literally. `formatting.markdown_to_whatsapp` (pure, idempotent, never touches code spans or URLs, leaves `---` burst lines byte-for-byte) converts every model-authored text:
- in `streams/processor.py` before delivery/embedding/save, and on the `[Partial - Error]` history row (saved Markdown would be replayed as history and reinforce the habit);
- in the sync `/chat` route;
- in the `send_whatsapp_message` tool.

The formatting *rule* lives in the `formatting_guidance` instructions hook (`agent/core.py`), sent on every run and to every client, because a `bot_prompt` DB override replaces `DEFAULT_SYSTEM_PROMPT` wholesale. Keep `**` out of the default prompt itself (pinned by `tests/mocked/test_formatting_guidance.py`), because it would demonstrate the syntax it forbids.

Telegram composes on top: model Markdown → WhatsApp markup (ai-api) → `---` bursts + Telegram HTML (client). `packages/ai-api/tests/fixtures/markdown_pipeline.json` pins both hops and is asserted from both sides (`tests/unit/test_formatting.py`, telegram-client `tests/unit/markdown-pipeline.test.ts`). Regenerate the expected columns if either converter changes on purpose.

## Reply images

The model sends an image by writing `![caption](https://…)` in its reply (the rule is in `formatting_guidance`). A tool can also queue a `reply_media.ReplyImage` on `AgentDeps.reply_media`. `reply_media.collect_reply_media` runs in the processor **before** `markdown_to_whatsapp`, which would otherwise flatten the directive into `caption: url`:
- Directives inside code fences or spans are ignored; only `https` URLs count; duplicates are fetched once; up to 8 are tried and the first 4 that arrive are sent (`MAX_JOB_MEDIA`), the rest stay links; a directive alone on its line (a list marker aside) takes the line with it, and a linked image `[![a](img)](link)` leaves its link as a bare URL; captions get the same Markdown conversion and are cut to 1,024 characters.
- Each image is downloaded by `safe_fetch.fetch_public_image`, the only code that fetches a model-chosen URL. It allows https only, refuses localhost, single-label names (Docker services) and any non-public address (IPv6 forms embedding an IPv4 are unwrapped: mapped, compatible, SIIT, NAT64; site-local `fec0::/10` is refused), converts IDN hosts to punycode, pins a checked address (`Host` + `sni_hostname`, so DNS can't rebind; IPv4 first, the next address when one can't connect), re-checks every redirect, and accepts JPEG/PNG up to 5 MB (`image/jpg` and `image/pjpeg` count as JPEG), checking the magic bytes and enforcing the cap while streaming.
- The bytes go to Redis (`job:media:{job_id}:{index}`, 10-minute TTL `JOB_MEDIA_TTL_SECONDS`) and the job metadata lists `media: [{index, caption, mimetype, url}]`, returned by GET `/chat/job/{id}` once complete. Delivered directives are removed from the text; a failed one stays and becomes a plain link.
- The saved history row gets one `![caption](url)` line per image, so the model knows what it sent and sees the form that works. The delivered text (and so TTS) never contains the URL.
- The clients send the images after the text and TTS (`utils/reply-media.ts`, bytes from GET `/chat/job/{id}/media/{index}`). Baileys goes through `sendSerialized` and never uses `{image: {url}}`, because Baileys would fetch the URL from inside the Docker network. Cloud uploads the bytes, because a `link` failure arrives only later by webhook. Telegram uses `sendPhoto` with the bytes and an HTML caption; an images-only reply quotes the triggering message, like a text reply. Any failure sends the caption and link as text, never the ❌ reaction. A private or http image host (e.g. an internal GeoServer) is refused by design and would need an explicit allowlist.
