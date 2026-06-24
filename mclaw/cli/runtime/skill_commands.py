"""Runtime coordination for slash-invoked local Skills."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mclaw.prompts.skills import build_skill_invocation_system
from mclaw.skills_hub import skill_store


@dataclass(frozen=True)
class RuntimeSkillCommandHooks:
    """Host operations used by UI-neutral Skill invocation."""

    get_agent_messages: Callable[[], list[dict[str, Any]]]
    enqueue_user_intent: Callable[[str], None]
    set_skip_next_prompt: Callable[[], None]
    render_read_error: Callable[[str], None]
    render_processing: Callable[[str], None]
    render_loaded: Callable[[str], None]
    record_loaded_skill: Callable[[str, dict[str, Any]], None] | None = None
    build_base_system_prompt: Callable[[], str] | None = None


@dataclass(frozen=True)
class RuntimeSkillImportConfirmationHooks:
    """Host operations used while resolving a pending Skill enable confirmation."""

    get_pending_confirmation: Callable[[], dict[str, Any] | None]
    clear_pending_confirmation: Callable[[], None]
    run_skill_import_action: Callable[[str, str], str]
    invalidate_skill_registry: Callable[[], None]
    render_invalid_input: Callable[[], None]
    render_installed: Callable[[str, str], None]
    render_cancelled: Callable[[], None]
    render_failed: Callable[[str], None]
    set_busy: Callable[[bool], None]
    clear_busy: Callable[[], None]
    invalidate: Callable[[], None]
    record_confirmation_result: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None


class RuntimeSkillCommandCoordinator:
    """Loads a Skill into agent context and optionally queues a user intent."""

    def __init__(self, hooks: RuntimeSkillCommandHooks) -> None:
        self.hooks = hooks

    def invoke_skill(self, skill: Any, user_intent: str) -> bool:
        try:
            view = skill_store.view_skill(str(skill.name))
            skill_content = str(view.get("content") or "")
        except Exception as exc:
            self.hooks.render_read_error(f"Cannot read skill: {exc}")
            return True

        system_msg = build_skill_invocation_system(
            str(skill.name),
            skill_content,
            user_intent=user_intent,
        )
        messages = self.hooks.get_agent_messages()
        if not any(msg.get("role") == "system" for msg in messages):
            base_prompt = ""
            if self.hooks.build_base_system_prompt is not None:
                try:
                    base_prompt = str(self.hooks.build_base_system_prompt() or "")
                except Exception:
                    base_prompt = ""
            if base_prompt:
                messages.insert(0, {"role": "system", "content": base_prompt})

        self.inject_skill_context(messages, system_msg)
        if self.hooks.record_loaded_skill is not None:
            self.hooks.record_loaded_skill(str(skill.name), view)

        intent = str(user_intent or "").strip()
        if intent:
            self.hooks.render_processing(str(skill.name))
            self.hooks.set_skip_next_prompt()
            self.hooks.enqueue_user_intent(intent)
        else:
            self.hooks.render_loaded(str(skill.name))
        return True

    @staticmethod
    def inject_skill_context(messages: list[dict[str, Any]], skill_text: str) -> None:
        """Append Skill guidance to the existing system message."""
        first_line = str(skill_text or "").splitlines()[0] if str(skill_text or "").splitlines() else ""
        marker = first_line if first_line.startswith("[Skill:") else skill_text
        for msg in messages:
            if msg.get("role") == "system":
                if marker not in msg.get("content", ""):
                    msg["content"] += f"\n\n{skill_text}"
                return
        messages.insert(0, {"role": "system", "content": skill_text})


class RuntimeSkillImportConfirmationCoordinator:
    """Normalizes and executes a pending Skill drafting confirmation decision."""

    APPROVE_VALUES = {"y", "yes", "allow", "install", "approve", "是", "允许", "确认", "安装"}
    REJECT_VALUES = {"__mclaw_confirm_esc__", "n", "no", "cancel", "reject", "否", "取消", "停止"}

    def __init__(self, hooks: RuntimeSkillImportConfirmationHooks) -> None:
        self.hooks = hooks

    def handle_input(self, user_input: str) -> bool:
        confirmation = self.hooks.get_pending_confirmation()
        if not confirmation:
            return False

        decision = self._decision(user_input)
        if decision is None:
            self.hooks.render_invalid_input()
            return True

        approve = decision
        drafting_id = str(confirmation.get("drafting_id") or confirmation.get("staging_id") or "")
        action = "enable_drafting" if approve else "cancel_drafting"
        self.hooks.clear_pending_confirmation()
        self.hooks.set_busy(approve)
        self.hooks.invalidate()
        try:
            result_text = self.hooks.run_skill_import_action(action, drafting_id)
            result = self._parse_result(result_text)
            self._record_confirmation_result(action, confirmation, result)
            if result.get("success"):
                if approve:
                    self.hooks.invalidate_skill_registry()
                    self.hooks.render_installed(
                        str(result.get("name") or confirmation.get("skill_name") or ""),
                        str(result.get("path") or ""),
                    )
                else:
                    self.hooks.render_cancelled()
            else:
                self.hooks.render_failed(str(result.get("error") or result_text))
        except Exception as exc:
            self._record_confirmation_result(
                action,
                confirmation,
                {"success": False, "action": action, "error": str(exc)},
            )
            self.hooks.render_failed(str(exc))
        finally:
            self.hooks.clear_busy()
            self.hooks.invalidate()
        return True

    def _record_confirmation_result(
        self,
        action: str,
        confirmation: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        recorder = self.hooks.record_confirmation_result
        if recorder is None:
            return
        try:
            recorder(action, confirmation, result)
        except Exception:
            return

    @classmethod
    def _decision(cls, user_input: str) -> bool | None:
        text = str(user_input or "").strip().lower()
        if text in cls.REJECT_VALUES:
            return False
        if text in cls.APPROVE_VALUES:
            return True
        return None

    @staticmethod
    def _parse_result(result_text: str) -> dict[str, Any]:
        try:
            result = json.loads(result_text)
        except Exception:
            return {"success": False, "error": result_text}
        return result if isinstance(result, dict) else {"success": False, "error": result_text}
