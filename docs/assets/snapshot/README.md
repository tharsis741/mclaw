# M-Claw 操作截图

本目录保存 Windows 与 Kaihong BotBook 上实际运行的 M-Claw 页面截图。图片中的布局、设备和列表内容对应采集时的真实界面。

## Kaihong BotBook

| 项目 | 记录 |
| --- | --- |
| 采集日期 | 2026-10-15 |
| 设备 | Kaihong BotBook（KHP-LC802） |
| 系统 | KaihongOS 5.0.1.52，OpenHarmony 5.0.2.123，API 14 |
| M-Claw 版本 | 1.1.2 |
| 浏览器 | 开鸿系统浏览器 `com.kaihong.browser` |
| 页面尺寸 | 1920 × 918 |
| 采集方式 | 在设备浏览器操作实际页面，通过系统截图工具采集，再裁剪至页面区域 |

| 文件 | 内容 |
| --- | --- |
| [kaihong-overview.png](kaihong-overview.png) | 主界面，用于项目说明与开始使用章节 |
| [kaihong-devices.png](kaihong-devices.png) | 已启用的设备协同与实际发现的附近设备 |

采集时间、原始截图尺寸、裁剪范围和文件校验信息见 [kaihong-capture-info.json](kaihong-capture-info.json)。

## Windows

22 张 JPEG 于 2026-10-15 从 Windows 本机运行的 M-Claw 页面直接采集。

| 项目 | 记录 |
| --- | --- |
| 系统 | Windows |
| 浏览器 | Google Chrome 155.0.8059.40 |
| 页面尺寸 | 1920 × 855 |
| 采集时程序版本 | 1.1.1，源码提交 `1bc407c795264f1989d115625d894dcc4eabe7fb` |
| 本机服务 | `http://127.0.0.1:18031/` |
| 构建环境 | Python 3.12.8、Node.js 24.14.0、npm 11.9.0 |
| 采集方式 | 在现有 Chrome 标签页操作实际页面，保存原始 JPEG 截图 |

图片中的模型、榜单、技能、计划和诊断状态对应采集时刻。新闻回复用于展示对话界面，内容由当时任务产生；行情、排名或新闻事实应回到相应来源核对。

供应商配置保留界面的凭据遮蔽。编辑弹窗采集后关闭，未提交修改。专用视觉模式图片展示表单选项，不表示已改变本机当前模式。

### 图片索引

| 文件 | 内容 |
| --- | --- |
| [webui-overview.jpg](webui-overview.jpg) | 首页与主要导航 |
| [webui-setup.jpg](webui-setup.jpg) | 从设置中重新打开的向导欢迎页 |
| [webui-conversation.jpg](webui-conversation.jpg) | 已有新闻简报任务的回复 |
| [webui-task-process.jpg](webui-task-process.jpg) | 同一任务展开的执行过程 |
| [webui-model-picker.jpg](webui-model-picker.jpg) | 会话模型选择器 |
| [webui-devices.jpg](webui-devices.jpg) | Windows 设备信息与协同状态 |
| [webui-schedules.jpg](webui-schedules.jpg) | 已有定时计划列表 |
| [webui-schedule-editor.jpg](webui-schedule-editor.jpg) | 已有计划的编辑表单 |
| [webui-tools.jpg](webui-tools.jpg) | 工具列表与启用状态 |
| [webui-vision-main.jpg](webui-vision-main.jpg) | 图片理解：当前模型模式 |
| [webui-vision-dedicated.jpg](webui-vision-dedicated.jpg) | 图片理解：专用模型表单（未保存） |
| [webui-skills.jpg](webui-skills.jpg) | 本机已安装技能 |
| [webui-skill-detail.jpg](webui-skill-detail.jpg) | internal-comms 技能说明 |
| [webui-market.jpg](webui-market.jpg) | SkillHub 中文榜单 |
| [webui-market-en.jpg](webui-market-en.jpg) | skills.sh 英文榜单 |
| [webui-market-detail.jpg](webui-market-detail.jpg) | 中文市场技能详情与第三方审核 |
| [webui-settings.jpg](webui-settings.jpg) | 常规设置 |
| [webui-providers.jpg](webui-providers.jpg) | 已添加供应商 |
| [webui-provider-settings.jpg](webui-provider-settings.jpg) | 供应商配置表单 |
| [webui-task-settings.jpg](webui-task-settings.jpg) | 任务执行设置 |
| [webui-channels.jpg](webui-channels.jpg) | 微信和钉钉渠道状态 |
| [webui-diagnostics.jpg](webui-diagnostics.jpg) | 本机运行检查结果 |

每张图片的采集时间记录在 [capture-info.json](capture-info.json)。

## 图片维护

1. 使用待发布版本的前后端，在目标设备浏览器打开对应模块。
2. 等待列表、详情与异步状态加载完成；选择可公开的会话与页面。
3. 检查 API Key、登录二维码、连接令牌与私人内容，保持敏感字段遮蔽。
4. 保存与实际编码相符的图片格式，更新采集记录、手册图片说明和相对链接。
5. 在文档预览中检查文字、缩放、裁切和链接；有明显界面变化时重新采集。

原有的 [vision-settings-main.png](vision-settings-main.png) 和 [vision-settings-dedicated.png](vision-settings-dedicated.png) 保留在本目录。当前手册使用上表中的新采集视觉设置图片。
