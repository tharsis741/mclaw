# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Download, decrypt, and cache inbound Weixin media attachments.

Weixin event payloads carry several media shapes and CDN URL variants. This
module adapts those protocol details into local files that the agent can safely
reference in prompts and tool calls.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from mclaw.channels.weixin.config import WeixinConfig
from mclaw.constants import get_mclaw_home

ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
_VOICE_MEDIA_MAX_BYTES = 7 * 1024 * 1024

DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
WEIXIN_CDN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


@dataclass(frozen=True)
class WeixinMediaAttachment:
    """Agent-facing description of one cached or failed Weixin media item."""

    kind: str
    mime_type: str
    path: str = ""
    filename: str = ""
    size_bytes: int = 0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def has_media_items(item_list: list[dict[str, Any]]) -> bool:
    """Return whether an item list contains direct or referenced media payloads."""
    return any(item.get("type") in {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO} for item in _iter_media_items(item_list))


def has_voice_items(item_list: list[dict[str, Any]]) -> bool:
    """Return whether an inbound payload contains a native voice message."""
    return any(item.get("type") == ITEM_VOICE for item in _iter_media_items(item_list))


def media_dedup_key(sender_id: str, item_list: list[dict[str, Any]]) -> str:
    """Build a stable dedup key from Weixin media references rather than text content."""
    refs: list[str] = []
    for item in _iter_media_items(item_list):
        item_type = str(item.get("type") or "")
        media = _media_reference_for_item(item)
        ref = (
            media.get("encrypt_query_param")
            or media.get("encrypted_query_param")
            or media.get("full_url")
            or media.get("url")
            or media.get("file_id")
            or ""
        )
        name = _filename_for_item(item)
        refs.append(f"{item_type}:{name}:{ref}")
    digest = hashlib.sha256("|".join(refs).encode("utf-8")).hexdigest()
    return f"media:{sender_id}:{digest}"


def format_media_for_agent(attachments: list[WeixinMediaAttachment]) -> str:
    """Render cached attachments as prompt context with suggested follow-up tools."""
    # Voice attachments are consumed by the shared inbound-audio pipeline. Do
    # not leak their local cache paths into the LLM prompt as media guidance.
    prompt_attachments = [attachment for attachment in attachments if attachment.kind != "voice"]
    if not prompt_attachments:
        return ""

    lines = ["## Weixin Attachments"]
    for idx, attachment in enumerate(prompt_attachments, 1):
        label = f"{idx}. {attachment.kind}"
        if attachment.filename:
            label += f" ({attachment.filename})"
        if attachment.error:
            lines.append(f"{label}: download failed: {attachment.error}")
            continue
        lines.append(
            f"{label}: {attachment.path} "
            f"[{attachment.mime_type}, {attachment.size_bytes} bytes]"
        )
        if attachment.kind == "image":
            lines.append(
                f"   Use vision_analyze(image_url={attachment.path!r}, question=...) "
                "when the user asks about this image or sends image-only content."
            )
        elif attachment.kind == "file":
            lines.append(
                f"   Use read_file(path={attachment.path!r}) or other file tools if the file type is readable."
            )
        elif attachment.kind == "video":
            lines.append("   Video is cached locally as a file attachment.")
    return "\n".join(lines)


