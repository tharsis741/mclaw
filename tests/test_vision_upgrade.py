# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import copy
import ipaddress
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from mclaw.agent import models_dev
from mclaw.agent.context_compressor import ContextCompressor, estimate_messages_tokens
from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport
from mclaw.agent.transports.base import ModelCallError, ModelCallResult
from mclaw.agent.vision import mark_vision_images_seen, prepare_user_images, project_vision_messages, recover_image_rejection, tool_images
from mclaw.cli import config as config_io
from mclaw.providers import capabilities
from mclaw.providers.capabilities import capability_key, resolve_image_capability
from mclaw.providers.registry import get_runtime_profile
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.runtime.paths import PathPolicy
from mclaw.state import SessionDB
from mclaw.tools import vision_tool
from mclaw.tools.vision import assets, credentials, image_io, processing
from mclaw.tools.vision.config import resolve_mode, resolve_timeout, resolve_download_timeout
from mclaw.tools.vision.routing import choose_vision_route


def runtime(provider="openai", model="vision-test", endpoint="https://example.test/v1"):
    return ProviderRuntimeContext(get_runtime_profile(provider), model, "fixture-key", endpoint)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(capabilities, "_observations", {})
    monkeypatch.setattr(models_dev, "cached_models_dev", lambda: {})
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda *args, **kwargs: {})
    monkeypatch.setattr(models_dev, "cached_models_dev_status", lambda: {"updated_at": 123})
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "")
    policy = PathPolicy(mclaw_home=tmp_path / "home")
    monkeypatch.setattr("mclaw.tools.file_operations._runtime", lambda: SimpleNamespace(paths=policy))
    monkeypatch.setattr(processing.tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture
def agent(tmp_path):
    return SimpleNamespace(provider_runtime=runtime(), session_id="session-one", workspace_path=str(tmp_path),
                           config={"auxiliary": {"vision": {"mode": "auto"}}},
                           _session_db=None, _tool_call_ids_pending_visibility=set())


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "picture.png"
    Image.new("RGB", (96, 64), "blue").save(path)
    return path


@pytest.mark.parametrize("mode", ["main", "auto", "auxiliary"])
def test_explicit_vision_disablement_covers_attachments_tools_and_screenshots(agent, picture, monkeypatch, mode):
    from mclaw.tools.vision.types import VisionInputError

    agent.config["auxiliary"]["vision"]["mode"] = mode
    agent.config["tools"] = {"disabled": ["vision_analyze"]}
    monkeypatch.setattr(vision_tool, "_resize_image_if_needed", lambda *_a, **_k: pytest.fail("must not read image"))
    monkeypatch.setattr(credentials, "resolve_vision_credentials", lambda *_a, **_k: pytest.fail("must not resolve provider"))
    with pytest.raises(VisionInputError, match="已关闭") as error:
        prepare_user_images(agent, "Read image", [str(picture)], None)
    assert error.value.code == "VISION_DISABLED"
    for prepare_only in (False, True):
        result = json.loads(vision_tool.vision_analyze(str(picture), "Read", agent, _prepare_only=prepare_only))
        assert result["success"] is False and result["code"] == "VISION_DISABLED"
    assert tool_images(agent, "browser_screenshot", json.dumps({"success": True, "path": str(picture)})) == []


def test_disabling_vision_keeps_historical_analysis_but_rejects_unseen_images(agent, picture):
    from mclaw.tools.vision.types import VisionInputError

    _, images = prepare_user_images(agent, "Color?", [str(picture)], None)
    images[0].update(pending=False, analysis="The image is blue.")
    messages = [{"role": "user", "content": "Color?", "_vision_images": images}]
    agent.config["tools"] = {"disabled": ["vision_analyze"]}
    projected = project_vision_messages(agent, messages)
    assert "The image is blue." in json.dumps(projected)
    assert "data:image" not in json.dumps(projected)
    images[0]["pending"] = True
    with pytest.raises(VisionInputError, match="已关闭"):
        project_vision_messages(agent, messages)


def test_invalid_attachment_surfaces_an_actionable_input_error(agent, tmp_path):
    from mclaw.tools.vision.types import VisionInputError

    broken = tmp_path / "broken.png"
    broken.write_text("not an image")
    with pytest.raises(VisionInputError, match="图片读取失败"):
        prepare_user_images(agent, "Read", [str(broken)], None)
    with pytest.raises(VisionInputError, match="8 张图片") as error:
        prepare_user_images(agent, "Read", [str(broken)] * 9, None)
    assert error.value.code == "TOO_MANY_IMAGES"


@pytest.mark.parametrize("inputs,expected", [(["text", "image"], "supported"), (["text"], "unsupported"),
                                           (None, "unknown"), ([], "unknown"), ("image", "unknown"), ([None], "unknown")])
def test_catalog_uses_image_input_and_keeps_missing_data_unknown(inputs, expected):
    catalog = {"openai": {"models": {"vision-test": {"modalities": {"input": inputs, "output": ["image"]}, "attachment": True}}}}
    result = resolve_image_capability(runtime(), registry=catalog)
    assert result.state == expected
    assert resolve_image_capability(runtime(model="vision-test-other"), registry=catalog).state == "unknown"
    assert resolve_image_capability(runtime(provider="anthropic"), registry=catalog).state == "unknown"


def test_override_and_observation_are_bound_to_actual_target():
    first = runtime()
    second = replace(first, base_url="https://other.test/v1")
    config = {"model_capabilities": {capability_key(first): {"image_input": "enabled"}}}
    assert resolve_image_capability(first, config).state == "supported"
    assert resolve_image_capability(second, config).state == "unknown"
    capabilities.observe_image_rejection(first)
    assert resolve_image_capability(first, config).source == "runtime"
    assert resolve_image_capability(replace(first, api_key="another-account"), config).source == "config"
    capabilities.clear_image_observation(first)
    assert resolve_image_capability(first, config).state == "supported"


@pytest.mark.parametrize("message,status,expected", [
    ("model does not support image input", 400, True), ("only supports text", 422, True),
    ("image_url is too large", 400, False), ("invalid image format", 400, False),
    ("does not support image", 401, False), ("image input timeout", None, False),
    ("rate limit: image input", 429, False), ("content policy", 400, False),
])
def test_capability_rejection_classification(message, status, expected):
    assert capabilities.is_image_rejection(ModelCallError(message, "p", "m", status_code=status)) is expected


def test_native_image_uses_workspace_without_auxiliary_key_and_survives_original_deletion(agent, picture):
    result = json.loads(vision_tool.vision_analyze(picture.name, "What color?", agent))
    assert result["status"] == "image_ready"
    assert result["mode"] == "main"
    assert "base64" not in json.dumps(result)
    asset = result["images"][0]
    picture.unlink()
    url = assets.image_data_url(asset, agent.session_id)
    assert url.startswith("data:image/png;base64,")
    with pytest.raises(FileNotFoundError):
        assets.image_data_url(asset, "another-session")
    result2 = json.loads(vision_tool.vision_analyze("asset:" + asset["asset_id"], "Again?", agent))
    assert result2["images"][0]["asset_id"] == asset["asset_id"]


def test_native_path_policy_blocks_credentials(agent, tmp_path):
    path = tmp_path / ".env"
    Image.new("RGB", (10, 10)).save(path, format="PNG")
    result = json.loads(vision_tool.vision_analyze(str(path), "read", agent))
    assert result["success"] is False
    assert "credential" in result["error"]


@pytest.mark.parametrize("contents,suffix", [(b"\x89PNG\r\n\x1a\nnot-a-png", ".png"), (b"<svg></svg>", ".svg")])
def test_corrupt_and_unsupported_images_fail_closed(agent, tmp_path, contents, suffix):
    path = tmp_path / ("broken" + suffix)
    path.write_bytes(contents)
    result = json.loads(vision_tool.vision_analyze(str(path), "read", agent))
    assert result["success"] is False


def test_original_image_preserves_exif_and_orientation_is_applied_when_resizing(tmp_path):
    path = tmp_path / "rotated.jpg"
    source = Image.new("RGB", (80, 40), "red")
    exif = source.getexif()
    exif[274] = 6
    source.save(path, exif=exif)
    original = path.read_bytes()
    assert processing._resize_image_if_needed(path) == path
    assert path.read_bytes() == original
    normalized = processing._resize_image_if_needed(path, max_pixels=800)
    with Image.open(normalized) as result:
        assert result.size == (20, 40)
        assert result.getexif().get(274, 1) == 1


@pytest.mark.parametrize("mode", ["main", "auxiliary"])
@pytest.mark.parametrize("attachment", [False, True])
def test_tool_and_attachment_send_original_image_bytes_to_both_models(agent, tmp_path, monkeypatch, mode, attachment):
    source = tmp_path / "screenshot.png"
    Image.new("RGB", (3840, 2160), "navy").save(source)
    original = source.read_bytes()
    agent.config["auxiliary"]["vision"].update(mode=mode, provider="qwen")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    requests = []

    async def analyze(messages, *_args, **_kwargs):
        requests.append(messages)
        return "The image is navy."

    monkeypatch.setattr(vision_tool, "_call_vision_llm", analyze)
    if attachment:
        text, images = prepare_user_images(agent, "Inspect", [str(source)], threading.Event())
        projected = project_vision_messages(agent, [{"role": "user", "content": text, "_vision_images": images}])
    else:
        result = json.loads(vision_tool.vision_analyze(str(source), "Inspect", agent))
        assert result["success"] is True
        images = result["images"]
        projected = project_vision_messages(agent, [{"role": "user", "content": "Inspect", "_vision_images": images}])
    asset = images[0]
    assert (asset["width"], asset["height"]) == (3840, 2160)
    assert assets.image_path(asset, agent.session_id).read_bytes() == original
    wire = requests[0] if mode == "auxiliary" else projected
    part = next(part for message in wire for part in message["content"] if part["type"] == "image_url")
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == original
    assert "max_pixels" not in part["image_url"]
    assert len(requests) == (1 if mode == "auxiliary" else 0)


@pytest.mark.parametrize("budget", [None, 1_310_720, 2_000_000])
def test_resize_config_round_trip_preserves_explicit_choices(budget, monkeypatch):
    from mclaw.cli import main
    from mclaw.tools.vision.config import resolve_max_pixels

    config = {"auxiliary": {"vision": {"mode": "main", "max_pixels": budget}}}
    monkeypatch.setattr(main, "_setup_qwen_provider", lambda: "qwen")
    main._ensure_vision_config(config)
    config_io.save_config(config)
    loaded = config_io.load_config(strict=True)
    assert resolve_max_pixels(SimpleNamespace(config=loaded)) == budget
    assert loaded["auxiliary"]["vision"]["mode"] == "main"
    fresh = {}
    main._ensure_vision_config(fresh)
    assert fresh["auxiliary"]["vision"]["max_pixels"] is None
    assert config_io.DEFAULT_CONFIG["auxiliary"]["vision"]["max_pixels"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True, "bad"])
