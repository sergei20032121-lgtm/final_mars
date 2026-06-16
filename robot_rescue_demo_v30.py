#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M.A.P.C. - DEMO VERSION (Optimized)
- Улучшенный FPS
- Красивый интерфейс
- Параллельная обработка видео
"""

import os
import sys
import json
import time
import math
import threading
import glob
import select
import subprocess
import tempfile
import io
from dataclasses import dataclass
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw

try:
    from flask import Flask, render_template_string, jsonify, request, Response
except ImportError:
    print("❌ Flask не установлен. Установи: pip3 install flask")
    sys.exit(1)

# GPIO — подключаем если доступно
try:
    import OPi.GPIO as GPIO
    GPIO_AVAILABLE = True
    print("[✓] OPi.GPIO доступен")
except ImportError:
    GPIO_AVAILABLE = False
    print("[⚠️] OPi.GPIO не найден — моторы в режиме симуляции")

# JPEG markers
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"

# ============================================================================
# ФУНКЦИИ УТИЛИТЫ
# ============================================================================

def discover_video_devices():
    """Обнаружить доступные видео-устройства"""
    devices = []
    for device in sorted(glob.glob("/dev/video*")):
        name = "USB camera"
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            for line in result.stdout.splitlines():
                if "Card type" in line:
                    name = line.split(":", 1)[1].strip() or name
                    break
        except Exception:
            pass
        devices.append({"device": device, "name": name})
    return devices

# ============================================================================
# КОНФИГУРАЦИЯ КАМЕРЫ
# ============================================================================

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

# ============================================================================
# КЛАССЫ КАМЕР (Оптимизированные)
# ============================================================================

class FsWebcamCamera:
    """Захват видео через fswebcam"""
    def __init__(self, config: CameraConfig):
        self.config = config
        self.tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
        self.last_frame = None

    def capture_jpeg(self) -> bytes:
        fd, path = tempfile.mkstemp(prefix="face_frame_", suffix=".jpg", dir=self.tmp_dir)
        os.close(fd)

        cmd = [
            "fswebcam", "--quiet", "--no-banner",
            "-d", self.config.device,
            "-r", f"{self.config.width}x{self.config.height}",
            "--jpeg", str(self.config.jpeg_quality),
            path,
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, 
                         check=True, timeout=3)
            with open(path, "rb") as file:
                self.last_frame = file.read()
                return self.last_frame
        except:
            return self.last_frame or b''
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def close(self):
        pass

    @property
    def name(self):
        return "fswebcam"


class FfmpegCamera:
    """Захват видео через ffmpeg (оптимизированный)"""
    def __init__(self, config: CameraConfig):
        self.config = config
        self.proc = None
        self.buffer = bytearray()
        self.mode_index = 0
        self.last_frame = None

    @property
    def name(self):
        mode = "mjpeg" if self.mode_index == 0 else "auto"
        return f"ffmpeg/{mode}"

    def _build_cmd(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2",
        ]
        if self.mode_index == 0:
            cmd += ["-input_format", "mjpeg"]
        
        cmd += [
            "-framerate", str(self.config.fps),
            "-video_size", f"{self.config.width}x{self.config.height}",
            "-i", self.config.device,
            "-an", "-q:v", "3",  # Выше качество для лучшей обработки
            "-f", "mjpeg", "pipe:1",
        ]
        return cmd

    def _start(self):
        self.close()
        self.buffer.clear()
        cmd = self._build_cmd()
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=1024*100
        )

    def _extract_frame(self):
        start = self.buffer.find(JPEG_START)
        if start < 0:
            if len(self.buffer) > 65536:
                self.buffer.clear()
            return None

        if start > 0:
            del self.buffer[:start]

        end = self.buffer.find(JPEG_END, 2)
        if end < 0:
            return None

        frame = bytes(self.buffer[:end + 2])
        del self.buffer[:end + 2]
        return frame

    def _read_frame_once(self, timeout=2.0):  # Уменьшен таймаут
        if self.proc is None or self.proc.poll() is not None:
            self._start()

        deadline = time.monotonic() + timeout
        fd = self.proc.stdout.fileno()

        while time.monotonic() < deadline:
            frame = self._extract_frame()
            if frame:
                self.last_frame = frame
                return frame

            if self.proc.poll() is not None:
                raise RuntimeError("ffmpeg stopped")

            wait_time = max(0.01, min(0.2, deadline - time.monotonic()))  # Меньше ждем
            ready, _, _ = select.select([fd], [], [], wait_time)
            if not ready:
                continue

            chunk = os.read(fd, 16384)  # Больше читаем за раз
            if not chunk:
                raise RuntimeError("ffmpeg returned empty data")

            self.buffer.extend(chunk)

        raise TimeoutError("ffmpeg frame timeout")

    def capture_jpeg(self) -> bytes:
        try:
            return self._read_frame_once(timeout=2.0)
        except Exception:
            if self.mode_index == 0:
                self.mode_index = 1
                self.close()
                try:
                    return self._read_frame_once(timeout=2.0)
                except:
                    return self.last_frame or b''
            return self.last_frame or b''

    def close(self):
        proc = self.proc
        self.proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class CameraManager:
    """Управление камерой с кэшированием"""
    def __init__(self, config: CameraConfig):
        self.config = config
        self.camera = None
        self.backend_name = "none"
        self.last_error = ""
        self.cached_frame = None
        self.cache_time = 0
        self.frame_rate_actual = 0
        self.frame_count = 0
        self.init_camera()

    def init_camera(self):
        """Инициализировать камеру"""
        backends = []
        
        if self.config.backend in ["auto", "ffmpeg"]:
            backends.append(("ffmpeg", FfmpegCamera))
        
        if self.config.backend in ["auto", "fswebcam"]:
            backends.append(("fswebcam", FsWebcamCamera))

        for backend_name, backend_class in backends:
            try:
                camera = backend_class(self.config)
                test_frame = camera.capture_jpeg()
                if test_frame:
                    self.camera = camera
                    self.backend_name = backend_name
                    print(f"[✓] Камера ({backend_name}): {self.config.width}x{self.config.height} @ {self.config.fps}fps")
                    return
            except Exception as e:
                self.last_error = str(e)
                continue

        print(f"[✗] Камера не инициализирована: {self.last_error}")

    def get_frame(self):
        """Получить JPEG кадр (с кэшированием)"""
        if self.camera:
            try:
                frame = self.camera.capture_jpeg()
                if frame:
                    self.cached_frame = frame
                    self.frame_count += 1
                    now = time.time()
                    if self.cache_time > 0:
                        dt = now - self.cache_time
                        if dt > 0:
                            self.frame_rate_actual = 1.0 / dt
                    self.cache_time = now
                    return frame
            except Exception as e:
                self.last_error = str(e)
                
        return self.cached_frame or None

    def close(self):
        if self.camera:
            self.camera.close()
            self.camera = None

    def change_device(self, device):
        self.close()
        self.config.device = device
        self.init_camera()

# ============================================================================
# СИМУЛЯТОР РОБОТА (Оптимизированный)
# ============================================================================



# ============================================================================
# ОБНАРУЖЕНИЕ ЛИЦ С PIGO (Асинхронно - не блокирует UI)
# ============================================================================


# ============================================================================
# ОБНАРУЖЕНИЕ ЛИЦ - MOCK (работает без Pigo, готово для реальной Pigo)
# ============================================================================

class FaceDetectorPigo:
    """Обнаружение лиц (mock mode - работает без Pigo)"""
    def __init__(self):
        self.cascade_path = "/usr/local/share/pigo/cascade/facefinder"
        self.initialized = False  # Всегда False для mock
        self.last_detections = []
        self.detect_thread = None
        self.last_detect_time = 0
        self.detect_interval = 1.0
        self.current_image_path = None
        print("[i] Face detection: Mock mode (Pigo not installed)")

    def detect_async(self, image_path):
        """Асинхронное обнаружение"""
        return self.last_detections


# ============================================================================
# УПРАВЛЕНИЕ МОТОРАМИ ЧЕРЕЗ GPIO (Orange Pi → плата машинки)
# ============================================================================
#
# Схема подключения:
#
#   Плата машинки YSM-6042R-C       Orange Pi (40-pin header)
#   ─────────────────────────        ─────────────────────────
#   GND  ──────────────────────────  Pin 6  (GND)
#   A    ──────────────────────────  Pin 11 (PA0  / GPIO 0)  ← вперёд
#   B    ──────────────────────────  Pin 13 (PA1  / GPIO 1)  ← назад
#   C    ──────────────────────────  Pin 15 (PA2  / GPIO 2)  ← влево
#   D    ──────────────────────────  Pin 16 (PA3  / GPIO 3)  ← вправо
#
#   VCC платы машинки — НЕ подключать! Питание от батареек машинки.
#   GND должен быть общим — обязательно!
#
# ============================================================================

# ============================================================================
# УПРАВЛЕНИЕ МОТОРАМИ — L9110S + ТАНК (гусеницы)
# ============================================================================
#
#   L9110S       Orange Pi (BOARD)    Танк
#   ──────       ─────────────────    ──────────────────
#   GND    ─────► Pin 6  (GND)
#   VCC    ─────► Pin 1  (3.3V)
#   VM     ─────► АКБ 4.8V [+]
#   A-IA   ─────► Pin 11             Левая гусеница
#   A-IB   ─────► Pin 13             Левая гусеница
#   B-IA   ─────► Pin 15             Правая гусеница
#   B-IB   ─────► Pin 19             Правая гусеница
#   OA1/OA2─────────────────────────► M1+/M1-
#   OB1/OB2─────────────────────────► M2+/M2-
#
#   Таблица правды:
#   Вперёд: L_FWD=1 L_BWD=0  R_FWD=1 R_BWD=0
#   Назад:  L_FWD=0 L_BWD=1  R_FWD=0 R_BWD=1
#   Влево:  L_FWD=0 L_BWD=1  R_FWD=1 R_BWD=0  (разворот на месте!)
#   Вправо: L_FWD=1 L_BWD=0  R_FWD=0 R_BWD=1  (разворот на месте!)
#   Стоп:   все 0
#
# ============================================================================

class MotorController:
    """
    Управление гусеницами через L9110S H-Bridge.
    sim_mode=True  — движение только на карте, GPIO молчит
    sim_mode=False — реальные команды на GPIO
    """

    PIN_L_FWD = 11    # A-IA — левая гусеница вперёд
    PIN_L_BWD = 13    # A-IB — левая гусеница назад
    PIN_R_FWD = 15    # B-IA — правая гусеница вперёд
    PIN_R_BWD = 3     # B-IB — правая гусеница назад

    # Алиасы для совместимости со старым кодом
    PIN_FORWARD  = 11
    PIN_BACKWARD = 13
    PIN_LEFT     = 15
    PIN_RIGHT    = 3

    def __init__(self):
        self.enabled  = False
        self.sim_mode = True
        self.current_cmd = 'STOP'

        if not GPIO_AVAILABLE:
            print("[⚠️] Моторы: OPi.GPIO не установлен — симуляция")
            return

        try:
            GPIO.setmode(GPIO.BOARD)
            GPIO.setwarnings(False)

            # Инвертированная логика: HIGH=стоп, LOW=движение
            for pin in [self.PIN_L_FWD, self.PIN_L_BWD,
                        self.PIN_R_FWD, self.PIN_R_BWD]:
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
            time.sleep(0.1)

            self.enabled = True
            self.sim_mode = False
            print("[✓] Моторы: L9110S инициализирован (РЕАЛЬНЫЙ режим)")
            print("[✓] Логика инвертирована: LOW=движение HIGH=стоп")
            print(f"    L: FWD=Pin{self.PIN_L_FWD} BWD=Pin{self.PIN_L_BWD}")
            print(f"    R: FWD=Pin{self.PIN_R_FWD} BWD=Pin{self.PIN_R_BWD}")
        except Exception as e:
            print(f"[✗] Моторы: ошибка GPIO — {e}")

    def _set(self, lf, lb, rf, rb):
        """Установить состояние 4 пинов L9110S (инвертированная логика: LOW=ON)"""
        if not self.enabled or self.sim_mode:
            return
        # Инвертируем: 1=движение → LOW, 0=стоп → HIGH
        GPIO.output(self.PIN_L_FWD, GPIO.LOW if lf else GPIO.HIGH)
        GPIO.output(self.PIN_L_BWD, GPIO.LOW if lb else GPIO.HIGH)
        GPIO.output(self.PIN_R_FWD, GPIO.LOW if rf else GPIO.HIGH)
        GPIO.output(self.PIN_R_BWD, GPIO.LOW if rb else GPIO.HIGH)

    def forward(self):
        self.current_cmd = 'FORWARD'
        self._set(1, 0, 1, 0)

    def backward(self):
        self.current_cmd = 'BACKWARD'
        self._set(0, 1, 0, 1)

    def left(self):
        self.current_cmd = 'LEFT'
        self._set(0, 1, 1, 0)  # левая назад, правая вперёд

    def right(self):
        self.current_cmd = 'RIGHT'
        self._set(1, 0, 0, 1)  # левая вперёд, правая назад

    def stop(self):
        self.current_cmd = 'STOP'
        self._set(0, 0, 0, 0)

    def set_sim_mode(self, enabled: bool):
        self.sim_mode = enabled
        if enabled and self.enabled:
            # Стопим реальные моторы при включении симуляции (HIGH=стоп)
            for pin in [self.PIN_L_FWD, self.PIN_L_BWD,
                        self.PIN_R_FWD, self.PIN_R_BWD]:
                GPIO.output(pin, GPIO.HIGH)
        mode = "СИМ" if enabled else "GPIO"
        print(f"[✓] Моторы: режим {mode}")

    def cleanup(self):
        if self.enabled:
            # HIGH = стоп перед очисткой
            for pin in [self.PIN_L_FWD, self.PIN_L_BWD,
                        self.PIN_R_FWD, self.PIN_R_BWD]:
                GPIO.output(pin, GPIO.HIGH)
            GPIO.cleanup()
            print("[✓] GPIO очищен")

    @property
    def status(self):
        mode = "СИМ" if self.sim_mode else "GPIO"
        return f"{mode} ({self.current_cmd})"

class RobotSimulator:
    """Симуляция + реальное управление через GPIO"""
    def __init__(self):
        self.x = robot_config.map_width / 2
        self.y = robot_config.map_height / 2
        self.angle = 0

        # Базовая точка (старт)
        self.base_x = self.x
        self.base_y = self.y

        self.battery_percent = 100
        self.battery_voltage = 5.0
        self.current_command = 'STOP'
        self.current_speed = 150

        self.found_humans = []
        self.path_history = deque(maxlen=200)
        self.path_history.append((self.x, self.y))

        self.start_time = time.time()
        self.move_thread = None
        self.should_move = False
        self.detected_faces = []

        # Детектор движения (предыдущий кадр)
        self.prev_frame = None
        self.motion_level = 0.0

        # Моторы через GPIO
        self.motors = MotorController()

    def move_forward(self):
        self.current_command = 'FORWARD'
        self.should_move = True
        self.motors.forward()
        self.start_smooth_movement()

    def move_backward(self):
        self.current_command = 'BACKWARD'
        self.should_move = True
        self.motors.backward()
        self.start_smooth_movement()

    def turn_left(self):
        self.current_command = 'LEFT'
        self.should_move = True
        self.motors.left()
        self.angle = (self.angle - 5) % 360
        self.path_history.append((self.x, self.y))

    def turn_right(self):
        self.current_command = 'RIGHT'
        self.should_move = True
        self.motors.right()
        self.angle = (self.angle + 5) % 360
        self.path_history.append((self.x, self.y))

    def stop(self):
        self.current_command = 'STOP'
        self.should_move = False
        self.motors.stop()

    def set_speed(self, speed):
        self.current_speed = max(0, min(255, int(speed)))

    def start_smooth_movement(self):
        if self.move_thread and self.move_thread.is_alive():
            return
        self.move_thread = threading.Thread(target=self._smooth_move_loop, daemon=True)
        self.move_thread.start()

    def _smooth_move_loop(self):
        while self.should_move:
            if self.current_command == 'FORWARD':
                angle_rad = math.radians(self.angle)
                dx = math.sin(angle_rad) * (self.current_speed / 255.0) * 2
                dy = -math.cos(angle_rad) * (self.current_speed / 255.0) * 2
                self.x += dx
                self.y += dy
                self.path_history.append((self.x, self.y))

            elif self.current_command == 'BACKWARD':
                angle_rad = math.radians(self.angle)
                dx = -math.sin(angle_rad) * (self.current_speed / 255.0) * 2
                dy = math.cos(angle_rad) * (self.current_speed / 255.0) * 2
                self.x += dx
                self.y += dy
                self.path_history.append((self.x, self.y))

            self.battery_percent = max(0, self.battery_percent - 0.01)
            self.battery_voltage = 3.0 + (self.battery_percent / 100.0) * 2.0

            time.sleep(0.05)

    def add_human_detection(self, x, y, image=None):
        """Добавить найденного человека — только если его нет рядом"""
        for h in self.found_humans:
            dist = ((h['x'] - x)**2 + (h['y'] - y)**2)**0.5
            if dist < 30:  # Уже есть рядом
                return

        # Сохраняем фото
        photo_b64 = None
        if image:
            try:
                buf = io.BytesIO()
                thumb = image.copy()
                thumb.thumbnail((120, 90))
                thumb.save(buf, 'JPEG', quality=70)
                import base64
                photo_b64 = base64.b64encode(buf.getvalue()).decode()
            except:
                pass

        self.found_humans.append({
            'id': len(self.found_humans),
            'x': x,
            'y': y,
            'timestamp': time.time(),
            'photo': photo_b64,
        })

    def get_state_dict(self):
        humans_light = [
            {'id': h['id'], 'x': h['x'], 'y': h['y'], 'timestamp': h['timestamp']}
            for h in self.found_humans
        ]
        # Статус каждого мотора
        motors_status = {
            'enabled':  self.motors.enabled,
            'status':   self.motors.status,
            'forward':  self.current_command == 'FORWARD',
            'backward': self.current_command == 'BACKWARD',
            'left':     self.current_command == 'LEFT',
            'right':    self.current_command == 'RIGHT',
            'stopped':  self.current_command == 'STOP',
        }
        return {
            'x': self.x,
            'y': self.y,
            'angle': self.angle,
            'battery': max(0, int(self.battery_percent)),
            'voltage': self.battery_voltage,
            'current_command': self.current_command,
            'current_speed': self.current_speed,
            'found_humans': humans_light,
            'face_count': len(self.detected_faces),
            'uptime': time.time() - self.start_time,
            'path_length': len(self.path_history),
            'gpio_enabled': self.motors.enabled,
            'gpio_status': self.motors.status,
            'motors': motors_status,
            'robot_mode': _robot_mode,
            'autopilot': autopilot.get_status() if autopilot else {'enabled': False, 'mode': 'СТОП'},
        }

# ============================================================================
# FLASK ПРИЛОЖЕНИЕ
# ============================================================================

app = Flask(__name__)
robot_simulator = None
camera_manager  = None
face_detector   = None
sonar_sensor    = None
logger          = None
autopilot       = None
motion_detector = None
heatmap         = None
current_frame   = None
frame_lock      = threading.Lock()

# Кэш карты — не перерисовываем чаще чем раз в 400мс
_map_cache      = None
_map_cache_time = 0.0
_MAP_CACHE_TTL  = 0.5  # v3.0: чуть реже перерисовываем

# Режим робота: 'sim' = симуляция, 'real' = реальный GPIO
_robot_mode = 'sim'

# ============================================================================
# ПРОСТОЙ ДЕТЕКТОР ЛИЦ (без Pigo)
# ============================================================================

class PigoCliDetector:
    """Рабочий Pigo детектор из оригинального face_detector_web.py"""
    def __init__(self, config):
        self.config = config
        self.binary = self._find_pigo_binary()
        self.cascade = self._find_cascade()
        self.tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()

    def _find_pigo_binary(self):
        import shutil
        val = os.environ.get("FACE_PIGO_BIN")
        if val and Path(val).exists():
            return val
        return shutil.which("pigo")

    def _find_cascade(self):
        val = os.environ.get("FACE_PIGO_CASCADE")
        if val and Path(val).exists():
            return val
        candidates = [
            Path(__file__).resolve().parent / "cascade" / "facefinder",
            Path("/usr/local/share/pigo/cascade/facefinder"),
            Path("/usr/share/pigo/cascade/facefinder"),
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    @property
    def available(self):
        return bool(self.binary and self.cascade)

    def detect_bytes(self, jpeg_bytes):
        if not self.available:
            return []
        fd, path = tempfile.mkstemp(prefix="pigo_", suffix=".jpg", dir=self.tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(jpeg_bytes)
            return self.detect_file(path)
        finally:
            try: os.remove(path)
            except: pass

    def detect_file(self, image_path):
        cmd = [
            self.binary,
            "-in", image_path,
            "-out", "empty",
            "-cf", self.cascade,
            "-json", "-",
            "-min", str(self.config.pigo_min_size),
            "-max", str(self.config.pigo_max_size),
            "-iou", str(self.config.pigo_iou),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=8)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pigo error")
        return self._parse_json(result.stdout)

    def _parse_json(self, output):
        output = (output or "").strip()
        if not output:
            return []
        start = output.find("[")
        end = output.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        detections = json.loads(output[start:end+1])
        boxes = []
        for item in detections:
            if not isinstance(item, dict):
                continue
            face = item.get("face") if isinstance(item.get("face"), dict) else item
            x = int(face.get("x", 0))
            y = int(face.get("y", 0))
            size = int(face.get("size", face.get("width", 0)))
            if size <= 0:
                continue
            boxes.append({"x": x, "y": y, "size": size,
                          "left": x, "top": y,
                          "right": x + size, "bottom": y + size})
        return boxes


class SimpleFaceDetector:
    """Обёртка над PigoCliDetector с асинхронным вызовом и рисованием bbox"""
    def __init__(self, config):
        self.config = config
        self.detector = PigoCliDetector(config)
        self.last_detections = []
        self.last_jpeg = None
        self.detect_thread = None
        self.last_detect_time = 0
        self.detect_interval = config.detect_interval  # на слабом железе лучше не меньше 0.5
        self.lock = threading.Lock()

        if self.detector.available:
            print(f"[✓] Pigo: {self.detector.binary}")
            print(f"[✓] Cascade: {self.detector.cascade}")
        else:
            print(f"[⚠️] Pigo не найден — обнаружение лиц отключено")
            print(f"     binary: {self.detector.binary}")
            print(f"     cascade: {self.detector.cascade}")

    def detect_async(self, image):
        """Принимает PIL Image, запускает детекцию в фоне"""
        now = time.time()
        if now - self.last_detect_time < self.detect_interval:
            return self.last_detections
        if self.detect_thread and self.detect_thread.is_alive():
            return self.last_detections

        self.last_detect_time = now

        # Конвертируем PIL Image в JPEG bytes для Pigo
        try:
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=75)
            self.last_jpeg = buf.getvalue()
        except:
            return self.last_detections

        self.detect_thread = threading.Thread(target=self._worker, daemon=True)
        self.detect_thread.start()
        return self.last_detections

    def _worker(self):
        if not self.last_jpeg:
            return
        try:
            faces = self.detector.detect_bytes(self.last_jpeg)
            with self.lock:
                self.last_detections = faces
        except Exception as e:
            pass

    def draw_on_frame(self, img):
        """Рисует зелёные прямоугольники — точно как в оригинале"""
        with self.lock:
            faces = list(self.last_detections)

        if not faces:
            return img

        draw = ImageDraw.Draw(img)
        for face in faces:
            left   = max(0, face["left"])
            top    = max(0, face["top"])
            right  = min(img.width - 1, face["right"])
            bottom = min(img.height - 1, face["bottom"])

            # Двойной прямоугольник как в оригинале
            for offset in range(2):
                draw.rectangle(
                    [left - offset, top - offset, right + offset, bottom + offset],
                    outline=(0, 255, 0),
                )
        return img


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ СИСТЕМЫ
# ============================================================================

# ============================================================================
# ДАТЧИК РАССТОЯНИЯ HY-SRF05 (симуляция + реальный GPIO)
# ============================================================================
#
#  HY-SRF05        Orange Pi (40-pin)
#  ─────────        ──────────────────
#  VCC     →        Pin 2  (5V)
#  GND     →        Pin 6  (GND)
#  TRIG    →        Pin 22 (PA3 / GPIO 3)
#  ECHO    →        Pin 18 (PA2 / GPIO 2)  ← через делитель 1кОм/2кОм!
#  OUT     →        не используется
#
#  ⚠️  ECHO выдаёт 5V — нужен делитель напряжения!
#  ECHO → R1(1кОм) → GPIO → R2(2кОм) → GND
#
# ============================================================================

class SonarSensor:
    """
    Ультразвуковой датчик HY-SRF05.
    Только реальный GPIO — без симуляции.
    Статусы: OK / ВЫКЛ / ОШИБКА / НЕТ_GPIO
    """

    PIN_TRIG = 22
    PIN_ECHO = 7
    MAX_HISTORY = 36

    # Статусы
    ST_OK       = 'OK'
    ST_OFF      = 'ВЫКЛ'
    ST_ERROR    = 'ОШИБКА'
    ST_NO_GPIO  = 'НЕТ_GPIO'
    ST_TIMEOUT  = 'ТАЙМАУТ'

    def __init__(self):
        self.enabled      = False
        self.active       = False   # Включён пользователем
        self.distance_cm  = 999.0
        self.sweep_angle  = 0
        self.status       = self.ST_OFF
        self.error_msg    = ''
        self.error_count  = 0
        self.last_ok_time = 0

        self.lock         = threading.Lock()
        self.radar_points = deque(maxlen=self.MAX_HISTORY)
        self.running      = False
        self.thread       = None

        # Инициализация GPIO
        if not GPIO_AVAILABLE:
            self.status    = self.ST_NO_GPIO
            self.error_msg = 'OPi.GPIO не установлен'
            print(f"[✗] Сонар: {self.error_msg}")
            return

        try:
            GPIO.setmode(GPIO.BOARD)
            GPIO.setwarnings(False)
            GPIO.setup(self.PIN_TRIG, GPIO.OUT)
            GPIO.setup(self.PIN_ECHO, GPIO.IN)
            GPIO.output(self.PIN_TRIG, GPIO.LOW)
            time.sleep(0.05)
            self.enabled   = True
            self.status    = self.ST_OFF  # GPIO готов, но ещё не включён
            print(f"[✓] Сонар: GPIO инициализирован")
            print(f"    TRIG=Pin{self.PIN_TRIG}  ECHO=Pin{self.PIN_ECHO}")
        except Exception as e:
            self.status    = self.ST_ERROR
            self.error_msg = str(e)
            print(f"[✗] Сонар: ошибка GPIO — {e}")

    def start(self):
        """Запустить поток измерений"""
        self.running = True
        self.thread  = threading.Thread(target=self._measure_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def turn_on(self):
        """Включить сонар"""
        if not self.enabled:
            return False, self.error_msg or 'GPIO недоступен'
        self.active = True
        self.status = self.ST_OK
        self.error_count = 0
        if logger:
            logger.log_event('СОНАР_ВКЛ', f'Pin TRIG={self.PIN_TRIG} ECHO={self.PIN_ECHO}')
        print("[✓] Сонар: включён")
        return True, 'OK'

    def turn_off(self):
        """Выключить сонар"""
        self.active = False
        self.status = self.ST_OFF
        with self.lock:
            self.distance_cm = 999.0
            self.radar_points.clear()
        if logger:
            logger.log_event('СОНАР_ВЫКЛ', 'Сонар отключён')
        print("[✓] Сонар: выключен")

    def _measure_loop(self):
        """Цикл измерений с вращающимся радаром"""
        sweep_angle = 0
        sweep_dir   = 1
        sweep_speed = 5

        while self.running:
            try:
                if not self.active or not self.enabled:
                    time.sleep(0.2)
                    continue

                dist, err = self._measure_real()

                robot_angle = robot_simulator.angle if robot_simulator else 0
                rx = robot_simulator.x if robot_simulator else 400
                ry = robot_simulator.y if robot_simulator else 300
                abs_angle = (robot_angle + sweep_angle) % 360

                with self.lock:
                    self.sweep_angle = sweep_angle

                    if err:
                        self.error_count += 1
                        # После 10 таймаутов подряд — статус ТАЙМАУТ
                        if self.error_count >= 10:
                            self.status    = self.ST_TIMEOUT
                            self.error_msg = 'Нет ответа от датчика'
                    else:
                        self.distance_cm  = dist
                        self.error_count  = 0
                        self.last_ok_time = time.time()
                        self.status       = self.ST_OK
                        self.error_msg    = ''
                        self.radar_points.append({
                            'angle':     abs_angle,
                            'rel_angle': sweep_angle,
                            'dist':      dist,
                            'x':         rx,
                            'y':         ry,
                            'ts':        time.time(),
                        })

                # Вращение ±60°
                sweep_angle += sweep_dir * sweep_speed
                if sweep_angle >= 60:
                    sweep_angle = 60
                    sweep_dir = -1
                elif sweep_angle <= -60:
                    sweep_angle = -60
                    sweep_dir = 1

                time.sleep(0.08)

            except Exception as e:
                with self.lock:
                    self.status    = self.ST_ERROR
                    self.error_msg = str(e)
                time.sleep(0.3)

    def _measure_real(self):
        """Одно измерение. Возвращает (dist, error)"""
        try:
            GPIO.output(self.PIN_TRIG, GPIO.HIGH)
            time.sleep(0.00001)
            GPIO.output(self.PIN_TRIG, GPIO.LOW)

            timeout = time.time() + 0.035
            while GPIO.input(self.PIN_ECHO) == 0:
                if time.time() > timeout:
                    return 999.0, 'timeout_low'
            t_start = time.time()

            timeout = time.time() + 0.035
            while GPIO.input(self.PIN_ECHO) == 1:
                if time.time() > timeout:
                    return 999.0, 'timeout_high'
            t_end = time.time()

            dist = (t_end - t_start) * 34300 / 2
            if dist < 2 or dist > 400:
                return 999.0, 'out_of_range'

            return round(dist, 1), None

        except Exception as e:
            return 999.0, str(e)

    def get_status(self):
        try:
            with self.lock:
                dist  = self.distance_cm
                pts   = list(self.radar_points)
                sweep = self.sweep_angle
                st    = self.status
                err   = self.error_msg
                errc  = self.error_count
            return {
                'distance_cm':  round(float(dist), 1),
                'radar_points': pts,
                'enabled':      self.enabled,
                'active':       self.active,
                'status':       st,
                'error_msg':    err,
                'error_count':  errc,
                'obstacle':     dist < 30 and self.active,
                'running':      self.running,
                'robot_angle':  robot_simulator.angle if robot_simulator else 0,
                'sweep_angle':  sweep,
                'obstacles':    [],
                'sim_mode':     False,
                'last_ok':      round(time.time() - self.last_ok_time, 1) if self.last_ok_time else None,
            }
        except Exception:
            return {
                'distance_cm': 999.0, 'radar_points': [], 'enabled': False,
                'active': False, 'status': self.ST_ERROR, 'error_msg': 'Исключение',
                'error_count': 0, 'obstacle': False, 'running': False,
                'robot_angle': 0, 'sweep_angle': 0, 'obstacles': [],
                'sim_mode': False, 'last_ok': None,
            }



# ============================================================================
# ЛОГГЕР — сохраняет маршрут, события и скриншоты
# ============================================================================

class Logger:
    """Логирование маршрута, событий и скриншотов"""

    def __init__(self):
        self.log_dir = os.path.expanduser('~/mapc_logs')
        os.makedirs(self.log_dir, exist_ok=True)

        # Текущая сессия
        self.session_id = time.strftime('%Y%m%d_%H%M%S')
        self.session_dir = os.path.join(self.log_dir, self.session_id)
        os.makedirs(self.session_dir, exist_ok=True)

        self.log_file = os.path.join(self.session_dir, 'events.log')
        self.path_file = os.path.join(self.session_dir, 'path.csv')
        self.events = []

        # CSV заголовок для маршрута
        with open(self.path_file, 'w') as f:
            f.write('timestamp,x,y,angle,command,distance_cm\n')

        self.log_event('SESSION_START', f'Сессия {self.session_id}')
        print(f"[✓] Logger: {self.session_dir}")
        # TODO: добавить ротацию логов чтобы не забивало диск

    def log_event(self, event_type, details=''):
        """Записать событие"""
        ts = time.strftime('%H:%M:%S')
        line = f"[{ts}] {event_type}: {details}"
        self.events.append({'time': ts, 'type': event_type, 'details': details})

        with open(self.log_file, 'a') as f:
            f.write(line + '\n')

    def log_path(self, x, y, angle, command, distance_cm):
        """Записать точку маршрута"""
        ts = time.time()
        with open(self.path_file, 'a') as f:
            f.write(f'{ts:.2f},{x:.1f},{y:.1f},{angle:.1f},{command},{distance_cm:.1f}\n')

    def save_screenshot(self, image, label='detection'):
        """Сохранить скриншот"""
        try:
            filename = f'{label}_{time.strftime("%H%M%S")}.jpg'
            path = os.path.join(self.session_dir, filename)
            image.save(path, 'JPEG', quality=85)
            self.log_event('SCREENSHOT', filename)
            return filename
        except Exception as e:
            return None

    def get_recent_events(self, n=20):
        """Получить последние N событий"""
        return self.events[-n:]

    def get_stats(self):
        """Статистика сессии"""
        return {
            'session_id': self.session_id,
            'log_dir': self.session_dir,
            'events_count': len(self.events),
            'uptime': time.strftime('%H:%M:%S'),
        }


# ============================================================================
# АВТОПИЛОТ — алгоритм автономного поиска
# ============================================================================

class AutoPilot:
    """Автономный поиск человека."""

    РЕЖИМ_СТОП    = 'СТОП'
    РЕЖИМ_ПОИСК   = 'ПОИСК'
    РЕЖИМ_ОБЪЕЗД  = 'ОБЪЕЗД'
    РЕЖИМ_ПОВОРОТ = 'ПОВОРОТ'
    РЕЖИМ_НАЙДЕН  = 'НАЙДЕН'
    РЕЖИМ_ВОЗВРАТ = 'ВОЗВРАТ'

    DIST_СТОП   = 25   # см — экстренный стоп
    DIST_ОБЪЕЗД = 45   # см — начало объезда
    ОТСТУП      = 70   # px — отступ от края карты

    def __init__(self):
        self.режим   = self.РЕЖИМ_СТОП
        self.enabled = False
        self.running = False
        self.thread  = None
        self._шаги         = 0
        self._макс_шагов   = 30
        self._поворотов    = 0
        self._шаги_поворота = 0
        self._макс_поворота = 18
        self._объезд_шаги  = 0
        self._return_path  = []
        self._return_idx   = 0
        print("[✓] Автопилот: готов")

    def start(self):
        if self.running:
            return
        self.режим      = self.РЕЖИМ_ПОИСК
        self.enabled    = True
        self.running    = True
        self._шаги      = 0
        self._поворотов = 0
        self._макс_шагов = 30
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        if logger:
            logger.log_event('АВТОПИЛОТ_СТАРТ', 'Поиск запущен')
        print("[✓] Автопилот: старт")

    def stop(self):
        self.running = False
        self.enabled = False
        self.режим   = self.РЕЖИМ_СТОП
        if robot_simulator:
            robot_simulator.stop()
        if logger:
            logger.log_event('АВТОПИЛОТ_СТОП', 'Остановлен вручную')
        print("[✓] Автопилот: остановлен")

    def _у_края(self):
        if not robot_simulator:
            return False
        x, y = robot_simulator.x, robot_simulator.y
        o = self.ОТСТУП
        return (x < o or x > robot_config.map_width - o or
                y < o or y > robot_config.map_height - o)

    def _препятствие(self):
        if not sonar_sensor or not sonar_sensor.active:
            return False, 999
        d = sonar_sensor.distance_cm
        return d < self.DIST_ОБЪЕЗД, d

    def _loop(self):
        while self.running:
            try:
                mode = self.режим

                if mode == self.РЕЖИМ_СТОП:
                    robot_simulator.stop()
                    time.sleep(0.2)
                elif mode == self.РЕЖИМ_НАЙДЕН:
                    robot_simulator.stop()
                    time.sleep(0.3)
                elif mode == self.РЕЖИМ_ВОЗВРАТ:
                    self._do_return()
                elif mode == self.РЕЖИМ_ПОВОРОТ:
                    self._do_turn()
                elif mode == self.РЕЖИМ_ОБЪЕЗД:
                    self._do_avoid()
                elif mode == self.РЕЖИМ_ПОИСК:
                    self._do_search()

                if robot_simulator and robot_simulator.detected_faces and \
                   mode not in (self.РЕЖИМ_НАЙДЕН, self.РЕЖИМ_СТОП):
                    self._on_found()

                time.sleep(0.1)
            except Exception:
                time.sleep(0.5)

    def _do_search(self):
        # Приоритет 1: край карты
        if self._у_края():
            robot_simulator.stop()
            self._поворот_к_центру()
            return
        # Приоритет 2: препятствие
        есть, dist = self._препятствие()
        if есть:
            robot_simulator.stop()
            self.режим = self.РЕЖИМ_ОБЪЕЗД
            self._объезд_шаги = 0
            if logger:
                logger.log_event('ПРЕПЯТСТВИЕ', f'{dist:.0f}см')
            return
        # Движение прямо
        if self._шаги < self._макс_шагов:
            robot_simulator.move_forward()
            self._шаги += 1
        else:
            robot_simulator.stop()
            self._поворотов += 1
            if self._поворотов % 2 == 0:
                self._макс_шагов = min(self._макс_шагов + 8, 80)
            self._начать_поворот(90)
            if logger:
                logger.log_event('СПИРАЛЬ', f'поворот #{self._поворотов}, прямо={self._макс_шагов}')

    def _начать_поворот(self, градусов=90):
        self.режим = self.РЕЖИМ_ПОВОРОТ
        self._шаги_поворота = 0
        self._макс_поворота = max(6, int(градусов / 5))
        self._шаги = 0

    def _поворот_к_центру(self):
        cx = robot_config.map_width  / 2
        cy = robot_config.map_height / 2
        dx = cx - robot_simulator.x
        dy = cy - robot_simulator.y
        target  = math.degrees(math.atan2(dx, -dy)) % 360
        current = robot_simulator.angle % 360
        diff    = (target - current + 360) % 360
        self.режим = self.РЕЖИМ_ПОВОРОТ
        self._шаги_поворота = 0
        self._макс_поворота = max(4, int(diff / 5))
        self._шаги = 0
        if logger:
            logger.log_event('РАЗВОРОТ', f'→ центру ({target:.0f}°)')

    def _do_turn(self):
        if self._шаги_поворота < self._макс_поворота:
            robot_simulator.turn_right()
            self._шаги_поворота += 1
        else:
            robot_simulator.stop()
            self.режим = self.РЕЖИМ_ПОИСК
            self._шаги = 0

    def _do_avoid(self):
        s = self._объезд_шаги
        _, dist = self._препятствие()
        if s < 3:
            robot_simulator.stop()
        elif s < 12:
            robot_simulator.turn_right()
        elif s < 30:
            if dist > self.DIST_СТОП:
                robot_simulator.move_forward()
            else:
                robot_simulator.turn_right()
                self._объезд_шаги = 12
        else:
            robot_simulator.stop()
            self.режим = self.РЕЖИМ_ПОИСК
            self._шаги = 0
            if logger:
                logger.log_event('ОБЪЕЗД_ОК', 'Продолжаю поиск')
        self._объезд_шаги += 1

    def _do_return(self):
        if not self._return_path or self._return_idx >= len(self._return_path):
            robot_simulator.stop()
            self.режим   = self.РЕЖИМ_СТОП
            self.running = False
            if logger:
                logger.log_event('БАЗА', 'Вернулся на базу')
            return
        tx, ty = self._return_path[self._return_idx]
        rx, ry = robot_simulator.x, robot_simulator.y
        dist   = math.sqrt((tx-rx)**2 + (ty-ry)**2)
        if dist < 15:
            self._return_idx += 3
            return
        target  = math.degrees(math.atan2(tx-rx, -(ty-ry))) % 360
        current = robot_simulator.angle % 360
        diff    = (target - current + 360) % 360
        if diff > 20 and diff < 340:
            robot_simulator.turn_right() if diff < 180 else robot_simulator.turn_left()
        else:
            robot_simulator.move_forward()
            self._return_idx += 1
        time.sleep(0.08)

    def _on_found(self):
        if self.режим == self.РЕЖИМ_НАЙДЕН:
            return
        self.режим = self.РЕЖИМ_НАЙДЕН
        robot_simulator.stop()
        if logger:
            logger.log_event('ЧЕЛОВЕК_НАЙДЕН',
                f'({robot_simulator.x:.0f}, {robot_simulator.y:.0f})')
        if current_frame and logger:
            with frame_lock:
                fc = current_frame.copy()
            logger.save_screenshot(fc, 'найден')
        print("[!] ЧЕЛОВЕК НАЙДЕН!")
        self._return_path = list(robot_simulator.path_history)[::-1]
        self._return_idx  = 0
        threading.Timer(3.0, lambda: setattr(self, 'режим', self.РЕЖИМ_ВОЗВРАТ)).start()

    def get_status(self):
        return {
            'enabled':   self.enabled,
            'mode':      self.режим,
            'steps':     self._шаги,
            'max_steps': self._макс_шагов,
            'turns':     self._поворотов,
        }


class MotionDetector:
    """Простой детектор движения — сравнивает кадры"""
    def __init__(self, threshold=0.08):
        self.threshold     = threshold
        self.prev_gray     = None
        self.motion_level  = 0.0
        self.motion_detected = False
        self.lock          = threading.Lock()
        print("[✓] Детектор движения: готов")

    def process(self, img):
        try:
            small  = img.resize((80, 60)).convert('L')
            pixels = list(small.getdata())
            w, h   = small.size

            if self.prev_gray is None:
                self.prev_gray = pixels
                return 0.0

            diff  = sum(abs(a - b) for a, b in zip(pixels, self.prev_gray))
            level = diff / (w * h * 255)
            self.prev_gray = pixels

            with self.lock:
                self.motion_level    = round(level, 3)
                self.motion_detected = level > self.threshold

            return level
        except Exception:
            return 0.0

    def get_status(self):
        with self.lock:
            return {
                'level':    self.motion_level,
                'detected': self.motion_detected,
                'percent':  round(self.motion_level * 100, 1),
            }



# ============================================================================
# IMU — АКСЕЛЕРОМЕТР/ГИРОСКОП (заготовка для MPU-6050)
# ============================================================================
# TODO: подключить MPU-6050 через I2C
#   SDA → Pin 3 (I2C SDA)
#   SCL → Pin 5 (I2C SCL)
#   VCC → Pin 1 (3.3V)
#   GND → Pin 6 (GND)
#   pip install mpu6050-raspberrypi
#
# Даст нам:
#   - Угол наклона (завал, опрокидывание)
#   - Угловая скорость поворота
#   - Ускорение (удар, препятствие)
#   - Компенсация дрейфа направления

class IMUSensor:
    """
    Заготовка для MPU-6050 акселерометр + гироскоп.
    Пока возвращает нули — подключить когда будет датчик.
    """
    def __init__(self):
        self.enabled     = False
        self.angle_x     = 0.0   # крен
        self.angle_y     = 0.0   # тангаж
        self.angle_z     = 0.0   # рыскание (курс)
        self.accel_x     = 0.0
        self.accel_y     = 0.0
        self.accel_z     = 0.0
        self.gyro_z      = 0.0   # угловая скорость

        # TODO: раскомментировать когда подключим MPU-6050
        # try:
        #     import mpu6050
        #     self.mpu = mpu6050.mpu6050(0x68)
        #     self.enabled = True
        #     print("[✓] IMU: MPU-6050 готов")
        # except Exception as e:
        #     print(f"[⚠️] IMU: {e} — используем GPS/карту для ориентации")
        print("[⚠️] IMU: не подключён (MPU-6050 нужен)")

    def get_status(self):
        return {
            'enabled': self.enabled,
            'angle_x': self.angle_x,
            'angle_y': self.angle_y,
            'angle_z': self.angle_z,
            'accel_x': self.accel_x,
            'accel_y': self.accel_y,
            'gyro_z':  self.gyro_z,
        }


# ============================================================================
# ТЕПЛОВАЯ КАРТА ПОКРЫТИЯ
# ============================================================================

class HeatMap:
    CELL = 40

    def __init__(self, width, height):
        self.cols   = width  // self.CELL + 1
        self.rows   = height // self.CELL + 1
        self.visits = [[0] * self.cols for _ in range(self.rows)]
        print(f"[✓] Тепловая карта: {self.cols}x{self.rows} ячеек")

    def update(self, x, y):
        col = int(x // self.CELL)
        row = int(y // self.CELL)
        if 0 <= row < self.rows and 0 <= col < self.cols:
            self.visits[row][col] = min(255, self.visits[row][col] + 1)

    def get_coverage(self):
        visited = sum(1 for row in self.visits for v in row if v > 0)
        total   = self.cols * self.rows
        return round(visited / total * 100, 1) if total > 0 else 0.0

    def draw_overlay(self, img):
        draw = ImageDraw.Draw(img, 'RGBA')
        max_v = max((max(row) for row in self.visits), default=1) or 1

        for r in range(self.rows):
            for c in range(self.cols):
                v = self.visits[r][c]
                if v == 0:
                    continue
                t = v / max_v
                if t < 0.5:
                    red, green, blue = 0, int(t * 2 * 200), 150
                else:
                    red, green, blue = int((t - 0.5) * 2 * 200), 200, 0
                alpha = int(35 + t * 75)
                x0 = c * self.CELL
                y0 = r * self.CELL
                draw.rectangle(
                    [x0, y0, x0 + self.CELL - 1, y0 + self.CELL - 1],
                    fill=(red, green, blue, alpha)
                )
        return img


# ============================================================================
# ГЕНЕРАТОР ОТЧЁТА
# ============================================================================

class ReportGenerator:
    def generate(self, robot_sim, logger_inst, map_img):
        import base64
        ts     = time.strftime('%d.%m.%Y %H:%M:%S')
        uptime = time.time() - robot_sim.start_time
        path   = list(robot_sim.path_history)

        dist_total = sum(
            math.sqrt((path[i][0]-path[i-1][0])**2 + (path[i][1]-path[i-1][1])**2)
            for i in range(1, len(path))
        )

        photos_html = ""
        for h in robot_sim.found_humans:
            if h.get("photo"):
                ts_h = time.strftime("%H:%M:%S", time.localtime(h["timestamp"]))
                photos_html += f"""
                <div class="photo-card">
                    <img src="data:image/jpeg;base64,{h["photo"]}">
                    <div class="photo-info">
                        <strong>Человек #{h["id"]}</strong><br>
                        {ts_h} · ({h["x"]:.0f}, {h["y"]:.0f})
                    </div>
                </div>"""

        map_b64 = ""
        try:
            buf = io.BytesIO()
            map_img.save(buf, "JPEG", quality=85)
            map_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass

        events_html = ""
        if logger_inst:
            for e in logger_inst.events:
                cls = " found" if "НАЙДЕН" in e["type"] else ""
                events_html += f'<div class="event{cls}">[{e["time"]}] <strong>{e["type"]}</strong> — {e["details"]}</div>\n'

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>М.А.Р.С. — Отчёт {ts}</title>
<style>
    body{{font-family:Arial,sans-serif;margin:30px;color:#222;}}
    h1{{color:#006644;border-bottom:3px solid #006644;padding-bottom:10px;}}
    h2{{color:#004488;margin-top:30px;}}
    .header{{display:flex;justify-content:space-between;align-items:flex-start;}}
    .logo{{font-size:48px;font-weight:900;color:#006644;letter-spacing:4px;}}
    .meta{{text-align:right;color:#666;font-size:13px;}}
    .stats-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin:20px 0;}}
    .stat-box{{background:#f0f8f4;border:1px solid #c0ddd0;border-radius:8px;padding:15px;text-align:center;}}
    .stat-num{{font-size:28px;font-weight:bold;color:#006644;}}
    .stat-lbl{{font-size:12px;color:#666;margin-top:4px;}}
    .map-img{{width:100%;border:2px solid #006644;border-radius:8px;margin:10px 0;}}
    .photo-grid{{display:flex;flex-wrap:wrap;gap:15px;margin:15px 0;}}
    .photo-card{{border:1px solid #ddd;border-radius:8px;overflow:hidden;width:200px;}}
    .photo-card img{{width:100%;display:block;}}
    .photo-info{{padding:8px;font-size:12px;background:#f9f9f9;}}
    .event{{padding:6px 12px;margin:4px 0;border-left:3px solid #006644;background:#f8f8f8;font-size:12px;}}
    .event.found{{border-color:#cc3333;background:#fff5f5;}}
    .footer{{margin-top:40px;padding-top:15px;border-top:1px solid #ddd;font-size:11px;color:#999;text-align:center;}}
    @media print{{@page{{margin:20mm;}}button{{display:none;}}}}
</style>
</head>
<body>
<div class="header">
    <div>
        <div class="logo">М.А.Р.С.</div>
        <div style="color:#666;font-size:13px;margin-top:4px;">Мобильный Автоматизированный Робот Спасатель</div>
    </div>
    <div class="meta">
        <strong>Отчёт о сессии поиска</strong><br>
        Дата: {ts}<br>
        Сессия: {logger_inst.session_id if logger_inst else "—"}
    </div>
</div>
<h1>Результаты поиска</h1>
<div class="stats-grid">
    <div class="stat-box"><div class="stat-num">{len(robot_sim.found_humans)}</div><div class="stat-lbl">Найдено людей</div></div>
    <div class="stat-box"><div class="stat-num">{int(uptime//60)}м {int(uptime%60)}с</div><div class="stat-lbl">Время поиска</div></div>
    <div class="stat-box"><div class="stat-num">{dist_total/100:.1f} м</div><div class="stat-lbl">Пройдено</div></div>
    <div class="stat-box"><div class="stat-num">{len(path)}</div><div class="stat-lbl">Точек маршрута</div></div>
    <div class="stat-box"><div class="stat-num">{int(robot_sim.battery_percent)}%</div><div class="stat-lbl">Батарея</div></div>
    <div class="stat-box"><div class="stat-num">{"GPIO" if robot_sim.motors.enabled else "СИМ"}</div><div class="stat-lbl">Режим</div></div>
</div>
<h2>Карта маршрута</h2>
{"<img class=\"map-img\" src=\"data:image/jpeg;base64," + map_b64 + "\">" if map_b64 else "<p>Карта недоступна</p>"}
<h2>Обнаруженные люди ({len(robot_sim.found_humans)})</h2>
{"<div class=\"photo-grid\">" + photos_html + "</div>" if photos_html else "<p>Людей не обнаружено</p>"}
<h2>Журнал событий</h2>
<div>{events_html}</div>
<button onclick="window.print()" style="margin-top:20px;padding:12px 24px;background:#006644;color:white;border:none;border-radius:6px;font-size:14px;cursor:pointer;">🖨️ Печать / Сохранить PDF</button>
<div class="footer">М.А.Р.С. v3.0 · Orange Pi PC H3 · {time.strftime("%Y")}</div>
</body></html>"""


