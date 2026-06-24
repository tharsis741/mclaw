"""Asset lookup and validation for desktop pet spritesheets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


REQUIRED_STATES = {"idle", "running", "review", "waiting", "waving", "failed"}


def bundled_assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


def resolve_asset_dir(asset: str) -> Path:
    if not asset or asset == "default":
        asset = "robot-dark"
    candidate = Path(asset)
    if candidate.exists():
        return candidate.resolve()
    return bundled_assets_dir() / asset


def load_pet_manifest(asset: str) -> Dict[str, Any]:
    asset_dir = resolve_asset_dir(asset)
    manifest_path = asset_dir / "pet.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Pet manifest not found: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    states = data.get("states")
    if not isinstance(states, dict):
        raise ValueError("pet.json must contain a states object")
    missing = sorted(REQUIRED_STATES - set(states))
    if missing:
        raise ValueError(f"pet.json missing states: {', '.join(missing)}")
    image = data.get("image")
    if not image or not (asset_dir / str(image)).exists():
        raise FileNotFoundError(f"Pet spritesheet not found: {asset_dir / str(image)}")
    return data
