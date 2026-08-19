"""Модель робота: координаты на карте + управление моторами.

autopilot — опциональная зависимость, проставляется снаружи (mars/state.py)
после создания AutoPilot, чтобы избежать циклического импорта
core.simulator <-> core.autopilot.
"""
import base64
import io
import math
import threading
import time
from collections import deque

from mars.config import robot_config
from mars.hardware.motors import MotorController


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

        # Режим робота: 'sim' | 'real' (переключается роутом /api/robot/mode)
        self.robot_mode = 'sim'

        # Проставляется извне после создания AutoPilot (mars/state.py)
        self.autopilot = None

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
                photo_b64 = base64.b64encode(buf.getvalue()).decode()
            except Exception:
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
            'robot_mode': self.robot_mode,
            'autopilot': self.autopilot.get_status() if self.autopilot else {'enabled': False, 'mode': 'СТОП'},
        }
