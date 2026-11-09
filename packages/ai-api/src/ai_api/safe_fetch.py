"""
SSRF-hardened image download for URLs chosen by the model (or a user).

Every model-chosen URL goes through `fetch_public_image`; nothing else in the
stack may fetch one. Per hop it:

- accepts only `https` URLs without credentials, and refuses `localhost` and
  single-label names (Docker service names such as `redis`);
- resolves the host once and refuses it if ANY address is not globally
  routable (loopback, RFC 1918 / Docker bridges, link-local and cloud
  metadata, CGNAT, ULA, ...), unwrapping IPv6 forms that embed an IPv4
  address first (mapped, compatible, SIIT, NAT64);
- connects to an address it checked (IPv4 first, the next one when a
  connection cannot be opened), sending the real name as `Host` and as
  the TLS SNI / certificate name (httpx's `sni_hostname` extension), so a
  second DNS answer can never redirect the connection (no rebinding gap);
- follows redirects by hand, re-validating each `Location`;
- checks status, `Content-Length` and `content-type`, streams the body with a
  hard size cap, and sniffs the magic bytes.

Errors are `ImageFetchError` with a short, generic reason safe to log; the
detail goes to the log only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from .logger import logger

#: WhatsApp Cloud API image limit; also under Telegram's 10 MB photo upload.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 20.0

#: Types every client can send as a photo (Cloud API accepts only these two).
_ALLOWED_TYPES = ("image/jpeg", "image/png")
_MAGIC = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}
#: Non-standard names some servers send for JPEG.
_TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
_REDIRECT_CODES = {301, 302, 303, 307, 308}

# IPv6 ranges whose low 32 bits are an IPv4 address. Python calls most of
# them global (e.g. 64:ff9b::7f00:1, ::127.0.0.1, ::ffff:0:a9fe:a9fe), so
# the embedded IPv4 is judged instead.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


class ImageFetchError(Exception):
    """The image could not be fetched safely. `str(e)` is a generic reason."""


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    mimetype: str


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or ip in _SIIT or ip in _IPV4_COMPATIBLE:
            # :: and ::1 fall in ::/96 too; as 0.0.0.0 / 0.0.0.1 they stay rejected.
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    # fec0::/10 (deprecated site-local) is still reported as global.
    return ip.is_global and not ip.is_multicast and not getattr(ip, "is_site_local", False)


def _validate_url(url: str) -> tuple[str, int]:
    """Return (hostname, port) or raise. Pure: no DNS."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ImageFetchError("invalid URL") from e
    if parts.scheme.lower() != "https":
        raise ImageFetchError("only https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise ImageFetchError("URLs with credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ImageFetchError("URL has no host")
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if not is_literal:
        # IDN to its ASCII (punycode) form: the Host header and SNI need it.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise ImageFetchError("invalid URL") from e
        if host == "localhost" or host.endswith(".localhost") or "." not in host:
            raise ImageFetchError("host is not allowed")
    return host, port or 443


async def _resolve_public(host: str, port: int) -> list[str]:
    """Resolve `host` to its addresses, IPv4 first, refusing if any is non-public."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            raise ImageFetchError("host could not be resolved") from e
        addresses = []
        for info in infos:
            raw = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 zone id
            try:
                addresses.append(ipaddress.ip_address(raw))
            except ValueError as e:
                raise ImageFetchError("host could not be resolved") from e
        if not addresses:
            raise ImageFetchError("host could not be resolved")
    for ip in addresses:
        if not _is_public(ip):
            logger.warning(f"Blocked image fetch: {host} resolves to non-public {ip}")
            raise ImageFetchError("host is not allowed")
    # IPv4 first: a dual-stack name must still work on an IPv4-only network.
    unique = list(dict.fromkeys(addresses))
    return [str(ip) for ip in sorted(unique, key=lambda ip: ip.version)]


def _pinned_request(
    client: httpx.AsyncClient, url: str, host: str, port: int, ip: str
) -> httpx.Request:
    parts = urlsplit(url)
    ip_host = f"[{ip}]" if ":" in ip else ip
    netloc = ip_host if port == 443 else f"{ip_host}:{port}"
    target = parts._replace(netloc=netloc, fragment="").geturl()
    name = f"[{host}]" if ":" in host else host  # an IPv6 literal goes in brackets
    host_header = name if port == 443 else f"{name}:{port}"
    return client.build_request(
        "GET",
        target,
        headers={"Host": host_header, "Accept": "image/jpeg, image/png"},
        extensions={"sni_hostname": host},
    )


def _sniff(data: bytes) -> str | None:
    for mimetype, magic in _MAGIC.items():
        if data.startswith(magic):
            return mimetype
    return None


async def _send_pinned(client: httpx.AsyncClient, url: str, host: str, port: int) -> httpx.Response:
    """Send to each checked address in turn until a connection opens."""
    addresses = await _resolve_public(host, port)
    for i, ip in enumerate(addresses):
        request = _pinned_request(client, url, host, port, ip)
        try:
            return await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if i == len(addresses) - 1:
                raise
            logger.info(f"Image fetch: {host} unreachable at {ip}, trying the next address")
    raise ImageFetchError("host could not be resolved")  # pragma: no cover


async def _fetch(
    url: str, max_bytes: int, max_redirects: int, transport: httpx.AsyncBaseTransport | None
) -> FetchedImage:
    # A fresh client per fetch: no pooled connection opened for another host
    # can be reused, and trust_env=False keeps proxy env vars from routing
    # around the pinned address.
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10.0),
    ) as client:
        current = url
        for _hop in range(max_redirects + 1):
            host, port = _validate_url(current)
            response = await _send_pinned(client, current, host, port)
            try:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise ImageFetchError("redirect without a location")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise ImageFetchError(f"server answered {response.status_code}")

                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > max_bytes:
                            raise ImageFetchError("image is too large")
                    except ValueError as e:
                        raise ImageFetchError("invalid content length") from e

                content_type = response.headers.get("content-type", "")
                content_type = content_type.split(";", 1)[0].strip().lower()
                content_type = _TYPE_ALIASES.get(content_type, content_type)
                if content_type not in _ALLOWED_TYPES:
                    raise ImageFetchError("URL is not a JPEG or PNG image")

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ImageFetchError("image is too large")
            finally:
                await response.aclose()

            sniffed = _sniff(bytes(body[:8]))
            if sniffed is None or sniffed != content_type:
                raise ImageFetchError("content is not a JPEG or PNG image")
            return FetchedImage(data=bytes(body), mimetype=sniffed)

        raise ImageFetchError("too many redirects")


async def fetch_public_image(
    url: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedImage:
    """Download a JPEG/PNG from a public https URL, or raise `ImageFetchError`.

    `transport` is for tests only (an `httpx.MockTransport`).
    """
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(url, max_bytes, max_redirects, transport)
    except ImageFetchError:
        raise
    except TimeoutError as e:
        raise ImageFetchError("download timed out") from e
    except httpx.HTTPError as e:
        logger.warning(f"Image fetch failed for {url[:80]}: {e}")
        raise ImageFetchError("download failed") from e
    except Exception as e:
        # Callers only expect ImageFetchError (e.g. an odd URL httpx rejects).
        logger.warning(f"Image fetch failed for {url[:80]}", exc_info=True)
        raise ImageFetchError("download failed") from e
