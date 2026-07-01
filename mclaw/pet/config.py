# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration helpers for the optional animated desktop pet."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from mclaw.platform import gui_available


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def _enabled_mode(value: Any) -> str:
    """Normalize user-facing enabled values while preserving the auto mode."""
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    return "on" if _truthy(value, False) else "off"


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _asset(value: Any) -> str:
    name = str(value or "robot-dark")
    return "robot-dark" if name == "default" else name


@dataclass
class PetNotifyConfig:
    """Notification toggles that decide which runtime events reach the pet."""

    turn_completed: bool = True
    tool_finished: bool = False
    background_completed: bool = True
    delegation_completed: bool = True
    sound: bool = False


@dataclass
class PetConfig:
    """Runtime-safe desktop pet settings derived from the main M-Claw config."""

    enabled: bool = False
    enabled_mode: str = "off"
    backend: str = "pyside6"
    asset: str = "robot-dark"
    scale: float = 1.0
    always_on_top: bool = True
    click_through: bool = False
    position: str = "bottom_right"
    x: int | None = None
    y: int | None = None
    show_bubble: bool = True
    bubble_seconds: float = 2.5
    sleep_after_seconds: float = 120.0
    notify: PetNotifyConfig = field(default_factory=PetNotifyConfig)

    @classmethod
    def from_config(cls, config: dict | None) -> "PetConfig":
        """Create a bounded runtime config from the nested display.pet section."""
        display = (config or {}).get("display", {})
        raw = display.get("pet", {}) if isinstance(display, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        notify_raw = raw.get("notify", {})
        if not isinstance(notify_raw, dict):
            notify_raw = {}

        mode = _enabled_mode(raw.get("enabled"))
        # Auto mode only enables the sidecar when a GUI session is available.
        enabled = gui_available() if mode == "auto" else mode == "on"

        return cls(
            enabled=enabled,
            enabled_mode=mode,
            backend=str(raw.get("backend") or "pyside6"),
            asset=_asset(raw.get("asset")),
            scale=max(0.25, min(_float(raw.get("scale"), 1.0), 4.0)),
            always_on_top=_truthy(raw.get("always_on_top"), True),
            click_through=_truthy(raw.get("click_through"), False),
            position=str(raw.get("position") or "bottom_right"),
            x=raw.get("x") if isinstance(raw.get("x"), int) else None,
            y=raw.get("y") if isinstance(raw.get("y"), int) else None,
            show_bubble=_truthy(raw.get("show_bubble"), True),
            bubble_seconds=max(0.5, min(_float(raw.get("bubble_seconds"), 2.5), 10.0)),
            sleep_after_seconds=max(10.0, min(_float(raw.get("sleep_after_seconds"), 120.0), 3600.0)),
            notify=PetNotifyConfig(
                turn_completed=_truthy(notify_raw.get("turn_completed"), True),
                tool_finished=_truthy(notify_raw.get("tool_finished"), False),
                background_completed=_truthy(notify_raw.get("background_completed"), True),
                delegation_completed=_truthy(notify_raw.get("delegation_completed"), True),
                sound=_truthy(notify_raw.get("sound"), False),
            ),
        )

    def to_runtime_dict(self) -> Dict[str, Any]:
        """Serialize the config shape passed to the isolated sidecar process."""
        return {
            "enabled": self.enabled,
            "enabled_mode": self.enabled_mode,
            "backend": self.backend,
            "asset": self.asset,
            "scale": self.scale,
            "always_on_top": self.always_on_top,
            "click_through": self.click_through,
            "position": self.position,
            "x": self.x,
            "y": self.y,
            "show_bubble": self.show_bubble,
            "bubble_seconds": self.bubble_seconds,
            "sleep_after_seconds": self.sleep_after_seconds,
            "notify": {
                "turn_completed": self.notify.turn_completed,
                "tool_finished": self.notify.tool_finished,
                "background_completed": self.notify.background_completed,
                "delegation_completed": self.notify.delegation_completed,
                "sound": self.notify.sound,
            },
        }


def ensure_pet_config(config: dict) -> dict:
    """Ensure the mutable app config contains every desktop pet default key."""
    display = config.setdefault("display", {})
    if not isinstance(display, dict):
        display = {}
        config["display"] = display
    pet = display.setdefault("pet", {})
    if not isinstance(pet, dict):
        pet = {}
        display["pet"] = pet
    pet.setdefault("enabled", "auto")
    pet.setdefault("backend", "pyside6")
    pet.setdefault("asset", "robot-dark")
    pet.setdefault("scale", 1.0)
    pet.setdefault("always_on_top", True)
    pet.setdefault("click_through", False)
    pet.setdefault("position", "bottom_right")
    pet.setdefault("x", None)
    pet.setdefault("y", None)
    pet.setdefault("show_bubble", True)
    pet.setdefault("bubble_seconds", 2.5)
    pet.setdefault("sleep_after_seconds", 120.0)
    notify = pet.setdefault("notify", {})
    if not isinstance(notify, dict):
        notify = {}
        pet["notify"] = notify
    notify.setdefault("turn_completed", True)
    notify.setdefault("tool_finished", False)
    notify.setdefault("background_completed", True)
    notify.setdefault("delegation_completed", True)
    notify.setdefault("sound", False)
    return pet