def test_timeouts_are_positive_finite_and_bounded(value):
    parent = SimpleNamespace(config={"auxiliary": {"vision": {"timeout": value, "download_timeout": value}}})
    assert resolve_timeout(parent) == 30
    assert resolve_download_timeout(parent) == 30


def test_tool_images_are_delivered_after_all_openai_tool_results(agent, picture):
    result = vision_tool.vision_analyze(str(picture), "What color?", agent)
    image_refs = tool_images(agent, "vision_analyze", result)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": result, "_vision_images": image_refs},
        {"role": "tool", "tool_call_id": "b", "content": "other result"},
    ]
    before = copy.deepcopy(messages)
    projected = project_vision_messages(agent, messages)
    assert [row["role"] for row in projected] == ["assistant", "tool", "tool", "user"]
    assert projected[-1]["content"][-1]["type"] == "image_url"
    assert messages == before
    assert tool_images(agent, "untrusted_remote_tool", result) == []


def test_anthropic_images_use_native_source_blocks_in_user_and_tool_results(agent, picture):
    asset = json.loads(vision_tool.vision_analyze(str(picture), "What color?", agent))["images"][0]
    agent.provider_runtime = runtime(provider="anthropic")
    messages = [{"role": "user", "content": "inspect", "_vision_images": [asset]},
                {"role": "tool", "tool_call_id": "a", "content": "ready", "_vision_images": [asset]}]
    _system, wire = AnthropicMessagesTransport._convert_messages(project_vision_messages(agent, messages))
    assert wire[0]["content"][-1]["source"]["media_type"] == "image/png"
    assert wire[1]["content"][0]["type"] == "tool_result"
    assert wire[1]["content"][0]["content"][-1]["type"] == "image"


