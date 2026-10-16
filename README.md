<p align="center">
  <strong>English</strong> | <a href="docs/README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="webui/public/mclaw-wordmark-fold.svg" alt="M-CLAW" width="360">
</p>

<h2 align="center">Self-Evolving Embodied Agent Harness</h2>

<p align="center">
  <a href="#installation-and-startup">Quick start</a> ·
  <a href="#updating-m-claw">Update</a> ·
  <a href="#capabilities">Features</a> ·
  <a href="docs/manual/README.md">User manual (Chinese)</a> ·
  <a href="#agent-loop">Architecture</a> ·
  <a href="#developer-ecosystem">Development</a>
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.1.2-2f80ed">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-green">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Kaihong%20OS%20%7C%20M--Robots%20OS-6f42c1">
</p>

**M-Claw is an embodied agent runtime native to M-Robots OS, connecting models to execution in the physical world across heterogeneous devices.**

Its agent harness brings context and memory, tools and Skills, device operations, and collaborative tasks into a single execution loop. Use the Web UI or CLI to start tasks and inspect progress and results. On supported M-Robots OS / Kaihong OS devices, M-DDS enables a distributed architecture in which each device runs its own agent and collaborates with others. Unified spatial representation and persistent location memory remain roadmap items.

![M-Claw 1.1.2 on Kaihong BotBook](docs/assets/snapshot/kaihong-overview.png)

> Current version: 1.1.2<br>
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

### November 2026 | 1.2.0 — Mycelium platform and Skill ecosystem (planned)

Develop the Mycelium Control Panel for centralized scheduling and lifecycle management of robots, devices, Skills, and tasks, extending one-off execution into persistent, evolving workflows.

Introduce a Skill Hub for publishing, installing, updating, and reusing Skills, expanding M-Claw into embodied intelligence infrastructure with a developer ecosystem.

### February 2027 | 2.0.0 — Spatial agent runtime (planned)

Unify spatial and temporal context and device capability semantics to coordinate heterogeneous devices. The intended runtime will understand environmental state, retain location memory, and orchestrate tasks across robots and devices.

Planned capabilities include:

- Persistent spatial and temporal context that accumulates environmental state and changes.
- Unified device capability semantics based on the Kaihong OS super-device model.
- Task allocation and execution coordination across heterogeneous devices in a shared spatial context.
- Action proposals from models, with actual execution decided by a separate control boundary.

## Current capabilities (1.1.2)

Version 1.1.2 adds a Web UI that shares the Python application backend with the CLI. Manage conversations, devices, scheduled tasks, tools, Skills, the Skill market, and settings through the interface. Trusted-device collaboration remains platform-dependent.

M-Claw supports multi-device agent collaboration on M-Robots OS / Kaihong OS. An agent can run inference and orchestrate tools locally, or invoke M-Claw agents on other trusted devices through the distributed soft bus.

The runtime includes:

- **Agent Runtime Core:** inference, planning, tool orchestration, session state, and execution recovery.
- **Tool Runtime:** files, terminals, networking, browsing, vision, memory, and device operations.
- **Skill Layer:** reusable workflows and stored execution experience.
- **M-DDS Runtime:** device discovery, agent communication, remote task dispatch, and file transfer.
- **Safety Runtime:** path policies, credential isolation, execution auditing, and state recovery.

## Capabilities

