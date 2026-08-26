# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Distribution metadata checks for the documented source-install workflow."""

from __future__ import annotations

import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _distribution_metadata() -> dict[str, object]:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _project_metadata() -> dict[str, object]:
    return _distribution_metadata()["project"]


def test_default_install_excludes_platform_optional_dependencies() -> None:
    project = _project_metadata()
    dependencies = tuple(project["dependencies"])
    optional = project["optional-dependencies"]

    assert not any(item.startswith("playwright") for item in dependencies)
    assert not any(item.startswith("PySide6") for item in dependencies)
    assert optional["browser"] == ["playwright>=1.40.0"]
    assert optional["desktop"] == ["PySide6>=6.7,<7"]


def test_kaihong_editable_install_command_is_documented() -> None:
    readme = (REPOSITORY_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    manual = (REPOSITORY_ROOT / "docs" / "manual" / "01-快速开始.md").read_text(
        encoding="utf-8"
    )

    command = "run python3 -m pip install -e ."
    assert command in readme
    assert command in manual
    assert "run mclaw setup" in readme
    assert "run mclaw setup" in manual


def test_standard_console_entrypoint_is_the_only_product_launcher() -> None:
    metadata = _distribution_metadata()
    project = metadata["project"]
    package_data = metadata["tool"]["setuptools"]["package-data"]["mclaw"]

    assert project["scripts"] == {"mclaw": "mclaw.cli.main:main"}
    assert not any("launcher" in item for item in package_data)
    assert not (
        REPOSITORY_ROOT
        / "mclaw"
        / "dsoftbus"
        / "resources"
        / "mclaw_product_launcher.sh"
    ).exists()