@pytest.mark.parametrize("mode", ["main", "auxiliary"])
def test_legacy_attachment_prompt_is_refreshed_on_restore_without_rewriting_history(agent, picture, tmp_path, monkeypatch, mode):
    manifest = [{"name": picture.name, "path": str(picture), "source": "upload"}]
    old_text = (
        "What color?\n\n[Attached files]\nThe user attached these local files. "
        "Read the relevant files with available tools before using their contents. "
        "Uploaded files are copies; local references retain their original paths.\n"
        + json.dumps(manifest)
    )
    asset = assets.store_image(picture, agent.session_id, old_text)
    asset.update(pending=False, analysis="The image is blue.", analysis_provider="qwen", analysis_model="fixture")
    agent.config["auxiliary"]["vision"].update(mode=mode, provider="qwen", model="fixture")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    with closing(SessionDB(tmp_path / "history.db")) as db:
        db.create_session(agent.session_id, "test")
        db.append_message(agent.session_id, "user", content=old_text, image_parts=[asset])
        original = db.get_messages(agent.session_id)
        restored = db.get_messages_as_conversation(agent.session_id)
        before = copy.deepcopy(restored)
        projected = project_vision_messages(agent, restored)
        serialized = json.dumps(projected)
        assert "Read the relevant files" not in serialized
        assert "Uploaded files are copies" not in serialized
        assert serialized.count("[Attached files]") == 1
        assert ("This image is included below; inspect it directly." in serialized) is (mode == "main")
        assert ("data:image/png;base64," in serialized) is (mode == "main")
        assert ("The image is blue." in serialized) is (mode == "auxiliary")
        assert restored == before
        assert db.get_messages(agent.session_id) == original


@pytest.mark.parametrize("with_image", [False, True])
def test_legacy_file_only_attachment_prompt_is_refreshed_with_or_without_image_history(agent, picture, with_image):
    from mclaw.application.files import attachment_context

    manifest = [{"name": "notes.txt", "path": "/work/notes.txt", "source": "upload"}]
    old_text = (
        "Use these notes\n\n[Attached files]\nThe user attached these local files. "
        "Read the relevant files with available tools before using their contents. "
        "Uploaded files are copies; local references retain their original paths.\n"
        + json.dumps(manifest)
    )
    messages = [{"role": "user", "content": old_text}, {"role": "assistant", "content": old_text}]
    if with_image:
        asset = assets.store_image(picture, agent.session_id, "Inspect")
        messages.append({"role": "user", "content": "Inspect", "_vision_images": [asset]})
    before = copy.deepcopy(messages)
    projected = project_vision_messages(agent if with_image else None, messages)
    assert projected[0]["content"] == "Use these notes" + attachment_context(manifest)
    assert projected[1]["content"] == old_text
    assert messages == before


def test_new_images_use_the_user_question_without_repeating_the_attachment_manifest(agent, picture):
    from mclaw.application.files import attachment_context

    text = "What color?" + attachment_context([
        {"name": picture.name, "path": str(picture), "source": "upload", "mime_type": "image/png"},
        {"name": "notes.txt", "path": "/work/notes.txt", "source": "local", "mime_type": "text/plain"},
    ])
    content, images = prepare_user_images(agent, text, [str(picture)], threading.Event())
    assert content == text
    assert images[0]["question"] == "What color?"
    projected = project_vision_messages(agent, [{"role": "user", "content": content, "_vision_images": images}])
    assert json.dumps(projected).count("[Attached files]") == 1
    assert projected[0]["content"][-1]["type"] == "image_url"


