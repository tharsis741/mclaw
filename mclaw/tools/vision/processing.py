# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Image detection, resizing, and encoding helpers."""

from __future__ import annotations

import base64
import io
import logging
import math
import tempfile
import uuid
from pathlib import Path

from mclaw.tools.vision.config import DEFAULT_MAX_PIXELS, MAX_IMAGE_SIZE_BYTES

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
    max_pixels: int | None = DEFAULT_MAX_PIXELS,
    alignment: int = 1,
    jpeg_quality: int = 85,
) -> Path:
    """Preserve original bytes; convert unsupported inputs or honor an explicit resize budget."""
    if max_pixels is not None and max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise RuntimeError("Pillow is required for safe image decoding and resizing.") from None

    resized_path = None
    try:
        with Image.open(image_path) as img:
            source_format = (img.format or "").upper()
            if source_format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
                raise ValueError("Unsupported image format; use PNG, JPEG, WebP, GIF or BMP.")
            if img.width * img.height > 64_000_000:
                raise ValueError("Image exceeds 64 million source pixels; crop it before analysis.")
            animated = getattr(img, "n_frames", 1) > 1
            img.load()
            source_pixels = img.width * img.height
            within_budget = max_pixels is None or source_pixels <= max_pixels
            if within_budget and source_format != "BMP" and not animated:
                return image_path

            img = ImageOps.exif_transpose(img)
            width, height = img.size
            scale = min(1.0, math.sqrt(max_pixels / source_pixels)) if max_pixels is not None else 1.0
            # Align only the long edge, then apply that one scale to both axes.
            # Independently rounding short edges can badly distort panoramas.
            longest = max(width, height)
            if scale < 1 and longest * scale >= alignment:
                scale = int(longest * scale) // alignment * alignment / longest
            scaled_width = max(1, int(width * scale))
            scaled_height = max(1, int(height * scale))
            new_width, new_height = scaled_width, scaled_height

            if img.size != (new_width, new_height):
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            temp_dir = Path(tempfile.gettempdir()) / "mclaw-vision"
            temp_dir.mkdir(parents=True, exist_ok=True)
            preserve_png = source_format in {"PNG", "BMP", "GIF"}
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
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        if resized_path is not None:
            resized_path.unlink(missing_ok=True)
        raise ValueError(f"Cannot decode or normalize this image: {exc}") from exc


def _image_to_base64_data_url(image_path: Path, mime_type: str | None = None) -> str:
    """Encode an image file into the data URL format expected by chat models."""
    from PIL import Image

    with image_path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_SIZE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_SIZE_BYTES:
        raise ValueError("Image exceeds the byte limit or is empty.")
    with Image.open(io.BytesIO(data)) as image:
        actual_mime = Image.MIME.get(image.format)
        image.verify()
    if actual_mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("Image must be normalized before encoding.")
    encoded = base64.b64encode(data).decode("ascii")
    mime = actual_mime
    return f"data:{mime};base64,{encoded}"
