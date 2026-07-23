# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Image source validation and download helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
from pathlib import Path
from urllib.parse import urlparse

from mclaw.tools.vision.config import MAX_IMAGE_SIZE_BYTES, resolve_download_timeout

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class VisionOperationCancelled(InterruptedError):
    """Raised when a turn cancels cooperative vision work."""


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VisionOperationCancelled("Vision analysis interrupted by user")


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IP address is safe for remote image fetching."""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_host_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to unique IP addresses for SSRF checks."""
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        pass

    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip not in ips:
            ips.append(ip)
    return ips


def _is_safe_url(url: str) -> bool:
    """Block private/internal addresses and non-HTTP schemes."""
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname or hostname in _LOCAL_HOSTS:
        return False

    try:
        ips = _resolve_host_ips(hostname)
    except OSError:
        return False
    return bool(ips) and all(_ip_is_public(ip) for ip in ips)


def _remove_partial_image(destination: Path) -> None:
    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("Could not remove partial image download: %s", destination)


async def _wait_for_retry(
    seconds: float,
    cancel_event: threading.Event | None,
) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        _raise_if_cancelled(cancel_event)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.05, remaining))


async def _download_image_async(
    image_url: str,
    destination: Path,
    max_retries: int = 3,
    parent_agent=None,
    max_bytes: int = MAX_IMAGE_SIZE_BYTES,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Download an image with cancellable I/O, retries, and a hard size cap.

    Every redirect is checked before HTTPX follows it, and the streamed body
    is capped even when the server omits Content-Length.
    """
    if cancel_event is None:
        from mclaw.tools.interrupt import get_interrupt_event

        cancel_event = get_interrupt_event()
    _raise_if_cancelled(cancel_event)
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    completed = False
    request_timeout = max(0.001, resolve_download_timeout(parent_agent))

    async def validate_request(request) -> None:
        if not await asyncio.to_thread(_is_safe_url, str(request.url)):
            raise ValueError("Blocked: URL targets a private or internal address.")

    try:
        for attempt in range(max_retries):
            _raise_if_cancelled(cancel_event)
            try:
                import httpx

                async with httpx.AsyncClient(
                    timeout=request_timeout,
                    follow_redirects=True,
                    event_hooks={"request": [validate_request]},
                ) as client:
                    async with asyncio.timeout(request_timeout):
                        async with client.stream(
                            "GET",
                            image_url,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0.0.0 Safari/537.36"
                                ),
                                "Accept": "image/*,*/*;q=0.8",
                            },
                        ) as resp:
                            resp.raise_for_status()
                            content_length = resp.headers.get("content-length")
                            if content_length and int(content_length) > max_bytes:
                                raise ValueError(
                                    f"Image too large ({int(content_length)} bytes > {max_bytes} bytes limit)."
                                )

                            total = 0
                            with destination.open("wb") as f:
                                async for chunk in resp.aiter_bytes():
                                    _raise_if_cancelled(cancel_event)
                                    if not chunk:
                                        continue
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise ValueError(
                                            f"Image too large ({total} bytes > {max_bytes} bytes limit)."
                                        )
                                    f.write(chunk)
                _raise_if_cancelled(cancel_event)
                completed = True
                return destination
            except Exception as exc:
                last_error = exc
                _remove_partial_image(destination)
                if isinstance(exc, (VisionOperationCancelled, ValueError)):
                    raise
                _raise_if_cancelled(cancel_event)
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Image download failed (attempt %d/%d): %s. Retrying in %ds...",
                        attempt + 1,
                        max_retries,
                        str(exc)[:60],
                        wait,
                    )
                    await _wait_for_retry(wait, cancel_event)
                else:
                    logger.error(
                        "Image download failed after %d attempts: %s",
                        max_retries,
                        str(exc)[:120],
                        exc_info=True,
                    )
    finally:
        if not completed:
            _remove_partial_image(destination)

    raise last_error or RuntimeError("Image download failed")
