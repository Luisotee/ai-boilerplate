"""Tests for the SSRF-hardened image download (ai_api/safe_fetch.py).

DNS is faked by patching the event loop's getaddrinfo; HTTP by
httpx.MockTransport, which records every request so the tests can check the
connection went to the pinned address.
"""

import asyncio
import socket
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

from ai_api.safe_fetch import ImageFetchError, fetch_public_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100

PUBLIC_IP = "93.184.216.34"


def fake_dns(table: dict[str, list[str]]):
    """Patch loop.getaddrinfo to answer from `table` (host -> addresses)."""

    async def getaddrinfo(self, host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("not found")
        out = []
        for addr in table[host]:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            sockaddr = (addr, port, 0, 0) if family == socket.AF_INET6 else (addr, port)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    return patch.object(asyncio.base_events.BaseEventLoop, "getaddrinfo", getaddrinfo)


def recording_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), seen


def image_response(body: bytes = PNG, content_type: str = "image/png", **headers):
    return httpx.Response(200, headers={"content-type": content_type, **headers}, content=body)


def never_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no request expected, got {request.url}")


async def fetch(url: str, transport, dns: dict[str, list[str]] | None = None, **kwargs):
    with fake_dns(dns or {}):
        return await fetch_public_image(url, transport=transport, **kwargs)


# ---------------------------------------------------------------------------
# Success and pinning
# ---------------------------------------------------------------------------


async def test_success_pins_the_checked_address():
    transport, seen = recording_transport(lambda r: image_response())
    result = await fetch(
        "https://images.example.com/a/map.png?x=1", transport, {"images.example.com": [PUBLIC_IP]}
    )

    assert result.data == PNG
    assert result.mimetype == "image/png"
    (request,) = seen
    assert request.url.host == PUBLIC_IP
    assert request.url.path == "/a/map.png"
    assert request.url.query == b"x=1"
    assert request.headers["host"] == "images.example.com"
    assert request.extensions["sni_hostname"] == "images.example.com"


async def test_non_default_port_kept_in_url_and_host_header():
    transport, seen = recording_transport(lambda r: image_response(JPEG, "image/jpeg"))
    result = await fetch(
        "https://img.example.com:8443/p.jpg", transport, {"img.example.com": [PUBLIC_IP]}
    )
    assert result.mimetype == "image/jpeg"
    assert seen[0].url.port == 8443
    assert seen[0].headers["host"] == "img.example.com:8443"


async def test_public_ipv6_is_bracketed():
    transport, seen = recording_transport(lambda r: image_response())
    await fetch("https://v6.example.com/p.png", transport, {"v6.example.com": ["2606:4700::1111"]})
    assert seen[0].url.host == "2606:4700::1111"


# ---------------------------------------------------------------------------
# URL checks (no DNS, no request)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://example.com/a.png", "only https"),
        ("ftp://example.com/a.png", "only https"),
        ("file:///etc/passwd", "only https"),
        ("https://user:pw@example.com/a.png", "credentials"),
        ("https:///a.png", "no host"),
        ("https://localhost/a.png", "not allowed"),
        ("https://foo.localhost/a.png", "not allowed"),
        ("https://redis/a.png", "not allowed"),  # Docker service name
        ("https://postgres:5432/a.png", "not allowed"),
        ("https://ai-api:8000/a.png", "not allowed"),
    ],
)
async def test_url_rejected_before_any_request(url, reason):
    transport, _ = recording_transport(never_called)
    with pytest.raises(ImageFetchError, match=reason):
        await fetch(url, transport)


# ---------------------------------------------------------------------------
# Address checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC 1918
        "172.18.0.2",  # Docker bridge
        "192.168.1.10",
        "169.254.169.254",  # link-local / cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "::1",  # IPv6 loopback
        "fd00::1",  # ULA
        "fe80::1",  # IPv6 link-local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1 (Python calls it global)
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "::127.0.0.1",  # IPv4-compatible loopback (Python calls it global)
        "::10.0.0.1",  # IPv4-compatible private
        "::169.254.169.254",  # IPv4-compatible metadata
        "::ffff:0:127.0.0.1",  # SIIT of 127.0.0.1 (Python calls it global)
        "::ffff:0:169.254.169.254",  # SIIT of the metadata address
        "::",
    ],
)
async def test_non_public_address_blocked(address):
    transport, _ = recording_transport(never_called)
    with pytest.raises(ImageFetchError, match="not allowed"):
        await fetch("https://evil.example.com/a.png", transport, {"evil.example.com": [address]})


