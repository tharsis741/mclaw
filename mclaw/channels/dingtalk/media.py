# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inbound DingTalk media extraction and caching."""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.dingtalk.download_security import (
    dingtalk_media_url_origin,
    normalize_dingtalk_media_url,
)
from mclaw.constants import get_mclaw_home

logger = logging.getLogger(__name__)

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
    "mediaUrl",
    "media_url",
)
_VOICE_MEDIA_MAX_BYTES = 7 * 1024 * 1024
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
    codec: str = ""
    path: str = ""
    filename: str = ""
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize attachment metadata for raw message context."""
        return asdict(self)


@dataclass(frozen=True)
class DetectedMediaType:
    """Media metadata derived from payload bytes instead of platform guesses."""

    mime_type: str
    extension: str
    codec: str = ""


def extract_text(message: Any) -> str:
    """Extract only text the user explicitly typed.

    DingTalk includes its own ASR result in ``content.recognition`` for voice
    messages.  That value is platform-generated metadata and deliberately does
    not enter the user text path; M-Claw transcribes the downloaded audio
    independently.
    """
    raw_payload = _raw_payload(message)
    message_type = str(
        getattr(message, "message_type", None)
        or getattr(message, "msgtype", "")
        or raw_payload.get("msgtype")
        or raw_payload.get("messageType")
        or ""
    ).strip().lower()
    text = "" if message_type in {"audio", "voice"} else (getattr(message, "text", None) or "")
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
        if message_type == "text":
            raw_text = raw_content.get("text") or raw_content.get("content")
            if isinstance(raw_text, str) and raw_text.strip():
                return raw_text.strip()

    rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
    if not rich_text:
        return ""
    rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
    if not isinstance(rich_list, list):
        return ""
    return _rich_text_parts(rich_list)


def extract_platform_transcript(message: Any) -> str:
    """Parse DingTalk's ASR hint so its deliberate runtime exclusion is testable."""
    candidates: list[Any] = []
    raw_content = _raw_content(message)
    if raw_content:
        candidates.append(raw_content.get("recognition"))

    raw_payload = _raw_payload(message)
    payload_content = raw_payload.get("content") if raw_payload else None
    if isinstance(payload_content, dict):
        candidates.append(payload_content.get("recognition"))

    for attr_name in ("audio_content", "audioContent", "voice_content", "voiceContent"):
        value = getattr(message, attr_name, None)
        if isinstance(value, dict):
            candidates.append(value.get("recognition"))
        elif value is not None:
            candidates.append(getattr(value, "recognition", None))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def extract_audio_duration_ms(message: Any) -> int:
    """Extract DingTalk's audio duration hint without trusting other content."""
    candidates: list[Any] = []
    raw_content = _raw_content(message)
    if raw_content:
        candidates.append(raw_content.get("duration"))

    raw_payload = _raw_payload(message)
    payload_content = raw_payload.get("content") if raw_payload else None
    if isinstance(payload_content, dict):
        candidates.append(payload_content.get("duration"))

    for attr_name in ("audio_content", "audioContent", "voice_content", "voiceContent"):
        value = getattr(message, attr_name, None)
        if isinstance(value, dict):
            candidates.append(value.get("duration"))
        elif value is not None:
            candidates.append(getattr(value, "duration", None))

    for candidate in candidates:
        if candidate in (None, "") or isinstance(candidate, bool):
            continue
        try:
            return max(0, int(candidate))
        except (TypeError, ValueError):
            continue
    return 0


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
        # A downloadCode is resolved through authenticated DingTalk OpenAPI.
        # Never let a sibling raw URL override that trusted route.
        url = "" if code else _first_mapping_value(candidate, _DOWNLOAD_URL_KEYS)
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
    # Audio is consumed by the shared inbound-audio pipeline.  Keeping its
    # private cache path out of the prompt also prevents a voice-only message
    # from bypassing ASR as generic attachment text.
    prompt_attachments = [
        attachment for attachment in attachments if attachment.kind not in {"voice", "audio"}
    ]
    if not prompt_attachments:
        return ""
    lines = ["## DingTalk Attachments"]
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
            lines.append(f"   Use read_file(path={attachment.path!r}) or other file tools if readable.")
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
        try:
            for index, ref in enumerate(extract_media_refs(message), 1):
                attachments.append(await self._download_ref(ref, message=message, message_id=message_id, index=index))
            return attachments
        except BaseException:
            _cleanup_cached_attachments(attachments)
            raise

    async def _download_ref(
        self,
        ref: DingTalkMediaRef,
        *,
        message: Any,
        message_id: str,
        index: int,
    ) -> DingTalkMediaAttachment:
        """Resolve a download code or URL and persist the bounded media payload."""
        stage = "resolve"
        url = ""
        try:
            if ref.download_code:
                url = await self.client.fetch_download_url(
                    download_code=ref.download_code,
                    robot_code=getattr(message, "robot_code", "") or self.config.robot_code,
                )
            elif ref.url:
                url = ref.url
            if not url:
                raise RuntimeError("missing DingTalk download URL")
            stage = "validate"
            url = normalize_dingtalk_media_url(url, upgrade_trusted_http=True)
            payload_limit = (
                min(self.config.media_max_bytes, _VOICE_MEDIA_MAX_BYTES)
                if ref.kind in {"voice", "audio"}
                else self.config.media_max_bytes
            )
            stage = "download"
            raw = await self.client.download_bytes(
                url,
                timeout_seconds=self.config.media_download_timeout_seconds,
                max_bytes=payload_limit,
            )
            stage = "inspect"
            detected = sniff_media_type(
                raw,
                kind=ref.kind,
                filename=ref.filename,
                fallback_mime=ref.mime_type,
            )
            path = self._write_cache(
                raw,
                ref=ref,
                media_type=detected,
                message_id=message_id,
                index=index,
            )
            return DingTalkMediaAttachment(
                kind=ref.kind,
                mime_type=detected.mime_type,
                codec=detected.codec,
                path=str(path),
                filename=ref.filename,
                size_bytes=len(raw),
            )
        except Exception as exc:
            safe_error = _safe_media_error(exc)
            scheme, hostname = dingtalk_media_url_origin(url)
            logger.warning(
                "dingtalk media download failed kind=%s stage=%s origin=%s://%s error=%s",
                ref.kind,
                stage,
                scheme,
                hostname or "unknown",
                safe_error,
            )
            return DingTalkMediaAttachment(
                kind=ref.kind,
                mime_type=ref.mime_type,
                filename=ref.filename,
                error=f"{stage}: {safe_error}",
            )

    def _write_cache(
        self,
        data: bytes,
        *,
        ref: DingTalkMediaRef,
        media_type: DetectedMediaType,
        message_id: str,
        index: int,
    ) -> Path:
        """Write media bytes under the per-client daily cache directory."""
        day = datetime.now().strftime("%Y%m%d")
        cache_dir = self.root / self.config.client_id / day
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(cache_dir, 0o700)
        stem = _safe_stem(message_id or uuid.uuid4().hex)
        extension = media_type.extension or _extension(ref.filename, media_type.mime_type, ref.kind)
        path = cache_dir / f"{stem}-{index}-{ref.kind}{extension}"
        return _write_private_bytes(path, data)


