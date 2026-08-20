# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from PIL import Image

from mclaw.tools import vision_tool
from mclaw.tools.vision import client as vision_client
from mclaw.tools.vision import processing
from mclaw.tools.vision.config import DEFAULT_MAX_PIXELS, resolve_max_pixels


def test_image_below_pixel_budget_is_not_reencoded(tmp_path) -> None:
    image_path = tmp_path / "large-file.bmp"
    Image.new("RGB", (800, 800), "white").save(image_path, format="BMP")
    original_bytes = image_path.read_bytes()

    result = processing._resize_image_if_needed(
        image_path,
        max_pixels=DEFAULT_MAX_PIXELS,
    )

    assert image_path.stat().st_size > 1_500_000
    assert result == image_path
    assert image_path.read_bytes() == original_bytes


def test_oversized_png_is_resized_proportionally_and_preserved(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screenshot.png"
    Image.new("RGBA", (2048, 1024), (255, 255, 255, 0)).save(image_path)
    monkeypatch.setattr(processing.tempfile, "gettempdir", lambda: str(tmp_path))

    result = processing._resize_image_if_needed(
        image_path,
        max_pixels=DEFAULT_MAX_PIXELS,
    )

    assert result != image_path
    assert result.suffix == ".png"
    with Image.open(result) as resized:
        assert resized.format == "PNG"
        assert resized.width * resized.height <= DEFAULT_MAX_PIXELS
        assert resized.width % 32 == 0
        assert resized.height % 32 == 0
        assert abs((resized.width / resized.height) - 2.0) < 0.01


def test_oversized_non_png_is_encoded_as_jpeg_once(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "photo.webp"
    Image.new("RGB", (2048, 1024), "navy").save(image_path, format="WEBP")
    monkeypatch.setattr(processing.tempfile, "gettempdir", lambda: str(tmp_path))

    result = processing._resize_image_if_needed(
        image_path,
        max_pixels=DEFAULT_MAX_PIXELS,
        jpeg_quality=85,
    )

    assert result.suffix == ".jpg"
    with Image.open(result) as resized:
        assert resized.format == "JPEG"
        assert resized.width * resized.height <= DEFAULT_MAX_PIXELS


def test_vision_message_uses_pixel_budget_and_direct_answer_prompt() -> None:
    messages = vision_tool._build_messages(
        "data:image/png;base64,aW1hZ2U=",
        "按钮上的文字是什么？",
        DEFAULT_MAX_PIXELS,
    )

    content = messages[0]["content"]
    assert content[1]["image_url"]["max_pixels"] == DEFAULT_MAX_PIXELS
    assert "直接给出结论" in content[0]["text"]
    assert "先客观描述图片" not in content[0]["text"]


def test_qwen3_vision_explicitly_disables_thinking(monkeypatch) -> None:
    requests: list[dict] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(content="ok", reasoning_content="")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

    monkeypatch.setattr(
        vision_client.openai,
        "AsyncOpenAI",
        lambda **_kwargs: FakeClient(),
    )

    result = asyncio.run(
        vision_client.call_vision_llm(
            messages=[],
            model="qwen3-vl-flash",
            api_key="test",
            base_url="https://example.test/v1",
            timeout=1,
        )
    )

    assert result == "ok"
    assert requests[0]["extra_body"] == {"enable_thinking": False}
    assert requests[0]["max_tokens"] == 2000


def test_max_pixels_config_is_bounded() -> None:
    valid = SimpleNamespace(
        config={"auxiliary": {"vision": {"max_pixels": 2_000_000}}}
    )
    invalid = SimpleNamespace(
        config={"auxiliary": {"vision": {"max_pixels": "2000000"}}}
    )

    assert resolve_max_pixels(valid) == 2_000_000
    assert resolve_max_pixels(invalid) == DEFAULT_MAX_PIXELS
