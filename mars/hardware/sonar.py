"""Ультразвуковой датчик HY-SRF05 — только реальный GPIO, без симуляции расстояния."""
import threading
import time
from collections import deque

from mars.hardware.gpio_compat import GPIO, GPIO_AVAILABLE


class SonarSensor:
    """
    Ультразвуковой датчик HY-SRF05.
    Только реальный GPIO — без симуляции.
    Статусы: OK / ВЫКЛ / ОШИБКА / НЕТ_GPIO

    robot_simulator/logger — опциональные зависимости, проставляются
    снаружи (см. mars/state.py) после создания всех компонентов, чтобы
    этот модуль не тянул за собой web-слой напрямую.
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

        self.robot_simulator = None
        self.logger = None

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
        if self.logger:
            self.logger.log_event('СОНАР_ВКЛ', f'Pin TRIG={self.PIN_TRIG} ECHO={self.PIN_ECHO}')
        print("[✓] Сонар: включён")
        return True, 'OK'

    def turn_off(self):
        """Выключить сонар"""
        self.active = False
        self.status = self.ST_OFF
        with self.lock:
            self.distance_cm = 999.0
            self.radar_points.clear()
        if self.logger:
            self.logger.log_event('СОНАР_ВЫКЛ', 'Сонар отключён')
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

                robot_angle = self.robot_simulator.angle if self.robot_simulator else 0
                rx = self.robot_simulator.x if self.robot_simulator else 400
                ry = self.robot_simulator.y if self.robot_simulator else 300
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
                'robot_angle':  self.robot_simulator.angle if self.robot_simulator else 0,
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
