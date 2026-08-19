"""Роуты камеры, карты и сонара."""
import io
import time

from flask import Blueprint, current_app, jsonify, request
from PIL import Image, ImageDraw

from mars.config import camera_config
from mars.core import frame_state
from mars.hardware.camera import discover_video_devices
from mars.hardware.gpio_compat import GPIO_AVAILABLE
from mars.vision.map_view import draw_map
from mars.web.auth import require_token

bp = Blueprint('sensors', __name__)

# Кэш карты — не перерисовываем чаще чем раз в 500мс
_map_cache = None
_map_cache_time = 0.0
_MAP_CACHE_TTL = 0.5


@bp.route('/api/map')
def get_map():
    global _map_cache, _map_cache_time
    state_obj = current_app.state
    now = time.time()
    if _map_cache is None or (now - _map_cache_time) > _MAP_CACHE_TTL:
        map_img = draw_map(state_obj.robot_simulator, state_obj.sonar_sensor,
                            state_obj.heatmap, state_obj.autopilot)
        buf = io.BytesIO()
        map_img.save(buf, 'JPEG', quality=60)
        _map_cache      = buf.getvalue()
        _map_cache_time = now
    return current_app.response_class(response=_map_cache, mimetype='image/jpeg')


@bp.route('/api/camera/frame')
def camera_frame():
    state_obj = current_app.state
    frame_copy = frame_state.get_frame_copy()

    if frame_copy is None:
        black_img = Image.new('RGB', (camera_config.width, camera_config.height), color='black')
        draw = ImageDraw.Draw(black_img)
        draw.text((50, 100), "Camera", fill=(100, 255, 100))
        img_io = io.BytesIO()
        black_img.save(img_io, 'JPEG', quality=70)
        img_io.seek(0)
    else:
        if state_obj.face_detector:
            frame_copy = state_obj.face_detector.draw_on_frame(frame_copy)
        img_io = io.BytesIO()
        frame_copy.save(img_io, 'JPEG', quality=75)
        img_io.seek(0)

    return current_app.response_class(response=img_io.getvalue(), mimetype='image/jpeg')


@bp.route('/api/cameras/list')
def list_cameras():
    devices = discover_video_devices()
    return jsonify({'devices': devices})


@bp.route('/api/cameras/select', methods=['POST'])
@require_token
def select_camera():
    state_obj = current_app.state
    data = request.get_json()
    device = data.get('device')
    if device:
        state_obj.camera_manager.change_device(device)
        return jsonify({'success': True, 'message': f'📹 {state_obj.camera_manager.backend_name.upper()}'})
    return jsonify({'success': False, 'message': 'Invalid device'})


@bp.route('/api/sonar')
def sonar_api():
    state_obj = current_app.state
    if not state_obj.sonar_sensor:
        return jsonify({'distance_cm':999,'obstacle':False,'radar_points':[],
                        'enabled':False,'active':False,'status':'НЕТ_GPIO',
                        'sim_mode':False,'robot_angle':0,'sweep_angle':0})
    return jsonify(state_obj.sonar_sensor.get_status())


@bp.route('/api/sonar/mode', methods=['POST'])
@require_token
def sonar_mode():
    """Включить / выключить сонар"""
    state_obj = current_app.state
    data = request.get_json()
    mode = data.get('mode', 'off')
    if not state_obj.sonar_sensor:
        return jsonify({'success': False, 'message': 'Сонар не инициализирован'})
    if mode == 'on':
        ok, msg = state_obj.sonar_sensor.turn_on()
        return jsonify({'success': ok, 'message': msg, 'status': state_obj.sonar_sensor.status})
    elif mode == 'off':
        state_obj.sonar_sensor.turn_off()
        return jsonify({'success': True, 'status': state_obj.sonar_sensor.status})
    return jsonify({'success': False, 'message': 'Неверный режим'})


@bp.route('/api/sonar/diagnostic')
def sonar_diagnostic():
    """Диагностика сонара"""
    state_obj = current_app.state
    sonar = state_obj.sonar_sensor
    if not sonar:
        return jsonify({'ok': False, 'message': 'Объект не создан'})
    result = {
        'gpio_available':   GPIO_AVAILABLE,
        'gpio_initialized': sonar.enabled,
        'active':           sonar.active,
        'status':           sonar.status,
        'error_msg':        sonar.error_msg,
        'error_count':      sonar.error_count,
        'pin_trig':         sonar.PIN_TRIG,
        'pin_echo':         sonar.PIN_ECHO,
        'last_distance':    sonar.distance_cm,
        'last_ok_ago':      round(time.time()-sonar.last_ok_time,1) if sonar.last_ok_time else None,
    }
    if not GPIO_AVAILABLE:
        result['verdict'] = '❌ OPi.GPIO не установлен'
    elif not sonar.enabled:
        result['verdict'] = f'❌ GPIO ошибка: {sonar.error_msg}'
    elif not sonar.active:
        result['verdict'] = '⚠️ Сонар выключен'
    elif sonar.status == 'ТАЙМАУТ':
        result['verdict'] = '❌ Нет ответа — проверь ECHO/TRIG'
    elif sonar.status == 'ОШИБКА':
        result['verdict'] = f'❌ Ошибка: {sonar.error_msg}'
    elif sonar.status == 'OK':
        result['verdict'] = f'✅ Работает — {sonar.distance_cm:.0f} см'
    else:
        result['verdict'] = f'⚠️ {sonar.status}'
    return jsonify(result)
