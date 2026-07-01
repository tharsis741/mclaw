# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inbound DingTalk media extraction and caching."""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.constants import get_mclaw_home

DINGTALK_TYPE_MAPPING = {
    "picture": "image",
    "voice": "voice",
    "audio": "audio",
    "video": "video",
    "file": "file",
}

_DOWNLOAD_CODE_KEYS = (
    "downloadCode",
    "pictureDownloadCode",
    "videoDownloadCode",
    "voiceDownloadCode",
    "fileDownloadCode",
    "audioDownloadCode",
    "mediaDownloadCode",
    "download_code",
    "picture_download_code",
    "video_download_code",
    "voice_download_code",
    "file_download_code",
    "audio_download_code",
    "media_download_code",
)
_DOWNLOAD_URL_KEYS = (
    "downloadUrl",
    "download_url",
    "url",
    "mediaUrl",
    "media_url",
)
_FILENAME_KEYS = ("fileName", "file_name", "name", "title")
_NESTED_MEDIA_KEYS = (
    "content",
    "media",
    "mediaContent",
    "media_content",
    "attachment",
    "attachmentContent",
    "attachment_content",
    "imageContent",
    "image_content",
    "pictureContent",
    "picture_content",
    "videoContent",
    "video_content",
    "voiceContent",
    "voice_content",
    "audioContent",
    "audio_content",
    "fileContent",
    "file_content",
)


