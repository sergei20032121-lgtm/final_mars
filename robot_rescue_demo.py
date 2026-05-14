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
    fps: int = 15
    jpeg_quality: int = 75
    detect_interval: float = 0.8
    backend: str = "auto"
    pigo_min_size: int = 40
    pigo_max_size: int = 320
    pigo_iou: float = 0.15

@dataclass
class RobotConfig:
    """Конфигурация робота"""
    map_width: int = 800
    map_height: int = 600
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

class MotorController:
    """
    Управление моторами через GPIO Orange Pi.
    Если GPIO недоступен — работает в режиме симуляции.
    """

    # Пины — BOARD нумерация (номера физических пинов на разъёме)
    PIN_FORWARD  = 11   # PA0 — физический Pin 11
    PIN_BACKWARD = 13   # PA1 — физический Pin 13
    PIN_LEFT     = 15   # PA3 — физический Pin 15
    PIN_RIGHT    = 16   # PA4 — физический Pin 16

    def __init__(self):
        self.enabled = False
        self.current_cmd = 'STOP'

        if not GPIO_AVAILABLE:
            print("[⚠️] MotorController: OPi.GPIO не установлен")
            return

        try:
            # OPi.GPIO 0.5.x — без setboard, используем BOARD нумерацию
            GPIO.setmode(GPIO.BOARD)
            GPIO.setwarnings(False)

            for pin in [self.PIN_FORWARD, self.PIN_BACKWARD,
                        self.PIN_LEFT, self.PIN_RIGHT]:
                GPIO.setup(pin, GPIO.OUT)
                GPIO.output(pin, GPIO.LOW)

            self.enabled = True
            print("[✓] MotorController: GPIO инициализирован")
            print(f"    FORWARD  = Pin {self.PIN_FORWARD}")
            print(f"    BACKWARD = Pin {self.PIN_BACKWARD}")
            print(f"    LEFT     = Pin {self.PIN_LEFT}")
            print(f"    RIGHT    = Pin {self.PIN_RIGHT}")

        except Exception as e:
            print(f"[✗] MotorController: Ошибка — {e}")
            print(f"    Тип ошибки: {type(e).__name__}")
            print("    Попробуй: sudo python3 robot_rescue_demo.py")

    def _all_low(self):
        """Сбросить все пины в LOW"""
        if not self.enabled:
            return
        for pin in [self.PIN_FORWARD, self.PIN_BACKWARD,
                    self.PIN_LEFT, self.PIN_RIGHT]:
            GPIO.output(pin, GPIO.LOW)

    def forward(self):
        self.current_cmd = 'FORWARD'
        if not self.enabled:
            return
        self._all_low()
        GPIO.output(self.PIN_FORWARD, GPIO.HIGH)

    def backward(self):
        self.current_cmd = 'BACKWARD'
        if not self.enabled:
            return
        self._all_low()
        GPIO.output(self.PIN_BACKWARD, GPIO.HIGH)

    def left(self):
        self.current_cmd = 'LEFT'
        if not self.enabled:
            return
        self._all_low()
        GPIO.output(self.PIN_LEFT, GPIO.HIGH)

    def right(self):
        self.current_cmd = 'RIGHT'
        if not self.enabled:
            return
        self._all_low()
        GPIO.output(self.PIN_RIGHT, GPIO.HIGH)

    def stop(self):
        self.current_cmd = 'STOP'
        self._all_low()

    def cleanup(self):
        """Вызывать при выходе"""
        self.stop()
        if self.enabled:
            GPIO.cleanup()
            print("[✓] GPIO очищен")

    @property
    def status(self):
        if self.enabled:
            return f"GPIO ({self.current_cmd})"
        return f"SIM ({self.current_cmd})"