report_generator = ReportGenerator()
motion_detector  = None
heatmap          = None

def initialize_system():
    global robot_simulator, camera_manager, face_detector, sonar_sensor
    global logger, autopilot, motion_detector, heatmap

    robot_simulator = RobotSimulator()
    camera_manager  = CameraManager(camera_config)
    face_detector   = SimpleFaceDetector(camera_config)
    sonar_sensor    = SonarSensor()
    sonar_sensor.start()
    logger          = Logger()
    autopilot       = AutoPilot()
    motion_detector = MotionDetector()
    heatmap         = HeatMap(robot_config.map_width, robot_config.map_height)

    threading.Thread(target=_path_logger_loop, daemon=True).start()

def _path_logger_loop():
    """Фоновый поток записи маршрута каждые 2 секунды"""
    while True:
        try:
            if robot_simulator and logger:
                dist = sonar_sensor.distance_cm if sonar_sensor else 999
                logger.log_path(
                    robot_simulator.x, robot_simulator.y,
                    robot_simulator.angle,
                    robot_simulator.current_command,
                    dist
                )
        except:
            pass
        time.sleep(2)

def draw_map():
    """Оптимизированная карта — слои"""
    W = robot_config.map_width
    H = robot_config.map_height

    img  = Image.new('RGB', (W, H), color='#060d0a')
    draw = ImageDraw.Draw(img)

    # Сетка (тонкая)
    for x in range(0, W, 60):
        draw.line([(x,0),(x,H)], fill=(15,28,18), width=1)
    for y in range(0, H, 60):
        draw.line([(0,y),(W,y)], fill=(15,28,18), width=1)

    # Граница зоны автопилота
    o = 60
    draw.rectangle([o,o,W-o,H-o], outline=(25,50,30), width=1)

    # Тепловая карта покрытия
    if heatmap:
        img = heatmap.draw_overlay(img)
        draw = ImageDraw.Draw(img)

    # Облако точек сонара
    if sonar_sensor:
        with sonar_sensor.lock:
            pts = list(sonar_sensor.radar_points)
        for pt in pts[::2]:  # Каждую вторую точку — быстрее
            if pt['dist'] < 260:
                px = pt['x'] + math.sin(math.radians(pt['angle'])) * pt['dist']
                py = pt['y'] - math.cos(math.radians(pt['angle'])) * pt['dist']
                if 0 <= px < W and 0 <= py < H:
                    draw.ellipse([px-2,py-2,px+2,py+2], fill=(160,80,20))

    # Луч сонара
    if sonar_sensor and robot_simulator:
        s = sonar_sensor.get_status()
        dist    = s['distance_cm']
        rx, ry  = robot_simulator.x, robot_simulator.y
        sweep   = s['sweep_angle']
        abs_ang = (robot_simulator.angle + sweep) % 360
        ang_r   = math.radians(abs_ang)
        cone    = min(dist, 180)

        # Конус (только центральный и ±15°)
        for a in [-15, 0, 15]:
            ar = math.radians(abs_ang + a)
            alpha = 30 if a == 0 else 12
            draw.line([(rx,ry),(rx+math.sin(ar)*cone, ry-math.cos(ar)*cone)],
                      fill=(0,alpha,0), width=1)

        # Луч
        draw.line([(rx,ry),(rx+math.sin(ang_r)*cone, ry-math.cos(ang_r)*cone)],
                  fill=(0,200,60), width=2)

        # Маркер препятствия
        if dist < 180:
            ox = rx + math.sin(ang_r)*dist
            oy = ry - math.cos(ang_r)*dist
            c  = (220,50,50) if dist < 30 else (220,140,0)
            draw.ellipse([ox-4,oy-4,ox+4,oy+4], fill=c)

    # История пути — упрощённо
    path = list(robot_simulator.path_history)
    if len(path) > 1:
        # Рисуем только каждую вторую точку
        pts2 = path[::2]
        total = len(pts2)
        for i in range(1, total):
            t = i / total
            g = int(60 + t * 140)
            draw.line([pts2[i-1], pts2[i]], fill=(0, g, int(g*0.4)), width=2)

    # База
    if robot_simulator:
        bx, by = robot_simulator.base_x, robot_simulator.base_y
        draw.ellipse([bx-14,by-14,bx+14,by+14], outline=(60,120,200), width=1)
        draw.ellipse([bx-5, by-5, bx+5, by+5],  fill=(60,120,200))

    # Найденные люди
    for h in robot_simulator.found_humans:
        x, y = h['x'], h['y']
        if 0 <= x < W and 0 <= y < H:
            draw.ellipse([x-10,y-10,x+10,y+10], fill=(180,50,50), outline=(255,120,80), width=2)
            draw.text((x-3,y-7), str(h['id']), fill=(255,255,255))

    # Робот
    rx = max(5, min(W-5, robot_simulator.x))
    ry = max(5, min(H-5, robot_simulator.y))
    robot_simulator.x, robot_simulator.y = rx, ry

    s    = robot_config.robot_size
    ang  = math.radians(robot_simulator.angle)
    ex   = rx + s*1.8*math.sin(ang)
    ey   = ry - s*1.8*math.cos(ang)

    draw.ellipse([rx-s,ry-s,rx+s,ry+s], fill=(40,180,80), outline=(0,230,60), width=2)
    draw.line([(rx,ry),(ex,ey)], fill=(0,180,255), width=3)

    # Автопилот режим
    if autopilot and autopilot.enabled:
        colors = {'ПОИСК':(0,200,80),'ОБЪЕЗД':(220,140,0),
                  'НАЙДЕН':(220,50,50),'ВОЗВРАТ':(80,120,255)}
        c = colors.get(autopilot.режим, (150,150,150))
        draw.text((W-110,8), f"АВТО:{autopilot.режим}", fill=c)

    # HUD
    draw.text((8,8),  f"({int(rx)},{int(ry)}) {int(robot_simulator.angle)}°",
              fill=(50,160,80))
    if heatmap:
        draw.text((8,24), f"Покрытие: {heatmap.get_coverage():.0f}%",
                  fill=(40,120,60))
    if sonar_sensor:
        dist = sonar_sensor.distance_cm
        c2 = (220,50,50) if dist < 30 else (160,230,80)
        draw.text((8,40), f"Сонар: {dist:.0f}см", fill=c2)

    return img

