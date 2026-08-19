"""Автопилот — автономный поиск человека, режимы SPIRAL/AVOID/RETURN/SCAN/IDLE
(в коде названия на русском, сохранены как в оригинале для совместимости с
логами и диломной документацией)."""
import math
import threading
import time

from mars.config import robot_config
from mars.core import frame_state


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

    def __init__(self, robot_simulator, sonar_sensor, logger):
        self.robot_simulator = robot_simulator
        self.sonar_sensor = sonar_sensor
        self.logger = logger

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
        if self.logger:
            self.logger.log_event('АВТОПИЛОТ_СТАРТ', 'Поиск запущен')
        print("[✓] Автопилот: старт")

    def stop(self):
        self.running = False
        self.enabled = False
        self.режим   = self.РЕЖИМ_СТОП
        if self.robot_simulator:
            self.robot_simulator.stop()
        if self.logger:
            self.logger.log_event('АВТОПИЛОТ_СТОП', 'Остановлен вручную')
        print("[✓] Автопилот: остановлен")

    def _у_края(self):
        if not self.robot_simulator:
            return False
        x, y = self.robot_simulator.x, self.robot_simulator.y
        o = self.ОТСТУП
        return (x < o or x > robot_config.map_width - o or
                y < o or y > robot_config.map_height - o)

    def _препятствие(self):
        if not self.sonar_sensor or not self.sonar_sensor.active:
            return False, 999
        d = self.sonar_sensor.distance_cm
        return d < self.DIST_ОБЪЕЗД, d

    def _loop(self):
        while self.running:
            try:
                mode = self.режим

                if mode == self.РЕЖИМ_СТОП:
                    self.robot_simulator.stop()
                    time.sleep(0.2)
                elif mode == self.РЕЖИМ_НАЙДЕН:
                    self.robot_simulator.stop()
                    time.sleep(0.3)
                elif mode == self.РЕЖИМ_ВОЗВРАТ:
                    self._do_return()
                elif mode == self.РЕЖИМ_ПОВОРОТ:
                    self._do_turn()
                elif mode == self.РЕЖИМ_ОБЪЕЗД:
                    self._do_avoid()
                elif mode == self.РЕЖИМ_ПОИСК:
                    self._do_search()

                if self.robot_simulator and self.robot_simulator.detected_faces and \
                   mode not in (self.РЕЖИМ_НАЙДЕН, self.РЕЖИМ_СТОП):
                    self._on_found()

                time.sleep(0.1)
            except Exception as e:
                print(f"[✗] Автопилот: ошибка цикла — {e}")
                time.sleep(0.5)

    def _do_search(self):
        # Приоритет 1: край карты
        if self._у_края():
            self.robot_simulator.stop()
            self._поворот_к_центру()
            return
        # Приоритет 2: препятствие
        есть, dist = self._препятствие()
        if есть:
            self.robot_simulator.stop()
            self.режим = self.РЕЖИМ_ОБЪЕЗД
            self._объезд_шаги = 0
            if self.logger:
                self.logger.log_event('ПРЕПЯТСТВИЕ', f'{dist:.0f}см')
            return
        # Движение прямо
        if self._шаги < self._макс_шагов:
            self.robot_simulator.move_forward()
            self._шаги += 1
        else:
            self.robot_simulator.stop()
            self._поворотов += 1
            if self._поворотов % 2 == 0:
                self._макс_шагов = min(self._макс_шагов + 8, 80)
            self._начать_поворот(90)
            if self.logger:
                self.logger.log_event('СПИРАЛЬ', f'поворот #{self._поворотов}, прямо={self._макс_шагов}')

    def _начать_поворот(self, градусов=90):
        self.режим = self.РЕЖИМ_ПОВОРОТ
        self._шаги_поворота = 0
        self._макс_поворота = max(6, int(градусов / 5))
        self._шаги = 0

    def _поворот_к_центру(self):
        cx = robot_config.map_width  / 2
        cy = robot_config.map_height / 2
        dx = cx - self.robot_simulator.x
        dy = cy - self.robot_simulator.y
        target  = math.degrees(math.atan2(dx, -dy)) % 360
        current = self.robot_simulator.angle % 360
        diff    = (target - current + 360) % 360
        self.режим = self.РЕЖИМ_ПОВОРОТ
        self._шаги_поворота = 0
        self._макс_поворота = max(4, int(diff / 5))
        self._шаги = 0
        if self.logger:
            self.logger.log_event('РАЗВОРОТ', f'→ центру ({target:.0f}°)')

    def _do_turn(self):
        if self._шаги_поворота < self._макс_поворота:
            self.robot_simulator.turn_right()
            self._шаги_поворота += 1
        else:
            self.robot_simulator.stop()
            self.режим = self.РЕЖИМ_ПОИСК
            self._шаги = 0

    def _do_avoid(self):
        s = self._объезд_шаги
        _, dist = self._препятствие()
        if s < 3:
            self.robot_simulator.stop()
        elif s < 12:
            self.robot_simulator.turn_right()
        elif s < 30:
            if dist > self.DIST_СТОП:
                self.robot_simulator.move_forward()
            else:
                self.robot_simulator.turn_right()
                self._объезд_шаги = 12
        else:
            self.robot_simulator.stop()
            self.режим = self.РЕЖИМ_ПОИСК
            self._шаги = 0
            if self.logger:
                self.logger.log_event('ОБЪЕЗД_ОК', 'Продолжаю поиск')
        self._объезд_шаги += 1

    def _do_return(self):
        if not self._return_path or self._return_idx >= len(self._return_path):
            self.robot_simulator.stop()
            self.режим   = self.РЕЖИМ_СТОП
            self.running = False
            if self.logger:
                self.logger.log_event('БАЗА', 'Вернулся на базу')
            return
        tx, ty = self._return_path[self._return_idx]
        rx, ry = self.robot_simulator.x, self.robot_simulator.y
        dist   = math.sqrt((tx-rx)**2 + (ty-ry)**2)
        if dist < 15:
            self._return_idx += 3
            return
        target  = math.degrees(math.atan2(tx-rx, -(ty-ry))) % 360
        current = self.robot_simulator.angle % 360
        diff    = (target - current + 360) % 360
        if diff > 20 and diff < 340:
            self.robot_simulator.turn_right() if diff < 180 else self.robot_simulator.turn_left()
        else:
            self.robot_simulator.move_forward()
            self._return_idx += 1
        time.sleep(0.08)

    def _on_found(self):
        if self.режим == self.РЕЖИМ_НАЙДЕН:
            return
        self.режим = self.РЕЖИМ_НАЙДЕН
        self.robot_simulator.stop()
        if self.logger:
            self.logger.log_event('ЧЕЛОВЕК_НАЙДЕН',
                f'({self.robot_simulator.x:.0f}, {self.robot_simulator.y:.0f})')
        fc = frame_state.get_frame_copy()
        if fc is not None and self.logger:
            self.logger.save_screenshot(fc, 'найден')
        print("[!] ЧЕЛОВЕК НАЙДЕН!")
        self._return_path = list(self.robot_simulator.path_history)[::-1]
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
