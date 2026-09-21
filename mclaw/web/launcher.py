# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Launch or reuse the background service independently of a browser window."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from mclaw.constants import get_mclaw_home


def running_service(home: Path) -> dict | None:
    """Authenticate an advertised endpoint before treating it as reusable."""
    try:
        value = json.loads((home / "web" / "service.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if (type(value.get("pid")) is not int or value["pid"] <= 0
                or not isinstance(value.get("token"), str) or not value["token"]):
            return None
        port = int(value["port"])
        if not 0 < port < 65536:
            return None
        response = httpx.get(f"http://127.0.0.1:{port}/api/v1/health", headers={"Authorization": f"Bearer {value['token']}"}, timeout=0.5, trust_env=False)
        status = response.json() if response.status_code == 200 else None
        if isinstance(status, dict) and status.get("api_version") == 1:
            return value
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        pass
    return None


def ensure_service(home: Path | None = None) -> dict:
    """Reuse the installed Python runtime; never create another environment."""
    home = (home or get_mclaw_home()).resolve()
    existing = running_service(home)
    if existing:
        return existing
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    else:
        options["start_new_session"] = True
    environment = dict(os.environ, MCLAW_HOME=str(home), PYTHONIOENCODING="utf-8")
    # The service has a stable directory; per-session workers receive their own cwd.
    with (log_dir / "web-service.log").open("ab") as output:
        process = subprocess.Popen([sys.executable, "-m", "mclaw.web.server"],
                                   cwd=str(home), env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, **options)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        existing = running_service(home)
        if existing:
            return existing
        if process.poll() is not None:
            # Another launcher can win the instance lock long before its HTTP
            # startup completes. Wait for that live owner using the same deadline.
            from mclaw.cli.runtime.session_lock import InteractiveSessionLock, _is_process_alive

            owner = InteractiveSessionLock("mclaw-application-service", home / "web" / "locks")._read_lock()
            if not _is_process_alive(int(owner.get("pid") or 0)):
                raise RuntimeError(f"后台服务启动失败，请查看 {log_dir / 'web-service.log'}")
        time.sleep(0.1)
    raise RuntimeError(f"后台服务启动超时，请查看 {log_dir / 'web-service.log'}")


def open_browser(url: str) -> bool:
    """Open the device's browser through its native application launcher."""
    from mclaw.runtime.manager import RuntimeManager

    if RuntimeManager.detect() == "kaihong":
        executable = shutil.which("aa")
        if not executable:
            return False
        # Kaihong's default URI resolver can open a spurious file-error dialog.
        # Address its browser explicitly; other images can use URI resolution.
        for arguments in (
            ["-b", "com.kaihong.browser", "-a", "MainAbility", "-U", url],
            ["-A", "ohos.want.action.viewData", "-U", url],
        ):
            try:
                result = subprocess.run(
                    [executable, "start", *arguments],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=8, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            # Some AA versions report a launch error with exit status zero.
            if result.returncode == 0 and "start ability successfully" in result.stdout.lower():
                return True
        return False
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


def launch() -> None:
    """Open the local application using a fragment-held bootstrap credential."""
    from mclaw.cli.tui.console import print_plain
    try:
        if not (Path(__file__).parent / "static" / "index.html").is_file():
            raise RuntimeError("Web 页面尚未构建。请在 webui 目录运行 npm ci 和 npm run build。")
        service = ensure_service()
        url = f"http://127.0.0.1:{service['port']}/#token={service['token']}"
        if open_browser(url):
            print_plain(f"M-CLAW Web UI已打开：http://127.0.0.1:{service['port']}/")
        else:
            print_plain(f"请在本机浏览器打开：{url}")
        print_plain("停止服务：mclaw stop · 查看状态：mclaw status")
    except RuntimeError as exc:
        print_plain(str(exc))
        raise SystemExit(1) from exc


def _service_owner_pid(home: Path) -> int | None:
    """Find a live service owner even while its HTTP listener is closing."""
    from mclaw.cli.runtime.session_lock import InteractiveSessionLock, _is_process_alive

    owner = InteractiveSessionLock("mclaw-application-service", home / "web" / "locks")._read_lock()
    try:
        pid = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if _is_process_alive(pid) else None


def show_status() -> None:
    """Report local service state without starting a process or opening a page."""
    from mclaw.cli.tui.console import print_plain

    home = get_mclaw_home().resolve()
    service = running_service(home)
    if service:
        print_plain(f"M-CLAW 服务运行中（PID {service['pid']}）")
        print_plain(f"Web UI：http://127.0.0.1:{service['port']}/")
        print_plain("停止服务：mclaw stop")
    elif pid := _service_owner_pid(home):
        print_plain(f"M-CLAW 服务暂时无法连接（PID {pid}），请稍后重试。")
    else:
        print_plain("M-CLAW 服务未运行。运行 mclaw 启动。")


def stop(*, timeout: float = 30.0) -> None:
    """Request normal shutdown and wait for the service to release ownership."""
    from mclaw.cli.tui.console import print_plain

    home = get_mclaw_home().resolve()
    service = running_service(home)
    if not service:
        if pid := _service_owner_pid(home):
            print_plain(f"M-CLAW 服务暂时无法连接（PID {pid}），请稍后重试。")
            raise SystemExit(1)
        print_plain("M-CLAW 服务未运行。")
        return
    try:
        response = httpx.post(
            f"http://127.0.0.1:{service['port']}/api/v1/service/stop",
            headers={"Authorization": f"Bearer {service['token']}"},
            timeout=5.0, trust_env=False,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print_plain("未能确认退出请求，请运行 mclaw status 检查服务状态。")
        raise SystemExit(1) from exc
    print_plain("正在停止 M-CLAW 服务，等待任务收尾…")
    deadline = time.monotonic() + timeout
    while _service_owner_pid(home) == service["pid"]:
        if time.monotonic() >= deadline:
            print_plain(f"退出请求已提交，服务仍在收尾（PID {service['pid']}）。运行 mclaw status 查看状态。")
            raise SystemExit(1)
        time.sleep(0.2)
    print_plain(f"M-CLAW 服务已停止（PID {service['pid']}）。")
