"""Watchdog/E-STOP — таймаут реально останавливает робота, e-stop блокирует команды."""
import time

from mars.core.safety import SafetyGuard


class FakeRobot:
    def __init__(self):
        self.current_command = 'FORWARD'
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        self.current_command = 'STOP'


def test_watchdog_stops_robot_after_timeout():
    robot = FakeRobot()
    guard = SafetyGuard(robot, autopilot=None, logger=None, timeout=0.2)
    guard.start()

    # Команда только что была — рано останавливать
    guard.touch()
    time.sleep(0.1)
    assert robot.stop_calls == 0

    # Тишина дольше таймаута — должен остановить
    time.sleep(0.4)
    assert robot.stop_calls >= 1
    assert robot.current_command == 'STOP'


def test_touch_resets_timer():
    robot = FakeRobot()
    guard = SafetyGuard(robot, autopilot=None, logger=None, timeout=0.3)
    guard.start()

    end = time.time() + 0.6
    while time.time() < end:
        guard.touch()
        time.sleep(0.05)

    # Постоянные touch() не должны были дать watchdog сработать
    assert robot.stop_calls == 0


def test_estop_blocks_and_stops():
    robot = FakeRobot()
    guard = SafetyGuard(robot, autopilot=None, logger=None, timeout=5.0)

    assert guard.estop_active is False
    guard.set_estop(True)
    assert guard.estop_active is True
    assert robot.stop_calls == 1

    guard.set_estop(False)
    assert guard.estop_active is False


def test_watchdog_survives_exception_in_loop():
    """Раньше необработанное исключение в цикле тихо убивало watchdog навсегда."""
    class ExplodingRobot(FakeRobot):
        def __init__(self):
            super().__init__()
            self.calls = 0

        @property
        def current_command(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return self._cmd

        @current_command.setter
        def current_command(self, v):
            self._cmd = v

    robot = ExplodingRobot()
    guard = SafetyGuard(robot, autopilot=None, logger=None, timeout=0.1)
    guard.start()
    guard.touch()

    # Первая итерация после touch словит исключение при чтении current_command —
    # поток не должен на этом умереть.
    time.sleep(0.5)
    assert guard._thread.is_alive()
