"""Public bot contact endpoint.

Provides direct WhatsApp and Telegram links for the bot so consumers
(FleetView, app-fogo backend) can display "Chat with bot" links and deep links
without hardcoding numbers or querying platform APIs directly.
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
    """Check whether a client has a non-empty base URL configured."""
    url = get_whatsapp_client_url(client_id)
    return bool(url and url.strip())


def _extract_number(data: dict | None) -> str | None:
    """Extract stripped phone digits from client status response payload."""
    if not data:
        return None
    raw_number = data.get("phoneNumber") or data.get("phone_number")
    if not raw_number:
        return None
    digits = re.sub(r"\D", "", str(raw_number))
    return digits or None


async def _fetch_client_status(client_id: str | None) -> tuple[dict | None, bool]:
    """Query GET /whatsapp/status on the designated messaging client.

    Returns:
        tuple[dict | None, bool]: (data, failed)
        - If client is unconfigured: (None, False)
        - If client answered 2xx: (data_dict, False)
        - If client is absent (connection refused / DNS error / connect timeout): (None, False)
        - If client failed (read timeout, 5xx, or other error): (None, True)
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
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # Client service is not running or absent (e.g. Docker profile not enabled).
        # Do not degrade the cache TTL for unstarted services.
        logger.debug("Contact client %s absent (%s): %s", channel_name, base_url, e)
        return None, False
    except (httpx.HTTPError, WhatsAppClientError) as e:
        logger.warning("%s contact fetch failed or unavailable: %s", channel_name, e)
        return None, True
    except Exception as e:
        logger.warning("%s unexpected error during contact fetch: %s", channel_name, e)
        return None, True


async def _fetch_whatsapp_contact() -> tuple[WhatsAppContact | None, bool]:
    """Fetch the bot's WhatsApp contact handle.

    Queries configured WhatsApp clients (Baileys and Cloud API) concurrently.
    Prefers a client whose status is 'connected' first (Baileys before Cloud).
    Falls back to a non-connected client with a number only when no connected
    client has one.

    Returns:
        tuple[WhatsAppContact | None, bool]: (contact, failed)
        - failed=True triggers short TTL (10s):
          * when no WhatsApp number was found (every bot has WhatsApp)
          * when Baileys failed (timeout, 5xx) and we fell back to Cloud
          * when the number came from a non-connected client
          * when a configured client failed
        - failed=False keeps long TTL (60s)
    """
    baileys_configured = _is_client_configured(None)
    cloud_configured = _is_client_configured("cloud")

    if not baileys_configured and not cloud_configured:
        return None, False

    results = await asyncio.gather(
        _fetch_client_status(None)
        if baileys_configured
        else asyncio.sleep(0, result=(None, False)),
        _fetch_client_status("cloud")
        if cloud_configured
        else asyncio.sleep(0, result=(None, False)),
    )
    (baileys_data, baileys_failed), (cloud_data, cloud_failed) = results

    baileys_number = _extract_number(baileys_data)
    cloud_number = _extract_number(cloud_data)
    baileys_connected = bool(baileys_data and baileys_data.get("status") == "connected")
    cloud_connected = bool(cloud_data and cloud_data.get("status") == "connected")

    chosen_number: str | None = None
    chosen_source: str | None = None

    # 1. Prefer connected client with a number (Baileys before Cloud)
    if baileys_connected and baileys_number:
        chosen_number = baileys_number
        chosen_source = "baileys"
    elif cloud_connected and cloud_number:
        chosen_number = cloud_number
        chosen_source = "cloud"
    # 2. Fall back to non-connected client with a number
    elif baileys_number:
        chosen_number = baileys_number
        chosen_source = "baileys"
    elif cloud_number:
        chosen_number = cloud_number
        chosen_source = "cloud"

    if chosen_number is None:
        # Every bot has WhatsApp: if no number was found for any reason, use short TTL (10s)
        return None, True

    # A number was found. Short TTL (failed=True) only if:
    # - Baileys failed (timeout, 5xx) and we fell back to Cloud
    # - Or the chosen source client is not connected
    # - Or a configured client failed (timeout, 5xx)
    failed = bool(
        baileys_failed
        or cloud_failed
        or (not baileys_connected if chosen_source == "baileys" else not cloud_connected)
    )

    return (
        WhatsAppContact(
            number=chosen_number,
            url=f"https://wa.me/{chosen_number}",
        ),
        failed,
    )


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
        # Running client with no handle yet keeps the short TTL
        return None, True

    return None, failed


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