def _cache_root(config: DingTalkConfig) -> Path:
    if config.media_cache_dir:
        return Path(config.media_cache_dir).expanduser()
    return get_mclaw_home() / "dingtalk" / "media"


def _mime_for(kind: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower() if filename else ""
    explicit = _MEDIA_TYPES_BY_SUFFIX.get(suffix)
    if explicit:
        return explicit.mime_type
    guessed = mimetypes.guess_type(filename)[0] if filename else None
    if guessed:
        return guessed
    if kind == "image":
        return "image/jpeg"
    if kind in {"voice", "audio"}:
        return "application/octet-stream"
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
        "video": ".mp4",
    }.get(kind, ".bin")


_MEDIA_TYPES_BY_SUFFIX = {
    ".aac": DetectedMediaType("audio/aac", ".aac", "aac"),
    ".aif": DetectedMediaType("audio/aiff", ".aif", "aiff"),
    ".aiff": DetectedMediaType("audio/aiff", ".aiff", "aiff"),
    ".amr": DetectedMediaType("audio/amr", ".amr", "amr"),
    ".awb": DetectedMediaType("audio/amr-wb", ".amr", "amr-wb"),
    ".caf": DetectedMediaType("audio/x-caf", ".caf", "caf"),
    ".flac": DetectedMediaType("audio/flac", ".flac", "flac"),
    ".m4a": DetectedMediaType("audio/mp4", ".m4a", "mp4a"),
    ".mp3": DetectedMediaType("audio/mpeg", ".mp3", "mp3"),
    ".ogg": DetectedMediaType("audio/ogg", ".ogg", ""),
    ".opus": DetectedMediaType("audio/ogg", ".ogg", "opus"),
    ".wav": DetectedMediaType("audio/wav", ".wav", "wav"),
    ".webm": DetectedMediaType("audio/webm", ".webm", ""),
    ".bmp": DetectedMediaType("image/bmp", ".bmp"),
    ".gif": DetectedMediaType("image/gif", ".gif"),
    ".jpeg": DetectedMediaType("image/jpeg", ".jpg"),
    ".jpg": DetectedMediaType("image/jpeg", ".jpg"),
    ".png": DetectedMediaType("image/png", ".png"),
    ".webp": DetectedMediaType("image/webp", ".webp"),
    ".mp4": DetectedMediaType("video/mp4", ".mp4", ""),
    ".pdf": DetectedMediaType("application/pdf", ".pdf"),
    ".zip": DetectedMediaType("application/zip", ".zip"),
}

