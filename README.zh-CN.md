<p align="center">
  <img src="docs/assets/mclaw%20logo.png" alt="M-Claw Logo" width="100%">
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

**M-Claw 是面向具身智能与机器人协作的 Agent Runtime，连接语言模型与物理世界执行系统，将模型能力、工具链、空间记忆与设备控制组织为统一运行时，使 Agent 能够直接在真实环境中完成任务，并支持“一机一脑、多脑协同”的分布式智能架构。**

> 当前版本：1.0.0  
> 开源协议：Apache-2.0  
> 当前安装方式：源码安装

## Roadmap

### 2026.06 | **1.0.0 单机智能体运行时**

完成最小智能体运行时闭环，在 M-Robots OS 上验证“模型驱动执行 + 工具调用 + 记忆 + Skill 扩展 + 机器人控制”的端到端执行链路。

### 2026.09 | **1.1.0 分布式运行时协同网络**

引入多 M-CLAW Runtime 协同机制，构建分布式执行网络，支持不同设备/机器人之间的任务分发、状态同步与能力共享。

同时建立“脑-小脑”分层控制架构：

- 运行时（大脑）：负责意图理解、任务规划与跨节点协同决策。
- 控制模块（小脑）：负责高频、稳定、实时的动作执行。


### 2026.10 | **1.2.0 Mycelium 控制平台与 Skill 生态**

构建统一任务控制平台（Mycelium Control Panel），实现对机器人、设备、Skill 与执行任务的集中调度与生命周期管理，使任务从“单次执行”升级为“可持续调度与可演化任务流”。

同时推出 Skill Hub 生态系统，支持 Skill 的发布、安装、更新与复用，使 M-CLAW 从执行运行时扩展为具备开发者生态的具身智能基础设施。

### 2026.12 | **2.0.0 空间智能体运行时**

面向空间记忆建模、环境感知与多机器人协同调度，M-CLAW 从单设备 Agent Runtime 演进为空间智能体运行时，使系统能够在统一空间表示下理解环境状态、维护位置记忆，并在多机器人与多设备之间进行任务协调与执行编排。
在该阶段，M-CLAW 引入“空间作为运行时状态”的核心抽象，将物理环境建模为可计算的空间结构（Spatial State），并在此基础上实现跨设备的任务分解、路径规划与协同执行。

能力演进包括：

- 空间记忆：构建环境与位置的持续性记忆，使 Agent 能够在时间维度上累积空间状态与变化。
- 空间建模：将物理环境抽象为可计算的空间图结构，用于支持定位、关系建模与任务规划。
- 跨机器人调度：在统一空间状态下进行任务分配与执行协调，实现多设备协同操作。
- 分布式空间执行：支持多 Agent 在统一空间语义下进行协同执行与状态同步，形成空间级任务网络。

## 当前能力

**1.0.0 定义了 M-CLAW 的最小智能体运行时闭环，在本地桌面与机器人控制环境中提供统一执行框架，使 Agent 能够在同一运行时内完成上下文感知、工具调用、指令执行、记忆持久化与 Skill 扩展，并形成从语言输入到物理执行的闭环执行机制。**

M-CLAW 运行时架构由四个逻辑层构成：

- **Agent Runtime Core**：负责统一执行循环与状态管理，驱动模型推理、任务调度与工具编排，并维护会话状态、记忆生命周期与执行恢复机制。
- **Tool Runtime**：提供面向外部环境的执行接口，包括系统操作、文件与进程管理、网络与浏览器交互以及子代理调用等，用于支持 Agent 与数字世界的交互能力。
- **Skill Layer**：提供可复用能力单元的抽象与运行时加载机制，将复杂任务流程模块化，并作为扩展接口连接机器人设备与物理执行能力。
- **Safety Runtime**：作为跨层执行控制与约束机制，对所有运行时操作进行策略约束、执行审计与状态回滚控制，以降低本地与物理执行风险。

## 能力概览

M-Claw 当前已经包含：

- **21 个基础工具**：默认工具集，覆盖凭据授权、文件读写、终端、记忆、Skill、历史会话检索和子代理委派。
- **13 个可选工具**：覆盖联网搜索、视觉分析、浏览器自动化、微信和钉钉通道发送能力。
- **22 个内置命令**：包括模型切换、搜索后端、语音输入、历史会话、rollback、checkpoint、定时任务、Skill 管理和运行环境诊断。
- **模型库与多 Provider 管理**：支持 OpenAI-compatible 与 Anthropic Messages 两类调用协议，并通过 models.dev 获取模型目录与上下文元数据。
- **三层记忆体系**：会话级记忆、长期记忆和 Skill 级运行/优化记忆。
- **系统运行时适配**：针对M Roboots OS以及Kaihong OS的路径策略、运行环境变量、shell profile 和运行域能力过滤。

## 模型支持

**M-Claw 支持在当前会话中动态切换模型和 Provider。**