# ============================================================================
# СИМУЛЯТОР РОБОТА
# ============================================================================

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
        self.path_history = deque(maxlen=500)
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
            'autopilot': autopilot.get_status() if autopilot else {'enabled': False, 'mode': 'IDLE'},
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
current_frame   = None
frame_lock      = threading.Lock()

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
        self.detect_interval = config.detect_interval
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
    Если GPIO доступен — читает реальные данные.
    Если нет — симулирует препятствия.
    """

    PIN_TRIG = 22   # PA2 — физический Pin 22
    PIN_ECHO = 18   # PA1 — физический Pin 18  ← через делитель!

    # Угол обзора сонара (градусы)
    SWEEP_ANGLE = 60

    # История показаний для радара: {угол: расстояние_см}
    MAX_HISTORY = 36  # 36 точек = каждые 10 градусов

    def __init__(self):
        self.enabled      = False
        self.distance_cm  = 999.0
        self.lock         = threading.Lock()
        self.radar_points = deque(maxlen=self.MAX_HISTORY)
        self.running      = False
        self.thread       = None

        self._sim_obstacles = [
            (200, 150), (500, 300), (300, 450),
            (600, 200), (150, 400), (650, 500),
        ]

        if GPIO_AVAILABLE:
            try:
                GPIO.setmode(GPIO.BOARD)
                GPIO.setwarnings(False)
                GPIO.setup(self.PIN_TRIG, GPIO.OUT)
                GPIO.setup(self.PIN_ECHO, GPIO.IN)
                GPIO.output(self.PIN_TRIG, GPIO.LOW)
                time.sleep(0.05)
                self.enabled = True
                print("[✓] Сонар: GPIO готов")
                print(f"    TRIG = Pin {self.PIN_TRIG}, ECHO = Pin {self.PIN_ECHO}")
            except Exception as e:
                print(f"[✗] Сонар: ошибка GPIO — {e}")
        else:
            print("[⚠️] Сонар: режим симуляции")

    def start(self):
        """Запустить поток измерений"""
        self.running = True
        self.thread = threading.Thread(target=self._measure_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _measure_loop(self):
        """Непрерывный цикл измерений"""
        while self.running:
            try:
                if self.enabled:
                    dist = self._measure_real()
                else:
                    dist = self._measure_sim()

                angle = robot_simulator.angle if robot_simulator else 0

                with self.lock:
                    self.distance_cm = dist
                    self.radar_points.append({
                        'angle': angle,
                        'dist': dist,
                        'x': robot_simulator.x if robot_simulator else 400,
                        'y': robot_simulator.y if robot_simulator else 300,
                    })

                time.sleep(0.1)  # 10 измерений в секунду

            except Exception:
                time.sleep(0.2)

    def _measure_real(self):
        """Реальное измерение через GPIO"""
        try:
            # Посылаем импульс TRIG
            GPIO.output(self.PIN_TRIG, GPIO.HIGH)
            time.sleep(0.00001)  # 10 мкс
            GPIO.output(self.PIN_TRIG, GPIO.LOW)

            # Ждём ECHO HIGH
            timeout = time.time() + 0.04
            while GPIO.input(self.PIN_ECHO) == 0:
                if time.time() > timeout:
                    return 999.0
            t_start = time.time()

            # Ждём ECHO LOW
            timeout = time.time() + 0.04
            while GPIO.input(self.PIN_ECHO) == 1:
                if time.time() > timeout:
                    return 999.0
            t_end = time.time()

            # Расстояние = время * скорость звука / 2
            dist = (t_end - t_start) * 34300 / 2
            return round(min(dist, 400.0), 1)

        except Exception:
            return 999.0

    def _measure_sim(self):
        """Симуляция — вычисляет расстояние до ближайшего препятствия"""
        if not robot_simulator:
            return 999.0

        rx, ry   = robot_simulator.x, robot_simulator.y
        angle    = robot_simulator.angle
        angle_r  = math.radians(angle)

        # Луч вперёд
        min_dist = 300.0
        step     = 5.0
        for d in range(5, 300, int(step)):
            bx = rx + math.sin(angle_r) * d
            by = ry - math.cos(angle_r) * d

            # Проверяем стены карты
            if bx < 0 or bx >= robot_config.map_width or \
               by < 0 or by >= robot_config.map_height:
                min_dist = d
                break

            # Проверяем препятствия
            for ox, oy in self._sim_obstacles:
                if abs(bx - ox) < 25 and abs(by - oy) < 25:
                    min_dist = d
                    break
            else:
                continue
            break

        # Переводим пиксели → сантиметры (1px ≈ 1cm в симуляции)
        return round(min_dist, 1)

    def get_status(self):
        try:
            with self.lock:
                dist  = self.distance_cm
                pts   = list(self.radar_points)
            return {
                'distance_cm':  round(float(dist), 1),
                'radar_points': pts,
                'enabled':      self.enabled,
                'sim_mode':     not self.enabled,
                'obstacle':     dist < 30,
                'running':      self.running,
                'robot_angle':  robot_simulator.angle if robot_simulator else 0,
            }
        except Exception:
            return {
                'distance_cm': 999.0,
                'radar_points': [],
                'enabled': False,
                'sim_mode': True,
                'obstacle': False,
                'running': False,
                'robot_angle': 0,
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
    """Автономный поиск человека по спирали с объездом препятствий."""

    РЕЖИМ_СТОП    = 'СТОП'
    РЕЖИМ_ПОИСК   = 'ПОИСК'
    РЕЖИМ_ОБЪЕЗД  = 'ОБЪЕЗД'
    РЕЖИМ_НАЙДЕН  = 'НАЙДЕН'
    РЕЖИМ_ВОЗВРАТ = 'ВОЗВРАТ'

    DIST_ПРЕПЯТСТВИЕ = 40   # см — срабатывание объезда
    ОТСТУП_СТЕНЫ     = 60   # px — отступ от края карты

    def __init__(self):
        self.режим     = self.РЕЖИМ_СТОП
        self.enabled   = False
        self.thread    = None
        self.running   = False

        self._шагов_прямо   = 0
        self._счётчик_шагов = 0
        self._поворотов     = 0
        self._плечо         = 1
        self._avoid_steps   = 0
        self._return_path   = []
        self._return_idx    = 0

        print("[✓] Автопилот: готов")

    def start(self):
        if self.running:
            return
        self.режим          = self.РЕЖИМ_ПОИСК
        self.enabled        = True
        self.running        = True
        self._счётчик_шагов = 0
        self._поворотов     = 0
        self._плечо         = 1

        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

        if logger:
            logger.log_event('АВТОПИЛОТ_СТАРТ', 'Автономный поиск запущен')
        print("[✓] Автопилот: поиск начат")

    def stop(self):
        self.running = False
        self.enabled = False
        self.режим   = self.РЕЖИМ_СТОП
        if robot_simulator:
            robot_simulator.stop()
        if logger:
            logger.log_event('АВТОПИЛОТ_СТОП', 'Автопилот остановлен')

    def _у_края(self):
        """Проверить что робот близко к краю карты"""
        if not robot_simulator:
            return False
        x, y = robot_simulator.x, robot_simulator.y
        o = self.ОТСТУП_СТЕНЫ
        return (x < o or x > robot_config.map_width - o or
                y < o or y > robot_config.map_height - o)

    def _развернуться(self):
        """Развернуться от края — поворот к центру карты"""
        if not robot_simulator:
            return
        robot_simulator.stop()
        time.sleep(0.1)

        cx = robot_config.map_width  / 2
        cy = robot_config.map_height / 2
        dx = cx - robot_simulator.x
        dy = cy - robot_simulator.y

        # Угол к центру
        target = math.degrees(math.atan2(dx, -dy)) % 360
        current = robot_simulator.angle

        # Повернуть в нужную сторону
        diff = (target - current + 360) % 360
        steps = int(diff / 5) + 2

        for _ in range(steps):
            robot_simulator.turn_right()
            time.sleep(0.08)

        robot_simulator.stop()
        self._счётчик_шагов = 0

        if logger:
            logger.log_event('РАЗВОРОТ', f'Край карты → центр ({target:.0f}°)')

    def _loop(self):
        while self.running:
            try:
                if self.режим == self.РЕЖИМ_НАЙДЕН:
                    robot_simulator.stop()
                    time.sleep(0.5)
                    continue

                if self.режим == self.РЕЖИМ_ВОЗВРАТ:
                    self._do_return()
                    continue

                # Проверка границ карты
                if self._у_края():
                    self._развернуться()
                    time.sleep(0.2)
                    continue

                # Препятствие
                dist = sonar_sensor.distance_cm if sonar_sensor else 999
                if dist < self.DIST_ПРЕПЯТСТВИЕ and self.режим == self.РЕЖИМ_ПОИСК:
                    self._start_avoid()

                if self.режим == self.РЕЖИМ_ОБЪЕЗД:
                    self._do_avoid()
                    continue

                if self.режим == self.РЕЖИМ_ПОИСК:
                    self._do_spiral()

                if robot_simulator and robot_simulator.detected_faces:
                    self._on_found()

                time.sleep(0.12)

            except Exception:
                time.sleep(0.5)

    def _do_spiral(self):
        """Движение по спирали"""
        if self._счётчик_шагов < self._плечо * 12:
            robot_simulator.move_forward()
            self._счётчик_шагов += 1
        else:
            robot_simulator.turn_right()
            time.sleep(0.35)
            robot_simulator.stop()
            self._счётчик_шагов = 0
            self._поворотов += 1
            if self._поворотов % 2 == 0:
                self._плечо += 1
            if logger:
                logger.log_event('СПИРАЛЬ',
                    f'Плечо={self._плечо}, поворот={self._поворотов}')

    def _start_avoid(self):
        self.режим = self.РЕЖИМ_ОБЪЕЗД
        self._avoid_steps = 0
        robot_simulator.stop()
        dist = sonar_sensor.distance_cm if sonar_sensor else 0
        if logger:
            logger.log_event('ПРЕПЯТСТВИЕ', f'{dist:.0f}см — объезд')

    def _do_avoid(self):
        s = self._avoid_steps
        if s < 4:
            robot_simulator.stop()
        elif s < 10:
            robot_simulator.turn_right()
        elif s < 25:
            dist = sonar_sensor.distance_cm if sonar_sensor else 999
            if dist > self.DIST_ПРЕПЯТСТВИЕ:
                robot_simulator.move_forward()
            else:
                robot_simulator.turn_right()
        else:
            self.режим = self.РЕЖИМ_ПОИСК
            self._счётчик_шагов = 0
            if logger:
                logger.log_event('ОБЪЕЗД_ЗАВЕРШЁН', 'Продолжаю поиск')
        self._avoid_steps += 1
        time.sleep(0.12)

    def _on_found(self):
        if self.режим == self.РЕЖИМ_НАЙДЕН:
            return
        self.режим = self.РЕЖИМ_НАЙДЕН
        robot_simulator.stop()
        if logger:
            logger.log_event('ЧЕЛОВЕК_НАЙДЕН',
                f'Позиция: ({robot_simulator.x:.0f}, {robot_simulator.y:.0f})')
        if current_frame and logger:
            with frame_lock:
                fc = current_frame.copy()
            logger.save_screenshot(fc, 'человек_найден')
        print("[!] ЧЕЛОВЕК НАЙДЕН! Останавливаюсь.")

        # Через 3 секунды — начать возврат на базу
        threading.Timer(3.0, self._start_return).start()

    def _start_return(self):
        """Начать возврат на базу"""
        if not self.running:
            return
        self.режим = self.РЕЖИМ_ВОЗВРАТ
        self._return_path = list(robot_simulator.path_history)[::-1]  # Обратный путь
        self._return_idx = 0
        if logger:
            logger.log_event('ВОЗВРАТ', 'Возвращаюсь на базу')
        print("[→] Возврат на базу...")

    def _do_return(self):
        """Движение по обратному пути к базе"""
        if not self._return_path or self._return_idx >= len(self._return_path):
            robot_simulator.stop()
            self.режим = self.РЕЖИМ_СТОП
            self.running = False
            if logger:
                logger.log_event('БАЗА', 'Вернулся на базу')
            print("[✓] На базе!")
            return

        # Целевая точка
        tx, ty = self._return_path[self._return_idx]
        rx, ry = robot_simulator.x, robot_simulator.y

        dist = math.sqrt((tx - rx)**2 + (ty - ry)**2)

        if dist < 20:
            self._return_idx += 5  # Пропускаем близкие точки
            return

        # Поворачиваем к цели
        target_angle = math.degrees(math.atan2(tx - rx, -(ty - ry))) % 360
        current = robot_simulator.angle
        diff = (target_angle - current + 360) % 360

        if diff > 15 and diff < 345:
            if diff < 180:
                robot_simulator.turn_right()
            else:
                robot_simulator.turn_left()
        else:
            robot_simulator.move_forward()

        self._return_idx += 1
        time.sleep(0.1)

    def get_status(self):
        return {
            'enabled':     self.enabled,
            'mode':        self.режим,
            'spiral_leg':  self._плечо,
            'turn_count':  self._поворотов,
        }



# ============================================================================
# ДЕТЕКТОР ДВИЖЕНИЯ
# ============================================================================

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
<div class="footer">М.А.Р.С. v2.3 · Orange Pi PC H3 · {time.strftime("%Y")}</div>
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
    """Карта с тепловой картой, сонаром и препятствиями"""
    img  = Image.new('RGB', (robot_config.map_width, robot_config.map_height), color='#030a08')
    draw = ImageDraw.Draw(img)

    # Сетка
    for x in range(0, robot_config.map_width, 50):
        draw.line([(x, 0), (x, robot_config.map_height)], fill=(15, 30, 20), width=1)
    for y in range(0, robot_config.map_height, 50):
        draw.line([(0, y), (robot_config.map_width, y)], fill=(15, 30, 20), width=1)

    # Граница безопасной зоны
    o = 60
    draw.rectangle([o, o, robot_config.map_width-o, robot_config.map_height-o],
                   outline=(30, 60, 40), width=1)

    # ── Тепловая карта покрытия ───────────────────────────────────────────
    if heatmap:
        img = heatmap.draw_overlay(img)
        draw = ImageDraw.Draw(img)

    # ── Препятствия ───────────────────────────────────────────────────────
    if sonar_sensor:
        for ox, oy in sonar_sensor._sim_obstacles:
            draw.rectangle([ox-20, oy-20, ox+20, oy+20],
                           fill=(40, 40, 40), outline=(70, 70, 70), width=1)
            draw.line([(ox-8, oy-8), (ox+8, oy+8)], fill=(100, 100, 100), width=1)
            draw.line([(ox+8, oy-8), (ox-8, oy+8)], fill=(100, 100, 100), width=1)

        with sonar_sensor.lock:
            pts = list(sonar_sensor.radar_points)
        for pt in pts:
            if pt['dist'] < 250:
                px = pt['x'] + math.sin(math.radians(pt['angle'])) * pt['dist']
                py = pt['y'] - math.cos(math.radians(pt['angle'])) * pt['dist']
                if 0 <= px < robot_config.map_width and 0 <= py < robot_config.map_height:
                    draw.ellipse([px-3, py-3, px+3, py+3], fill=(180, 90, 20))

    # ── Луч сонара ────────────────────────────────────────────────────────
    if sonar_sensor and robot_simulator:
        sonar_data = sonar_sensor.get_status()
        dist    = sonar_data['distance_cm']
        rx, ry  = robot_simulator.x, robot_simulator.y
        angle_r = math.radians(robot_simulator.angle)
        cone_dist = min(dist, 200)

        for a_off in range(-30, 31, 5):
            ar = math.radians(robot_simulator.angle + a_off)
            alpha = max(8, 35 - abs(a_off))
            draw.line([(rx, ry), (rx + math.sin(ar)*cone_dist,
                                  ry - math.cos(ar)*cone_dist)],
                      fill=(0, alpha, 0), width=1)

        end_x = rx + math.sin(angle_r) * cone_dist
        end_y = ry - math.cos(angle_r) * cone_dist
        draw.line([(rx, ry), (end_x, end_y)], fill=(0, 220, 70), width=2)

        if dist < 200:
            ox = rx + math.sin(angle_r) * dist
            oy = ry - math.cos(angle_r) * dist
            color = (255, 50, 50) if dist < 30 else (255, 160, 0)
            draw.ellipse([ox-5, oy-5, ox+5, oy+5], fill=color, outline=(255, 255, 255))

        col = (255, 50, 50) if dist < 30 else (180, 255, 100)
        draw.text((10, 50), f"Сонар: {dist:.0f} см", fill=col)
        if dist < 30:
            draw.text((10, 68), "⚠ ПРЕПЯТСТВИЕ", fill=(255, 50, 50))

    # ── База (стартовая точка) ─────────────────────────────────────────────
    if robot_simulator:
        bx, by = robot_simulator.base_x, robot_simulator.base_y
        for ring, alpha in [(16, 80), (10, 150)]:
            draw.ellipse([bx-ring, by-ring, bx+ring, by+ring],
                         outline=(100, 180, 255), width=1)
        draw.ellipse([bx-5, by-5, bx+5, by+5], fill=(100, 180, 255))
        draw.text((bx+8, by-6), "БАЗА", fill=(100, 180, 255))

    # ── История пути ──────────────────────────────────────────────────────
    path_list = list(robot_simulator.path_history)
    if len(path_list) > 1:
        total = len(path_list)
        for i in range(1, total):
            t = i / total
            r = int(40 + t * 30)
            g = int(80 + t * 100)
            b = int(50 + t * 30)
            draw.line([path_list[i-1], path_list[i]], fill=(r, g, b), width=2)

    # ── Найденные люди ─────────────────────────────────────────────────────
    for human in robot_simulator.found_humans:
        x, y = human['x'], human['y']
        if 0 <= x < robot_config.map_width and 0 <= y < robot_config.map_height:
            for ring, alpha in [(18, 30), (13, 60), (8, 120)]:
                draw.ellipse([x-ring, y-ring, x+ring, y+ring],
                             outline=(200, 60, 60), width=1)
            draw.ellipse([x-8, y-8, x+8, y+8],
                         fill=(200, 60, 60), outline=(255, 150, 100), width=2)
            draw.text((x-4, y-7), str(human['id']), fill=(255, 255, 255))

    # ── Робот ──────────────────────────────────────────────────────────────
    rx = max(0, min(robot_config.map_width  - 1, robot_simulator.x))
    ry = max(0, min(robot_config.map_height - 1, robot_simulator.y))
    robot_simulator.x, robot_simulator.y = rx, ry

    size    = robot_config.robot_size
    angle_r = math.radians(robot_simulator.angle)

    draw.ellipse([rx-size-2, ry-size-2, rx+size+2, ry+size+2], fill=(0, 40, 20))
    draw.ellipse([rx-size, ry-size, rx+size, ry+size],
                 fill=(60, 200, 100), outline=(0, 255, 80), width=2)
    ex = rx + size * 1.8 * math.sin(angle_r)
    ey = ry - size * 1.8 * math.cos(angle_r)
    draw.line([(rx, ry), (ex, ey)], fill=(0, 200, 255), width=3)
    draw.ellipse([ex-3, ey-3, ex+3, ey+3], fill=(255, 255, 100))

    # ── Статус автопилота ─────────────────────────────────────────────────
    if autopilot and autopilot.enabled:
        режим_цвет = {
            'ПОИСК':(0,200,100), 'ОБЪЕЗД':(255,160,0),
            'НАЙДЕН':(255,50,50), 'ВОЗВРАТ':(100,150,255),
        }
        col = режим_цвет.get(autopilot.режим, (150,150,150))
        draw.rectangle([robot_config.map_width-135, 8,
                        robot_config.map_width-8, 26], fill=(0,0,0))
        draw.text((robot_config.map_width-130, 10),
                  f"АВТО: {autopilot.режим}", fill=col)

    # ── Детектор движения ─────────────────────────────────────────────────
    if motion_detector:
        ms = motion_detector.get_status()
        if ms['detected']:
            draw.text((10, 85), f"⚡ ДВИЖЕНИЕ {ms['percent']:.0f}%", fill=(255, 200, 0))

    # ── Легенда (правый нижний угол) ──────────────────────────────────────
    lx = robot_config.map_width - 110
    ly = robot_config.map_height - 75
    legend = [
        ((60, 200, 100), "Робот"),
        ((100, 180, 255), "База"),
        ((200, 60, 60),  "Человек"),
        ((180, 90, 20),  "Препятствие"),
    ]
    draw.rectangle([lx-4, ly-4, robot_config.map_width-4, robot_config.map_height-4],
                   fill=(0, 0, 0, 180))
    for i, (color, label) in enumerate(legend):
        y = ly + i * 16
        draw.ellipse([lx, y+2, lx+8, y+10], fill=color)
        draw.text((lx+12, y), label, fill=(150, 150, 150))

    # ── Инфо ──────────────────────────────────────────────────────────────
    draw.text((10, 10), f"({int(rx)}, {int(ry)})  {int(robot_simulator.angle)}°",
              fill=(60, 180, 100))
    if heatmap:
        draw.text((10, 28), f"Покрытие: {heatmap.get_coverage():.0f}%",
                  fill=(40, 120, 70))

    return img
    img  = Image.new('RGB', (robot_config.map_width, robot_config.map_height), color='#030a08')
    draw = ImageDraw.Draw(img)

    # Сетка
    for x in range(0, robot_config.map_width, 50):
        draw.line([(x, 0), (x, robot_config.map_height)], fill=(15, 30, 20), width=1)
    for y in range(0, robot_config.map_height, 50):
        draw.line([(0, y), (robot_config.map_width, y)], fill=(15, 30, 20), width=1)

    # Граница безопасной зоны (отступ автопилота)
    o = 60
    draw.rectangle([o, o, robot_config.map_width-o, robot_config.map_height-o],
                   outline=(30, 60, 40), width=1)

    # ── Карта препятствий ────────────────────────────────────────────────
    if sonar_sensor:
        # Симуляция препятствий
        for ox, oy in sonar_sensor._sim_obstacles:
            draw.rectangle([ox-20, oy-20, ox+20, oy+20],
                           fill=(40, 40, 40), outline=(70, 70, 70), width=1)
            # Крестик на препятствии
            draw.line([(ox-8, oy-8), (ox+8, oy+8)], fill=(100, 100, 100), width=1)
            draw.line([(ox+8, oy-8), (ox-8, oy+8)], fill=(100, 100, 100), width=1)

        # Облако точек сонара (карта препятствий)
        with sonar_sensor.lock:
            pts = list(sonar_sensor.radar_points)
        for pt in pts:
            if pt['dist'] < 250:
                px = pt['x'] + math.sin(math.radians(pt['angle'])) * pt['dist']
                py = pt['y'] - math.cos(math.radians(pt['angle'])) * pt['dist']
                if 0 <= px < robot_config.map_width and 0 <= py < robot_config.map_height:
                    draw.ellipse([px-3, py-3, px+3, py+3], fill=(180, 90, 20))

    # ── Луч сонара ──────────────────────────────────────────────────────
    if sonar_sensor and robot_simulator:
        sonar_data = sonar_sensor.get_status()
        dist    = sonar_data['distance_cm']
        rx, ry  = robot_simulator.x, robot_simulator.y
        angle_r = math.radians(robot_simulator.angle)
        cone_dist = min(dist, 200)

        # Конус обзора
        for a_off in range(-30, 31, 5):
            ar = math.radians(robot_simulator.angle + a_off)
            ex = rx + math.sin(ar) * cone_dist
            ey = ry - math.cos(ar) * cone_dist
            alpha = max(8, 35 - abs(a_off))
            draw.line([(rx, ry), (ex, ey)], fill=(0, alpha, 0), width=1)

        # Центральный луч
        end_x = rx + math.sin(angle_r) * cone_dist
        end_y = ry - math.cos(angle_r) * cone_dist
        draw.line([(rx, ry), (end_x, end_y)], fill=(0, 220, 70), width=2)

        # Маркер препятствия
        if dist < 200:
            ox = rx + math.sin(angle_r) * dist
            oy = ry - math.cos(angle_r) * dist
            r = 5
            color = (255, 50, 50) if dist < 30 else (255, 160, 0)
            draw.ellipse([ox-r, oy-r, ox+r, oy+r], fill=color, outline=(255, 255, 255))

        # Дистанция
        col = (255, 50, 50) if dist < 30 else (180, 255, 100)
        draw.text((10, 50), f"Сонар: {dist:.0f} см", fill=col)
        if dist < 30:
            draw.text((10, 68), "⚠ ПРЕПЯТСТВИЕ", fill=(255, 50, 50))

    # ── История пути ──────────────────────────────────────────────────────
    path_list = list(robot_simulator.path_history)
    if len(path_list) > 1:
        # Градиент пути: старое — тускло, новое — ярко
        total = len(path_list)
        for i in range(1, total):
            t = i / total
            r = int(40 + t * 30)
            g = int(80 + t * 100)
            b = int(50 + t * 30)
            draw.line([path_list[i-1], path_list[i]], fill=(r, g, b), width=2)

    # ── Найденные люди ────────────────────────────────────────────────────
    for human in robot_simulator.found_humans:
        x, y = human['x'], human['y']
        if 0 <= x < robot_config.map_width and 0 <= y < robot_config.map_height:
            # Пульсирующий маркер (несколько колец)
            for ring, alpha in [(18, 30), (13, 60), (8, 120)]:
                draw.ellipse([x-ring, y-ring, x+ring, y+ring],
                             outline=(200, 60, 60, alpha), width=1)
            draw.ellipse([x-8, y-8, x+8, y+8],
                         fill=(200, 60, 60), outline=(255, 150, 100), width=2)
            draw.text((x-4, y-7), str(human['id']), fill=(255, 255, 255))

    # ── Робот ─────────────────────────────────────────────────────────────
    rx = max(0, min(robot_config.map_width  - 1, robot_simulator.x))
    ry = max(0, min(robot_config.map_height - 1, robot_simulator.y))
    robot_simulator.x, robot_simulator.y = rx, ry

    size    = robot_config.robot_size
    angle_r = math.radians(robot_simulator.angle)

    # Тень
    draw.ellipse([rx-size-2, ry-size-2, rx+size+2, ry+size+2],
                 fill=(0, 40, 20))
    # Тело
    draw.ellipse([rx-size, ry-size, rx+size, ry+size],
                 fill=(60, 200, 100), outline=(0, 255, 80), width=2)
    # Направление
    ex = rx + size * 1.8 * math.sin(angle_r)
    ey = ry - size * 1.8 * math.cos(angle_r)
    draw.line([(rx, ry), (ex, ey)], fill=(0, 200, 255), width=3)
    # Глаз
    draw.ellipse([ex-3, ey-3, ex+3, ey+3], fill=(255, 255, 100))

    # ── Статус автопилота ─────────────────────────────────────────────────
    if autopilot and autopilot.enabled:
        режим_цвет = {
            'ПОИСК':   (0, 200, 100),
            'ОБЪЕЗД':  (255, 160, 0),
            'НАЙДЕН':  (255, 50, 50),
            'РАЗВОРОТ':(100, 150, 255),
        }
        col = режим_цвет.get(autopilot.режим, (150, 150, 150))
        draw.rectangle([robot_config.map_width-130, 8,
                        robot_config.map_width-8, 26],
                       fill=(0, 0, 0, 180))
        draw.text((robot_config.map_width-125, 10),
                  f"АВТО: {autopilot.режим}", fill=col)

    # ── Инфо (левый верхний угол) ─────────────────────────────────────────
    draw.text((10, 10), f"({int(rx)}, {int(ry)})  {int(robot_simulator.angle)}°",
              fill=(60, 180, 100))
    draw.text((10, 28), f"Путь: {len(path_list)} точек",
              fill=(40, 120, 70))

    return img

def capture_and_process_video():
    """Захват видео с детектором лиц и движения"""
    global current_frame
    frame_count = 0
    while True:
        try:
            frame_bytes = camera_manager.get_frame()
            if frame_bytes:
                img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
                with frame_lock:
                    current_frame = img

                frame_count += 1

                # Детектор движения (каждый кадр)
                if motion_detector:
                    motion_detector.process(img)

                # Детектор лиц (каждый 5-й кадр)
                if frame_count % 5 == 0 and face_detector:
                    faces = face_detector.detect_async(img)
                    if robot_simulator:
                        robot_simulator.detected_faces = faces or []
                        if faces:
                            robot_simulator.add_human_detection(
                                robot_simulator.x,
                                robot_simulator.y,
                                img
                            )

                # Тепловая карта (каждые 10 кадров)
                if frame_count % 10 == 0 and heatmap and robot_simulator:
                    heatmap.update(robot_simulator.x, robot_simulator.y)

            time.sleep(0.05)
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>М.А.Р.С.</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg:        #03070f;
            --bg2:       #060d1a;
            --bg3:       #0a1525;
            --accent:    #00c8ff;
            --accent2:   #00ff9d;
            --accent3:   #ff4060;
            --warn:      #ffb020;
            --text:      #8ab8d8;
            --text2:     #4a7a9a;
            --border:    rgba(0,200,255,0.15);
            --border2:   rgba(0,255,157,0.15);
            --glow:      0 0 20px rgba(0,200,255,0.2);
            --glow2:     0 0 20px rgba(0,255,157,0.2);
        }

        * { margin:0; padding:0; box-sizing:border-box; }

        body {
            font-family: 'Share Tech Mono', 'Courier New', monospace;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
        }

        /* Фоновая анимация — сканирующая линия */
        body::before {
            content: '';
            position: fixed;
            top: 0; left: 0; right: 0;
            height: 2px;
            background: linear-gradient(90deg, transparent, var(--accent), transparent);
            animation: scanline 4s linear infinite;
            z-index: 9998;
            opacity: 0.4;
            pointer-events: none;
        }

        @keyframes scanline {
            0%   { top: 0; }
            100% { top: 100vh; }
        }

        .container { max-width: 1600px; margin: 0 auto; padding: 12px; }

        /* ── HEADER ─────────────────────────────────────────────── */
        header {
            text-align: center;
            padding: 20px 24px;
            margin-bottom: 16px;
            background: linear-gradient(180deg, rgba(0,40,60,0.6) 0%, rgba(3,7,15,0.9) 100%);
            border: 1px solid var(--border);
            border-radius: 16px;
            position: relative;
            overflow: hidden;
            box-shadow: var(--glow), inset 0 1px 0 rgba(0,200,255,0.1);
        }

        header::before {
            content: '';
            position: absolute;
            inset: 0;
            background: repeating-linear-gradient(
                0deg,
                transparent,
                transparent 2px,
                rgba(0,200,255,0.015) 2px,
                rgba(0,200,255,0.015) 4px
            );
            pointer-events: none;
        }

        .logo-wrap {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 20px;
        }

        .logo-icon {
            font-size: 40px;
            filter: drop-shadow(0 0 12px var(--accent));
            animation: pulse-icon 3s ease-in-out infinite;
        }

        @keyframes pulse-icon {
            0%,100% { filter: drop-shadow(0 0 8px var(--accent)); }
            50%      { filter: drop-shadow(0 0 20px var(--accent)); }
        }

        h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 36px;
            font-weight: 900;
            letter-spacing: 8px;
            background: linear-gradient(135deg, var(--accent) 0%, var(--accent2) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            filter: drop-shadow(0 0 10px rgba(0,200,255,0.5));
        }

        .header-right { text-align: left; }

        .subtitle {
            font-size: 11px;
            color: var(--text2);
            letter-spacing: 3px;
            margin-top: 3px;
            text-transform: uppercase;
        }

        .version-badge {
            display: inline-block;
            background: rgba(0,200,255,0.1);
            border: 1px solid var(--border);
            color: var(--accent);
            font-size: 10px;
            padding: 2px 8px;
            border-radius: 20px;
            letter-spacing: 1px;
            margin-top: 6px;
        }

        /* ── STATUS BAR ─────────────────────────────────────────── */
        .status-bar {
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 8px;
            margin-bottom: 14px;
        }

        .stat {
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 12px 8px;
            text-align: center;
            position: relative;
            overflow: hidden;
            transition: all 0.3s;
        }

        .stat::before {
            content: '';
            position: absolute;
            bottom: 0; left: 0; right: 0;
            height: 2px;
            background: linear-gradient(90deg, transparent, var(--accent), transparent);
            opacity: 0.5;
        }

        .stat:hover {
            border-color: rgba(0,200,255,0.4);
            box-shadow: var(--glow);
            transform: translateY(-2px);
        }

        .stat-label {
            font-size: 9px;
            color: var(--text2);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 6px;
        }

        .stat-value {
            font-family: 'Orbitron', sans-serif;
            font-size: 20px;
            font-weight: 700;
            color: var(--accent);
        }

        .stat-value.good  { color: var(--accent2); }
        .stat-value.warn  { color: var(--warn); }
        .stat-value.alert {
            color: var(--accent3);
            animation: blink-alert 0.8s infinite;
        }

        @keyframes blink-alert {
            0%,100% { opacity: 1; }
            50%      { opacity: 0.4; }
        }

        /* ── MAIN GRID ──────────────────────────────────────────── */
        .main-grid {
            display: grid;
            grid-template-columns: 3fr 2fr;
            gap: 12px;
            margin-bottom: 12px;
        }

        @media(max-width:1100px) { .main-grid { grid-template-columns: 1fr; } }

        .panel {
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            position: relative;
            overflow: hidden;
        }

        /* Угловые декоры */
        .panel::before, .panel::after {
            content: '';
            position: absolute;
            width: 12px; height: 12px;
            border-color: var(--accent);
            border-style: solid;
            opacity: 0.6;
        }
        .panel::before { top: 6px; left: 6px; border-width: 1px 0 0 1px; }
        .panel::after  { bottom: 6px; right: 6px; border-width: 0 1px 1px 0; }

        .panel-title {
            font-family: 'Orbitron', sans-serif;
            font-size: 11px;
            color: var(--accent);
            text-transform: uppercase;
            letter-spacing: 2px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .panel-title .dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            background: var(--accent2);
            box-shadow: 0 0 6px var(--accent2);
            animation: pulse-dot 2s infinite;
        }

        @keyframes pulse-dot {
            0%,100% { opacity: 1; transform: scale(1); }
            50%      { opacity: 0.5; transform: scale(0.7); }
        }

        /* ── CANVAS ─────────────────────────────────────────────── */
        #mapCanvas {
            width: 100%;
            height: auto;
            border-radius: 8px;
            border: 1px solid var(--border);
            display: block;
            cursor: crosshair;
            box-shadow: inset 0 0 30px rgba(0,0,0,0.5);
        }

        #videoImg {
            width: 100%;
            border-radius: 8px;
            border: 1px solid var(--border);
            display: block;
            background: #000;
        }

        /* ── SELECT ─────────────────────────────────────────────── */
        select {
            width: 100%;
            background: var(--bg3);
            color: var(--accent);
            border: 1px solid var(--border);
            padding: 8px 10px;
            border-radius: 8px;
            margin-top: 8px;
            font-family: inherit;
            font-size: 11px;
            cursor: pointer;
            outline: none;
        }

        select:hover, select:focus {
            border-color: rgba(0,200,255,0.4);
            box-shadow: var(--glow);
        }

        /* ── BUTTONS ────────────────────────────────────────────── */
        button {
            background: rgba(0,40,60,0.7);
            color: var(--accent2);
            border: 1px solid var(--border2);
            padding: 12px;
            font-family: 'Orbitron', sans-serif;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
            border-radius: 8px;
            transition: all 0.15s;
            text-transform: uppercase;
            letter-spacing: 1px;
            position: relative;
            overflow: hidden;
        }

        button::before {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, transparent 40%, rgba(0,255,157,0.05));
            pointer-events: none;
        }

        button:hover {
            background: rgba(0,60,80,0.9);
            border-color: var(--accent2);
            box-shadow: var(--glow2);
            transform: translateY(-1px);
        }

        button:active { transform: scale(0.96); }

        button.stop-btn {
            background: rgba(60,10,20,0.8);
            color: var(--accent3);
            border-color: rgba(255,60,80,0.3);
        }

        button.stop-btn:hover {
            background: rgba(100,20,30,0.9);
            border-color: var(--accent3);
            box-shadow: 0 0 20px rgba(255,60,80,0.3);
        }

        button.auto-btn {
            background: rgba(0,20,50,0.8);
            color: var(--accent);
            border-color: var(--border);
        }

        button.auto-btn.active {
            background: rgba(60,10,20,0.8);
            color: var(--accent3);
            border-color: rgba(255,60,80,0.3);
            box-shadow: 0 0 15px rgba(255,60,80,0.2);
        }

        /* ── WASD ───────────────────────────────────────────────── */
        .wasd-key {
            background: rgba(0,40,60,0.6);
            border: 1px solid var(--border);
            border-radius: 6px;
            text-align: center;
            padding: 7px 4px;
            font-family: 'Orbitron', sans-serif;
            font-size: 11px;
            font-weight: 700;
            color: var(--accent);
            transition: all 0.08s;
            user-select: none;
        }

        .wasd-key.stop-key {
            background: rgba(60,10,20,0.6);
            color: var(--accent3);
            border-color: rgba(255,60,80,0.2);
            font-size: 9px;
        }

        /* ── SLIDERS ────────────────────────────────────────────── */
        .speed-row {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }

        .speed-row label {
            font-size: 10px;
            color: var(--text2);
            text-transform: uppercase;
            letter-spacing: 1px;
            white-space: nowrap;
        }

        input[type="range"] {
            flex: 1;
            -webkit-appearance: none;
            appearance: none;
            height: 4px;
            border-radius: 2px;
            background: linear-gradient(90deg, var(--accent2), var(--accent));
            outline: none;
        }

        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 14px; height: 14px;
            border-radius: 50%;
            background: var(--accent);
            box-shadow: 0 0 8px var(--accent);
            cursor: pointer;
        }

        .speed-val {
            font-family: 'Orbitron', sans-serif;
            font-size: 14px;
            font-weight: 700;
            color: var(--accent);
            min-width: 32px;
        }

        /* ── INFO BOX ───────────────────────────────────────────── */
        .info-box {
            background: rgba(0,0,0,0.4);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 14px;
            font-size: 11px;
            color: var(--text2);
            line-height: 1.9;
        }

        .info-box strong { color: var(--accent); }

        /* ── HUMANS LIST ────────────────────────────────────────── */
        .humans-list {
            max-height: 150px;
            overflow-y: auto;
        }

        .human-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 7px 8px;
            border-radius: 6px;
            cursor: pointer;
            transition: background 0.2s;
            border-bottom: 1px solid rgba(0,200,255,0.06);
        }

        .human-item:hover { background: rgba(0,200,255,0.06); }

        .human-thumb {
            width: 48px; height: 36px;
            object-fit: cover;
            border-radius: 4px;
            border: 1px solid var(--border);
            background: #0a1525;
        }

        .human-info { font-size: 11px; color: var(--text2); }
        .human-info strong { color: var(--accent2); display: block; }

        /* ── CAMERA STATUS ──────────────────────────────────────── */
        .camera-status {
            font-size: 10px;
            color: var(--text2);
            text-align: center;
            margin-top: 6px;
            padding: 4px;
            letter-spacing: 1px;
        }

        /* ── AUTOPILOT STATUS ───────────────────────────────────── */
        .ap-status {
            display: none;
            background: rgba(0,40,30,0.6);
            border: 1px solid var(--border2);
            border-radius: 6px;
            padding: 8px;
            font-size: 11px;
            color: var(--accent2);
            margin-bottom: 10px;
            text-align: center;
            letter-spacing: 1px;
        }

        /* ── EVENT LOG ──────────────────────────────────────────── */
        .event-log {
            background: rgba(0,0,0,0.5);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 8px 10px;
            font-size: 10px;
            max-height: 110px;
            overflow-y: auto;
            line-height: 1.7;
            font-family: 'Share Tech Mono', monospace;
        }

        /* ── POPUP ──────────────────────────────────────────────── */
        .popup-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            z-index: 999;
            backdrop-filter: blur(4px);
        }

        .human-popup {
            display: none;
            position: fixed;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            background: var(--bg2);
            border: 1px solid var(--accent);
            border-radius: 16px;
            padding: 24px;
            z-index: 1000;
            min-width: 300px;
            box-shadow: 0 0 60px rgba(0,200,255,0.3);
        }

        .popup-close {
            position: absolute;
            top: 12px; right: 16px;
            cursor: pointer;
            color: var(--accent);
            font-size: 18px;
            opacity: 0.7;
        }

        .popup-close:hover { opacity: 1; }

        /* ── SCROLLBARS ─────────────────────────────────────────── */
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb {
            background: rgba(0,200,255,0.3);
            border-radius: 2px;
        }

        /* Светлая тема */
        body.light {
            --bg:     #f0f4f8;
            --bg2:    #e2eaf2;
            --bg3:    #d4dfe8;
            --accent: #0066aa;
            --accent2:#007755;
            --accent3:#cc2233;
            --warn:   #cc7700;
            --text:   #334455;
            --text2:  #667788;
            --border: rgba(0,100,180,0.2);
            --border2:rgba(0,120,90,0.2);
        }

        body.light header { background: rgba(200,220,240,0.8); }
        body.light .panel  { background: var(--bg2); }
        body.light .stat   { background: var(--bg3); }
        body.light .event-log { background: rgba(200,210,220,0.5); }
        body.light .info-box  { background: rgba(200,210,220,0.5); }

        /* Карточки статистики */
        .stat-card {
            background: var(--bg3);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            text-align: center;
        }

        .sc-label {
            font-size: 10px;
            color: var(--text2);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 6px;
        }

        .sc-val {
            font-family: 'Orbitron', sans-serif;
            font-size: 18px;
            font-weight: 700;
            color: var(--accent);
        }
    </style>
</head>
<body>
<div class="container">

    <!-- HEADER -->
    <header>
        <div class="logo-wrap">
            <div class="logo-icon">🤖</div>
            <div>
                <h1>М.А.Р.С.</h1>
                <div class="subtitle">Мобильный Автоматизированный Робот Спасатель</div>
                <div class="version-badge">v2.3 · Orange Pi H3</div>
            </div>
        </div>
    </header>

    <!-- ИНФО-ПОЛОСКА (плавающая) -->
    <div id="infoStrip" style="
        position: sticky; top: 0; z-index: 100;
        background: rgba(3,7,15,0.92);
        backdrop-filter: blur(12px);
        border-bottom: 1px solid var(--border);
        padding: 8px 16px;
        display: flex;
        align-items: center;
        gap: 20px;
        font-size: 11px;
        overflow-x: auto;
        white-space: nowrap;
        margin: -12px -12px 14px -12px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    ">
        <!-- Логотип в полоске -->
        <span style="font-family:Orbitron,sans-serif;font-weight:900;
                     font-size:14px;color:var(--accent);letter-spacing:3px;
                     flex-shrink:0;">М.А.Р.С.</span>

        <span style="color:var(--border);flex-shrink:0;">│</span>

        <!-- GPIO -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">GPIO:</span>
            <span id="s_gpio" style="color:var(--warn);font-weight:bold;">SIM</span>
        </span>

        <!-- Батарея с прогрессом -->
        <span style="flex-shrink:0;display:flex;align-items:center;gap:6px;">
            <span style="color:var(--text2);">🔋</span>
            <div style="width:50px;height:8px;background:rgba(255,255,255,0.1);
                        border-radius:4px;overflow:hidden;border:1px solid var(--border);">
                <div id="s_batt_bar" style="height:100%;width:100%;
                     background:linear-gradient(90deg,var(--accent2),var(--accent));
                     border-radius:4px;transition:width 0.5s;"></div>
            </div>
            <span id="s_batt" style="color:var(--accent2);font-weight:bold;">100%</span>
        </span>

        <span style="color:var(--border);flex-shrink:0;">│</span>

        <!-- Позиция -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">📍</span>
            <span id="s_pos" style="color:var(--accent);">—</span>
        </span>

        <!-- Угол -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">🧭</span>
            <span id="s_angle" style="color:var(--accent);">—</span>
        </span>

        <!-- Скорость -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">⚡</span>
            <span id="s_speed" style="color:var(--accent);">—</span>
        </span>

        <span style="color:var(--border);flex-shrink:0;">│</span>

        <!-- Статус -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">Статус:</span>
            <span id="s_cmd" style="color:var(--accent2);font-weight:bold;">СТОП</span>
        </span>

        <!-- Автопилот -->
        <span id="s_auto" style="flex-shrink:0;display:none;
              padding:2px 8px;border-radius:10px;
              background:rgba(0,200,100,0.1);border:1px solid rgba(0,200,100,0.3);
              color:var(--accent2);">
            🤖 АВТО
        </span>

        <span style="color:var(--border);flex-shrink:0;">│</span>

        <!-- Сонар -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">📡</span>
            <span id="s_sonar" style="color:var(--accent);">—</span>
        </span>

        <!-- Найдено -->
        <span id="s_found_wrap" style="flex-shrink:0;">
            <span style="color:var(--text2);">👥</span>
            <span id="s_found" style="color:var(--accent3);font-weight:bold;">0</span>
        </span>

        <!-- FPS -->
        <span style="flex-shrink:0;margin-left:auto;">
            <span style="color:var(--text2);">FPS:</span>
            <span id="s_fps" style="color:var(--text2);">—</span>
        </span>

        <!-- Время -->
        <span style="flex-shrink:0;">
            <span style="color:var(--text2);">⏱</span>
            <span id="s_time" style="color:var(--text2);">0:00</span>
        </span>
    </div>

    <!-- STATUS BAR (компактный, только главное) -->
    <div class="status-bar">
        <div class="stat">
            <div class="stat-label">GPIO моторы</div>
            <div class="stat-value" id="gpioStatus">SIM</div>
        </div>
        <div class="stat">
            <div class="stat-label">Камера FPS</div>
            <div class="stat-value" id="fpsStat">—</div>
        </div>
        <div class="stat">
            <div class="stat-label">Лиц в кадре</div>
            <div class="stat-value" id="facesStat">0</div>
        </div>
        <div class="stat">
            <div class="stat-label">Найдено людей</div>
            <div class="stat-value alert" id="foundStat">0</div>
        </div>
        <div class="stat">
            <div class="stat-label">Покрытие</div>
            <div class="stat-value" id="coverStat">—</div>
        </div>
        <div class="stat">
            <div class="stat-label">Пройдено</div>
            <div class="stat-value" id="distStat">—</div>
        </div>
    </div>

    <!-- MAIN GRID -->
    <div class="main-grid">

        <!-- MAP -->
        <div class="panel">
            <div class="panel-title">
                <span class="dot"></span>
                Навигационная карта — нажми на маркер
            </div>
            <canvas id="mapCanvas" width="800" height="600"></canvas>
        </div>

        <!-- RIGHT COLUMN -->
        <div style="display:flex;flex-direction:column;gap:12px;">

            <!-- CAMERA -->
            <div class="panel">
                <div class="panel-title">
                    <span class="dot"></span>
                    Live камера
                    <span id="camStatus" style="margin-left:auto;font-size:9px;color:var(--text2);">Loading...</span>
                </div>
                <img id="videoImg" src="" alt="Camera">
                <select id="camSelect" onchange="changeCamera(this.value)">
                    <option value="">📷 Выбрать камеру...</option>
                </select>
            </div>

            <!-- SONAR -->
            <div class="panel">
                <div class="panel-title">
                    <span class="dot"></span>
                    Сонар HY-SRF05
                    <span id="sonarMode" style="margin-left:auto;font-size:9px;
                          padding:2px 8px;border-radius:10px;
                          background:rgba(255,180,0,0.15);
                          color:var(--warn);border:1px solid rgba(255,180,0,0.3);">
                        СИМ
                    </span>
                </div>
                <div style="display:flex;align-items:center;gap:16px;">
                    <canvas id="radarCanvas" width="160" height="160"
                            style="border-radius:50%;flex-shrink:0;"></canvas>
                    <div style="flex:1;">
                        <div id="sonarWarn"
                             style="font-family:Orbitron,sans-serif;font-size:13px;
                                    font-weight:700;color:var(--accent2);
                                    letter-spacing:1px;margin-bottom:8px;">📡 —</div>
                        <div style="font-size:11px;color:var(--text2);line-height:2;">
                            Дальность: <strong id="sonarDist" style="color:var(--accent);">—</strong><br>
                            Препятствие: <strong id="sonarObs" style="color:var(--accent2);">нет</strong>
                        </div>
                        <!-- Переключатель режима -->
                        <div style="margin-top:10px;display:flex;gap:6px;">
                            <button id="btnSimMode" onclick="setSonarMode('sim')"
                                    style="flex:1;padding:6px;font-size:9px;
                                           background:rgba(255,180,0,0.2);
                                           color:var(--warn);
                                           border-color:rgba(255,180,0,0.4);">
                                🔵 СИМУЛЯЦИЯ
                            </button>
                            <button id="btnRealMode" onclick="setSonarMode('real')"
                                    style="flex:1;padding:6px;font-size:9px;
                                           background:rgba(0,60,40,0.4);
                                           color:var(--text2);
                                           border-color:var(--border);">
                                🟢 РЕАЛЬНЫЙ
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- FOUND HUMANS -->
            <div class="panel">
                <div class="panel-title">
                    <span class="dot"></span>
                    Обнаруженные люди
                    <span style="margin-left:auto;font-family:'Orbitron',sans-serif;
                                 color:var(--accent3);" id="humanCount">0</span>
                </div>
                <div class="humans-list" id="humansList">
                    <div style="color:var(--text2);font-size:11px;padding:10px;text-align:center;">
                        Людей не обнаружено
                    </div>
                </div>
            </div>

        </div>
    </div>

    <!-- CONTROL PANEL -->
    <div class="panel">
        <div class="panel-title">
            <span class="dot"></span>
            Панель управления
            <span style="margin-left:8px;font-size:9px;color:var(--text2);
                         font-family:'Share Tech Mono',monospace;letter-spacing:1px;">
                WASD / ↑↓←→ · SPACE=стоп · Q=авто · P=снимок
            </span>
        </div>

        <div style="display:grid;grid-template-columns:auto 1fr auto;gap:20px;align-items:start;">

            <!-- WASD -->
            <div style="display:grid;grid-template-columns:repeat(3,38px);
                        grid-template-rows:repeat(3,38px);gap:4px;">
                <div></div>
                <div class="wasd-key" id="kW">W</div>
                <div></div>
                <div class="wasd-key" id="kA">A</div>
                <div class="wasd-key stop-key" id="kSP">SPC</div>
                <div class="wasd-key" id="kD">D</div>
                <div></div>
                <div class="wasd-key" id="kS">S</div>
                <div></div>
            </div>

            <!-- CENTER CONTROLS -->
            <div>
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
                            margin-bottom:12px;">
                    <div></div>
                    <button onmousedown="cmd('forward')" onmouseup="cmd('stop')"
                            ontouchstart="cmd('forward')" ontouchend="cmd('stop')">⬆ FWD</button>
                    <div></div>
                    <button onmousedown="cmd('left')" onmouseup="cmd('stop')"
                            ontouchstart="cmd('left')" ontouchend="cmd('stop')">⬅ LEFT</button>
                    <button class="stop-btn" onclick="cmd('stop')">⛔ STOP</button>
                    <button onmousedown="cmd('right')" onmouseup="cmd('stop')"
                            ontouchstart="cmd('right')" ontouchend="cmd('stop')">RIGHT ➡</button>
                    <div></div>
                    <button onmousedown="cmd('backward')" onmouseup="cmd('stop')"
                            ontouchstart="cmd('backward')" ontouchend="cmd('stop')">⬇ BWD</button>
                    <div></div>
                </div>

                <div class="speed-row">
                    <label>⚡ Скорость</label>
                    <input type="range" id="speedSlider" min="0" max="255" value="150"
                           oninput="setSpeed(this.value)">
                    <span class="speed-val" id="speedVal">150</span>
                </div>

                <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:8px;margin-bottom:12px;">
                    <button id="apBtn" class="auto-btn" onclick="toggleAutopilot()">
                        🤖 АВТО ПОИСК
                    </button>
                    <button onclick="takeScreenshot()"
                            style="color:#a080ff;border-color:rgba(160,128,255,0.3);">
                        📸 СНИМОК
                    </button>
                    <button onclick="exportMap()"
                            style="color:var(--accent);border-color:var(--border);"
                            title="Скачать карту PNG">
                        🗺 КАРТА
                    </button>
                    <button onclick="openReport()"
                            style="color:#80ff80;border-color:rgba(128,255,128,0.3);"
                            title="Сформировать отчёт PDF">
                        📄 ОТЧЁТ
                    </button>
                    <button onclick="clearPath()"
                            style="color:var(--text2);border-color:var(--border);padding:12px 10px;">
                        🗑
                    </button>
                </div>

                <div class="ap-status" id="autopilotStatus">
                    🤖 Автопилот активен — <span id="apMode">ПОИСК</span>
                </div>
            </div>

            <!-- INFO + LOG -->
            <div style="min-width:220px;">
                <div class="info-box" id="infoBox" style="margin-bottom:10px;">
                    <strong>Позиция:</strong> загрузка...<br>
                    <strong>Статус:</strong> готов
                </div>
                <div style="font-size:9px;color:var(--text2);letter-spacing:1px;
                            text-transform:uppercase;margin-bottom:4px;">
                    📋 Журнал событий
                    <span id="logDir" style="color:var(--text2);opacity:0.5;margin-left:4px;"></span>
                </div>
                <div class="event-log" id="eventLog">Ожидание событий...</div>
            </div>
        </div>
    </div>

</div>

<!-- СТАТИСТИКА -->
<div class="panel" style="margin-top:12px;">
    <div class="panel-title">
        <span class="dot"></span>
        Статистика сессии
        <button onclick="toggleTheme()" style="margin-left:auto;padding:4px 10px;
                font-size:10px;border-color:var(--border);color:var(--text2);">
            🌙 Тема
        </button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;"
         id="statsGrid">
        <div class="stat-card" id="sc-time">
            <div class="sc-label">Время работы</div>
            <div class="sc-val" id="st-uptime">—</div>
        </div>
        <div class="stat-card" id="sc-dist">
            <div class="sc-label">Пройдено</div>
            <div class="sc-val" id="st-dist">—</div>
        </div>
        <div class="stat-card">
            <div class="sc-label">Покрытие</div>
            <div class="sc-val" id="st-cover">—</div>
        </div>
        <div class="stat-card">
            <div class="sc-label">Точек пути</div>
            <div class="sc-val" id="st-path">—</div>
        </div>
        <div class="stat-card">
            <div class="sc-label">Найдено людей</div>
            <div class="sc-val" id="st-humans" style="color:var(--accent3);">0</div>
        </div>
        <div class="stat-card">
            <div class="sc-label">Сессия</div>
            <div class="sc-val" id="st-session" style="font-size:11px;">—</div>
        </div>
    </div>
</div>

<!-- HUMAN POPUP -->
<div class="popup-overlay" id="popupOverlay" onclick="closePopup()"></div>
<div class="human-popup" id="humanPopup">
    <span class="popup-close" onclick="closePopup()">✕</span>
    <div id="popupContent"></div>
</div>

<script>
const mapCanvas = document.getElementById('mapCanvas');
const mapCtx    = mapCanvas.getContext('2d');
const videoImg  = document.getElementById('videoImg');

let lastHumans   = [];
let frameCount   = 0;
let fpsTimer     = performance.now();
let mapHumans    = [];
let lastFoundCount = 0;
let notifAudio   = null;
let autopilotOn  = false;

// ── Видео ─────────────────────────────────────────────────────────────────
let videoActive = false;
function videoLoop() {
    if (videoActive) return;
    videoActive = true;
    const img = new Image();
    img.onload = () => {
        videoImg.src = img.src;
        const camSt = document.getElementById('camStatus');
        if (camSt) { camSt.textContent = '● LIVE'; camSt.style.color = 'var(--accent2)'; }
        frameCount++;
        const now = performance.now();
        if (now - fpsTimer >= 1000) {
            const fps = (frameCount * 1000 / (now - fpsTimer)).toFixed(0);
            const fpsSt = document.getElementById('fpsStat');
            if (fpsSt) fpsSt.textContent = fps;
            const sFps = document.getElementById('s_fps');
            if (sFps) sFps.textContent = fps;
            frameCount = 0; fpsTimer = now;
        }
        videoActive = false;
        setTimeout(videoLoop, 150);
    };
    img.onerror = () => {
        const camSt = document.getElementById('camStatus');
        if (camSt) { camSt.textContent = '○ NO SIGNAL'; camSt.style.color = 'var(--accent3)'; }
        videoActive = false;
        setTimeout(videoLoop, 500);
    };
    img.src = '/api/camera/frame?t=' + Date.now();
}

// ── Карта ─────────────────────────────────────────────────────────────────
function mapLoop() {
    const img = new Image();
    img.onload = () => {
        mapCtx.drawImage(img, 0, 0, mapCanvas.width, mapCanvas.height);
        URL.revokeObjectURL(img.src);
    };
    fetch('/api/map').then(r => r.blob()).then(b => { img.src = URL.createObjectURL(b); }).catch(() => {});
    setTimeout(mapLoop, 500);
}

// ── Состояние ────────────────────────────────────────────────────────────
async function stateLoop() {
    try {
        const r = await fetch('/api/robot/state');
        const d = await r.json();
        const s = d.state;
        const found = s.found_humans ? s.found_humans.length : 0;

        // ── Статус-бар ────────────────────────────────────────────────────
        const gpio = document.getElementById('gpioStatus');
        if (gpio) {
            gpio.textContent = s.gpio_enabled ? '🟢 ON' : 'SIM';
            gpio.className   = 'stat-value ' + (s.gpio_enabled ? 'good' : 'warn');
        }
        const facesEl = document.getElementById('facesStat');
        if (facesEl) facesEl.textContent = s.face_count || 0;

        const foundEl = document.getElementById('foundStat');
        if (foundEl) {
            foundEl.textContent = found;
            foundEl.className = 'stat-value ' + (found > 0 ? 'alert' : 'good');
        }

        // ── Инфо-полоска ──────────────────────────────────────────────────
        // GPIO
        const sgpio = document.getElementById('s_gpio');
        if (sgpio) {
            sgpio.textContent = s.gpio_enabled ? '🟢 ON' : 'SIM';
            sgpio.style.color = s.gpio_enabled ? 'var(--accent2)' : 'var(--warn)';
        }

        // Батарея
        const sbatt = document.getElementById('s_batt');
        if (sbatt) sbatt.textContent = s.battery + '%';
        const battBar = document.getElementById('s_batt_bar');
        if (battBar) {
            battBar.style.width = s.battery + '%';
            battBar.style.background = s.battery > 30
                ? 'linear-gradient(90deg,var(--accent2),var(--accent))'
                : s.battery > 10 ? 'linear-gradient(90deg,#ff8800,#ffbb00)'
                : 'linear-gradient(90deg,#ff2040,#ff6060)';
        }

        // Позиция, угол, скорость
        const spos   = document.getElementById('s_pos');
        const sangle = document.getElementById('s_angle');
        const sspeed = document.getElementById('s_speed');
        if (spos)   spos.textContent   = `(${s.x.toFixed(0)}, ${s.y.toFixed(0)})`;
        if (sangle) sangle.textContent = s.angle.toFixed(0) + '°';
        if (sspeed) sspeed.textContent = s.current_speed + '/255';

        // Команда
        const cmdEl = document.getElementById('s_cmd');
        if (cmdEl) {
            const cmdRu = {'FORWARD':'ВПЕРЁД','BACKWARD':'НАЗАД',
                           'LEFT':'ВЛЕВО','RIGHT':'ВПРАВО','STOP':'СТОП'};
            const cmdClr = {'FORWARD':'var(--accent2)','BACKWARD':'var(--warn)',
                            'LEFT':'var(--accent)','RIGHT':'var(--accent)','STOP':'var(--text2)'};
            cmdEl.textContent = cmdRu[s.current_command] || s.current_command;
            cmdEl.style.color = cmdClr[s.current_command] || 'var(--text2)';
        }

        // Автопилот в полоске
        const sauto = document.getElementById('s_auto');
        if (sauto) {
            if (s.autopilot && s.autopilot.enabled) {
                sauto.style.display = 'inline';
                sauto.textContent = '🤖 ' + (s.autopilot.mode || 'АВТО');
            } else {
                sauto.style.display = 'none';
            }
        }

        // Найдено в полоске
        const sfound = document.getElementById('s_found');
        if (sfound) {
            sfound.textContent = found;
            sfound.style.color = found > 0 ? 'var(--accent3)' : 'var(--text2)';
        }

        // Время
        const stime = document.getElementById('s_time');
        if (stime) stime.textContent = fmtTime(s.uptime);

        // Автопилот статус-блок
        if (s.autopilot && s.autopilot.enabled) {
            document.getElementById('apMode').textContent = s.autopilot.mode;
            document.getElementById('autopilotStatus').style.display = 'block';
        } else if (!autopilotOn) {
            const apSt = document.getElementById('autopilotStatus');
            if (apSt) apSt.style.display = 'none';
        }

        // Humans list
        if (s.found_humans && s.found_humans.length !== lastHumans.length) {
            lastHumans = s.found_humans;
            updateHumansList(s.found_humans);
            mapHumans = s.found_humans;
        }
        const hcEl = document.getElementById('humanCount');
        if (hcEl) hcEl.textContent = found;

        // infoBox
        const infoBox = document.getElementById('infoBox');
        if (infoBox) {
            infoBox.innerHTML =
                `<strong>Позиция:</strong> (${s.x.toFixed(0)}, ${s.y.toFixed(0)})<br>` +
                `<strong>Угол:</strong> ${s.angle.toFixed(0)}°  ` +
                `<strong>Скорость:</strong> ${s.current_speed}/255<br>` +
                `<strong>Команда:</strong> ${s.current_command}  ` +
                `<strong>Путь:</strong> ${s.path_length} точек`;
        }

        checkNewHumans(s);

    } catch(e) {}
    setTimeout(stateLoop, 800);
}

// Обновляем FPS в полоске из videoLoop
const _origFpsUpdate = (fps) => {
    const el = document.getElementById('s_fps');
    if (el) el.textContent = fps;
    const fpsSt = document.getElementById('fpsStat');
    if (fpsSt) fpsSt.textContent = fps;
};

function updateHumansList(humans) {
    const list = document.getElementById('humansList');
    if (!humans.length) {
        list.innerHTML = '<div style="color:var(--text2);font-size:11px;padding:10px;text-align:center;">Людей не обнаружено</div>';
        return;
    }
    list.innerHTML = humans.map(h => `
        <div class="human-item" onclick="showHuman(${h.id})">
            <img class="human-thumb" src="/api/humans/photo/${h.id}" onerror="this.style.display='none'">
            <div class="human-info">
                <strong>#${h.id} — Человек обнаружен</strong>
                Позиция: (${Math.round(h.x)}, ${Math.round(h.y)})<br>
                ${new Date(h.timestamp * 1000).toLocaleTimeString()}
            </div>
        </div>
    `).join('');
}

// ── Клик на карту ─────────────────────────────────────────────────────────
mapCanvas.addEventListener('click', function(e) {
    const rect = mapCanvas.getBoundingClientRect();
    const sx = mapCanvas.width / rect.width;
    const sy = mapCanvas.height / rect.height;
    const cx = (e.clientX - rect.left) * sx;
    const cy = (e.clientY - rect.top)  * sy;
    for (const h of mapHumans) {
        if (Math.sqrt((h.x-cx)**2 + (h.y-cy)**2) < 20) { showHuman(h.id); return; }
    }
});

function showHuman(id) {
    const h = mapHumans.find(x => x.id === id);
    if (!h) return;
    document.getElementById('popupContent').innerHTML =
        `<h3 style="color:var(--accent);font-family:Orbitron,sans-serif;
                    margin-bottom:14px;letter-spacing:2px;">👤 ЧЕЛОВЕК #${id}</h3>` +
        `<img src="/api/humans/photo/${id}" onerror="this.remove()"
              style="width:100%;border-radius:8px;margin-bottom:12px;
                     border:1px solid var(--border);">` +
        `<div style="font-size:12px;color:var(--text2);line-height:1.9;">` +
        `<strong style="color:var(--accent)">Время:</strong> ${new Date(h.timestamp*1000).toLocaleTimeString()}<br>` +
        `<strong style="color:var(--accent)">Позиция на карте:</strong> (${Math.round(h.x)}, ${Math.round(h.y)})` +
        `</div>`;
    document.getElementById('popupOverlay').style.display = 'block';
    document.getElementById('humanPopup').style.display   = 'block';
}

function closePopup() {
    document.getElementById('popupOverlay').style.display = 'none';
    document.getElementById('humanPopup').style.display   = 'none';
}

// ── Радар ─────────────────────────────────────────────────────────────────
let radarCanvas, radarCtx;
function initRadar() {
    radarCanvas = document.getElementById('radarCanvas');
    if (!radarCanvas) return;
    radarCtx = radarCanvas.getContext('2d');
    radarLoop();
}

async function radarLoop() {
    try {
        const r = await fetch('/api/sonar');
        const d = await r.json();
        drawRadar(d);

        const dist = d.distance_cm || 999;
        document.getElementById('sonarDist').textContent =
            dist > 300 ? '> 300 см' : dist.toFixed(0) + ' см';
        document.getElementById('sonarObs').textContent =
            d.obstacle ? '⚠️ ДА' : 'нет';
        document.getElementById('sonarObs').style.color =
            d.obstacle ? 'var(--accent3)' : 'var(--accent2)';
        document.getElementById('sonarWarn').textContent =
            d.obstacle ? `⚠️ ${dist.toFixed(0)} см` :
            `📡 ${dist > 300 ? 'Свободно' : dist.toFixed(0)+' см'}`;
        document.getElementById('sonarWarn').style.color =
            d.obstacle ? 'var(--accent3)' : 'var(--accent2)';

        // Сонар в полоске
        const ssonar = document.getElementById('s_sonar');
        if (ssonar) {
            ssonar.textContent = dist > 300 ? 'свободно' : dist.toFixed(0) + ' см';
            ssonar.style.color = d.obstacle ? 'var(--accent3)' : 'var(--accent)';
        }
        const modeEl = document.getElementById('sonarMode');
        if (d.sim_mode) {
            modeEl.textContent = 'СИМ';
            modeEl.style.color = 'var(--warn)';
            modeEl.style.background = 'rgba(255,180,0,0.15)';
            modeEl.style.borderColor = 'rgba(255,180,0,0.3)';
        } else {
            modeEl.textContent = 'GPIO';
            modeEl.style.color = 'var(--accent2)';
            modeEl.style.background = 'rgba(0,255,157,0.1)';
            modeEl.style.borderColor = 'rgba(0,255,157,0.3)';
        }
    } catch(e) {}
    setTimeout(radarLoop, 200);
}

// ── Переключатель режима сонара ───────────────────────────────────────────
async function setSonarMode(mode) {
    try {
        const r = await fetch('/api/sonar/mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode})
        });
        const d = await r.json();

        const btnSim  = document.getElementById('btnSimMode');
        const btnReal = document.getElementById('btnRealMode');

        if (mode === 'sim') {
            btnSim.style.background  = 'rgba(255,180,0,0.2)';
            btnSim.style.color       = 'var(--warn)';
            btnSim.style.borderColor = 'rgba(255,180,0,0.4)';
            btnReal.style.background = 'rgba(0,60,40,0.4)';
            btnReal.style.color      = 'var(--text2)';
            btnReal.style.borderColor= 'var(--border)';
        } else {
            btnReal.style.background = 'rgba(0,200,100,0.2)';
            btnReal.style.color      = 'var(--accent2)';
            btnReal.style.borderColor= 'rgba(0,255,157,0.4)';
            btnSim.style.background  = 'rgba(0,60,40,0.4)';
            btnSim.style.color       = 'var(--text2)';
            btnSim.style.borderColor = 'var(--border)';
        }

        if (!d.success && mode === 'real') {
            alert('GPIO недоступен! Подключи датчик HY-SRF05.');
        }
    } catch(e) {}
}

function drawRadar(data) {
    if (!radarCtx) return;
    const W = radarCanvas.width, H = radarCanvas.height;
    const cx = W/2, cy = H/2, R = W/2 - 6;
    radarCtx.clearRect(0, 0, W, H);

    // Фон
    const bg = radarCtx.createRadialGradient(cx,cy,0, cx,cy,R);
    bg.addColorStop(0, 'rgba(0,40,30,0.9)');
    bg.addColorStop(1, 'rgba(0,10,5,0.95)');
    radarCtx.fillStyle = bg;
    radarCtx.beginPath(); radarCtx.arc(cx,cy,R,0,Math.PI*2); radarCtx.fill();

    // Сетка
    radarCtx.strokeStyle = 'rgba(0,200,255,0.12)';
    radarCtx.lineWidth = 1;
    for (let i = 1; i <= 4; i++) {
        radarCtx.beginPath(); radarCtx.arc(cx,cy,R*i/4,0,Math.PI*2); radarCtx.stroke();
    }
    radarCtx.beginPath(); radarCtx.moveTo(cx-R,cy); radarCtx.lineTo(cx+R,cy); radarCtx.stroke();
    radarCtx.beginPath(); radarCtx.moveTo(cx,cy-R); radarCtx.lineTo(cx,cy+R); radarCtx.stroke();

    // Подписи дистанций
    radarCtx.fillStyle = 'rgba(0,200,255,0.4)';
    radarCtx.font = '8px monospace';
    ['75','150','225','300'].forEach((label, i) => {
        radarCtx.fillText(label + 'см', cx + 3, cy - R*(i+1)/4 + 3);
    });

    // Текущий угол робота
    const robotAngle = data.robot_angle || 0;
    const robotAngleRad = robotAngle * Math.PI / 180;

    // Исторические точки — относительно угла робота
    for (const pt of (data.radar_points || [])) {
        if (pt.dist >= 300) continue;

        // Относительный угол точки к текущему направлению
        const relAngle = (pt.angle - robotAngle) * Math.PI / 180;
        const distR = (pt.dist / 300) * R;
        const px = cx + Math.sin(relAngle) * distR;
        const py = cy - Math.cos(relAngle) * distR;

        // Цвет зависит от дальности
        const t = 1 - pt.dist / 300;
        radarCtx.fillStyle = `rgba(255,${Math.floor(100+t*100)},0,${0.3+t*0.5})`;
        radarCtx.beginPath();
        radarCtx.arc(px, py, pt.dist < 40 ? 4 : 2, 0, Math.PI*2);
        radarCtx.fill();
    }

    // Конус обзора (вперёд = вверх на радаре)
    for (let a = -30; a <= 30; a += 5) {
        const ar = a * Math.PI / 180;
        const alpha = Math.max(0.02, 0.08 - Math.abs(a) * 0.002);
        radarCtx.strokeStyle = `rgba(0,255,157,${alpha})`;
        radarCtx.lineWidth = 1;
        radarCtx.beginPath(); radarCtx.moveTo(cx,cy);
        radarCtx.lineTo(cx + Math.sin(ar)*R, cy - Math.cos(ar)*R);
        radarCtx.stroke();
    }

    // Центральный луч (вперёд)
    const dist = Math.min(data.distance_cm || 999, 300);
    const lx = cx;
    const ly = cy - (dist/300)*R;
    radarCtx.strokeStyle = 'rgba(0,255,157,0.9)';
    radarCtx.lineWidth = 2;
    radarCtx.beginPath(); radarCtx.moveTo(cx,cy); radarCtx.lineTo(lx,ly); radarCtx.stroke();

    // Точка препятствия
    if (dist < 299) {
        const isClose = data.obstacle;
        radarCtx.fillStyle = isClose ? '#ff3050' : '#ffb020';
        radarCtx.beginPath();
        radarCtx.arc(lx, ly, isClose ? 6 : 4, 0, Math.PI*2);
        radarCtx.fill();
        // Обводка
        radarCtx.strokeStyle = '#ffffff';
        radarCtx.lineWidth = 1;
        radarCtx.stroke();

        // Пульсация если близко
        if (isClose) {
            radarCtx.strokeStyle = 'rgba(255,50,80,0.4)';
            radarCtx.lineWidth = 2;
            radarCtx.beginPath();
            radarCtx.arc(lx, ly, 10 + (Date.now() % 1000) / 100, 0, Math.PI*2);
            radarCtx.stroke();
        }
    }

    // Центр (робот)
    radarCtx.fillStyle = 'var(--accent2)';
    radarCtx.beginPath(); radarCtx.arc(cx,cy,4,0,Math.PI*2); radarCtx.fill();
    // Стрелка направления
    radarCtx.strokeStyle = 'var(--accent2)';
    radarCtx.lineWidth = 2;
    radarCtx.beginPath();
    radarCtx.moveTo(cx, cy);
    radarCtx.lineTo(cx, cy - 10);
    radarCtx.stroke();

    // Рамка
    radarCtx.strokeStyle = 'rgba(0,200,255,0.3)';
    radarCtx.lineWidth = 1.5;
    radarCtx.beginPath(); radarCtx.arc(cx,cy,R,0,Math.PI*2); radarCtx.stroke();

    // Режим в углу
    radarCtx.fillStyle = data.enabled ? 'rgba(0,255,157,0.6)' : 'rgba(255,180,0,0.6)';
    radarCtx.font = '8px monospace';
    radarCtx.fillText(data.enabled ? 'GPIO' : 'СИМ', 4, H - 4);
}

// ── Управление ────────────────────────────────────────────────────────────
async function cmd(c) {
    await fetch('/api/robot/command', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({cmd:c})
    });
}

async function setSpeed(v) {
    document.getElementById('speedVal').textContent = v;
    await fetch('/api/robot/speed', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({speed:parseInt(v)})
    });
}

async function clearPath() {
    await fetch('/api/robot/clear_path', {method:'POST'});
}

async function loadCameras() {
    try {
        const r = await fetch('/api/cameras/list');
        const d = await r.json();
        const sel = document.getElementById('camSelect');
        for (const c of d.devices) {
            const o = document.createElement('option');
            o.value = c.device;
            o.textContent = c.name + ' (' + c.device + ')';
            sel.appendChild(o);
        }
    } catch(e) {}
}

async function changeCamera(dev) {
    if (!dev) return;
    await fetch('/api/cameras/select', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({device:dev})
    });
}

// ── WASD ─────────────────────────────────────────────────────────────────
const keysDown = new Set();

document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (keysDown.has(e.code)) return;
    keysDown.add(e.code);
    switch(e.code) {
        case 'KeyW': case 'ArrowUp':    cmd('forward');  e.preventDefault(); break;
        case 'KeyS': case 'ArrowDown':  cmd('backward'); e.preventDefault(); break;
        case 'KeyA': case 'ArrowLeft':  cmd('left');     e.preventDefault(); break;
        case 'KeyD': case 'ArrowRight': cmd('right');    e.preventDefault(); break;
        case 'Space':                   cmd('stop');     e.preventDefault(); break;
        case 'KeyP':                    takeScreenshot(); break;
        case 'KeyQ':                    toggleAutopilot(); break;
    }
    updateWASD();
});

document.addEventListener('keyup', (e) => {
    keysDown.delete(e.code);
    const moveKeys = ['KeyW','KeyS','KeyA','KeyD','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'];
    if (moveKeys.includes(e.code)) cmd('stop');
    updateWASD();
});

function updateWASD() {
    const map = {
        'KeyW':'kW','ArrowUp':'kW','KeyS':'kS','ArrowDown':'kS',
        'KeyA':'kA','ArrowLeft':'kA','KeyD':'kD','ArrowRight':'kD','Space':'kSP'
    };
    for (const [code, id] of Object.entries(map)) {
        const el = document.getElementById(id);
        if (el) el.style.background = keysDown.has(code)
            ? 'rgba(0,200,255,0.35)' : '';
    }
}

// ── Автопилот ─────────────────────────────────────────────────────────────
async function toggleAutopilot() {
    const btn = document.getElementById('apBtn');
    if (!autopilotOn) {
        const r = await fetch('/api/autopilot/start', {method:'POST'});
        const d = await r.json();
        if (d.success) {
            autopilotOn = true;
            btn.textContent = '⏹ СТОП АВТО';
            btn.classList.add('active');
            document.getElementById('autopilotStatus').style.display = 'block';
        }
    } else {
        await fetch('/api/autopilot/stop', {method:'POST'});
        autopilotOn = false;
        btn.textContent = '🤖 АВТО ПОИСК';
        btn.classList.remove('active');
        document.getElementById('autopilotStatus').style.display = 'none';
    }
}

async function takeScreenshot() {
    await fetch('/api/log/screenshot', {method:'POST'});
    const btn = event ? event.target : null;
    if (btn) {
        const orig = btn.style.background;
        btn.style.background = 'rgba(120,80,200,0.5)';
        setTimeout(() => btn.style.background = orig, 300);
    }
}

// ── Лог ──────────────────────────────────────────────────────────────────
async function logLoop() {
    try {
        const r = await fetch('/api/log/events');
        const d = await r.json();
        if (d.stats && d.stats.session_id)
            document.getElementById('logDir').textContent = d.stats.session_id;
        const logEl = document.getElementById('eventLog');
        if (d.events && d.events.length > 0) {
            const colors = {
                'SESSION_START':'var(--accent2)','AUTOPILOT_START':'var(--accent)',
                'AUTOPILOT_STOP':'var(--warn)','HUMAN_FOUND':'var(--accent3)',
                'OBSTACLE':'var(--warn)','SCREENSHOT':'#a080ff',
                'SPIRAL_TURN':'var(--text2)','AVOID_DONE':'var(--accent2)',
            };
            logEl.innerHTML = d.events.slice().reverse().map(e =>
                `<span style="color:${colors[e.type]||'var(--text2)'};">[${e.time}] ${e.type}</span>` +
                (e.details ? ` <span style="color:var(--text2);opacity:0.7;">${e.details}</span>` : '')
            ).join('<br>');
        }
    } catch(e) {}
    setTimeout(logLoop, 1500);
}

// ── Уведомления ──────────────────────────────────────────────────────────
function initNotifications() {
    try { notifAudio = new (window.AudioContext || window.webkitAudioContext)(); } catch(e) {}
}

function playAlertSound() {
    if (!notifAudio) return;
    [0, 300, 600].forEach(delay => {
        setTimeout(() => {
            try {
                const osc = notifAudio.createOscillator();
                const gain = notifAudio.createGain();
                osc.connect(gain); gain.connect(notifAudio.destination);
                osc.frequency.value = 880; osc.type = 'square';
                gain.gain.setValueAtTime(0.3, notifAudio.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, notifAudio.currentTime + 0.2);
                osc.start(notifAudio.currentTime); osc.stop(notifAudio.currentTime + 0.2);
            } catch(e) {}
        }, delay);
    });
}

function showHumanFoundAlert(count) {
    const flash = document.createElement('div');
    flash.style.cssText = 'position:fixed;inset:0;z-index:9999;pointer-events:none;background:rgba(255,60,80,0.2);animation:flashAnim 0.8s ease-out forwards;';
    document.body.appendChild(flash);
    setTimeout(() => flash.remove(), 1000);

    const notif = document.createElement('div');
    notif.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);background:var(--bg2);border:2px solid var(--accent3);border-radius:14px;padding:18px 28px;z-index:10000;text-align:center;box-shadow:0 0 40px rgba(255,60,80,0.4);animation:slideDown 0.3s ease-out;min-width:300px;';
    notif.innerHTML = `
        <div style="font-size:32px;margin-bottom:8px;">🚨</div>
        <div style="font-family:Orbitron,sans-serif;color:var(--accent3);
                    font-size:20px;font-weight:900;letter-spacing:3px;margin-bottom:6px;">
            ЧЕЛОВЕК НАЙДЕН!</div>
        <div style="color:rgba(255,100,120,0.8);font-size:13px;">
            Обнаружено: ${count} чел. — робот остановлен</div>
        <div style="color:var(--text2);font-size:10px;margin-top:8px;">нажми для закрытия</div>
    `;
    notif.onclick = () => notif.remove();
    document.body.appendChild(notif);
    setTimeout(() => notif && notif.remove(), 5000);
    playAlertSound();

    let blink = 0;
    const orig = document.title;
    const t = setInterval(() => {
        document.title = blink++ % 2 === 0 ? '🚨 ЧЕЛОВЕК НАЙДЕН!' : 'М.А.Р.С.';
        if (blink > 10) { clearInterval(t); document.title = orig; }
    }, 500);
}

function checkNewHumans(state) {
    const found = state.found_humans ? state.found_humans.length : 0;
    if (found > lastFoundCount) { showHumanFoundAlert(found); lastFoundCount = found; }
}

function fmtTime(s) {
    const m = Math.floor(s/60);
    return `${m}:${String(Math.floor(s%60)).padStart(2,'0')}`;
}

// ── Статистика ────────────────────────────────────────────────────────────
async function statsLoop() {
    try {
        const r = await fetch('/api/stats');
        const d = await r.json();

        // Статус-бар
        const coverEl = document.getElementById('coverStat');
        if (coverEl) coverEl.textContent = (d.coverage_pct || 0) + '%';
        const distEl = document.getElementById('distStat');
        if (distEl) distEl.textContent = (d.dist_m || 0) + ' м';

        // Сонар в полоске
        const ssonar = document.getElementById('s_sonar');
        // обновляется в radarLoop

        // Статистика-блок
        const stUptime = document.getElementById('st-uptime');
        if (stUptime) stUptime.textContent = d.uptime_str || '—';
        const stDist = document.getElementById('st-dist');
        if (stDist) stDist.textContent = (d.dist_m || 0) + ' м';
        const stCover = document.getElementById('st-cover');
        if (stCover) stCover.textContent = (d.coverage_pct || 0) + '%';
        const stPath = document.getElementById('st-path');
        if (stPath) stPath.textContent = d.path_points || '—';
        const stHumans = document.getElementById('st-humans');
        if (stHumans) stHumans.textContent = d.humans_found || '0';
        const stSession = document.getElementById('st-session');
        if (stSession) stSession.textContent = d.session_id || '—';
        const logDirEl = document.getElementById('logDir');
        if (logDirEl && d.session_id) logDirEl.textContent = d.session_id;

    } catch(e) {}
    setTimeout(statsLoop, 2000);
}

// ── Тема ─────────────────────────────────────────────────────────────────
function toggleTheme() {
    document.body.classList.toggle('light');
    localStorage.setItem('theme', document.body.classList.contains('light') ? 'light' : 'dark');
}

// Восстановить тему
if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light');
}

// ── Экспорт карты ─────────────────────────────────────────────────────────
function exportMap() {
    const a = document.createElement('a');
    a.href = '/api/map/export';
    a.download = 'mars_map.png';
    a.click();
}

function openReport() {
    window.open('/api/report', '_blank');
}

// ── Детектор движения ─────────────────────────────────────────────────────
async function motionLoop() {
    try {
        const r = await fetch('/api/motion');
        const d = await r.json();

        // Показываем в инфо-полоске
        const stripEl = document.getElementById('infoStrip');
        let motEl = document.getElementById('s_motion');
        if (!motEl && stripEl) {
            motEl = document.createElement('span');
            motEl.id = 's_motion';
            motEl.style.cssText = 'flex-shrink:0;display:none;padding:2px 8px;' +
                'border-radius:10px;background:rgba(255,200,0,0.15);' +
                'border:1px solid rgba(255,200,0,0.3);color:var(--warn);';
            stripEl.appendChild(motEl);
        }
        if (motEl) {
            if (d.detected) {
                motEl.style.display = 'inline';
                motEl.textContent = `⚡ ДВИЖЕНИЕ ${d.percent.toFixed(0)}%`;
            } else {
                motEl.style.display = 'none';
            }
        }
    } catch(e) {}
    setTimeout(motionLoop, 300);
}

// ── Старт ─────────────────────────────────────────────────────────────────
loadCameras(); videoLoop(); mapLoop(); stateLoop(); initRadar(); logLoop(); initNotifications(); statsLoop(); motionLoop();
</script>

<style>
@keyframes flashAnim { 0%{opacity:1} 100%{opacity:0} }
@keyframes slideDown { from{transform:translateX(-50%) translateY(-20px);opacity:0} to{transform:translateX(-50%) translateY(0);opacity:1} }
</style>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/robot/state')
def robot_state_api():
    return jsonify({'mode': 'SIMULATION', 'state': robot_simulator.get_state_dict()})

@app.route('/api/map')
def get_map():
    map_img = draw_map()
    img_io = io.BytesIO()
    map_img.save(img_io, 'JPEG', quality=80)
    img_io.seek(0)
    return app.response_class(response=img_io.getvalue(), mimetype='image/jpeg')

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

@app.route('/api/robot/command', methods=['POST'])
def robot_command():
    data = request.get_json()
    cmd = data.get('cmd')
    
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
    """Данные сонара для радара"""
    if not sonar_sensor:
        return jsonify({'distance_cm': 999, 'obstacle': False,
                        'radar_points': [], 'enabled': False,
                        'sim_mode': True, 'robot_angle': 0})
    return jsonify(sonar_sensor.get_status())

@app.route('/api/sonar/mode', methods=['POST'])
def sonar_mode():
    """Переключить режим: sim / real"""
    data = request.get_json()
    mode = data.get('mode', 'sim')
    if not sonar_sensor:
        return jsonify({'success': False})
    if mode == 'sim':
        sonar_sensor.enabled = False
        if logger: logger.log_event('СОНАР_РЕЖИМ', 'Симуляция')
        return jsonify({'success': True, 'mode': 'sim'})
    elif mode == 'real':
        if not GPIO_AVAILABLE:
            return jsonify({'success': False,
                            'message': 'GPIO недоступен — подключи датчик'})
        try:
            GPIO.setmode(GPIO.BOARD)
            GPIO.setwarnings(False)
            GPIO.setup(sonar_sensor.PIN_TRIG, GPIO.OUT)
            GPIO.setup(sonar_sensor.PIN_ECHO, GPIO.IN)
            GPIO.output(sonar_sensor.PIN_TRIG, GPIO.LOW)
            sonar_sensor.enabled = True
            if logger: logger.log_event('СОНАР_РЕЖИМ', 'GPIO реальный')
            return jsonify({'success': True, 'mode': 'real'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})
    return jsonify({'success': False})

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
    map_img.save(img_io, 'JPEG', quality=80)
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

if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║  М.А.Р.С. — Мобильный Автоматизированный Робот Спасатель ║
    ║  v2.2 · Orange Pi PC H3 · Armbian                         ║
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