def test_empty_auxiliary_result_is_failure_and_cancellation_skips_retry(agent, picture, monkeypatch):
    agent.config["auxiliary"]["vision"].update(mode="auxiliary", provider="qwen")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    calls = []
    async def empty(*args, **kwargs):
        calls.append(True)
        return "   "
    monkeypatch.setattr(vision_tool, "_call_vision_llm", empty)
    result = json.loads(vision_tool.vision_analyze(str(picture), "read", agent))
    assert not result["success"] and result["code"] == "EMPTY_VISION_RESPONSE"
    assert len(calls) == 2
    cancel = threading.Event()
    async def interrupted(*args, **kwargs):
        calls.append(True)
        cancel.set()
        return ""
    monkeypatch.setattr(vision_tool, "_call_vision_llm", interrupted)
    result = json.loads(vision_tool.vision_analyze(str(picture), "read", agent, cancel_event=cancel))
    assert result["status"] == "cancelled"
    assert len(calls) == 3


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "qwen-intl"])
def test_independent_provider_uses_declared_scoped_credentials(provider, monkeypatch):
    profile = get_runtime_profile(provider)
    selected = profile.env_vars[0]
    monkeypatch.setattr(credentials, "authorized_env_value", lambda name: "fixture" if name == selected else "")
    config = {"auxiliary": {"vision": {"mode": "auxiliary", "provider": provider, "model": "vision-test", "credential_env": selected}}}
    resolved = credentials.resolve_vision_credentials(config=config)
    assert resolved.available
    assert resolved.provider == provider
    assert resolved.runtime.api_mode == profile.api_mode
    assert resolved.env_var == selected


def test_custom_local_provider_does_not_require_key_and_catalog_alias_is_supported():
    config = {"auxiliary": {"vision": {"mode": "auxiliary", "provider": "custom", "model": "local-alias", "base_url": "http://127.0.0.1:8080/v1"}}}
    assert credentials.resolve_vision_credentials(config=config).available
    catalog = {"openai": {"models": {"catalog-name": {"modalities": {"input": ["text", "image"]}}}}}
    assert resolve_image_capability(runtime(), registry=catalog, catalog_provider="openai", catalog_model="catalog-name").state == "supported"


def test_auxiliary_user_images_are_analyzed_together(agent, picture, monkeypatch):
    agent.config["auxiliary"]["vision"].update(mode="auxiliary", provider="qwen")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    calls = []
    async def analyze(messages, *_args, **_kwargs):
        calls.append(messages)
        return "The two images match."
    monkeypatch.setattr(vision_tool, "_call_vision_llm", analyze)
    text, images = prepare_user_images(agent, "Compare both", [str(picture), str(picture)], threading.Event())
    assert text == "Compare both" and len(images) == 2 and not calls
    messages = [{"role": "user", "content": text, "_vision_images": images}]
    assert "The two images match" in project_vision_messages(agent, messages)[0]["content"]
    assert "The two images match" in project_vision_messages(agent, messages)[0]["content"]
    assert len(calls) == 1
    assert sum(part["type"] == "image_url" for part in calls[0][0]["content"]) == 2


def test_main_mode_never_falls_back_and_auto_only_falls_back_on_explicit_rejection(agent, picture, monkeypatch):
    asset = json.loads(vision_tool.vision_analyze(str(picture), "read", agent))["images"][0]
    messages = project_vision_messages(agent, [{"role": "user", "content": "read", "_vision_images": [asset]}])
    assert not recover_image_rejection(agent, messages, TimeoutError("timeout"))
    agent.config["auxiliary"]["vision"]["mode"] = "main"
    error = ModelCallError("model does not support image input", "p", "m", status_code=400)
    assert not recover_image_rejection(agent, messages, error)
    agent.config["auxiliary"]["vision"]["mode"] = "auto"
    assert not recover_image_rejection(agent, messages, error)
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    assert recover_image_rejection(agent, messages, error)


def test_text_only_image_analysis_error_does_not_poison_current_model_capability(agent):
    error = ModelCallError("model does not support image input", "p", "m", status_code=400)
    messages = [{"role": "user", "content": "Previous analysis: image input is unsupported", "_vision_images": [{"asset_id": "history"}]}]
    assert not recover_image_rejection(agent, messages, error)
    assert resolve_image_capability(agent.provider_runtime).state == "unknown"


def test_auxiliary_attachment_survives_resume_and_can_switch_to_current_model(agent, picture, tmp_path, monkeypatch):
    agent.config["auxiliary"]["vision"].update(mode="auxiliary", provider="qwen")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    calls = []
    async def analyze(*args, **kwargs):
        calls.append(True)
        return "Blue image."
    monkeypatch.setattr(vision_tool, "_call_vision_llm", analyze)
    text, images = prepare_user_images(agent, "Inspect", [str(picture)], threading.Event())
    with closing(SessionDB(tmp_path / "history.db")) as db:
        db.create_session(agent.session_id, "test")
        agent._session_db = db
        db.append_message(agent.session_id, "user", content=text, image_parts=images)
        history = db.get_messages_as_conversation(agent.session_id)
        assert "Blue image" in project_vision_messages(agent, history)[0]["content"]
        mark_vision_images_seen(agent, history)
        picture.unlink()
        restored = db.get_messages_as_conversation(agent.session_id)
        assert restored[0]["content"] == "Inspect"
        assert restored[0]["_vision_images"][0]["pending"] is False
        assert "Blue image" in project_vision_messages(agent, restored)[0]["content"]
        assert len(calls) == 1
        agent.config["auxiliary"]["vision"]["mode"] = "main"
        assert project_vision_messages(agent, restored)[0]["content"][-1]["type"] == "image_url"


def test_auxiliary_tool_retains_reopenable_image_reference(agent, picture, monkeypatch):
    agent.config["auxiliary"]["vision"].update(mode="auxiliary", provider="qwen")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    async def analyze(*args, **kwargs):
        return "Blue image."
    monkeypatch.setattr(vision_tool, "_call_vision_llm", analyze)
    result = vision_tool.vision_analyze(str(picture), "Color?", agent)
    refs = tool_images(agent, "vision_analyze", result)
    assert refs[0]["analysis"] == "Blue image."
    picture.unlink()
    assert assets.image_data_url(refs[0], agent.session_id).startswith("data:image/png")


