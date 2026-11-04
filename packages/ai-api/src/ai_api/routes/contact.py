"""Public bot contact endpoint.

Provides direct WhatsApp and Telegram links for the bot so public clients
(FleetView, app-fogo, mobile app) can display "Chat with bot" buttons without
hardcoding numbers or querying platform APIs directly.
"""

import asyncio
import re
import time

import httpx
from fastapi import APIRouter
from starlette.requests import Request

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..deps import limiter
from ..logger import logger
from ..schemas import ContactResponse, TelegramContact, WhatsAppContact
from ..whatsapp import WhatsAppClientError, create_whatsapp_client

router = APIRouter(tags=["Contact"])

_CACHE_TTL_SECONDS = 60.0
_cached_contact: ContactResponse | None = None
_cached_at: float = 0.0
_cache_lock = asyncio.Lock()


def _clear_contact_cache_for_tests() -> None:
    """Reset the module-level contact cache (for test isolation)."""
    global _cached_contact, _cached_at
    _cached_contact = None
    _cached_at = 0.0


async def _fetch_channel_status(client_id: str | None) -> dict | None:
    """Query a client's GET /whatsapp/status endpoint.

    Returns the parsed response dictionary or None on error/timeout.
    Catches network, HTTP, or client errors and logs at debug level.
    """
    channel_name = client_id or "baileys"
    try:
        base_url = get_whatsapp_client_url(client_id)
        api_key = get_whatsapp_api_key(client_id)
        async with httpx.AsyncClient(timeout=2.0) as http_client:
            client = create_whatsapp_client(
                http_client=http_client,
                base_url=base_url,
                api_key=api_key,
            )
            return await client.get_client_status()
    except (httpx.HTTPError, WhatsAppClientError) as e:
        logger.debug("%s contact fetch failed or unavailable: %s", channel_name, e)
    except Exception as e:
        # Non-network unexpected errors (e.g. malformed JSON) should never fail the route
        logger.debug("%s unexpected error during contact fetch: %s", channel_name, e)
    return None


async def _fetch_whatsapp_contact() -> WhatsAppContact | None:
    """Fetch the bot's WhatsApp contact handle.

    Checks the primary WhatsApp client (Baileys) first, reading its paired phone
    number (which survives socket reconnects). If Baileys is unavailable or has no
    paired number, falls back to the Cloud API client's display phone number.
    """
    for client_id in (None, "cloud"):
        data = await _fetch_channel_status(client_id)
        if not data:
            continue
        raw_number = data.get("phoneNumber") or data.get("phone_number")
        if raw_number:
            digits = re.sub(r"\D", "", str(raw_number))
            if digits:
                return WhatsAppContact(
                    number=digits,
                    url=f"https://wa.me/{digits}",
                )
    return None


async def _fetch_telegram_contact() -> TelegramContact | None:
    """Fetch the bot's Telegram contact handle (username without '@')."""
    data = await _fetch_channel_status("telegram")
    if not data:
        return None
    username = data.get("username")
    if username:
        clean_username = str(username).strip().lstrip("@").strip()
        if clean_username:
            return TelegramContact(
                username=clean_username,
                url=f"https://t.me/{clean_username}",
            )
    return None


@router.get("/contact", response_model=ContactResponse)
@limiter.limit(f"{settings.rate_limit_global}/minute")
async def get_contact(request: Request) -> ContactResponse:
    """Return public contact handles for the bot on WhatsApp and Telegram.

    Unauthenticated endpoint, rate-limited and cached for ~60s. Never fails with
    500 when downstream clients are unavailable; returns null per channel when unknown.
    """
    global _cached_contact, _cached_at

    now = time.monotonic()
    if _cached_contact is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_contact

    async with _cache_lock:
        now = time.monotonic()
        if _cached_contact is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
            return _cached_contact

        wa_result, tg_result = await asyncio.gather(
            _fetch_whatsapp_contact(),
            _fetch_telegram_contact(),
            return_exceptions=True,
        )

        whatsapp_contact = wa_result if isinstance(wa_result, WhatsAppContact) else None
        telegram_contact = tg_result if isinstance(tg_result, TelegramContact) else None

        response = ContactResponse(
            whatsapp=whatsapp_contact,
            telegram=telegram_contact,
        )
        _cached_contact = response
        _cached_at = time.monotonic()
        return response
