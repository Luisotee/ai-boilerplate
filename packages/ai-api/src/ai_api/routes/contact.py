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
_SHORT_CACHE_TTL_SECONDS = 10.0
_cached_contact: ContactResponse | None = None
_cached_at: float = 0.0
_cached_ttl: float = _CACHE_TTL_SECONDS
_cache_lock = asyncio.Lock()


def _clear_contact_cache_for_tests() -> None:
    """Reset the module-level contact cache (for test isolation)."""
    global _cached_contact, _cached_at, _cached_ttl
    _cached_contact = None
    _cached_at = 0.0
    _cached_ttl = _CACHE_TTL_SECONDS


def _is_client_configured(client_id: str | None) -> bool:
    """Check whether a client has a non-empty base URL configured.

    Returns False if the URL setting is missing (e.g. peer bots without Telegram),
    None, or empty string.
    """
    try:
        url = get_whatsapp_client_url(client_id)
        return bool(url and url.strip())
    except (AttributeError, Exception):
        return False


async def _fetch_client_status(client_id: str | None) -> tuple[dict | None, bool]:
    """Query GET /whatsapp/status on the designated messaging client.

    Returns:
        tuple[dict | None, bool]: (data, failed)
        - If client is unconfigured: (None, False)
        - If client answered 2xx: (data_dict, False)
        - If client failed (network, timeout, non-2xx): (None, True)
    """
    if not _is_client_configured(client_id):
        return None, False

    channel_name = client_id or "baileys"
    base_url = get_whatsapp_client_url(client_id)
    api_key = get_whatsapp_api_key(client_id)
    try:
        async with httpx.AsyncClient(timeout=2.0) as http_client:
            client = create_whatsapp_client(
                http_client=http_client,
                base_url=base_url,
                api_key=api_key,
            )
            data = await client.get_client_status()
            return data, False
    except (httpx.HTTPError, WhatsAppClientError) as e:
        logger.debug("%s contact fetch failed or unavailable: %s", channel_name, e)
        return None, True
    except Exception as e:
        logger.debug("%s unexpected error during contact fetch: %s", channel_name, e)
        return None, True


async def _fetch_whatsapp_contact() -> tuple[WhatsAppContact | None, bool]:
    """Fetch the bot's WhatsApp contact handle.

    Checks configured WhatsApp clients (Baileys first, Cloud fallback).
    Returns (WhatsAppContact | None, failed).
    """
    for client_id in (None, "cloud"):
        if not _is_client_configured(client_id):
            continue
        data, failed = await _fetch_client_status(client_id)
        if failed:
            return None, True
        if data:
            raw_number = data.get("phoneNumber") or data.get("phone_number")
            if raw_number:
                digits = re.sub(r"\D", "", str(raw_number))
                if digits:
                    return (
                        WhatsAppContact(
                            number=digits,
                            url=f"https://wa.me/{digits}",
                        ),
                        False,
                    )
    return None, False


async def _fetch_telegram_contact() -> tuple[TelegramContact | None, bool]:
    """Fetch the bot's Telegram contact handle (username without '@').

    Returns (TelegramContact | None, failed).
    """
    if not _is_client_configured("telegram"):
        return None, False

    data, failed = await _fetch_client_status("telegram")
    if failed:
        return None, True

    if data:
        username = data.get("username")
        if username:
            clean_username = str(username).strip().lstrip("@").strip()
            if clean_username:
                return (
                    TelegramContact(
                        username=clean_username,
                        url=f"https://t.me/{clean_username}",
                    ),
                    False,
                )

    return None, False


@router.get("/contact", response_model=ContactResponse)
@limiter.limit(f"{settings.rate_limit_global}/minute")
async def get_contact(request: Request) -> ContactResponse:
    """Return public contact handles for the bot on WhatsApp and Telegram.

    Unauthenticated endpoint, rate-limited and cached for ~60s (or ~10s if any
    configured client failed). Never fails with 500 when downstream clients are
    unavailable; returns null per channel when unknown.
    """
    global _cached_contact, _cached_at, _cached_ttl

    now = time.monotonic()
    if _cached_contact is not None and (now - _cached_at) < _cached_ttl:
        return _cached_contact

    async with _cache_lock:
        now = time.monotonic()
        if _cached_contact is not None and (now - _cached_at) < _cached_ttl:
            return _cached_contact

        wa_result, tg_result = await asyncio.gather(
            _fetch_whatsapp_contact(),
            _fetch_telegram_contact(),
            return_exceptions=True,
        )

        whatsapp_contact = (
            wa_result[0]
            if isinstance(wa_result, tuple) and isinstance(wa_result[0], WhatsAppContact)
            else None
        )
        telegram_contact = (
            tg_result[0]
            if isinstance(tg_result, tuple) and isinstance(tg_result[0], TelegramContact)
            else None
        )

        wa_failed = wa_result[1] if isinstance(wa_result, tuple) else True
        tg_failed = tg_result[1] if isinstance(tg_result, tuple) else True

        ttl = _SHORT_CACHE_TTL_SECONDS if (wa_failed or tg_failed) else _CACHE_TTL_SECONDS

        response = ContactResponse(
            whatsapp=whatsapp_contact,
            telegram=telegram_contact,
        )
        _cached_contact = response
        _cached_at = time.monotonic()
        _cached_ttl = ttl
        return response
