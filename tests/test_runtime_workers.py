# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import threading

from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.cli.runtime.workers import RuntimeWorkerHooks, RuntimeWorkerSupervisor


def test_runtime_worker_supervisor_processes_input_and_stops() -> None:
    runtime = InteractiveRuntime()
    handled = threading.Event()
    received: list[str] = []
    errors: list[Exception] = []

    def handle_input(text: str) -> None:
        received.append(text)
        handled.set()
        runtime.request_exit()

    supervisor = RuntimeWorkerSupervisor(
        runtime,
        RuntimeWorkerHooks(
            handle_input=handle_input,
            on_idle=lambda: None,
            on_animation_tick=lambda: None,
            on_error=errors.append,
            invalidate=lambda: None,
            animation_enabled=lambda: False,
            animation_interval=lambda _running: 0.01,
        ),
    )
    process_thread, animation_thread = supervisor.start()
    runtime.submit_text("hello")

    assert handled.wait(1)
    process_thread.join(1)
    animation_thread.join(1)
    assert received == ["hello"]
    assert errors == []
    assert not process_thread.is_alive()
    assert not animation_thread.is_alive()
