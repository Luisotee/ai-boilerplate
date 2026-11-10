"""Terminal chat with the bot, through the real pipeline (no WhatsApp needed).

Posts each line to ``POST /chat/enqueue`` as the private chat ``cli:<name>`` and
polls ``GET /chat/job/{id}`` like the chat clients do, so it exercises the
stream worker, the model chain, the tools and the slash commands. Needs the API
and the stream worker running.

    docker compose exec api python -m ai_api.scripts.chat --name luis
    cd packages/ai-api && uv run python -m ai_api.scripts.chat      # local dev

The API accepts a ``cli:`` chat only from a loopback address, or when that exact
``cli:<name>`` is in the whitelist (``routes/chat.py`` ``_is_allowed``). In Docker
a call from the host arrives from the bridge network, so run it inside the
container (as above) or whitelist the name. Replies use WhatsApp markup, as the
chat clients would show them. ``/quit`` or Ctrl-D exits.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import httpx

POLL_INTERVAL_SECONDS = 0.5
TIMEOUT_SECONDS = 120.0
# Kept to characters that are safe in a URL path segment: /admin/users/{jid}/messages
# (FleetView) would 404 on a jid with a "/".
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _name(value: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("use only letters, digits, '_', '.' and '-'")
    return value


def send(
    client: httpx.Client,
    jid: str,
    text: str,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Send one message and return the bot's reply (or an error line)."""
    r = client.post(
        "/chat/enqueue",
        json={"whatsapp_jid": jid, "message": text, "conversation_type": "private"},
    )
    if r.status_code == 403:
        return (
            "[blocked: a cli: chat is allowed only from localhost or when "
            f"{jid} is in WHITELIST_PHONES]"
        )
    r.raise_for_status()
    data = r.json()
    if data.get("is_command"):
        return data.get("response") or ""

    job_id = data["job_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/chat/job/{job_id}")
        if resp.is_error:
            return f"[poll failed: HTTP {resp.status_code} for job {job_id}]"
        job = resp.json()
        if job.get("status") == "complete":
            return job.get("full_response") or ""
        if job.get("status") == "failed":
            return f"[job failed: {job.get('error') or 'unknown error'}]"
        time.sleep(poll_interval)
    return f"[no reply after {timeout:.0f}s; is the stream worker running? job {job_id}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai_api.scripts.chat")
    parser.add_argument(
        "--name", type=_name, default="local", help="chat id suffix: cli:<name> ([A-Za-z0-9_.-])"
    )
    parser.add_argument("--url", default="http://localhost:8000", help="AI API base URL")
    args = parser.parse_args(argv)

    from ..config import settings  # reads AI_API_KEY from the root .env / env

    jid = f"cli:{args.name}"
    sys.stdout.write(f"Chatting as {jid} with {args.url}. /quit or Ctrl-D to exit.\n")
    with httpx.Client(
        base_url=args.url, headers={"X-API-Key": settings.ai_api_key}, timeout=30.0
    ) as client:
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                return 0
            if not text:
                continue
            if text in ("/quit", "/exit"):
                return 0
            try:
                reply = send(client, jid, text)
            except httpx.HTTPError as e:
                reply = f"[request failed: {e}]"
            sys.stdout.write(f"bot> {reply}\n")


if __name__ == "__main__":
    sys.exit(main())