def test_new_images_across_multiple_user_messages_cannot_be_silently_dropped(agent, picture):
    _, images = prepare_user_images(agent, "Compare", [str(picture)] * 8, threading.Event())
    _, supplement = prepare_user_images(agent, "Also this", [str(picture)], threading.Event())
    messages = [{"role": "user", "content": "Compare", "_vision_images": images},
                {"role": "user", "content": "Also this", "_vision_images": supplement}]
    with pytest.raises(ValueError, match="Too many new images"):
        project_vision_messages(agent, messages)
    mark_vision_images_seen(agent, messages[:1])
    projected = project_vision_messages(agent, messages)
    assert sum(part["type"] == "image_url" for row in projected for part in row["content"]) == 8


def test_missing_old_image_does_not_block_text_but_new_image_fails(agent, picture):
    _, images = prepare_user_images(agent, "Inspect", [str(picture)], threading.Event())
    messages = [{"role": "user", "content": "Inspect", "_vision_images": images}]
    assets.image_path(images[0], agent.session_id).unlink()
    with pytest.raises(ValueError, match="new image asset"):
        project_vision_messages(agent, messages)
    images[0]["pending"] = False
    messages.extend([{"role": "assistant", "content": "Blue"}, {"role": "user", "content": "Hello"}])
    projected = project_vision_messages(agent, messages)
    assert "Earlier image file unavailable" in json.dumps(projected)
    assert projected[-1]["content"] == "Hello"


def test_text_model_can_resume_seen_images_without_failing_new_text(agent, picture):
    _, images = prepare_user_images(agent, "Inspect", [str(picture)], threading.Event())
    images[0]["pending"] = False
    agent.config["auxiliary"]["vision"]["mode"] = "main"
    agent.config["model_capabilities"] = {capability_key(agent.provider_runtime): {"image_input": "disabled"}}
    messages = [{"role": "user", "content": "Inspect", "_vision_images": images},
                {"role": "assistant", "content": "Blue"}, {"role": "user", "content": "Hello"}]
    assert "Image not supplied" in json.dumps(project_vision_messages(agent, messages))
    images[0]["pending"] = True
    with pytest.raises(ValueError, match="disabled"):
        project_vision_messages(agent, messages)


def test_auxiliary_model_name_never_inherits_current_text_model(monkeypatch):
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    config = {"model": "text-only", "auxiliary": {"vision": {"mode": "auxiliary", "provider": "openai"}}}
    resolved = credentials.resolve_vision_credentials(config=config)
    assert not resolved.available and not resolved.model
    assert "model name" in resolved.unsupported_reason


def test_diagnosis_uses_same_unknown_model_route_as_execution(agent, monkeypatch):
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    monkeypatch.setattr(credentials, "resolve_provider_runtime_context", lambda **kwargs: agent.provider_runtime if "provider" not in kwargs else runtime("qwen"))
    native, _ = choose_vision_route(agent)
    assert not native
    assert credentials.diagnose_vision_credentials(agent.config)["route"] == "auxiliary"


def test_explicit_dedicated_capability_override_beats_shared_declaration():
    context = runtime()
    config = {"model_capabilities": {capability_key(context): {"image_input": "enabled"}}}
    assert resolve_image_capability(context, config, image_input="disabled").state == "unsupported"
    config["model_capabilities"][capability_key(context)] = {"catalog_provider": ["invalid"]}
    assert resolve_image_capability(context, config).state == "unknown"


def test_image_supplement_is_persisted_and_replayed_once_at_model_boundary(agent, picture, tmp_path, monkeypatch):
    from mclaw.application.files import FileService
    from mclaw.application.worker import SessionWorker

    home = tmp_path / "home"
    monkeypatch.setattr(FileService, "_policy", lambda _self: PathPolicy(mclaw_home=home))
    files = FileService(home)
    attachment = files.upload(agent.session_id, picture.name, picture.read_bytes())
    item = {"id": "addition", "content": "Also inspect", "attachments": [attachment]}
    events = []
    worker = SessionWorker(home, {"id": agent.session_id, "workspace": agent.workspace_path}, events.append)
    worker.agent = agent
    with closing(SessionDB(home / "state.db")) as db:
        db.create_session(agent.session_id, "test")
        worker.db = db
        agent._session_db = db
        worker._supplements = {item["id"]: item}
        messages = []
        worker.cancel.set()
        with pytest.raises(InterruptedError):
            worker._apply_supplements(messages, "turn-one")
        assert not messages and not db.get_messages(agent.session_id) and not events
        assert item["id"] in worker._supplements
        worker.cancel.clear()
        worker._apply_supplements(messages, "turn-one")
        assert messages[0]["_vision_images"]
        assert "already supplied in this conversation directly" in messages[0]["content"]
        assert messages[0]["_vision_images"][0]["question"] == "Also inspect"
        assert project_vision_messages(agent, messages)[0]["content"][-1]["type"] == "image_url"
        assert db.get_messages_as_conversation(agent.session_id)[0]["_vision_images"]
        Path(attachment["path"]).unlink()
        worker._supplements = {item["id"]: item}
        worker._apply_supplements(messages, "turn-one")
        assert len(db.get_messages(agent.session_id)) == 1
        assert all(event["type"] == "supplement_applied" for event in events)


