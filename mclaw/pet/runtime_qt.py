"""PySide6 desktop pet sidecar runtime.

This module is imported only inside the sidecar process.
"""

from __future__ import annotations

import json
import os
import queue
import time
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.pet.assets import resolve_asset_dir
from mclaw.pet.events import PetEvent, PetState
from mclaw.utils import atomic_json_write


REST_STATE = PetState.IDLE.value
SLEEP_STATE = PetState.SLEEPING.value
QUIET_REST_STATES = {PetState.IDLE.value, PetState.SLEEPING.value}
QUIET_EVENTS = {"app_started"}


STATE_BY_EVENT = {
    "app_started": REST_STATE,
    "turn_started": PetState.JUMPING.value,
    "model_streaming": PetState.TYPING.value,
    "tool_started": PetState.READING.value,
    "tool_finished": PetState.RUNNING_RIGHT.value,
    "status_changed": None,
    "waiting_for_user": PetState.WAITING.value,
    "turn_completed": PetState.WAVING.value,
    "turn_failed": PetState.FAILED.value,
    "turn_interrupted": PetState.FAILED.value,
    "background_process_updated": PetState.TYPING.value,
    "background_process_completed": PetState.WAVING.value,
    "delegation_started": PetState.CARRYING.value,
    "delegation_task_started": PetState.CARRYING.value,
    "delegation_task_tool": PetState.READING.value,
    "delegation_task_completed": PetState.CARRYING.value,
    "delegation_task_failed": PetState.FAILED.value,
    "delegation_completed": PetState.WAVING.value,
}

EVENT_MESSAGES = {
    "app_started": "休息中，我眯一会儿，叫我就行！",
    "turn_started": "收到，开始处理！",
    "model_streaming": "敲键盘中，字在冒出来了！",
    "tool_started": "翻资料中，我把线索翻出来！",
    "tool_finished": "继续推进，工具执行完成，接着整！",
    "status_changed": "状态更新，我在跟进进度！",
    "waiting_for_user": "等待确认，这个 Skill 要你点头才继续！",
    "turn_completed": "完成啦！任务处理完成。",
    "turn_failed": "出问题了，这里卡住了，需要看一下！",
    "turn_interrupted": "已中断，我先刹住了！",
    "background_process_updated": "后台盯梢，我边敲边等结果！",
    "background_process_completed": "后台完成，后台任务有结果了！",
    "delegation_started": "搬文件中，我把任务分出去！",
    "delegation_task_started": "搬文件中，其中一路出发了！",
    "delegation_task_tool": "翻资料中，小分队在查证！",
    "delegation_task_completed": "文件送达，一路结果回来了！",
    "delegation_task_failed": "小分队卡住，有一路没跑过去！",
    "delegation_completed": "汇总完成，分头处理收拢了！",
    "manual_state": "手动测试，状态切过去了！",
    "pet_clicked": "戳到我了，继续执行！",
    "position_reset": "位置已重置，我挪回默认位置！",
    "file_dropped": "吃到文件了！我转给 MClaw 分析！",
}

STATE_MESSAGES = {
    PetState.IDLE.value: "待命中，我在这儿！",
    PetState.RUNNING.value: "思考中，正在处理！",
    PetState.REVIEW.value: "核对中，我在看细节！",
    PetState.WAITING.value: "待命中，小爪就位！",
    PetState.SLEEPING.value: "休息中，我眯一会儿！",
    PetState.READING.value: "翻资料中，我在看细节！",
    PetState.TYPING.value: "敲键盘中，字在冒出来！",
    PetState.CARRYING.value: "搬文件中，我去送一趟！",
    PetState.WAVING.value: "完成啦，这轮收好了！",
    PetState.FAILED.value: "出问题了，需要看一下！",
    PetState.JUMPING.value: "开工，我动起来了！",
    PetState.RUNNING_LEFT.value: "我去左边看看！",
    PetState.RUNNING_RIGHT.value: "我去右边看看！",
}


def run_pet(event_queue, command_queue, runtime_config: dict) -> None:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except Exception:
        return

    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    parent_pid = int(runtime_config.get("parent_pid") or 0)
    parent_watch = _ParentProcessWatch(parent_pid)
    asset_dir = resolve_asset_dir(str(runtime_config.get("asset") or "robot-dark"))
    window = _PetWindow(QtCore, QtGui, QtWidgets, asset_dir, runtime_config, command_queue)
    window.show()

    def pump() -> None:
        while True:
            try:
                raw = event_queue.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError, ValueError):
                app.quit()
                return
            event = PetEvent.from_dict(raw)
            if event.type == "app_exiting":
                _quit_pet(app, window)
                return
            window.handle_event(event)

    def watch_parent() -> None:
        if not parent_watch.is_alive():
            _quit_pet(app, window)

    timer = QtCore.QTimer()
    timer.timeout.connect(pump)
    timer.start(80)
    parent_timer = QtCore.QTimer()
    parent_timer.timeout.connect(watch_parent)
    parent_timer.start(1000)
    try:
        app.exec()
    finally:
        parent_watch.close()
        _quit_pet(app, window)


