"""Простой shared-secret токен для эндпоинтов, меняющих состояние робота.

Раньше (v30) app.run(host='0.0.0.0', ...) был полностью открыт всем в
локальной сети без единой проверки — любой, кто подключился к WiFi
робота, мог слать /api/robot/command. Это не пароль пользователя и не
полноценная авторизация — просто граница доверия перед тем, как к API
подключится второй сетевой клиент (коптер, см. routes_swarm.py).

Токен читается из переменной окружения MARS_TOKEN. Если она не задана,
при первом запуске генерируется случайный токен и сохраняется в
.mars_token (файл в .gitignore — секрет никогда не коммитится).
"""
import functools
import os
import secrets

from flask import current_app, jsonify, request

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.mars_token')


def get_or_create_token() -> str:
    env_token = os.environ.get('MARS_TOKEN')
    if env_token:
        return env_token

    if os.path.exists(_TOKEN_FILE):
        with open(_TOKEN_FILE, 'r') as f:
            token = f.read().strip()
            if token:
                return token

    token = secrets.token_hex(16)
    with open(_TOKEN_FILE, 'w') as f:
        f.write(token)
    print(f"[✓] Сгенерирован новый MARS_TOKEN, сохранён в {_TOKEN_FILE}")
    return token


def require_token(view):
    """Декоратор: требует заголовок X-MARS-Token, совпадающий с app.config['MARS_TOKEN']."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        expected = current_app.config.get('MARS_TOKEN')
        provided = request.headers.get('X-MARS-Token', '')
        if not expected or not secrets.compare_digest(provided, expected):
            return jsonify({'success': False, 'reason': 'unauthorized'}), 401
        return view(*args, **kwargs)
    return wrapped
