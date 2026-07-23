# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vision Tool — analyze images via Qwen vision models."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from mclaw.tools.interrupt import get_cancel_id, get_interrupt_event, safe_cancel_trace
from mclaw.tools.registry import registry, tool_error
from mclaw.tools.vision.client import call_vision_llm as _call_vision_llm
from mclaw.tools.vision.config import (
    MAX_IMAGE_SIZE_BYTES as _MAX_IMAGE_SIZE_BYTES,
    resolve_download_timeout as _resolve_download_timeout,
    resolve_timeout as _resolve_timeout,
)
from mclaw.tools.vision.credentials import (
    diagnose_vision_credentials,
    resolve_vision_credentials,
)
from mclaw.tools.vision.image_io import (
    VisionOperationCancelled,
    _download_image_async,
    _remove_partial_image,
)
from mclaw.tools.vision.processing import (
    _compress_image_if_needed,
    _detect_image_mime_type,
    _image_to_base64_data_url,
)

logger = logging.getLogger(__name__)


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VisionOperationCancelled("Vision analysis interrupted by user")


def _cancelled_result() -> str:
    return tool_error(
        "Vision analysis interrupted by user",
        success=False,
        interrupted=True,
        status="cancelled",
    )


def _run_vision_io(
    coro,
    *,
    phase: str,
    timeout: float,
    parent_agent,
    cancel_event: threading.Event | None,
):
    """Run one async Vision I/O stage on the shared cancellable bridge."""
    from mclaw.tools.dispatch import _run_async

    started = time.monotonic()
    trigger: str | None = None
    try:
        return _run_async(
            coro,
            parent_agent=parent_agent,
            diagnostic_name=f"vision_{phase}",
            timeout_seconds=max(0.001, timeout),
            raise_on_stop=True,
        )
    except TimeoutError:
        trigger = "deadline"
        raise
    except InterruptedError as exc:
        if cancel_event is not None and cancel_event.is_set():
            trigger = "event"
            raise VisionOperationCancelled("Vision analysis interrupted by user") from exc
        raise
    finally:
        if trigger is None and cancel_event is not None and cancel_event.is_set():
            trigger = "event"
        if trigger is not None:
            safe_cancel_trace(
                lambda: logger.warning(
                    "[CANCEL_TRACE] vision_io_cancel cancel_id=%s session=%s "
                    "phase=%s trigger=%s elapsed_ms=%d",
                    get_cancel_id(cancel_event),
                    getattr(parent_agent, "session_id", "?"),
                    phase,
                    trigger,
                    int((time.monotonic() - started) * 1000),
                )
            )


