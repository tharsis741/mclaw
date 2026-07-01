# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build outbound Weixin media payload descriptors.

The builders normalize local file paths and media metadata before the channel
runtime sends them through Weixin-specific APIs.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import secrets
from pathlib import Path
from typing import Any, Callable

from mclaw.channels.weixin.media import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    ITEM_VOICE,
    _assert_weixin_cdn_url,
)

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4


def aes_padded_size(size: int) -> int:
    """Return the ciphertext size expected after Weixin AES media padding."""
    return ((size + 1 + 15) // 16) * 16


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt outbound media bytes with the AES mode expected by Weixin CDN upload."""
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("cryptography package is required to encrypt Weixin media") from exc

    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    """Build the Weixin CDN upload URL from an encrypted query token and file key."""
    from urllib.parse import quote

    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def prepare_outbound_media(path: Path, *, force_file_attachment: bool = False) -> tuple[int, Callable[..., dict[str, Any]]]:
    """Select the Weixin media type and payload builder for a local outbound file."""
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if mime.startswith("image/") and not force_file_attachment:
        return MEDIA_IMAGE, lambda **kw: {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "mid_size": kw["ciphertext_size"],
            },
        }
    if mime.startswith("video/") and not force_file_attachment:
        return MEDIA_VIDEO, lambda **kw: {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "video_size": kw["ciphertext_size"],
                "play_length": kw.get("play_length", 0),
                "video_md5": kw.get("rawfilemd5", ""),
            },
        }
    if path.suffix.lower() == ".silk" and not force_file_attachment:
        return MEDIA_VOICE, lambda **kw: {
            "type": ITEM_VOICE,
            "voice_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "encode_type": kw.get("encode_type", 6),
                "bits_per_sample": kw.get("bits_per_sample", 16),
                "sample_rate": kw.get("sample_rate", 24000),
                "playtime": kw.get("playtime", 0),
            },
        }
    return MEDIA_FILE, lambda **kw: {
        "type": ITEM_FILE,
        "file_item": {
            "media": {
                "encrypt_query_param": kw["encrypt_query_param"],
                "aes_key": kw["aes_key_for_api"],
                "encrypt_type": 1,
            },
            "file_name": kw["filename"],
            "len": str(kw["plaintext_size"]),
        },
    }


def build_upload_payload(path: Path, plaintext: bytes) -> dict[str, Any]:
    """Create per-upload encryption metadata for a Weixin media file."""
    return {
        "filekey": secrets.token_hex(16),
        "aes_key": secrets.token_bytes(16),
        "rawsize": len(plaintext),
        "rawfilemd5": hashlib.md5(plaintext).hexdigest(),
    }


def upload_url_from_response(*, cdn_base_url: str, upload_response: dict[str, Any], filekey: str) -> str:
    """Resolve Weixin upload URL response variants and enforce the CDN allowlist."""
    upload_full_url = str(upload_response.get("upload_full_url") or "")
    if upload_full_url:
        _assert_weixin_cdn_url(upload_full_url)
        return upload_full_url
    upload_param = str(upload_response.get("upload_param") or "")
    if upload_param:
        # Some responses provide only the encrypted token, so the client rebuilds the URL.
        url = cdn_upload_url(cdn_base_url, upload_param, filekey)
        _assert_weixin_cdn_url(url)
        return url
    raise RuntimeError(f"getuploadurl returned neither upload_param nor upload_full_url: {upload_response}")


def aes_key_for_api(aes_key: bytes) -> str:
    """Encode a raw AES key in the hex-then-base64 form accepted by Weixin APIs."""
    return base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