def test_tui_queue_handoff_prepares_new_images_but_resume_uses_history(agent, picture, tmp_path, monkeypatch):
    from mclaw.application.files import FileService
    from mclaw.cli.app import InteractiveChat

    home = tmp_path / "home"
    monkeypatch.setattr(FileService, "_policy", lambda _self: PathPolicy(mclaw_home=home))
    attachment = FileService(home).upload(agent.session_id, picture.name, picture.read_bytes())
    ui = InteractiveChat.__new__(InteractiveChat)
    ui.session_id = agent.session_id
    with closing(SessionDB(home / "state.db")) as db:
        db.create_session(agent.session_id, "test")
        ui._session_db = db
        task = {"id": "work", "content": "Inspect", "attachments": [attachment]}
        assert "Inspect" in ui._prepare_task_input(task)
        assert "already supplied in this conversation directly" in ui._prepare_task_input(task)
        assert len(ui._task_image_paths) == 1 and Path(ui._task_image_paths[0]).is_file()
        db.append_message(agent.session_id, "user", content="Inspect", operation_id="task:work")
        Path(attachment["path"]).unlink()
        assert "继续处理" in ui._prepare_task_input(task)
        assert not ui._task_image_paths


def test_old_image_budget_cannot_crowd_out_new_attachment(agent, monkeypatch):
    import mclaw.agent.vision as projection

    monkeypatch.setattr(projection, "image_data_url", lambda asset, _session: asset["asset_id"] * (18 * 1024 * 1024))
    messages = [{"role": "user", "content": "old", "_vision_images": [{"asset_id": "a", "pending": False}]},
                {"role": "assistant", "content": "Seen"},
                {"role": "user", "content": "new", "_vision_images": [{"asset_id": "b", "pending": True}]}]
    projected = project_vision_messages(agent, messages)
    assert "omitted to fit" in projected[0]["content"][1]["text"]
    assert projected[-1]["content"][-1]["image_url"]["url"].startswith("b")
    messages[0]["_vision_images"][0]["pending"] = True
    with pytest.raises(ValueError, match="Combined new image"):
        project_vision_messages(agent, messages)


def test_compression_preserves_unseen_images_before_later_user_supplements(agent, picture, monkeypatch):
    compressor = ContextCompressor(agent.provider_runtime, 20_000, protect_first_n=1, quiet_mode=True)
    _, refs = prepare_user_images(agent, "Read", [str(picture)], threading.Event())
    messages = [{"role": "system", "content": "System"},
                {"role": "user", "content": "Read", "_vision_images": refs},
                *[{"role": "user", "content": "Additional details"} for _ in range(12)]]
    monkeypatch.setattr(compressor, "_find_tail_cut_by_tokens", lambda *_args: len(messages) - 2)
    monkeypatch.setattr(compressor, "_generate_summary", lambda *_args: pytest.fail("Unseen images cannot be summarized away"))
    assert compressor.compress(messages) == messages
    compressor.close()


def test_anthropic_base64_error_payload_is_redacted():
    from mclaw.agent.transports.base import _redact_error_message

    data = "aW1hZ2U=" * 64
    payload = json.dumps({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    assert data not in _redact_error_message(payload, "")
    assert "<redacted>" in _redact_error_message(payload, "")


def test_history_migration_backup_and_image_resume(agent, picture, tmp_path):
    database = tmp_path / "state.db"
    db = SessionDB(database)
    db.create_session(agent.session_id, "test")
    db.append_message(agent.session_id, "user", content="old text")
    db.close()
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE messages DROP COLUMN image_parts")
        connection.execute("UPDATE schema_version SET version=9")
    backup = tmp_path / "state.backup.db"
    # SQLite may still have a WAL: a plain file copy is not a database backup.
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    db = SessionDB(database)
    assert db.get_messages_as_conversation(agent.session_id)[0]["content"] == "old text"
    asset = json.loads(vision_tool.vision_analyze(str(picture), "read", agent))["images"][0]
    identifier = db.append_message(agent.session_id, "user", content="new image", image_parts=[asset])
    db.close()
    picture.unlink()
    db = SessionDB(database)
    history = db.get_messages_as_conversation(agent.session_id)
    assert history[-1]["_message_id"] == identifier
    assert project_vision_messages(agent, history)[-1]["content"][-1]["type"] == "image_url"
    db.update_message_images("wrong-session", identifier, [])
    assert db.get_messages_as_conversation(agent.session_id)[-1]["_vision_images"]
    db.close()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 9
        assert connection.execute("SELECT content FROM messages").fetchone()[0] == "old text"
    restored = SessionDB(backup)
    assert [row["content"] for row in restored.get_messages_as_conversation(agent.session_id)] == ["old text"]
    restored.close()


def test_image_pressure_is_counted_and_confirmed_images_can_be_pruned(agent, picture):
    asset = json.loads(vision_tool.vision_analyze(str(picture), "read", agent))["images"][0]
    messages = [{"role": "tool", "tool_call_id": "a", "content": "ready", "_vision_images": [asset]}]
    assert estimate_messages_tokens(messages) > 2048
    compressor = object.__new__(ContextCompressor)
    kept, count, _ = compressor.prune_confirmed_tool_results(messages, tokens_to_save=100, protected_tool_call_ids={"a"})
    assert count == 0 and kept[0]["_vision_images"]
    pruned, count, _ = compressor.prune_confirmed_tool_results(messages, tokens_to_save=100)
    assert count == 1 and "_vision_images" not in pruned[0]


def test_legacy_vision_config_preserves_mode_without_writing_user_data(tmp_path):
    config_io.save_config({"auxiliary": {"vision": {"provider": "qwen-intl", "model": "keep-model"}}})
    path = config_io.get_config_path()
    before = path.read_bytes()
    assert resolve_mode(config=config_io.load_config(strict=True)) == "auxiliary"
    assert path.read_bytes() == before
    assert resolve_mode(config=config_io.DEFAULT_CONFIG) == "auto"


def test_project_override_preserves_an_explicit_user_vision_mode(tmp_path):
    project = {"auxiliary": {"vision": {"model": "project-vision"}}}
    user = {"auxiliary": {"vision": {"mode": "auto", "provider": "qwen"}}}
    source = config_io.ConfigSourceSnapshot(user, project, tmp_path / ".mclaw.yaml")
    merged = config_io.load_merged_config(source_snapshot=source)
    assert merged["auxiliary"]["vision"]["mode"] == "auto"
    assert merged["auxiliary"]["vision"]["model"] == "project-vision"
    legacy = config_io.ConfigSourceSnapshot({}, project, tmp_path / ".mclaw.yaml")
    assert config_io.load_merged_config(source_snapshot=legacy)["auxiliary"]["vision"]["mode"] == "auxiliary"


def test_pinned_download_transport_uses_validated_ip_and_original_tls_name(monkeypatch):
    seen = []
    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(request)
            return httpx.Response(200, content=b"image")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: Inner())
    monkeypatch.setattr(image_io, "_resolve_host_ips", lambda _host: [ipaddress.ip_address("1.1.1.1")])
    async def run():
        async with image_io._PinnedImageTransport() as transport:
            return await transport.handle_async_request(httpx.Request("GET", "https://images.example.test/photo.png"))
    assert asyncio.run(run()).status_code == 200
    assert seen[0].url.host == "1.1.1.1"
    assert seen[0].headers["host"] == "images.example.test"
    assert seen[0].extensions["sni_hostname"] == "images.example.test"
    monkeypatch.setattr(image_io, "_resolve_host_ips", lambda _host: [ipaddress.ip_address("127.0.0.1")])
    with pytest.raises(ValueError, match="private"):
        asyncio.run(run())
    assert len(seen) == 1


def test_native_tool_end_to_end_reaches_next_main_request_and_persists(agent, picture, monkeypatch, tmp_path):
    from mclaw.agent.core import MClaw

    calls = []
    class Transport:
        def call(self, **kwargs):
            calls.append(copy.deepcopy(kwargs["messages"]))
            tool_calls = [{"id": "vision-call", "type": "function", "function": {"name": "vision_analyze", "arguments": json.dumps({"image_url": str(picture), "question": "What color?"})}}] if len(calls) == 1 else None
            return ModelCallResult("" if tool_calls else "blue", tool_calls, "tool_calls" if tool_calls else "stop", None, None, False, "openai", "vision-test")
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda context: Transport())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    db = SessionDB(tmp_path / "state.db")
    real_agent = MClaw(provider_runtime=agent.provider_runtime, session_db=db, session_id=agent.session_id,
                       workspace=agent.workspace_path, system_prompt="system", skip_memory=True,
                       config={**agent.config, "compression": {"enabled": False}, "prompt_cache": {"enabled": False}})
    real_agent.valid_tool_names = {"vision_analyze"}
    real_agent.tools = [vision_tool.VISION_ANALYZE_SCHEMA]
    result = real_agent.run_conversation("Inspect the picture", advance_background_review=False)
    assert result["final_response"] == "blue"
    assert len(calls) == 2
    assert calls[1][-1]["role"] == "user"
    assert calls[1][-1]["content"][-1]["type"] == "image_url"
    assert any(row.get("_vision_images") for row in db.get_messages_as_conversation(agent.session_id))
    assert "base64" not in json.dumps(db.get_messages(agent.session_id))
    real_agent.close()
    db.close()


