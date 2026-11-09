"""Tests for image directives in replies (ai_api/reply_media.py)."""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from ai_api import reply_media
from ai_api.formatting import markdown_to_whatsapp
from ai_api.reply_media import (
    MAX_CAPTION_CHARS,
    ReplyImage,
    collect_reply_media,
    extract_image_directives,
)
from ai_api.safe_fetch import FetchedImage, ImageFetchError

PNG = FetchedImage(data=b"\x89PNG\r\n\x1a\n" + b"0" * 10, mimetype="image/png")
JPEG = FetchedImage(data=b"\xff\xd8\xff" + b"0" * 10, mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# extract_image_directives
# ---------------------------------------------------------------------------


def urls(text: str) -> list[str]:
    return [d.image.url for d in extract_image_directives(text)]


def test_extracts_caption_and_url():
    (d,) = extract_image_directives("Here:\n![Fire map](https://maps.example.com/a.png)\nBye")
    assert d.image == ReplyImage(url="https://maps.example.com/a.png", caption="Fire map")


def test_empty_caption_allowed():
    (d,) = extract_image_directives("![](https://x.example.com/a.png)")
    assert d.image.caption == ""


def test_only_https():
    assert urls("![a](http://x.example.com/a.png) ![b](ftp://x.example.com/b.png)") == []


def test_plain_links_are_not_images():
    assert urls("[a](https://x.example.com/a.png)") == []


def test_ignores_code_fence_and_inline_code():
    text = (
        "```\n![a](https://x.example.com/fence.png)\n```\n"
        "Use `![b](https://x.example.com/inline.png)` like this.\n"
        "![c](https://x.example.com/real.png)"
    )
    assert urls(text) == ["https://x.example.com/real.png"]


def test_caption_whitespace_collapsed_and_truncated():
    long = "word " * 400
    (d,) = extract_image_directives(f"![{long}](https://x.example.com/a.png)")
    assert len(d.image.caption) == MAX_CAPTION_CHARS
    assert "  " not in d.image.caption


def test_directive_grammar_has_no_chat_target():
    """Images only ever go to the chat the job came from: nothing in the
    directive or the API names another conversation."""
    params = inspect.signature(collect_reply_media).parameters
    assert not {"jid", "whatsapp_jid", "chat_id", "target"} & set(params)
    assert set(ReplyImage.__dataclass_fields__) == {"url", "caption"}


# ---------------------------------------------------------------------------
# collect_reply_media
# ---------------------------------------------------------------------------


async def collect(text, fetch_results, extra=None):
    """Run collect_reply_media with fetches answered from `fetch_results`
    (url -> FetchedImage | Exception) and storage mocked."""

    async def fake_fetch(url, **_kwargs):
        result = fetch_results[url]
        if isinstance(result, Exception):
            raise result
        return result

    save = AsyncMock()
    with (
        patch.object(reply_media, "fetch_public_image", fake_fetch),
        patch.object(reply_media, "save_job_media", save),
    ):
        result = await collect_reply_media(AsyncMock(), "job-1", text, extra)
    return result, save


async def test_no_directive_returns_text_unchanged():
    result, save = await collect("Plain *reply*\n", {})
    assert result.text == "Plain *reply*\n"
    assert result.media == []
    save.assert_not_awaited()


async def test_delivered_directive_removed_and_stored():
    text = "Here is the map:\n\n![Fire map](https://m.example.com/a.png)\n\nStay safe."
    result, save = await collect(text, {"https://m.example.com/a.png": PNG})

    assert result.text == "Here is the map:\n\nStay safe."
    assert result.media == [
        {
            "index": 0,
            "caption": "Fire map",
            "mimetype": "image/png",
            "url": "https://m.example.com/a.png",
        }
    ]
    save.assert_awaited_once()
    assert save.call_args.args[1:] == ("job-1", 0, PNG.data)
    assert result.history_lines == ["[Image sent: Fire map] https://m.example.com/a.png"]


async def test_images_only_reply_leaves_empty_text():
    result, _ = await collect(
        "![x](https://m.example.com/a.png)", {"https://m.example.com/a.png": PNG}
    )
    assert result.text == ""
    assert len(result.media) == 1


async def test_failed_fetch_stays_as_text_and_is_flattened_to_a_link():
    text = "See ![Map](https://m.example.com/missing.png)"
    result, save = await collect(
        text, {"https://m.example.com/missing.png": ImageFetchError("server answered 404")}
    )
    assert result.media == []
    save.assert_not_awaited()
    assert markdown_to_whatsapp(result.text) == "See Map: https://m.example.com/missing.png"


async def test_unexpected_fetch_error_is_skipped_not_raised():
    result, _ = await collect(
        "![a](https://m.example.com/a.png)", {"https://m.example.com/a.png": RuntimeError("boom")}
    )
    assert result.media == []


async def test_mixed_success_and_failure_indexes_are_dense():
    text = (
        "![one](https://m.example.com/1.png)\n"
        "![two](https://m.example.com/2.png)\n"
        "![three](https://m.example.com/3.jpg)"
    )
    result, _ = await collect(
        text,
        {
            "https://m.example.com/1.png": PNG,
            "https://m.example.com/2.png": ImageFetchError("image is too large"),
            "https://m.example.com/3.jpg": JPEG,
        },
    )
    assert [(m["index"], m["caption"]) for m in result.media] == [(0, "one"), (1, "three")]
    assert result.text == "![two](https://m.example.com/2.png)"


async def test_duplicates_fetched_once_and_all_copies_removed():
    text = "![a](https://m.example.com/a.png)\n![a again](https://m.example.com/a.png)\nok"
    fetches = []

    async def fake_fetch(url, **_kwargs):
        fetches.append(url)
        return PNG

    with (
        patch.object(reply_media, "fetch_public_image", fake_fetch),
        patch.object(reply_media, "save_job_media", AsyncMock()),
    ):
        result = await collect_reply_media(AsyncMock(), "job-1", text)
    assert fetches == ["https://m.example.com/a.png"]
    assert len(result.media) == 1
    assert result.text == "ok"


async def test_cap_leaves_extra_directives_as_text():
    lines = [f"![{i}](https://m.example.com/{i}.png)" for i in range(6)]
    results = {f"https://m.example.com/{i}.png": PNG for i in range(6)}
    result, _ = await collect("\n".join(lines), results)
    assert len(result.media) == 4
    assert result.text == "\n".join(lines[4:])


async def test_tool_added_images_come_first_and_count_toward_cap():
    extra = [ReplyImage(url="https://tool.example.com/clip.png", caption="Clip")]
    result, _ = await collect(
        "Text ![d](https://m.example.com/d.png)",
        {"https://tool.example.com/clip.png": PNG, "https://m.example.com/d.png": JPEG},
        extra,
    )
    assert [m["url"] for m in result.media] == [
        "https://tool.example.com/clip.png",
        "https://m.example.com/d.png",
    ]
    assert result.text == "Text"


async def test_storage_failure_skips_the_image():
    save = AsyncMock(side_effect=ConnectionError("redis down"))
    with (
        patch.object(reply_media, "fetch_public_image", AsyncMock(return_value=PNG)),
        patch.object(reply_media, "save_job_media", save),
    ):
        result = await collect_reply_media(
            AsyncMock(), "job-1", "hi ![a](https://m.example.com/a.png)"
        )
    assert result.media == []
    assert "https://m.example.com/a.png" in result.text


@pytest.mark.parametrize("burst", ["Part one\n---\n![a](https://m.example.com/a.png)\nPart two"])
async def test_burst_delimiters_survive(burst):
    result, _ = await collect(burst, {"https://m.example.com/a.png": PNG})
    assert result.text == "Part one\n---\nPart two"
