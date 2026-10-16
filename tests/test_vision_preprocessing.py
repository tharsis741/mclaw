# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import openai
import pytest
from PIL import Image

from mclaw.tools import vision_tool
from mclaw.tools.vision import client as vision_client
from mclaw.tools.vision import processing
from mclaw.tools.vision.config import DEFAULT_MAX_PIXELS, resolve_max_pixels

RESIZE_BUDGET = 1_310_720


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_default_keeps_original_bytes_above_old_pixel_budget(tmp_path, image_format):
    source = tmp_path / ("original." + image_format.lower())
    Image.new("RGB", (2048, 1024), "navy").save(source, format=image_format)
    original = source.read_bytes()

    prepared = processing._resize_image_if_needed(source)

    assert prepared == source
    assert prepared.read_bytes() == original
    encoded = processing._image_to_base64_data_url(prepared)
    assert base64.b64decode(encoded.split(",", 1)[1]) == original


def test_bmp_below_pixel_budget_is_normalized_to_supported_png(tmp_path) -> None:
    image_path = tmp_path / "large-file.bmp"
    Image.new("RGB", (800, 800), "white").save(image_path, format="BMP")
    original_bytes = image_path.read_bytes()

    result = processing._resize_image_if_needed(
        image_path,
        max_pixels=DEFAULT_MAX_PIXELS,
    )

    assert image_path.stat().st_size > 1_500_000
    assert result != image_path
    with Image.open(result) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (800, 800)
    result.unlink()
    assert image_path.read_bytes() == original_bytes


def test_oversized_png_is_resized_proportionally_and_preserved(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screenshot.png"
    Image.new("RGBA", (2048, 1024), (255, 255, 255, 0)).save(image_path)
    monkeypatch.setattr(processing.tempfile, "gettempdir", lambda: str(tmp_path))

    result = processing._resize_image_if_needed(
        image_path,
        max_pixels=RESIZE_BUDGET,
    )

    assert result != image_path
    assert result.suffix == ".png"
    with Image.open(result) as resized:
        assert resized.format == "PNG"
        assert resized.width * resized.height <= RESIZE_BUDGET
        assert abs((resized.width / resized.height) - 2.0) < 0.01


@pytest.mark.parametrize("size", [(10000, 80), (80, 10000), (3100, 2100)])
def test_downscale_preserves_aspect_ratio_of_narrow_images(tmp_path, monkeypatch, size):
    source = tmp_path / "panorama.png"
    Image.new("RGB", size, "white").save(source)
    monkeypatch.setattr(processing.tempfile, "gettempdir", lambda: str(tmp_path))
    result = processing._resize_image_if_needed(source, max_pixels=500_000)
    with Image.open(result) as resized:
        assert resized.width * resized.height <= 500_000
        assert abs((resized.width / resized.height) / (size[0] / size[1]) - 1) < .02


def test_oversized_non_png_is_encoded_as_jpeg_once(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "photo.webp"
    Image.new("RGB", (2048, 1024), "navy").save(image_path, format="WEBP")
    monkeypatch.setattr(processing.tempfile, "gettempdir", lambda: str(tmp_path))

    result = processing._resize_image_if_needed(
        image_path,
        max_pixels=RESIZE_BUDGET,
        jpeg_quality=85,
    )

    assert result.suffix == ".jpg"
    with Image.open(result) as resized:
        assert resized.format == "JPEG"
        assert resized.width * resized.height <= RESIZE_BUDGET


def test_vision_message_uses_pixel_budget_and_direct_answer_prompt() -> None:
    messages = vision_tool._build_messages(
        "data:image/png;base64,aW1hZ2U=",
        "按钮上的文字是什么？",
        RESIZE_BUDGET,
    )

    content = messages[0]["content"]
    assert content[1]["image_url"]["max_pixels"] == RESIZE_BUDGET
    assert "直接给出结论" in content[0]["text"]
    assert "先客观描述图片" not in content[0]["text"]


@pytest.mark.parametrize("provider", ["qwen", "qwen-intl", "dashscope", "openai", "anthropic"])
def test_original_image_request_does_not_add_a_provider_resize_budget(provider):
    url = "data:image/png;base64,aW1hZ2U="
    messages = vision_tool._build_messages(url, "Inspect", None, provider)
    assert messages[0]["content"][1]["image_url"] == {"url": url}


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
        openai,
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


@pytest.mark.parametrize("value", [None, True, 0, -1, 65_535, 16_777_217, "1310720"])
def test_missing_or_invalid_resize_budget_keeps_original_resolution(value):
    assert resolve_max_pixels(SimpleNamespace(config={})) is None
    parent = SimpleNamespace(config={"auxiliary": {"vision": {"max_pixels": value}}})
    assert resolve_max_pixels(parent) is None
