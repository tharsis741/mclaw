# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render plain slash-command output through shared panel models."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from mclaw.cli.runtime.panels import (
    PanelColumn,
    PanelModel,
    command_block,
    key_value_block,
    notice_panel,
    panel_cell,
    section_block,
    spacer_block,
    table_block,
    text_block,
)
from mclaw.cli.tui.panel_renderer import render_panel_model
from mclaw.cli.tui.renderers.status import format_duration


class CommandsRenderer:
    """Render simple slash command outputs through a shared TUI boundary."""

    def __init__(
        self,
        *,
        printer: Callable[[str], None],
        run_external_output: Callable[[Callable[[], None]], None] | None = None,
        box_factory: Callable[[], object] | None = None,
        panel_sink: Callable[[PanelModel], None] | None = None,
    ):
        self._printer = printer
        self._run_external_output = run_external_output
        self._box_factory = box_factory
        self._panel_sink = panel_sink

    def line(self, text: str = "") -> None:
        self._printer(text)

    def lines(self, lines: list[str]) -> None:
        for line in lines:
            self._printer(line)

    def _panel_model(self, panel: PanelModel, *, border_style: str | None = None) -> None:
        if self._panel_sink is not None:
            self._panel_sink(panel)
            return
        render_panel_model(
            panel,
            printer=self._printer,
            border_style=border_style,
            box=self._box_factory() if self._box_factory is not None else None,
            run_external_output=self._run_external_output,
        )

    def render_notice(
        self,
        title: str,
        message: str,
        *,
        detail: str | None = None,
        kind: str = "info",
    ) -> None:
        self._panel_model(
            notice_panel(
                title,
                message,
                detail=detail,
                tone=kind if kind in {"success", "warning", "danger", "info"} else "info",
            )
        )

    def render_text_panel(
        self,
        title: str,
        message: str,
        *,
        detail: str | None = None,
        kind: str = "info",
        namespace: str = "command",
    ) -> None:
        blocks = [text_block(message or "")]
        if detail:
            blocks.extend([spacer_block(), section_block("提示"), text_block(detail)])
        self._panel_model(PanelModel(
            title=title,
            blocks=tuple(blocks),
            tone=kind if kind in {"success", "warning", "danger", "info"} else "info",
            namespace=namespace,
        ))

    def render_help(self, *, push_to_talk_label: str) -> None:
        self._panel_model(PanelModel(
            title="M-Claw 命令中心",
            namespace="help",
            blocks=(
                section_block("常用命令"),
                command_block([
                    ("/help", "显示命令列表"),
                    ("/model <名称>", "切换模型，缺少密钥时提示配置"),
                    ("/model-update", "刷新和查看 models.dev 模型库缓存"),
                    ("/provider", "查看供应商和密钥状态"),
                    ("/skills", "查看和使用技能"),
                    ("/usage", "查看本次会话 Token 用量"),
                    ("/doctor [fix]", "检查运行环境和工具可用性"),
                    ("/history", "查看历史会话"),
                    ("/resume <id>", "恢复历史会话"),
                    ("/title <名称>", "设置会话标题"),
                    ("/clear", "清屏并开启新会话"),
                    ("/save", "导出会话 JSON"),
                    ("/quit", "退出 M-Claw"),
                ]),
                spacer_block(),
                section_block("文件安全层"),
                command_block([
                    ("/rollback", "查看最近可撤销文件变更"),
                    ("/rollback 1", "撤销第 1 条变更"),
                    ("/rollback undo", "恢复最近一条已撤销变更"),
                    ("/checkpoints status", "查看 checkpoint 存储状态"),
                    ("/checkpoints prune", "清理过期或孤儿 checkpoint"),
                ]),
                spacer_block(),
                section_block("语音与桌面宠物"),
                command_block([
                    ("/asr-mode [wake_word|push_to_talk]", "启用语音输入"),
                    ("/asr-once", "录制一次语音输入"),
                    ("/asr-status", "查看 ASR 状态"),
                    ("/keyboard-mode", "关闭 ASR，切回键盘输入"),
                    ("/pet [on|off|status|save|test]", "控制桌面宠物"),
                ]),
                spacer_block(),
                section_block("快捷键"),
                key_value_block([
                    ("发送", "Enter"),
                    ("换行", "Esc+Enter"),
                    ("中断", "Ctrl+C"),
                    ("退出", "Ctrl+D"),
                    ("清空输入", "Ctrl+U"),
                    ("光标/历史/补全", "Up/Down, Right/Ctrl+E, Tab"),
                    ("ASR 按键说话", push_to_talk_label),
                ]),
                spacer_block(),
                text_block("Ctrl+U = 清空当前输入 · Right/Ctrl+E = 接受历史建议 · Tab = 接受命令补全 · Up/Down = 光标/历史/补全导航", muted=True),
            ),
        ))

    def render_doctor(self, text: str) -> None:
        self._panel_model(_doctor_panel_model(text or "无输出"))

    def render_model_status(self, *, model: str, provider: str) -> None:
        self._panel_model(PanelModel(
            title="M-Claw 模型",
            blocks=(
                key_value_block([
                    ("当前模型", model),
                    ("供应商", provider),
                ]),
                spacer_block(),
                section_block("命令"),
                command_block([
                    ("/model <模型名> --provider <供应商>", "临时切换"),
                    ("/model <模型名> --provider <供应商> --profile <接口>", "指定接口类型"),
                    ("/model <模型名> --provider <供应商> --global", "全局保存"),
                    ("/model-update provider <供应商>", "查看接口类型和模型库"),
                    ("/provider", "查看内置/自定义供应商及密钥状态"),
                ]),
            ),
            tone="info",
        ))

    def render_model_key_prompt(self, *, provider_display_name: str, key_url: str | None = None) -> None:
        rows = [
            ("供应商", provider_display_name),
            ("状态", "API Key 尚未配置"),
            ("输入", "请在下方粘贴 API Key，然后按回车"),
            ("取消", "/cancel"),
        ]
        if key_url:
            rows.insert(2, ("获取密钥", key_url))
        self._panel_model(PanelModel(
            title="M-Claw 密钥配置",
            blocks=(key_value_block(rows),),
            tone="warning",
        ))

    def render_usage(self, *, agent, model: str, session_start: datetime) -> None:
        elapsed = (datetime.now() - session_start).total_seconds()
        total = agent.session_input_tokens + agent.session_output_tokens
        self._panel_model(PanelModel(
            title="M-Claw 会话用量",
            namespace="usage",
            blocks=(key_value_block([
                ("时长", format_duration(elapsed)),
                ("模型", model),
                ("API 调用", agent.session_api_calls),
                ("输入", f"{agent.session_input_tokens:,} tokens"),
                ("输出", f"{agent.session_output_tokens:,} tokens"),
                ("合计", f"{total:,} tokens"),
            ]),),
        ))

    def render_history(self, sessions: list[dict]) -> None:
        if not sessions:
            self._panel_model(PanelModel(
                title="M-Claw 历史会话",
                namespace="history",
                blocks=(key_value_block([("状态", "暂无会话记录")]),),
            ))
            return
        rows = []
        for session in sessions:
            sid = session["id"][:16]
            title = session.get("title") or session.get("preview", "")
            model = session.get("model", "?")
            user_messages = session.get("user_message_count", session.get("message_count", 0))
            rows.append((sid, model, str(user_messages), title))
        self._panel_model(PanelModel(
            title="M-Claw 历史会话",
            namespace="history",
            blocks=(
                table_block(
                    (
                        PanelColumn("会话", role="accent", no_wrap=True),
                        PanelColumn("模型", role="muted", no_wrap=True),
                        PanelColumn("用户消息", role="muted", justify="right", no_wrap=True),
                        PanelColumn("标题", role="primary"),
                    ),
                    rows,
                ),
                spacer_block(),
                text_block("使用 /resume <id> 恢复会话", muted=True),
            ),
        ))

    def render_providers(
        self,
        *,
        model: str,
        provider: str,
        base_url: str,
        api_mode: str,
        configured: list[dict],
        provider_registry: dict,
    ) -> None:
        blocks = [
            section_block("当前接入"),
            key_value_block([
                ("当前模型", model),
                ("当前接入方", provider),
                ("接口地址", base_url or "(默认)"),
                ("调用模式", api_mode),
            ]),
        ]

        if configured:
            rows = []
            for item in configured:
                is_active = item["name"] == provider
                configured_model = str(item.get("model") or "").strip()
                display_model = model if is_active else (configured_model or "未记录")
                marker = "当前" if is_active else ("已配置" if configured_model else "仅密钥")
                rows.append((item["display_name"], display_model, marker))
            blocks.extend([
                spacer_block(),
                section_block("已启用接入方"),
                table_block(
                    (
                        PanelColumn("接入方", role="primary"),
                        PanelColumn("模型", role="muted"),
                        PanelColumn("状态", role="accent", no_wrap=True),
                    ),
                    rows,
                ),
            ])
        else:
            blocks.extend([spacer_block(), key_value_block([("已启用接入方", "暂无")])])

        blocks.extend([
            spacer_block(),
            section_block("常用命令示例"),
            command_block(_provider_command_rows()),
            spacer_block(),
            text_block("模型库负责识别模型信息；接入方负责实际调用模型。", muted=True),
        ])
        self._panel_model(PanelModel(
            title="M-Claw · 大模型接入",
            blocks=tuple(blocks),
            tone="info",
            namespace="provider",
        ))

    def render_asr_status(self, *, cfg: dict, service_status: dict) -> None:
        rows = [
            ("模式", service_status.get("listen_mode") or cfg.get("listen_mode")),
            ("运行", bool(service_status.get("running"))),
            ("录音", bool(service_status.get("recording"))),
            ("Provider", cfg.get("provider")),
            ("Backend", cfg.get("backend")),
            ("Model", cfg.get("model")),
            ("Recorder", cfg.get("recorder_backend")),
            ("arecord device", cfg.get("arecord_device")),
            ("Key", f"{cfg.get('api_key')} ({cfg.get('key_source') or 'not configured'})"),
            ("PTT key", f"{cfg.get('push_to_talk_key')} ({cfg.get('push_to_talk_behavior')})"),
        ]
        if service_status.get("last_error"):
            rows.append(("Error", service_status["last_error"]))
        self._panel_model(PanelModel(
            title="M-Claw ASR 状态",
            namespace="asr",
            blocks=(key_value_block(rows),),
        ))

    def render_pet_status(self, *, pet_cfg: dict, running: bool, last_error: str | None = None) -> None:
        notify = pet_cfg.get("notify", {}) if isinstance(pet_cfg.get("notify"), dict) else {}
        rows = [
            ("启用 / Pet enabled", bool(pet_cfg.get("enabled"))),
            ("运行 / Pet running", running),
            ("资源 / Pet asset", pet_cfg.get("asset", "robot-dark")),
            ("缩放 / Pet scale", pet_cfg.get("scale", 1.0)),
            ("气泡 / Pet bubble", bool(pet_cfg.get("show_bubble", True))),
            (
                "通知",
                "，".join([
                    f"回合={bool(notify.get('turn_completed', True))}",
                    f"后台={bool(notify.get('background_completed', True))}",
                    f"子代理={bool(notify.get('delegation_completed', True))}",
                    f"工具={bool(notify.get('tool_finished', False))}",
                ]),
            ),
        ]
        if last_error:
            rows.append(("错误", last_error))
        self._panel_model(PanelModel(
            title="M-Claw 桌面宠物",
            namespace="pet",
            blocks=(key_value_block(rows),),
        ))

    def render_pet_notice(self, message: str) -> None:
        self._printer(f"  {message}")

    def render_pet_usage(self) -> None:
        self._panel_model(PanelModel(
            title="M-Claw 桌面宠物命令",
            namespace="pet",
            blocks=(command_block([
                ("/pet [on|off|status|save|restart|test]", "基础控制"),
                ("/pet scale <数字>", "设置缩放"),
                ("/pet position <bottom_right|bottom_left|top_right|top_left|reset>", "设置或重置位置"),
                ("/pet bubble <on|off>", "开关气泡"),
                ("/pet notify <turn|background|delegation|tool> <on|off>", "开关事件通知"),
                ("/pet asset <名称或路径>", "切换资源"),
            ]),),
        ))


