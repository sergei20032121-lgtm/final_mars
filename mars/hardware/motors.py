"""Управление гусеницами через L9110S H-Bridge.

ВАЖНО (проверено на реальном железе, Orange Pi PC H3 + Armbian):
логика L9110S на этой сборке ИНВЕРТИРОВАНА относительно документации
драйвера — GPIO.LOW включает канал, GPIO.HIGH его останавливает.
Причина — внутренняя подтяжка пинов GPIO к высокому уровню в состоянии
покоя. См. диплом, раздел 2.3. Не "исправлять" на неинвертированную
логику без повторной проверки на реальном роботе.
"""
import threading
import time

from mars.hardware.gpio_compat import GPIO, GPIO_AVAILABLE


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
        self._lock = threading.Lock()

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
        try:
            GPIO.output(self.PIN_L_FWD, GPIO.LOW if lf else GPIO.HIGH)
            GPIO.output(self.PIN_L_BWD, GPIO.LOW if lb else GPIO.HIGH)
            GPIO.output(self.PIN_R_FWD, GPIO.LOW if rf else GPIO.HIGH)
            GPIO.output(self.PIN_R_BWD, GPIO.LOW if rb else GPIO.HIGH)
        except Exception as e:
            # Рантайм-сбой GPIO (например обрыв контакта) не должен ронять
            # поток Flask-запроса — отключаем моторы и просим команду stop.
            print(f"[✗] Моторы: сбой GPIO при выводе команды — {e}")
            self.enabled = False

    def forward(self):
        with self._lock:
            self.current_cmd = 'FORWARD'
        self._set(1, 0, 1, 0)

    def backward(self):
        with self._lock:
            self.current_cmd = 'BACKWARD'
        self._set(0, 1, 0, 1)

    def left(self):
        with self._lock:
            self.current_cmd = 'LEFT'
        self._set(0, 1, 1, 0)  # левая назад, правая вперёд

    def right(self):
        with self._lock:
            self.current_cmd = 'RIGHT'
        self._set(1, 0, 0, 1)  # левая вперёд, правая назад

    def stop(self):
        with self._lock:
            self.current_cmd = 'STOP'
        self._set(0, 0, 0, 0)

    def set_sim_mode(self, enabled: bool):
        self.sim_mode = enabled
        if enabled and self.enabled:
            # Стопим реальные моторы при включении симуляции (HIGH=стоп)
            try:
                for pin in [self.PIN_L_FWD, self.PIN_L_BWD,
                            self.PIN_R_FWD, self.PIN_R_BWD]:
                    GPIO.output(pin, GPIO.HIGH)
            except Exception as e:
                print(f"[✗] Моторы: сбой GPIO при переходе в sim_mode — {e}")
        mode = "СИМ" if enabled else "GPIO"
        print(f"[✓] Моторы: режим {mode}")

    def cleanup(self):
        if self.enabled:
            try:
                # HIGH = стоп перед очисткой
                for pin in [self.PIN_L_FWD, self.PIN_L_BWD,
                            self.PIN_R_FWD, self.PIN_R_BWD]:
                    GPIO.output(pin, GPIO.HIGH)
                GPIO.cleanup()
                print("[✓] GPIO очищен")
            except Exception as e:
                print(f"[✗] Моторы: сбой GPIO при очистке — {e}")

    @property
    def status(self):
        mode = "СИМ" if self.sim_mode else "GPIO"
        with self._lock:
            cmd = self.current_cmd
        return f"{mode} ({cmd})"
