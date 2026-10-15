# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser recording adapter using the existing Qwen credential and ASR code."""

from __future__ import annotations

import asyncio
import copy
import inspect
import tempfile
from pathlib import Path

from mclaw.application.sessions import ApplicationError
from mclaw.system.lock import file_lock

MAX_AUDIO_BYTES = 7 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
MIME_TYPES = ["audio/webm", "audio/ogg", "audio/wav"]


class VoiceService:
    def __init__(self, home: Path, *, transcriber_factory=None) -> None:
        self.home = Path(home).resolve()
        self.transcriber_factory = transcriber_factory
        self._limit = asyncio.Semaphore(2)
        self._active: set[asyncio.Task] = set()
        self._closing = False

    def _configuration(self) -> tuple[dict, dict, object]:
        from mclaw.cli.config import ConfigError, load_config
        from mclaw.voice.config import _qwen_profile, resolve_inbound_audio_config
        import yaml

        try:
            config = load_config(strict=True)
            raw_path = self.home / "config.yaml"
            raw = yaml.safe_load(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
            raw_asr = ((raw or {}).get("auxiliary") or {}).get("asr") or {}
            asr = (config.get("auxiliary") or {}).get("asr") or {}
        except (ConfigError, OSError, AttributeError, TypeError, yaml.YAMLError) as exc:
            raise ApplicationError("CONFIG_INVALID", "语音配置无法读取，请检查设置", 409) from exc
        profile = _qwen_profile(asr, region_explicit="region" in raw_asr)
        # Complete-file ASR shares the microphone's region and credentials, but
        # must not depend on the backend machine's sound card or IM audio switch.
        temporary = copy.deepcopy(config)
        inbound = temporary.setdefault("capabilities", {}).setdefault("inbound_audio", {})
        if inbound.get("provider") != profile.name:
            inbound["base_url"] = ""
        inbound.update(provider=profile.name, enabled=True)
        resolved = resolve_inbound_audio_config(config=temporary)
        resolved["browser_enabled"] = str(asr.get("enabled", "auto")).lower() not in {"false", "off", "0"}
        return config, resolved, profile

    def status(self) -> dict:
        from mclaw.cli.config import get_env_value

        _config, resolved, profile = self._configuration()
        configured = any(get_env_value(key) for key in profile.env_vars)
        return {"enabled": resolved["browser_enabled"],
                "ready": bool(resolved["browser_enabled"] and resolved["api_key"] and not resolved["configuration_error"]),
                "credential_configured": configured, "authorized": bool(resolved["api_key"]),
                "provider": profile.name, "region": "intl" if profile.name == "qwen-intl" else "cn",
                "model": resolved["model"], "max_seconds": MAX_AUDIO_SECONDS, "max_bytes": MAX_AUDIO_BYTES,
                "mime_types": MIME_TYPES, "notice": "录音将发送至配置的 Qwen 服务进行转写。"}

    def configure(self, data: dict) -> dict:
        from mclaw.application.setup import _WORKFLOW_LOCK
        from mclaw.cli.config import get_env_value, save_config, save_env_value
        from mclaw.constants import get_mclaw_home
        from mclaw.providers.registry import get_runtime_profile
        from mclaw.runtime.secrets import authorize

        if get_mclaw_home().resolve() != self.home:
            raise ApplicationError("CONFIG_ROOT_MISMATCH", "配置目录与本机服务不一致", 409)
        if not isinstance(data.get("enabled"), bool):
            raise ApplicationError("INVALID_VOICE_CONFIG", "请选择是否启用语音输入")
        provider = data.get("provider") or self.status()["provider"]
        if provider not in {"qwen", "qwen-intl"}:
            raise ApplicationError("INVALID_VOICE_PROVIDER", "请选择 Qwen 服务区域")
        key = data.get("api_key", "")
        if not isinstance(key, str) or len(key) > 8192:
            raise ApplicationError("INVALID_VOICE_KEY", "凭据格式错误")
        if not _WORKFLOW_LOCK.acquire(blocking=False):
            raise ApplicationError("SETUP_IN_PROGRESS", "请先完成或退出正在进行的配置向导", 409)
        try:
            profile = get_runtime_profile(provider)
            with file_lock(self.home / "config.lock"):
                config, _resolved, _profile = self._configuration()
                if key.strip():
                    save_env_value(profile.env_vars[0], key.strip())
                names = [name for name in profile.env_vars if get_env_value(name)]
                if data["enabled"] and not names:
                    raise ApplicationError("VOICE_KEY_REQUIRED", "请先填写 Qwen API Key", 422)
                if data["enabled"]:
                    authorize("runtime:asr", names)
                asr = config.setdefault("auxiliary", {}).setdefault("asr", {})
                asr.update(enabled=data["enabled"], provider=provider,
                           region="intl" if provider == "qwen-intl" else "cn")
                # A region change must not leave a realtime endpoint in the old region.
                asr["websocket_url"] = ""
                save_config(config)
        finally:
            _WORKFLOW_LOCK.release()
        return self.status()

    async def transcribe(self, audio: bytes, mime_type: str, duration_ms: int) -> dict:
        from mclaw.voice.audio_probe import AudioProbeError, probe_audio
        from mclaw.voice.providers.qwen_file import QwenFileTranscriber
        from mclaw.voice.transcription import AudioTranscriptionError, TranscriptionRequest

        if self._closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        mime_type = mime_type.partition(";")[0].lower().strip()
        if mime_type not in MIME_TYPES:
            raise ApplicationError("AUDIO_FORMAT_UNSUPPORTED", "此浏览器的录音格式暂不支持", 415)
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise ApplicationError("AUDIO_TOO_LARGE", "录音为空或超过大小限制，请重新录制", 413)
        if not 0 < duration_ms <= MAX_AUDIO_SECONDS * 1000:
            raise ApplicationError("AUDIO_DURATION_INVALID", "请录制两分钟以内的语音", 422)
        _config, resolved, profile = self._configuration()
        if not resolved["browser_enabled"] or not resolved["api_key"] or resolved["configuration_error"]:
            raise ApplicationError("VOICE_NOT_READY", "请先配置并启用语音输入", 409)
        task = asyncio.current_task()
        self._active.add(task)
        client = None
        path = None
        try:
            async with self._limit:
                root = self.home / "web" / "recordings"
                root.mkdir(parents=True, exist_ok=True)
                # Use a neutral extension so content probing cannot fall back to a
                # client-supplied filename when the bytes are not recognizable audio.
                with tempfile.NamedTemporaryFile(dir=root, suffix=".bin", delete=False) as stream:
                    stream.write(audio)
                    path = Path(stream.name)
                try:
                    info = probe_audio(path, duration_ms_hint=duration_ms)
                except AudioProbeError as exc:
                    raise ApplicationError("AUDIO_INVALID", "无法读取这段录音，请重新录制", 422) from exc
                if info.mime_type not in {*MIME_TYPES, "audio/opus"}:
                    raise ApplicationError("AUDIO_FORMAT_UNSUPPORTED", "这段录音的格式不受支持", 415)
                if info.duration_ms and info.duration_ms > MAX_AUDIO_SECONDS * 1000:
                    raise ApplicationError("AUDIO_DURATION_INVALID", "录音超过两分钟，请缩短后重试", 422)
                factory = self.transcriber_factory or QwenFileTranscriber
                client = factory(api_key=resolved["api_key"], base_url=resolved["base_url"],
                                 provider=profile.name, model=resolved["model"], max_audio_bytes=MAX_AUDIO_BYTES,
                                 timeout_seconds=min(float(resolved["transcription_timeout_seconds"]), 60))
                result = await client.transcribe(TranscriptionRequest(
                    path=str(path), mime_type=info.mime_type, codec=info.codec,
                    language=resolved["language"], duration_ms=info.duration_ms or duration_ms))
                return {"text": result.text, "provider": result.provider, "model": result.model,
                        "duration_ms": result.duration_ms or info.duration_ms or duration_ms}
        except AudioTranscriptionError as exc:
            raise ApplicationError(exc.code, exc.safe_message, 502) from exc
        finally:
            try:
                if path:
                    path.unlink(missing_ok=True)
                inner = getattr(client, "_client", None)
                close = getattr(inner, "close", None)
                if close:
                    pending = close()
                    if inspect.isawaitable(pending):
                        await pending
            finally:
                self._active.discard(task)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