@pytest.mark.parametrize(
    "literal",
    [
        "127.0.0.1",
        "[::1]",
        "169.254.169.254",
        "[::ffff:10.0.0.1]",
        "[::127.0.0.1]",
        "[::ffff:0:169.254.169.254]",
    ],
)
async def test_ip_literal_host_blocked(literal):
    transport, _ = recording_transport(never_called)
    with pytest.raises(ImageFetchError, match="not allowed"):
        await fetch(f"https://{literal}/a.png", transport)


async def test_public_ip_literal_allowed():
    transport, seen = recording_transport(lambda r: image_response())
    await fetch(f"https://{PUBLIC_IP}/a.png", transport)
    assert seen[0].url.host == PUBLIC_IP


@pytest.mark.parametrize("address", ["::93.184.216.34", "::ffff:0:93.184.216.34"])
async def test_embedded_public_ipv4_allowed(address):
    transport, seen = recording_transport(lambda r: image_response())
    await fetch("https://e.example.com/a.png", transport, {"e.example.com": [address]})
    assert len(seen) == 1


async def test_ipv4_tried_first_then_next_address_on_connect_failure():
    def handler(request):
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("network is unreachable")
        return image_response()

    transport, seen = recording_transport(handler)
    result = await fetch(
        "https://dual.example.com/a.png",
        transport,
        {"dual.example.com": ["2606:4700::1111", "93.184.216.34", "93.184.216.34"]},
    )
    assert result.data == PNG
    assert [r.url.host for r in seen] == ["93.184.216.34", "2606:4700::1111"]
    assert all(r.headers["host"] == "dual.example.com" for r in seen)
    assert all(r.extensions["sni_hostname"] == "dual.example.com" for r in seen)


async def test_every_address_unreachable_is_a_generic_failure():
    def handler(request):
        raise httpx.ConnectError("network is unreachable")

    transport, seen = recording_transport(handler)
    with pytest.raises(ImageFetchError, match="download failed"):
        await fetch(
            "https://dual.example.com/a.png",
            transport,
            {"dual.example.com": ["2606:4700::1111", PUBLIC_IP]},
        )
    assert len(seen) == 2


async def test_idn_host_sent_as_punycode():
    transport, seen = recording_transport(lambda r: image_response())
    await fetch("https://münchen.example/a.png", transport, {"xn--mnchen-3ya.example": [PUBLIC_IP]})
    assert seen[0].headers["host"] == "xn--mnchen-3ya.example"
    assert seen[0].extensions["sni_hostname"] == "xn--mnchen-3ya.example"


async def test_unexpected_error_maps_to_image_fetch_error():
    def handler(request):
        raise ValueError("odd")

    transport, _ = recording_transport(handler)
    with pytest.raises(ImageFetchError, match="download failed"):
        await fetch("https://a.example.com/a.png", transport, DNS)


async def test_any_private_answer_blocks_even_with_a_public_one():
    transport, _ = recording_transport(never_called)
    with pytest.raises(ImageFetchError, match="not allowed"):
        await fetch(
            "https://mixed.example.com/a.png",
            transport,
            {"mixed.example.com": [PUBLIC_IP, "10.0.0.1"]},
        )


async def test_unresolvable_host():
    transport, _ = recording_transport(never_called)
    with pytest.raises(ImageFetchError, match="could not be resolved"):
        await fetch("https://nope.example.com/a.png", transport, {})


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------

DNS = {
    "a.example.com": [PUBLIC_IP],
    "b.example.com": ["93.184.216.35"],
    "internal.example.com": ["10.1.2.3"],
}


async def test_redirect_followed_and_revalidated():
    def handler(request):
        if request.headers["host"] == "a.example.com":
            return httpx.Response(302, headers={"location": "https://b.example.com/img.png"})
        return image_response()

    transport, seen = recording_transport(handler)
    result = await fetch("https://a.example.com/start", transport, DNS)
    assert result.data == PNG
    assert [r.headers["host"] for r in seen] == ["a.example.com", "b.example.com"]
    assert seen[1].url.host == "93.184.216.35"


async def test_relative_redirect_resolved_against_hostname_not_ip():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/real.png"})
        return image_response()

    transport, seen = recording_transport(handler)
    await fetch("https://a.example.com/start", transport, DNS)
    assert seen[1].headers["host"] == "a.example.com"
    assert seen[1].url.path == "/real.png"


async def test_redirect_to_http_blocked():
    transport, seen = recording_transport(
        lambda r: httpx.Response(302, headers={"location": "http://b.example.com/a.png"})
    )
    with pytest.raises(ImageFetchError, match="only https"):
        await fetch("https://a.example.com/a.png", transport, DNS)
    assert len(seen) == 1