def _doctor_panel_model(text: str) -> PanelModel:
    lines = str(text or "").splitlines() or ["无输出"]
    blocks = []
    check_rows = []
    detail_rows = []

    for line_no, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        parsed = _parse_doctor_check(stripped)
        if parsed:
            label, role, name, detail = parsed
            check_rows.append((panel_cell(label, role), name, detail))
            continue

        if line_no == 0:
            blocks.append(text_block(stripped))
        elif stripped.startswith("fix:"):
            detail_rows.append(("修复建议", stripped[4:].strip()))
        elif stripped.startswith("Summary:"):
            detail_rows.append(("汇总", stripped[len("Summary:"):].strip()))
        else:
            detail_rows.append(("输出", stripped))

    if check_rows:
        if blocks:
            blocks.append(spacer_block())
        blocks.extend([
            section_block("检查项"),
            table_block(
                (
                    PanelColumn("状态", role="accent", no_wrap=True),
                    PanelColumn("项目", role="primary"),
                    PanelColumn("详情", role="muted"),
                ),
                check_rows,
            ),
        ])

    if detail_rows:
        if blocks:
            blocks.append(spacer_block())
        blocks.append(key_value_block(detail_rows))

    return PanelModel(
        title="M-Claw Doctor",
        namespace="doctor",
        blocks=tuple(blocks or [text_block("无输出")]),
    )