class WeixinMediaCache:
    """Resolve Weixin media references into bounded local cache files."""

    def __init__(self, *, config: WeixinConfig, client: Any) -> None:
        self.config = config
        self.client = client
        self.root = _cache_root(config)

    async def collect(self, item_list: list[dict[str, Any]], *, message_id: str = "") -> list[WeixinMediaAttachment]:
        """Download every media-bearing item, returning per-item failures as attachments."""
        if not self.config.media_cache_enabled:
            return []
        attachments: list[WeixinMediaAttachment] = []
        try:
            for index, item in enumerate(_iter_media_items(item_list), 1):
                attachments.append(await self._download_item(item, message_id=message_id, index=index))
            return attachments
        except BaseException:
            _cleanup_cached_attachments(attachments)
            raise

    async def _download_item(self, item: dict[str, Any], *, message_id: str, index: int) -> WeixinMediaAttachment:
        """Fetch, optionally decrypt, size-check, and cache one Weixin media item."""
        kind = _kind_for_type(item.get("type"))
        filename = _filename_for_item(item)
        mime_type = _mime_for_item(item, filename)
        metadata = _metadata_for_item(item)
        media = _media_reference_for_item(item)
        if not media:
            return WeixinMediaAttachment(
                kind=kind,
                mime_type=mime_type,
                filename=filename,
                error="missing media reference",
                metadata=metadata,
            )

        try:
            payload_limit = (
                min(self.config.media_max_bytes, _VOICE_MEDIA_MAX_BYTES)
                if kind == "voice"
                else self.config.media_max_bytes
            )
            url = _download_url(
                cdn_base_url=self.config.media_cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param") or media.get("encrypted_query_param"),
                full_url=media.get("full_url") or media.get("url"),
            )
            raw = await self.client.download_bytes(
                url,
                timeout_ms=int(self.config.media_download_timeout_seconds * 1000),
                # AES padding can add one block to the encrypted form. The
                # plaintext limit is enforced immediately after decryption.
                max_bytes=payload_limit + 16,
            )
            aes_key = _aes_key_for_item(item, media)
            if aes_key:
                # Weixin may provide encrypted CDN bytes; decrypt after the size guard.
                raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key))
            if len(raw) > payload_limit:
                raise RuntimeError(
                    f"media exceeds max size ({len(raw)} > {payload_limit} bytes)"
                )
            path = self._write_cache(raw, kind=kind, filename=filename, mime_type=mime_type, message_id=message_id, index=index)
            return WeixinMediaAttachment(
                kind=kind,
                mime_type=mime_type,
                path=str(path),
                filename=filename,
                size_bytes=len(raw),
                metadata=metadata,
            )
        except Exception as exc:
            return WeixinMediaAttachment(
                kind=kind,
                mime_type=mime_type,
                filename=filename,
                error=str(exc),
                metadata=metadata,
            )

    def _write_cache(
        self,
        data: bytes,
        *,
        kind: str,
        filename: str,
        mime_type: str,
        message_id: str,
        index: int,
    ) -> Path:
        day = datetime.now().strftime("%Y%m%d")
        cache_dir = self.root / self.config.account_id / day
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(cache_dir, 0o700)
        stem = _safe_stem(message_id or uuid.uuid4().hex)
        extension = _extension(filename, mime_type, kind)
        path = cache_dir / f"{stem}-{index}-{kind}{extension}"
        return _write_private_bytes(path, data)


def _cache_root(config: WeixinConfig) -> Path:
    if config.media_cache_dir:
        return Path(config.media_cache_dir).expanduser()
    return get_mclaw_home() / "weixin" / "media"


def _iter_media_items(item_list: list[dict[str, Any]]):
    """Yield direct media items and media nested inside quoted/reference messages."""
    for item in item_list:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO}:
            yield item
        ref_item = ((item.get("ref_msg") or {}).get("message_item") or {})
        if isinstance(ref_item, dict) and ref_item.get("type") in {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO}:
            yield ref_item


def _kind_for_type(item_type: Any) -> str:
    if item_type == ITEM_IMAGE:
        return "image"
    if item_type == ITEM_VOICE:
        return "voice"
    if item_type == ITEM_VIDEO:
        return "video"
    return "file"


def _media_reference_for_item(item: dict[str, Any]) -> dict[str, Any]:
    item_type = item.get("type")
    if item_type == ITEM_IMAGE:
        return (item.get("image_item") or {}).get("media") or {}
    if item_type == ITEM_VOICE:
        return (item.get("voice_item") or {}).get("media") or {}
    if item_type == ITEM_VIDEO:
        return (item.get("video_item") or {}).get("media") or {}
    if item_type == ITEM_FILE:
        return (item.get("file_item") or {}).get("media") or {}
    return {}


