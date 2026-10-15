# Third Party Notices and Information

This file contains third-party notices for software included in M-Claw. These
notices are provided for attribution and license compliance; they do not modify
the license terms for M-Claw's original work.

## Bundled skills

| M-Claw skill directories | Upstream source | License and copyright |
| --- | --- | --- |
| `minimax-docx`, `minimax-pdf`, `minimax-xlsx`, `pptx-generator`, `mmx-cli` | [MiniMax-AI/skills](https://github.com/MiniMax-AI/skills/tree/main/skills); `mmx-cli` originates from `minimax-multimodal-toolkit` | MIT, Copyright (c) 2026 MiniMax; the DOCX package retains its Copyright (c) 2026 MiniMaxAI notice |
| `aihot`, `hv-analysis`, `khazix-writer`, `neat-freak` | [KKKKhazix/khazix-skills](https://github.com/KKKKhazix/khazix-skills) | MIT, Copyright (c) 2026 数字生命卡兹克 |
| `design-taste-frontend` | [Leonxlnx/taste-skill](https://github.com/Leonxlnx/taste-skill/tree/f624f37a8c650b9cc550253104925efe4c8de246/skills/taste-skill), revision `f624f37a8c650b9cc550253104925efe4c8de246` | MIT, Copyright (c) 2026 Leonxlnx |
| `internal-comms` | [anthropics/skills](https://github.com/anthropics/skills/tree/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/internal-comms), revision `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | Apache-2.0, Copyright 2026 Anthropic, PBC. |
| `knowledge-synthesis` | [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins/tree/f652327d4e953d7957d99e735c3c7f9964e9d2e5/enterprise-search/skills/knowledge-synthesis), revision `f652327d4e953d7957d99e735c3c7f9964e9d2e5` | Apache-2.0; upstream author Anthropic; the upstream license supplies no filled-in copyright notice |
| `humanizer-zh` | [op7418/Humanizer-zh](https://github.com/op7418/Humanizer-zh/tree/f4518a8eab97b8bfebc66a89d34320a89bef6930), revision `f4518a8eab97b8bfebc66a89d34320a89bef6930` | MIT, Copyright (c) 2026 歸藏 |
| `weather` | [openclaw/openclaw](https://github.com/openclaw/openclaw/tree/246adaa1191438b9e9e178384a026b4c123782f1/skills/weather), revision `246adaa1191438b9e9e178384a026b4c123782f1` | MIT, Copyright (c) 2025 Peter Steinberger |

These packages are under `mclaw/skills/`. Each includes an upstream `LICENSE`
and a `mclaw_skill.yaml` recording the original skill URL, author, license,
copyright where supplied by upstream, and license URL. Revisions are recorded where the bundled source
version has been verified; a `main` URL identifies origin, not a pinned version.
M-Claw's descriptions, metadata, and runtime integrations may differ from upstream.

Taste, Internal Communications, Knowledge Synthesis and Humanizer-zh also include
a `NOTICE.md` identifying M-Claw's integration changes. Humanizer-zh retains its
upstream references to blader/humanizer v3.0.0, Humanizer-zh PR 39,
hardikpandya/stop-slop, and Wikipedia's Signs of AI writing.
External images, fonts, and other assets used or discovered by a skill retain
their respective licenses. Repository-level licenses do not replace notices
or terms applicable to individual third-party assets and dependencies.

## Hermes Agent

Portions of M-Claw include code or source text derived from Hermes Agent,
developed by Nous Research.

- Project: Hermes Agent
- Source: https://github.com/NousResearch/hermes-agent
- License: MIT License
- Copyright: Copyright (c) 2025 Nous Research

The following paths identify the M-Claw source files that contain
Hermes Agent-derived material and the corresponding upstream reference paths.
Paths in the right column are relative to the Hermes Agent repository.

| M-Claw file | Hermes Agent reference path |
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
