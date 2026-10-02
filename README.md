<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="docs/assets/mclaw%20logo.png" alt="M-Claw Logo" width="100%">
</p>

<h2 align="center">Self-Evolving Embodied Agent Harness</h2>

<p align="center">
  <a href="#installation-and-startup">Quick start</a> ·
  <a href="#capabilities">Features</a> ·
  <a href="docs/manual/README.md">User manual (Chinese)</a> ·
  <a href="#agent-loop">Architecture</a> ·
  <a href="#developer-ecosystem">Development</a>
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.1.1-2f80ed">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-green">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Kaihong%20OS%20%7C%20M--Robots%20OS-6f42c1">
</p>

**M-Claw is an embodied agent runtime native to M-Robots OS, connecting models to execution in the physical world across heterogeneous devices.**

Its agent harness brings context and memory, tools and Skills, device control, and collaborative tasks into a single execution loop. Device coordination through M-DDS on M-Robots OS supports a distributed architecture in which each device runs its own agent and collaborates with others.

> Current version: 1.1.1<br>
> License: Apache-2.0<br>
> Distribution: install from source

> 📖 **Complete user guide:** [M-Claw user manual (Chinese)](docs/manual/README.md), with design notes, procedures, commands, example prompts, and platform limitations.

## Roadmap

This is the upstream project roadmap. Versions 1.0 and 1.1 are existing milestones; 1.2 and 2.0 describe planned work. Dates are upstream targets and do not imply that planned features are available in the current release.

### June 2026 | 1.0.0 — Single-device agent runtime

Establish the complete agent loop on M-Robots OS: model-driven execution, tool calls, memory, Skill extensions, and robot control.

### September 2026 | 1.1.0 — Distributed collaboration runtime

Introduce distributed agent collaboration using M-DDS on M-Robots OS for task dispatch, state synchronization, and capability sharing between devices and robots.

The layered robot control architecture separates:

- **Runtime (brain):** intent understanding, task planning, and coordination across nodes.
- **Control module (cerebellum):** stable, high-frequency, real-time motion execution.

### October 2026 | 1.2.0 — Mycelium platform and Skill ecosystem (planned)

Develop the Mycelium Control Panel for centralized scheduling and lifecycle management of robots, devices, Skills, and tasks, extending one-off execution into persistent, evolving workflows.

Introduce a Skill Hub for publishing, installing, updating, and reusing Skills, expanding M-Claw into embodied intelligence infrastructure with a developer ecosystem.

### December 2026 | 2.0.0 — Spatial agent runtime (planned)

Unify spatial and temporal context and device capability semantics to coordinate heterogeneous devices. The intended runtime will understand environmental state, retain location memory, and orchestrate tasks across robots and devices.

Planned capabilities include:

- Persistent spatial and temporal context that accumulates environmental state and changes.
- Unified device capability semantics based on the Kaihong OS super-device model.
- Task allocation and execution coordination across heterogeneous devices in a shared spatial context.
- Action proposals from models, with actual execution decided by a separate control boundary.

## Current capabilities (1.1.1)

M-Claw supports multi-device agent collaboration on M-Robots OS / Kaihong OS. An agent can run inference and orchestrate tools locally, or invoke M-Claw agents on other trusted devices through the distributed soft bus.

The runtime includes:

- **Agent Runtime Core:** inference, planning, tool orchestration, session state, and execution recovery.
- **Tool Runtime:** files, terminals, networking, browsing, vision, memory, and device operations.
- **Skill Layer:** reusable workflows and stored execution experience.
- **M-DDS Runtime:** device discovery, agent communication, remote task dispatch, and file transfer.
- **Safety Runtime:** path policies, credential isolation, execution auditing, and state recovery.

## Capabilities

