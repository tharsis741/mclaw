# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Microphone recorder for voice input."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

_KAIHONG_ALSA_CONFIG = """\
defaults.pcm.card 0
defaults.pcm.device 0
defaults.ctl.card 0

ctl.hw {
    @args [ CARD ]
    @args.CARD {
        type string
    }
    type hw
    card $CARD
}

pcm.hw {
    @args [ CARD DEV SUBDEV ]
    @args.CARD {
        type string
    }
    @args.DEV {
        type integer
        default 0
    }
    @args.SUBDEV {
        type integer
        default -1
    }
    type hw
    card $CARD
    device $DEV
    subdevice $SUBDEV
}

pcm.plughw {
    @args [ CARD DEV SUBDEV ]
    @args.CARD {
        type string
    }
    @args.DEV {
        type integer
        default 0
    }
    @args.SUBDEV {
        type integer
        default -1
    }
    type plug
    slave.pcm {
        type hw
        card $CARD
        device $DEV
        subdevice $SUBDEV
    }
}
"""


def _is_kaihong_runtime() -> bool:
    try:
        from mclaw.runtime.manager import RuntimeManager

        return RuntimeManager.current().kind == "kaihong"
    except Exception:
        return False


class AudioRecorder:
    """Select and expose the available microphone capture backend."""

    def __init__(self, config: dict):
        self.config = config or {}
        self._impl = self._select_recorder(self.config)

    def start(self, on_audio_chunk):
        self._impl.start(on_audio_chunk)

    @property
    def running(self) -> bool:
        return bool(getattr(self._impl, "running", False))

    @staticmethod
    def input_available(config: dict | None = None) -> bool:
        """Probe configured backends without starting a long-lived capture."""
        config = config or {}
        backend = _recorder_backend(config)
        if backend in {"sounddevice", "portaudio"}:
            return SounddeviceAudioRecorder.input_available()
        if backend == "arecord":
            return ArecordAudioRecorder.input_available(config)
        return SounddeviceAudioRecorder.input_available() or ArecordAudioRecorder.input_available(config)

    @staticmethod
    def _select_recorder(config: dict):
        """Prefer explicit backend selection, then fall back by capability probe."""
        backend = _recorder_backend(config)
        if backend in {"sounddevice", "portaudio"}:
            return SounddeviceAudioRecorder(config)
        if backend == "arecord":
            return ArecordAudioRecorder(config)
        if SounddeviceAudioRecorder.input_available():
            return SounddeviceAudioRecorder(config)
        if ArecordAudioRecorder.input_available(config):
            return ArecordAudioRecorder(config)
        return SounddeviceAudioRecorder(config)

    def stop(self):
        self._impl.stop()


def _recorder_backend(config: dict) -> str:
    return str(config.get("recorder_backend") or "auto").strip().lower()