def _skin_color_detect(img):
    """Детектор кожного цвета — HSV анализ без нейросети.
    Возвращает список (x, y, w, h) найденных областей."""
    try:
        w, h = img.size
        small = img.resize((160, 120))
        pixels = list(small.getdata())
        bboxes = []
        # Маска кожного цвета в HSV
        skin_pixels = []
        for idx, (r, g, b) in enumerate(pixels):
            # RGB → HSV
            r_, g_, b_ = r/255.0, g/255.0, b/255.0
            mx = max(r_, g_, b_)
            mn = min(r_, g_, b_)
            df = mx - mn
            if df == 0:
                continue
            if mx == r_:
                hue = (60 * ((g_ - b_) / df) % 360)
            elif mx == g_:
                hue = 60 * ((b_ - r_) / df) + 120
            else:
                hue = 60 * ((r_ - g_) / df) + 240
            sat = 0 if mx == 0 else df / mx
            val = mx
            # Диапазоны кожного цвета
            is_skin = ((0 <= hue <= 25) or (170 <= hue <= 180)) and                       (0.12 <= sat <= 1.0) and (0.2 <= val <= 1.0)
            if is_skin:
                x_px = (idx % 160) * (w // 160)
                y_px = (idx // 160) * (h // 120)
                skin_pixels.append((x_px, y_px))
        # Простая кластеризация — ищем крупные области
        if len(skin_pixels) > 50:
            xs = [p[0] for p in skin_pixels]
            ys = [p[1] for p in skin_pixels]
            bx, by = min(xs), min(ys)
            bw, bh = max(xs) - bx, max(ys) - by
            if bw * bh > 400:
                bboxes.append((bx, by, bw, bh))
        return bboxes
    except Exception:
        return []


def _combined_detect(img, prev_gray, motion_detector):
    """Комбинированный детектор: движение + skin-color.
    Возвращает (confidence 0-1, bboxes, motion_level)"""
    confidence = 0.0
    bboxes = []

    # Детектор движения
    motion_level = 0.0
    if motion_detector:
        motion_level = motion_detector.process(img)

    # Skin-color детектор
    skin_boxes = _skin_color_detect(img)

    # Совмещаем сигналы
    if skin_boxes:
        confidence += 0.6
        bboxes = skin_boxes
    if motion_level > 0.05:
        confidence += 0.4

    # Нормализуем
    confidence = min(1.0, confidence)
    return confidence, bboxes, motion_level


def capture_and_process_video():
    """Захват видео с комбинированным детектором (движение + skin-color)."""
    global current_frame
    frame_count = 0
    prev_gray = None
    while True:
        try:
            frame_bytes = camera_manager.get_frame()
            if frame_bytes:
                img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
                with frame_lock:
                    current_frame = img

                frame_count += 1

                # Комбинированный детектор (каждый 3-й кадр)
                if frame_count % 3 == 0:
                    confidence, bboxes, motion_level = _combined_detect(img, prev_gray, motion_detector)

                    if robot_simulator:
                        # Обновляем detected_faces для совместимости
                        robot_simulator.detected_faces = [
                            {'x': b[0], 'y': b[1], 'size': max(b[2], b[3]),
                             'left': b[0], 'top': b[1],
                             'right': b[0]+b[2], 'bottom': b[1]+b[3],
                             'confidence': round(confidence, 2)}
                            for b in bboxes
                        ] if bboxes else []

                        if confidence > 0.5 and bboxes:
                            robot_simulator.add_human_detection(
                                robot_simulator.x,
                                robot_simulator.y,
                                img
                            )

                # Pigo детектор лиц (каждый 5-й кадр, если доступен)
                if frame_count % 5 == 0 and face_detector and face_detector.detector.available:
                    faces = face_detector.detect_async(img)
                    if faces and robot_simulator:
                        robot_simulator.detected_faces = faces
                        robot_simulator.add_human_detection(
                            robot_simulator.x, robot_simulator.y, img)

                # Тепловая карта (каждые 10 кадров)
                if frame_count % 10 == 0 and heatmap and robot_simulator:
                    heatmap.update(robot_simulator.x, robot_simulator.y)

            time.sleep(0.04)
        except Exception:
            time.sleep(0.5)

# ============================================================================
# НОВЫЙ КРАСИВЫЙ HTML ИНТЕРФЕЙС
# ============================================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>М.А.Р.С. v3.0</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
:root{
  --bg:    #050908;
  --bg2:   #0a110e;
  --bg3:   #0f1a15;
  --bg4:   #162218;
  --ac:    #00e676;
  --ac2:   #00bcd4;
  --ac3:   #ff5252;
  --warn:  #ffab40;
  --txt:   #7a9a88;
  --txt2:  #3d5c48;
  --brd:   rgba(0,230,118,.1);
  --brd2:  rgba(0,188,212,.1);
  --glow:  0 0 12px rgba(0,230,118,.15);
  --glow2: 0 0 12px rgba(0,188,212,.15);
}

*{margin:0;padding:0;box-sizing:border-box;}

body{
  font-family:'Share Tech Mono',monospace;
  background:var(--bg);
  color:var(--txt);
  font-size:12px;
  overflow-x:hidden;
}

/* фоновая сетка убрана */

/* сканирующая линия убрана */

/* ── HEADER ── */
#header{
  position:relative;z-index:10;
  padding:10px 16px;
  background:linear-gradient(180deg,rgba(0,30,15,.95) 0%,rgba(5,9,8,.98) 100%);
  border-bottom:1px solid var(--brd);
  display:flex;align-items:center;gap:0;
}

.logo-block{
  display:flex;align-items:center;gap:14px;
  padding-right:20px;
  border-right:1px solid var(--brd);
}

.logo-icon{
  font-size:28px;
  animation:logo-pulse 3s ease-in-out infinite;
  filter:drop-shadow(0 0 8px rgba(0,230,118,.5));
}
@keyframes logo-pulse{
  0%,100%{filter:drop-shadow(0 0 6px rgba(0,230,118,.4));}
  50%{filter:drop-shadow(0 0 16px rgba(0,230,118,.8));}
}

.logo-text{
  font-family:Orbitron,sans-serif;
  font-weight:900;
  font-size:22px;
  letter-spacing:6px;
  background:linear-gradient(135deg,#00e676,#00bcd4);
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  background-clip:text;
}

.logo-sub{
  font-size:9px;
  color:var(--txt2);
  letter-spacing:2px;
  margin-top:2px;
  text-transform:uppercase;
}

.logo-badge{
  font-size:9px;
  padding:2px 7px;
  border:1px solid var(--brd);
  border-radius:20px;
  color:var(--txt2);
  margin-top:3px;
  display:inline-block;
}

/* ── ИНФО-ПОЛОСКА ── */
#strip{
  display:flex;
  align-items:center;
  gap:0;
  flex:1;
  padding-left:16px;
  overflow-x:auto;
  scrollbar-width:none;
}
#strip::-webkit-scrollbar{display:none;}

.strip-item{
  display:flex;align-items:center;gap:6px;
  padding:4px 14px;
  border-right:1px solid var(--brd);
  white-space:nowrap;
  transition:background .2s;
}
.strip-item:hover{background:rgba(0,230,118,.03);}
.strip-lbl{font-size:9px;color:var(--txt2);text-transform:uppercase;letter-spacing:1px;}
.strip-val{
  font-family:Orbitron,sans-serif;
  font-size:13px;
  font-weight:700;
  color:var(--ac);
  transition:color .3s;
}
.strip-val.warn{color:var(--warn);}
.strip-val.alert{color:var(--ac3);animation:blink .7s infinite;}
.strip-val.dim{color:var(--txt2);}

#batt-wrap{display:flex;align-items:center;gap:6px;}
#batt-bar{
  width:36px;height:7px;
  background:var(--bg3);
  border-radius:3px;
  border:1px solid var(--brd);
  overflow:hidden;
}
#batt-fill{
  height:100%;width:100%;
  background:linear-gradient(90deg,var(--ac),#69f0ae);
  border-radius:3px;
  transition:width .8s ease,background .5s;
}