- **21 baseline tools:** scoped credential requests, file operations, terminal execution, memory, Skills, session search, and subagent delegation.
- **13 optional tools:** web search and extraction, vision, browser automation, and file delivery through Weixin and DingTalk.
- **Trusted-device tools:** four collaboration entry points on M-Robots OS / Kaihong OS, plus five context-dependent tools for returning results, requesting input, and retrieving shared source files in remote tasks.
- **22 built-in commands:** model switching, search/extraction backend selection, voice input, session history, rollback, scheduling, Skill management, and diagnostics. Trusted-device collaboration adds `/devices`, `/pair`, and `/unpair` on supported platforms.
- **Three memory layers:** session memory, long-term memory, and Skill execution/evolution memory.
- **OS-specific runtimes:** adapt execution and capabilities to the host system.
- **Trusted-device collaboration:** device management, agent communication, task dispatch, streaming progress, task continuation, and file exchange.

## Models and providers

M-Claw supports switching models and providers within a session.

- **Model catalog:** uses models.dev metadata for model IDs, providers, and context lengths; catalog contents change upstream.
- **Provider adapters:** integrations and runtime adaptations for multiple model providers.
- **Custom endpoints:** support for OpenAI-compatible and Anthropic Messages interfaces.
- **Cache performance:** depends on the provider, prompt structure, and workload; no fixed hit rate is promised here.

Actual tool calling, reasoning, and vision capabilities depend on the selected model.

## Agent loop

```text
User input / channel message / scheduled task
        ↓
Build system context: platform, tools, Skill index, long-term memory
        ↓
Call model: OpenAI-compatible or Anthropic Messages interface
        ↓
Parse tool calls: files, terminal, search, Skills, memory, etc.
        ↓
Execute tools: reads may run concurrently; writes use checkpoints and safety policies
        ↓
Persist session, trim context, and compact history when needed
        ↓
Continue until a final result or an explainable blocker
        ↓
Trigger background memory and Skill evolution reviews
```

The agent gathers context, uses tools, verifies results, preserves state, and retains reusable experience in memory or Skills.

## Agent tools

M-Claw provides baseline, optional, and platform-specific toolsets. Available tools are filtered by system capabilities, user configuration, and credentials. Trusted-device tools activate only on M-Robots OS / Kaihong OS.

### Baseline toolsets

| Toolset | Tools | Purpose |
| --- | --- | --- |
| credentials | `secret_request_many` | Request scoped secrets for Skills, tools, runtimes, or channels. Secrets are saved to the M-Claw home `.env` without returning plaintext. |
| terminal | `terminal`, `process` | Run commands, maintain the session working directory, start background processes, inspect logs, wait, or terminate processes. |
| file | `read_file`, `write_file`, `patch`, `edit_file`, `delete_file`, `search_files`, `list_directory` | Read, write, replace, delete, search, and list files under runtime path policies. |
| memory | `memory_read`, `memory_add`, `memory_replace`, `memory_remove` | Manage long-term and user-profile memory, with injection and leakage checks before writing. |
| skills | `skills_list`, `skill_tree`, `skill_view`, `skill_search`, `skill_manage` | Discover, inspect, search, create, install, validate, edit, and evolve Skills. |
| session_search | `session_search` | Search session history by keyword using SQLite and FTS5. |
| delegation | `delegate_task` | Dispatch independent tasks to up to five isolated subagents, with terminal and file capabilities enabled by default. |

### Optional toolsets

