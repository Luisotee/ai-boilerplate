"""The out-of-band send_whatsapp_message tool bypasses the stream processor's
Markdown conversion, so it must apply markdown_to_whatsapp itself."""

from unittest.mock import AsyncMock, MagicMock

from ai_api.agent.tools.whatsapp import send_whatsapp_message


def _ctx():
    ctx = MagicMock()
    ctx.deps.whatsapp_jid = "5511999999999@s.whatsapp.net"
    ctx.deps.whatsapp_client = AsyncMock()
    ctx.deps.whatsapp_client.send_text.return_value = MagicMock(message_id="wamid-1")
    return ctx


async def test_markdown_is_converted_before_sending():
    ctx = _ctx()
    await send_whatsapp_message(ctx, "**One sec** — checking [the docs](https://example.com)")

    sent = ctx.deps.whatsapp_client.send_text.call_args.kwargs["text"]
    assert sent == "*One sec* — checking the docs: https://example.com"


async def test_whatsapp_markup_is_sent_unchanged():
    ctx = _ctx()
    await send_whatsapp_message(ctx, "*One sec*, _checking_ https://example.com/a_b_c")

    sent = ctx.deps.whatsapp_client.send_text.call_args.kwargs["text"]
    assert sent == "*One sec*, _checking_ https://example.com/a_b_c"
