"""Inbound Weixin media download and cache helpers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import uuid
from dataclasses import asdict, dataclass
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
    kind: str
    mime_type: str
    path: str = ""
    filename: str = ""
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def has_media_items(item_list: list[dict[str, Any]]) -> bool:
    return any(item.get("type") in {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO} for item in _iter_media_items(item_list))


def media_dedup_key(sender_id: str, item_list: list[dict[str, Any]]) -> str:
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
    if not attachments:
        return ""

    lines = ["## Weixin Attachments"]
    for idx, attachment in enumerate(attachments, 1):
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
        elif attachment.kind == "voice":
            lines.append(
                "   Audio is cached locally. Use the file path if a later audio/transcription tool is available."
            )
        elif attachment.kind == "video":
            lines.append("   Video is cached locally as a file attachment.")
    return "\n".join(lines)


class WeixinMediaCache:
    def __init__(self, *, config: WeixinConfig, client: Any) -> None:
        self.config = config
        self.client = client
        self.root = _cache_root(config)

    async def collect(self, item_list: list[dict[str, Any]], *, message_id: str = "") -> list[WeixinMediaAttachment]:
        if not self.config.media_cache_enabled:
            return []
        attachments: list[WeixinMediaAttachment] = []
        for index, item in enumerate(_iter_media_items(item_list), 1):
            attachments.append(await self._download_item(item, message_id=message_id, index=index))
        return attachments

    async def _download_item(self, item: dict[str, Any], *, message_id: str, index: int) -> WeixinMediaAttachment:
        kind = _kind_for_type(item.get("type"))
        filename = _filename_for_item(item)
        mime_type = _mime_for_item(item, filename)
        media = _media_reference_for_item(item)
        if not media:
            return WeixinMediaAttachment(kind=kind, mime_type=mime_type, filename=filename, error="missing media reference")

        try:
            url = _download_url(
                cdn_base_url=self.config.media_cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param") or media.get("encrypted_query_param"),
                full_url=media.get("full_url") or media.get("url"),
            )
            raw = await self.client.download_bytes(url, timeout_ms=int(self.config.media_download_timeout_seconds * 1000))
            if len(raw) > self.config.media_max_bytes:
                raise RuntimeError(
                    f"media exceeds max size ({len(raw)} > {self.config.media_max_bytes} bytes)"
                )
            aes_key = _aes_key_for_item(item, media)
            if aes_key:
                raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key))
            path = self._write_cache(raw, kind=kind, filename=filename, mime_type=mime_type, message_id=message_id, index=index)
            return WeixinMediaAttachment(
                kind=kind,
                mime_type=mime_type,
                path=str(path),
                filename=filename,
                size_bytes=len(raw),
            )
        except Exception as exc:
            return WeixinMediaAttachment(kind=kind, mime_type=mime_type, filename=filename, error=str(exc))

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
        cache_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(message_id or uuid.uuid4().hex)
        extension = _extension(filename, mime_type, kind)
        path = cache_dir / f"{stem}-{index}-{kind}{extension}"
        path.write_bytes(data)
        return path


def _cache_root(config: WeixinConfig) -> Path:
    if config.media_cache_dir:
        return Path(config.media_cache_dir).expanduser()
    return get_mclaw_home() / "weixin" / "media"


def _iter_media_items(item_list: list[dict[str, Any]]):
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


def _download_url(*, cdn_base_url: str, encrypted_query_param: str | None, full_url: str | None) -> str:
    if encrypted_query_param:
        url = f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"
        _assert_weixin_cdn_url(url)
        return url
    if full_url:
        _assert_weixin_cdn_url(full_url)
        return full_url
    raise RuntimeError("media item had neither encrypt_query_param nor full_url")


def _assert_weixin_cdn_url(url: str) -> None:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme not in {"http", "https"}:
        raise ValueError(f"media URL has disallowed scheme {scheme!r}")
    if host not in WEIXIN_CDN_ALLOWLIST:
        raise ValueError(f"media URL host {host!r} is not in the WeChat CDN allowlist")


def _aes_key_for_item(item: dict[str, Any], media: dict[str, Any]) -> str:
    image_aes_hex = str((item.get("image_item") or {}).get("aeskey") or "")
    if image_aes_hex:
        try:
            return base64.b64encode(bytes.fromhex(image_aes_hex)).decode("ascii")
        except ValueError:
            return image_aes_hex
    return str(media.get("aes_key") or "")


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
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
