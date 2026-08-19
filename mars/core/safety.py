"""Watchdog + E-STOP.

Заменяет голые module-level globals `_last_cmd_time`/`_estop_active` из
v30 (там watchdog-поток стартовал на уровне импорта модуля — до того как
robot_simulator вообще существовал — и тело цикла было без try/except,
так что одно необработанное исключение тихо и навсегда убивало watchdog).

Здесь: состояние под threading.Lock, цикл watchdog оборачивает каждую
итерацию в try/except с логированием, а поток стартует явно из
mars.app.run(), а не при импорте модуля.
"""
import threading
import time

WATCHDOG_TIMEOUT = 1.5  # секунд без команды → стоп


class SafetyGuard:
    def __init__(self, robot_simulator, autopilot, logger, timeout=WATCHDOG_TIMEOUT):
        self.robot_simulator = robot_simulator
        self.autopilot = autopilot
        self.logger = logger
        self.timeout = timeout

        self._lock = threading.Lock()
        self._last_cmd_time = time.time()
        self._estop_active = False
        self._thread = None

    def touch(self):
        """Сбросить таймер watchdog — вызывается из /api/robot/command и /api/robot/keepalive"""
        with self._lock:
            self._last_cmd_time = time.time()

    @property
    def estop_active(self):
        with self._lock:
            return self._estop_active

    def set_estop(self, activate: bool):
        with self._lock:
            self._estop_active = activate
        if activate:
            if self.robot_simulator:
                self.robot_simulator.stop()
            if self.autopilot:
                self.autopilot.stop()
            if self.logger:
                self.logger.log_event('E-STOP', 'Аварийная остановка активирована')
            print("[!] E-STOP АКТИВИРОВАН")
        else:
            if self.logger:
                self.logger.log_event('E-STOP', 'Система разблокирована')
            print("[✓] E-STOP снят")

    def start(self):
        """Запустить фоновый поток watchdog. Вызывать один раз при старте приложения."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                time.sleep(0.3)
                with self._lock:
                    elapsed = time.time() - self._last_cmd_time
                if (elapsed > self.timeout and self.robot_simulator
                        and self.robot_simulator.current_command != 'STOP'):
                    self.robot_simulator.stop()
            except Exception as e:
                # Раньше необработанное исключение здесь тихо убивало watchdog
                # навсегда. Логируем и продолжаем — сторожевой пёс не имеет
                # права молча заснуть.
                print(f"[✗] Watchdog: ошибка цикла — {e}")
