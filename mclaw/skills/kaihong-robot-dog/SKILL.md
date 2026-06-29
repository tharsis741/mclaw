---
name: kaihong-robot-dog
description: Control Kaihong 'Yu' robot dog over a LAN HTTP server. Use this skill when the user asks M-Claw to make the robot dog stand, sit, lie down, run a named action, run a safe sequence of multiple actions, move forward or backward by distance, turn left or right by degrees, take a photo, return to home stance, check status, list actions, or stop.
---

# Kaihong Robot Dog

Use the bundled host client:

```powershell
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" <command>
```

## Connection

- Use secret scope `skill:kaihong-robot-dog`.
- Read `ROBOT_DOG_IP` from the environment.
- Control endpoint: `http://<ROBOT_DOG_IP>:8082`.
- If `ROBOT_DOG_IP` is missing or `health` fails, call `secret_request_many` before asking in normal chat:

```text
secret_request_many({
  "required_for": "skill:kaihong-robot-dog",
  "secrets": [{
    "env_var": "ROBOT_DOG_IP",
    "provider": "Kaihong robot dog",
    "purpose": "Robot LAN IP for HTTP control"
  }]
})
```

- Add `"force_refresh": true` only when an existing `ROBOT_DOG_IP` fails `health`.
- When running the host client through `terminal`, pass `required_for="skill:kaihong-robot-dog"` so the scoped `ROBOT_DOG_IP` is injected, then retry the command.

## Functions

| Function | User intent | Command | HTTP request |
|---|---|---|---|
| `robotdog_health()` | check robot status | `health` | `GET /health` |
| `robotdog_actions()` | list available actions | `actions` | `GET /actions` |
| `robotdog_home()` | return to normal home stance | `home` | `POST /go_home {}` |
| `robotdog_action(name)` | run a named action | `action <name>` | `POST /api/action {"name": name, "wait": true}` |
| `robotdog_sequence(names)` | run multiple named actions safely | `sequence <name> [name ...]` | client validates names, then runs `home -> action -> home` |
| `robotdog_forward(meters)` | move forward by distance | `forward --meters <m>` | `POST /move {"x": 10, "y": 0, "yaw_degrees": 0, "duration": meters*100/10*0.83}` |
| `robotdog_backward(meters)` | move backward by distance | `backward --meters <m>` | `POST /move {"x": -10, "y": 0, "yaw_degrees": 0, "duration": meters*100/10*0.83}` |
| `robotdog_turn_left(degrees)` | turn left by angle | `turn-left --degrees <deg>` | `POST /move {"x": 0, "y": 0, "yaw_degrees": 15, "duration": degrees/15*0.58}` |
| `robotdog_turn_right(degrees)` | turn right by angle | `turn-right --degrees <deg>` | `POST /move {"x": 0, "y": 0, "yaw_degrees": -15, "duration": degrees/15*0.58}` |
| `robotdog_photo(output=None)` | take a photo and save it locally | `photo [--output <path>]` | `GET /snapshot` |
| `robotdog_stop()` | stop movement/action | `stop` | `POST /stop {}` |

## Movement Constants

```text
BASE_LINEAR_SPEED_CM_S = 10
LINEAR_DURATION_SCALE = 0.83
BASE_TURN_DEG_S = 15
TURN_DURATION_SCALE = 0.58
HTTP_TIMEOUT_SECONDS = 30
SEQUENCE_SETTLE_SECONDS = 1.0
SEQUENCE_ACTION_SETTLE_SECONDS = 0.5
MAX_SEQUENCE_ACTIONS = 20
```

The host client also accepts `ROBOT_DOG_LINEAR_SPEED_CM_S`, `ROBOT_DOG_LINEAR_DURATION_SCALE`, `ROBOT_DOG_TURN_DEG_S`, `ROBOT_DOG_TURN_DURATION_SCALE`, `ROBOT_DOG_HTTP_TIMEOUT_SECONDS`, `ROBOT_DOG_SEQUENCE_SETTLE_SECONDS`, `ROBOT_DOG_SEQUENCE_ACTION_SETTLE_SECONDS`, and `ROBOT_DOG_MAX_SEQUENCE_ACTIONS` environment overrides for calibration.

The robot server schedules the stop for `/move` asynchronously. Do not split a single requested movement or turn only to avoid request timeout.

`photo` saves the returned image under `ROBOT_DOG_PHOTO_DIR` when set. Without that override, Windows saves to the user's Desktop `robotdog_photos` folder, Kaihong/OpenHarmony saves to `/data/acs/acs/file_sharing/photo`, and other environments save to `./robotdog_photos`. It prints JSON containing the saved image path.

## Command Mapping

Use `actions` to refresh the available action names when needed.

| User intent | Command |
|---|---|
| 往前走 / 前进 N 米 | `forward --meters N` |
| 往前走 / 前进 N 厘米 | `forward --meters N/100` |
| 后退 N 米 | `backward --meters N` |
| 后退 N 厘米 | `backward --meters N/100` |
| 左转 N 度 | `turn-left --degrees N` |
| 右转 N 度 | `turn-right --degrees N` |
| 拍照 / 拍一张照片 / 看一下前面 | `photo` |
| 编舞 / 连续做多个动作 / 表演 A B C | `sequence A B C` |
| 回正 / 归位 / 恢复正常站姿 | `home` |
| 停下 / 停止 | `stop` |
| 站立 / 站起来 | `action stand` |
| 坐下 | `action sit` |
| 趴下 | `action lie_down` |
| 摇摆 | `action wave` |
| 鞠躬 | `action bow` |
| 点头 | `action nod` |
| 摇头 | `action shake_head` |
| 握手 | `action shake_hands` |
| 撒尿 / 抬腿 | `action pee` |
| 低头 / 往下看 | `action look_down` |
| 伸展 / 伸懒腰 | `action stretch` |
| 打拳 | `action boxing` |
| 太空步 | `action spacewalk` |
| 月球漫步 / 后滑步 | `action moonwalk` |
| 俯卧撑 | `action push-up` |
| 俯卧撑变体 | `action push-up01` |
| 双腿站立 | `action 2_legs_stand` |

When a distance is not specified for a small forward/backward movement, use `0.2` meters.
When an angle is not specified for a left/right turn, use `30` degrees.

## Sequencing

For any routine, dance, performance, or request containing two or more named action groups, use `sequence` instead of separate `action` commands.

`sequence` validates all action names before moving, then runs `home`, waits briefly, runs the action, waits briefly again, and repeats for each action. It finishes with `home` by default. If any step fails, the client calls `stop` and exits with an error.

Use `action <name>` only for a single named action. Simple forward/backward/turn movement does not need a separate `home` first.

## Examples

```powershell
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" health
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" actions
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" home
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" action stand
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" sequence wave bow pee
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" forward --meters 1
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" backward --meters 0.5
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" turn-left --degrees 90
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" photo
python "{{SKILL_DIR}}/scripts/host/robotdog_client.py" stop
```
