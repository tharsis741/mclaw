# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Security checks for short-lived DingTalk media download URLs."""

from __future__ import annotations

from urllib.parse import urlparse


DINGTALK_MEDIA_HOST_SUFFIXES = (
    ".dingtalk.com",
    ".dingtalkusercontent.com",
    ".alicdn.com",
    ".aliyuncs.com",
    ".alibabausercontent.com",
)


def normalize_dingtalk_media_url(
    url: str,
    *,
    upgrade_trusted_http: bool = False,
) -> str:
    """Return a trusted HTTPS media URL without ever making an HTTP request.

    DingTalk's documented download response still contains HTTP examples.  For
    an initial API-provided URL we can safely rewrite the scheme to HTTPS, but
    redirects are never allowed to downgrade an established HTTPS request.
    """

    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme.casefold()
    if scheme == "http" and upgrade_trusted_http:
        parsed = parsed._replace(scheme="https")
        scheme = "https"
    if scheme != "https":
        raise ValueError("DingTalk media download URL must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("DingTalk media download URL must not contain user info")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DingTalk media download URL has an invalid port") from exc
    if port not in {None, 443}:
        raise ValueError("DingTalk media download URL must use the default https port")
    hostname = str(parsed.hostname or "").rstrip(".").casefold()
    if not hostname:
        raise ValueError("DingTalk media download URL is missing a host")
    if not any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in DINGTALK_MEDIA_HOST_SUFFIXES
    ):
        raise ValueError("DingTalk media download host is not trusted")
    return parsed.geturl()


def dingtalk_media_url_origin(url: str) -> tuple[str, str]:
    """Return only non-sensitive URL origin fields for diagnostic logging."""

    parsed = urlparse(str(url or ""))
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        scheme = "unknown"
    hostname = str(parsed.hostname or "").rstrip(".").casefold()
    hostname = "".join(
        character if character.isalnum() or character in {"-", "."} else "-"
        for character in hostname
    )[:253]
    return scheme, hostname
