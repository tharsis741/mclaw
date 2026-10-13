# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Project session image references into the next ordinary model request."""

from __future__ import annotations

import json
import logging
from copy import deepcopy

from mclaw.providers.capabilities import (
    is_image_rejection, observe_image_rejection, resolve_image_capability,
)
from mclaw.runtime.attachments import image_question, refresh_attachment_context
from mclaw.tools.vision.assets import MAX_IMAGES, image_data_url
from mclaw.tools.vision.config import resolve_mode
from mclaw.tools.vision.image_io import VisionOperationCancelled
from mclaw.tools.vision.types import VisionInputError

logger = logging.getLogger(__name__)


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VisionOperationCancelled("Image input interrupted by user")


def prepare_user_images(agent, text: str, paths: list[str], cancel_event) -> tuple[str, list[dict]]:
    """Use the same preprocessing and routing as vision_analyze for attachments."""
    from mclaw.tools.vision_tool import vision_analyze
    from mclaw.tools.vision.routing import choose_vision_route

    if len(paths) > MAX_IMAGES:
        raise VisionInputError(f"每条消息最多添加 {MAX_IMAGES} 张图片，请减少图片后重试。", "TOO_MANY_IMAGES")
    images = []
    try:
        choose_vision_route(agent)
    except VisionInputError:
        raise
    except ValueError as error:
        raise VisionInputError(str(error), "VISION_CONFIGURATION") from None
    for path in paths:
        _check_cancel(cancel_event)
        result = json.loads(vision_analyze(path, (image_question(text) or "Describe the relevant visual information.")[:4000], agent, cancel_event=cancel_event, _prepare_only=True))
        _check_cancel(cancel_event)
        if not result.get("success"):
            raise VisionInputError("图片读取失败：" + (result.get("error") or "请检查图片是否完整、格式是否受支持。"))
        images.extend({**asset, "pending": True} for asset in result.get("images", []))
    # Persist the original input before any paid analysis. Projection caches a
    # dedicated model's answer on these same references for retries and resume.
    return text, images


def tool_images(agent, name: str, result: str, cancel_event=None) -> list[dict]:
    """Only trusted built-ins can publish image references into the model context."""
    if name not in {"vision_analyze", "browser_screenshot"}:
        return []
    try:
        from mclaw.tools.vision.routing import ensure_vision_enabled

        ensure_vision_enabled(agent)
        payload = json.loads(result)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return []
        if name == "browser_screenshot":
            runtime = getattr(agent, "provider_runtime", None)
            if not runtime or resolve_mode(agent) == "auxiliary":
                return []
            if resolve_image_capability(runtime, agent.config).state != "supported":
                return []
            from mclaw.tools.vision_tool import vision_analyze

            payload = json.loads(vision_analyze(
                payload.get("path", ""), "Inspect this browser screenshot to continue the current task.",
                agent, mode="main", cancel_event=cancel_event,
            ))
        if (payload.get("status"), payload.get("mode")) in {("image_ready", "main"), ("analyzed", "auxiliary")}:
            images = payload.get("images")
            if isinstance(images, list) and len(images) <= MAX_IMAGES and all(isinstance(item, dict) for item in images):
                return [{**deepcopy(item), "pending": True} for item in images]
    except (ValueError, TypeError):
        pass
    return []


def _persist(agent, message: dict) -> None:
    db = getattr(agent, "_session_db", None)
    identifier = message.get("_message_id")
    if db is not None and isinstance(identifier, int):
        db.update_message_images(agent.session_id, identifier, message.get("_vision_images", []))


def _selected_images(agent, messages: list[dict]) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    images = [(index, offset, asset) for index, message in enumerate(messages)
              for offset, asset in enumerate(message.get("_vision_images", []))]
    last_response = max((index for index, message in enumerate(messages) if message.get("role") == "assistant"), default=-1)
    pending_tools = getattr(agent, "_tool_call_ids_pending_visibility", set())
    pending = {(index, offset) for index, offset, asset in images
               if asset.get("pending", index > last_response) or messages[index].get("tool_call_id") in pending_tools}
    selected = {(index, offset) for index, offset, _ in images[-MAX_IMAGES:]}
    if pending - selected:
        raise ValueError(f"Too many new images in one model request; use at most {MAX_IMAGES}.")
    return selected, pending


def mark_vision_images_seen(agent, messages: list[dict]) -> None:
    """Only a complete model response acknowledges newly attached images."""
    selected, _ = _selected_images(agent, messages)
    for index, message in enumerate(messages):
        changed = False
        for offset, asset in enumerate(message.get("_vision_images", [])):
            if (index, offset) in selected and asset.get("pending") is not False:
                asset["pending"] = False
                changed = True
        if changed:
            _persist(agent, message)


def _native_image_urls(agent, messages: list[dict], selected: set, pending: set) -> tuple[dict, dict]:
    urls, omitted = {}, {}
    total_bytes = 0
    # Reserve the request budget for unseen images first, then recent history.
    for key in sorted(selected, key=lambda item: (item not in pending, -item[0], -item[1])):
        index, offset = key
        asset = messages[index]["_vision_images"][offset]
        try:
            url = image_data_url(asset, agent.session_id)
        except (OSError, ValueError):
            if key in pending:
                raise ValueError("A new image asset is missing or invalid; attach it again.") from None
            omitted[key] = "Earlier image file unavailable; request a new attachment if needed."
            continue
        if total_bytes + len(url) > 32 * 1024 * 1024:
            if key in pending:
                raise ValueError("Combined new image input exceeds 32 MiB; reduce the number or size of images.")
            omitted[key] = "Earlier image omitted to fit the request; reopen with vision_analyze if needed."
            continue
        urls[key] = url
        total_bytes += len(url)
    return urls, omitted


