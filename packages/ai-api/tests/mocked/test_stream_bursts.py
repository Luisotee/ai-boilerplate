"""Burst merging: a user's quick messages (and albums) answered in one run."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

from ai_api.streams import consumer as consumer_module
from ai_api.streams.consumer import plan_runs
from ai_api.streams.manager import GROUP_NAME

USER = "user-1"
STREAM = f"stream:user:{USER}"
JID = "5511999999999@s.whatsapp.net"


def _job(job_id: str, text: str = "hi", **extra) -> dict:
    data = {
        "job_id": job_id,
        "user_id": USER,
        "whatsapp_jid": JID,
        "message": text,
        "conversation_type": "private",
        "user_message_id": f"umid-{job_id}",
        "whatsapp_message_id": f"wamid-{job_id}",
        "client_id": "baileys",
    }
    data.update(extra)
    return data


def _b(data: dict) -> dict:
    return {k.encode(): v.encode() for k, v in data.items()}


@pytest.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis()
    await client.xgroup_create(STREAM, GROUP_NAME, id="0", mkstream=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from ai_api.config import settings

    monkeypatch.setattr(settings, "chat_burst_quiet_ms", 2000)
    monkeypatch.setattr(settings, "chat_burst_max_wait_ms", 8000)


async def _run(redis):
    """One pass of process_user_stream; returns (processor calls, single-entry calls)."""

    async def read(_r, user_id, count=1, block=5000):
        # Non-blocking: everything was queued before the pass, so an empty
        # read is the quiet period ending.
        return await redis.xreadgroup(
            GROUP_NAME, "me", {f"stream:user:{user_id}": ">"}, count=count
        )

    singles: list[str] = []

    async def single(user_id, message_id, data):
        singles.append(data[b"job_id"].decode())

    with (
        patch.object(consumer_module, "read_stream_messages", read),
        patch.object(consumer_module, "process_chat_job_direct", AsyncMock()) as processor,
        patch.object(consumer_module, "process_single_message", side_effect=single),
    ):
        await consumer_module.process_user_stream(redis, USER, {"running": True})
    return processor, singles


async def test_three_quick_messages_become_one_run(redis):
    for i, text in enumerate(["oi", "tudo bem?", "qual o preço?"]):
        await redis.xadd(STREAM, _job(f"j{i}", text))

    processor, singles = await _run(redis)

    assert singles == []
    processor.assert_awaited_once()
    kwargs = processor.await_args.kwargs
    assert kwargs["message"] == "oi\ntudo bem?\nqual o preço?"
    assert kwargs["job_id"] == "j2"  # the last job carries the reply
    assert kwargs["whatsapp_message_id"] == "wamid-j2"
    assert kwargs["absorbed_job_ids"] == ["j0", "j1"]
    assert kwargs["run_user_message_ids"] == ["umid-j0", "umid-j1", "umid-j2"]
    assert (await redis.xpending(STREAM, GROUP_NAME))["pending"] == 0


async def test_images_of_a_burst_all_reach_the_run(redis):
    await redis.xadd(
        STREAM, _job("a", "Image received", has_image="true", image_mimetype="image/jpeg")
    )
    await redis.xadd(
        STREAM,
        _job("b", "look", has_image="true", image_mimetype="image/png", image_count="2"),
    )

    processor, _ = await _run(redis)

    kwargs = processor.await_args.kwargs
    assert kwargs["has_image"] is True
    assert kwargs["image_sources"] == [("a", "image/jpeg", 1), ("b", "image/png", 2)]


async def test_merging_off_runs_each_message_alone(redis, monkeypatch):
    from ai_api.config import settings

    monkeypatch.setattr(settings, "chat_burst_quiet_ms", 0)
    for i in range(3):
        await redis.xadd(STREAM, _job(f"j{i}"))

    processor, singles = await _run(redis)

    assert singles == ["j0", "j1", "j2"]
    processor.assert_not_called()


async def test_scheduled_entry_is_never_merged(redis):
    await redis.xadd(STREAM, _job("t1"))
    await redis.xadd(STREAM, _job("t2"))
    await redis.xadd(
        STREAM, {"trigger": "scheduled", "run_id": "r1", "job_id": "s1", "user_id": USER}
    )
    await redis.xadd(STREAM, _job("t3"))

    processor, singles = await _run(redis)

    # t1+t2 merged, then the scheduled entry alone, then t3 alone (in order)
    assert processor.await_args_list[0].kwargs["absorbed_job_ids"] == ["t1"]
    assert singles == ["s1", "t3"]


async def test_finished_job_inside_a_burst_is_skipped(redis):
    await redis.xadd(STREAM, _job("done"))
    await redis.xadd(STREAM, _job("x"))
    await redis.xadd(STREAM, _job("y"))
    await redis.set("job:meta:done", json.dumps({"total_chunks": 1}))

    processor, _ = await _run(redis)

    assert processor.await_args.kwargs["absorbed_job_ids"] == ["x"]


class TestPlanRuns:
    def _ids(self, runs):
        return [[data[b"job_id"].decode() for _mid, data in run] for run in runs]

    def test_pdf_job_runs_alone(self):
        entries = [
            ("1-0", _b(_job("a"))),
            ("2-0", _b(_job("pdf", has_document="true"))),
            ("3-0", _b(_job("b"))),
        ]
        assert self._ids(plan_runs(entries)) == [["a"], ["pdf"], ["b"]]

    def test_different_group_senders_are_not_merged(self):
        group = {"conversation_type": "group", "whatsapp_jid": "1203@g.us"}
        entries = [
            ("1-0", _b(_job("a", sender_name="Ana", **group))),
            ("2-0", _b(_job("b", sender_name="Ana", **group))),
            ("3-0", _b(_job("c", sender_name="Bia", **group))),
        ]
        assert self._ids(plan_runs(entries)) == [["a", "b"], ["c"]]

    def test_admin_flag_must_match(self):
        entries = [
            ("1-0", _b(_job("a", is_group_admin="true"))),
            ("2-0", _b(_job("b", is_group_admin="false"))),
        ]
        assert self._ids(plan_runs(entries)) == [["a"], ["b"]]

    def test_image_cap_starts_a_new_run(self):
        img = {"has_image": "true", "image_mimetype": "image/jpeg"}
        entries = [
            ("1-0", _b(_job("a", image_count="8", **img))),
            ("2-0", _b(_job("b", image_count="3", **img))),
        ]
        assert self._ids(plan_runs(entries)) == [["a"], ["b"]]


class TestProcessorClosesAbsorbedJobs:
    async def test_absorbed_jobs_read_silent(self):
        from httpx import ASGITransport, AsyncClient

        from ai_api.main import app
        from ai_api.streams import processor as proc_module

        fake = fakeredis.FakeAsyncRedis()

        @asynccontextmanager
        async def get_client():
            yield fake

        async def agent(*_a, **_kw):
            yield "one answer"

        with (
            patch.object(proc_module, "SessionLocal", return_value=MagicMock()),
            patch.object(proc_module, "get_conversation_history", return_value=[]),
            patch.object(proc_module, "create_embedding_service", return_value=None),
            patch.object(proc_module, "create_whatsapp_client", return_value=AsyncMock()),
            patch.object(proc_module, "get_ai_response", agent),
            patch.object(proc_module, "get_redis_client", get_client),
            patch.object(proc_module, "save_message", return_value=MagicMock(id="m")),
        ):
            await proc_module.process_chat_job_direct(
                user_id="u",
                whatsapp_jid=JID,
                message="a\nb",
                conversation_type="private",
                user_message_id="m2",
                job_id="carrier",
                absorbed_job_ids=["early"],
            )

        with (
            patch("ai_api.main.init_db"),
            patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
            patch("ai_api.main.cleanup_expired_documents"),
            patch("ai_api.routes.chat.get_redis_client", get_client),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                headers = {"X-API-Key": "test-api-key"}
                early = (await c.get("/chat/job/early", headers=headers)).json()
                carrier = (await c.get("/chat/job/carrier", headers=headers)).json()

        assert early["complete"] is True and early["silent"] is True
        assert carrier["full_response"] == "one answer" and carrier["silent"] is False


async def test_agent_prompt_carries_every_image(monkeypatch):
    from pydantic_ai import BinaryContent

    from ai_api.agent import response as response_module

    captured = {}

    class FakeAgent:
        def run_stream_events(self, prompt, **_kw):
            captured["prompt"] = prompt

            async def events():
                return
                yield  # pragma: no cover

            return events()

    monkeypatch.setattr(response_module, "agent", FakeAgent())
    monkeypatch.setattr(response_module, "build_runtime_model", lambda: None)

    async for _ in response_module.get_ai_response(
        "two photos", None, images=[("aGk=", "image/jpeg"), ("aGk=", "image/png")]
    ):
        pass

    prompt = captured["prompt"]
    assert prompt[0] == "two photos"
    assert [p.media_type for p in prompt[1:] if isinstance(p, BinaryContent)] == [
        "image/jpeg",
        "image/png",
    ]