@pytest.mark.parametrize("mode,status,falls_back", [("auto", 400, True), ("main", 400, False), ("auto", 401, False), ("auto", 429, False)])
def test_main_rejection_fallback_is_once_and_never_reexecutes_tools(agent, picture, monkeypatch, tmp_path, mode, status, falls_back):
    from mclaw.agent.core import MClaw

    agent.config["auxiliary"]["vision"].update(mode=mode, provider="qwen")
    agent.config["model_capabilities"] = {capability_key(agent.provider_runtime): {"image_input": "enabled"}}
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    calls, auxiliary_calls = [], []
    async def analyze(messages, *_args, **_kwargs):
        auxiliary_calls.append(messages)
        return "The picture is blue."
    monkeypatch.setattr(vision_tool, "_call_vision_llm", analyze)
    class Transport:
        def call(self, **kwargs):
            calls.append(copy.deepcopy(kwargs["messages"]))
            if len(calls) == 1:
                tool_call = {"id": "read-image", "type": "function", "function": {"name": "vision_analyze", "arguments": json.dumps({"image_url": str(picture), "question": "What color?"})}}
                return ModelCallResult("", [tool_call], "tool_calls", None, None, False, "openai", "vision-test")
            if len(calls) == 2:
                raise ModelCallError("model does not support image input", "openai", "vision-test", status_code=status, retryable=False)
            assert all(isinstance(row.get("content") or "", str) for row in kwargs["messages"])
            assert "data:image" not in json.dumps(kwargs["messages"])
            assert "image_ready" not in json.dumps(kwargs["messages"])
            assert "The picture is blue" in json.dumps(kwargs["messages"])
            return ModelCallResult("blue", None, "stop", None, None, False, "openai", "vision-test")
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda context: Transport())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    db = SessionDB(tmp_path / "state.db")
    real_agent = MClaw(provider_runtime=agent.provider_runtime, session_db=db, session_id=agent.session_id,
                       workspace=agent.workspace_path, system_prompt="system", skip_memory=True,
                       config={**agent.config, "compression": {"enabled": False}, "prompt_cache": {"enabled": False}})
    real_agent.valid_tool_names = {"vision_analyze"}
    real_agent.tools = [vision_tool.VISION_ANALYZE_SCHEMA]
    result = real_agent.run_conversation("Inspect the picture", advance_background_review=False)
    assert len(calls) == (3 if falls_back else 2)
    assert len(auxiliary_calls) == int(falls_back)
    history = db.get_messages_as_conversation(agent.session_id)
    assert sum(row["role"] == "tool" for row in history) == 1
    if falls_back:
        assert result["final_response"] == "blue"
        # Replaying a persisted fallback must not bill another auxiliary call.
        restored = project_vision_messages(real_agent, history)
        assert "data:image" not in json.dumps(restored)
        assert len(auxiliary_calls) == 1
    real_agent.close()
    db.close()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("content", ["blue", ""])
