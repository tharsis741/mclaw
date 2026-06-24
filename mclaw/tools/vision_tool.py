# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vision Tool — analyse images via Qwen vision models."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from mclaw.tools.registry import registry, tool_error
from mclaw.tools.vision.client import call_vision_llm as _call_vision_llm
from mclaw.tools.vision.config import (
    MAX_IMAGE_SIZE_BYTES as _MAX_IMAGE_SIZE_BYTES,
    authorized_env_value as _authorized_env_value,
    effective_config as _effective_config,
    env_value as _env_value,
    resolve_download_timeout as _resolve_download_timeout,
    resolve_timeout as _resolve_timeout,
)
from mclaw.tools.vision.credentials import (
    diagnose_vision_credentials,
    feature_env_configured as _feature_env_configured,
    resolve_vision_credentials,
)
from mclaw.tools.vision.image_io import _download_image_sync, _is_safe_url
from mclaw.tools.vision.processing import (
    _compress_image_if_needed,
    _detect_image_mime_type,
    _image_to_base64_data_url,
)

logger = logging.getLogger(__name__)


def _build_messages(data_url: str, question: str) -> list[dict]:
    full_prompt = (
        "先客观描述图片中与问题相关的内容，再回答下面的问题。\n\n"
        f"问题：{question}"
    )
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": full_prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


def vision_analyze(
    image_url: str,
    question: str,
    parent_agent=None,
) -> str:
    """Analyse an image from a URL or local path."""
    temp_image_path: Optional[Path] = None
    compressed_image_path: Optional[Path] = None
    should_cleanup = True
    model_for_error = "unresolved"

    try:
        if not image_url or not isinstance(image_url, str):
            return tool_error("image_url is required", success=False)
        if not question or not isinstance(question, str):
            return tool_error("question is required", success=False)

        logger.info("Vision analyse: %s", image_url[:80])

        local_path = Path(os.path.expanduser(image_url))
        if local_path.is_file():
            logger.info("Using local image: %s", image_url)
            temp_image_path = local_path
            should_cleanup = False
        else:
            if not image_url.startswith(("http://", "https://")):
                return tool_error(
                    "Invalid image_url. Provide an HTTP/HTTPS URL or a local file path.",
                    success=False,
                )
            if not _is_safe_url(image_url):
                return tool_error(
                    "Blocked: URL targets a private or internal address.",
                    success=False,
                )

            logger.info("Downloading image...")
            temp_dir = Path(tempfile.gettempdir()) / "mclaw-vision"
            temp_image_path = temp_dir / f"temp_image_{uuid.uuid4().hex[:8]}.jpg"
            _download_image_sync(image_url, temp_image_path, parent_agent=parent_agent)
            should_cleanup = True

        if not temp_image_path.exists():
            return tool_error("Image file not found after download.", success=False)

        image_size = temp_image_path.stat().st_size
        if image_size > _MAX_IMAGE_SIZE_BYTES:
            return tool_error(
                f"Image too large ({image_size / 1024 / 1024:.1f} MB > {_MAX_IMAGE_SIZE_BYTES / 1024 / 1024:.0f} MB limit).",
                success=False,
            )

        mime = _detect_image_mime_type(temp_image_path)
        if not mime:
            return tool_error(
                "Only real image files are supported (JPEG, PNG, GIF, BMP, WebP, SVG).",
                success=False,
            )

        logger.info("Image ready: %s (%.1f KB, %s)", temp_image_path.name, image_size / 1024, mime)

        compressed_image_path = _compress_image_if_needed(temp_image_path)
        if compressed_image_path != temp_image_path:
            logger.info("Using compressed image for upload")
            target_path = compressed_image_path
            mime = "image/jpeg"
        else:
            target_path = temp_image_path

        data_url = _image_to_base64_data_url(target_path, mime_type=mime)
        logger.info("Base64 encoded: %.1f KB", len(data_url) / 1024)

        credentials = resolve_vision_credentials(parent_agent=parent_agent)
        model_for_error = credentials.model or model_for_error
        if credentials.unsupported_reason:
            return tool_error(credentials.unsupported_reason, success=False)
        if not credentials.api_key:
            return tool_error(
                "No API key available for vision analysis. "
                "Call secret_request_many(required_for='tool:vision_analyze', ...) "
                "or rerun setup to authorize DASHSCOPE_API_KEY/QWEN_API_KEY.",
                success=False,
            )

        messages = _build_messages(data_url, question)
        base_timeout = _resolve_timeout(parent_agent)
        extra = (len(data_url) / (1024 * 1024)) * 60
        timeout = max(base_timeout, min(base_timeout + extra, 600.0))
        logger.info("Calling vision model: %s ...", credentials.model)
        logger.info("Vision timeout: %.0fs (base %.0fs + extra %.0fs)", timeout, base_timeout, extra)

        analysis = _call_vision_llm(
            messages,
            credentials.model,
            credentials.api_key,
            credentials.base_url,
            timeout,
            provider=credentials.provider,
        )
        if not analysis:
            logger.warning("Vision model returned empty content, retrying once...")
            analysis = _call_vision_llm(
                messages,
                credentials.model,
                credentials.api_key,
                credentials.base_url,
                timeout,
                provider=credentials.provider,
            )

        logger.info("Vision analysis completed (%d chars)", len(analysis))
        return json.dumps({
            "success": True,
            "analysis": analysis or "The image could not be analysed.",
        }, ensure_ascii=False)

    except Exception as exc:
        err_str = str(exc).lower()
        logger.exception("Vision analyse error: %s", exc)

        if any(h in err_str for h in ("402", "insufficient", "payment required", "credits", "billing")):
            analysis = (
                "Insufficient credits or payment required. Please top up your "
                f"API provider account and try again. Error: {exc}"
            )
        elif any(h in err_str for h in (
            "does not support", "not support image", "invalid_request",
            "content_policy", "image_url", "multimodal", "unrecognized request argument",
            "image input",
        )):
            analysis = (
                f"The model ({model_for_error}) does not support vision or the request was not "
                f"accepted by the server. Error: {exc}"
            )
        else:
            analysis = (
                "There was a problem with the request and the image could not be "
                f"analysed. Error: {exc}"
            )

        return json.dumps({
            "success": False,
            "error": str(exc),
            "analysis": analysis,
        }, ensure_ascii=False)

    finally:
        if should_cleanup and temp_image_path and temp_image_path.exists():
            try:
                temp_image_path.unlink()
                logger.debug("Cleaned up temporary image file")
            except Exception as cleanup_err:
                logger.warning("Could not delete temporary file: %s", cleanup_err)
        if (
            compressed_image_path
            and compressed_image_path != temp_image_path
            and compressed_image_path.exists()
        ):
            try:
                compressed_image_path.unlink()
                logger.debug("Cleaned up compressed image file")
            except Exception as cleanup_err:
                logger.warning("Could not delete compressed file: %s", cleanup_err)


