# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Translate image input blocks at the provider protocol boundary."""

from __future__ import annotations


def anthropic_image_content(content):
    if not isinstance(content, list):
        return content
    result = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "image_url":
            result.append(block)
            continue
        image = block.get("image_url")
        url = image.get("url") if isinstance(image, dict) else None
        if not isinstance(url, str):
            raise ValueError("Image URL must be a string.")
        if url.startswith("data:"):
            header, separator, data = url.partition(",")
            mime = header.removeprefix("data:").removesuffix(";base64")
            if not separator or not header.endswith(";base64") or mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError("Unsupported image data URL.")
            source = {"type": "base64", "media_type": mime, "data": data}
        elif url.startswith(("https://", "http://")):
            source = {"type": "url", "url": url}
        else:
            raise ValueError("Image URL must be HTTP(S) or base64 image data.")
        result.append({"type": "image", "source": source})
    return result
