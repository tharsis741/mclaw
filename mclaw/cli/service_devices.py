# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Device management data for the terminal's existing selector dialogs."""

from mclaw.application.device_bridge import READ_OPERATIONS, MANAGEMENT_OPERATIONS


class DeviceClient:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, operation):
        if operation not in READ_OPERATIONS | MANAGEMENT_OPERATIONS:
            raise AttributeError(operation)

        def call(*args, **kwargs):
            from mclaw.dsoftbus.runtime import DsoftbusRuntimeError
            result = self.client.request("POST", "/terminal/devices", {
                "operation": operation, "args": args, "kwargs": kwargs}, timeout=90)
            if result.get("error"):
                error = result["error"]
                raise DsoftbusRuntimeError(error["code"], outcome_unknown=error.get("outcome_unknown", False))
            return result.get("value")
        return call

    def start(self):
        return self.health()

    def begin_shutdown(self):
        pass

    def stop(self, *_args):
        pass
