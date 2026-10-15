# M-Claw Web UI

M-Claw 的 React / TypeScript 前端，与终端界面共用 Python 应用后台。使用说明见 [Web 界面](../docs/manual/18-Web界面.md)。

## 环境要求

- Python 3.11+，使用已安装 M-Claw 的 Python 环境。
- Node.js 22.12+、npm 10+，用于开发和构建。
- 生产运行由 Python 服务提供静态页面，目标设备无需安装 Node.js。

## 目录结构

| 路径                                      | 内容                                           |
| ----------------------------------------- | ---------------------------------------------- |
| `src/components/`                         | 页面、对话输入、选择器和交互组件               |
| `src/api.ts`、`src/types.ts`              | HTTP / WebSocket 客户端和数据类型              |
| `src/skill-market-api.ts`                 | 技能市场 HTTP 客户端和榜单、审核数据类型       |
| `src/*.test.*`、`src/components/*.test.*` | Vitest 交互测试                                |
| `public/`                                 | 产品图片、供应商及渠道图标、第三方许可证       |
| `../mclaw/application/`                   | 会话、任务、配置和设备等共享业务服务           |
| `../mclaw/web/`                           | 本机 HTTP / WebSocket 服务、认证和静态资源适配 |

后台负责会话控制权、队列、取消与恢复、权限请求和持久化。前端提交操作并呈现后台状态；重连后以服务端快照恢复会话。Web 与 TUI 保留各自的输入和呈现方式。

## 构建与检查

在本目录执行：

```sh
npm ci
npm run typecheck
npm test
npm run format:check
npm run build
```

`npm run build` 生成 `../mclaw/web/static/`，构建目标为 ES2020。返回仓库根目录后运行 `mclaw` 打开页面，运行 `mclaw cli` 使用终端界面。

`npm run dev` 启动 Vite 热更新服务。开发服务器只监听本机，`/api` 代理目标位于 `vite.config.ts`，需与 Python 后台的监听端口一致；HTTP 和 WebSocket 请求均沿用后台认证。

## 打包

发布包应从干净的代码检出目录构建。在仓库根目录执行：

```sh
npm --prefix webui ci
npm --prefix webui run build
python -m pip wheel . --no-deps -w dist
```

前端资源通过 `pyproject.toml` 的 package-data 配置进入 wheel。打包后应核对包内 `mclaw/web/static/` 与本次前端构建一致。

Git 保存源码、测试、`package-lock.json`、构建配置和 `public/` 中的正式资源。`node_modules/`、`*.tsbuildinfo`、静态构建目录和 Python 打包产物由构建命令生成，已配置忽略规则。

## 开发约定

- 通过 `api.ts` 及功能专用客户端（如 `skill-market-api.ts`）访问后台，为请求、响应和事件维护明确的 TypeScript 类型。
- API Key 等凭据由后台保存；页面仅保留提交所需的短期表单值。
- 保持现有配置、技能和历史数据兼容，涉及共享服务时同时验证 Web 与 TUI。
- 组件测试使用隔离数据和模拟服务；平台发布前分别检查目标浏览器的输入、文件、录音、重连和设备协同。
- 产品资源来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