def _filename_for_item(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == ITEM_FILE:
        return str((item.get("file_item") or {}).get("file_name") or "document.bin")
    if item_type == ITEM_VIDEO:
        return "video.mp4"
    if item_type == ITEM_VOICE:
        return "voice.silk"
    return "image.jpg"


def _mime_for_item(item: dict[str, Any], filename: str) -> str:
    item_type = item.get("type")
    if item_type == ITEM_IMAGE:
        return "image/jpeg"
    if item_type == ITEM_VOICE:
        return "audio/silk"
    if item_type == ITEM_VIDEO:
        return "video/mp4"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _metadata_for_item(item: dict[str, Any]) -> dict[str, Any]:
    """Preserve protocol metadata needed by downstream attachment processors."""
    item_type = item.get("type")
    metadata: dict[str, Any] = {
        "channel": "weixin",
        "item_type": item_type,
    }
    if item_type == ITEM_VOICE:
        voice = item.get("voice_item") or {}
        for key in ("encode_type", "sample_rate", "playtime", "bits_per_sample"):
            value = voice.get(key)
            if value is not None:
                metadata[key] = value
    return metadata


def _download_url(*, cdn_base_url: str, encrypted_query_param: str | None, full_url: str | None) -> str:
    """Resolve Weixin's encrypted-query or full-URL media reference into a safe CDN URL."""
    if encrypted_query_param:
        url = f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"
        _assert_weixin_cdn_url(url)
        return url
    if full_url:
        _assert_weixin_cdn_url(full_url)
        return full_url
    raise RuntimeError("media item had neither encrypt_query_param nor full_url")


def _assert_weixin_cdn_url(url: str) -> None:
    """Reject media URLs outside the known Weixin CDN surface."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme != "https":
        raise ValueError("Weixin media download URL must use https")
    if host not in WEIXIN_CDN_ALLOWLIST:
        raise ValueError(f"media URL host {host!r} is not in the WeChat CDN allowlist")


def _aes_key_for_item(item: dict[str, Any], media: dict[str, Any]) -> str:
    """Normalize image-specific hex keys and media-level keys to the API form."""
    image_aes_hex = str((item.get("image_item") or {}).get("aeskey") or "")
    if image_aes_hex:
        try:
            return base64.b64encode(bytes.fromhex(image_aes_hex)).decode("ascii")
        except ValueError:
            return image_aes_hex
    return str(media.get("aes_key") or "")


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """Decode Weixin AES key variants into the 16-byte key used for media decryption."""
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt Weixin CDN media bytes and remove PKCS-style padding when present."""
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("cryptography package is required to decrypt Weixin media") from exc

    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return stem[:64] or uuid.uuid4().hex


def _write_private_bytes(path: Path, data: bytes) -> Path:
    """Create a cache artifact atomically with owner-only POSIX permissions."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    candidate = path
    for _attempt in range(3):
        try:
            descriptor = os.open(candidate, flags, 0o600)
            break
        except FileExistsError:
            candidate = path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}")
    else:
        raise FileExistsError(f"could not allocate private cache path for {path.name}")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        if os.name == "posix":
            os.chmod(candidate, 0o600)
        return candidate
    except BaseException:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _cleanup_cached_attachments(attachments: list[WeixinMediaAttachment]) -> None:
    for attachment in attachments:
        if not attachment.path:
            continue
        try:
            Path(attachment.path).unlink(missing_ok=True)
        except OSError:
            pass


def _extension(filename: str, mime_type: str, kind: str) -> str:
    suffix = Path(filename).suffix
    if suffix:
        return suffix[:16]
    guessed = mimetypes.guess_extension(mime_type) if mime_type else None
    if guessed:
        return guessed
    if kind == "voice":
        return ".silk"
    if kind == "video":
        return ".mp4"
    if kind == "image":
        return ".jpg"
    return ".bin"
