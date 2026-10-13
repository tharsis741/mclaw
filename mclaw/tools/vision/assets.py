# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Immutable, session-owned image snapshots; histories contain references only."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import tempfile
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.tools.vision.config import MAX_IMAGE_SIZE_BYTES

MAX_IMAGES = 8


def _root(session_id: str) -> Path:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("A session is required for native image input.")
    home = get_mclaw_home().resolve()
    root = home / "vision_assets" / hashlib.sha256(session_id.encode()).hexdigest()
    if root.resolve() != root:
        raise ValueError("Image storage must not redirect to another directory or session.")
    return root


def store_image(path: Path, session_id: str, question: str) -> dict:
    from PIL import Image

    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_SIZE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_SIZE_BYTES:
        raise ValueError("Image exceeds the byte limit or is empty.")
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        mime = Image.MIME.get(image.format)
        image.verify()
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("Image must be normalized to PNG, JPEG, WebP or GIF.")
    identifier = hashlib.sha256(data).hexdigest()
    root = _root(session_id)
    root.mkdir(parents=True, exist_ok=True)
    target = root / identifier
    if target.is_symlink():
        raise ValueError("Image asset cannot be a symbolic link.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {"asset_id": identifier, "mime_type": mime, "width": width, "height": height,
            "bytes": len(data), "question": question}


def image_path(asset: dict, session_id: str) -> Path:
    identifier = asset.get("asset_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("Invalid image asset reference.")
    root = _root(session_id)
    path = root / identifier
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Image asset is outside this session.")
    return path


def image_data_url(asset: dict, session_id: str) -> str:
    path = image_path(asset, session_id)
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_SIZE_BYTES + 1)
    if len(data) > MAX_IMAGE_SIZE_BYTES or hashlib.sha256(data).hexdigest() != asset["asset_id"]:
        raise ValueError("Image asset has changed or exceeds the byte limit.")
    mime = asset.get("mime_type")
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("Unsupported image asset format.")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def remove_session_images(session_id: str) -> None:
    """Remove only the resolved, owned snapshot directory of a deleted session."""
    root = _root(session_id)
    if root.exists():
        shutil.rmtree(root)
