"""Reply images in the stream processor: directives become job `media`."""

from unittest.mock import AsyncMock, patch

import pytest

from ai_api import reply_media
from ai_api.safe_fetch import FetchedImage, ImageFetchError

from .test_stream_processor import _make_streaming_agent, _run_processor

PNG = FetchedImage(data=b"\x89PNG\r\n\x1a\n" + b"0" * 10, mimetype="image/png")
URL = "https://maps.example.com/fire.png"


@pytest.fixture(autouse=True)
def _no_bug_reports():
    from ai_api.streams import processor as proc_module

    with patch.object(proc_module, "record_auto_report"):
        yield


async def run(tokens, fetch):
    save_media = AsyncMock()
    with (
        patch.object(reply_media, "fetch_public_image", fetch),
        patch.object(reply_media, "save_job_media", save_media),
    ):
        out = await _run_processor(_make_streaming_agent(tokens))
    return (*out, save_media)


async def test_directive_becomes_media_and_leaves_the_text():
    _, save_chunk, set_meta, save_message, _, save_media = await run(
        ["**Here** is the map:\n", f"![Fire map]({URL})\n", "Stay safe."],
        AsyncMock(return_value=PNG),
    )

    delivered = save_chunk.call_args.args[3]
    assert delivered == "*Here* is the map:\nStay safe."  # converted, directive gone
    assert "https://" not in delivered  # so TTS never reads the URL

    meta = set_meta.call_args.args[2]
    assert meta["media"] == [
        {"index": 0, "caption": "Fire map", "mimetype": "image/png", "url": URL}
    ]
    save_media.assert_awaited_once()

    saved = save_message.call_args.args[3]
    assert saved == f"*Here* is the map:\nStay safe.\n![Fire map]({URL})"


async def test_images_only_reply_saves_an_empty_chunk():
    _, save_chunk, set_meta, save_message, _, _ = await run(
        [f"![]({URL})"], AsyncMock(return_value=PNG)
    )
    assert save_chunk.call_args.args[3] == ""
    assert len(set_meta.call_args.args[2]["media"]) == 1
    assert save_message.call_args.args[3] == f"![]({URL})"


async def test_failed_image_is_flattened_to_a_link():
    _, save_chunk, set_meta, _, _, save_media = await run(
        [f"See ![Map]({URL})"], AsyncMock(side_effect=ImageFetchError("host is not allowed"))
    )
    assert save_chunk.call_args.args[3] == f"See Map: {URL}"
    assert set_meta.call_args.args[2]["media"] == []
    save_media.assert_not_awaited()


async def test_media_handling_crash_never_fails_the_reply():
    with patch(
        "ai_api.streams.processor.collect_reply_media", AsyncMock(side_effect=RuntimeError("x"))
    ):
        result, save_chunk, set_meta, _, _ = await _run_processor(
            _make_streaming_agent([f"Text ![a]({URL})"])
        )
    assert result["success"] is True
    assert save_chunk.call_args.args[3] == f"Text a: {URL}"
    assert set_meta.call_args.args[2]["media"] == []


async def test_plain_reply_has_empty_media_and_unchanged_history():
    _, save_chunk, set_meta, save_message, _, _ = await run(
        ["Just text "], AsyncMock(side_effect=AssertionError("no fetch expected"))
    )
    assert set_meta.call_args.args[2]["media"] == []
    assert save_message.call_args.args[3] == save_chunk.call_args.args[3]
