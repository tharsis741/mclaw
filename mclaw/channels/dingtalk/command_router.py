"""Small command router for DingTalk chats."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommandResult:
    handled: bool = False
    text: str = ""
    action: str = ""


class DingTalkCommandRouter:
    def parse(self, text: str) -> tuple[str, str]:
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return "", stripped
        head, _, tail = stripped[1:].partition(" ")
        return head.strip().lower(), tail.strip()

    def handle(self, text: str, *, status: str = "idle", status_details: str = "") -> CommandResult:
        command, _args = self.parse(text)
        if not command:
            return CommandResult()
        if command in {"help", "mclaw"}:
            return CommandResult(
                handled=True,
                action="help",
                text=(
                    "M-Claw DingTalk commands:\n"
                    "/help - show this help\n"
                    "/new - start a fresh session\n"
                    "/stop - interrupt the current turn\n"
                    "/status - show current session status"
                ),
            )
        if command == "status":
            body = f"Session status: {status}"
            if status_details.strip():
                body = f"{body}\n\n{status_details.strip()}"
            return CommandResult(handled=True, action="status", text=body)
        if command == "new":
            return CommandResult(handled=True, action="new", text="Started a fresh DingTalk session.")
        if command == "stop":
            return CommandResult(handled=True, action="stop", text="")
        return CommandResult(handled=False)
