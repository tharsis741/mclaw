# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vision tool with native main-model images and independent provider analysis."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
from pathlib import Path

from mclaw.tools.interrupt import get_cancel_id, get_interrupt_event, safe_cancel_trace, set_interrupt_event, reset_interrupt_event
from mclaw.tools.registry import registry, tool_error
from mclaw.tools.vision.client import call_vision_llm as _call_vision_llm
from mclaw.tools.vision.config import (
    MAX_IMAGE_SIZE_BYTES as _MAX_IMAGE_SIZE_BYTES,
    resolve_download_timeout as _resolve_download_timeout,
    resolve_max_pixels as _resolve_max_pixels,
    resolve_timeout as _resolve_timeout,
    resolve_mode,
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
    _detect_image_mime_type,
    _image_to_base64_data_url,
    _resize_image_if_needed,
)
from mclaw.tools.vision.types import VisionInputError

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
    interrupt_token = set_interrupt_event(cancel_event)
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
        reset_interrupt_event(interrupt_token)
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


def _build_messages(data_url: str, question: str, max_pixels: int | None, provider: str = "qwen") -> list[dict]:
    """Build provider-neutral image blocks with optional Qwen pixel guidance."""
    full_prompt = (
        "只检查回答问题所需的视觉信息，直接给出结论。"
        "除非问题明确要求完整描述、解释或文字提取，否则不要复述整张图片。"
        "无法辨认的内容请明确说明，不要猜测。\n\n"
        f"问题：{question}"
    )
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": full_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, **({"max_pixels": max_pixels} if max_pixels is not None and provider in {"qwen", "qwen-intl", "dashscope"} else {})},
                },
            ],
        }
    ]


