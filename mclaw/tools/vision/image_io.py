"""Image source validation and download helpers."""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from mclaw.tools.vision.config import MAX_IMAGE_SIZE_BYTES, resolve_download_timeout

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_host_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
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


def _download_image_sync(
    image_url: str,
    destination: Path,
    max_retries: int = 3,
    parent_agent=None,
    max_bytes: int = MAX_IMAGE_SIZE_BYTES,
) -> Path:
    """Download an image synchronously with retry logic and a hard size cap."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            import httpx

            with httpx.Client(
                timeout=resolve_download_timeout(parent_agent),
                follow_redirects=True,
            ) as client:
                with client.stream(
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
                    if not _is_safe_url(str(resp.url)):
                        raise ValueError("Blocked: redirected URL targets a private or internal address.")
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise ValueError(f"Image too large ({int(content_length)} bytes > {max_bytes} bytes limit).")

                    total = 0
                    with destination.open("wb") as f:
                        for chunk in resp.iter_bytes():
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(f"Image too large ({total} bytes > {max_bytes} bytes limit).")
                            f.write(chunk)
            return destination
        except Exception as exc:
            last_error = exc
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                logger.debug("Could not remove partial image download: %s", destination)
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "Image download failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1,
                    max_retries,
                    str(exc)[:60],
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Image download failed after %d attempts: %s",
                    max_retries,
                    str(exc)[:120],
                    exc_info=True,
                )

    raise last_error or RuntimeError("Image download failed")

