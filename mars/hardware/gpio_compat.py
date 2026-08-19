"""Единая точка определения доступности GPIO — используется motors.py и sonar.py.

На Orange Pi (Armbian) с установленным OPi.GPIO GPIO_AVAILABLE=True.
На любой другой машине (в т.ч. эта — для разработки/тестов) — False, всё
работает в sim_mode без обращения к железу.
"""
try:
    import OPi.GPIO as GPIO
    GPIO_AVAILABLE = True
    print("[✓] OPi.GPIO доступен")
except ImportError:
    GPIO = None
    GPIO_AVAILABLE = False
    print("[⚠️] OPi.GPIO не найден — моторы/сонар в режиме симуляции")
