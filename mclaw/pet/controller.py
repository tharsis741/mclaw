# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thread-safe controller for the optional animated desktop pet sidecar."""

from __future__ import annotations

import logging
import atexit
import importlib.util
import multiprocessing as mp
import os
import queue
import threading
import time
from typing import Any

from mclaw.pet.assets import load_pet_manifest
from mclaw.pet.config import PetConfig
from mclaw.pet.events import LOW_PRIORITY_EVENTS, PetEvent, PetEventType, PetState

logger = logging.getLogger(__name__)


class PetController:
    """Non-blocking bridge from M-Claw runtime events to the pet sidecar."""

    def __init__(self, config: PetConfig, session_id: str = ""):
        self.config = config
        self.session_id = session_id
        self._queue: mp.Queue | None = None
        self._command_queue: mp.Queue | None = None
        self._process: mp.Process | None = None
        self._lock = threading.Lock()
        self._last_low_priority_at: dict[str, float] = {}
        self._last_error: str = ""
        self._atexit_registered = False

    @classmethod
    def from_config(cls, config: dict | None, session_id: str = "") -> "PetController":
        return cls(PetConfig.from_config(config), session_id=session_id)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.is_alive())

    @property
    def last_error(self) -> str:
        return self._last_error

    def start_if_enabled(self) -> bool:
        if not self.config.enabled:
            return False
        return self.start()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            if self.config.backend.lower() != "pyside6":
                self._last_error = f"Unsupported pet backend: {self.config.backend}"
                return False
            if importlib.util.find_spec("PySide6") is None:
                self._last_error = "PySide6 is not installed. Install with: pip install -e \".[pet]\""
                return False
            try:
                load_pet_manifest(self.config.asset)
            except Exception as exc:
                self._last_error = f"Invalid pet asset: {exc}"
                return False
            try:
                mp.freeze_support()
                ctx = mp.get_context("spawn")
                event_queue = ctx.Queue(maxsize=128)
                command_queue = ctx.Queue(maxsize=32)
                runtime_config = self.config.to_runtime_dict()
                runtime_config["parent_pid"] = os.getpid()
                proc = ctx.Process(
                    target=_run_sidecar,
                    args=(event_queue, command_queue, runtime_config),
                    daemon=True,
                )
                proc.start()
                self._queue = event_queue
                self._command_queue = command_queue
                self._process = proc
                self._last_error = ""
                if not self._atexit_registered:
                    atexit.register(self.stop)
                    self._atexit_registered = True
                self.emit(PetEventType.APP_STARTED, state=PetState.IDLE)
                return True
            except Exception as exc:
                self._last_error = str(exc)
                logger.debug("Pet sidecar failed to start: %s", exc)
                self._queue = None
                self._command_queue = None
                self._process = None
                return False

    def stop(self) -> None:
        self.emit(PetEventType.APP_EXITING, state=PetState.IDLE)
        with self._lock:
            proc = self._process
            event_queue = self._queue
            command_queue = self._command_queue
            self._process = None
            self._queue = None
            self._command_queue = None
        if proc and proc.is_alive():
            proc.join(timeout=1.0)
            if proc.is_alive():
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc.join(timeout=0.5)
            if proc.is_alive():
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.join(timeout=0.5)
        for q in (event_queue, command_queue):
            if q is None:
                continue
            try:
                q.close()
            except Exception:
                pass
            try:
                q.join_thread()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.close()
            except Exception:
                pass

    def emit(
        self,
        event_type: str | PetEventType,
        *,
        state: str | PetState | None = None,
        text: str = "",
        payload: dict[str, Any] | None = None,
        throttle_seconds: float = 0.5,
    ) -> bool:
        if not self.config.enabled:
            return False
        q = self._queue
        if q is None or not self.running:
            return False

        event_name = event_type.value if isinstance(event_type, PetEventType) else str(event_type)
        if event_name in LOW_PRIORITY_EVENTS:
            now = time.monotonic()
            last = self._last_low_priority_at.get(event_name, 0.0)
            if now - last < throttle_seconds:
                return False
            self._last_low_priority_at[event_name] = now

        state_name = state.value if isinstance(state, PetState) else state
        event = PetEvent(
            type=event_name,
            state=state_name,
            text=text,
            session_id=self.session_id,
            payload=payload or {},
        )
        try:
            q.put_nowait(event.to_dict())
            return True
        except queue.Full:
            return False
        except Exception as exc:
            self._last_error = str(exc)
            return False


    def get_command_nowait(self) -> dict[str, Any] | None:
        q = self._command_queue
        if q is None or not self.running:
            return None
        try:
            command = q.get_nowait()
        except queue.Empty:
            return None
        except Exception as exc:
            self._last_error = str(exc)
            return None
        return command if isinstance(command, dict) else None


def _run_sidecar(event_queue: mp.Queue, command_queue: mp.Queue, runtime_config: dict) -> None:
    from mclaw.pet.runtime_qt import run_pet

    run_pet(event_queue, command_queue, runtime_config)

