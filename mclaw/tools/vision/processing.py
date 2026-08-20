# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Image detection, resizing, and encoding helpers."""

from __future__ import annotations

import base64
import logging
import math
import tempfile
import uuid
from pathlib import Path

from mclaw.tools.vision.config import DEFAULT_MAX_PIXELS

logger = logging.getLogger(__name__)


def _detect_image_mime_type(image_path: Path) -> str | None:
    """Return a MIME type when the file looks like a supported image."""
    try:
        with image_path.open("rb") as f:
            header = f.read(64)
    except Exception:
        return None

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if image_path.suffix.lower() == ".svg":
        try:
            head = image_path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
            if "<svg" in head:
                return "image/svg+xml"
        except Exception:
            pass
    return None


def _rgb_for_jpeg(img):
    """Convert image modes to JPEG-safe RGB while preserving transparency."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        from PIL import Image

        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return img.convert("RGB")


def _resize_image_if_needed(
    image_path: Path,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    alignment: int = 32,
    jpeg_quality: int = 85,
) -> Path:
    """Downscale an image only when its total pixel count exceeds ``max_pixels``."""
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    if image_path.suffix.lower() == ".svg":
        return image_path

    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.debug("Pillow not installed; skipping image resize")
        return image_path

    try:
        with Image.open(image_path) as img:
            source_format = (img.format or "").upper()
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            source_pixels = width * height
            if source_pixels <= max_pixels:
                return image_path

            scale = math.sqrt(max_pixels / source_pixels)
            scaled_width = max(1, int(width * scale))
            scaled_height = max(1, int(height * scale))
            new_width = (
                scaled_width // alignment * alignment
                if scaled_width >= alignment
                else scaled_width
            )
            new_height = (
                scaled_height // alignment * alignment
                if scaled_height >= alignment
                else scaled_height
            )

            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            temp_dir = Path(tempfile.gettempdir()) / "mclaw-vision"
            temp_dir.mkdir(parents=True, exist_ok=True)
            preserve_png = source_format == "PNG"
            suffix = ".png" if preserve_png else ".jpg"
            resized_path = temp_dir / f"resized_{uuid.uuid4().hex[:8]}{suffix}"

            if preserve_png:
                img.save(resized_path, format="PNG", optimize=True)
            else:
                _rgb_for_jpeg(img).save(
                    resized_path,
                    format="JPEG",
                    quality=jpeg_quality,
                    optimize=True,
                )

            logger.info(
                "Image resized %dx%d (%d pixels) -> %dx%d (%d pixels)",
                width,
                height,
                source_pixels,
                new_width,
                new_height,
                new_width * new_height,
            )
            return resized_path
    except Exception as exc:
        logger.warning("Image resize failed (%s), using original", exc)
        return image_path


def _image_to_base64_data_url(image_path: Path, mime_type: str | None = None) -> str:
    """Encode an image file into the data URL format expected by chat models."""
    data = image_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    mime = mime_type or "image/jpeg"
    return f"data:{mime};base64,{encoded}"