def test_independent_provider_wire_protocol_and_usage(provider, content, monkeypatch):
    import openai
    import anthropic
    from mclaw.tools.vision.client import call_vision_llm

    seen = []
    async def respond(request):
        seen.append(json.loads(request.content))
        if provider == "anthropic":
            body = {"id": "test", "type": "message", "role": "assistant", "model": "vision-test", "content": [{"type": "thinking", "thinking": "internal reasoning", "signature": "fixture"}, {"type": "text", "text": content}], "stop_reason": "end_turn", "usage": {"input_tokens": 17, "output_tokens": 2}}
        else:
            body = {"id": "test", "object": "chat.completion", "created": 1, "model": "vision-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "reasoning_content": "internal reasoning"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 17, "completion_tokens": 2, "total_tokens": 19}}
        return httpx.Response(200, json=body)
    sdk = anthropic if provider == "anthropic" else openai
    name = "AsyncAnthropic" if provider == "anthropic" else "AsyncOpenAI"
    original = getattr(sdk, name)
    def build(**kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        return original(**kwargs)
    monkeypatch.setattr(sdk, name, build)
    context = runtime(provider=provider)
    result = asyncio.run(call_vision_llm(vision_tool._build_messages("data:image/png;base64,aW1hZ2U=", "Color?", 1000, provider),
                                        context.model, context.api_key, context.base_url, 1, provider, context))
    assert result == content and "internal reasoning" not in result
    assert result.usage.input_tokens == 17 and result.usage.source == "vision"
    part = seen[0]["messages"][0]["content"][-1]
    assert part["type"] == ("image" if provider == "anthropic" else "image_url")
    assert "max_pixels" not in json.dumps(seen)


def test_auxiliary_usage_is_recorded(agent, picture, monkeypatch):
    from mclaw.agent.usage import UsageRecord
    from mclaw.tools.vision.client import VisionAnalysis

    agent.config["auxiliary"]["vision"].update(mode="auxiliary", provider="qwen")
    monkeypatch.setattr(credentials, "authorized_env_value", lambda _name: "fixture")
    records = []
    agent._record_usage = records.append
    record = UsageRecord(provider="qwen", model="m", input_tokens=17, source="vision")
    async def analyze(*_args, **_kwargs):
        return VisionAnalysis("blue", record)
    monkeypatch.setattr(vision_tool, "_call_vision_llm", analyze)
    assert json.loads(vision_tool.vision_analyze(str(picture), "Color?", agent))["analysis"] == "blue"
    assert records == [record]


def test_tampered_asset_is_rejected_and_many_images_are_bounded(agent, picture):
    asset = json.loads(vision_tool.vision_analyze(str(picture), "read", agent))["images"][0]
    assets.image_path(asset, agent.session_id).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        assets.image_data_url(asset, agent.session_id)
    with pytest.raises(ValueError, match="8 张图片"):
        prepare_user_images(agent, "read", [str(picture)] * 9, threading.Event())


def test_setup_vision_features_follow_the_selected_mode_and_provider():
    from mclaw.runtime.features import get_feature, is_feature_configured, secret_requests_for_feature

    main = get_feature("vision_analyze", config={"auxiliary": {"vision": {"mode": "main"}}})
    assert is_feature_configured(main, lambda _: "")
    assert secret_requests_for_feature(main, lambda _: "") == []
    independent = get_feature("vision_analyze", config={"auxiliary": {"vision": {"mode": "auxiliary", "provider": "anthropic", "model": "fixture"}}})
    assert independent.requires_any == (get_runtime_profile("anthropic").env_vars,)


def test_delete_session_removes_only_its_live_image_assets(agent, picture, tmp_path):
    from mclaw.constants import get_mclaw_home

    asset = assets.store_image(picture, agent.session_id, "read")
    other = assets.store_image(picture, "other-session", "read")
    with_db = SessionDB(get_mclaw_home() / "state.db")
    with_db.create_session(agent.session_id, "test")
    backup_db = SessionDB(tmp_path / "export.db")
    backup_db.create_session(agent.session_id, "test")
    assert backup_db.delete_session(agent.session_id)
    assert assets.image_path(asset, agent.session_id).exists()
    backup_db.close()
    assert with_db.delete_session(agent.session_id)
    assert not assets.image_path(asset, agent.session_id).exists()
    assert assets.image_path(other, "other-session").exists()
    with_db.close()


def test_provider_error_redacts_echoed_image_data_and_api_key():
    from mclaw.agent.transports.base import _redact_error_message

    result = _redact_error_message('Invalid image data:image/png;base64,cHJpdmF0ZQ==, key=fixture-secret', 'fixture-secret')
    assert 'cHJpdmF0ZQ==' not in result and 'fixture-secret' not in result


def test_resize_failure_removes_partial_temporary_file(tmp_path, monkeypatch):
    source = tmp_path / "source.bmp"
    Image.new("RGB", (60, 60)).save(source)
    def broken_save(self, filename, *_args, **_kwargs):
        Path(filename).write_bytes(b"partial")
        raise OSError("disk write failed")
    monkeypatch.setattr(Image.Image, "save", broken_save)
    with pytest.raises(ValueError, match="disk write failed"):
        processing._resize_image_if_needed(source)
    assert list((tmp_path / "mclaw-vision").iterdir()) == []
