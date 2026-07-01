# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Asset lookup and validation for desktop pet spritesheets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


REQUIRED_STATES = {"idle", "running", "review", "waiting", "waving", "failed"}


def bundled_assets_dir() -> Path:
    """Return the directory that contains packaged pet asset bundles."""
    return Path(__file__).resolve().parent / "assets"


def resolve_asset_dir(asset: str) -> Path:
    """Resolve a bundled asset name or an explicit filesystem asset path."""
    if not asset or asset == "default":
        asset = "robot-dark"
    candidate = Path(asset)
    if candidate.exists():
        return candidate.resolve()
    return bundled_assets_dir() / asset


def load_pet_manifest(asset: str) -> Dict[str, Any]:
    """Load and validate the manifest contract required by the pet runtime."""
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