- **21 baseline tools:** scoped credential requests, file operations, terminal execution, memory, Skills, session search, and subagent delegation.
- **21 optional tools:** web search and extraction, vision, browser automation, and file delivery through Weixin and DingTalk.
- **Trusted-device tools:** four collaboration entry points on M-Robots OS / Kaihong OS, plus five context-dependent tools for returning results, requesting input, and retrieving shared source files in remote tasks.
- **Web-based management:** conversations, devices, scheduled tasks, tools, Skills, Skill market, and settings. Conversation [slash shortcuts](docs/manual/对话.md#斜杠快捷操作) provide session and model selection; task controls handle running work.
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
| vision | `vision_analyze` | Analyze images with the current model or a dedicated vision model; image-input capability is determined using models.dev. |
| browser | `browser_navigate`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_press`, `browser_download`, `browser_tabs`, `browser_select`, `browser_check`, `browser_hover`, `browser_upload`, `browser_handle_dialog`, `browser_wait_for`, `browser_back` | Interact with pages, tabs, forms, uploads/downloads, dialogs, conditional waits, and screenshots. |
| weixin | `weixin_send_file` | Send files to the current Weixin conversation. |
| dingtalk | `dingtalk_send_file` | Send files to the current DingTalk conversation. |

Web search supports Tavily and Qwen. Select the provider and configure credentials under Tools → Web search.

Web extraction supports Trafilatura, Tavily, and Firecrawl. Select the provider and save settings under Tools → Web extraction.

### Trusted-device collaboration toolsets

Trusted-device collaboration is provided by `KaihongRuntime` on M-Robots OS / Kaihong OS. Once M-Claw collaboration is enabled on the Devices page and the environment is validated, the local agent receives:

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

RollbackCoordinator restores files from operation logs, creates conflict backups, and can roll back session context. Use Changes at the top of a conversation to inspect, undo, or restore file operations. Settings → Task execution → File recovery controls retention.

**The current version does not provide a security sandbox or permission tiers.**

## Operating-system runtimes

At startup, M-Claw selects a runtime for command execution, path policies, process management, and optional capabilities.

| Platform | Runtime | Adaptation |
| --- | --- | --- |
| Windows | `WindowsRuntime` | PowerShell or cmd, Windows path protection, and capabilities enabled according to Git, PySide6, and Playwright availability. |
| Linux and other POSIX environments | `LinuxRuntime` | bash or sh, POSIX path and mount protection, and capabilities based on installed dependencies. |
| M-Robots OS / Kaihong OS | `KaihongRuntime` | Device shell, OpenHarmony path policies, platform adapter selection based on system APIs, CPU ABI, and environment, plus trusted-device collaboration. |

Trusted-device collaboration requires M-Robots OS / Kaihong OS with a supported ARM64 OpenHarmony environment, DSoftBus system components, and appropriate permissions.

Enable M-Claw collaboration on the Devices page. After environment validation succeeds, discover, pair, and manage trusted devices there; the runtime loads collaboration tools according to the collaboration state.

## Channels, voice, and scheduled tasks

- **Conversations:** submit tasks and inspect progress and results; use the input-area controls to add input, stop, or continue work.
- **Voice input:** configure it in Settings → General → Voice input, record with the microphone control, and review the transcription before sending.
- **Weixin / DingTalk:** configure message channels in Settings, receive tasks from connected chats, and return results to the originating conversation.
- **Scheduled tasks:** create schedules, choose where results are saved or delivered, and inspect individual run records.

## CLI entry points

```bash
mclaw          # Start or reuse the local backend and open the Web UI
mclaw status   # Show the service address and process ID
mclaw stop     # Save progress, wait for tools to finish, and stop the backend
mclaw cli      # Open the terminal interface
mclaw setup    # Initial configuration
mclaw doctor   # Check the runtime, tools, and IM channels
mclaw help     # Show command guidance
```

## Installation and startup

M-Claw is distributed as source code. The development/build computer needs Python 3.11+, Git, and Node.js 22.12+ with npm 10+. Once built, frontend assets are served by the Python backend; the runtime device does not need Node.js.

### Windows / Linux

Install [Python](https://www.python.org/downloads/), [Git](https://git-scm.com/downloads), and [Node.js](https://nodejs.org/en/download), then run:

```bash
git clone https://github.com/tharsis741/mclaw.git
cd mclaw
python -m pip install -e .
npm --prefix webui ci
npm --prefix webui run build
mclaw
```

On Linux, use `python3` if required by your environment. Complete configuration in the page opened on first launch. Use `mclaw` to start again and `mclaw stop` to stop the backend. Keep the source directory when using an editable installation.

### Kaihong OS / M-Robots OS

The device needs Python 3.11+. The development computer needs Git, Node.js 22.12+, npm 10+, and [HDC](https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/dfx/hdc.md). Build the frontend on the computer, not on the device.

**1. Download and build on the development computer.**

```powershell
git clone https://github.com/tharsis741/mclaw.git
cd mclaw
npm --prefix webui ci
npm --prefix webui run build
```

**2. Copy the source and built assets to the device.**

Prepare the following layout under `C:\\deploy\\mclaw` on the computer:

```text
mclaw/
├── pyproject.toml
├── LICENSE
├── docs/
│   ├── README.zh-CN.md
│   ├── THIRD_PARTY_NOTICES.md
│   └── ACKNOWLEDGEMENTS.md
└── mclaw/                    # Complete Python package, including built web/static/
```

Preserve the `docs/` paths because package metadata references them. From PowerShell:

```powershell
hdc file send C:\\deploy\\mclaw /data/local/tmp/
hdc shell
```

**3. Install and start on the device.**

```sh
cd /data/local/tmp/mclaw
run python3 -m pip install -e .
run mclaw
```

Follow the initial page configuration or use `run mclaw setup` in the terminal. Use `run mclaw stop` to stop the backend. Retain the source directory after installation.

### Optional browser automation

In the original Python environment and source directory:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

On Linux, Chromium installation can use `python -m playwright install --with-deps chromium`.

See the [user manual](docs/manual/README.md) and [troubleshooting guide](docs/manual/故障排查.md) for further guidance.

## Updating M-Claw

Back up configuration and application data first. Inspect and resolve local source changes before pulling; do not overwrite uncommitted work.

### Windows / Linux

From the original source directory and Python environment:

```bash
mclaw stop
git status --porcelain
git pull --ff-only
python -m pip install -e .
npm --prefix webui ci
npm --prefix webui run build
mclaw
```

### Kaihong OS / M-Robots OS

**1. Update and build on the development computer.**

```powershell
git status --porcelain
git pull --ff-only
npm --prefix webui ci
npm --prefix webui run build
```

**2. Stop the device backend.**

```sh
run mclaw stop
```

**3. Refresh the deployment directory using the layout above**, including the complete newly built `mclaw/web/static/`, and send it to the same device location:

```powershell
hdc file send C:\\deploy\\mclaw /data/local/tmp/
```

**4. Update dependencies and restart on the device.**

```sh
cd /data/local/tmp/mclaw
run python3 -m pip install -e .
run mclaw
```

Updates reuse existing configuration and application data. See [updates and data recovery](docs/manual/更新与数据恢复.md) for backups, recovery, and post-update checks.

### Runtime requirements and limitations

- Python 3.11+ is required on the runtime host.
- Git on `PATH` is needed for Windows/Linux source management and Checkpoint/Rollback. Checkpoints are disabled when Git is unavailable; Kaihong OS / M-Robots OS currently disables checkpoints.
- Playwright-managed Chromium is required for browser automation.
- Run `mclaw doctor` to inspect the runtime, tools, and channels.
- Some built-in Skills may install additional toolchains on first use.

## Developer ecosystem

The upstream project plans an open Skill ecosystem around Skill Hub. Developers can create Skills for office work, industry, education, robotics, data analysis, smart homes, and domain knowledge. Local Skill management already supports installation, updates, reuse, and evolution.

## Source and license

This repository is synchronized from [m-robots/mclaw on AtomGit](https://atomgit.com/m-robots/mclaw). The original project author is Shenzhen Kaihong Digital Industry Development Co., Ltd. This GitHub repository preserves upstream commit history, copyright, and open-source notices, while maintaining an English homepage and Chinese documentation entry point.

Synchronization baseline: upstream `v1.1.2`, commit `624cc6489e898a900ca244412c5bb6f818b715eb`.

M-Claw is licensed under Apache-2.0. See [LICENSE](LICENSE).

See [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) for third-party notices and [ACKNOWLEDGEMENTS.md](docs/ACKNOWLEDGEMENTS.md) for acknowledgements.
