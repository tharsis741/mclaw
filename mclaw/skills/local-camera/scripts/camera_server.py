#!/usr/bin/env python3
"""
Local Camera MCP Server
基于 OpenCV 的本地摄像头控制工具
"""

import cv2
import os
import base64
from mcp.server.fastmcp import FastMCP

# 初始化 MCP Server
mcp = FastMCP("local_camera", description="本地摄像头控制工具集")


@mcp.tool()
def check_camera_status(camera_index: int = 0) -> str:
    """检测指定摄像头是否可用

    Args:
        camera_index: 摄像头索引，默认为 0（系统默认摄像头）

    Returns:
        摄像头状态信息
    """
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        return "❌ 摄像头未打开或已被其他程序占用"
    ret, _ = cap.read()
    cap.release()
    return "✅ 摄像头状态正常" if ret else "⚠️ 摄像头已打开但无法读取画面"


@mcp.tool()
def capture_photo(save_path: str = "./captured_frame.jpg", camera_index: int = 0) -> str:
    """从指定摄像头捕获一帧图像并保存到本地

    Args:
        save_path: 图片保存路径，默认为 ./captured_frame.jpg
        camera_index: 摄像头索引，默认为 0

    Returns:
        保存成功的消息或错误信息
    """
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

    success = cv2.imwrite(save_path, frame)
    if not success:
        return f"错误：图片保存失败，请检查路径或磁盘空间: {abs_path}"

    return f"📸 拍照成功！图片已保存至: {abs_path}"


@mcp.tool()
def capture_to_base64(camera_index: int = 0) -> str:
    """捕获指定摄像头的当前画面并返回 Base64 编码（带 data URI 前缀）

    Args:
        camera_index: 摄像头索引，默认为 0

    Returns:
        data:image/jpeg;base64,xxx 格式字符串，可直接用于多模态模型
    """
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


@mcp.tool()
def list_cameras() -> str:
    """列出系统可用的摄像头设备

    Returns:
        可用摄像头列表
    """
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
