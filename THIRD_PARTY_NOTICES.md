# Third Party Notices and Information

This file contains third-party notices for software included in M Claw. These
notices are provided for attribution and license compliance; they do not modify
the license terms for M Claw's original work.

## Hermes Agent

Portions of M Claw include code or source text derived from Hermes Agent,
developed by Nous Research.

- Project: Hermes Agent
- Source: https://github.com/NousResearch/hermes-agent
- License: MIT License
- Copyright: Copyright (c) 2025 Nous Research

The following paths identify the M Claw source files that contain
Hermes Agent-derived material and the corresponding upstream reference paths.
Paths in the right column are relative to the Hermes Agent repository.

| M Claw file | Hermes Agent reference path |
| --- | --- |
| `mclaw/agent/context_compressor.py` | `agent/context_compressor.py` |
| `mclaw/agent/memory_manager.py` | `agent/memory_manager.py` |
| `mclaw/agent/retry_utils.py` | `agent/retry_utils.py` |
| `mclaw/agent/skill_utils.py` | `agent/skill_utils.py` |
| `mclaw/channels/weixin/context_token_store.py` | `gateway/platforms/weixin.py` |
| `mclaw/channels/weixin/ilink_client.py` | `gateway/platforms/weixin.py` |
| `mclaw/channels/weixin/media.py` | `gateway/platforms/weixin.py` |
| `mclaw/channels/weixin/outbound_media.py` | `gateway/platforms/weixin.py` |
| `mclaw/cli/colors.py` | `hermes_cli/colors.py` |
| `mclaw/cli/config.py` | `hermes_cli/config.py` |
| `mclaw/cli/env_loader.py` | `hermes_cli/env_loader.py` |
| `mclaw/constants.py` | `hermes_constants.py` |
| `mclaw/skills_hub/builtin_sync.py` | `tools/skills_sync.py` |
| `mclaw/skills_hub/security_scan.py` | `tools/skills_guard.py` |
| `mclaw/state.py` | `hermes_state.py` |
| `mclaw/tools/ansi_strip.py` | `tools/ansi_strip.py` |
| `mclaw/tools/checkpoint_manager.py` | `tools/checkpoint_manager.py` |
| `mclaw/tools/memory_tool.py` | `tools/memory_tool.py` |
| `mclaw/tools/process_registry.py` | `tools/process_registry.py` |
| `mclaw/tools/read_tracker.py` | `tools/file_tools.py` |
| `mclaw/tools/registry.py` | `tools/registry.py` |
| `mclaw/tools/vision/processing.py` | `tools/vision_tools.py` |
| `mclaw/utils.py` | `utils.py` |
| `mclaw/channels/dingtalk/adapter.py` | `gateway/platforms/dingtalk.py` |
| `mclaw/channels/dingtalk/stream_client.py` | `gateway/platforms/dingtalk.py` |
| `mclaw/channels/weixin/adapter.py` | `gateway/platforms/weixin.py` |
| `mclaw/channels/weixin/config.py` | `gateway/platforms/weixin.py` |
| `mclaw/tools/delegate_tool.py` | `tools/delegate_tool.py` |
| `mclaw/tools/file_tools.py` | `tools/file_tools.py` |
| `mclaw/tools/vision_tool.py` | `tools/vision_tools.py` |

### MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