async def test_redirect_to_private_host_blocked():
    transport, seen = recording_transport(
        lambda r: httpx.Response(307, headers={"location": "https://internal.example.com/a.png"})
    )
    with pytest.raises(ImageFetchError, match="not allowed"):
        await fetch("https://a.example.com/a.png", transport, DNS)
    assert len(seen) == 1


async def test_redirect_to_docker_service_blocked():
    transport, _ = recording_transport(
        lambda r: httpx.Response(302, headers={"location": "https://redis:6379/"})
    )
    with pytest.raises(ImageFetchError, match="not allowed"):
        await fetch("https://a.example.com/a.png", transport, DNS)


async def test_too_many_redirects():
    transport, seen = recording_transport(
        lambda r: httpx.Response(302, headers={"location": "https://a.example.com/loop"})
    )
    with pytest.raises(ImageFetchError, match="too many redirects"):
        await fetch("https://a.example.com/a.png", transport, DNS, max_redirects=2)
    assert len(seen) == 3


# ---------------------------------------------------------------------------
# Response checks
# ---------------------------------------------------------------------------


async def test_non_200_rejected():
    transport, _ = recording_transport(lambda r: httpx.Response(404))
    with pytest.raises(ImageFetchError, match="404"):
        await fetch("https://a.example.com/a.png", transport, DNS)


async def test_declared_length_over_limit_rejected():
    transport, _ = recording_transport(
        lambda r: image_response(PNG, "image/png", **{"content-length": str(10_000_000)})
    )
    with pytest.raises(ImageFetchError, match="too large"):
        await fetch("https://a.example.com/a.png", transport, DNS)


async def test_streamed_body_over_limit_rejected_without_length():
    class Endless(httpx.AsyncByteStream):
        def __init__(self):
            self.sent = 0

        async def __aiter__(self):
            yield PNG
            while True:
                self.sent += 1024
                yield b"\x00" * 1024

    stream = Endless()
    transport, _ = recording_transport(
        lambda r: httpx.Response(200, headers={"content-type": "image/png"}, stream=stream)
    )
    with pytest.raises(ImageFetchError, match="too large"):
        await fetch("https://a.example.com/a.png", transport, DNS, max_bytes=10_000)
    assert stream.sent < 20_000  # aborted while streaming, not after


@pytest.mark.parametrize("content_type", ["text/html", "image/gif", "image/svg+xml", ""])
async def test_wrong_content_type_rejected(content_type):
    transport, _ = recording_transport(lambda r: image_response(PNG, content_type))
    with pytest.raises(ImageFetchError, match="not a JPEG or PNG"):
        await fetch("https://a.example.com/a.png", transport, DNS)


@pytest.mark.parametrize("content_type", ["image/jpg", "image/pjpeg", "IMAGE/JPG"])
async def test_jpeg_type_aliases_accepted(content_type):
    transport, _ = recording_transport(lambda r: image_response(JPEG, content_type))
    result = await fetch("https://a.example.com/a.jpg", transport, DNS)
    assert result.mimetype == "image/jpeg"


async def test_content_type_parameters_ignored():
    transport, _ = recording_transport(lambda r: image_response(PNG, "image/png; charset=binary"))
    result = await fetch("https://a.example.com/a.png", transport, DNS)
    assert result.mimetype == "image/png"


@pytest.mark.parametrize(
    "body,content_type",
    [(b"<html>not an image</html>", "image/png"), (PNG, "image/jpeg"), (JPEG, "image/png")],
)
async def test_magic_bytes_must_match(body, content_type):
    transport, _ = recording_transport(lambda r: image_response(body, content_type))
    with pytest.raises(ImageFetchError, match="not a JPEG or PNG"):
        await fetch("https://a.example.com/a.png", transport, DNS)


async def test_transport_error_is_generic():
    def boom(request):
        raise httpx.ConnectError("connect failed to 93.184.216.34")

    transport, _ = recording_transport(boom)
    with pytest.raises(ImageFetchError) as exc:
        await fetch("https://a.example.com/a.png", transport, DNS)
    assert str(exc.value) == "download failed"


async def test_timeout():
    async def slow(request):
        await asyncio.sleep(5)
        return image_response()

    transport = httpx.MockTransport(slow)
    with pytest.raises(ImageFetchError, match="timed out"):
        await fetch("https://a.example.com/a.png", transport, DNS, timeout=0.05)
