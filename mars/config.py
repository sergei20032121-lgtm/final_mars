"""Конфигурация камеры и робота."""
from dataclasses import dataclass


@dataclass
class CameraConfig:
    device: str = "/dev/video0"
    width: int = 320
    height: int = 240
    fps: int = 30          # запрашиваем 30 у камеры
    jpeg_quality: int = 65 # чуть ниже качество = выше FPS
    detect_interval: float = 1.0  # детекция лиц реже = меньше нагрузка
    backend: str = "auto"
    pigo_min_size: int = 40
    pigo_max_size: int = 320
    pigo_iou: float = 0.15


@dataclass
class RobotConfig:
    """Конфигурация робота"""
    map_width: int = 800
    map_height: int = 480
    map_scale: float = 10.0
    robot_size: int = 15
    battery_capacity: int = 5000


robot_config = RobotConfig()
camera_config = CameraConfig()