.status-pill{
  padding:2px 8px;
  border-radius:10px;
  font-size:9px;
  font-family:Orbitron,sans-serif;
  font-weight:700;
  letter-spacing:1px;
  border:1px solid;
  animation:pill-in .3s ease;
}
@keyframes pill-in{from{opacity:0;transform:scale(.8)}to{opacity:1;transform:scale(1)}}

@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── LAYOUT ── */
.wrap{
  position:relative;z-index:2;
  max-width:1600px;
  margin:0 auto;
  padding:10px;
  display:grid;
  grid-template-columns:1fr 360px;
  gap:10px;
}
@media(max-width:1100px){.wrap{grid-template-columns:1fr;}}

/* ── ПАНЕЛИ ── */
.p{
  background:var(--bg2);
  border:1px solid var(--brd);
  border-radius:8px;
  padding:12px;
  position:relative;
  overflow:hidden;
  transition:border-color .3s;
}
.p:hover{border-color:rgba(0,230,118,.15);}

/* Угловые декоры */
/* угловые декоры убраны */

.p-title{
  font-family:Orbitron,sans-serif;
  font-size:10px;
  color:var(--ac2);
  letter-spacing:2px;
  text-transform:uppercase;
  padding-bottom:8px;
  border-bottom:1px solid var(--brd);
  margin-bottom:10px;
  display:flex;
  align-items:center;
  gap:7px;
}

.dot{
  width:5px;height:5px;
  border-radius:50%;
  background:var(--ac);
  box-shadow:0 0 6px var(--ac);
  flex-shrink:0;
  animation:dot-pulse 2s ease-in-out infinite;
}
@keyframes dot-pulse{
  0%,100%{box-shadow:0 0 4px var(--ac);opacity:1;}
  50%{box-shadow:0 0 10px var(--ac);opacity:.6;}
}

/* ── СТАТУС-СЕТКА ── */
.sg{
  display:grid;
  grid-template-columns:repeat(6,1fr);
  gap:6px;
  margin-bottom:10px;
}

.sc{
  background:var(--bg3);
  border:1px solid var(--brd);
  border-radius:6px;
  padding:9px 6px;
  text-align:center;
  position:relative;
  overflow:hidden;
  cursor:default;
  transition:all .2s;
}
.sc:hover{background:var(--bg4);border-color:rgba(0,230,118,.2);}
.sc::after{
  content:'';
  position:absolute;
  bottom:0;left:0;right:0;
  height:2px;
  background:linear-gradient(90deg,transparent,var(--ac),transparent);
  opacity:.3;
}

.sl{font-size:9px;color:var(--txt2);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
.sv{
  font-family:Orbitron,sans-serif;
  font-size:18px;
  font-weight:700;
  color:var(--ac);
  transition:color .3s;
}
.sv.w{color:var(--warn);}
.sv.a{color:var(--ac3);animation:blink .7s infinite;}

/* ── КАРТА ── */
#mapCanvas{
  width:100%;height:auto;
  display:block;
  border-radius:4px;
  cursor:crosshair;
  transition:opacity .3s;
}
#mapCanvas:hover{opacity:.95;}

/* ── ВИДЕО ── */
#videoImg{
  width:100%;display:block;
  border-radius:4px;
  background:#000;
  min-height:100px;
  transition:opacity .3s;
}
.cam-st{
  text-align:center;
  font-size:10px;
  color:var(--txt2);
  padding:3px 0;
  display:flex;align-items:center;justify-content:center;gap:4px;
}
.cam-dot{
  width:5px;height:5px;
  border-radius:50%;
  background:var(--ac3);
  transition:background .3s;
}
.cam-dot.live{background:var(--ac);box-shadow:0 0 6px var(--ac);}

/* ── РАДАР ── */
#radarCanvas{display:block;margin:0 auto;}

/* ── WASD ── */
.wasd{
  display:grid;
  grid-template-columns:repeat(3,34px);
  gap:4px;
  margin:0 auto 10px;
  width:fit-content;
}
.wk{
  background:var(--bg3);
  border:1px solid var(--brd);
  border-radius:5px;
  text-align:center;
  padding:7px 4px;
  font-size:10px;
  font-family:Orbitron,sans-serif;
  color:var(--ac);
  user-select:none;
  transition:all .08s;
  cursor:default;
}
.wk.pressed{
  background:rgba(0,230,118,.2);
  border-color:var(--ac);
  box-shadow:0 0 8px rgba(0,230,118,.3);
  transform:scale(.93);
}
.wk.stop{color:var(--ac3);border-color:rgba(255,82,82,.2);}
.wk.stop.pressed{background:rgba(255,82,82,.2);border-color:var(--ac3);}

/* ── КНОПКИ ── */
button{
  background:var(--bg3);
  color:var(--ac);
  border:1px solid var(--brd);
  padding:9px 10px;
  font-family:Orbitron,sans-serif;
  font-size:9px;
  font-weight:700;
  cursor:pointer;
  border-radius:5px;
  transition:all .15s;
  text-transform:uppercase;
  letter-spacing:1px;
  position:relative;
  overflow:hidden;
}
button::after{
  content:'';
  position:absolute;
  inset:0;
  background:linear-gradient(135deg,transparent 50%,rgba(255,255,255,.04));
  pointer-events:none;
}
button:hover{background:var(--bg4);border-color:rgba(0,230,118,.3);}
button:active{transform:scale(.95)!important;box-shadow:none;}

.btn-danger{color:var(--ac3);border-color:rgba(255,82,82,.2);background:rgba(255,82,82,.06);}
.btn-danger:hover{border-color:rgba(255,82,82,.5);box-shadow:0 0 10px rgba(255,82,82,.2);}
.btn-info{color:var(--ac2);border-color:var(--brd2);}
.btn-info:hover{box-shadow:var(--glow2);}
.btn-active{background:rgba(0,230,118,.12);border-color:rgba(0,230,118,.4);color:var(--ac);}

.btn-row{display:flex;gap:6px;margin-bottom:8px;}

/* ── СЛАЙДЕР ── */
.speed-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.speed-row .lbl{font-size:10px;color:var(--txt2);white-space:nowrap;}
input[type=range]{
  flex:1;
  -webkit-appearance:none;
  height:3px;
  border-radius:2px;
  background:linear-gradient(90deg,var(--ac),var(--ac2));
  outline:none;
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;
  width:12px;height:12px;
  border-radius:50%;
  background:var(--ac);
  box-shadow:0 0 6px var(--ac);
  cursor:pointer;
  transition:box-shadow .2s;
}
input[type=range]::-webkit-slider-thumb:hover{box-shadow:0 0 12px var(--ac);}
.spd-val{
  font-family:Orbitron,sans-serif;
  color:var(--ac);
  font-size:12px;
  font-weight:700;
  min-width:30px;
}