def vision_analyze(
    image_url: str,
    question: str,
    parent_agent=None,
    *,
    mode: str | None = None,
    cancel_event: threading.Event | None = None,
    _prepare_only: bool = False,
) -> str:
    """Analyze an image from a URL or local path.

    Remote images are downloaded through SSRF-safe helpers and local images are
    read in place. Temporary downloads and resized artifacts are cleaned up
    after the model call.
    """
    temp_image_path: Path | None = None
    resized_image_path: Path | None = None
    should_cleanup = True
    remote_download_finished = threading.Event()
    remote_download_abandoned = threading.Event()
    remote_download_abandon_trigger = ""
    credentials = None
    cancel_event = cancel_event or get_interrupt_event()

    try:
        if not isinstance(image_url, str) or not image_url.strip() or "\0" in image_url:
            return tool_error("image_url is required", success=False)
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            return tool_error("question is required", success=False)
        _raise_if_cancelled(cancel_event)

        image_url = image_url.strip()
        from mclaw.tools.file_operations import _checked_path
        from mclaw.tools.file_tools import _resolve_agent_path
        from mclaw.tools.vision.routing import choose_vision_route, ensure_vision_enabled

        ensure_vision_enabled(parent_agent)
        runtime = getattr(parent_agent, "provider_runtime", None)
        if _prepare_only:
            native = True
        else:
            native, credentials = choose_vision_route(parent_agent, mode, credentials_resolver=resolve_vision_credentials)

        if image_url.startswith("asset:"):
            from mclaw.tools.vision.assets import image_path

            local_path = image_path({"asset_id": image_url[6:]}, getattr(parent_agent, "session_id", ""))
        else:
            local_path = None if image_url.lower().startswith(("http://", "https://")) else _checked_path(_resolve_agent_path(image_url, parent_agent), "read")
        if local_path is not None and local_path.is_file():
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
                "Only decodable JPEG, PNG, GIF, BMP or WebP images are supported.",
                success=False,
            )

        logger.info("Image ready: %s (%.1f KB, %s)", temp_image_path.name, image_size / 1024, mime)

        _raise_if_cancelled(cancel_event)
        max_pixels = _resolve_max_pixels(parent_agent)
        resized_image_path = _resize_image_if_needed(
            temp_image_path,
            max_pixels=max_pixels,
        )
        if resized_image_path != temp_image_path:
            logger.info("Using normalized image for upload (max_pixels=%s)", max_pixels)
            target_path = resized_image_path
            mime = _detect_image_mime_type(target_path) or mime
        else:
            target_path = temp_image_path

        _raise_if_cancelled(cancel_event)
        if native:
            from mclaw.tools.vision.assets import store_image

            asset = store_image(target_path, getattr(parent_agent, "session_id", ""), question)
            _raise_if_cancelled(cancel_event)
            return json.dumps({"success": True, "status": "image_ready", "mode": "main",
                               "provider": runtime.provider if runtime else "", "model": runtime.model if runtime else "",
                               "images": [asset], "message": "The image is attached to your next model request. Inspect it and answer the question."}, ensure_ascii=False)

        data_url = _image_to_base64_data_url(target_path, mime_type=mime)
        logger.info("Base64 encoded: %.1f KB", len(data_url) / 1024)

        _raise_if_cancelled(cancel_event)
        credentials = credentials or resolve_vision_credentials(parent_agent=parent_agent)
        _raise_if_cancelled(cancel_event)
        if credentials.unsupported_reason:
            return tool_error(credentials.unsupported_reason, success=False)
        if not getattr(credentials, "available", bool(credentials.api_key)):
            return tool_error(
                "No API key available for vision analysis. "
                "Call secret_request_many(required_for='tool:vision_analyze', ...) "
                "or configure a vision provider credential, or select main-model vision.",
                success=False,
            )

        messages = _build_messages(data_url, question, max_pixels, credentials.provider)
        timeout = _resolve_timeout(parent_agent)
        logger.info("Calling vision model: %s ...", credentials.model)
        logger.info("Vision timeout: %.0fs", timeout)

        _raise_if_cancelled(cancel_event)
        analysis = _run_vision_io(
            _call_vision_llm(
                messages,
                credentials.model,
                credentials.api_key,
                credentials.base_url,
                timeout,
                provider=credentials.provider,
                runtime=getattr(credentials, "runtime", None),
            ),
            phase="model",
            timeout=timeout + 5,
            parent_agent=parent_agent,
            cancel_event=cancel_event,
        )
        _record_vision_usage(parent_agent, analysis)
        _raise_if_cancelled(cancel_event)
        if not isinstance(analysis, str) or not analysis.strip():
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
                    runtime=getattr(credentials, "runtime", None),
                ),
                phase="model_retry",
                timeout=timeout + 5,
                parent_agent=parent_agent,
                cancel_event=cancel_event,
            )
            _record_vision_usage(parent_agent, analysis)
            _raise_if_cancelled(cancel_event)

        if not isinstance(analysis, str) or not analysis.strip():
            return tool_error("Vision model returned no analysis after retry.", success=False, code="EMPTY_VISION_RESPONSE")
        from mclaw.tools.vision.assets import store_image

        images = []
        if getattr(parent_agent, "session_id", None):
            asset = store_image(target_path, parent_agent.session_id, question)
            asset.update(analysis=analysis.strip(), analysis_provider=credentials.provider, analysis_model=credentials.model)
            images.append(asset)
        _raise_if_cancelled(cancel_event)
        logger.info("Vision analysis completed (%d chars)", len(analysis))
        return json.dumps({
            "success": True,
            "status": "analyzed", "mode": "auxiliary",
            "provider": credentials.provider, "model": credentials.model,
            "analysis": analysis.strip(),
            "images": images,
        }, ensure_ascii=False)

    except VisionOperationCancelled:
        return _cancelled_result()
    except VisionInputError as exc:
        return tool_error(str(exc), success=False, code=exc.code)
    except Exception as exc:
        if getattr(exc, "termination_fence", None) is not None:
            raise
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_result()
        from mclaw.agent.transports.base import _redact_error_message
        from mclaw.providers.capabilities import is_image_rejection, observe_image_rejection

        error = _redact_error_message(str(exc), getattr(credentials, "api_key", ""))
        runtime = getattr(credentials, "runtime", None)
        if runtime and is_image_rejection(exc):
            observe_image_rejection(runtime, getattr(parent_agent, "config", None))
        logger.warning("Vision failed: %s", error)
        return tool_error(error, success=False, code="VISION_REQUEST_FAILED")

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
            resized_image_path
            and resized_image_path != temp_image_path
            and resized_image_path.exists()
        ):
            try:
                resized_image_path.unlink()
                logger.debug("Cleaned up resized image file")
            except Exception as cleanup_err:
                logger.warning("Could not delete resized file: %s", cleanup_err)


