# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render slash-command results as panel models or legacy Rich output."""

from __future__ import annotations

import re
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
    """Render slash command outputs through the panel abstraction shared by frontends."""

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

    def _panel_model(self, panel: PanelModel, *, border_style: str | None = None) -> None:
        """Route panels through injected sinks before falling back to classic Rich output."""
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
                    ("/search-backend", "查看或切换联网搜索后端"),
                    ("/schedule", "打开本地任务中心"),
                    ("/skills", "查看和使用技能"),
                    ("/usage", "查看本次会话 Token 用量"),
                    ("/doctor", "检查运行环境和工具可用性"),
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
        """Render the plain doctor report through the structured panel parser below."""
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
        cache_read = getattr(agent, "session_cache_read_tokens", 0)
        cache_write = getattr(agent, "session_cache_write_tokens", 0)
        reasoning = getattr(agent, "session_reasoning_tokens", 0)
        self._panel_model(PanelModel(
            title="M-Claw 会话用量",
            namespace="usage",
            blocks=(key_value_block([
                ("时长", format_duration(elapsed)),
                ("模型", model),
                ("API 调用", agent.session_api_calls),
                ("输入", f"{agent.session_input_tokens:,} tokens"),
                ("输出", f"{agent.session_output_tokens:,} tokens"),
                ("缓存读取", f"{cache_read:,} tokens"),
                ("缓存写入", f"{cache_write:,} tokens"),
                ("推理", f"{reasoning:,} tokens"),
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
    """Convert the textual doctor report into a structured panel model."""
    lines = str(text or "").splitlines() or ["无输出"]
    blocks = []
    check_rows = []
    detail_rows = []
    panel_title = "M-CLAW 运行环境诊断"
    current_section = ""
    first_content_seen = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if not first_content_seen:
            first_content_seen = True
            if stripped.startswith("M-CLAW"):
                panel_title = stripped
                continue
            blocks.append(text_block(stripped))
            continue

        if stripped in _DOCTOR_SECTIONS:
            current_section = stripped
            continue

        parsed = _parse_doctor_check(stripped)
        if parsed:
            label, role, name, detail = parsed
            section = "" if current_section == "总览" else current_section
            check_rows.append((section, panel_cell(label, role), name, detail))
            continue

        if stripped.startswith("修复:"):
            detail_rows.append(("修复建议", stripped[len("修复:"):].strip()))
            continue

        overview_row = _parse_doctor_key_value(stripped)
        if current_section == "总览" and overview_row:
            detail_rows.append(overview_row)
            continue

        detail_rows.append(("输出", stripped))

    if check_rows:
        if blocks:
            blocks.append(spacer_block())
        blocks.extend([
            section_block("检查项"),
            table_block(
                (
                    PanelColumn("分组", role="muted", no_wrap=True),
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
        title=panel_title,
        namespace="doctor",
        blocks=tuple(blocks or [text_block("无输出")]),
    )


_DOCTOR_SECTIONS = {"总览", "核心运行时", "工具能力", "IM通道", "其他"}
_DOCTOR_CHECK_RE = re.compile(r"^(OK|WARN|FAIL)\s+(.+?)(?:\s{2,}(.+))?$")
_DOCTOR_FIELD_RE = re.compile(r"^(.+?)\s{2,}(.+)$")
_DOCTOR_STATUS = {
    "OK": ("OK", "success"),
    "WARN": ("WARN", "warning"),
    "FAIL": ("FAIL", "danger"),
}


def _parse_doctor_check(line: str):
    """Parse status-prefixed doctor rows without binding the command layer to Rich."""
    match = _DOCTOR_CHECK_RE.match(line)
    if not match:
        return None
    status, name, detail = match.groups()
    label, role = _DOCTOR_STATUS[status]
    return label, role, name.strip(), (detail or "").strip()


def _parse_doctor_key_value(line: str) -> tuple[str, str] | None:
    match = _DOCTOR_FIELD_RE.match(line)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


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