class SounddeviceAudioRecorder:
    """PortAudio/sounddevice recorder used on desktop-like runtimes."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.running = False
        self._stream = None

    def start(self, on_audio_chunk):
        """Start an InputStream whose callback must stay non-blocking."""
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Install M-Claw dependencies from the source directory: pip install -e .") from exc
        if self.running:
            return

        if not self._has_input_device(sd):
            raise RuntimeError("No audio input device detected by PortAudio/sounddevice.")

        sample_rate = int(self.config.get("sample_rate") or 16000)
        channels = int(self.config.get("channels") or 1)
        block_ms = int(self.config.get("chunk_ms") or 100)
        blocksize = max(1, int(sample_rate * block_ms / 1000))

        def _callback(indata, frames, time_info, status):
            if status:
                # Non-fatal under load; the ASR backend can tolerate dropped chunks.
                pass
            on_audio_chunk(indata.tobytes())

        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            callback=_callback,
        )
        self._stream.start()
        self.running = True

    @staticmethod
    def input_available() -> bool:
        try:
            import sounddevice as sd
        except ImportError:
            return False
        return SounddeviceAudioRecorder._has_input_device(sd)

    @staticmethod
    def _has_input_device(sd) -> bool:
        try:
            default_input = sd.default.device[0]
            if isinstance(default_input, int) and default_input >= 0:
                return True
        except Exception:
            pass
        try:
            devices = sd.query_devices()
        except Exception:
            return False
        try:
            for device in devices:
                if int(device.get("max_input_channels") or 0) > 0:
                    return True
        except Exception:
            return False
        return False

    def stop(self):
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        self.running = False


class ArecordAudioRecorder:
    """ALSA arecord backend used for Kaihong and other minimal Linux runtimes."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.running = False
        self._process = None
        self._reader = None
        self._stderr_reader = None
        self._stderr_tail = deque(maxlen=20)

    def start(self, on_audio_chunk):
        """Start arecord and stream stdout chunks from a background reader thread."""
        if self.running:
            return

        device = self.resolve_device(self.config)
        if not device:
            raise RuntimeError("No ALSA capture device detected by arecord.")

        sample_rate = int(self.config.get("sample_rate") or 16000)
        channels = int(self.config.get("channels") or 1)
        block_ms = int(self.config.get("chunk_ms") or 100)
        chunk_size = max(2, int(sample_rate * channels * 2 * block_ms / 1000))
        cmd = [
            "arecord",
            "-q",
            "-D",
            device,
            "-f",
            "S16_LE",
            "-r",
            str(sample_rate),
            "-c",
            str(channels),
            "-t",
            "raw",
        ]

        last_message = ""
        for attempt in range(3):
            self._stderr_tail.clear()
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    env=_arecord_env(self.config),
                )
            except FileNotFoundError as exc:
                raise RuntimeError("arecord is not available for ALSA capture.") from exc
            except OSError as exc:
                raise RuntimeError(f"Failed to start arecord: {exc}") from exc

            self.running = True
            self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
            self._stderr_reader.start()
            time.sleep(0.15)
            if self._process.poll() is None:
                self._reader = threading.Thread(target=self._read_stdout, args=(on_audio_chunk, chunk_size), daemon=True)
                self._reader.start()
                return

            self.running = False
            last_message = self._stderr_message() or "process exited"
            self.stop()
            if "resource busy" in last_message.lower() and attempt < 2:
                time.sleep(0.25 * (attempt + 1))
                continue
            break

        raise RuntimeError(f"arecord capture failed: {_explain_arecord_failure(last_message, device)}")

    @staticmethod
    def input_available(config: dict | None = None) -> bool:
        """Allow auto selection only on Kaihong, unless arecord was requested."""
        config = config or {}
        if _recorder_backend(config) == "auto" and not _is_kaihong_runtime():
            return False
        return bool(ArecordAudioRecorder.resolve_device(config))

    @staticmethod
    def resolve_device(config: dict | None = None) -> str:
        """Return an explicit ALSA device or the first detected capture device."""
        config = config or {}
        configured = str(config.get("arecord_device") or config.get("alsa_device") or "").strip()
        if configured and configured.lower() != "auto":
            return configured
        devices = _arecord_capture_devices(config)
        if not devices:
            return ""
        card, device, _label = devices[0]
        return f"plughw:{card},{device}"

    def _read_stdout(self, on_audio_chunk, chunk_size: int):
        process = self._process
        stream = process.stdout if process is not None else None
        try:
            while self.running and process is not None and stream is not None:
                data = stream.read(chunk_size)
                if data:
                    on_audio_chunk(data)
                    continue
                if process.poll() is not None:
                    break
        finally:
            self.running = False

    def _read_stderr(self):
        process = self._process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        while True:
            data = stream.readline()
            if not data:
                break
            try:
                self._stderr_tail.append(data.decode("utf-8", errors="replace").strip())
            except Exception:
                pass

    def _stderr_message(self) -> str:
        return " ".join(item for item in self._stderr_tail if item).strip()

    def stop(self):
        """Terminate the capture process and join reader threads best-effort."""
        process = self._process
        self._process = None
        self.running = False
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        for stream_name in ("stdout", "stderr"):
            stream = getattr(process, stream_name, None) if process is not None else None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=1.0)
        self._reader = None
        self._stderr_reader = None