/* ── INFO BOX ── */
.ibox{
  background:var(--bg3);
  border:1px solid var(--brd);
  border-radius:5px;
  padding:9px 12px;
  font-size:11px;
  color:var(--txt2);
  line-height:2;
  margin-bottom:8px;
}
.ibox strong{color:var(--ac2);}

/* ── AP STATUS ── */
.ap-st{
  display:none;
  background:rgba(0,230,118,.05);
  border:1px solid rgba(0,230,118,.15);
  border-radius:5px;
  padding:7px;
  text-align:center;
  font-size:10px;
  font-family:Orbitron,sans-serif;
  color:var(--ac);
  letter-spacing:1px;
  margin-bottom:8px;
  animation:ap-glow 2s ease-in-out infinite;
}
@keyframes ap-glow{
  0%,100%{box-shadow:none;}
  50%{box-shadow:0 0 15px rgba(0,230,118,.15);}
}

/* ── ЛЮДИ ── */
.hlist{max-height:130px;overflow-y:auto;}
.hi{
  display:flex;align-items:center;gap:8px;
  padding:5px 6px;
  border-radius:4px;
  cursor:pointer;
  transition:background .15s;
  border-bottom:1px solid rgba(0,230,118,.04);
}
.hi:hover{background:rgba(0,230,118,.05);}
.hthumb{
  width:44px;height:33px;
  object-fit:cover;
  border-radius:3px;
  border:1px solid var(--brd);
  background:var(--bg);
}
.hinfo{font-size:10px;color:var(--txt2);}
.hinfo strong{color:var(--ac);display:block;}

/* ── СОНАР ИНФО ── */
.sonar-info{font-size:11px;color:var(--txt2);line-height:2.1;}
.sonar-info strong{color:var(--ac2);}

/* ── СТАТИСТИКА ── */
.stats-grid{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:6px;
  margin-top:8px;
}
.sm{
  background:var(--bg3);
  border-radius:5px;
  border:1px solid var(--brd);
  padding:8px;
  text-align:center;
  transition:all .2s;
}
.sm:hover{border-color:rgba(0,230,118,.15);}
.sm .n{
  font-family:Orbitron,sans-serif;
  font-size:15px;
  color:var(--ac);
  font-weight:700;
}
.sm .l{font-size:9px;color:var(--txt2);margin-top:2px;text-transform:uppercase;letter-spacing:1px;}

/* ── МОТОРЫ ── */
.mtr-cell{
  background:var(--bg2);
  border:1px solid var(--brd);
  border-radius:4px;
  padding:5px 8px;
  font-size:10px;
  font-family:Orbitron,sans-serif;
  color:var(--txt2);
  text-align:center;
  transition:all .15s;
}
.mtr-cell.active{
  background:rgba(0,230,118,.15);
  border-color:rgba(0,230,118,.5);
  color:var(--ac);
  box-shadow:0 0 8px rgba(0,230,118,.2);
}

/* ── ЛОГ ── */
.elog{
  background:var(--bg3);
  border-radius:4px;
  padding:7px 9px;
  font-size:10px;
  max-height:90px;
  overflow-y:auto;
  line-height:1.8;
  font-family:'Share Tech Mono',monospace;
  border:1px solid var(--brd);
}

/* ── POPUP ── */
.overlay{
  display:none;
  position:fixed;inset:0;
  background:rgba(0,0,0,.75);
  z-index:999;
  backdrop-filter:blur(6px);
}
.popup{
  display:none;
  position:fixed;top:50%;left:50%;
  transform:translate(-50%,-50%);
  background:var(--bg2);
  border:1px solid var(--ac);
  border-radius:10px;
  padding:22px;
  z-index:1000;
  min-width:290px;
  box-shadow:0 0 50px rgba(0,230,118,.2),0 20px 60px rgba(0,0,0,.6);
  animation:popup-in .25s ease;
}
@keyframes popup-in{from{opacity:0;transform:translate(-50%,-48%)}to{opacity:1;transform:translate(-50%,-50%)}}
.popup-close{
  position:absolute;top:12px;right:14px;
  cursor:pointer;color:var(--ac);font-size:16px;opacity:.6;
  transition:opacity .2s;
}
.popup-close:hover{opacity:1;}

/* ── SELECT ── */
select{
  width:100%;
  background:var(--bg3);
  color:var(--ac2);
  border:1px solid var(--brd);
  padding:6px 8px;
  border-radius:5px;
  margin-top:6px;
  font-family:inherit;
  font-size:10px;
  outline:none;
  transition:border-color .2s;
}
select:hover,select:focus{border-color:rgba(0,188,212,.3);}

/* ── УВЕДОМЛЕНИЕ НАЙДЕН ── */
.found-notif{
  position:fixed;
  top:60px;left:50%;
  transform:translateX(-50%);
  background:var(--bg2);
  border:2px solid var(--ac3);
  border-radius:10px;
  padding:16px 24px;
  z-index:10000;
  text-align:center;
  min-width:280px;
  box-shadow:0 0 40px rgba(255,82,82,.4),0 10px 40px rgba(0,0,0,.6);
  animation:notif-in .3s ease;
}
@keyframes notif-in{from{opacity:0;top:40px}to{opacity:1;top:60px}}

::-webkit-scrollbar{width:3px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:rgba(0,230,118,.2);border-radius:2px;}

/* ── ТЕМЫ ── */
:root{--theme:steel;}
body.amber{
  --bg:#0f0a04;--bg2:#1a1305;--bg3:#23190a;--bg4:#2e2110;
  --ac:#ef9f27;--ac2:#ba7517;--ac3:#e24b4a;--warn:#ff8a8a;
  --txt:#9c7a3d;--txt2:#6b5020;--brd:rgba(239,159,39,.1);
  --brd2:rgba(186,117,23,.1);--glow:0 0 12px rgba(239,159,39,.15);
}
body.night{
  --bg:#040a05;--bg2:#081005;--bg3:#0d180a;--bg4:#162010;
  --ac:#97c459;--ac2:#639922;--ac3:#e24b4a;--warn:#ef9f27;
  --txt:#5a8c3d;--txt2:#3b6020;--brd:rgba(151,196,89,.1);
  --brd2:rgba(99,153,34,.1);--glow:0 0 12px rgba(151,196,89,.15);
}

/* ── E-STOP ── */
.btn-estop{
  background:rgba(162,45,45,.15);
  color:#ff6b6b;
  border:2px solid rgba(226,75,74,.4);
  font-size:10px;
  font-weight:700;
  letter-spacing:1.5px;
  padding:10px 14px;
  width:100%;
  margin-bottom:6px;
  transition:all .15s;
  position:relative;
  overflow:hidden;
}
.btn-estop:hover{
  background:rgba(162,45,45,.35);
  border-color:rgba(226,75,74,.8);
  box-shadow:0 0 16px rgba(226,75,74,.3);
}
.btn-estop.armed{
  background:rgba(162,45,45,.5);
  border-color:#ff6b6b;
  color:#fff;
  animation:estop-pulse .8s ease-in-out infinite;
}
@keyframes estop-pulse{
  0%,100%{box-shadow:0 0 8px rgba(226,75,74,.4);}
  50%{box-shadow:0 0 24px rgba(226,75,74,.7);}
}

.theme-row{display:flex;gap:4px;margin-bottom:8px;}
.tbtn{
  flex:1;padding:5px;font-size:8px;letter-spacing:.5px;
  background:var(--bg3);border:1px solid var(--brd);color:var(--txt2);
  border-radius:4px;cursor:pointer;transition:all .15s;
  font-family:Orbitron,sans-serif;
}
.tbtn:hover{border-color:rgba(0,230,118,.2);color:var(--txt);}
.tbtn.on{background:rgba(0,230,118,.1);border-color:rgba(0,230,118,.3);color:var(--ac);}

/* ── СВЕТЛАЯ ТЕМА ── */
body.light{
  --bg:#eef2ee;--bg2:#e0e8e2;--bg3:#d4dfd6;--bg4:#c8d8cc;
  --ac:#00796b;--ac2:#0277bd;--ac3:#c62828;--warn:#e65100;
  --txt:#37474f;--txt2:#607d6b;--brd:rgba(0,121,107,.15);
}
body.light #header{background:rgba(220,235,225,.97);}
body.light body::before{background-image:linear-gradient(rgba(0,121,107,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,121,107,.03) 1px,transparent 1px);background-size:40px 40px;}
</style>
</head>
<body>

<!-- HEADER -->
<div id="header">
  <div class="logo-block">
    <div class="logo-icon">🤖</div>
    <div>
      <div class="logo-text">М.А.Р.С.</div>
      <div class="logo-sub">Мобильный Автоматизированный Робот Спасатель</div>
      <div class="logo-badge">v3.0 · Orange Pi H3 · Armbian</div>
    </div>
  </div>

  <!-- ИНФО-ПОЛОСКА -->
  <div id="strip">
    <div class="strip-item">
      <div>
        <div class="strip-lbl">GPIO</div>
        <div class="strip-val dim" id="s_gpio">SIM</div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Батарея</div>
        <div id="batt-wrap">
          <div id="batt-bar"><div id="batt-fill"></div></div>
          <div class="strip-val" id="s_batt">100%</div>
        </div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Позиция</div>
        <div class="strip-val" id="s_pos">—</div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Угол</div>
        <div class="strip-val" id="s_angle">—</div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Скорость</div>
        <div class="strip-val" id="s_speed">—</div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Команда</div>
        <div class="strip-val" id="s_cmd">СТОП</div>
      </div>
    </div>
    <div class="strip-item" id="s_auto_wrap" style="display:none;">
      <div class="status-pill" style="color:var(--ac);border-color:rgba(0,230,118,.3);background:rgba(0,230,118,.08);">
        🤖 <span id="s_auto_mode">ПОИСК</span>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Сонар</div>
        <div class="strip-val" id="s_sonar">—</div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Найдено</div>
        <div class="strip-val" id="s_found">0</div>
      </div>
    </div>
    <div class="strip-item" style="margin-left:auto;">
      <div>
        <div class="strip-lbl">FPS</div>
        <div class="strip-val dim" id="s_fps">—</div>
      </div>
    </div>
    <div class="strip-item">
      <div>
        <div class="strip-lbl">Время</div>
        <div class="strip-val dim" id="s_time">0:00</div>
      </div>
    </div>
    <div style="padding:4px 10px;">
      <button onclick="toggleTheme()" style="padding:4px 10px;font-size:8px;color:var(--txt2);border-color:var(--brd);">🌙</button>
    </div>
  </div>
</div>

<div class="wrap">

  <!-- ЛЕВАЯ КОЛОНКА -->
  <div style="display:flex;flex-direction:column;gap:10px;">

    <!-- СТАТУС-СЕТКА -->
    <div class="sg">
      <div class="sc"><div class="sl">GPIO</div><div class="sv" id="gpioStat">SIM</div></div>
      <div class="sc"><div class="sl">Камера</div><div class="sv" id="fpsStat">—</div></div>
      <div class="sc"><div class="sl">Лиц</div><div class="sv" id="facesStat">0</div></div>
      <div class="sc"><div class="sl">Найдено</div><div class="sv a" id="foundStat">0</div></div>
      <div class="sc"><div class="sl">Покрытие</div><div class="sv" id="coverStat">—</div></div>
      <div class="sc"><div class="sl">Путь</div><div class="sv" id="distStat">—</div></div>
    </div>

    <!-- КАРТА -->
    <div class="p">
      <div class="p-title">
        <span class="dot"></span>
        Навигационная карта
        <span style="margin-left:auto;font-size:9px;color:var(--txt2);" id="mapStatus">Загрузка...</span>
      </div>
      <canvas id="mapCanvas" width="800" height="600"></canvas>
    </div>

  </div>

  <!-- ПРАВАЯ КОЛОНКА -->
  <div style="display:flex;flex-direction:column;gap:10px;">

    <!-- КАМЕРА -->
    <div class="p">
      <div class="p-title">
        <span class="dot"></span>
        Live камера
      </div>
      <img id="videoImg" src="" alt="">
      <div class="cam-st">
        <div class="cam-dot" id="camDot"></div>
        <span id="camSt">Инициализация...</span>
      </div>
      <select id="camSel" onchange="changeCamera(this.value)">
        <option value="">📷 Выбрать камеру...</option>
      </select>
    </div>

    <!-- СОНАР -->
    <div class="p">
      <div class="p-title">
        <span class="dot"></span>
        Радар — HY-SRF05
        <span id="sonarMode" style="margin-left:auto;font-size:9px;padding:1px 6px;border-radius:8px;background:rgba(255,82,82,.1);color:var(--ac3);border:1px solid rgba(255,82,82,.2);">ВЫКЛ</span>
      </div>
      <div style="display:flex;gap:12px;align-items:center;">
        <canvas id="radarCanvas" width="170" height="170" style="flex-shrink:0;"></canvas>
        <div style="flex:1;min-width:0;">
          <div id="sonarWarn" style="font-family:Orbitron,sans-serif;font-size:12px;font-weight:700;color:var(--txt2);margin-bottom:8px;letter-spacing:1px;">📡 ВЫКЛЮЧЕН</div>
          <div class="sonar-info">
            <strong>Дистанция:</strong> <span id="sonarDist">—</span><br>
            <strong>Угол:</strong> <span id="sonarSweep">0°</span><br>
            <strong>Объектов:</strong> <span id="sonarObjCount">0</span><br>
            <strong>Статус:</strong> <span id="sonarObs">—</span>
          </div>
          <div style="display:flex;gap:5px;margin-top:10px;">
            <button id="btnSonarOn" onclick="setSonarMode('on')" style="flex:1;padding:6px;font-size:8px;">▶ ВКЛ</button>
            <button id="btnSonarOff" onclick="setSonarMode('off')" class="btn-danger" style="flex:1;padding:6px;font-size:8px;">■ ВЫКЛ</button>
            <button onclick="sonarDiagnostic()" class="btn-info" style="flex:0;padding:6px 8px;font-size:9px;" title="Диагностика">🔍</button>
          </div>
          <div id="sonarDiagResult" style="display:none;margin-top:6px;font-size:10px;background:var(--bg3);border-radius:4px;padding:7px;color:var(--txt2);line-height:1.9;border:1px solid var(--brd);"></div>
        </div>
      </div>
    </div>

    <!-- УПРАВЛЕНИЕ -->
    <div class="p">
      <div class="p-title">
        <span class="dot"></span>
        Управление
        <span style="margin-left:6px;font-size:9px;color:var(--txt2);">WASD · SPC · Q · P</span>
      </div>

      <div class="wasd">
        <div></div>
        <div class="wk" id="kW">W</div>
        <div></div>
        <div class="wk" id="kA">A</div>
        <div class="wk stop" id="kSP">SPC</div>
        <div class="wk" id="kD">D</div>
        <div></div>
        <div class="wk" id="kS">S</div>
        <div></div>
      </div>

      <div class="btn-row">
        <button onmousedown="cmd('forward')" onmouseup="cmd('stop')" ontouchstart="cmd('forward')" ontouchend="cmd('stop')">↑ Вперёд</button>
        <button onmousedown="cmd('backward')" onmouseup="cmd('stop')" ontouchstart="cmd('backward')" ontouchend="cmd('stop')">↓ Назад</button>
      </div>
      <div class="btn-row">
        <button onmousedown="cmd('left')" onmouseup="cmd('stop')" ontouchstart="cmd('left')" ontouchend="cmd('stop')">← Влево</button>
        <button class="btn-danger" onclick="cmd('stop')">■ Стоп</button>
        <button onmousedown="cmd('right')" onmouseup="cmd('stop')" ontouchstart="cmd('right')" ontouchend="cmd('stop')">Вправо →</button>
      </div>

      <div class="speed-row">
        <span class="lbl">⚡ Скорость</span>
        <input type="range" id="speedSlider" min="0" max="255" value="150" oninput="setSpeed(this.value)">
        <span class="spd-val" id="speedVal">150</span>
      </div>

      <!-- ПЕРЕКЛЮЧАТЕЛЬ ТЕМ -->
      <div class="theme-row">
        <button class="tbtn on" id="th-steel" onclick="setTheme('')">СТАЛЬ</button>
        <button class="tbtn" id="th-amber" onclick="setTheme('amber')">ЯНТАРЬ</button>
        <button class="tbtn" id="th-night" onclick="setTheme('night')">НОЧЬ</button>
      </div>

      <!-- E-STOP -->
      <button class="btn-estop" id="estopBtn" onclick="toggleEstop()">
        ⬛ АВАРИЙНЫЙ СТОП
      </button>

      <div class="btn-row">
        <button id="apBtn" onclick="toggleAutopilot()">🤖 Автопоиск</button>
        <button onclick="startMission()" class="btn-info" id="missionBtn">🎯 Миссия</button>
        <button onclick="takeScreenshot()" class="btn-info">📸</button>
        <button onclick="exportMap()" class="btn-info">🗺</button>
        <button onclick="openReport()">📄</button>
        <button onclick="clearPath()" style="flex:0;padding:9px 10px;color:var(--txt2);">🗑</button>
      </div>

      <div class="ap-st" id="apStatus">🤖 Автопилот активен — <span id="apMode">ПОИСК</span></div>

      <!-- РЕЖИМ РОБОТА + СИМУЛЯЦИЯ МОТОРОВ -->
      <div style="display:flex;gap:6px;margin-bottom:8px;align-items:center;">
        <span style="font-size:9px;color:var(--txt2);letter-spacing:1px;">Движ:</span>
        <button id="btnModeSim" onclick="setRobotMode('sim')"
                class="btn-active" style="flex:1;padding:6px;font-size:8px;">
            🔵 Карта
        </button>
        <button id="btnModeReal" onclick="setRobotMode('real')"
                style="flex:1;padding:6px;font-size:8px;">
            🟢 Реальный
        </button>
      </div>
      <div style="display:flex;gap:6px;margin-bottom:8px;align-items:center;">
        <span style="font-size:9px;color:var(--txt2);letter-spacing:1px;">Мотор:</span>
        <button id="btnMotorSim" onclick="setMotorSim(true)"
                class="btn-active" style="flex:1;padding:6px;font-size:8px;">
            ⚫ СИМ
        </button>
        <button id="btnMotorReal" onclick="setMotorSim(false)"
                style="flex:1;padding:6px;font-size:8px;">
            ⚡ GPIO
        </button>
      </div>

      <!-- СТАТУС МОТОРОВ -->
      <div id="motorsWidget" style="background:var(--bg3);border:1px solid var(--brd);
           border-radius:6px;padding:8px;margin-bottom:8px;">
        <div style="font-size:9px;color:var(--txt2);text-transform:uppercase;
                    letter-spacing:1px;margin-bottom:6px;">⚙ Состояние моторов</div>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:5px;">
          <div id="mtr-forward"  class="mtr-cell">↑ Вперёд</div>
          <div id="mtr-backward" class="mtr-cell">↓ Назад</div>
          <div id="mtr-left"     class="mtr-cell">← Влево</div>
          <div id="mtr-right"    class="mtr-cell">Вправо →</div>
        </div>
        <div style="margin-top:6px;font-size:10px;color:var(--txt2);">
          GPIO: <span id="mtr-gpio" style="color:var(--txt2);">—</span>
        </div>
      </div>

      <div class="ibox" id="infoBox">Загрузка данных...</div>
    </div>

    <!-- НАЙДЕННЫЕ ЛЮДИ -->
    <div class="p">
      <div class="p-title">
        <span class="dot"></span>
        Обнаруженные люди
        <span style="margin-left:auto;font-family:Orbitron,sans-serif;color:var(--ac3);font-size:14px;font-weight:700;" id="humanCount">0</span>
      </div>
      <div class="hlist" id="humansList">
        <div style="color:var(--txt2);padding:10px;text-align:center;font-size:11px;">Людей не обнаружено</div>
      </div>
    </div>

    <!-- СТАТИСТИКА -->
    <div class="p">
      <div class="p-title"><span class="dot"></span>Статистика сессии</div>
      <div class="stats-grid">
        <div class="sm"><div class="n" id="st-uptime">—</div><div class="l">Время</div></div>
        <div class="sm"><div class="n" id="st-dist">—</div><div class="l">Путь</div></div>
        <div class="sm"><div class="n" id="st-cover">—</div><div class="l">Покрытие</div></div>
        <div class="sm"><div class="n" id="st-humans" style="color:var(--ac3);">0</div><div class="l">Найдено</div></div>
        <div class="sm"><div class="n" id="st-path">—</div><div class="l">Точек</div></div>
        <div class="sm"><div class="n" id="st-session" style="font-size:9px;">—</div><div class="l">Сессия</div></div>
      </div>
      <div style="margin-top:8px;">
        <div style="font-size:9px;color:var(--txt2);margin-bottom:4px;display:flex;align-items:center;gap:6px;">
          <span>📋 Журнал событий</span>
          <span id="logDir" style="opacity:.4;"></span>
        </div>
        <div class="elog" id="eventLog">Ожидание событий...</div>
      </div>
    </div>

  </div>