class _ParentProcessWatch:
    def __init__(self, parent_pid: int = 0):
        self.parent_pid = int(parent_pid or 0)
        self._windows_handle = _windows_open_process(self.parent_pid) if os.name == "nt" else None

    def is_alive(self) -> bool:
        if self._windows_handle is not None:
            return _windows_handle_alive(self._windows_handle)
        return _parent_process_alive(self.parent_pid)

    def close(self) -> None:
        if self._windows_handle is not None:
            _windows_close_handle(self._windows_handle)
            self._windows_handle = None


def _parent_process_alive(parent_pid: int = 0) -> bool:
    try:
        import multiprocessing as _mp

        parent = _mp.parent_process()
        if parent is not None:
            return parent.is_alive()
    except Exception:
        pass

    if not parent_pid:
        return True
    if parent_pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_alive(parent_pid)
    try:
        os.kill(parent_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _windows_open_process(pid: int):
    if not pid:
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return kernel32.OpenProcess(0x101000, False, int(pid)) or None
    except Exception:
        return None


def _windows_handle_alive(process) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait_result = kernel32.WaitForSingleObject(process, 0)
        if wait_result == 0:
            return False
        if wait_result == 0x102:
            return True
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    except Exception:
        return False


def _windows_close_handle(process) -> None:
    try:
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(process)
    except Exception:
        pass


def _windows_pid_alive(pid: int) -> bool:
    process = _windows_open_process(pid)
    if not process:
        try:
            import ctypes

            return ctypes.get_last_error() == 5
        except Exception:
            return False
    try:
        return _windows_handle_alive(process)
    finally:
        _windows_close_handle(process)


def _quit_pet(app, window=None) -> None:
    try:
        if window is not None:
            tray = getattr(window, "tray", None)
            if tray is not None:
                tray.hide()
            widget = getattr(window, "window", None)
            if widget is not None:
                widget.hide()
                widget.close()
    except Exception:
        pass
    try:
        app.quit()
    except Exception:
        pass


def _drag_state_for_dx(dx: int) -> str:
    return PetState.RUNNING_LEFT.value if dx < 0 else PetState.RUNNING_RIGHT.value


class _PetWindow:
    def __init__(self, QtCore, QtGui, QtWidgets, asset_dir: Path, config: dict, command_queue=None):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.config = config
        self.command_queue = command_queue
        self.asset_dir = asset_dir
        self.state = REST_STATE
        self.frame = 0
        self.drag_pos = None
        self.press_pos = None
        self.press_started_at = 0.0
        self.state_hold_until = 0.0
        self.bubble_until = 0.0
        self.last_activity_at = time.monotonic()
        self.sleep_after_seconds = _sleep_after_seconds(config)
        self.last_event_text = ""
        self.tray = None

        self.manifest = _load_manifest(asset_dir)
        self.frame_width = int(self.manifest.get("frame_width", 192))
        self.frame_height = int(self.manifest.get("frame_height", 208))
        self.states = self.manifest.get("states", {})
        self.scale = float(config.get("scale") or 1.0)

        self.window = QtWidgets.QWidget()
        flags = QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool
        if config.get("always_on_top", True):
            flags |= QtCore.Qt.WindowStaysOnTopHint
        self.window.setWindowFlags(flags)
        self.window.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        if config.get("click_through", False):
            self.window.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.window.setAcceptDrops(True)

        self.sprite_label = QtWidgets.QLabel(self.window)
        self.sprite_label.setAlignment(QtCore.Qt.AlignCenter)
        self.bubble_label = QtWidgets.QLabel(self.window)
        self.bubble_label.setAlignment(QtCore.Qt.AlignCenter)
        self.bubble_label.setWordWrap(False)
        self.bubble_label.setStyleSheet(
            "QLabel {"
            "color: white;"
            "background-color: rgba(20, 36, 54, 190);"
            "border: 1px solid rgba(144, 202, 249, 180);"
            "border-radius: 10px;"
            "font-size: 10px;"
            "font-weight: 500;"
            "padding: 6px 10px;"
            "}"
        )
        self.bubble_label.setText(self._display_label(self.state))
        self._resize_window()

        image_path = asset_dir / str(self.manifest.get("image") or "spritesheet.webp")
        self.sheet = QtGui.QPixmap(str(image_path))

        self.anim_timer = QtCore.QTimer()
        self.anim_timer.timeout.connect(self._advance)
        self.anim_timer.start(self._state_interval_ms(self.state))

        self.window.mousePressEvent = self._mouse_press
        self.window.mouseMoveEvent = self._mouse_move
        self.window.mouseReleaseEvent = self._mouse_release
        self.window.contextMenuEvent = self._context_menu
        self.window.dragEnterEvent = self._drag_enter
        self.window.dropEvent = self._drop_event
        self.window.setToolTip("M-Claw pet")
        self._setup_tray()
        self._place_window()
        self._render_frame()
        if self.state in QUIET_REST_STATES:
            self.bubble_label.hide()

    def show(self) -> None:
        self.window.show()

    def handle_event(self, event: PetEvent) -> None:
        self._mark_activity()
        next_state = event.state or STATE_BY_EVENT.get(event.type)
        if next_state:
            self.set_state(next_state)
        if event.type in QUIET_EVENTS:
            return
        self._set_bubble(event)

    def set_state(self, state: str) -> None:
        if state not in self.states:
            state = REST_STATE
        if state != self.state:
            self.state = state
            self.frame = 0
            self.state_hold_until = self._state_hold_deadline(state)
            self.anim_timer.setInterval(self._state_interval_ms(state))
            self._render_frame()

    def _advance(self) -> None:
        if self._should_sleep():
            self.set_state(SLEEP_STATE)
            self._render_frame()
            self._refresh_bubble()
            return
        spec = self.states.get(self.state, {})
        frames = max(1, int(spec.get("frames", 1)))
        self.frame += 1
        if self.frame >= frames:
            if spec.get("loop", True):
                self.frame = 0
            elif time.monotonic() < self.state_hold_until:
                self.frame = frames - 1
            else:
                self.set_state(REST_STATE)
                return
        self._render_frame()
        self._refresh_bubble()

    def _render_frame(self) -> None:
        spec = self.states.get(self.state, self.states.get(PetState.IDLE.value, {}))
        row = int(spec.get("row", 0))
        col = min(self.frame, max(0, int(spec.get("frames", 1)) - 1))
        cropped = self.sheet.copy(
            col * self.frame_width,
            row * self.frame_height,
            self.frame_width,
            self.frame_height,
        )
        effects = set(spec.get("effects") or [])
        if "shake" in effects and self.frame % 2:
            jittered = self.QtGui.QPixmap(cropped.size())
            jittered.fill(self.QtCore.Qt.transparent)
            painter = self.QtGui.QPainter(jittered)
            painter.drawPixmap(2, 0, cropped)
            painter.end()
            cropped = jittered
        size = self.sprite_label.size()
        self.sprite_label.setPixmap(cropped.scaled(size, self.QtCore.Qt.KeepAspectRatio, self.QtCore.Qt.SmoothTransformation))

    def _resize_window(self) -> None:
        sprite_width = int(self.frame_width * self.scale)
        width = max(sprite_width, int(225 * self.scale))
        sprite_height = int(self.frame_height * self.scale)
        bubble_height = 26 if self.config.get("show_bubble", True) else 0
        if bubble_height:
            bubble_height = max(50, int(50 * self.scale))
        self.window.resize(width, sprite_height + bubble_height)
        sprite_x = max(0, (width - sprite_width) // 2)
        self.sprite_label.setGeometry(sprite_x, 0, sprite_width, sprite_height)
        self.bubble_label.setGeometry(8, sprite_height, max(1, width - 16), bubble_height)
        self.bubble_label.setVisible(bool(bubble_height))

    def _set_bubble(self, event: PetEvent) -> None:
        if not self.config.get("show_bubble", True):
            return
        state = event.state or STATE_BY_EVENT.get(event.type) or self.state
        label = self._display_event(event, state)
        self.bubble_label.setText(label)
        self.last_event_text = label
        self.bubble_until = time.monotonic() + self._bubble_seconds_for_event(event)
        self.bubble_label.show()
        self.window.setToolTip(f"M-Claw pet: {label}")
        if self.tray is not None:
            self.tray.setToolTip(f"M-Claw pet: {label}")

    def _refresh_bubble(self) -> None:
        if not self.config.get("show_bubble", True):
            return
        if self.last_event_text and time.monotonic() < self.bubble_until:
            return
        self.last_event_text = ""
        if self.state in QUIET_REST_STATES:
            self.bubble_label.hide()
            return
        label = self._display_label(self.state)
        if self.bubble_label.text() != label:
            self.bubble_label.setText(label)
        self.bubble_label.show()

    @staticmethod
    def _display_label(state: str | None) -> str:
        return _one_line(STATE_MESSAGES.get(str(state or ""), STATE_MESSAGES[REST_STATE]))

    @staticmethod
    def _display_event(event: PetEvent, state: str | None) -> str:
        label = EVENT_MESSAGES.get(str(event.type or ""), STATE_MESSAGES.get(str(state or ""), STATE_MESSAGES[REST_STATE]))
        detail = _event_detail(event)
        return _one_line(f"{label} {detail}" if detail else label)

    @staticmethod
    def _state_hold_deadline(state: str) -> float:
        hold_seconds = {
            PetState.WAVING.value: 3.2,
            PetState.FAILED.value: 2.2,
            PetState.JUMPING.value: 0.9,
            PetState.RUNNING_LEFT.value: 0.9,
            PetState.RUNNING_RIGHT.value: 0.9,
            PetState.CARRYING.value: 1.1,
            PetState.WAITING.value: 1.2,
        }.get(state, 0.0)
        return time.monotonic() + hold_seconds

    def _bubble_seconds_for_event(self, event: PetEvent) -> float:
        base = float(self.config.get("bubble_seconds") or 2.5)
        return {
            "turn_completed": max(base, 4.2),
            "background_process_completed": max(base, 3.5),
            "delegation_completed": max(base, 3.8),
        }.get(event.type, base)

    def _state_interval_ms(self, state: str) -> int:
        spec = self.states.get(state, {})
        try:
            fps = float(spec.get("fps") or 7)
        except (TypeError, ValueError):
            fps = 7
        fps = max(1, min(fps, 30))
        return int(1000 / fps)

    def _place_window(self) -> None:
        remembered = _load_position()
        if remembered:
            self.window.move(remembered["x"], remembered["y"])
            return
        x = self.config.get("x")
        y = self.config.get("y")
        if isinstance(x, int) and isinstance(y, int):
            self.window.move(x, y)
            return
        screen = self.QtWidgets.QApplication.primaryScreen().availableGeometry()
        margin = 24
        pos = str(self.config.get("position") or "bottom_right")
        width = self.window.width()
        height = self.window.height()
        if pos == "bottom_left":
            self.window.move(screen.left() + margin, screen.bottom() - height - margin)
        elif pos == "top_left":
            self.window.move(screen.left() + margin, screen.top() + margin)
        elif pos == "top_right":
            self.window.move(screen.right() - width - margin, screen.top() + margin)
        else:
            self.window.move(screen.right() - width - margin, screen.bottom() - height - margin)

    def _mouse_press(self, event) -> None:
        self._mark_activity()
        if event.button() == self.QtCore.Qt.RightButton:
            return
        if event.button() == self.QtCore.Qt.LeftButton:
            self.press_pos = event.globalPosition().toPoint()
            self.press_started_at = time.monotonic()
            self.drag_pos = self.press_pos - self.window.frameGeometry().topLeft()

    def _mouse_move(self, event) -> None:
        if self.drag_pos is not None:
            self._mark_activity()
            current = event.globalPosition().toPoint()
            if self.press_pos is not None and (current - self.press_pos).manhattanLength() < 4:
                return
            self.window.move(current - self.drag_pos)
            dx = current.x() - self.press_pos.x() if self.press_pos is not None else 0
            self.set_state(_drag_state_for_dx(dx))

    def _mouse_release(self, event) -> None:
        current = event.globalPosition().toPoint()
        clicked = (
            self.press_pos is not None
            and (current - self.press_pos).manhattanLength() < 6
            and (time.monotonic() - self.press_started_at) < 0.45
        )
        self.drag_pos = None
        self.press_pos = None
        if clicked:
            self._on_click_feedback()
        else:
            _save_position(self.window.x(), self.window.y())
            self.set_state(REST_STATE)


    def _drag_enter(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop_event(self, event) -> None:
        self._mark_activity()
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path:
                    paths.append(path)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.set_state(PetState.CARRYING.value)
        self._set_bubble(PetEvent(type="file_dropped", state=PetState.CARRYING.value, text=str(len(paths))))
        command_queue = getattr(self, "command_queue", None)
        if command_queue is None:
            return
        try:
            command_queue.put_nowait({"type": "file_drop", "paths": paths, "ts": time.time()})
        except Exception:
            pass

    def _context_menu(self, event) -> None:
        menu = self._build_menu()
        menu.exec(event.globalPos())

    def _setup_tray(self) -> None:
        if not self.QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self._make_icon()
        tray = self.QtWidgets.QSystemTrayIcon(icon, self.window)
        tray.setToolTip("M-Claw pet: 待命中")
        tray.setContextMenu(self._build_menu())
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray = tray

    def _build_menu(self):
        menu = self.QtWidgets.QMenu(self.window)
        show_action = menu.addAction("Show / Hide")
        show_action.triggered.connect(self._toggle_visible)
        menu.addSeparator()
        reset_action = menu.addAction("Reset saved position")
        reset_action.triggered.connect(self._reset_position)
        bubble_action = menu.addAction("Toggle bubble")
        bubble_action.triggered.connect(self._toggle_bubble)
        menu.addSeparator()
        quit_action = menu.addAction("Quit pet")
        quit_action.triggered.connect(self.QtWidgets.QApplication.quit)
        return menu

    def _toggle_visible(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

    def _manual_state(self, state: str) -> None:
        self.set_state(state)
        self._set_bubble(PetEvent(type="manual_state", state=state, text="menu"))

    def _on_click_feedback(self) -> None:
        self._mark_activity()
        if self.state in (PetState.IDLE.value, PetState.WAITING.value, PetState.SLEEPING.value):
            self.set_state(PetState.JUMPING.value)
            self._set_bubble(PetEvent(type="pet_clicked", state=PetState.JUMPING.value))
        elif self.state in (PetState.RUNNING.value, PetState.REVIEW.value):
            self._set_bubble(PetEvent(type="pet_clicked", state=self.state))
        elif self.state == PetState.WAITING.value:
            self._set_bubble(PetEvent(type="pet_clicked", state=self.state))
        else:
            self.set_state(PetState.JUMPING.value)
            self._set_bubble(PetEvent(type="pet_clicked", state=PetState.JUMPING.value))

    def _reset_position(self) -> None:
        self._mark_activity()
        try:
            _state_path().unlink(missing_ok=True)
        except Exception:
            pass
        self.config["x"] = None
        self.config["y"] = None
        self._place_window()
        self._set_bubble(PetEvent(type="position_reset", state=self.state))

    def _toggle_bubble(self) -> None:
        self._mark_activity()
        self.config["show_bubble"] = not bool(self.config.get("show_bubble", True))
        self._resize_window()
        self._render_frame()

    def _on_tray_activated(self, reason) -> None:
        if reason == self.QtWidgets.QSystemTrayIcon.Trigger:
            self._toggle_visible()

    def _make_icon(self):
        icon_pixmap = self.sheet.copy(0, 0, self.frame_width, self.frame_height).scaled(
            32,
            32,
            self.QtCore.Qt.KeepAspectRatio,
            self.QtCore.Qt.SmoothTransformation,
        )
        return self.QtGui.QIcon(icon_pixmap)

    def _mark_activity(self) -> None:
        self.last_activity_at = time.monotonic()
        if self.state == SLEEP_STATE:
            self.set_state(REST_STATE)

    def _should_sleep(self) -> bool:
        if self.state == SLEEP_STATE:
            return False
        if self.state != REST_STATE:
            return False
        if time.monotonic() < self.state_hold_until:
            return False
        return (time.monotonic() - self.last_activity_at) >= self.sleep_after_seconds

def _one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def _sleep_after_seconds(config: dict) -> float:
    try:
        value = float(config.get("sleep_after_seconds", 120.0))
    except (TypeError, ValueError):
        value = 120.0
    return max(10.0, min(value, 3600.0))


def _event_detail(event: PetEvent) -> str:
    if event.type == "tool_started":
        tool = (event.payload or {}).get("tool") or event.text
        return f"工具：{_short_text(tool, 28)}" if tool else ""
    if event.type in {"turn_started", "background_process_completed", "background_process_updated"}:
        return _short_text(event.text, 34)
    if event.type in {"turn_failed", "delegation_task_failed"}:
        return _short_text(event.text, 34)
    return ""


def _short_text(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    return value[: max(0, limit - 1)] + "…" if len(value) > limit else value

def _load_manifest(asset_dir: Path) -> dict:
    import json

    path = asset_dir / "pet.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _state_path() -> Path:
    return get_mclaw_home() / "pet_state.json"


def _load_position() -> dict | None:
    path = _state_path()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        x = data.get("x")
        y = data.get("y")
        if isinstance(x, int) and isinstance(y, int):
            return {"x": x, "y": y}
    except Exception:
        return None
    return None


def _save_position(x: int, y: int) -> None:
    try:
        atomic_json_write(_state_path(), {"x": int(x), "y": int(y), "updated_at": time.time()})
    except Exception:
        pass