def _arecord_capture_devices(config: dict | None = None) -> list[tuple[int, int, str]]:
    """Discover ALSA capture devices from arecord output and procfs fallback."""
    if shutil.which("arecord") is None:
        return []
    devices: list[tuple[int, int, str]] = []
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            env=_arecord_env(config),
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    output = ""
    if result is not None:
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
    for line in output.splitlines():
        match = re.search(r"card\s+(\d+):\s+(.+?),\s+device\s+(\d+):\s+(.+)", line)
        if not match:
            continue
        card = int(match.group(1))
        device = int(match.group(3))
        label = f"{match.group(2).strip()} {match.group(4).strip()}"
        devices.append((card, device, label))
    devices.extend(_proc_asound_capture_devices())
    deduped: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for card, device, label in devices:
        key = (card, device)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((card, device, label))
    return deduped


def _arecord_env(config: dict | None = None) -> dict[str, str]:
    """Build the capture environment, installing Kaihong's minimal ALSA config."""
    env = os.environ.copy()
    configured = str((config or {}).get("alsa_config_path") or env.get("ALSA_CONFIG_PATH") or "").strip()
    if configured:
        env["ALSA_CONFIG_PATH"] = configured
        return env
    if _is_kaihong_runtime():
        config_path = _ensure_kaihong_alsa_config()
        if config_path:
            env["ALSA_CONFIG_PATH"] = config_path
    return env


def _ensure_kaihong_alsa_config() -> str:
    """Materialize the ALSA config required by Kaihong's stripped-down runtime."""
    home = Path(os.environ.get("MCLAW_HOME") or "/data/local/tmp/.mclaw")
    config_path = home / "runtime" / "alsa" / "asound.conf"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        current = config_path.read_text(encoding="utf-8", errors="replace") if config_path.exists() else ""
        if current != _KAIHONG_ALSA_CONFIG:
            config_path.write_text(_KAIHONG_ALSA_CONFIG, encoding="utf-8")
        return str(config_path)
    except OSError:
        return ""


def _explain_arecord_failure(message: str, device: str) -> str:
    """Add device-holder diagnostics to common ALSA busy failures."""
    if "resource busy" not in (message or "").lower():
        return message or "process exited"
    device_path = _capture_device_path(device)
    owners = _device_fd_owners(device_path) if device_path else []
    if owners:
        return f"{message}; device holders: {', '.join(owners)}"
    if device_path:
        return f"{message}; no visible process holds {device_path}"
    return message or "process exited"


def _capture_device_path(device: str) -> str:
    match = re.search(r"(?:plug)?hw:(\d+),(\d+)", device or "")
    if not match:
        return ""
    return f"/dev/snd/pcmC{int(match.group(1))}D{int(match.group(2))}c"


def _device_fd_owners(device_path: str) -> list[str]:
    """Inspect procfs for processes that currently hold the capture device."""
    owners: list[str] = []
    seen: set[str] = set()
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) != device_path:
                    continue
            except OSError:
                continue
            owner = _proc_label(entry)
            if owner and owner not in seen:
                seen.add(owner)
                owners.append(owner)
            break
        if len(owners) >= 5:
            break
    return owners


def _proc_label(proc_entry: Path) -> str:
    pid = proc_entry.name
    try:
        raw = (proc_entry / "cmdline").read_bytes().replace(b"\x00", b" ").strip()
        text = raw.decode("utf-8", errors="replace").strip()
    except OSError:
        text = ""
    if not text:
        try:
            text = (proc_entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
    return f"{pid}:{text}" if text else pid


def _proc_asound_capture_devices() -> list[tuple[int, int, str]]:
    try:
        text = Path("/proc/asound/pcm").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    devices: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        if "capture" not in line.lower():
            continue
        match = re.match(r"\s*(\d+)-(\d+):\s+(.+)", line)
        if not match:
            continue
        devices.append((int(match.group(1)), int(match.group(2)), match.group(3).strip()))
    return devices
