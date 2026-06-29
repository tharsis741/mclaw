#!/usr/bin/env python3
"""HTTP client for the PuppyPi robot dog control server."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_PORT = 8082
KAIHONG_PHOTO_DIR = Path("/data/acs/acs/file_sharing/photo")


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


BASE_LINEAR_SPEED_CM_S = env_float("ROBOT_DOG_LINEAR_SPEED_CM_S", 10.0)
BASE_TURN_DEG_S = env_float("ROBOT_DOG_TURN_DEG_S", 15.0)
LINEAR_DURATION_SCALE = env_float("ROBOT_DOG_LINEAR_DURATION_SCALE", 0.83)
TURN_DURATION_SCALE = env_float("ROBOT_DOG_TURN_DURATION_SCALE", 0.58)
HTTP_TIMEOUT_SECONDS = env_float("ROBOT_DOG_HTTP_TIMEOUT_SECONDS", 30.0)
SEQUENCE_SETTLE_SECONDS = env_float("ROBOT_DOG_SEQUENCE_SETTLE_SECONDS", 1.0)
SEQUENCE_ACTION_SETTLE_SECONDS = env_float("ROBOT_DOG_SEQUENCE_ACTION_SETTLE_SECONDS", 0.5)
MAX_SEQUENCE_ACTIONS = env_int("ROBOT_DOG_MAX_SEQUENCE_ACTIONS", 20)


def normalize_base_url(host: str | None) -> str:
    value = (host or os.environ.get("ROBOT_DOG_IP") or os.environ.get("ROBOTDOG_HOST") or "").strip()
    if not value:
        raise SystemExit("Missing --host or ROBOT_DOG_IP")
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlparse(value)
    netloc = parsed.netloc
    if ":" not in netloc:
        netloc = f"{netloc}:{DEFAULT_PORT}"
    return urllib.parse.urlunparse((parsed.scheme, netloc, "", "", "", "")).rstrip("/")


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def request_timeout(body: dict[str, Any] | None) -> float:
    timeout = HTTP_TIMEOUT_SECONDS
    if body is not None:
        try:
            duration = float(body.get("duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if math.isfinite(duration) and duration > 0:
            timeout = max(timeout, duration + 10.0)
    return timeout


def request_json(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=request_timeout(body)) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body_obj: Any = json.loads(raw)
        except json.JSONDecodeError:
            body_obj = raw
        raise SystemExit(json.dumps({"ok": False, "status": exc.code, "error": body_obj}, ensure_ascii=False, indent=2))
    except urllib.error.URLError as exc:
        raise SystemExit(f"HTTP request failed: {exc}")


def request_bytes(
    base_url: str,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[bytes, str, dict[str, str]]:
    if query:
        clean_query = {key: value for key, value in query.items() if value is not None}
        if clean_query:
            path = path + "?" + urllib.parse.urlencode(clean_query)
    req = urllib.request.Request(base_url + path, headers={"Accept": "image/jpeg,image/png,*/*"}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout or HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read()
            content_type = str(resp.headers.get("Content-Type") or "application/octet-stream")
            headers = {str(key): str(value) for key, value in resp.headers.items()}
            return body, content_type, headers
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body_obj: Any = json.loads(raw)
        except json.JSONDecodeError:
            body_obj = raw
        raise SystemExit(json.dumps({"ok": False, "status": exc.code, "error": body_obj}, ensure_ascii=False, indent=2))
    except urllib.error.URLError as exc:
        raise SystemExit(f"HTTP request failed: {exc}")


def default_photo_output(output: str | None, content_type: str) -> Path:
    suffix = ".png" if "png" in content_type.lower() else ".jpg"
    filename = f"robotdog_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"
    if output:
        target = Path(output).expanduser()
        if target.exists() and target.is_dir():
            return target / filename
        if str(output).endswith(("/", "\\")) or not target.suffix:
            return target / filename
        return target

    env_dir = os.environ.get("ROBOT_DOG_PHOTO_DIR")
    if env_dir:
        root = Path(env_dir).expanduser()
    elif os.environ.get("USERPROFILE"):
        desktop = Path(os.environ["USERPROFILE"]).expanduser() / "Desktop"
        root = (desktop if desktop.exists() else Path.cwd()) / "robotdog_photos"
    elif KAIHONG_PHOTO_DIR.exists():
        root = KAIHONG_PHOTO_DIR
    else:
        desktop = Path.home() / "Desktop"
        root = (desktop if desktop.exists() else Path.cwd()) / "robotdog_photos"
    return root / filename


def health(base_url: str) -> dict[str, Any]:
    return request_json(base_url, "GET", "/health")


def actions(base_url: str) -> dict[str, Any]:
    return request_json(base_url, "GET", "/actions")


def available_action_names(base_url: str) -> set[str]:
    payload = actions(base_url)
    values = payload.get("actions", [])
    if not isinstance(values, list):
        raise SystemExit("Actions response is not a list")
    names: set[str] = set()
    for item in values:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


def run_action(base_url: str, name: str, wait: bool = True) -> dict[str, Any]:
    return request_json(base_url, "POST", "/api/action", {"name": name, "wait": wait})


def go_home(base_url: str) -> dict[str, Any]:
    return request_json(base_url, "POST", "/go_home", {})


def move_raw(base_url: str, *, x: float = 0.0, y: float = 0.0, yaw_degrees: float = 0.0, duration: float = 0.0) -> dict[str, Any]:
    return request_json(
        base_url,
        "POST",
        "/move",
        {"x": x, "y": y, "yaw_degrees": yaw_degrees, "duration": duration},
    )


def forward(base_url: str, meters: float) -> dict[str, Any]:
    duration = abs(float(meters)) * 100.0 / BASE_LINEAR_SPEED_CM_S * LINEAR_DURATION_SCALE
    return move_raw(base_url, x=BASE_LINEAR_SPEED_CM_S, duration=duration)


def backward(base_url: str, meters: float) -> dict[str, Any]:
    duration = abs(float(meters)) * 100.0 / BASE_LINEAR_SPEED_CM_S * LINEAR_DURATION_SCALE
    return move_raw(base_url, x=-BASE_LINEAR_SPEED_CM_S, duration=duration)


def turn_left(base_url: str, degrees: float) -> dict[str, Any]:
    duration = abs(float(degrees)) / BASE_TURN_DEG_S * TURN_DURATION_SCALE
    return move_raw(base_url, yaw_degrees=BASE_TURN_DEG_S, duration=duration)


def turn_right(base_url: str, degrees: float) -> dict[str, Any]:
    duration = abs(float(degrees)) / BASE_TURN_DEG_S * TURN_DURATION_SCALE
    return move_raw(base_url, yaw_degrees=-BASE_TURN_DEG_S, duration=duration)


def stop(base_url: str) -> dict[str, Any]:
    return request_json(base_url, "POST", "/stop", {})


def photo(base_url: str, *, output: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    timeout = max(0.1, float(timeout))
    body, content_type, headers = request_bytes(
        base_url,
        "GET",
        "/snapshot",
        query={"timeout": timeout},
        timeout=max(HTTP_TIMEOUT_SECONDS, timeout + 5.0),
    )
    if not body:
        raise SystemExit("Snapshot returned an empty image")
    target = default_photo_output(output, content_type)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return {
        "ok": True,
        "path": str(target.resolve()),
        "bytes": len(body),
        "content_type": content_type,
        "source": headers.get("X-MClaw-Snapshot-Source"),
        "topic": headers.get("X-MClaw-Snapshot-Topic"),
        "format": headers.get("X-MClaw-Snapshot-Format"),
    }


def require_ok(result: dict[str, Any], context: str) -> dict[str, Any]:
    if result.get("ok") is False:
        raise SystemExit(json.dumps({"ok": False, "step": context, "result": result}, ensure_ascii=False, indent=2))
    return result


def stop_quietly(base_url: str) -> dict[str, Any]:
    try:
        return stop(base_url)
    except SystemExit as exc:
        return {"ok": False, "error": str(getattr(exc, "code", exc))}


def run_sequence(
    base_url: str,
    names: list[str],
    *,
    settle_seconds: float = SEQUENCE_SETTLE_SECONDS,
    action_settle_seconds: float = SEQUENCE_ACTION_SETTLE_SECONDS,
    final_home: bool = True,
) -> dict[str, Any]:
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    if not cleaned:
        raise SystemExit("sequence requires at least one action name")
    if len(cleaned) > MAX_SEQUENCE_ACTIONS:
        raise SystemExit(f"sequence exceeds {MAX_SEQUENCE_ACTIONS} actions")
    if not math.isfinite(settle_seconds) or settle_seconds < 0:
        raise SystemExit("settle seconds must be a non-negative number")
    if settle_seconds > 5:
        raise SystemExit("settle seconds must not exceed 5")
    if not math.isfinite(action_settle_seconds) or action_settle_seconds < 0:
        raise SystemExit("action settle seconds must be a non-negative number")
    if action_settle_seconds > 5:
        raise SystemExit("action settle seconds must not exceed 5")

    available = available_action_names(base_url)
    missing = [name for name in cleaned if name not in available]
    if missing:
        raise SystemExit(
            json.dumps(
                {"ok": False, "error": "unknown action names", "missing": missing, "available": sorted(available)},
                ensure_ascii=False,
                indent=2,
            )
        )

    steps: list[dict[str, Any]] = []

    def settle() -> None:
        if settle_seconds > 0:
            time.sleep(settle_seconds)

    def action_settle() -> None:
        if action_settle_seconds > 0:
            time.sleep(action_settle_seconds)

    try:
        for index, name in enumerate(cleaned, start=1):
            home_result = require_ok(go_home(base_url), f"home before {name}")
            steps.append({"index": index, "type": "home", "before": name, "result": home_result})
            settle()
            action_result = require_ok(run_action(base_url, name, wait=True), f"action {name}")
            steps.append({"index": index, "type": "action", "name": name, "result": action_result})
            action_settle()
        if final_home:
            final_home_result = require_ok(go_home(base_url), "final home")
            steps.append({"type": "home", "final": True, "result": final_home_result})
        return {
            "ok": True,
            "mode": "safe_sequence",
            "actions": cleaned,
            "count": len(cleaned),
            "settle_seconds": settle_seconds,
            "action_settle_seconds": action_settle_seconds,
            "final_home": final_home,
            "steps": steps,
        }
    except KeyboardInterrupt:
        stop_quietly(base_url)
        raise
    except SystemExit as exc:
        stop_result = stop_quietly(base_url)
        raise SystemExit(
            json.dumps(
                {
                    "ok": False,
                    "mode": "safe_sequence",
                    "actions": cleaned,
                    "error": str(getattr(exc, "code", exc)),
                    "steps": steps,
                    "stop": stop_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control a PuppyPi robot dog over HTTP.")
    parser.add_argument("--host", help="Robot host or URL, for example 192.168.1.138 or 192.168.1.138:8082")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    sub.add_parser("actions")
    sub.add_parser("home")
    sub.add_parser("stop")

    photo_cmd = sub.add_parser("photo", aliases=["snapshot"])
    photo_cmd.add_argument("--output", "-o", help="Output image file or directory")
    photo_cmd.add_argument("--timeout", type=float, default=5.0, help="Seconds to wait for a camera frame")

    action = sub.add_parser("action")
    action.add_argument("name")
    action.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)

    sequence = sub.add_parser("sequence", aliases=["routine"])
    sequence.add_argument("names", nargs="+")
    sequence.add_argument("--settle", type=float, default=SEQUENCE_SETTLE_SECONDS)
    sequence.add_argument("--action-settle", type=float, default=SEQUENCE_ACTION_SETTLE_SECONDS)
    sequence.add_argument("--no-final-home", action="store_true")

    forward_cmd = sub.add_parser("forward")
    forward_cmd.add_argument("--meters", type=float, required=True)

    backward_cmd = sub.add_parser("backward")
    backward_cmd.add_argument("--meters", type=float, required=True)

    left_cmd = sub.add_parser("turn-left")
    left_cmd.add_argument("--degrees", type=float, required=True)

    right_cmd = sub.add_parser("turn-right")
    right_cmd.add_argument("--degrees", type=float, required=True)

    move = sub.add_parser("move")
    move.add_argument("--x", type=float, default=0.0)
    move.add_argument("--y", type=float, default=0.0)
    move.add_argument("--yaw-deg", type=float, default=0.0)
    move.add_argument("--duration", type=float, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = normalize_base_url(args.host)

    if args.command == "health":
        print_json(health(base_url))
    elif args.command == "actions":
        print_json(actions(base_url))
    elif args.command == "home":
        print_json(go_home(base_url))
    elif args.command == "action":
        print_json(run_action(base_url, args.name, wait=args.wait))
    elif args.command in ("sequence", "routine"):
        print_json(
            run_sequence(
                base_url,
                args.names,
                settle_seconds=args.settle,
                action_settle_seconds=args.action_settle,
                final_home=not args.no_final_home,
            )
        )
    elif args.command == "forward":
        print_json(forward(base_url, args.meters))
    elif args.command == "backward":
        print_json(backward(base_url, args.meters))
    elif args.command == "turn-left":
        print_json(turn_left(base_url, args.degrees))
    elif args.command == "turn-right":
        print_json(turn_right(base_url, args.degrees))
    elif args.command == "move":
        print_json(move_raw(base_url, x=args.x, y=args.y, yaw_degrees=args.yaw_deg, duration=args.duration))
    elif args.command == "stop":
        print_json(stop(base_url))
    elif args.command in ("photo", "snapshot"):
        print_json(photo(base_url, output=args.output, timeout=args.timeout))
    else:
        raise SystemExit(f"Unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
