# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reuse the terminal scheduler wizard without a second scheduler engine."""

from mclaw.application.scheduler_transport import OPERATIONS, decode, encode


class SchedulerClient:
    def __init__(self, client, area):
        self.client, self.area = client, area

    def __getattr__(self, operation):
        if self.area == "engine" and operation in {"tick_once", "shutdown"}:
            return lambda *args, **kwargs: None
        if operation not in OPERATIONS.get(self.area, ()):
            raise AttributeError(operation)

        def call(*args, **kwargs):
            return decode(self.client.request("POST", "/terminal/scheduler", {
                "area": self.area, "operation": operation, "args": encode(args), "kwargs": encode(kwargs)}, timeout=120)["value"])
        return call


def coordinator(owner):
    from mclaw.cli.runtime.scheduler import RuntimeSchedulerCoordinator
    from mclaw.tools.toolsets import get_optional_toolset_names

    return RuntimeSchedulerCoordinator(
        store=SchedulerClient(owner.service_client, "store"), engine=SchedulerClient(owner.service_client, "engine"),
        delivery=SchedulerClient(owner.service_client, "delivery"), config=owner.config, workspace=owner.workspace_path,
        render_notice=lambda title, message, detail=None, kind="info": owner._get_commands_renderer().render_text_panel(
            title, message, detail=detail, kind=kind, namespace="scheduler"), available_toolsets=get_optional_toolset_names,
    )
