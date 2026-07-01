<p align="center">
  <img src="docs/assets/rich%20logo.png" alt="M-CLAW Rich Logo" width="100%">
</p>

<h2 align="center">自进化空间智能体<br>Self-Evolving Robot Intelligence</h2>

<p align="center">
  <a href="#安装与启动">快速开始</a> ·
  <a href="#能力概览">功能</a> ·
  <a href="#智能体循环">架构</a> ·
  <a href="#开发者生态">开发</a>
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.0.0-2f80ed">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-green">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Kaihong%20OS%20%7C%20M--Robots%20OS-6f42c1">
</p>

**M-Claw 是面向具身智能、端侧运行时与机器人协作场景设计的 Agent Runtime，旨在将模型、工具、记忆、Skill、安全策略、任务调度与设备控制组织为一个可运行的机器人行动系统，并探索“一机一脑，多脑协同”的架构，让 Agent 从对话走向真实任务，从软件工具走向物理设备。**

> 当前版本：1.0.0  
> 开源协议：Apache-2.0  
> 当前安装方式：源码安装

## 当前能力

**1.0.0 聚焦“智能体运行时”的最小可用闭环：在本地桌面与机器人控制场景中，让 M-CLAW 能够安全地读取上下文、调用工具、执行命令、沉淀记忆、加载 Skill，并通过 Skill 连接真实机器人设备。**

它的系统边界可以概括为：

- **Agent Runtime**：负责模型调用、工具调度、上下文压缩、会话状态、记忆召回、后台审查和中断恢复。
- **Tool Runtime**：提供文件、终端、后台进程、搜索、视觉、浏览器、凭据、通道和子代理委派能力。
- **Skill Runtime**：将可复用流程封装为 Skill，并通过 `SKILL.md`、`mclaw_skill.yaml` 和 `skill_evolution.json` 管理能力记忆。
- **Safety Runtime**：通过 PathPolicy、Scoped Secret、Checkpoint、Rollback 和 Operation Journal 降低本地执行风险。

## Roadmap

**M-Claw 将从 1.0.0 的单机智能体运行时，逐步演进为面向分布式机器人群体智能的操作底座。**

### 2026.06 | **1.0.0 桌面端原型版发布**

完成原型能力展示，验证M-Claw原型版在 M-Robots OS 上运行、执行日常办公与开发任务，并形成“智能体运行时 + 机器人控制”的最小可用闭环。

### 2026.09 | **1.1.0 多机协同**

探索多 M-Claw Runtime 之间的智能体协同协议，让不同机器人能够交换任务、状态和能力，在保持本地自治的同时完成协作任务。同时引入动作边界与安全验证能力，并探索“脑-小脑”异步控制架构：M-Claw Runtime 作为机器人本地大脑，负责理解意图、规划任务和协同决策；底层控制模块作为小脑，负责高频、稳定、实时的动作执行。

### 2026.10 | **1.2.0 Mycelium 机器人控制中心以及生态建设**

v1.2.0 将推出 M-CLAW Mycelium 机器人控制中心，统一管理机器人状态、任务队列、Skill 调用、后台执行、任务恢复与失败反馈，让机器人任务从一次性指令执行升级为可持续调度、可复盘、可演化的任务流。

同时，M-Claw 将引入 Skill Hub 生态机制，让开发者可以围绕机器人动作、办公流程、行业工具和设备控制发布、安装、更新和复用 Skill。

### 2026.12 | **2.0.0 空间智能体**

面向空间记忆、位置建模、跨机器人协同调度和万物互联集群治理发展。M-Claw 将从单一设备控制走向空间级智能体，使 Agent 能够理解环境、记忆位置、协调多台机器人和多类智能设备，在真实空间中完成更复杂的自动化任务。

## 能力概览

M-Claw 当前已经包含：

