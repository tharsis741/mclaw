#!/usr/bin/env python3
"""
Local Camera 摄像头控制工具
直接使用 OpenCV，无需 MCP
"""

import cv2
import os
import sys
import base64
from datetime import datetime


def check_camera_status(camera_index: int = 0) -> str:
    """检测指定摄像头是否可用"""
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        return "[FAIL] 摄像头未打开或已被其他程序占用"
    ret, _ = cap.read()
    cap.release()
    return "[OK] 摄像头状态正常" if ret else "[WARN] 摄像头已打开但无法读取画面"


def capture_photo(save_path: str = None, camera_index: int = 0) -> str:
    """拍照并保存到本地"""
    if save_path is None:
        save_path = os.path.join(os.path.expanduser("~"), f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        return "错误：无法打开摄像头"

    # 等待摄像头稳定
    for _ in range(5):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret:
        return "错误：抓取视频帧失败"

    # 确保目录存在
    abs_path = os.path.abspath(save_path)
    dir_path = os.path.dirname(abs_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    success = cv2.imwrite(abs_path, frame)
    if not success:
        return f"错误：图片保存失败: {abs_path}"

    return f"[OK] 拍照成功！图片已保存至: {abs_path}"


def capture_to_base64(camera_index: int = 0) -> str:
    """拍照并返回 Base64 编码（带 data URI 前缀）"""
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        return "错误：无法打开摄像头"

    # 等待摄像头稳定
    for _ in range(5):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret:
        return "错误：无法获取画面"

    _, buffer = cv2.imencode('.jpg', frame)
    b64_img = base64.b64encode(buffer).decode('utf-8')
    return f"data:image/jpeg;base64,{b64_img}"


def list_cameras() -> str:
    """列出系统可用摄像头"""
    available_cameras = []
    for i in range(10):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                available_cameras.append(f"摄像头 {i}: 可用")
            cap.release()

    if available_cameras:
        return "\n".join(available_cameras)
    return "未检测到可用摄像头"


# CLI 入口
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python capture.py <command> [args]")
        print("命令:")
        print("  status [camera_index]  - 检查摄像头状态")
        print("  capture [path] [index] - 拍照保存")
        print("  base64 [camera_index]  - 返回 Base64")
        print("  list                   - 列出可用摄像头")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        print(check_camera_status(idx))
    elif cmd == "capture":
        path = sys.argv[2] if len(sys.argv) > 2 else None
        idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        print(capture_photo(path, idx))
    elif cmd == "base64":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        print(capture_to_base64(idx))
    elif cmd == "list":
        print(list_cameras())
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
