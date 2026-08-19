#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""М.А.Р.С. — тонкий entrypoint.

Вся логика теперь в пакете mars/ (см. mars/app.py). Имя файла и путь
запуска не менялись специально — setup_autostart.sh запускает именно
/home/komar/Desktop/robot_rescue_demo.py и не требует правок.
"""
from mars.app import run

if __name__ == '__main__':
    run()