- **模型目录**：基于 [models.dev](https://models.dev/) 获取模型 ID、Provider 归属和上下文长度等元数据；当前公开目录包含 145 个 provider、5,246 条模型记录。
- **Provider 配置**：适配31个国内外主流供应商，并支持用户自定义 OpenAI-compatible/Anthropic message endpoint接口。
- **协议调用**：根据 Provider 使用 OpenAI-compatible 或 Anthropic Messages 接口，支撑模型切换、上下文预算、流式输出和工具调用流程。

不同模型的实际可用能力取决于 Provider endpoint 与模型自身对工具调用、流式输出、视觉输入等能力的支持。

首次启动建议通过 `mclaw setup` 配置默认模型和 Provider。CLI、微信网关和钉钉网关都会从当前配置解析可用模型、Provider、API Key 和自定义 endpoint；

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

**M-CLAW 1.0.0 定义了 Skill 作为运行时可演化的能力单元，用于封装可复用的任务执行逻辑，并在运行过程中持续积累执行经验与行为优化信息。**

### Skill 结构定义
每个 Skill 由以下组成：

- `SKILL.md（执行语义层）`：定义 Skill 的功能边界、适用场景与执行逻辑，是 Skill 的行为定义层。
- `mclaw_skill.yaml（元数据层）`：描述 Skill 的基本信息。
- `skill_evolution.json（演化记忆层）`：记录 Skill 在运行过程中的经验状态，包括适配结果、用户偏好、失败案例与执行注意事项，是 Skill 的运行时经验沉淀层。
- `执行资产`：包括 scripts、references、模板或其他运行时资源，用于支持 Skill 的实际执行能力。

### Skill 写入与治理机制

所有 Skill 写操作都通过 `skill_manage` 工具集完成。禁止通过普通文件工具和终端工具直接修改 Skill 存储，以确保能力单元的一致性与安全性。

### Skill 能力演化

- `skill_manage(action="evolution_update")` 用于更新 Skill 的运行时经验状态。
- 后台审查机制周期性触发 Skill 使用情况分析与演化建议生成。
- 后台线程仅允许访问受限工具白名单，以降低自动修改 Skill 的风险。


## 安全策略与可恢复执行


### PathPolicy

文件与终端工具使用当前操作系统用户的主机权限，并由 PathPolicy 保护凭据文件、MCLAW_HOME、系统关键目录和危险递归操作。首次使用工作目录时需要完成风险确认。

### Scoped Secret

凭据不会通过运行时直接暴露给模型。`secret_request_many` 只请求环境变量名和用途，保存后通过 `required_for` 作用域注入给对应 Skill、工具、runtime 或 channel；未授权的敏感环境变量会从子进程删除，命令输出中的已授权值会被脱敏。

### Checkpoint

CheckpointManager 使用单个共享 shadow git store，在文件写入、patch、edit、delete 或破坏性终端操作前创建透明快照。它不会把 git 状态写进用户项目目录，而是将 checkpoint 存储在 M-Claw home 下。

### Rollback

RollbackCoordinator 基于操作日志恢复文件、创建冲突备份，并可同步回滚会话上下文。 内置命令提供 `/rollback` 和 `/checkpoints` 命令查看、预览、撤销和恢复变更。

### 当前缺陷

**M-Claw 1.0.0 不提供安全沙箱或权限分级。Full access 模式下的凭据与破坏性操作保护是降低误操作风险的防护栏，不能替代操作系统级隔离。**

## 系统运行时适配

**M-Claw 通过系统信息动态适配运行时：**

- Windows：使用 WindowsRuntime。
- Linux：使用LinuxRuntime。
- Kaihong OS/M-Robots OS/OpenHarmony：识别 Kaihong/M-Robots OS/OpenHarmony 主机后使用 KaihongRuntime。

KaihongRuntime 会禁用 checkpoint、桌面宠物和浏览器自动化，并保留其他基础能力。

## 通道、语音与定时任务

M-Claw 支持以下交互入口：

- **CLI/TUI**：基于 Rich 与 prompt_toolkit 终端交互入口。
- **Voice Input**：通过 Qwen realtime ASR 接入语音输入，支持 wake word、push-to-talk 和一次性录音模式。
- **Weixin / DingTalk Channels**：通过 channel runner 将外部消息映射到 M-Claw session，并把结果发回对应通道。


## 终端命令入口

```bash
mclaw                         # 启动Agent Runtime
mclaw setup                   # 首次配置
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
- Windows/Linux 桌面环境建议安装 Git，并确保 `git` 已加入 `PATH`。源码获取、版本管理以及 Checkpoint/Rollback 会使用 Git；未安装时 checkpoint 能力会被禁用。Kaihong OS/M-Robots OS当前禁用 checkpoint，不要求安装 Git。
- 如需使用浏览器自动化，需要安装 Playwright 管理的 Chromium。
- 安装完成后可运行 `mclaw doctor` 验证核心运行时、工具能力和 IM 通道状态。若 Browser Tools 显示未配置，请确认 Chromium 安装命令运行在安装 M-Claw 的同一个 Python 环境中。
- 部分内置 Skill 可能仍需要按场景安装额外工具链。

## 开发者生态

M-Claw 将围绕 Skill Hub 构建开放技能生态。开发者可以面向办公、工业、教育、机器人、数据分析、智能家居和行业知识等方向开发 Skill，并通过本地 Skill 管理能力完成安装、更新、复用和演化。

## License

M-Claw 基于 Apache-2.0 发布，详见 [LICENSE](LICENSE)。

第三方开源声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

项目致谢见 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)。
