"""Image detection, compression, and encoding helpers."""

from __future__ import annotations

import base64
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _detect_image_mime_type(image_path: Path) -> Optional[str]:
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
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        from PIL import Image

        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return img.convert("RGB")


def _compress_image_if_needed(
    image_path: Path,
    max_dimension: int = 2048,
    quality: int = 85,
    max_file_size: int = 1_500_000,
) -> Path:
    """Resize and/or re-encode only when the image exceeds configured limits."""
    file_size = image_path.stat().st_size
    if image_path.suffix.lower() == ".svg":
        return image_path

    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.debug("Pillow not installed; skipping image compression")
        return image_path

    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            needs_resize = max(width, height) > max_dimension
            needs_compress = file_size > max_file_size
            if not needs_resize and not needs_compress:
                return image_path

            if needs_resize:
                ratio = max_dimension / max(width, height)
                new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
                img = img.resize(new_size, Image.LANCZOS)
                logger.info("Image resized %dx%d -> %dx%d", width, height, new_size[0], new_size[1])

            img = _rgb_for_jpeg(img)
            temp_dir = Path(tempfile.gettempdir()) / "mclaw-vision"
            temp_dir.mkdir(parents=True, exist_ok=True)
            compressed_path = temp_dir / f"compressed_{uuid.uuid4().hex[:8]}.jpg"

            last_quality = quality
            for q in (quality, 70, 50, 30):
                last_quality = q
                img.save(compressed_path, format="JPEG", quality=q, optimize=True)
                if compressed_path.stat().st_size <= max_file_size:
                    break

            compressed_size = compressed_path.stat().st_size
            if compressed_size >= file_size and not needs_resize:
                compressed_path.unlink(missing_ok=True)
                return image_path

            logger.info(
                "Image compressed %.1f KB -> %.1f KB (quality=%d)",
                file_size / 1024,
                compressed_size / 1024,
                last_quality,
            )
            return compressed_path
    except Exception as exc:
        logger.warning("Image compression failed (%s), using original", exc)
        return image_path


def _image_to_base64_data_url(image_path: Path, mime_type: Optional[str] = None) -> str:
    data = image_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    mime = mime_type or "image/jpeg"
    return f"data:{mime};base64,{encoded}"