def _parse_doctor_check(line: str):
    markers = {
        "[OK]": ("OK", "success"),
        "[WARN]": ("WARN", "warning"),
        "[FAIL]": ("FAIL", "danger"),
    }
    for marker, (label, role) in markers.items():
        if line.startswith(f"{marker} "):
            rest = line[len(marker):].strip()
            if ":" in rest:
                name, detail = rest.split(":", 1)
                return label, role, name.strip(), detail.strip()
            return label, role, rest, ""
    return None


def _provider_command_rows() -> list[tuple[str, str]]:
    return [
        (
            "/model <模型名> --provider <供应商>",
            "临时切换；示例：/model gpt-5.5 --provider openai",
        ),
        (
            "/model <模型名> --provider <供应商> --profile <接口>",
            "指定接口类型；示例：/model kimi-k2.6 --provider moonshot --profile api",
        ),
        (
            "/model <模型名> --provider <供应商> --global",
            "全局保存；示例：/model gpt-5.5 --provider openai --global",
        ),
        (
            "/provider <供应商> --profile",
            "查看某个接入方的接口类型；示例：/provider moonshot --profile",
        ),
        (
            "/provider --profile",
            "查看当前接入方的接口类型",
        ),
        (
            "/model-update provider <供应商>",
            "查看模型库来源；示例：/model-update provider qwen",
        ),
    ]