_PREFERRED_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "audio/aac": ".aac",
    "audio/aiff": ".aiff",
    "audio/amr": ".amr",
    "audio/amr-wb": ".amr",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-caf": ".caf",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}


def sniff_media_type(
    data: bytes,
    *,
    kind: str,
    filename: str = "",
    fallback_mime: str = "application/octet-stream",
) -> DetectedMediaType:
    """Identify common DingTalk media from magic bytes with safe fallbacks.

    DingTalk's inbound audio callback provides no codec or filename.  In
    particular, ``msgtype=audio`` does not imply MP3.  Byte signatures take
    precedence over a filename supplied by the platform.
    """
    prefix = bytes(data[:4096])

    if prefix.startswith(b"#!AMR-WB\n"):
        return DetectedMediaType("audio/amr-wb", ".amr", "amr-wb")
    if prefix.startswith(b"#!AMR\n"):
        return DetectedMediaType("audio/amr", ".amr", "amr-nb")
    if prefix.startswith(b"OggS"):
        codec = ""
        if b"OpusHead" in prefix:
            codec = "opus"
        elif b"\x01vorbis" in prefix:
            codec = "vorbis"
        elif b"Speex   " in prefix:
            codec = "speex"
        return DetectedMediaType("audio/ogg", ".ogg", codec)
    if prefix.startswith(b"fLaC"):
        return DetectedMediaType("audio/flac", ".flac", "flac")
    if prefix.startswith(b"ID3") or _looks_like_mp3_frame(prefix):
        return DetectedMediaType("audio/mpeg", ".mp3", "mp3")
    if prefix.startswith(b"RIFF") and len(prefix) >= 12:
        if prefix[8:12] == b"WAVE":
            return DetectedMediaType("audio/wav", ".wav", _wav_codec(prefix))
        if prefix[8:12] == b"WEBP":
            return DetectedMediaType("image/webp", ".webp")
    if prefix.startswith(b"FORM") and prefix[8:12] in {b"AIFF", b"AIFC"}:
        return DetectedMediaType("audio/aiff", ".aiff", "aiff")
    if prefix.startswith(b"caff"):
        return DetectedMediaType("audio/x-caf", ".caf", "caf")
    if kind in {"audio", "voice"} and _looks_like_aac_adts(prefix):
        return DetectedMediaType("audio/aac", ".aac", "aac")
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        if kind in {"audio", "voice"}:
            return DetectedMediaType("audio/mp4", ".m4a", "mp4a")
        return DetectedMediaType("video/mp4", ".mp4", "")
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        mime_type = "audio/webm" if kind in {"audio", "voice"} else "video/webm"
        return DetectedMediaType(mime_type, ".webm", "")
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectedMediaType("image/png", ".png")
    if prefix.startswith(b"\xff\xd8\xff"):
        return DetectedMediaType("image/jpeg", ".jpg")
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return DetectedMediaType("image/gif", ".gif")
    if prefix.startswith(b"BM"):
        return DetectedMediaType("image/bmp", ".bmp")
    if prefix.startswith(b"%PDF-"):
        return DetectedMediaType("application/pdf", ".pdf")
    if prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return DetectedMediaType("application/zip", ".zip")

    suffix = Path(filename).suffix.lower() if filename else ""
    from_suffix = _MEDIA_TYPES_BY_SUFFIX.get(suffix)
    if from_suffix and (kind not in {"audio", "voice"} or from_suffix.mime_type.startswith("audio/")):
        return from_suffix

    clean_mime = str(fallback_mime or "").partition(";")[0].strip().lower()
    if clean_mime and clean_mime != "application/octet-stream":
        if kind not in {"audio", "voice"} or clean_mime.startswith("audio/"):
            extension = _PREFERRED_EXTENSIONS.get(clean_mime) or mimetypes.guess_extension(clean_mime) or ".bin"
            return DetectedMediaType(clean_mime, extension, "")

    return DetectedMediaType("application/octet-stream", ".bin", "")