@dataclass(frozen=True)
class DingTalkMediaRef:
    """Download handle extracted from a DingTalk message payload."""

    kind: str
    download_code: str = ""
    url: str = ""
    filename: str = ""
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True)
class DingTalkMediaAttachment:
    """Local cached media artifact passed to the agent as context."""

    kind: str
    mime_type: str
    path: str = ""
    filename: str = ""
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize attachment metadata for raw message context."""
        return asdict(self)


def extract_text(message: Any) -> str:
    """Extract user-visible text from DingTalk SDK and raw payload shapes."""
    text = getattr(message, "text", None) or ""
    if hasattr(text, "content"):
        content = str(text.content or "").strip()
    elif isinstance(text, dict):
        content = str(text.get("content") or "").strip()
    else:
        content = str(text or "").strip()

    if content:
        return content

    raw_content = _raw_content(message)
    if raw_content:
        raw_rich_text = raw_content.get("richText") or raw_content.get("rich_text")
        if isinstance(raw_rich_text, list):
            raw_parts = _rich_text_parts(raw_rich_text)
            if raw_parts:
                return raw_parts
        recognition = raw_content.get("recognition") or raw_content.get("text") or raw_content.get("content")
        if recognition:
            return str(recognition).strip()

    rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
    if not rich_text:
        return ""
    rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
    if not isinstance(rich_list, list):
        return ""
    return _rich_text_parts(rich_list)


def extract_media_refs(message: Any) -> list[DingTalkMediaRef]:
    """Collect media download references across SDK fields and raw payloads."""
    refs: list[DingTalkMediaRef] = []
    msg_type = str(getattr(message, "message_type", None) or getattr(message, "msgtype", "") or "").strip()
    raw_payload = _raw_payload(message)
    if raw_payload:
        refs.extend(_extract_media_refs_from_mapping(raw_payload, fallback_msg_type=msg_type))
    raw_content = _raw_content(message)
    if raw_content:
        raw_rich_text = raw_content.get("richText") or raw_content.get("rich_text")
        if isinstance(raw_rich_text, list):
            refs.extend(_extract_rich_text_refs(raw_rich_text))
        refs.extend(_extract_media_refs_from_mapping(raw_content, fallback_msg_type=msg_type))

    image_content = getattr(message, "image_content", None)
    if image_content:
        code = getattr(image_content, "download_code", None) or getattr(image_content, "downloadCode", None)
        if code:
            refs.append(DingTalkMediaRef(kind="image", download_code=str(code), mime_type="image/jpeg"))

    for attr_name, kind in (
        ("video_content", "video"),
        ("videoContent", "video"),
        ("voice_content", "voice"),
        ("voiceContent", "voice"),
        ("audio_content", "audio"),
        ("audioContent", "audio"),
        ("file_content", "file"),
        ("fileContent", "file"),
    ):
        refs.extend(_extract_media_refs_from_object(getattr(message, attr_name, None), kind=kind))

    rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
    rich_list = getattr(rich_text, "rich_text_list", None) if rich_text else None
    if rich_list is None and isinstance(rich_text, list):
        rich_list = rich_text
    if isinstance(rich_list, list):
        refs.extend(_extract_rich_text_refs(rich_list))
    return _dedupe_media_refs(refs)


def _dedupe_media_refs(refs: list[DingTalkMediaRef]) -> list[DingTalkMediaRef]:
    deduped: list[DingTalkMediaRef] = []
    seen: set[tuple[str, str, str, str]] = set()
    for ref in refs:
        key = (ref.kind, ref.download_code, ref.url, ref.filename)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _rich_text_parts(rich_list: list[Any]) -> str:
    parts: list[str] = []
    for item in rich_list:
        if isinstance(item, dict):
            value = item.get("text") or item.get("content") or ""
        else:
            value = getattr(item, "text", "") or getattr(item, "content", "")
        if value:
            parts.append(str(value))
    return " ".join(parts).strip()


def _extract_rich_text_refs(rich_list: list[Any]) -> list[DingTalkMediaRef]:
    refs: list[DingTalkMediaRef] = []
    for item in rich_list:
        data = item if isinstance(item, dict) else getattr(item, "__dict__", {})
        item_type = str(data.get("type") or data.get("msgtype") or "").strip()
        refs.extend(_extract_media_refs_from_mapping(data, fallback_msg_type=item_type))
    return refs


def _extract_media_refs_from_object(value: Any, *, kind: str) -> list[DingTalkMediaRef]:
    if value is None:
        return []
    if isinstance(value, dict):
        return _extract_media_refs_from_mapping(value, fallback_msg_type=kind)
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return _extract_media_refs_from_mapping(data, fallback_msg_type=kind)
    return []


def _extract_media_refs_from_mapping(data: dict[str, Any], *, fallback_msg_type: str) -> list[DingTalkMediaRef]:
    refs: list[DingTalkMediaRef] = []
    for candidate, path_hint in _iter_media_candidates(data):
        code = _first_mapping_value(candidate, _DOWNLOAD_CODE_KEYS)
        url = _first_mapping_value(candidate, _DOWNLOAD_URL_KEYS)
        if not code and not url:
            continue
        candidate_type = str(candidate.get("type") or candidate.get("msgtype") or candidate.get("messageType") or "").strip()
        kind = _media_kind(candidate_type=candidate_type, fallback_msg_type=fallback_msg_type, path_hint=path_hint)
        filename = str(_first_mapping_value(candidate, _FILENAME_KEYS) or "")
        refs.append(
            DingTalkMediaRef(
                kind=kind,
                download_code=str(code),
                url=str(url),
                filename=filename,
                mime_type=_mime_for(kind, filename),
            )
        )
    return refs


def _iter_media_candidates(data: dict[str, Any], *, _path: tuple[str, ...] = (), _depth: int = 0) -> list[tuple[dict[str, Any], tuple[str, ...]]]:
    """Walk nested DingTalk payload fragments that may contain media handles."""
    candidates: list[tuple[dict[str, Any], tuple[str, ...]]] = [(data, _path)]
    if _depth >= 4:
        return candidates
    for key, value in data.items():
        if isinstance(value, dict):
            if key in _NESTED_MEDIA_KEYS or _looks_media_mapping(value):
                candidates.extend(_iter_media_candidates(value, _path=(*_path, key), _depth=_depth + 1))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict) and _looks_media_mapping(item):
                    candidates.extend(_iter_media_candidates(item, _path=(*_path, key, str(idx)), _depth=_depth + 1))
    return candidates


def _looks_media_mapping(data: dict[str, Any]) -> bool:
    if _first_mapping_value(data, _DOWNLOAD_CODE_KEYS) or _first_mapping_value(data, _DOWNLOAD_URL_KEYS):
        return True
    raw_type = str(data.get("type") or data.get("msgtype") or data.get("messageType") or "").strip()
    return raw_type in DINGTALK_TYPE_MAPPING


def _media_kind(*, candidate_type: str, fallback_msg_type: str, path_hint: tuple[str, ...]) -> str:
    mapped = DINGTALK_TYPE_MAPPING.get(candidate_type or fallback_msg_type)
    if mapped:
        return mapped
    path_text = ".".join(path_hint).lower()
    for marker, kind in (
        ("picture", "image"),
        ("image", "image"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("video", "video"),
        ("file", "file"),
        ("attachment", "file"),
    ):
        if marker in path_text:
            return kind
    return "file"


def _first_mapping_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return ""


def _raw_content(message: Any) -> dict[str, Any]:
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if content is not None:
        data = getattr(content, "__dict__", None)
        if isinstance(data, dict):
            return data
    return {}


def _raw_payload(message: Any) -> dict[str, Any]:
    payload = getattr(message, "_raw_payload", None)
    return payload if isinstance(payload, dict) else {}


def format_media_for_agent(attachments: list[DingTalkMediaAttachment]) -> str:
    """Render cached attachment paths as tool-ready guidance for the agent."""
    if not attachments:
        return ""
    lines = ["## DingTalk Attachments"]
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
            lines.append(f"   Use read_file(path={attachment.path!r}) or other file tools if readable.")
        elif attachment.kind in {"voice", "audio"}:
            lines.append("   Audio is cached locally. Use the file path if a transcription tool is available.")
        elif attachment.kind == "video":
            lines.append("   Video is cached locally as a file attachment.")
    return "\n".join(lines)


class DingTalkMediaCache:
    """Download and cache DingTalk inbound media within configured safety limits."""

    def __init__(self, *, config: DingTalkConfig, client: Any) -> None:
        self.config = config
        self.client = client
        self.root = _cache_root(config)

    async def collect(self, message: Any, *, message_id: str = "") -> list[DingTalkMediaAttachment]:
        """Download every media reference found on a message, preserving failures."""
        if not self.config.media_cache_enabled:
            return []
        attachments: list[DingTalkMediaAttachment] = []
        for index, ref in enumerate(extract_media_refs(message), 1):
            attachments.append(await self._download_ref(ref, message=message, message_id=message_id, index=index))
        return attachments

    async def _download_ref(
        self,
        ref: DingTalkMediaRef,
        *,
        message: Any,
        message_id: str,
        index: int,
    ) -> DingTalkMediaAttachment:
        """Resolve a download code or URL and persist the bounded media payload."""
        try:
            url = ref.url
            if not url and ref.download_code:
                url = await self.client.fetch_download_url(
                    download_code=ref.download_code,
                    robot_code=getattr(message, "robot_code", "") or self.config.robot_code,
                )
            if not url:
                raise RuntimeError("missing DingTalk download URL")
            _assert_download_url(url)
            raw = await self.client.download_bytes(url, timeout_seconds=self.config.media_download_timeout_seconds)
            if len(raw) > self.config.media_max_bytes:
                raise RuntimeError(f"media exceeds max size ({len(raw)} > {self.config.media_max_bytes} bytes)")
            path = self._write_cache(raw, ref=ref, message_id=message_id, index=index)
            return DingTalkMediaAttachment(
                kind=ref.kind,
                mime_type=ref.mime_type,
                path=str(path),
                filename=ref.filename,
                size_bytes=len(raw),
            )
        except Exception as exc:
            return DingTalkMediaAttachment(
                kind=ref.kind,
                mime_type=ref.mime_type,
                filename=ref.filename,
                error=str(exc),
            )

    def _write_cache(self, data: bytes, *, ref: DingTalkMediaRef, message_id: str, index: int) -> Path:
        """Write media bytes under the per-client daily cache directory."""
        day = datetime.now().strftime("%Y%m%d")
        cache_dir = self.root / self.config.client_id / day
        cache_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(message_id or uuid.uuid4().hex)
        extension = _extension(ref.filename, ref.mime_type, ref.kind)
        path = cache_dir / f"{stem}-{index}-{ref.kind}{extension}"
        path.write_bytes(data)
        return path


def _cache_root(config: DingTalkConfig) -> Path:
    if config.media_cache_dir:
        return Path(config.media_cache_dir).expanduser()
    return get_mclaw_home() / "dingtalk" / "media"


def _mime_for(kind: str, filename: str) -> str:
    guessed = mimetypes.guess_type(filename)[0] if filename else None
    if guessed:
        return guessed
    if kind == "image":
        return "image/jpeg"
    if kind in {"voice", "audio"}:
        return "audio/mpeg"
    if kind == "video":
        return "video/mp4"
    return "application/octet-stream"


def _extension(filename: str, mime_type: str, kind: str) -> str:
    suffix = Path(filename).suffix if filename else ""
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed:
        return guessed
    return {
        "image": ".jpg",
        "voice": ".amr",
        "audio": ".mp3",
        "video": ".mp4",
    }.get(kind, ".bin")


def _safe_stem(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return safe[:80] or uuid.uuid4().hex


def _assert_download_url(url: str) -> None:
    """Validate URL shape before handing it to the DingTalk download client."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("DingTalk media download URL must use http or https")
    if not parsed.hostname:
        raise ValueError("DingTalk media download URL is missing a host")