def diagnose_vision_requirements(config: dict | None = None) -> dict:
    return diagnose_vision_credentials(config=config)


def check_vision_requirements(config: dict | None = None) -> bool:
    diagnostics = diagnose_vision_requirements(config=config)
    return bool(diagnostics.get("available"))


VISION_ANALYZE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision_analyze",
        "description": (
            "Analyse an image using Qwen vision. Provide either an "
            "HTTP/HTTPS URL or a local file path. The tool downloads remote "
            "images, validates and optionally compresses them, then sends the "
            "image to a Qwen vision-capable model.\n\n"
            "The response includes a full description of the image and an answer "
            "to your specific question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": (
                        "Image URL (http/https) or local file path to analyse. "
                        "Examples: 'https://example.com/photo.jpg' or '/home/user/screenshot.png'"
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Your specific question or request about the image. "
                        "The AI will provide a complete description AND answer this question."
                    ),
                },
            },
            "required": ["image_url", "question"],
        },
    },
}


def _handle_vision_analyze(args: dict, **kw) -> str:
    return vision_analyze(
        image_url=args.get("image_url", ""),
        question=args.get("question", ""),
        parent_agent=kw.get("parent_agent"),
    )


registry.register(
    name="vision_analyze",
    toolset="vision",
    schema=VISION_ANALYZE_SCHEMA,
    handler=_handle_vision_analyze,
    check_fn=check_vision_requirements,
    diagnose_fn=diagnose_vision_requirements,
    description="分析图片内容（支持URL和本地路径）",
    emoji="👁️",
    max_result_size_chars=10_000,
)
