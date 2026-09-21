# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run terminal file commands in the owning worker, returning presentation data."""

from pathlib import Path

from mclaw.cli.runtime.file_safety_commands import RuntimeFileSafetyCommandCoordinator, RuntimeFileSafetyCommandHooks


def execute(worker, request, config, manager, coordinator):
    from mclaw.application.sessions import ApplicationError
    from mclaw.cli.config import load_config
    from mclaw.cli.runtime.workspace_trust import is_workspace_trusted
    from mclaw.safety.context_rollback import ContextRollbackManager
    from mclaw.tools.checkpoint_manager import clear_all, maybe_auto_prune_checkpoints, store_status

    command, arguments = request.get("command"), str(request.get("arguments") or "")
    if command not in {"rollback", "checkpoints"}:
        raise ApplicationError("INVALID_RECOVERY", "未知文件命令")
    records = []

    def record(name):
        return lambda *args, **kwargs: records.append({"method": name, "args": args, "kwargs": kwargs})

    def workspace(options):
        cwd, file_path = str(worker.workspace), None
        if options.get("dir"):
            cwd = str(Path(options["dir"]).expanduser().resolve())
        elif options.get("path"):
            path = Path(options["path"]).expanduser().resolve()
            cwd, file_path = (str(path), None) if path.is_dir() else (str(path.parent), path.name)
        if not is_workspace_trusted(load_config(strict=True), cwd):
            raise ApplicationError("WORKSPACE_UNTRUSTED", "请先通过此目录中的会话确认工作区信任，再执行文件恢复", 409)
        return cwd, file_path

    def resolve(reference, checkpoints):
        try:
            index = int(reference) - 1
        except ValueError:
            return reference
        if 0 <= index < len(checkpoints):
            return checkpoints[index]["hash"]
        record("render_notice")("Checkpoint", f"无效 checkpoint 编号。请使用 1-{len(checkpoints)}。", kind="warning")
        return None

    def restore_context(metadata, mode):
        if not config.get("checkpoints", {}).get("restore_chat_context", True) or metadata.get("session_id") != worker.session["id"]:
            return
        result = ContextRollbackManager(worker.db, worker.agent).apply(
            session_id=worker.session["id"], marker_message_id=metadata.get("message_id_before_turn"), mode=mode,
            checkpoint_hash=metadata.get("commit") or metadata.get("rollback_target"),
            operation_id=metadata.get("operation_id"), metadata=metadata, scope="tail")
        if not result.get("skipped"):
            record("render_notice")("上下文回滚", f"已同步回退聊天上下文：软失效 {result.get('invalidated', 0)} 条消息。", kind="success")

    render = {name: record(name) for name in RuntimeFileSafetyCommandHooks.__dataclass_fields__ if name.startswith("render_")}
    render["render_notice"] = lambda title, message, detail, kind: record("render_notice")(title, message, detail=detail, kind=kind)
    render["render_rollback_result"] = lambda result, grouped, label: record("render_rollback_result")(result, grouped=grouped, ref_label=label)
    render["render_project_restore_result"] = lambda result, path: record("render_project_restore_result")(result, file_path=path)
    command_coordinator = RuntimeFileSafetyCommandCoordinator(RuntimeFileSafetyCommandHooks(
        get_checkpoint_manager=lambda: manager, create_rollback_coordinator=lambda _: coordinator,
        resolve_rollback_workspace=workspace, resolve_checkpoint_ref=resolve,
        checkpoint_config=lambda: config.get("checkpoints", {}), store_status=store_status,
        prune_checkpoints=lambda days, orphans: maybe_auto_prune_checkpoints(retention_days=days, delete_orphans=orphans, min_interval_hours=0),
        clear_checkpoints=clear_all, restore_chat_context=restore_context, **render,
    ))
    getattr(command_coordinator, "handle_" + command)(arguments)
    return {"records": records}
