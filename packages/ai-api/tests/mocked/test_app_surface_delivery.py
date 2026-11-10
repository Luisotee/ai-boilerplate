"""App-surface jobs (/v1 web API, terminal chat) on the chat pipeline: no burst
merging, no late push, no reply-media extraction, no scheduled tasks."""

from unittest.mock import AsyncMock, patch

import pytest

from ai_api.services import scheduled_tasks as svc
from ai_api.streams import delivery
from ai_api.streams.consumer import plan_runs
from tests.mocked.test_scheduled_tasks_tool import _create
from tests.mocked.test_stream_bursts import _b, _job
from tests.mocked.test_stream_processor import _make_streaming_agent, _run_processor

WA = "5511999999999@s.whatsapp.net"


@pytest.mark.parametrize(
    "extra",
    [
        {"client_id": "web"},  # a key on a WhatsApp user
        {"client_id": "web", "whatsapp_jid": "web:abc"},
        {"client_id": "cli", "whatsapp_jid": "cli:luis"},
    ],
)
def test_app_jobs_never_merge(extra):
    entries = [(f"{i}-0", _b(_job(f"j{i}", **extra))) for i in range(3)]
    assert [len(run) for run in plan_runs(entries)] == [1, 1, 1]


def test_chat_client_jobs_still_merge():
    entries = [(f"{i}-0", _b(_job(f"j{i}"))) for i in range(3)]
    assert [len(run) for run in plan_runs(entries)] == [3]


@pytest.mark.parametrize("client_id,jid", [("web", WA), (None, "web:abc"), ("cli", "cli:luis")])
async def test_push_text_once_skips_app_jobs(client_id, jid):
    with patch.object(delivery, "deliver_if_late", AsyncMock()) as deliver:
        assert not await delivery.push_text_once(AsyncMock(), client_id, "j", jid, "hi", 0)
    deliver.assert_not_awaited()


async def test_push_text_once_still_pushes_chat_jobs():
    with patch.object(delivery, "deliver_if_late", AsyncMock(return_value=True)) as deliver:
        assert await delivery.push_text_once(AsyncMock(), None, "j", WA, "hi", 0)
    deliver.assert_awaited_once()


class TestProcessor:
    REPLY = "Look: ![a cat](https://example.com/cat.png)\n**done**"

    async def _run(self, client_id, jid):
        from ai_api.streams import processor as proc_module

        with (
            patch.object(proc_module, "collect_reply_media", AsyncMock()) as collect,
            patch.object(proc_module, "deliver_if_late", AsyncMock()) as deliver,
        ):
            collect.side_effect = lambda _r, _j, text, _m: proc_module.ReplyMedia(
                text=text, media=[], history_lines=[]
            )
            _res, save_chunk, meta, _save, _wa = await _run_processor(
                _make_streaming_agent([self.REPLY]),
                whatsapp_jid=jid,
                client_id=client_id,
                whatsapp_message_id=None,
            )
        return collect, deliver, save_chunk, meta

    @pytest.mark.parametrize("client_id,jid", [("web", WA), ("web", "web:abc")])
    async def test_web_reply_keeps_images_inline_and_is_never_pushed(self, client_id, jid):
        collect, deliver, save_chunk, meta = await self._run(client_id, jid)
        collect.assert_not_awaited()
        assert save_chunk.call_args.args[3] == self.REPLY  # Markdown, image inline
        assert "![a cat](https://example.com/cat.png)" in save_chunk.call_args.args[3]
        assert meta.call_args.args[2]["media"] == []
        assert deliver.call_args.args[1] is None  # no client to push through

    async def test_chat_reply_still_extracts_media_and_can_push(self):
        collect, deliver, _chunk, _meta = await self._run(None, WA)
        collect.assert_awaited_once()
        assert deliver.call_args.args[1] is not None


@pytest.mark.parametrize("address,client_id", [("web:abc", None), ("cli:luis", None), (WA, "web")])
def test_scheduled_task_refused_for_app_surfaces(address, client_id):
    with patch("ai_api.services.scheduled_tasks.runtime_config") as rc:
        rc.get.return_value = 10
        with pytest.raises(svc.TaskError, match="WhatsApp and Telegram"):
            _create(address=address, client_id=client_id)