def _record_vision_usage(parent_agent, analysis) -> None:
    usage = getattr(analysis, "usage", None)
    recorder = getattr(parent_agent, "_record_usage", None)
    if usage is not None and callable(recorder):
        recorder(usage)


def analyze_image_assets(images: list[dict], parent_agent, cancel_event=None, question: str = "") -> tuple[str, str, str]:
    """Analyze prepared session assets together, including a native-input fallback."""
    from mclaw.tools.vision.assets import MAX_IMAGES, image_data_url
    from mclaw.tools.vision.routing import ensure_vision_enabled

    ensure_vision_enabled(parent_agent)
    if not images or len(images) > MAX_IMAGES:
        raise ValueError(f"Vision accepts 1 to {MAX_IMAGES} images.")
    _raise_if_cancelled(cancel_event)
    credentials = resolve_vision_credentials(parent_agent)
    if not credentials.available:
        raise ValueError(credentials.unsupported_reason or "Independent vision credentials are missing or not authorized.")
    messages = _build_messages(image_data_url(images[0], parent_agent.session_id), question or images[0].get("question", "Describe this image."), _resolve_max_pixels(parent_agent), credentials.provider)
    for asset in images[1:]:
        _raise_if_cancelled(cancel_event)
        messages[0]["content"].append({"type": "image_url", "image_url": {"url": image_data_url(asset, parent_agent.session_id)}})
    if sum(len(part.get("image_url", {}).get("url", "")) for part in messages[0]["content"]) > 32 * 1024 * 1024:
        raise ValueError("Combined image input exceeds 32 MiB.")
    timeout = _resolve_timeout(parent_agent)
    for attempt in range(2):
        _raise_if_cancelled(cancel_event)
        try:
            analysis = _run_vision_io(
                _call_vision_llm(messages, credentials.model, credentials.api_key, credentials.base_url, timeout,
                                 provider=credentials.provider, runtime=credentials.runtime),
                phase="fallback" if not attempt else "fallback_retry", timeout=timeout + 5,
                parent_agent=parent_agent, cancel_event=cancel_event,
            )
        except (VisionOperationCancelled, InterruptedError):
            raise
        except Exception as exc:
            from mclaw.agent.transports.base import _redact_error_message
            from mclaw.providers.capabilities import is_image_rejection, observe_image_rejection

            if getattr(exc, "termination_fence", None) is not None:
                raise
            if credentials.runtime and is_image_rejection(exc):
                observe_image_rejection(credentials.runtime, getattr(parent_agent, "config", None))
            raise ValueError(_redact_error_message(str(exc), credentials.api_key)) from None
        _record_vision_usage(parent_agent, analysis)
        _raise_if_cancelled(cancel_event)
        if isinstance(analysis, str) and analysis.strip():
            return analysis.strip(), credentials.provider, credentials.model
    raise ValueError("Vision model returned no analysis after retry.")


def diagnose_vision_requirements(config: dict | None = None) -> dict:
    """Expose registry diagnostics for the currently configured vision provider."""
    return diagnose_vision_credentials(config=config)


def check_vision_requirements(config: dict | None = None) -> bool:
    """Return whether a supported vision provider has authorized credentials."""
    # Main-model selection is session-specific and can differ from global setup.
    if resolve_mode(config=config) in {"main", "auto"}:
        return True
    diagnostics = diagnose_vision_requirements(config=config)
    return bool(diagnostics.get("available"))


VISION_ANALYZE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision_analyze",
        "description": (
            "Inspect an image with the current model or the configured dedicated vision model. "
            "Use image content or visual analysis already supplied in the conversation directly. "
            "Call this tool when the needed image content is missing. Provide either an "
            "HTTP/HTTPS URL or a local file path. The tool downloads remote "
            "images, validates and conditionally resizes them, then sends the "
            "image to the selected vision route. In main mode, status=image_ready means the image is attached to your next request; inspect it directly and answer, without calling this tool again.\n\n"
            "In dedicated mode, the response answers your question directly and includes only the "
            "relevant visual details unless you request a full description."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": (
                        "Image URL (http/https), local file path relative to the session workspace, or asset:<id> from an earlier image result. "
                        "Examples: 'https://example.com/photo.jpg' or '/home/user/screenshot.png'"
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Your specific question or request about the image. "
                        "Describe only the visual details needed to answer this question."
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
    max_result_size_chars=32_000,
)
