"""services/chat_jobs.enqueue_text_job: the /v1 API's save + queue step."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from ai_api.services import chat_jobs
from tests.helpers.factories import make_user


async def test_saves_the_turn_and_queues_a_web_job_on_the_users_jid():
    user = make_user("5511999999999@s.whatsapp.net")  # a key linked to a WhatsApp user
    redis = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield redis

    saved = MagicMock()
    with (
        patch.object(chat_jobs, "create_embedding_service", return_value=None),
        patch.object(chat_jobs, "save_message", return_value=saved) as save,
        patch.object(chat_jobs, "get_redis_client", side_effect=lambda: ctx()),
        patch.object(chat_jobs, "add_message_to_stream", new_callable=AsyncMock) as add,
    ):
        job_id = await chat_jobs.enqueue_text_job(MagicMock(), user, "hello", client_id="web")

    assert save.call_args.args[1:5] == (user.whatsapp_jid, "user", "hello", "private")
    job = add.call_args.kwargs["job_data"]
    assert job["job_id"] == job_id
    assert job["whatsapp_jid"] == user.whatsapp_jid
    assert job["client_id"] == "web"
    assert job["user_message_id"] == str(saved.id)
    assert "whatsapp_message_id" not in job  # nothing to react to
    assert add.call_args.kwargs["user_id"] == str(user.id)
