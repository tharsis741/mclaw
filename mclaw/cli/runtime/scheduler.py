"""Runtime coordinator for local scheduler TUI control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from mclaw.scheduler.ids import new_job_id, new_pairing_code
from mclaw.scheduler.models import DeliverySpec, ScheduleSpec, SchedulerJob, SchedulerTargetPairing
from mclaw.scheduler.store import SchedulerStore
from mclaw.scheduler.targets import TargetManager
from mclaw.scheduler.triggers import humanize_schedule, next_run_after, parse_schedule, preview_next_runs
from mclaw.tools.toolsets import validate_toolset


@dataclass
class SchedulerDraft:
    delivery: DeliverySpec = field(default_factory=lambda: DeliverySpec(target_id="local"))
    schedule: ScheduleSpec | None = None
    name: str = ""
    prompt: str = ""
    enabled_toolsets: list[str] = field(default_factory=list)
    workdir: str = ""
    session_policy: str = "task_thread"


class RuntimeSchedulerCoordinator:
    def __init__(
        self,
        *,
        store: SchedulerStore,
        engine: Any,
        delivery: Any,
        config: dict[str, Any],
        workspace: str,
        render_notice: Callable[[str, str, str | None, str], None],
        available_toolsets: Callable[[], list[str]],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.engine = engine
        self.delivery = delivery
        self.config = config
        self.workspace = workspace
        self.render_notice = render_notice
        self.available_toolsets = available_toolsets
        self.now = now
        scheduler_cfg = config.get("scheduler", {}) if isinstance(config, dict) else {}
        self.tick_interval_seconds = max(1, int(scheduler_cfg.get("tick_interval_seconds", 30) or 30))
        self._last_tick = 0.0
        self._state: str | None = None
        self._draft: SchedulerDraft | None = None
        self._job_map: list[str] = []
        self._run_map: list[str] = []
        self._target_map: list[str] = []
        self._job_page_offset: int = 0
        self._job_has_next: bool = False
        self._run_page_offset: int = 0
        self._run_has_next: bool = False
        self._run_page_job_id: str | None = None
        self._selected_job_id: str = ""
        self._selected_run_id: str = ""
        self._selected_run_scope: str | None = None
        self._selected_target_id: str = ""
        self._selected_target_action: str = ""
        self._pairing_code: str = ""
        self._pairing_target_manager_flow: bool = False
        self._last_pairing_status: str = ""
        self._error: str = ""
        self._last_render_title: str = ""
        self._last_render_body: str = ""
        self._last_render_kind: str = "info"

    def pump(self) -> None:
        scheduler_cfg = self.config.get("scheduler", {}) if isinstance(self.config, dict) else {}
        if scheduler_cfg.get("enabled", True):
            now = self.now()
            if now - self._last_tick >= self.tick_interval_seconds:
                self._last_tick = now
                self.engine.tick_once(now=now)
        if self._state == "pairing_wait" and self._pairing_code:
            pairing = self.store.get_pairing(self._pairing_code)
            status = pairing.status if pairing else "expired"
            if status != self._last_pairing_status and status != "waiting":
                self._last_pairing_status = status
                if status == "bound":
                    self._render_pairing_bound()
                else:
                    self._render_pairing_wait()

    def has_pending_input(self) -> bool:
        return self._state is not None

    def handle_command(self, args: str = "") -> None:
        raw = (args or "").strip().lower()
        if raw == "new":
            self.render_schedule_new_wizard()
        elif raw == "targets":
            self.render_target_manager()
        else:
            self.render_schedule_dashboard()

    def handle_input(self, text: str) -> None:
        value = (text or "").strip()
        state = self._state
        self._error = ""
        try:
            if state == "dashboard":
                self._handle_dashboard_input(value)
            elif state == "job_select":
                self._handle_job_select(value)
            elif state == "job_detail":
                self._handle_job_detail(value)
            elif state == "delete_confirm":
                self._handle_delete_confirm(value)
            elif state == "runs_scope":
                self._handle_runs_scope(value)
            elif state == "runs_job_select":
                self._handle_runs_job_select(value)
            elif state == "runs_list":
                self._handle_runs_list(value)
            elif state == "run_detail":
                self._handle_run_detail(value)
            elif state == "created_done":
                self._handle_created_done(value)
            elif state == "new_overview":
                self._handle_new_overview(value)
            elif state == "new_step1":
                self._handle_new_step1(value)
            elif state == "new_existing_targets":
                self._handle_existing_targets(value)
            elif state == "new_target_type":
                self._handle_new_target_type(value)
            elif state == "pairing_wait":
                self._handle_pairing_wait(value)
            elif state == "pairing_bound":
                self._handle_pairing_bound(value)
            elif state == "new_step2":
                self._handle_new_step2(value)
            elif state and state.startswith("new_schedule_"):
                self._handle_schedule_value(state, value)
            elif state == "new_name":
                self._handle_new_name(value)
            elif state == "new_prompt":
                self._handle_new_prompt(value)
            elif state == "new_output_choice":
                self._handle_new_output_choice(value)
            elif state == "new_output_path":
                self._handle_new_output_path(value)
            elif state == "new_output_template":
                self._handle_new_output_template(value)
            elif state == "new_preview":
                self._handle_new_preview(value)
            elif state == "targets":
                self._handle_targets(value)
            elif state == "target_type":
                self._handle_target_type(value)
            elif state == "target_select":
                self._handle_target_select(value)
            elif state == "target_rename":
                self._handle_target_rename(value)
            elif state == "target_delete_confirm":
                self._handle_target_delete_confirm(value)
            else:
                self.render_schedule_dashboard()
        except Exception as exc:
            self._error = f"错误：{exc}"
            self._rerender_current()

    def render_schedule_dashboard(self) -> None:
        jobs = self.store.list_jobs(include_paused=True)
        targets = self.store.list_targets(enabled_only=False)
        if self._job_page_offset >= len(jobs):
            self._job_page_offset = max(0, ((max(0, len(jobs) - 1)) // 10) * 10)
        page_jobs = jobs[self._job_page_offset : self._job_page_offset + 10]
        self._job_has_next = self._job_page_offset + 10 < len(jobs)
        self._job_map = [job.id for job in page_jobs]
        next_job = min((job for job in jobs if job.enabled and job.next_run_at), key=lambda item: item.next_run_at or 0, default=None)
        target_counts: dict[str, int] = {}
        for target in targets:
            target_counts[target.type] = target_counts.get(target.type, 0) + 1
        lines = [
            f"状态        {'enabled' if self.config.get('scheduler', {}).get('enabled', True) else 'disabled'} · tick {self.tick_interval_seconds}s",
            f"下次执行    {_fmt_time(next_job.next_run_at) + ' · ' + next_job.name if next_job else '-'}",
            f"输出方式    本地执行任务 · 已绑定 钉钉群 {target_counts.get('dingtalk_group', 0)} · 钉钉私聊 {target_counts.get('dingtalk_private', 0)} · 微信私聊 {target_counts.get('weixin_private', 0)}",
            "",
            "任务",
            "#   状态      下次执行              名称              投递目标",
        ]
        for idx, job in enumerate(page_jobs, 1):
            status = "enabled" if job.enabled else "paused"
            if job.status == "error":
                status = "error"
            target = self.store.get_target(job.delivery.target_id)
            target_text = TargetManager().display_target(target) if target else job.delivery.target_id
            lines.append(f"{idx:<3} {status:<9} {_fmt_time(job.next_run_at):<20} {job.name:<16} {target_text}")
        if not jobs:
            lines.append("-   -         -                   暂无任务")
        lines.extend([
            "",
            "操作",
            "1 新建定时任务   2 查看定时任务   3 立即运行   4 暂停/恢复",
            "5 投递目标   6 运行记录   7 删除任务   B 返回聊天",
        ])
        if len(jobs) > 10:
            lines.append("8 下一页   9 上一页")
        self._state = "dashboard"
        self._render("M-Claw Scheduler", "\n".join(lines))

    def render_schedule_new_wizard(self) -> None:
        self._draft = SchedulerDraft(workdir=self.workspace, enabled_toolsets=self._default_toolsets())
        self._state = "new_overview"
        self._render(
            "新建定时任务",
            "\n".join([
                "定时任务配置流程",
                "",
                "- 选择结果输出方式",
                "- 定时任务执行时间",
                "- 设置任务内容",
                "- 输出设置",
                "- 预览并创建",
                "",
                "可选输入",
                "Y 进入设置向导",
                "N 退出新建任务流程并控制台",
            ]),
        )

    def render_target_manager(self) -> None:
        targets = self.store.list_targets(enabled_only=False)
        self._target_map = [target.id for target in targets]
        lines = ["输出方式", ""]
        for idx, target in enumerate(targets, 1):
            validated = TargetManager().validate_target(target)
            enabled = "enabled" if validated.enabled else "disabled"
            lines.append(f"{idx}. {TargetManager().display_target(validated)}    {validated.route_status} · {enabled}")
        lines.extend([
            "",
            "可选输入",
            "1 新增输出方式",
            "2 改名",
            "3 测试发送 ",
            "4 停用/启用",
            "5 删除此输出方式",
            "B 返回控制台",
        ])
        self._state = "targets"
        self._render("投递目标", "\n".join(lines))

    def render_pairing_wait(self, code: str) -> None:
        self._pairing_code = code
        self._last_pairing_status = "waiting"
        self._render_pairing_wait()

    def _handle_dashboard_input(self, value: str) -> None:
        if value.upper() == "B":
            self._state = None
            self._render("M-Claw Scheduler", "已返回聊天输入。", kind="success")
            return
        if value == "1":
            self.render_schedule_new_wizard()
            return
        if value == "2":
            self._state = "job_select"
            self._render_job_select("查看任务")
            return
        if value == "3":
            self._state = "job_select"
            self._selected_run_scope = "run_now"
            self._render_job_select("立即运行")
            return
        if value == "4":
            self._state = "job_select"
            self._selected_run_scope = "toggle"
            self._render_job_select("暂停/恢复")
            return
        if value == "5":
            self.render_target_manager()
            return
        if value == "6":
            self._state = "runs_scope"
            self._render_runs_scope()
            return
        if value == "7":
            self._state = "job_select"
            self._selected_run_scope = "delete"
            self._render_job_select("删除任务")
            return
        if value == "8" and self._job_has_next:
            self._job_page_offset += 10
            self.render_schedule_dashboard()
            return
        if value == "9" and self._job_page_offset > 0:
            self._job_page_offset = max(0, self._job_page_offset - 10)
            self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_job_select(self, value: str) -> None:
        if value.upper() == "B":
            self.render_schedule_dashboard()
            return
        if value == "8" and self._job_has_next:
            self._job_page_offset += 10
            self._render_job_select(_job_select_title(self._selected_run_scope))
            return
        if value == "9" and self._job_page_offset > 0:
            self._job_page_offset = max(0, self._job_page_offset - 10)
            self._render_job_select(_job_select_title(self._selected_run_scope))
            return
        job = self._job_from_number(value)
        if not job:
            self._invalid()
            return
        action = self._selected_run_scope or "view"
        self._selected_job_id = job.id
        self._selected_run_scope = None
        if action == "run_now":
            run = self.engine.run_job_now(job.id)
            self._render_run_completed(run, ["B 返回任务列表"])
            self._state = "job_detail"
            return
        if action == "toggle":
            if job.enabled:
                self.store.pause_job(job.id)
            else:
                self.store.resume_job(job.id)
            self.render_schedule_dashboard()
            return
        if action == "delete":
            self._state = "delete_confirm"
            self._render("删除定时任务", f"将删除：{job.name}\n历史 run output 保留在本地输出目录。\n\n操作\nY 确认删除\nN 取消删除并返回任务列表")
            return
        self._render_job_detail(job)

    def _handle_job_detail(self, value: str) -> None:
        if value.upper() == "B":
            self.render_schedule_dashboard()
            return
        job = self.store.get_job(self._selected_job_id)
        if not job:
            self.render_schedule_dashboard()
            return
        if value == "1":
            run = self.engine.run_job_now(job.id)
            self._render_run_completed(run, ["B 返回任务列表"])
            return
        if value == "2":
            self.store.pause_job(job.id) if job.enabled else self.store.resume_job(job.id)
            self.render_schedule_dashboard()
            return
        if value == "3":
            self._selected_run_scope = job.id
            self._render_runs_list(job_id=job.id, reset_page=True)
            return
        if value == "4":
            self._state = "delete_confirm"
            self._render("删除定时任务", f"将删除：{job.name}\n历史 run output 保留在本地输出目录。\n\n操作\nY 确认删除\nN 取消删除并返回上一页")
            return
        self._invalid()

    def _handle_delete_confirm(self, value: str) -> None:
        if value.upper() == "Y":
            self.store.delete_job(self._selected_job_id)
            self.render_schedule_dashboard()
            return
        if value.upper() == "N":
            self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_runs_scope(self, value: str) -> None:
        if value.upper() == "B":
            self.render_schedule_dashboard()
        elif value.lower() == "a":
            self._render_runs_list(job_id=None, reset_page=True)
        else:
            job = self._job_from_number(value)
            if not job:
                self._invalid()
                return
            self._render_runs_list(job_id=job.id, reset_page=True)

    def _handle_runs_job_select(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "runs_scope"
            self._render_runs_scope()
            return
        if value == "8" and self._job_has_next:
            self._job_page_offset += 10
            self._render_job_select("选择运行记录任务")
            return
        if value == "9" and self._job_page_offset > 0:
            self._job_page_offset = max(0, self._job_page_offset - 10)
            self._render_job_select("选择运行记录任务")
            return
        job = self._job_from_number(value)
        if not job:
            self._invalid()
            return
        self._render_runs_list(job_id=job.id, reset_page=True)

    def _handle_runs_list(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "runs_scope"
            self._render_runs_scope()
            return
        if value == "8" and self._run_has_next:
            self._run_page_offset += 10
            self._render_runs_list(job_id=self._run_page_job_id)
            return
        if value == "9" and self._run_page_offset > 0:
            self._run_page_offset = max(0, self._run_page_offset - 10)
            self._render_runs_list(job_id=self._run_page_job_id)
            return
        try:
            idx = int(value) - 1
        except ValueError:
            self._invalid()
            return
        if idx < 0 or idx >= len(self._run_map):
            self._invalid()
            return
        run = self.store.get_run(self._run_map[idx])
        if not run:
            self._invalid()
            return
        self._selected_run_id = run.id
        job = self.store.get_job(run.job_id)
        self._state = "run_detail"
        self._render(
            "运行记录详情",
            "\n".join([
                f"run_id       {run.id}",
                f"job          {job.name if job else run.job_id}",
                f"状态         {run.status}",
                f"计划时间     {_fmt_time(run.scheduled_for)}",
                f"开始时间     {_fmt_time(run.started_at)}",
                f"结束时间     {_fmt_time(run.finished_at)}",
                f"session      {run.session_id}",
                f"保存路径      {run.output_path or '-'}",
                f"指定保存路径  {_final_response_output_path(run) or '-'}",
                f"delivery     {(run.delivery_result or {}).get('status', '-')}",
                f"token        {_token_summary(run.token_usage)}",
                "",
                "Final Response",
                _truncate(run.final_response),
                "",
                "Error",
                run.error or "-",
                "",
                "操作",
                "1 查看保存路径   2 重新运行该任务   B 返回运行记录列表",
            ]),
        )

    def _handle_run_detail(self, value: str) -> None:
        if value.upper() == "B":
            self._render_runs_list(job_id=self._selected_run_scope)
            return
        if value == "1":
            run = self.store.get_run(self._selected_run_id) if self._selected_run_id else None
            paths = [
                f"保存路径：{run.output_path if run else '-'}",
                f"指定保存路径：{_final_response_output_path(run) if run else '-'}",
            ]
            self._render("Output 路径", "\n".join(paths) + "\n\n操作\nB 返回运行记录列表")
            return
        if value == "2":
            run = self.store.get_run(self._selected_run_id) if self._selected_run_id else None
            if run:
                self.engine.run_job_now(run.job_id)
            self._render_runs_list(job_id=self._selected_run_scope)
            return
        self._invalid()

    def _handle_new_overview(self, value: str) -> None:
        if value.upper() == "Y":
            self._state = "new_step1"
            self._render_new_step1()
        elif value.upper() == "N":
            self.render_schedule_dashboard()
        else:
            self._invalid()

    def _handle_new_step1(self, value: str) -> None:
        if value.upper() == "B":
            self.render_schedule_new_wizard()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        if value == "1":
            assert self._draft is not None
            self._draft.delivery = DeliverySpec(
                target_id="local",
                final_response_path=self._draft.delivery.final_response_path,
                final_response_filename_template=self._draft.delivery.final_response_filename_template,
            )
            self._state = "new_step2"
            self._render_new_step2()
            return
        if value == "2":
            self._render_existing_targets()
            return
        if value == "3":
            self._state = "new_target_type"
            self._render_target_type_panel(cancel_to="取消创建并丢弃草稿")
            return
        self._invalid()

    def _handle_existing_targets(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_step1"
            self._render_new_step1()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        target = self._target_from_number(value)
        if not target:
            self._invalid()
            return
        assert self._draft is not None
        self._draft.delivery = DeliverySpec(
            target_id=target.id,
            final_response_path=self._draft.delivery.final_response_path,
            final_response_filename_template=self._draft.delivery.final_response_filename_template,
        )
        self._state = "new_step2"
        self._render_new_step2()

    def _handle_new_target_type(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_step1"
            self._render_new_step1()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        requested = {"1": "dingtalk_group", "2": "dingtalk_private", "3": "weixin_private"}.get(value)
        if not requested:
            self._invalid()
            return
        self._create_pairing(requested)

    def _handle_pairing_wait(self, value: str) -> None:
        if value.upper() == "B":
            if self._pairing_code:
                self.store.cancel_pairing(self._pairing_code)
            self._state = "target_type" if self._pairing_target_manager_flow else "new_target_type"
            self._render_target_type_panel(
                cancel_to="取消本次绑定并返回上一步"
                if self._pairing_target_manager_flow
                else "取消创建并丢弃草稿"
            )
            return
        if value.upper() == "C":
            if self._pairing_code:
                self.store.cancel_pairing(self._pairing_code)
            if self._pairing_target_manager_flow:
                self.render_target_manager()
            else:
                self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_pairing_bound(self, value: str) -> None:
        pairing = self.store.get_pairing(self._pairing_code)
        target_id = pairing.target_id if pairing else ""
        target = self.store.get_target(target_id) if target_id else None
        if value == "1" and target:
            job = self._draft_job_stub()
            self.delivery.deliver_one(DeliverySpec(target.id), job, _empty_run(), "M-CLAW 定时任务测试!")
            self._render_pairing_bound()
            return
        if value == "2" and target:
            if self._pairing_target_manager_flow:
                self.render_target_manager()
                return
            assert self._draft is not None
            self._draft.delivery = DeliverySpec(
                target_id=target.id,
                final_response_path=self._draft.delivery.final_response_path,
                final_response_filename_template=self._draft.delivery.final_response_filename_template,
            )
            self._state = "new_step2"
            self._render_new_step2()
            return
        if value.upper() == "B":
            self._state = "target_type" if self._pairing_target_manager_flow else "new_target_type"
            self._render_target_type_panel(
                cancel_to="取消本次绑定并返回上一步"
                if self._pairing_target_manager_flow
                else "取消创建并丢弃草稿"
            )
            return
        if value.upper() == "C":
            if self._pairing_target_manager_flow:
                self.render_target_manager()
            else:
                self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_new_step2(self, value: str) -> None:
        mapping = {
            "1": "new_schedule_daily",
            "2": "new_schedule_weekly",
            "3": "new_schedule_monthly",
            "4": "new_schedule_interval",
            "5": "new_schedule_once",
            "6": "new_schedule_cron",
        }
        if value.upper() == "B":
            self._state = "new_step1"
            self._render_new_step1()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        state = mapping.get(value)
        if not state:
            self._invalid()
            return
        self._state = state
        prompts = {
            "new_schedule_daily": "请填写每日执行时间，格式 HH:MM\n例如: 09:00",
            "new_schedule_weekly": "请填写每周执行时间，格式 Weekday HH:MM\n例如: Monday 09:00",
            "new_schedule_monthly": "请填写每月执行时间，格式 day HH:MM\n例如: 1 09:00",
            "new_schedule_interval": "请填写间隔，格式 30s/30m/2h/1d\n例如: 30m",
            "new_schedule_once": "请填写单次执行时间，格式 YYYY-MM-DD HH:MM\n例如: 2026-06-17 09:00",
            "new_schedule_cron": "请输入 5 段 cron 表达式\n例如: 0 9 * * 1-5",
        }
        action_prompts = {
            "new_schedule_daily": "可选输入\n输入<时间>并预览下3次执行\nB 返回上一步\nC 取消创建并丢弃草稿",
            "new_schedule_weekly": "可选输入\n输入<时间>并预览下3次执行\nB 返回上一步\nC 取消创建并丢弃草稿",
            "new_schedule_monthly": "可选输入\n输入<时间>并预览下3次执行\nB 返回上一步\nC 取消创建并丢弃草稿",
            "new_schedule_interval": "可选输入\n输入<时间>保存并预览下3次执行\nB 返回上一步\nC 取消创建并丢弃草稿",
            "new_schedule_once": "可选输入\n输入<时间>保存并预览下3次执行\nB 返回上一步\nC 取消创建并丢弃 draft",
            "new_schedule_cron": "可选输入\n输入<时间>保存并预览下3次执行\nB 返回执上一步\nC 取消创建并丢弃草稿",
        }
        self._render("执行时间", prompts[state] + "\n\n" + action_prompts[state])

    def _handle_schedule_value(self, state: str, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_step2"
            self._render_new_step2()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        raw = self._schedule_raw_from_input(state, value)
        scheduler_cfg = self.config.get("scheduler", {}) if isinstance(self.config, dict) else {}
        spec = parse_schedule(raw, default_timezone=str(scheduler_cfg.get("default_timezone") or "Asia/Shanghai"))
        assert self._draft is not None
        self._draft.schedule = spec
        now_dt = datetime.fromtimestamp(self.now(), ZoneInfo(spec.timezone))
        previews = preview_next_runs(spec, after=now_dt, count=3)
        lines = ["下 3 次执行："]
        for idx, item in enumerate(previews, 1):
            lines.append(f"{idx}. {item.strftime('%Y-%m-%d %H:%M')} {spec.timezone}")
        lines.extend(["", "可选输入", "输入<任务名称>并进入下一步", "B 返回上一步", "C 取消创建并丢弃草稿"])
        self._state = "new_name"
        self._render("任务名称", "\n".join(lines))

    def _handle_new_name(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_step2"
            self._render_new_step2()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        if not value:
            self._invalid()
            return
        assert self._draft is not None
        self._draft.name = value
        self._state = "new_prompt"
        self._render("任务内容", "例如: 提醒大家同步昨日进展、今日计划和阻塞。\n\n可选输入\n输入<任务内容>并进入下一步\nB 返回上一步\nC 取消创建并丢弃草稿")

    def _handle_new_prompt(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_name"
            self._render("任务名称", "可选输入\n输入<任务名称>并进入下一步\nB 返回上一步\nC 取消创建并丢弃草稿")
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        if not value:
            self._invalid()
            return
        assert self._draft is not None
        self._draft.prompt = value
        self._state = "new_output_choice"
        self._render_new_output_choice()

    def _handle_new_output_choice(self, value: str) -> None:
        if value == "1":
            assert self._draft is not None
            self._draft.delivery.final_response_path = ""
            self._draft.delivery.final_response_filename_template = ""
            self._state = "new_preview"
            self._render_preview()
            return
        if value == "2":
            self._state = "new_output_path"
            self._render(
                "输出设置",
                "请输入结果保存位置。\n例如：\nC:\\Users\\administrator\\Desktop(Windows)\n\n可选输入\n输入<路径位置>并进入下一步\nB 返回上一步\nC 取消创建并丢弃草稿",
            )
            return
        if value.upper() == "B":
            self._state = "new_prompt"
            self._render("任务内容", "例如: 提醒大家同步昨日进展、今日计划和阻塞。\n\n可选输入\n输入<任务内容>并进入下一步\nB 返回上一步\nC 取消创建并丢弃草稿")
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_new_output_path(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_output_choice"
            self._render_new_output_choice()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        if not value:
            self._invalid()
            return
        assert self._draft is not None
        self._draft.delivery.final_response_path = value
        self._state = "new_output_template"
        self._render_output_template_prompt()

    def _handle_new_output_template(self, value: str) -> None:
        if value.upper() == "B":
            self._state = "new_output_path"
            self._render(
                "输出设置",
                "请输入结果保存位置。\n例如：\nC:\\Users\\administrator\\Desktop(Windows)\n\n可选输入\n输入<路径位置>并进入下一步\nB 返回上一步\nC 取消创建并丢弃草稿",
            )
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        if not value:
            self._invalid()
            return
        assert self._draft is not None
        self._draft.delivery.final_response_filename_template = value
        self._state = "new_preview"
        self._render_preview()

    def _handle_new_preview(self, value: str) -> None:
        if value in {"1", "2"}:
            enabled = value == "1"
            job = self._create_job_from_draft(enabled=enabled)
            self._state = "created_done"
            self._selected_job_id = job.id
            self._render("已创建定时任务", f"id: {job.id}\n下次执行：{_fmt_time(job.next_run_at)}\n投递目标：{job.delivery.target_id}\n\n操作\n1 立即试跑一次\nB 返回任务列表", kind="success")
            return
        if value == "3":
            self._state = "new_step1"
            self._render_new_step1()
            return
        if value.upper() == "B":
            self._state = "new_output_choice"
            self._render_new_output_choice()
            return
        if value.upper() == "C":
            self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_created_done(self, value: str) -> None:
        if value == "1" and self._selected_job_id:
            run = self.engine.run_job_now(self._selected_job_id)
            self._render_run_completed(run, ["B 返回任务列表"])
            return
        if value.upper() == "B":
            self.render_schedule_dashboard()
            return
        self._invalid()

    def _handle_targets(self, value: str) -> None:
        if value.upper() == "B":
            self.render_schedule_dashboard()
            return
        if value == "1":
            self._state = "target_type"
            self._render_target_type_panel(cancel_to="取消本次绑定并返回上一步")
            return
        actions = {"2": "rename", "3": "test", "4": "toggle", "5": "delete"}
        action = actions.get(value)
        if not action:
            self._invalid()
            return
        self._selected_target_action = action
        self._state = "target_select"
        self._render_target_select()

    def _handle_target_type(self, value: str) -> None:
        if value.upper() == "B":
            self.render_target_manager()
            return
        if value.upper() == "C":
            self.render_target_manager()
            return
        requested = {"1": "dingtalk_group", "2": "dingtalk_private", "3": "weixin_private"}.get(value)
        if not requested:
            self._invalid()
            return
        self._create_pairing(requested, target_manager_flow=True)

    def _handle_target_select(self, value: str) -> None:
        if value.upper() == "B":
            self.render_target_manager()
            return
        target = self._target_from_number(value)
        if not target:
            self._invalid()
            return
        action = self._selected_target_action
        if target.id == "local" and action in {"rename", "toggle", "delete"}:
            self._render("输出方式", "内置本地输出路径不允许改名、停用或删除。\n\n操作\nB 返回投递目标列表", kind="warning")
            self._state = "target_select"
            return
        self._selected_target_id = target.id
        if action == "rename":
            self._state = "target_rename"
            self._render("改名", f"当前名称：{target.display_name}\n\n可选输入\n输入<文本>保存新的备注名\nB 返回投递目标列表")
            return
        if action == "test":
            result = self.delivery.deliver_one(
                DeliverySpec(target.id),
                self._draft_job_stub(),
                _empty_run(),
                "M-CLAW 定时任务测试!",
            )
            status = result.get("status", "sent" if result.get("success") else "failed") if isinstance(result, dict) else "unknown"
            self._render("测试发送", f"目标：{TargetManager().display_target(target)}\n结果：{status}\n\n操作\nB 返回投递目标列表", kind="success" if isinstance(result, dict) and result.get("success") else "warning")
            self._state = "target_select"
            return
        if action == "toggle":
            updated = self.store.update_target(target.id, {"enabled": not target.enabled})
            self._render("停用/启用", f"目标：{TargetManager().display_target(updated)}\n状态：{'enabled' if updated.enabled else 'disabled'}\n\n操作\nB 返回投递目标列表", kind="success")
            self._state = "target_select"
            return
        if action == "delete":
            self._state = "target_delete_confirm"
            self._render("删除输出方式", f"将删除：{TargetManager().display_target(target)}\n已有 job 若仍引用该 target，后续投递会失败。\n\n操作\nY 确认删除\nN 取消删除并上一步")
            return
        self._invalid()

    def _handle_target_rename(self, value: str) -> None:
        if value.upper() == "B":
            self.render_target_manager()
            return
        if not value:
            self._invalid()
            return
        self.store.update_target(self._selected_target_id, {"display_name": value})
        self.render_target_manager()

    def _handle_target_delete_confirm(self, value: str) -> None:
        if value.upper() == "Y":
            self.store.delete_target(self._selected_target_id)
            self.render_target_manager()
            return
        if value.upper() == "N":
            self.render_target_manager()
            return
        self._invalid()

    def _render_new_step1(self) -> None:
        self._render(
            "定时任务结果输出方式",
            "- 保存本地输出\n- 选择已有输出方式\n- 添加钉钉/微信交付目标\n\n可选输入\n1 将定时任务结果保存到本地\n2 选择已有输出方式\n3 添加钉钉/微信输出方式\nB 返回上一步\nC 取消创建并丢弃草稿",
        )

    def _render_new_step2(self) -> None:
        self._render(
            "执行间隔规则",
            "每天/每周/每月\n间隔执行/单次任务\ncron表达式快速创建\n\n可选输入\n1 配置每天执行\n2 配置每周执行\n3 配置每月执行\n4 配置间隔执行\n5 配置单次任务\n6 输入cron表达式\nB 返回上一步\nC 取消创建并丢弃草稿",
        )

    def _render_new_output_choice(self) -> None:
        current = _delivery_output_display(self._draft.delivery) if self._draft else ""
        self._render(
            "输出设置",
            "\n".join([
                "完整运行结果会保存到config中的输出目录。",
                "可以把输出结果保存到你指定的位置。",
                "",
                f"当前设置：{current or '不单独保存模型输出结果'}",
                "",
                "可选输入",
                "1 不单独保存并进入下一步",
                "2 输出内容保存到你指定的位置",
                "B 返回上一步",
                "C 取消创建并丢弃草稿",
            ]),
        )

    def _render_output_template_prompt(self) -> None:
        self._render(
            "输出设置",
            "\n".join([
                "可设定输出结果的文件名模板。",
                "",
                "当前可用：",
                "",
                "日期占位符：yyyy-mm-dd",
                "例如：yyyy-mm-dd-会议纪要",
                "",
                "可选输入",
                "输入<模板>并进入下一步",
                "B 返回上一步",
                "C 取消创建并丢弃草稿",
            ]),
        )

    def _render_existing_targets(self) -> None:
        ready_targets = [target for target in self.store.list_targets(enabled_only=True) if TargetManager().validate_target(target).route_status == "ready"]
        self._target_map = [target.id for target in ready_targets]
        lines = [""]
        for idx, target in enumerate(ready_targets, 1):
            lines.append(f"{idx}. {TargetManager().display_target(target)}")
        lines.extend(["", "可选输入", "输入 <目标编号> 使用该已有输出方式", "B 返回投递目标选择", "C 取消创建并丢弃草稿"])
        self._state = "new_existing_targets"
        self._render("已有输出方式", "\n".join(lines))

    def _render_target_type_panel(self, *, cancel_to: str) -> None:
        self._render(
            "新增输出方式",
            f"- 钉钉群聊\n- 钉钉私聊\n- 微信私聊\n\n注意：\n输出结果到钉钉/微信对应频道需要开启相应Session\n若在M-CLAW初始化阶段未进行配置：\nmclaw dingtalk login\nmclaw weixin login\n\n可选输入\n1 创建钉钉群聊绑定码\n2 创建钉钉私聊绑定码\n3 创建微信私聊绑定码\nB 返回投递目标选择\nC {cancel_to}",
        )

    def _create_pairing(self, requested_type: str, *, target_manager_flow: bool = False) -> None:
        now = self.now()
        code = new_pairing_code()
        pairing = SchedulerTargetPairing(
            code=code,
            requested_type=requested_type,  # type: ignore[arg-type]
            status="waiting",
            expires_at=now + 600,
            created_at=now,
            updated_at=now,
        )
        self.store.create_pairing(pairing)
        self._pairing_code = code
        self._pairing_target_manager_flow = target_manager_flow
        self._last_pairing_status = "waiting"
        self._state = "pairing_wait"
        self._render_pairing_wait(target_manager_flow=target_manager_flow)

    def _render_pairing_wait(self, *, target_manager_flow: bool | None = None) -> None:
        if target_manager_flow is None:
            target_manager_flow = self._pairing_target_manager_flow
        pairing = self.store.get_pairing(self._pairing_code)
        target_type = pairing.requested_type if pairing else ""
        remaining = max(0, int((pairing.expires_at if pairing else self.now()) - self.now()))
        if pairing and pairing.status == "bound":
            self._render_pairing_bound()
            return
        status_line = "还没有收到绑定消息。" if not pairing or pairing.status == "waiting" else f"状态：{pairing.status} · {pairing.error}"
        instruction = _binding_instruction(target_type, self._pairing_code)
        lines = [
            f"绑定{_target_type_label(target_type)}",
            "",
            "请在目标聊天里发送：",
            "",
            instruction,
            "",
            "绑定码 10 分钟内有效。",
            "等待绑定中...",
            "",
            f"绑定码：{self._pairing_code}",
            f"类型：{_target_type_label(target_type)}",
            f"剩余时间：{remaining // 60:02d}:{remaining % 60:02d}",
            "",
            status_line,
            "",
            "请确认对应网关正在运行：",
            "- 钉钉：mclaw dingtalk",
            "- 微信：mclaw weixin",
            "",
            "可选输入",
            "B 取消本次绑定并返回上一步",
            "C 取消创建并丢弃草稿" if not target_manager_flow else "C 取消本次绑定并返回投递目标列表",
        ]
        self._render("等待绑定结果", "\n".join(lines), kind="warning")

    def _render_pairing_bound(self) -> None:
        pairing = self.store.get_pairing(self._pairing_code)
        target = self.store.get_target(pairing.target_id) if pairing and pairing.target_id else None
        use_action = "2 返回投递目标列表" if self._pairing_target_manager_flow else "2 使用这个目标并进入下一步"
        cancel_action = "C 取消本次绑定并返回投递目标列表" if self._pairing_target_manager_flow else "C 取消创建并丢弃草稿"
        lines = [
            f"类型：{_target_type_label(target.type if target else '')}",
            f"名称：{target.display_name if target else '-'}",
            f"状态：{target.route_status if target else '-'}",
            "",
        ]
        if self._pairing_target_manager_flow:
            lines.extend(["- 返回投递目标列表", "", "操作"])
        else:
            lines.append("可选输入")
        lines.extend([
            "1 测试发送",
            use_action,
            "B 返回投递目标类型选择",
            cancel_action,
        ])
        self._state = "pairing_bound"
        self._render("已绑定目标", "\n".join(lines), kind="success")

    def _render_target_select(self) -> None:
        targets = self.store.list_targets(enabled_only=False)
        self._target_map = [target.id for target in targets]
        action_label = {
            "rename": "改名",
            "test": "测试发送",
            "toggle": "停用/启用",
            "delete": "删除此输出方式",
        }.get(self._selected_target_action, "选择此输出方式")
        lines = [action_label, "", "#   名称                        状态"]
        for idx, target in enumerate(targets, 1):
            enabled = "enabled" if target.enabled else "disabled"
            lines.append(f"{idx:<3} {TargetManager().display_target(target):<26} {target.route_status} · {enabled}")
        lines.extend(["", "可选输入", "输入<目标编号>执行所选操作", "B 返回投递目标列表"])
        self._render(action_label, "\n".join(lines))

    def _render_job_select(self, title: str) -> None:
        jobs = self.store.list_jobs(include_paused=True)
        if self._job_page_offset >= len(jobs):
            self._job_page_offset = max(0, ((max(0, len(jobs) - 1)) // 10) * 10)
        page_jobs = jobs[self._job_page_offset : self._job_page_offset + 10]
        self._job_has_next = self._job_page_offset + 10 < len(jobs)
        self._job_map = [job.id for job in page_jobs]
        lines = [title, "", "#   名称              状态      下次执行"]
        for idx, job in enumerate(page_jobs, 1):
            lines.append(f"{idx:<3} {job.name:<16} {'enabled' if job.enabled else 'paused':<9} {_fmt_time(job.next_run_at)}")
        lines.extend(["", "可选输入", "输入<任务编号>选择任务", "B 返回控制台"])
        if len(jobs) > 10:
            lines.append("8 下一页   9 上一页")
        self._render(title, "\n".join(lines))

    def _render_job_detail(self, job: SchedulerJob) -> None:
        target = self.store.get_target(job.delivery.target_id)
        recent = self.store.list_runs(job.id, limit=1)
        self._state = "job_detail"
        self._render(
            "定时任务详情",
            "\n".join([
                f"编号        {self._job_map.index(job.id) + 1 if job.id in self._job_map else '-'}",
                f"名称        {job.name}",
                f"状态        {'enabled' if job.enabled else 'paused'}",
                f"时间        {humanize_schedule(job.schedule)}",
                f"下次执行    {_fmt_time(job.next_run_at)}",
                f"投递目标    {TargetManager().display_target(target) if target else job.delivery.target_id}",
                f"模型输出    {_delivery_output_display(job.delivery) or '不单独保存'}",
                f"最大API调用 {job.max_iterations}",
                f"最大运行时常 {job.timeout_seconds}s",
                f"最近结果    {recent[0].status if recent else '-'}",
                "",
                "Prompt",
                job.prompt,
                "",
                "操作",
                "1 立即运行   2 暂停/恢复   3 运行记录   4 删除",
                "B 返回任务列表",
            ]),
        )

    def _render_runs_scope(self) -> None:
        self._render(
            "运行记录",
            "选择运行记录范围\n\n操作\na 查看全部任务\n输入<任务编号>查看单个任务运行记录\nB 返回控制台",
        )

    def _render_runs_list(self, job_id: str | None, *, reset_page: bool = False) -> None:
        if reset_page or job_id != self._run_page_job_id:
            self._run_page_offset = 0
        self._run_page_job_id = job_id
        runs = self.store.list_runs(job_id=job_id, limit=11, offset=self._run_page_offset)
        page_runs = runs[:10]
        self._run_has_next = len(runs) > 10
        self._run_map = [run.id for run in page_runs]
        self._selected_run_scope = job_id
        self._state = "runs_list"
        job = self.store.get_job(job_id) if job_id else None
        lines = [f"范围        {job.name if job else '全部任务'}", "", "#   状态       计划时间        任务              投递      Run Output"]
        for idx, run in enumerate(page_runs, 1):
            run_job = self.store.get_job(run.job_id)
            lines.append(f"{idx:<3} {run.status:<10} {_fmt_time(run.scheduled_for):<15} {(run_job.name if run_job else run.job_id):<16} {(run.delivery_result or {}).get('status', '-'):<8} {_short_path(run.output_path)}")
        if not page_runs:
            lines.append("-   -          -              暂无记录")
        lines.extend(["", "操作", "输入<记录编号> 查看详情   8 下一页   9 上一页   B 返回范围选择"])
        self._render("运行记录", "\n".join(lines))

    def _render_run_completed(self, run: Any, actions: list[str]) -> None:
        lines = [
            f"状态：{run.status}",
            f"结果保存路径：{run.output_path or '-'}",
            f"指定结果保存路径：{_final_response_output_path(run) or '未单独保存'}",
            f"Delivery：{run.delivery_result.get('status') if run.delivery_result else '-'}",
            "",
            "操作",
            *actions,
        ]
        self._render("运行完成", "\n".join(lines), kind="success" if run.status == "succeeded" else "warning")

    def _render_preview(self) -> None:
        assert self._draft and self._draft.schedule
        next_run = next_run_after(self._draft.schedule, datetime.fromtimestamp(self.now(), ZoneInfo(self._draft.schedule.timezone)))
        target = self.store.get_target(self._draft.delivery.target_id)
        max_iterations, timeout_seconds = self._scheduler_run_limits()
        self._render(
            "预览定时任务",
            "\n".join([
                f"任务：{self._draft.name}",
                f"时间：{humanize_schedule(self._draft.schedule)}",
                f"目标：{TargetManager().display_target(target) if target else self._draft.delivery.target_id}",
                f"模型输出结果：{_delivery_output_display(self._draft.delivery) or '不单独保存'}",
                f"Session：{self._draft.session_policy}",
                f"最大API调用：{max_iterations}",
                f"最大运行时间：{timeout_seconds}s",
                f"下次执行：{_fmt_time(next_run.timestamp() if next_run else None)}",
                "",
                "操作",
                "1 创建并启用",
                "2 创建但暂停",
                "3 返回第一步",
                "B 返回上一步",
                "C 取消创建并丢弃草稿",
            ]),
        )

    def _create_job_from_draft(self, *, enabled: bool) -> SchedulerJob:
        assert self._draft and self._draft.schedule
        now = self.now()
        next_run = next_run_after(self._draft.schedule, datetime.fromtimestamp(now, ZoneInfo(self._draft.schedule.timezone)))
        max_iterations, timeout_seconds = self._scheduler_run_limits()
        job = SchedulerJob(
            id=new_job_id(),
            name=self._draft.name,
            enabled=enabled,
            schedule=self._draft.schedule,
            prompt=self._draft.prompt,
            enabled_toolsets=self._draft.enabled_toolsets,
            workdir=self._draft.workdir,
            session_policy=self._draft.session_policy,  # type: ignore[arg-type]
            session_id="",
            delivery=self._draft.delivery,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
            concurrency_policy="skip",
            next_run_at=next_run.timestamp() if enabled and next_run else None,
            last_run_at=None,
            failure_count=0,
            created_at=now,
            updated_at=now,
            status="idle" if enabled else "paused",
        )
        return self.store.create_job(job)

    def _scheduler_run_limits(self) -> tuple[int, int]:
        scheduler_cfg = self.config.get("scheduler", {}) if isinstance(self.config, dict) else {}
        return (
            int(scheduler_cfg.get("default_max_iterations", 200) or 200),
            int(scheduler_cfg.get("default_timeout_seconds", 3600) or 3600),
        )

    def _schedule_raw_from_input(self, state: str, value: str) -> dict[str, Any]:
        if state == "new_schedule_daily":
            return {"type": "daily", "time": value}
        if state == "new_schedule_weekly":
            weekday, _, time_text = value.partition(" ")
            return {"type": "weekly", "weekday": weekday, "time": time_text.strip()}
        if state == "new_schedule_monthly":
            day, _, time_text = value.partition(" ")
            return {"type": "monthly", "day": day, "time": time_text.strip()}
        if state == "new_schedule_interval":
            return {"type": "interval", "every": value}
        if state == "new_schedule_once":
            return {"type": "once", "at": value}
        return {"type": "cron", "expr": value}

    def _default_toolsets(self) -> list[str]:
        configured = list(self.config.get("toolsets") or ["mclaw-required"])
        for name in self.available_toolsets():
            if name not in configured:
                configured.append(name)
        result = [name for name in configured if validate_toolset(name)]
        return result or ["mclaw-required"]

    def _draft_job_stub(self) -> SchedulerJob:
        now = self.now()
        return SchedulerJob(
            id="job_test",
            name="M-CLAW 定时任务测试",
            enabled=True,
            schedule=parse_schedule({"type": "interval", "every": "1d"}, default_timezone="Asia/Shanghai"),
            prompt="M-CLAW 定时任务测试!",
            enabled_toolsets=["mclaw-required"],
            workdir=self.workspace,
            session_policy="task_thread",
            session_id="",
            delivery=DeliverySpec("local"),
            max_iterations=1,
            timeout_seconds=30,
            concurrency_policy="skip",
            next_run_at=None,
            last_run_at=None,
            failure_count=0,
            created_at=now,
            updated_at=now,
        )

    def _job_from_number(self, value: str) -> SchedulerJob | None:
        try:
            idx = int(value) - 1
        except ValueError:
            return None
        if idx < 0 or idx >= len(self._job_map):
            return None
        return self.store.get_job(self._job_map[idx])

    def _target_from_number(self, value: str):
        try:
            idx = int(value) - 1
        except ValueError:
            return None
        if idx < 0 or idx >= len(self._target_map):
            return None
        return self.store.get_target(self._target_map[idx])

    def _rerender_current(self) -> None:
        if self._last_render_title:
            self._render(self._last_render_title, self._last_render_body, kind=self._last_render_kind)
            return
        self._render("M-Claw Scheduler", "无效输入：请输入本页操作区列出的内容。（颜色红色）")

    def _invalid(self) -> None:
        self._error = "无效输入：请输入本页操作区列出的内容。（颜色红色）"
        self._rerender_current()

    def _render(self, title: str, body: str, *, kind: str = "info") -> None:
        self._last_render_title = title
        self._last_render_body = body
        self._last_render_kind = kind
        detail = self._error or None
        self.render_notice(title, body, detail, kind)


def _empty_run() -> Any:
    class _Run:
        id = "run_test"
        output_path = ""
    return _Run()


def _fmt_time(value: float | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")


def _short_path(path: str) -> str:
    if not path:
        return "-"
    return "..." + path[-24:] if len(path) > 27 else path


def _final_response_output_path(run: Any) -> str:
    result = getattr(run, "delivery_result", {}) or {}
    saved = result.get("final_response_output") if isinstance(result, dict) else None
    if not isinstance(saved, dict) or not saved.get("success"):
        return ""
    return str(saved.get("path") or "")


def _delivery_output_display(delivery: DeliverySpec) -> str:
    path = str(getattr(delivery, "final_response_path", "") or "").strip()
    template = str(getattr(delivery, "final_response_filename_template", "") or "").strip()
    if path and template:
        return f"{path} / {template}"
    return path


def _truncate(text: str, limit: int = 500) -> str:
    value = text or "-"
    lines = value.splitlines()[:6]
    value = "\n".join(lines)
    return value[:limit] + ("..." if len(value) > limit else "")


def _token_summary(usage: dict[str, Any]) -> str:
    if not usage:
        return "-"
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", "-"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", "-"))
    return f"input {input_tokens} · output {output_tokens}"


def _target_type_label(value: str) -> str:
    return {
        "local": "本地输出",
        "dingtalk_group": "钉钉群聊",
        "dingtalk_private": "钉钉私聊",
        "weixin_private": "微信私聊",
    }.get(value, value or "-")


def _binding_instruction(target_type: str, code: str) -> str:
    if target_type == "dingtalk_group":
        return f"@钉钉机器人名称 /schedule-bind {code} 研发群"
    if target_type == "dingtalk_private":
        return f"/schedule-bind {code} 张三"
    if target_type == "weixin_private":
        return f"/schedule-bind {code} 李四"
    return f"/schedule-bind {code} <备注名>"


def _job_select_title(scope: str | None) -> str:
    return {
        "run_now": "立即运行",
        "toggle": "暂停/恢复",
        "delete": "删除任务",
    }.get(scope or "", "查看任务")