</div>

<!-- POPUP -->
<div class="overlay" id="popupOverlay" onclick="closePopup()"></div>
<div class="popup" id="humanPopup">
  <span class="popup-close" onclick="closePopup()">✕</span>
  <div id="popupContent"></div>
</div>

<script>
const mapCanvas=document.getElementById('mapCanvas');
const mapCtx=mapCanvas.getContext('2d');
const videoImg=document.getElementById('videoImg');

let lastHumans=[],mapHumans=[],frameCount=0;
let fpsTimer=performance.now(),lastFoundCount=0;
let notifAudio=null,autopilotOn=false,lastSonarData={};

// ── ВИДЕО ──────────────────────────────────────────────────────────────────
let videoActive=false;
function videoLoop(){
    if(videoActive) return;
    videoActive=true;
    const img=new Image();
    img.onload=()=>{
        videoImg.src=img.src;
        const dot=document.getElementById('camDot');
        const st=document.getElementById('camSt');
        if(dot){dot.className='cam-dot live';}
        if(st) st.textContent='● LIVE';
        frameCount++;
        const now=performance.now();
        if(now-fpsTimer>=1000){
            const fps=(frameCount*1000/(now-fpsTimer)).toFixed(0);
            _set('fpsStat',fps+' fps'); _set('s_fps',fps);
            frameCount=0;fpsTimer=now;
        }
        videoActive=false;
        setTimeout(videoLoop,160);
    };
    img.onerror=()=>{
        const dot=document.getElementById('camDot');
        const st=document.getElementById('camSt');
        if(dot) dot.className='cam-dot';
        if(st) st.textContent='○ Нет сигнала';
        videoActive=false;
        setTimeout(videoLoop,700);
    };
    img.src='/api/camera/frame?t='+Date.now();
}

// ── КАРТА ──────────────────────────────────────────────────────────────────
let mapLoading=false;
function mapLoop(){
    const img=new Image();
    const ms=document.getElementById('mapStatus');
    img.onload=()=>{
        mapCtx.drawImage(img,0,0,mapCanvas.width,mapCanvas.height);
        URL.revokeObjectURL(img.src);
        if(ms) ms.textContent='Обновлено '+new Date().toLocaleTimeString();
    };
    fetch('/api/map').then(r=>r.blob()).then(b=>{img.src=URL.createObjectURL(b);}).catch(()=>{});
    setTimeout(mapLoop,500);
}

// ── РАДАР ──────────────────────────────────────────────────────────────────
let radarCanvas,radarCtx;
function initRadar(){
    radarCanvas=document.getElementById('radarCanvas');
    if(!radarCanvas) return;
    radarCtx=radarCanvas.getContext('2d');
    radarLoop();
}

function radarLoop(){
    if(Object.keys(lastSonarData).length>0) drawRadar(lastSonarData);
    setTimeout(radarLoop,120);
}

function drawRadar(d){
    if(!radarCtx) return;
    const W=radarCanvas.width,H=radarCanvas.height,cx=W/2,cy=H/2,R=W/2-8;

    radarCtx.fillStyle='#020a05';
    radarCtx.fillRect(0,0,W,H);
    radarCtx.save();
    radarCtx.beginPath();radarCtx.arc(cx,cy,R,0,Math.PI*2);radarCtx.clip();

    // Фон
    const bg=radarCtx.createRadialGradient(cx,cy,0,cx,cy,R);
    bg.addColorStop(0,'rgba(0,60,25,.9)');bg.addColorStop(1,'rgba(0,12,5,.98)');
    radarCtx.fillStyle=bg;radarCtx.fillRect(0,0,W,H);

    // Кольца
    [1,2,3,4].forEach(i=>{
        radarCtx.strokeStyle=i===4?'rgba(0,230,118,.45)':'rgba(0,230,118,.1)';
        radarCtx.lineWidth=i===4?1.5:.7;
        radarCtx.beginPath();radarCtx.arc(cx,cy,R*i/4,0,Math.PI*2);radarCtx.stroke();
    });

    // Линии
    radarCtx.strokeStyle='rgba(0,230,118,.08)';radarCtx.lineWidth=.7;
    for(let a=0;a<360;a+=45){
        const ar=a*Math.PI/180;
        radarCtx.beginPath();radarCtx.moveTo(cx,cy);
        radarCtx.lineTo(cx+Math.sin(ar)*R,cy-Math.cos(ar)*R);radarCtx.stroke();
    }

    // Подписи
    radarCtx.fillStyle='rgba(0,230,118,.4)';radarCtx.font='bold 8px monospace';
    ['75','150','225','300'].forEach((l,i)=>radarCtx.fillText(l,cx+3,cy-R*(i+1)/4+4));

    // Вращающийся луч
    const sw=(d.sweep_angle||0)*Math.PI/180;
    // Хвост
    for(let i=25;i>=0;i--){
        const tr=sw-i*.065;
        radarCtx.strokeStyle=`rgba(0,230,118,${((25-i)/25)*.3})`;
        radarCtx.lineWidth=2;
        radarCtx.beginPath();radarCtx.moveTo(cx,cy);
        radarCtx.lineTo(cx+Math.sin(tr)*R,cy-Math.cos(tr)*R);radarCtx.stroke();
    }
    // Луч
    radarCtx.shadowColor='#00e676';radarCtx.shadowBlur=8;
    radarCtx.strokeStyle='rgba(0,230,118,1)';radarCtx.lineWidth=2;
    radarCtx.beginPath();radarCtx.moveTo(cx,cy);
    radarCtx.lineTo(cx+Math.sin(sw)*R,cy-Math.cos(sw)*R);radarCtx.stroke();
    radarCtx.shadowBlur=0;

    // Объекты
    let objCount=0;
    const nowS=Date.now()/1000;
    for(const pt of (d.radar_points||[])){
        if(pt.dist>=270) continue;
        const ra=(pt.rel_angle||0)*Math.PI/180;
        const dr=(pt.dist/300)*R;
        const px=cx+Math.sin(ra)*dr,py=cy-Math.cos(ra)*dr;
        const age=pt.ts?Math.max(0,1-(nowS-pt.ts)/8):.5;
        if(age<.05) continue;
        objCount++;
        const cl=pt.dist<40;
        if(cl){radarCtx.shadowColor='#ff5252';radarCtx.shadowBlur=6;}
        radarCtx.fillStyle=cl?`rgba(255,82,82,${age*.85})`:`rgba(0,230,118,${age*.6})`;
        radarCtx.beginPath();radarCtx.arc(px,py,cl?4:2.5,0,Math.PI*2);radarCtx.fill();
        radarCtx.shadowBlur=0;
    }

    // Текущее препятствие
    const dist=Math.min(d.distance_cm||999,290);
    if(dist<290){
        const lx=cx+Math.sin(sw)*(dist/300)*R,ly=cy-Math.cos(sw)*(dist/300)*R;
        const cl=d.obstacle,pulse=(Math.sin(Date.now()/200)+1)/2;
        radarCtx.strokeStyle=cl?`rgba(255,82,82,${.5+pulse*.4})`:`rgba(255,171,64,${.4+pulse*.3})`;
        radarCtx.lineWidth=1.5;
        radarCtx.beginPath();radarCtx.arc(lx,ly,8+pulse*5,0,Math.PI*2);radarCtx.stroke();
        radarCtx.shadowColor=cl?'#ff5252':'#ffab40';radarCtx.shadowBlur=12;
        radarCtx.fillStyle=cl?'#ff5252':'#ffab40';
        radarCtx.beginPath();radarCtx.arc(lx,ly,cl?6:4,0,Math.PI*2);radarCtx.fill();
        radarCtx.shadowBlur=0;
        radarCtx.fillStyle='rgba(255,255,255,.85)';radarCtx.font='bold 9px monospace';
        radarCtx.fillText(dist.toFixed(0)+'см',lx+8,ly-3);
    }

    radarCtx.restore();

    // Центр
    radarCtx.shadowColor='#00e676';radarCtx.shadowBlur=10;
    radarCtx.fillStyle='#00e676';
    radarCtx.beginPath();radarCtx.arc(cx,cy,4,0,Math.PI*2);radarCtx.fill();
    radarCtx.shadowBlur=0;

    // Рамка
    radarCtx.strokeStyle='rgba(0,230,118,.5)';radarCtx.lineWidth=1.5;
    radarCtx.beginPath();radarCtx.arc(cx,cy,R,0,Math.PI*2);radarCtx.stroke();

    // Метки
    radarCtx.fillStyle='rgba(0,230,118,.5)';radarCtx.font='bold 8px monospace';
    radarCtx.fillText('С',cx-4,10);radarCtx.fillText('В',W-12,cy+4);
    radarCtx.fillText('Ю',cx-4,H-3);radarCtx.fillText('З',3,cy+4);

    radarCtx.fillStyle=d.active?'rgba(0,230,118,.7)':'rgba(255,82,82,.6)';
    radarCtx.font='bold 7px monospace';
    radarCtx.fillText(d.active?'● АКТИВЕН':'● ВЫКЛ',5,H-5);

    _set('sonarObjCount',objCount);
    _set('sonarSweep',(d.sweep_angle||0).toFixed(0)+'°');
    const badge=document.getElementById('sonarMode');
    if(badge){
        if(d.active){badge.textContent='ВКЛ';badge.style.color='var(--ac)';badge.style.background='rgba(0,230,118,.1)';badge.style.borderColor='rgba(0,230,118,.2)';}
        else{badge.textContent='ВЫКЛ';badge.style.color='var(--ac3)';badge.style.background='rgba(255,82,82,.1)';badge.style.borderColor='rgba(255,82,82,.2)';}
    }
}

// ── СОСТОЯНИЕ ─────────────────────────────────────────────────────────────
async function stateLoop(){
    try{
        const r=await fetch('/api/robot/state');
        const d=await r.json();
        const s=d.state;
        const found=s.found_humans?s.found_humans.length:0;

        // Сонар
        lastSonarData={
            distance_cm:s.sonar_dist||999,obstacle:s.sonar_obstacle||false,
            enabled:s.sonar_enabled||false,active:s.sonar_active||false,
            sweep_angle:s.sweep_angle||0,radar_points:s.radar_points||[],
            status:s.sonar_status||'ВЫКЛ',
        };

        const dist=lastSonarData.distance_cm;
        _set('sonarDist',dist>300?'>300 см':dist.toFixed(0)+' см');
        const obsEl=document.getElementById('sonarObs');
        if(obsEl){obsEl.textContent=lastSonarData.obstacle?'⚠ ПРЕПЯТСТВИЕ!':'Свободно';obsEl.style.color=lastSonarData.obstacle?'var(--ac3)':'var(--ac)';}
        const warnEl=document.getElementById('sonarWarn');
        if(warnEl){
            if(!lastSonarData.active){warnEl.textContent='📡 ВЫКЛЮЧЕН';warnEl.style.color='var(--txt2)';}
            else if(lastSonarData.obstacle){warnEl.textContent=`⚠ СТОП! ${dist.toFixed(0)} см`;warnEl.style.color='var(--ac3)';}
            else{warnEl.textContent=`📡 ${dist>300?'Чисто':dist.toFixed(0)+' см'}`;warnEl.style.color='var(--ac)';}
        }

        // Полоска
        _setv('s_gpio',s.gpio_enabled?'🟢 GPIO':'SIM',s.gpio_enabled?'':'dim');
        _set('s_batt',s.battery+'%');
        const bf=document.getElementById('batt-fill');
        if(bf){bf.style.width=s.battery+'%';bf.style.background=s.battery>30?'linear-gradient(90deg,var(--ac),#69f0ae)':s.battery>10?'linear-gradient(90deg,var(--warn),#ffcc02)':'linear-gradient(90deg,var(--ac3),#ff867c)';}
        _set('s_pos',`(${s.x.toFixed(0)},${s.y.toFixed(0)})`);
        _set('s_angle',s.angle.toFixed(0)+'°');
        _set('s_speed',s.current_speed+'/255');
        const cRu={'FORWARD':'ВПЕРЁД','BACKWARD':'НАЗАД','LEFT':'ВЛЕВО','RIGHT':'ВПРАВО','STOP':'СТОП'};
        const cCl={'FORWARD':'ac','BACKWARD':'warn','LEFT':'','RIGHT':'','STOP':'dim'};
        _setv('s_cmd',cRu[s.current_command]||s.current_command,cCl[s.current_command]||'');
        _set('s_sonar',dist>300?'—':dist.toFixed(0)+' см');
        _setv('s_found',found,found>0?'alert':'dim');
        _set('s_time',fmtTime(s.uptime));

        // Автопилот
        const aw=document.getElementById('s_auto_wrap');
        if(aw) aw.style.display=s.autopilot&&s.autopilot.enabled?'flex':'none';
        if(s.autopilot&&s.autopilot.enabled) _set('s_auto_mode',s.autopilot.mode);

        // Статус-бар
        _setv('gpioStat',s.gpio_enabled?'ON':'SIM',s.gpio_enabled?'':'w');
        _set('facesStat',s.face_count||0);
        _setv('foundStat',found,found>0?'a':'');
        _setv('s_sonar',dist>300?'—':dist.toFixed(0)+' см','');

        // Автопилот статус
        const apSt=document.getElementById('apStatus');
        if(s.autopilot&&s.autopilot.enabled){
            _set('apMode',s.autopilot.mode);
            if(apSt) apSt.style.display='block';
        } else if(!autopilotOn&&apSt){
            apSt.style.display='none';
        }

        // Info
        const ib=document.getElementById('infoBox');
        if(ib) ib.innerHTML=
            `<strong>Позиция:</strong> X=${s.x.toFixed(0)}, Y=${s.y.toFixed(0)}<br>`+
            `<strong>Угол:</strong> ${s.angle.toFixed(0)}° &nbsp; <strong>Скорость:</strong> ${s.current_speed}/255<br>`+
            `<strong>Команда:</strong> ${cRu[s.current_command]||s.current_command} &nbsp; <strong>Путь:</strong> ${s.path_length} точек`;

        // Люди
        if(s.found_humans&&s.found_humans.length!==lastHumans.length){
            lastHumans=s.found_humans;
            updateHumansList(s.found_humans);
            mapHumans=s.found_humans;
        }
        _set('humanCount',found);
        checkNewHumans(s);
        updateMotors(s);

    }catch(e){}
    setTimeout(stateLoop,700);
}

// ── СТАТИСТИКА ─────────────────────────────────────────────────────────────
async function statsLoop(){
    try{
        const r=await fetch('/api/stats');
        const d=await r.json();
        _set('st-uptime',d.uptime_str||'—'); _set('st-dist',(d.dist_m||0)+'м');
        _set('st-cover',(d.coverage_pct||0)+'%'); _set('st-humans',d.humans_found||'0');
        _set('st-path',d.path_points||'—'); _set('st-session',d.session_id||'—');
        _set('coverStat',(d.coverage_pct||0)+'%'); _set('distStat',(d.dist_m||0)+'м');
        const ld=document.getElementById('logDir');
        if(ld&&d.session_id) ld.textContent=d.session_id;
    }catch(e){}
    setTimeout(statsLoop,2500);
}