def _build_messages(data_url: str, question: str) -> list[dict]:
    """Build the multimodal chat payload expected by Qwen-compatible clients."""
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
    """Analyze an image from a URL or local path.

    Remote images are downloaded through SSRF-safe helpers and local images are
    read in place. Temporary downloads and compression artifacts are cleaned up
    after the model call.
    """
    temp_image_path: Path | None = None
    compressed_image_path: Path | None = None
    should_cleanup = True
    remote_download_finished = threading.Event()
    remote_download_abandoned = threading.Event()
    remote_download_abandon_trigger = ""
    model_for_error = "unresolved"
    cancel_event = get_interrupt_event()

    try:
        if not image_url or not isinstance(image_url, str):
            return tool_error("image_url is required", success=False)
        if not question or not isinstance(question, str):
            return tool_error("question is required", success=False)
        _raise_if_cancelled(cancel_event)

        logger.info("Vision analyze: %s", image_url[:80])

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
            _raise_if_cancelled(cancel_event)
            logger.info("Downloading image...")
            temp_dir = Path(tempfile.gettempdir()) / "mclaw-vision"
            temp_image_path = temp_dir / f"temp_image_{uuid.uuid4().hex[:8]}.jpg"
            download_timeout = max(0.001, _resolve_download_timeout(parent_agent))

            async def download_remote_image() -> Path:
                path = await _download_image_async(
                    image_url,
                    temp_image_path,
                    parent_agent=parent_agent,
                    cancel_event=cancel_event,
                )
                # Mark completion inside the real task, before its result is
                # propagated through the cross-thread Future.
                remote_download_finished.set()
                if remote_download_abandoned.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    _remove_partial_image(path)
                    cleanup_trigger = remote_download_abandon_trigger or "event"
                    safe_cancel_trace(
                        lambda: logger.info(
                            "[CANCEL_TRACE] vision_download_cleanup_after_cancel "
                            "cancel_id=%s phase=download trigger=%s",
                            get_cancel_id(cancel_event),
                            cleanup_trigger,
                        )
                    )
                return path

            try:
                temp_image_path = _run_vision_io(
                    download_remote_image(),
                    phase="download",
                    timeout=(3 * download_timeout) + 11,
                    parent_agent=parent_agent,
                    cancel_event=cancel_event,
                )
            except BaseException as exc:
                remote_download_abandon_trigger = (
                    "event"
                    if cancel_event is not None and cancel_event.is_set()
                    else "deadline"
                    if isinstance(exc, TimeoutError)
                    else "bridge_failure"
                )
                remote_download_abandoned.set()
                raise
            should_cleanup = True

        _raise_if_cancelled(cancel_event)
        if not temp_image_path.exists():
            return tool_error("Image file not found after download.", success=False)

        image_size = temp_image_path.stat().st_size
        _raise_if_cancelled(cancel_event)
        if image_size > _MAX_IMAGE_SIZE_BYTES:
            return tool_error(
                f"Image too large ({image_size / 1024 / 1024:.1f} MB > {_MAX_IMAGE_SIZE_BYTES / 1024 / 1024:.0f} MB limit).",
                success=False,
            )

        mime = _detect_image_mime_type(temp_image_path)
        _raise_if_cancelled(cancel_event)
        if not mime:
            return tool_error(
                "Only real image files are supported (JPEG, PNG, GIF, BMP, WebP, SVG).",
                success=False,
            )

        logger.info("Image ready: %s (%.1f KB, %s)", temp_image_path.name, image_size / 1024, mime)

        _raise_if_cancelled(cancel_event)
        compressed_image_path = _compress_image_if_needed(temp_image_path)
        if compressed_image_path != temp_image_path:
            logger.info("Using compressed image for upload")
            target_path = compressed_image_path
            mime = "image/jpeg"
        else:
            target_path = temp_image_path

        _raise_if_cancelled(cancel_event)
        data_url = _image_to_base64_data_url(target_path, mime_type=mime)
        logger.info("Base64 encoded: %.1f KB", len(data_url) / 1024)

        _raise_if_cancelled(cancel_event)
        credentials = resolve_vision_credentials(parent_agent=parent_agent)
        _raise_if_cancelled(cancel_event)
        model_for_error = credentials.model or model_for_error
        if credentials.unsupported_reason:
            return tool_error(credentials.unsupported_reason, success=False)
        if not credentials.api_key:
            return tool_error(
                "No API key available for vision analysis. "
                "Call secret_request_many(required_for='tool:vision_analyze', ...) "
                "or rerun setup to authorize a registry-declared Qwen credential.",
                success=False,
            )

        messages = _build_messages(data_url, question)
        base_timeout = _resolve_timeout(parent_agent)
        extra = (len(data_url) / (1024 * 1024)) * 60
        timeout = max(base_timeout, min(base_timeout + extra, 600.0))
        logger.info("Calling vision model: %s ...", credentials.model)
        logger.info("Vision timeout: %.0fs (base %.0fs + extra %.0fs)", timeout, base_timeout, extra)

        _raise_if_cancelled(cancel_event)
        analysis = _run_vision_io(
            _call_vision_llm(
                messages,
                credentials.model,
                credentials.api_key,
                credentials.base_url,
                timeout,
                provider=credentials.provider,
            ),
            phase="model",
            timeout=timeout + 5,
            parent_agent=parent_agent,
            cancel_event=cancel_event,
        )
        _raise_if_cancelled(cancel_event)
        if not analysis:
            logger.warning("Vision model returned empty content, retrying once...")
            _raise_if_cancelled(cancel_event)
            analysis = _run_vision_io(
                _call_vision_llm(
                    messages,
                    credentials.model,
                    credentials.api_key,
                    credentials.base_url,
                    timeout,
                    provider=credentials.provider,
                ),
                phase="model_retry",
                timeout=timeout + 5,
                parent_agent=parent_agent,
                cancel_event=cancel_event,
            )
            _raise_if_cancelled(cancel_event)

        logger.info("Vision analysis completed (%d chars)", len(analysis))
        return json.dumps({
            "success": True,
            "analysis": analysis or "The image could not be analyzed.",
        }, ensure_ascii=False)

    except VisionOperationCancelled:
        return _cancelled_result()
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_result()
        err_str = str(exc).lower()
        logger.exception("Vision analyze error: %s", exc)

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
                f"analyzed. Error: {exc}"
            )

        return json.dumps({
            "success": False,
            "error": str(exc),
            "analysis": analysis,
        }, ensure_ascii=False)

    finally:
        if (
            should_cleanup
            and (
                remote_download_finished.is_set()
                or cancel_event is None
                or not cancel_event.is_set()
            )
            and temp_image_path
            and temp_image_path.exists()
        ):
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
    """Expose registry diagnostics for the currently configured vision provider."""
    return diagnose_vision_credentials(config=config)


def check_vision_requirements(config: dict | None = None) -> bool:
    """Return whether a supported vision provider has authorized credentials."""
    diagnostics = diagnose_vision_requirements(config=config)
    return bool(diagnostics.get("available"))


VISION_ANALYZE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision_analyze",
        "description": (
            "Analyze an image using Qwen vision. Provide either an "
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
                        "Image URL (http/https) or local file path to analyze. "
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
    """Registry wrapper that forwards parent agent context for config/secrets."""
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
    description="Analyze images from URL or local path",
    emoji="👁️",
    max_result_size_chars=10_000,
)