- **21 个基础工具**：默认工具集，覆盖凭据授权、文件读写、终端、记忆、Skill、历史会话检索和子代理委派。
- **13 个可选工具**：覆盖联网搜索、视觉分析、浏览器自动化、微信和钉钉通道发送能力。
- **22 个内置命令**：包括模型切换、搜索后端、语音输入、历史会话、rollback、checkpoint、定时任务、Skill 管理和运行环境诊断。
- **模型库与多 Provider 管理**：支持 OpenAI-compatible 与 Anthropic Messages 两类调用协议，并通过 models.dev 获取模型目录与上下文元数据。
- **三层记忆体系**：会话级记忆、长期记忆和 Skill 级运行/优化记忆。
- **系统运行时适配**：针对M Roboots OS以及Kaihong OS的路径策略、运行环境变量、shell profile 和运行域能力过滤。

## 模型支持

**M-Claw 支持在当前会话中动态切换模型和 Provider，并在不改动 Agent Loop 的情况下进入同一套运行流程。**

- **模型目录**：基于 [models.dev](https://models.dev/) 获取模型 ID、Provider 归属和上下文长度等元数据；当前公开目录包含 145 个 provider、5,246 条模型记录。
- **Provider 配置**：内置 29 个可配置 provider key，覆盖主流国内外模型服务、托管平台和路由器，并支持用户自定义 OpenAI-compatible/Anthropic message endpoint。
- **协议调用**：根据 Provider 使用 OpenAI-compatible 或 Anthropic Messages 接口，支撑模型切换、上下文预算、流式输出和工具调用流程。

不同模型的实际可用能力取决于 Provider endpoint 与模型自身对工具调用、流式输出、视觉输入等能力的支持。

首次启动建议通过 `mclaw setup` 配置默认模型和 Provider。CLI、微信网关和钉钉网关都会从当前配置解析可用模型、Provider、API Key 和自定义 endpoint；如果缺少模型或密钥，会提示继续配置，不会隐式注入内置默认模型。

## 智能体循环

```text
用户输入 / 通道消息 / 定时任务
        ↓
构建系统上下文：平台信息、工具列表、Skill 索引、长期记忆
        ↓
调用模型：OpenAI-compatible 或 Anthropic Messages 接口
        ↓
解析工具调用：文件、终端、搜索、Skill、记忆等
        ↓
执行工具：读操作可并发，写操作进入 checkpoint 和安全策略
        ↓
写入会话、裁剪上下文、必要时压缩历史
        ↓
继续循环，直到得到最终结果或遇到可解释阻塞
        ↓
后台触发记忆审查与 Skill 演化审查
```

**先理解需求，再读取资料、调用工具、验证结果、保留状态，并在任务结束后把可复用经验沉淀到记忆或 Skill 中。**

## 智能体工具

**M-Claw 提供了基础工具集和可选工具集。运行时会根据平台能力、配置和凭据状态过滤可用工具。**

### 基础工具集

| 工具集 | 工具 | 能力 |
| --- | --- | --- |
| credentials | `secret_request_many` | 为 Skill、工具、runtime 或 channel 请求 scoped secret，密钥保存到 M-Claw home `.env`，不会返回明文。 |
| terminal | `terminal`, `process` | 执行命令、维护会话工作目录、启动后台进程、轮询日志、等待或终止进程。 |
| file | `read_file`, `write_file`, `patch`, `edit_file`, `delete_file`, `search_files`, `list_directory` | 读取、写入、精确替换、删除、搜索和列目录，受运行时路径策略约束。 |
| memory | `memory_read`, `memory_add`, `memory_replace`, `memory_remove` | 管理长期记忆和用户画像记忆，写入前执行注入与泄露风险检查。 |
| skills | `skills_list`, `skill_tree`, `skill_view`, `skill_search`, `skill_manage` | 发现、查看、搜索、创建、安装、校验、编辑和自进化 Skill。 |
| session_search | `session_search` | 基于 SQLite + FTS5 检索历史会话，支持按关键词召回过去任务。 |
| delegation | `delegate_task` | 启动最多 5 个隔离子代理处理独立子任务，默认只开放终端和文件能力。 |

### 可选工具集

| 工具集 | 工具 | 能力 |
| --- | --- | --- |
| web | `web_search` | 通过 Tavily 或 DashScope/Qwen 搜索实时信息，返回带来源的综合结果。 |
| vision | `vision_analyze` | 使用 Qwen 视觉模型分析 URL 或本地图片。 |
| browser | `browser_navigate`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_press`, `browser_download` | 基于 Playwright 的浏览器自动化，支持导航、快照、点击、输入、滚动、截图和下载。 |
| weixin | `weixin_send_file` | 在当前微信会话中发送文件。 |
| dingtalk | `dingtalk_send_text`, `dingtalk_send_file` | 在当前钉钉会话中发送文本或文件。 |

联网搜索默认使用 `auto` 后端：配置 Tavily 密钥时优先使用 Tavily，否则使用 DashScope/Qwen；当 Tavily 调用失败且 DashScope 凭据可用时，运行时会自动降级到 DashScope。可在交互式界面中使用 `/search-backend dashscope|tavily|auto` 查看或切换后端。

## 三层记忆体系

**M-Claw 将记忆视为分层的运行知识，而不是单一的聊天历史缓存。**

| 记忆层 | 作用 | 典型实现 |
| --- | --- | --- |
| 会话记忆 | 保存会话、消息、工具调用、token 用量、任务标题和可搜索历史。 | SQLite state store、FTS5、`session_search` |
| 长期记忆 | 保存稳定事实、用户偏好、环境约定、工具习惯和排障经验。 | `MEMORY.md`、`USER.md`、Memory Tools |
| Skill 记忆 | 保存可复用流程、失败经验、运行注意事项和技能演化记录。 | `SKILL.md`、`mclaw_skill.yaml`、`skill_evolution.json` |

**三层记忆结构让 M-Claw 不只是在一次会话中完成任务，而是能够持续积累“下次如何更好地执行”的经验。**

## Skill as Memory

**1.0.0版本定义了Skill级的记忆系统，并在运行时中长期维护。**

每个 Skill 可以包含：

- `SKILL.md`：主要执行说明，定义 Skill 做什么、何时使用、如何执行。
- `mclaw_skill.yaml`：Skill 元数据。
- `skill_evolution.json`：Skill 的经验记录，包括适配摘要、用户偏好、已知失败和运行注意事项。
- `scripts/`、`references/`、模板或资源文件：可选的执行资产。

所有 Skill 写操作都通过 `skill_manage` 完成。普通文件工具和终端工具会阻止直接修改 M-Claw 管理的 Skill 存储，避免能力包被绕过校验地改坏。

**M-Claw 1.0.0 已经具备 Skill 演化基础：**

- `skill_manage(action="evolution_update")` 可更新 Skill 演化记录。
- 后台审查线程可在配置轮次后触发记忆审查和 Skill 演化审查。
- 后台审查仅开放受限工具白名单，降低自动写入 Skill 的风险。


## 安全策略与可恢复执行


### PathPolicy

运行时会根据平台对路径进行分类，包括 workspace、exchange、runtime、system、device、home、tmp 等范围。系统路径和设备路径默认限制写入或执行，KaihongRuntime 还对 `/proc`、`/sys`、`/dev`、系统目录和交换目录做了专门策略。

### Scoped Secret

凭据不会通过运行时直接暴露给模型。`secret_request_many` 只请求环境变量名和用途，保存后通过 `required_for` 作用域注入给对应 Skill、工具、runtime 或 channel。

### Checkpoint

CheckpointManager 使用单个共享 shadow git store，在文件写入、patch、edit、delete 或破坏性终端操作前创建透明快照。它不会把 git 状态写进用户项目目录，而是将 checkpoint 存储在 M-Claw home 下。

### Rollback

RollbackCoordinator 基于操作日志恢复文件、创建冲突备份，并可同步回滚会话上下文。CLI 提供 `/rollback` 和 `/checkpoints` 命令查看、预览、撤销和恢复变更。

### 当前缺陷

**M-Claw 1.0.0 的安全机制仍以路径策略、凭据作用域和可恢复执行为主，尚未形成统一的高风险操作确认、沙箱隔离和权限分级体系。**

## 系统运行时适配

**M-Claw 通过系统信息动态适配运行时：**

- Windows：使用 WindowsRuntime。
- Linux：使用LinuxRuntime。
- Kaihong OS/M-Robots OS/OpenHarmony：识别 Kaihong/M-Robots OS/OpenHarmony 主机后使用 KaihongRuntime。

KaihongRuntime 会禁用 checkpoint、桌面宠物和浏览器自动化，并保留其他基础能力。

## 通道、语音与定时任务

M-Claw 支持以下交互入口：

- **CLI/TUI**：基于 Rich 与 prompt_toolkit 的交互式终端体验。
- **Voice Input**：通过 Qwen realtime ASR 接入语音输入，支持 wake word、push-to-talk 和一次性录音模式。
- **Scheduler**：本地定时任务引擎，支持 due detection、queued run、并发策略、失败计数、输出文件和投递结果。
- **Weixin / DingTalk Channels**：通过 channel runner 将外部消息映射到 M-Claw session，并把结果发回对应通道。

`/schedule` 支持一次性、每日、每周、每月、间隔和 cron 任务。每周任务的 weekday 可填写英文星期或数字：`1` 到 `7` 对应 Monday 到 Sunday，`0` 按 Monday 处理；每月任务的日期超过当月天数时，会在执行时落到当月最后一天。定时任务投递到微信或钉钉前，需要先完成 `mclaw weixin login` 或 `mclaw dingtalk login`，并保持对应网关进程运行。

## 命令入口

```bash
mclaw                         # 启动交互式 Agent Runtime
mclaw setup                   # 配置模型、工具、通道和可选能力
mclaw doctor                  # 检查核心运行时、工具能力和 IM 通道状态
mclaw help                    # 查看命令指引
mclaw resume                  # 恢复最近一次会话
mclaw resume <session_id>     # 按会话 ID 或前缀恢复会话
mclaw weixin login            # 微信 iLink Bot 扫码登录
mclaw weixin                  # 启动微信私聊网关
mclaw dingtalk login          # 配置钉钉 Stream 网关凭据
mclaw dingtalk check          # 检查钉钉 Stream 网关依赖和配置
mclaw dingtalk                # 启动钉钉 Stream 网关
```

## 安装与启动

**当前以源码形式分发，请在源码目录中安装。**

### Windows / Linux

```powershell
git clone https://gitcode.com/m-robots/mclaw.git
cd mclaw
```

如果本机尚未安装 Git，可使用以下方式安装：

- 官方下载：[git-scm.com/download/win](https://git-scm.com/download/win)
- 国内镜像：[华为云 Git for Windows 镜像](https://mirrors.huaweicloud.com/git-for-windows/)

安装完成后确认 Git 可用：

```powershell
git --version
```

安装 Python 依赖：

```powershell
pip install -e .
```

安装浏览器自动化运行时：

```powershell
python -m playwright install chromium
```

首次配置：

```powershell
mclaw setup
```

启动命令：

```powershell
mclaw
```

### Kaihong OS / M-Robots OS

```bash
run mclaw setup
```

启动命令：

```bash
run mclaw
```

要求：

- Python 3.11 或更新版本。
- Windows/Linux 桌面环境建议安装 Git，并确保 `git` 已加入 `PATH`。源码获取、版本管理以及 Checkpoint/Rollback 会使用 Git；未安装时 checkpoint 能力会被禁用。Kaihong OS/M-Robots OS当前禁用 checkpoint，不要求 Git。
- 如需使用浏览器自动化，需要安装 Playwright 管理的 Chromium。
- 安装完成后可运行 `mclaw doctor` 验证核心运行时、工具能力和 IM 通道状态。若 Browser Tools 显示未配置，请确认 Chromium 安装命令运行在安装 M-Claw 的同一个 Python 环境中。
- 部分内置 Skill 可能仍需要按场景安装额外工具链。

## 开发者生态

M-Claw 将围绕 Skill Hub 构建开放技能生态。开发者可以面向办公、工业、教育、机器人、数据分析、智能家居和行业知识等方向开发 Skill，并通过本地 Skill 管理能力完成安装、更新、复用和演化。

## License

Apache-2.0. See `LICENSE` for details.