// ── ЛОГ ────────────────────────────────────────────────────────────────────
async function logLoop(){
    try{
        const r=await fetch('/api/log/events');
        const d=await r.json();
        const el=document.getElementById('eventLog');
        if(el&&d.events&&d.events.length>0){
            const clrs={
                'SESSION_START':'var(--ac)','АВТОПИЛОТ_СТАРТ':'var(--ac2)',
                'АВТОПИЛОТ_СТОП':'var(--warn)','ЧЕЛОВЕК_НАЙДЕН':'var(--ac3)',
                'ПРЕПЯТСТВИЕ':'var(--warn)','СОНАР_ВКЛ':'var(--ac)',
                'СОНАР_ВЫКЛ':'var(--txt2)','РАЗВОРОТ':'var(--ac2)',
            };
            el.innerHTML=d.events.slice().reverse().map(e=>
                `<span style="color:${clrs[e.type]||'var(--txt2)'};">[${e.time}] ${e.type}</span>`+
                (e.details?` <span style="opacity:.55;">${e.details}</span>`:'')
            ).join('<br>');
        }
    }catch(e){}
    setTimeout(logLoop,2000);
}

// ── УПРАВЛЕНИЕ ─────────────────────────────────────────────────────────────
async function cmd(c){await fetch('/api/robot/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})});}
async function setSpeed(v){_set('speedVal',v);await fetch('/api/robot/speed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({speed:parseInt(v)})});}
async function clearPath(){await fetch('/api/robot/clear_path',{method:'POST'});}
function exportMap(){window.open('/api/map/export','_blank');}
function openReport(){window.open('/api/report','_blank');}

// ── WASD ────────────────────────────────────────────────────────────────────
const keysDown=new Set();
document.addEventListener('keydown',e=>{
    if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT') return;
    if(keysDown.has(e.code)) return;
    keysDown.add(e.code);
    const m={'KeyW':'forward','KeyS':'backward','KeyA':'left','KeyD':'right',
              'ArrowUp':'forward','ArrowDown':'backward','ArrowLeft':'left','ArrowRight':'right'};
    if(m[e.code]){cmd(m[e.code]);e.preventDefault();}
    if(e.code==='Space'){cmd('stop');e.preventDefault();}
    if(e.code==='KeyQ') toggleAutopilot();
    if(e.code==='KeyP') takeScreenshot();
    updateWASD();
});
document.addEventListener('keyup',e=>{
    keysDown.delete(e.code);
    const move=['KeyW','KeyS','KeyA','KeyD','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'];
    if(move.includes(e.code)) cmd('stop');
    updateWASD();
});
function updateWASD(){
    const m={'KeyW':'kW','ArrowUp':'kW','KeyS':'kS','ArrowDown':'kS',
             'KeyA':'kA','ArrowLeft':'kA','KeyD':'kD','ArrowRight':'kD','Space':'kSP'};
    for(const[code,id] of Object.entries(m)){
        const el=document.getElementById(id);
        if(el){if(keysDown.has(code)) el.classList.add('pressed');else el.classList.remove('pressed');}
    }
}

// ── АВТОПИЛОТ ───────────────────────────────────────────────────────────────
async function toggleAutopilot(){
    const btn=document.getElementById('apBtn');
    if(!autopilotOn){
        const r=await fetch('/api/autopilot/start',{method:'POST'});
        const d=await r.json();
        if(d.success){
            autopilotOn=true;
            if(btn){btn.textContent='⏹ Остановить';btn.classList.add('btn-active');}
            const apSt=document.getElementById('apStatus');
            if(apSt) apSt.style.display='block';
        }
    } else {
        await fetch('/api/autopilot/stop',{method:'POST'});
        autopilotOn=false;
        const btn2=document.getElementById('apBtn');
        if(btn2){btn2.textContent='🤖 Автопоиск';btn2.classList.remove('btn-active');}
        const apSt=document.getElementById('apStatus');
        if(apSt) apSt.style.display='none';
    }
}
async function takeScreenshot(){await fetch('/api/log/screenshot',{method:'POST'});}

// ── СОНАР ───────────────────────────────────────────────────────────────────
async function setSonarMode(mode){
    const r=await fetch('/api/sonar/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    const d=await r.json();
    const btnOn=document.getElementById('btnSonarOn');
    if(mode==='on'&&d.success){
        if(btnOn){btnOn.classList.add('btn-active');}
    } else if(mode==='off'){
        if(btnOn) btnOn.classList.remove('btn-active');
    } else if(!d.success){
        alert(`Ошибка: ${d.message||'Неизвестная ошибка'}\n\nНажми 🔍 для диагностики`);
    }
}
async function sonarDiagnostic(){
    const el=document.getElementById('sonarDiagResult');
    if(!el) return;
    el.style.display='block';el.textContent='🔍 Диагностика...';
    try{
        const r=await fetch('/api/sonar/diagnostic');
        const d=await r.json();
        const sc={'OK':'var(--ac)','ВЫКЛ':'var(--txt2)','ОШИБКА':'var(--ac3)','НЕТ_GPIO':'var(--ac3)','ТАЙМАУТ':'var(--warn)'}[d.status]||'var(--txt2)';
        el.innerHTML=
            `<div style="color:${sc};font-weight:bold;margin-bottom:4px;">${d.verdict}</div>`+
            `GPIO: ${d.gpio_available?'✅':'❌'} &nbsp; Init: ${d.gpio_initialized?'✅':'❌'} &nbsp; Активен: ${d.active?'✅':'❌'}<br>`+
            `TRIG = Pin ${d.pin_trig} &nbsp; ECHO = Pin ${d.pin_echo}<br>`+
            `Ошибок: ${d.error_count||0}`+
            (d.last_ok_ago!==null?` &nbsp; OK: ${d.last_ok_ago}с назад`:'')+
            (d.error_msg?`<br><span style="color:var(--warn);">⚠ ${d.error_msg}</span>`:'');
    }catch(e){el.textContent='❌ Ошибка запроса';}
}

// ── КАМЕРЫ ──────────────────────────────────────────────────────────────────
async function loadCameras(){
    try{
        const r=await fetch('/api/cameras/list');
        const d=await r.json();
        const sel=document.getElementById('camSel');
        for(const c of d.devices){
            const o=document.createElement('option');
            o.value=c.device;o.textContent=c.name+' ('+c.device+')';
            sel.appendChild(o);
        }
    }catch(e){}
}
async function changeCamera(dev){
    if(!dev) return;
    await fetch('/api/cameras/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:dev})});
}

// ── КАРТА — КЛИК ────────────────────────────────────────────────────────────
mapCanvas.addEventListener('click',e=>{
    const rect=mapCanvas.getBoundingClientRect();
    const sx=mapCanvas.width/rect.width,sy=mapCanvas.height/rect.height;
    const cx=(e.clientX-rect.left)*sx,cy=(e.clientY-rect.top)*sy;
    for(const h of mapHumans){
        if(Math.sqrt((h.x-cx)**2+(h.y-cy)**2)<18){showHuman(h.id);return;}
    }
});

function showHuman(id){
    const h=mapHumans.find(x=>x.id===id);if(!h) return;
    document.getElementById('popupContent').innerHTML=
        `<h3 style="font-family:Orbitron,sans-serif;color:var(--ac);margin-bottom:14px;font-size:13px;letter-spacing:2px;">ЧЕЛОВЕК #${id}</h3>`+
        `<img src="/api/humans/photo/${id}" onerror="this.remove()" style="width:100%;border-radius:6px;margin-bottom:12px;border:1px solid var(--brd);">`+
        `<div style="font-size:11px;color:var(--txt2);line-height:2;">`+
        `<strong style="color:var(--ac2)">Обнаружен:</strong> ${new Date(h.timestamp*1000).toLocaleTimeString()}<br>`+
        `<strong style="color:var(--ac2)">Позиция:</strong> X=${Math.round(h.x)}, Y=${Math.round(h.y)}</div>`;
    document.getElementById('popupOverlay').style.display='block';
    document.getElementById('humanPopup').style.display='block';
}
function closePopup(){
    document.getElementById('popupOverlay').style.display='none';
    document.getElementById('humanPopup').style.display='none';
}

function updateHumansList(humans){
    const list=document.getElementById('humansList');
    if(!humans.length){list.innerHTML='<div style="color:var(--txt2);padding:10px;text-align:center;font-size:11px;">Людей не обнаружено</div>';return;}
    list.innerHTML=humans.map(h=>`
        <div class="hi" onclick="showHuman(${h.id})">
            <img class="hthumb" src="/api/humans/photo/${h.id}" onerror="this.style.display='none'">
            <div class="hinfo">
                <strong>#${h.id} — Обнаружен</strong>
                (${Math.round(h.x)}, ${Math.round(h.y)}) · ${new Date(h.timestamp*1000).toLocaleTimeString()}
            </div>
        </div>`).join('');
}

// ── УВЕДОМЛЕНИЯ ─────────────────────────────────────────────────────────────
function initNotifications(){try{notifAudio=new(window.AudioContext||window.webkitAudioContext)();}catch(e){}}
function playAlert(){
    if(!notifAudio) return;
    [0,300,600].forEach(delay=>setTimeout(()=>{
        try{
            const o=notifAudio.createOscillator(),g=notifAudio.createGain();
            o.connect(g);g.connect(notifAudio.destination);
            o.frequency.value=880;o.type='square';
            g.gain.setValueAtTime(.3,notifAudio.currentTime);
            g.gain.exponentialRampToValueAtTime(.001,notifAudio.currentTime+.2);
            o.start(notifAudio.currentTime);o.stop(notifAudio.currentTime+.2);
        }catch(e){}
    },delay));
}

function showFoundAlert(count){
    // Вспышка
    const f=document.createElement('div');
    f.style.cssText='position:fixed;inset:0;z-index:9998;pointer-events:none;background:rgba(255,82,82,.2);animation:fadeFlash .8s forwards;';
    document.body.appendChild(f);
    setTimeout(()=>f.remove(),900);

    // Уведомление
    const n=document.createElement('div');
    n.className='found-notif';
    n.innerHTML=
        `<div style="font-size:30px;margin-bottom:8px;">🚨</div>`+
        `<div style="font-family:Orbitron,sans-serif;color:var(--ac3);font-size:15px;font-weight:900;letter-spacing:3px;margin-bottom:6px;">ЧЕЛОВЕК НАЙДЕН!</div>`+
        `<div style="color:var(--txt2);font-size:11px;">Обнаружено: ${count} чел. — робот останавливается</div>`+
        `<div style="color:var(--txt2);font-size:10px;margin-top:6px;opacity:.6;">нажми для закрытия</div>`;
    n.onclick=()=>n.remove();
    document.body.appendChild(n);
    setTimeout(()=>n&&n.remove(),6000);
    playAlert();

    let b=0;const orig=document.title;
    const t=setInterval(()=>{
        document.title=b++%2===0?'🚨 НАЙДЕН!':'М.А.Р.С.';
        if(b>12){clearInterval(t);document.title=orig;}
    },400);
}
function checkNewHumans(s){
    const f=s.found_humans?s.found_humans.length:0;
    if(f>lastFoundCount){showFoundAlert(f);lastFoundCount=f;}
}

// ── ТЕМА ────────────────────────────────────────────────────────────────────
function toggleTheme(){
    document.body.classList.toggle('light');
    localStorage.setItem('theme',document.body.classList.contains('light')?'light':'dark');
}
if(localStorage.getItem('theme')==='light') document.body.classList.add('light');

// ── УТИЛИТЫ ─────────────────────────────────────────────────────────────────
function _set(id,val){const el=document.getElementById(id);if(el) el.textContent=val;}
function _setv(id,val,cls){
    const el=document.getElementById(id);
    if(!el) return;
    el.textContent=val;
    // Убираем все классы val и ставим нужный
    el.className=el.className.replace(/\b(warn|alert|dim|ac)\b/g,'').trim();
    if(cls) el.classList.add(cls);
}
function fmtTime(s){const m=Math.floor(s/60);return `${m}:${String(Math.floor(s%60)).padStart(2,'0')}`;}

// ── РЕЖИМ РОБОТА ─────────────────────────────────────────────────────────────
async function setMotorSim(sim){
    await fetch('/api/motors/sim',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sim})});
    const bs=document.getElementById('btnMotorSim');
    const br=document.getElementById('btnMotorReal');
    if(sim){
        if(bs) bs.classList.add('btn-active');
        if(br) br.classList.remove('btn-active');
    } else {
        if(br) br.classList.add('btn-active');
        if(bs) bs.classList.remove('btn-active');
    }
}

async function setRobotMode(mode){
    const r=await fetch('/api/robot/mode',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    const d=await r.json();
    const bs=document.getElementById('btnModeSim');
    const br=document.getElementById('btnModeReal');
    if(mode==='sim'||d.success){
        if(mode==='sim'){
            if(bs){bs.classList.add('btn-active');}
            if(br){br.classList.remove('btn-active');}
        } else {
            if(br){br.classList.add('btn-active');}
            if(bs){bs.classList.remove('btn-active');}
        }
        if(logger) console.log(`Режим: ${mode}`);
    } else {
        alert(`Ошибка переключения: ${d.message||'GPIO недоступен'}`);
    }
}

// ── ОБНОВЛЕНИЕ МОТОРОВ ────────────────────────────────────────────────────────
function updateMotors(s){
    const m=s.motors;
    if(!m) return;

    // Подсвечиваем активный мотор
    const cells={
        'mtr-forward':  m.forward,
        'mtr-backward': m.backward,
        'mtr-left':     m.left,
        'mtr-right':    m.right,
    };
    for(const[id,active] of Object.entries(cells)){
        const el=document.getElementById(id);
        if(el){if(active) el.classList.add('active');else el.classList.remove('active');}
    }

    // GPIO статус
    const gpioEl=document.getElementById('mtr-gpio');
    if(gpioEl){
        gpioEl.textContent=m.enabled?'🟢 Активен ('+m.status+')':'⚫ Симуляция';
        gpioEl.style.color=m.enabled?'var(--ac)':'var(--txt2)';
    }

    // Режим кнопки
    const rm=s.robot_mode||'sim';
    const bs=document.getElementById('btnModeSim');
    const br=document.getElementById('btnModeReal');
    if(rm==='real'){
        if(br&&!br.classList.contains('btn-active')) br.classList.add('btn-active');
        if(bs) bs.classList.remove('btn-active');
    } else {
        if(bs&&!bs.classList.contains('btn-active')) bs.classList.add('btn-active');
        if(br) br.classList.remove('btn-active');
    }
}

// ── KEEPALIVE — сброс watchdog каждые 800мс ──────────────────────────────────
function keepAliveLoop(){
    fetch('/api/robot/keepalive',{method:'POST'}).catch(()=>{});
    setTimeout(keepAliveLoop, 800);
}

// ── СТАРТ ────────────────────────────────────────────────────────────────────
// ── ТЕМЫ ─────────────────────────────────────────────────────────────────
function setTheme(t){
    document.body.className=document.body.className.replace(/\b(amber|night|light)\b/g,'').trim();
    if(t) document.body.classList.add(t);
    localStorage.setItem('mars-theme',t);
    document.querySelectorAll('[id^="th-"]').forEach(function(b){b.classList.remove('on');});
    const tid='th-'+(t||'steel');
    const tb=document.getElementById(tid);
    if(tb) tb.classList.add('on');
}
const savedTheme=localStorage.getItem('mars-theme')||'';
setTheme(savedTheme);

// ── E-STOP ────────────────────────────────────────────────────────────────
let estopActive=false;
async function toggleEstop(){
    estopActive=!estopActive;
    const btn=document.getElementById('estopBtn');
    await fetch('/api/robot/estop',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({activate:estopActive})});
    if(estopActive){
        if(btn){btn.textContent='🔓 РАЗБЛОКИРОВАТЬ';btn.classList.add('armed');}
        autopilotOn=false;
        const ab=document.getElementById('apBtn');
        if(ab){ab.textContent='🤖 Автопоиск';ab.classList.remove('btn-active');}
        const apSt=document.getElementById('apStatus');
        if(apSt) apSt.style.display='none';
    } else {
        if(btn){btn.textContent='⬛ АВАРИЙНЫЙ СТОП';btn.classList.remove('armed');}
    }
}

// ── МИССИЯ ───────────────────────────────────────────────────────────────
let missionActive=false;
async function startMission(){
    if(estopActive){alert('E-STOP активен! Сначала разблокируй.');return;}
    const btn=document.getElementById('missionBtn');
    if(!missionActive){
        const r=await fetch('/api/robot/mission',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({action:'start'})});
        const d=await r.json();
        if(d.success){
            missionActive=true;autopilotOn=true;
            if(btn){btn.textContent='⏹ Стоп миссия';btn.classList.add('btn-active');}
            const ab=document.getElementById('apBtn');
            if(ab){ab.textContent='⏹ Остановить';ab.classList.add('btn-active');}
            const apSt=document.getElementById('apStatus');
            if(apSt) apSt.style.display='block';
        }
    } else {
        await fetch('/api/robot/mission',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({action:'stop'})});
        missionActive=false;autopilotOn=false;
        if(btn){btn.textContent='🎯 Миссия';btn.classList.remove('btn-active');}
        const ab=document.getElementById('apBtn');
        if(ab){ab.textContent='🤖 Автопоиск';ab.classList.remove('btn-active');}
        const apSt=document.getElementById('apStatus');
        if(apSt) apSt.style.display='none';
    }
}

// ── ДЕТЕКТОР — уверенность ────────────────────────────────────────────────
function updateDetectorConfidence(faces){
    if(!faces||!faces.length) return;
    const conf=faces[0].confidence||0;
    const pct=Math.round(conf*100);
    const el=document.getElementById('facesStat');
    if(el) el.textContent=faces.length+' ('+pct+'%)';
}

// Перехватываем stateLoop для обновления confidence
const _origStateLoop=stateLoop;

loadCameras();videoLoop();mapLoop();stateLoop();initRadar();logLoop();statsLoop();initNotifications();keepAliveLoop();
</script>

<style>
@keyframes fadeFlash{0%{opacity:1}100%{opacity:0}}
</style>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/robot/state')
def robot_state_api():
    state = robot_simulator.get_state_dict()
    # Добавляем данные сонара прямо в state — экономим один запрос
    if sonar_sensor:
        sonar = sonar_sensor.get_status()
        state['sonar_dist']    = sonar['distance_cm']
        state['sonar_obstacle']= sonar['obstacle']
        state['sonar_enabled'] = sonar['enabled']
        state['sonar_active']  = sonar['active']
        state['sonar_status']  = sonar['status']
        state['sweep_angle']   = sonar['sweep_angle']
        state['radar_points']  = sonar['radar_points']
        state['estop']         = _estop_active
    else:
        state['sonar_dist']    = 999
        state['sonar_obstacle']= False
        state['sonar_enabled'] = False
        state['sweep_angle']   = 0
    return jsonify({'mode': 'SIMULATION', 'state': state})

@app.route('/api/map')
def get_map():
    global _map_cache, _map_cache_time
    now = time.time()
    if _map_cache is None or (now - _map_cache_time) > _MAP_CACHE_TTL:
        map_img = draw_map()
        buf = io.BytesIO()
        map_img.save(buf, 'JPEG', quality=60)
        _map_cache      = buf.getvalue()
        _map_cache_time = now
    return app.response_class(response=_map_cache, mimetype='image/jpeg')

@app.route('/api/camera/frame')
def camera_frame():
    global current_frame
    
    if current_frame is None:
        black_img = Image.new('RGB', (camera_config.width, camera_config.height), color='black')
        draw = ImageDraw.Draw(black_img)
        draw.text((50, 100), "Camera", fill=(100, 255, 100))
        img_io = io.BytesIO()
        black_img.save(img_io, 'JPEG', quality=70)
        img_io.seek(0)
    else:
        with frame_lock:
            frame_copy = current_frame.copy()
        # Рисуем bbox лиц прямо на кадре
        if face_detector:
            frame_copy = face_detector.draw_on_frame(frame_copy)
        img_io = io.BytesIO()
        frame_copy.save(img_io, 'JPEG', quality=75)
        img_io.seek(0)
    
    return app.response_class(response=img_io.getvalue(), mimetype='image/jpeg')

@app.route('/api/cameras/list')
def list_cameras():
    devices = discover_video_devices()
    return jsonify({'devices': devices})

@app.route('/api/cameras/select', methods=['POST'])
def select_camera():
    data = request.get_json()
    device = data.get('device')
    if device:
        camera_manager.change_device(device)
        return jsonify({'success': True, 'message': f'📹 {camera_manager.backend_name.upper()}'})
    return jsonify({'success': False, 'message': 'Invalid device'})

# Watchdog — последнее время получения команды
_last_cmd_time = time.time()
_WATCHDOG_TIMEOUT = 1.5  # секунд без команды → стоп

def _watchdog_loop():
    """Если связь пропала — останавливаем моторы"""
    global _last_cmd_time
    while True:
        time.sleep(0.3)
        if time.time() - _last_cmd_time > _WATCHDOG_TIMEOUT:
            if robot_simulator and robot_simulator.current_command != 'STOP':
                robot_simulator.stop()

_watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
_watchdog_thread.start()

@app.route('/api/robot/command', methods=['POST'])
def robot_command():
    global _last_cmd_time
    data = request.get_json()
    cmd = data.get('cmd')
    _last_cmd_time = time.time()

    # Блокируем движение при активном E-STOP
    if _estop_active and cmd != 'stop':
        return jsonify({'success': False, 'reason': 'estop_active'})

    if cmd == 'forward':
        robot_simulator.move_forward()
    elif cmd == 'backward':
        robot_simulator.move_backward()
    elif cmd == 'left':
        robot_simulator.turn_left()
    elif cmd == 'right':
        robot_simulator.turn_right()
    elif cmd == 'stop':
        robot_simulator.stop()

    return jsonify({'success': True, 'command': cmd})

@app.route('/api/robot/keepalive', methods=['POST'])
def robot_keepalive():
    """Heartbeat от браузера — сбрасывает watchdog"""
    global _last_cmd_time
    _last_cmd_time = time.time()
    return jsonify({'ok': True})

# E-STOP — аварийная остановка всего
_estop_active = False

@app.route('/api/robot/estop', methods=['POST'])
def robot_estop():
    """Аварийная остановка — рубит всё: моторы, автопилот, миссию"""
    global _estop_active, _last_cmd_time
    data = request.get_json() or {}
    activate = data.get('activate', True)
    _estop_active = activate
    if activate:
        # Стопим всё
        if robot_simulator:
            robot_simulator.stop()
        if autopilot:
            autopilot.stop()
        if logger:
            logger.log_event('E-STOP', 'Аварийная остановка активирована')
        print("[!] E-STOP АКТИВИРОВАН")
    else:
        _estop_active = False
        if logger:
            logger.log_event('E-STOP', 'Система разблокирована')
        print("[✓] E-STOP снят")
    return jsonify({'ok': True, 'estop': _estop_active})

@app.route('/api/robot/estop/status')
def estop_status():
    return jsonify({'estop': _estop_active})

@app.route('/api/robot/speed', methods=['POST'])
def set_robot_speed():
    data = request.get_json()
    speed = data.get('speed', 150)
    robot_simulator.set_speed(speed)
    return jsonify({'success': True, 'speed': speed})

@app.route('/api/robot/clear_path', methods=['POST'])
def clear_path():
    """Очистить историю пути"""
    if robot_simulator:
        robot_simulator.path_history.clear()
        robot_simulator.path_history.append((robot_simulator.x, robot_simulator.y))
        if logger:
            logger.log_event('PATH_CLEARED', 'История пути очищена')
    return jsonify({'success': True})

@app.route('/api/motors/sim', methods=['POST'])
def set_motors_sim():
    """Включить/выключить симуляцию моторов"""
    data = request.get_json()
    sim  = data.get('sim', True)
    if robot_simulator:
        robot_simulator.motors.set_sim_mode(sim)
        if logger:
            logger.log_event('МОТОРЫ_РЕЖ', 'Симуляция' if sim else 'GPIO реальный')
    return jsonify({'success': True, 'sim_mode': sim})

@app.route('/api/robot/mode', methods=['POST'])
def set_robot_mode():
    """Переключить режим: sim / real"""
    global _robot_mode
    data = request.get_json()
    mode = data.get('mode', 'sim')

    if mode == 'sim':
        _robot_mode = 'sim'
        # В симуляции отключаем GPIO моторов
        if robot_simulator:
            robot_simulator.motors.enabled = False
        if logger:
            logger.log_event('РЕЖИМ', 'Симуляция движения')
        return jsonify({'success': True, 'mode': 'sim'})

    elif mode == 'real':
        if not GPIO_AVAILABLE:
            return jsonify({'success': False, 'message': 'GPIO недоступен'})
        if not robot_simulator or not robot_simulator.motors.enabled:
            # Пробуем переинициализировать GPIO
            try:
                GPIO.setmode(GPIO.BOARD)
                GPIO.setwarnings(False)
                m = robot_simulator.motors
                for pin in [m.PIN_FORWARD, m.PIN_BACKWARD, m.PIN_LEFT, m.PIN_RIGHT]:
                    GPIO.setup(pin, GPIO.OUT)
                    GPIO.output(pin, GPIO.LOW)
                robot_simulator.motors.enabled = True
                _robot_mode = 'real'
                if logger:
                    logger.log_event('РЕЖИМ', 'Реальное управление GPIO')
                return jsonify({'success': True, 'mode': 'real'})
            except Exception as e:
                return jsonify({'success': False, 'message': str(e)})
        else:
            _robot_mode = 'real'
            return jsonify({'success': True, 'mode': 'real'})

    return jsonify({'success': False, 'message': 'Неверный режим'})

@app.route('/api/humans/photo/<int:human_id>')
def human_photo(human_id):
    """Фото найденного человека"""
    for h in robot_simulator.found_humans:
        if h['id'] == human_id and h.get('photo'):
            import base64
            photo_bytes = base64.b64decode(h['photo'])
            return app.response_class(response=photo_bytes, mimetype='image/jpeg')
    return app.response_class(response=b'', status=404)

@app.route('/api/humans/list')
def humans_list():
    """Список найденных людей с фото"""
    result = []
    for h in robot_simulator.found_humans:
        result.append({
            'id': h['id'],
            'x': h['x'],
            'y': h['y'],
            'timestamp': h['timestamp'],
            'has_photo': bool(h.get('photo')),
        })
    return jsonify({'humans': result, 'count': len(result)})

@app.route('/api/sonar')
def sonar_api():
    if not sonar_sensor:
        return jsonify({'distance_cm':999,'obstacle':False,'radar_points':[],
                        'enabled':False,'active':False,'status':'НЕТ_GPIO',
                        'sim_mode':False,'robot_angle':0,'sweep_angle':0})
    return jsonify(sonar_sensor.get_status())

@app.route('/api/sonar/mode', methods=['POST'])
def sonar_mode():
    """Включить / выключить сонар"""
    data = request.get_json()
    mode = data.get('mode', 'off')
    if not sonar_sensor:
        return jsonify({'success': False, 'message': 'Сонар не инициализирован'})
    if mode == 'on':
        ok, msg = sonar_sensor.turn_on()
        return jsonify({'success': ok, 'message': msg, 'status': sonar_sensor.status})
    elif mode == 'off':
        sonar_sensor.turn_off()
        return jsonify({'success': True, 'status': sonar_sensor.status})
    return jsonify({'success': False, 'message': 'Неверный режим'})

@app.route('/api/sonar/diagnostic')
def sonar_diagnostic():
    """Диагностика сонара"""
    if not sonar_sensor:
        return jsonify({'ok': False, 'message': 'Объект не создан'})
    result = {
        'gpio_available':   GPIO_AVAILABLE,
        'gpio_initialized': sonar_sensor.enabled,
        'active':           sonar_sensor.active,
        'status':           sonar_sensor.status,
        'error_msg':        sonar_sensor.error_msg,
        'error_count':      sonar_sensor.error_count,
        'pin_trig':         sonar_sensor.PIN_TRIG,
        'pin_echo':         sonar_sensor.PIN_ECHO,
        'last_distance':    sonar_sensor.distance_cm,
        'last_ok_ago':      round(time.time()-sonar_sensor.last_ok_time,1) if sonar_sensor.last_ok_time else None,
    }
    if not GPIO_AVAILABLE:
        result['verdict'] = '❌ OPi.GPIO не установлен'
    elif not sonar_sensor.enabled:
        result['verdict'] = f'❌ GPIO ошибка: {sonar_sensor.error_msg}'
    elif not sonar_sensor.active:
        result['verdict'] = '⚠️ Сонар выключен'
    elif sonar_sensor.status == 'ТАЙМАУТ':
        result['verdict'] = '❌ Нет ответа — проверь ECHO/TRIG'
    elif sonar_sensor.status == 'ОШИБКА':
        result['verdict'] = f'❌ Ошибка: {sonar_sensor.error_msg}'
    elif sonar_sensor.status == 'OK':
        result['verdict'] = f'✅ Работает — {sonar_sensor.distance_cm:.0f} см'
    else:
        result['verdict'] = f'⚠️ {sonar_sensor.status}'
    return jsonify(result)


@app.route('/api/autopilot/start', methods=['POST'])
def autopilot_start():
    if autopilot:
        autopilot.start()
        return jsonify({'success': True, 'mode': autopilot.mode})
    return jsonify({'success': False})

@app.route('/api/autopilot/stop', methods=['POST'])
def autopilot_stop():
    if autopilot:
        autopilot.stop()
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/api/log/events')
def log_events():
    if logger:
        return jsonify({
            'events': logger.get_recent_events(30),
            'stats': logger.get_stats()
        })
    return jsonify({'events': [], 'stats': {}})

@app.route('/api/log/screenshot', methods=['POST'])
def log_screenshot():
    global current_frame
    if logger and current_frame:
        with frame_lock:
            frame_copy = current_frame.copy()
        name = logger.save_screenshot(frame_copy, 'manual')
        return jsonify({'success': True, 'filename': name})
    return jsonify({'success': False})

@app.route('/api/stats')
def get_stats():
    """Статистика сессии"""
    if not robot_simulator:
        return jsonify({})

    path = list(robot_simulator.path_history)

    # Считаем пройденное расстояние
    dist_total = 0.0
    for i in range(1, len(path)):
        dx = path[i][0] - path[i-1][0]
        dy = path[i][1] - path[i-1][1]
        dist_total += math.sqrt(dx*dx + dy*dy)

    # Покрытие карты (уникальные ячейки 50x50)
    cells = set()
    for x, y in path:
        cells.add((int(x // 50), int(y // 50)))
    total_cells = (robot_config.map_width // 50) * (robot_config.map_height // 50)
    coverage = round(len(cells) / total_cells * 100, 1)

    uptime = time.time() - robot_simulator.start_time

    return jsonify({
        'uptime_sec':    round(uptime),
        'uptime_str':    f"{int(uptime//60)}м {int(uptime%60)}с",
        'dist_px':       round(dist_total),
        'dist_m':        round(dist_total / 100, 1),
        'path_points':   len(path),
        'coverage_pct':  coverage,
        'cells_visited': len(cells),
        'humans_found':  len(robot_simulator.found_humans),
        'faces_now':     len(robot_simulator.detected_faces),
        'autopilot':     autopilot.get_status() if autopilot else {},
        'session_id':    logger.session_id if logger else '—',
        'log_dir':       logger.session_dir if logger else '—',
    })

@app.route('/api/map/export')
def export_map():
    """Экспорт карты в PNG высокого качества"""
    map_img = draw_map()
    export_img = map_img.resize(
        (robot_config.map_width * 2, robot_config.map_height * 2),
        Image.NEAREST
    )
    img_io = io.BytesIO()
    export_img.save(img_io, 'PNG')
    img_io.seek(0)
    return app.response_class(
        response=img_io.getvalue(),
        mimetype='image/png',
        headers={'Content-Disposition': 'attachment; filename=mars_map.png'}
    )

@app.route('/api/report')
def get_report():
    """Генерация HTML отчёта"""
    map_img = draw_map()
    html    = report_generator.generate(robot_simulator, logger, map_img)
    if logger:
        logger.log_event('ОТЧЁТ', 'Сформирован отчёт PDF')
    return app.response_class(response=html, mimetype='text/html; charset=utf-8')

@app.route('/api/motion')
def get_motion():
    """Данные детектора движения"""
    if not motion_detector:
        return jsonify({'level': 0, 'detected': False, 'percent': 0})
    return jsonify(motion_detector.get_status())

@app.route('/api/heatmap')
def get_heatmap():
    """Тепловая карта покрытия"""
    if not heatmap:
        return jsonify({'coverage': 0})
    map_img  = draw_map()
    img_io   = io.BytesIO()
    map_img.save(img_io, 'JPEG', quality=65)
    img_io.seek(0)
    return app.response_class(response=img_io.getvalue(), mimetype='image/jpeg')

@app.route('/api/graphs')
def get_graphs():
    """Данные для графиков"""
    path = list(robot_simulator.path_history) if robot_simulator else []

    # Скорости по точкам пути
    speeds = []
    for i in range(1, len(path)):
        dx = path[i][0] - path[i-1][0]
        dy = path[i][1] - path[i-1][1]
        speeds.append(round(math.sqrt(dx*dx + dy*dy), 1))

    return jsonify({
        'path_x':    [round(p[0], 1) for p in path[::5]],
        'path_y':    [round(p[1], 1) for p in path[::5]],
        'speeds':    speeds[::5],
        'coverage':  heatmap.get_coverage() if heatmap else 0,
        'motion':    motion_detector.get_status() if motion_detector else {},
    })


@app.route('/api/robot/mission', methods=['POST'])
def robot_mission():
    """Запуск / остановка автономной миссии поиска"""
    data = request.get_json() or {}
    action = data.get('action', 'start')
    if action == 'start':
        if _estop_active:
            return jsonify({'success': False, 'reason': 'estop_active'})
        if autopilot:
            autopilot.start()
        if logger:
            logger.log_event('МИССИЯ_СТАРТ', 'Автономная миссия запущена')
        return jsonify({'success': True, 'status': 'started'})
    elif action == 'stop':
        if autopilot:
            autopilot.stop()
        if logger:
            logger.log_event('МИССИЯ_СТОП', 'Миссия остановлена оператором')
        return jsonify({'success': True, 'status': 'stopped'})
    return jsonify({'success': False, 'reason': 'unknown_action'})

if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║  М.А.Р.С. — Мобильный Автоматизированный Робот Спасатель ║
    ║  v3.0 · Orange Pi PC H3 · Armbian · ВКР 2026             ║
    ║  Пины: L_FWD=11 L_BWD=13 R_FWD=15 R_BWD=3               ║
    ║  Логика: LOW=движение HIGH=стоп (инвертированная)         ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    
    print("\n[*] Инициализация системы...")
    initialize_system()
    
    print(f"[✓] Камера: {camera_manager.backend_name}")
    print(f"[✓] Разрешение: {camera_config.width}x{camera_config.height}")
    print(f"[✓] FPS: {camera_config.fps}")
    
    print("[*] Запуск захвата видео...")
    video_thread = threading.Thread(target=capture_and_process_video, daemon=True)
    video_thread.start()
    
    print("\n[✓] Система готова! 🚀")
    print(f"[→] Открой браузер: http://localhost:5000")
    print(f"[→] Или с другого ПК: http://<IP_ORANGE_PI>:5000")
    print(f"\n[!] GPIO моторы: {'ВКЛЮЧЕНЫ' if robot_simulator.motors.enabled else 'СИМУЛЯЦИЯ'}")
    print("\n[Press Ctrl+C to stop]\n")
    
    try:
        app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\n[*] Выключение...")
    finally:
        if robot_simulator:
            robot_simulator.motors.cleanup()
        print("[✓] Готово!")