def project_vision_messages(agent, messages: list[dict], cancel_event=None) -> list[dict]:
    """Build ephemeral image blocks; keep bytes out of history, events and logs."""
    messages = [
        {**message, "content": refresh_attachment_context(message["content"])}
        if message.get("role") == "user" and isinstance(message.get("content"), str) else message
        for message in messages
    ]
    if not any(message.get("_vision_images") for message in messages):
        return messages
    from mclaw.tools.vision_tool import analyze_image_assets
    from mclaw.tools.vision.routing import choose_vision_route

    selected_keys, pending = _selected_images(agent, messages)
    unavailable = ""
    try:
        native, credentials = choose_vision_route(agent)
    except ValueError as error:
        if pending:
            raise
        native, credentials, unavailable = False, None, str(error)
    if not native and credentials is not None and not credentials.available:
        unavailable = credentials.unsupported_reason or "Configure a dedicated vision model to inspect these images."
    native_urls, omitted = _native_image_urls(agent, messages, selected_keys, pending) if native else ({}, {})

    projected = []
    pending_images = []
    for index, original in enumerate(messages):
        _check_cancel(cancel_event)
        message = {key: value for key, value in original.items() if not key.startswith("_vision") and key != "_message_id"}
        if message.get("role") != "tool" and pending_images:
            projected.append({"role": "user", "content": pending_images})
            pending_images = []
        parts = []
        selected_assets = [asset for offset, asset in enumerate(original.get("_vision_images", [])) if (index, offset) in selected_keys]
        if not native and not unavailable and any(not asset.get("analysis") for asset in selected_assets):
            # Keep related images together so a fallback can answer comparisons.
            available = []
            for offset, asset in enumerate(original.get("_vision_images", [])):
                if (index, offset) not in selected_keys:
                    continue
                try:
                    image_data_url(asset, agent.session_id)
                except (OSError, ValueError):
                    if (index, offset) in pending and not asset.get("analysis"):
                        raise ValueError("A new image asset is missing or invalid; attach it again.") from None
                    omitted[index, offset] = "Earlier image file unavailable; request a new attachment if needed."
                else:
                    available.append(asset)
            if any(not asset.get("analysis") for asset in available):
                question = "\n".join(dict.fromkeys(image_question(asset.get("question", "")) for asset in available))
                analysis, provider, model = analyze_image_assets(available, agent, cancel_event, question=question)
                _check_cancel(cancel_event)
                for asset in available:
                    asset.update(analysis=analysis, analysis_provider=provider, analysis_model=model)
                _persist(agent, original)
        emitted_analysis = set()
        for offset, asset in enumerate(original.get("_vision_images", [])):
            question = image_question(asset.get("question", ""))
            label = f"Image asset:{asset.get('asset_id', '')}. Question: {question}"
            if (index, offset) not in selected_keys:
                parts.append({"type": "text", "text": label + " [Earlier image retained; reopen with vision_analyze if needed.]"})
                continue
            if native:
                url = native_urls.get((index, offset))
                if url is None:
                    parts.append({"type": "text", "text": label + " [" + omitted[index, offset] + "]"
                                  + ("\nPrevious analysis: " + asset["analysis"] if asset.get("analysis") else "")})
                    continue
                parts.extend([{"type": "text", "text": label + "\nThis image is included below; inspect it directly."},
                              {"type": "image_url", "image_url": {"url": url}}])
            else:
                cached = asset.get("analysis")
                if not cached:
                    if (index, offset) in pending:
                        raise ValueError(unavailable)
                    parts.append({"type": "text", "text": label + " [Image not supplied to this model: " + omitted.get((index, offset), unavailable) + "]"})
                    continue
                text = label
                if cached not in emitted_analysis:
                    text += f"\n[Dedicated vision model: {asset.get('analysis_provider')}/{asset.get('analysis_model')}]\n" + cached
                    emitted_analysis.add(cached)
                parts.append({"type": "text", "text": text})
        if parts:
            if not native:
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
                elif message.get("role") == "tool":
                    try:
                        status = json.loads(content)
                    except (ValueError, TypeError):
                        status = None
                    if isinstance(status, dict) and status.get("status") in {"image_ready", "analyzed"}:
                        content = json.dumps({"success": True, "status": "analyzed", "mode": "auxiliary",
                                              "message": "Image context follows; check whether image data or a previous analysis is available."})
                message["content"] = content + "\n\n" + "\n".join(part["text"] for part in parts)
            elif message.get("role") == "tool" and agent.provider_runtime.api_mode == "chat_completions":
                pending_images.extend(parts)
            else:
                content = message.get("content")
                message["content"] = ([{"type": "text", "text": content}] if isinstance(content, str) and content else list(content or [])) + parts
        projected.append(message)
    if pending_images:
        projected.append({"role": "user", "content": pending_images})
    logger.info("[VISION INPUT] mode=%s selected=%d pending=%d supplied_images=%d omitted=%d",
                "main" if native else "auxiliary", len(selected_keys), len(pending), len(native_urls), len(omitted))
    return projected


def recover_image_rejection(agent, request_messages: list[dict], error: BaseException, cancel_event=None) -> bool:
    has_images = any(isinstance(message.get("content"), list) and any(
        isinstance(part, dict) and part.get("type") in {"image_url", "image"}
        for part in message["content"]) for message in request_messages)
    if not has_images or not is_image_rejection(error):
        return False
    _check_cancel(cancel_event)
    observe_image_rejection(agent.provider_runtime, agent.config)
    if resolve_mode(agent) != "auto":
        return False
    from mclaw.tools.vision.credentials import resolve_vision_credentials

    return resolve_vision_credentials(agent).available
