# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Adapt shared tool configuration to the terminal's active conversation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING

from mclaw.cli import config as config_io
from mclaw.cli.env_loader import load_mclaw_dotenv

if TYPE_CHECKING:
    from mclaw.agent.core import MClaw


def refresh_interactive_tools(agent: MClaw, workspace: str | Path) -> None:
    """Refresh TUI tools between turns while keeping the session's model."""
    load_mclaw_dotenv()
    # TUI session paths are persisted strings; shared config readers use Path.
    latest = config_io.load_merged_config(cwd=Path(workspace))
    previous = agent.config
    configured = previous.get("toolsets", ["mclaw-required"])
    if agent.enabled_toolsets is None or agent.enabled_toolsets == configured:
        agent.enabled_toolsets = latest.get("toolsets", ["mclaw-required"])
    updated = copy.deepcopy(previous)
    updated["tools"] = latest.get("tools", {})
    updated["toolsets"] = latest.get("toolsets", ["mclaw-required"])
    for identifier in ("web_search", "web_extract", "vision"):
        updated.setdefault("auxiliary", {})[identifier] = latest.get("auxiliary", {}).get(identifier, {})
    agent.config = updated
    agent._discover_tools()
    agent.mark_prompt_epoch_dirty()