| Toolset | Tools | Purpose |
| --- | --- | --- |
| web | `web_search`, `web_extract` | Search the web with sources and extract readable page content. |
| vision | `vision_analyze` | Analyze local images or image URLs with a Qwen vision model. |
| browser | `browser_navigate`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_press`, `browser_download` | Navigate, click, type, scroll, capture screenshots, and download files. |
| weixin | `weixin_send_file` | Send files to the current Weixin conversation. |
| dingtalk | `dingtalk_send_file` | Send files to the current DingTalk conversation. |

Web search supports Tavily and Qwen. Use `/search-backend dashscope|tavily|auto` to inspect or change the backend.

Web extraction supports Trafilatura, Tavily, and Firecrawl. Use `/extract-backend trafilatura|tavily|firecrawl` to inspect or change the backend.

### Trusted-device collaboration toolsets

Trusted-device collaboration is provided by `KaihongRuntime` on M-Robots OS / Kaihong OS. Once enabled in Setup and validated against the environment, the local agent receives:

| Toolset | Tools | Purpose |
| --- | --- | --- |
| dsoftbus | `dsoftbus_list_peers`, `dsoftbus_get_device_context`, `dsoftbus_run_agent_task`, `dsoftbus_continue_agent_task` | List reachable trusted devices, read device context, create remote A2A tasks, and continue a task when the remote agent requests more information or files. |

Agents executing remote tasks receive additional tools according to task context:

- `return_artifact`: return structured results or files from the task workspace.
- `request_task_input`: request text, files, or a directory scope from the caller.
- `dsoft_bus_source_list`, `dsoft_bus_source_search`, `dsoft_bus_source_fetch`: browse, search, and retrieve files within the caller's explicitly shared scope, saving them as task working copies.

## Three-layer memory

Memory is layered execution knowledge rather than a single chat-history cache.

| Layer | Purpose | Typical implementation |
| --- | --- | --- |
| Session memory | Sessions, messages, tool calls, token usage, task titles, and searchable history. | `SQLite`, `session_search` |
| Long-term memory | Stable facts, user preferences, environment conventions, tool habits, and troubleshooting experience. | `MEMORY.md`, `USER.md` |
| Skill memory | Reusable procedures, failure lessons, execution notes, and evolution records. | `SKILL.md`, `mclaw_skill.yaml`, `skill_evolution.json` |

Together, these layers let M-Claw accumulate experience for future tasks.

## Skill as Memory

M-Claw 1.0.0 introduced Skills as evolvable runtime capability units: reusable task logic with accumulated execution experience and behavioral improvements.

### Skill structure

- **`SKILL.md` — execution semantics:** capability boundaries, applicable scenarios, and execution logic.
- **`mclaw_skill.yaml` — metadata:** basic information about the Skill.
- **`skill_evolution.json` — evolution memory:** adaptation results, preferences, failure cases, and execution notes.
- **Execution assets:** scripts, references, templates, and other supporting resources.

### Skill writing and governance

All Skill writes go through `skill_manage`. Ordinary file and terminal tools must not modify Skill storage directly, to preserve consistency and safety.

### Skill evolution

- `skill_manage(action="evolution_update")` updates execution experience.
- Background reviews periodically analyze Skill usage and propose evolution.
- Background threads use a restricted tool allowlist to reduce risks from automatic changes.

## Safety policies and recoverable execution

### PathPolicy

File and terminal tools run with the host OS user's permissions. PathPolicy protects credential files, `MCLAW_HOME`, critical system directories, and dangerous recursive operations. A working directory requires a risk acknowledgment on first use.

### Scoped Secret

The runtime does not directly expose credentials to the model. `secret_request_many` requests environment variable names and purposes, then injects saved values only into the authorized Skill, tool, runtime, or channel through `required_for` scopes. Unauthorized sensitive environment variables are removed from child processes, and authorized values are redacted from command output.

### Checkpoint

CheckpointManager uses a shared shadow Git store to take snapshots before file writes, patches, edits, deletions, or destructive terminal operations. Checkpoints live under M-Claw home rather than adding Git state to the user's project directory.

### Rollback

RollbackCoordinator restores files from operation logs, creates conflict backups, and can roll back session context. `/rollback` and `/checkpoints` provide inspection, preview, undo, and restoration.

**The current version does not provide a security sandbox or permission tiers.**

## Operating-system runtimes

At startup, M-Claw selects a runtime for command execution, path policies, process management, and optional capabilities.

| Platform | Runtime | Adaptation |
| --- | --- | --- |
| Windows | `WindowsRuntime` | PowerShell or cmd, Windows path protection, and capabilities enabled according to Git, PySide6, and Playwright availability. |
| Linux and other POSIX environments | `LinuxRuntime` | bash or sh, POSIX path and mount protection, and capabilities based on installed dependencies. |
| M-Robots OS / Kaihong OS | `KaihongRuntime` | Device shell, OpenHarmony path policies, platform adapter selection based on system APIs, CPU ABI, and environment, plus trusted-device collaboration. |

Trusted-device collaboration requires M-Robots OS / Kaihong OS with a supported ARM64 OpenHarmony environment, DSoftBus system components, and appropriate permissions.

The DSoftBus Runtime, collaboration tools, and `/devices`, `/pair`, `/unpair` commands activate only after the user enables trusted-device collaboration in Setup and environment validation succeeds.

## Channels, voice, and scheduled tasks

M-Claw supports these interaction entry points:

- **CLI/TUI:** terminal interaction built with Rich and prompt_toolkit.
- **Voice input:** Qwen realtime ASR with wake-word and push-to-talk modes.
- **Weixin / DingTalk channels:** channel runners map incoming messages to M-Claw sessions and send results back to the originating channel.

Scheduled tasks can also trigger the agent loop. See the [user manual (Chinese)](docs/manual/README.md) for scheduling commands and configuration.

## CLI entry points

```bash
mclaw                        # Start the agent runtime
mclaw setup                  # Initial configuration
mclaw doctor                 # Check runtime, tools, and IM channels
mclaw help                   # Show command guidance
mclaw resume                 # Resume the most recent session
mclaw resume <session_id>    # Resume by session ID or prefix
mclaw weixin login           # Sign in to the Weixin iLink Bot with a QR code
mclaw weixin                 # Start the Weixin direct-message gateway
mclaw dingtalk login         # Configure DingTalk Stream credentials
mclaw dingtalk               # Start the DingTalk Stream gateway
```

## Installation and startup

M-Claw is currently distributed as source code.

### Windows / Linux

#### Step 1: Install Git

On Windows, install Git from the [official download page](https://git-scm.com/download/win) or the [Huawei Cloud mirror](https://mirrors.huaweicloud.com/git-for-windows/).

On Debian/Ubuntu Linux:

```bash
sudo apt update
sudo apt install -y git
```

Verify the installation:

```bash
git --version
```

#### Step 2: Clone and install M-Claw

```bash
git clone https://github.com/tharsis741/mclaw.git
cd mclaw
python -m pip install -e .
```

For browser automation, first install the optional dependencies:

```bash
python -m pip install -e ".[browser]"
```

Then install Chromium:

```powershell
# Windows
python -m playwright install chromium
```

```bash
# Linux
python -m playwright install --with-deps chromium
```

#### Step 3: Run initial setup

```bash
mclaw setup
```

#### Step 4: Start M-Claw

```bash
mclaw
```

#### Updating M-Claw

Exit any running M-Claw process, then run these commands from the source directory. Check and resolve local changes before pulling.

```bash
git status --porcelain
git pull --ff-only
python -m pip install -e .
```

### Kaihong OS / M-Robots OS

Download or extract the source onto the device, enter its root directory, and install:

```bash
run python3 -m pip install -e .
```

Configure and start:

```bash
run mclaw setup
run mclaw
```

### Requirements

- Python 3.11 or newer.
- `git` on `PATH` for Windows/Linux source management and Checkpoint/Rollback. Checkpoints are disabled when Git is missing. Kaihong OS / M-Robots OS currently disables checkpoints and does not require Git.
- Playwright-managed Chromium for browser automation.
- Run `mclaw doctor` after installation to check the runtime, tools, and IM channels.
- Some built-in Skills may install additional toolchains on first use.

## Developer ecosystem

The upstream project plans an open Skill ecosystem around Skill Hub. Developers can create Skills for office work, industry, education, robotics, data analysis, smart homes, and domain knowledge. Local Skill management already supports installation, updates, reuse, and evolution.

## Source and license

This repository was imported from [m-robots/mclaw on AtomGit](https://atomgit.com/m-robots/mclaw). The original project author is Shenzhen Kaihong Digital Industry Development Co., Ltd. This GitHub repository preserves upstream commit history, copyright, and open-source notices.

M-Claw is licensed under Apache-2.0. See [LICENSE](LICENSE).

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party notices and [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) for acknowledgements.
