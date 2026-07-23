---
name: local-camera
description: 控制本地摄像头拍照并输出图片数据。
tags: [camera, webcam, opencv, hardware]
---

# Local Camera Skill

本地摄像头控制，基于 OpenCV 实现。

## 功能

- `check_camera_status` - 检测摄像头是否可用
- `capture_photo` - 拍照并保存到本地
- `capture_to_base64` - 拍照并返回 Base64 编码
- `list_cameras` - 列出系统可用摄像头

## 使用方法

### 安装依赖

```bash
pip install opencv-python numpy
```

### 直接调用

```bash
# 检查摄像头状态
python scripts/capture.py status

# 拍照保存
python scripts/capture.py capture [save_path] [camera_index]

# 返回 Base64
python scripts/capture.py base64 [camera_index]

# 列出可用摄像头
python scripts/capture.py list
```

### 在 Python 中调用

```python
from scripts.capture import check_camera_status, capture_photo, capture_to_base64, list_cameras

print(check_camera_status())
print(capture_photo("photo.jpg"))
print(capture_to_base64())
print(list_cameras())
```

## 注意事项

1. 确保系统已授权摄像头权限
2. 确保没有其他程序（如 Zoom、系统相机）占用摄像头
3. Windows 上可能需要允许 Python 访问摄像头

## 文件结构

```
local-camera/
├── SKILL.md
└── scripts/
    └── capture.py
```