def _looks_like_mp3_frame(prefix: bytes) -> bool:
    if len(prefix) < 2 or prefix[0] != 0xFF:
        return False
    # MPEG audio sync (11 bits) with a valid layer and bitrate index.
    return (prefix[1] & 0xE0) == 0xE0 and (prefix[1] & 0x06) != 0


def _looks_like_aac_adts(prefix: bytes) -> bool:
    return len(prefix) >= 2 and prefix[0] == 0xFF and (prefix[1] & 0xF6) == 0xF0


def _wav_codec(prefix: bytes) -> str:
    fmt_index = prefix.find(b"fmt ")
    if fmt_index < 0 or len(prefix) < fmt_index + 10:
        return "wav"
    format_tag = int.from_bytes(prefix[fmt_index + 8 : fmt_index + 10], "little")
    bits_per_sample = (
        int.from_bytes(prefix[fmt_index + 22 : fmt_index + 24], "little")
        if len(prefix) >= fmt_index + 24
        else 0
    )
    if format_tag == 1:
        if bits_per_sample == 8:
            return "pcm_u8"
        if bits_per_sample in {16, 24, 32}:
            return f"pcm_s{bits_per_sample}le"
        return "pcm"
    if format_tag == 3:
        return f"pcm_f{bits_per_sample}le" if bits_per_sample in {32, 64} else "pcm_f"
    return {
        6: "pcm_alaw",
        7: "pcm_mulaw",
        17: "adpcm_ima_wav",
    }.get(format_tag, "wav")


def _safe_stem(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return safe[:80] or uuid.uuid4().hex


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


def _cleanup_cached_attachments(attachments: list[DingTalkMediaAttachment]) -> None:
    for attachment in attachments:
        if not attachment.path:
            continue
        try:
            Path(attachment.path).unlink(missing_ok=True)
        except OSError:
            pass


def _assert_download_url(url: str) -> None:
    """Validate URL shape before handing it to the DingTalk download client."""
    normalize_dingtalk_media_url(url)


def _safe_media_error(exc: Exception) -> str:
    """Describe a media failure without retaining signed URLs or handles."""

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"{type(exc).__name__} status={status}"
    message = str(exc)
    safe_messages = (
        "missing DingTalk download URL",
        "DingTalk media download URL must use https",
        "DingTalk media download URL must not contain user info",
        "DingTalk media download URL has an invalid port",
        "DingTalk media download URL must use the default https port",
        "DingTalk media download URL is missing a host",
        "DingTalk media download host is not trusted",
        "DingTalk media redirect is missing Location",
        "DingTalk media redirect loop detected",
        "DingTalk media redirect limit exceeded",
        "media exceeds max size",
    )
    for safe_message in safe_messages:
        if safe_message in message:
            return safe_message
    return type(exc).__name__
