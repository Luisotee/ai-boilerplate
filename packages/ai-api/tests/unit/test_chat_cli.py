"""scripts/chat.py: one message through /chat/enqueue and /chat/job polling."""

import json

import httpx

from ai_api.scripts.chat import send


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api")


def test_polls_until_complete():
    seen = []
    polls = iter(["queued", "in_progress", "complete"])

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/chat/enqueue":
            body = json.loads(request.content)
            assert body == {
                "whatsapp_jid": "cli:luis",
                "message": "hi",
                "conversation_type": "private",
            }
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        status = next(polls)
        return httpx.Response(
            200, json={"status": status, "full_response": "hello" if status == "complete" else None}
        )

    assert send(_client(handler), "cli:luis", "hi", poll_interval=0) == "hello"
    assert seen[0] == ("POST", "/chat/enqueue")
    assert seen[1:] == [("GET", "/chat/job/j1")] * 3


def test_command_reply_is_returned_directly():
    def handler(request):
        return httpx.Response(200, json={"is_command": True, "response": "Commands: ..."})

    assert send(_client(handler), "cli:luis", "/help") == "Commands: ..."


def test_failed_job():
    def handler(request):
        if request.url.path == "/chat/enqueue":
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        return httpx.Response(200, json={"status": "failed", "error": "boom"})

    assert send(_client(handler), "cli:luis", "hi", poll_interval=0) == "[job failed: boom]"


def test_blocked_explains_the_gate():
    reply = send(_client(lambda r: httpx.Response(403, json={"detail": "x"})), "cli:luis", "hi")
    assert "localhost" in reply and "cli:luis" in reply


def test_timeout():
    def handler(request):
        if request.url.path == "/chat/enqueue":
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        return httpx.Response(200, json={"status": "queued"})

    reply = send(_client(handler), "cli:luis", "hi", poll_interval=0, timeout=0.01)
    assert reply.startswith("[no reply after")
