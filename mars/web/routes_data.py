"""Роуты данных: люди, логи, статистика, карта на экспорт, отчёт, графики.

/api/humans/list, /api/graphs и /api/heatmap не используются текущим
фронтендом (не вызываются из app.js) — оставлены рабочими как
осознанный резерв API, а не мёртвый код для удаления. См. API.md.
"""
import base64
import io
import math
import time

from flask import Blueprint, current_app, jsonify, request
from PIL import Image

from mars.config import robot_config
from mars.core import frame_state
from mars.vision.map_view import draw_map
from mars.web.auth import require_token

bp = Blueprint('data', __name__)


@bp.route('/api/humans/photo/<int:human_id>')
def human_photo(human_id):
    """Фото найденного человека"""
    state_obj = current_app.state
    for h in state_obj.robot_simulator.found_humans:
        if h['id'] == human_id and h.get('photo'):
            photo_bytes = base64.b64decode(h['photo'])
            return current_app.response_class(response=photo_bytes, mimetype='image/jpeg')
    return current_app.response_class(response=b'', status=404)


@bp.route('/api/humans/list')
def humans_list():
    """Список найденных людей с фото"""
    state_obj = current_app.state
    result = []
    for h in state_obj.robot_simulator.found_humans:
        result.append({
            'id': h['id'],
            'x': h['x'],
            'y': h['y'],
            'timestamp': h['timestamp'],
            'has_photo': bool(h.get('photo')),
        })
    return jsonify({'humans': result, 'count': len(result)})


@bp.route('/api/log/events')
def log_events():
    logger_inst = current_app.state.logger
    if logger_inst:
        return jsonify({
            'events': logger_inst.get_recent_events(30),
            'stats': logger_inst.get_stats()
        })
    return jsonify({'events': [], 'stats': {}})


@bp.route('/api/log/screenshot', methods=['POST'])
@require_token
def log_screenshot():
    logger_inst = current_app.state.logger
    frame_copy = frame_state.get_frame_copy()
    if logger_inst and frame_copy is not None:
        name = logger_inst.save_screenshot(frame_copy, 'manual')
        return jsonify({'success': True, 'filename': name})
    return jsonify({'success': False})


@bp.route('/api/stats')
def get_stats():
    """Статистика сессии"""
    state_obj = current_app.state
    robot_simulator = state_obj.robot_simulator
    if not robot_simulator:
        return jsonify({})

    path = list(robot_simulator.path_history)

    dist_total = 0.0
    for i in range(1, len(path)):
        dx = path[i][0] - path[i-1][0]
        dy = path[i][1] - path[i-1][1]
        dist_total += math.sqrt(dx*dx + dy*dy)

    cells = set()
    for x, y in path:
        cells.add((int(x // 50), int(y // 50)))
    total_cells = (robot_config.map_width // 50) * (robot_config.map_height // 50)
    coverage = round(len(cells) / total_cells * 100, 1)

    uptime = time.time() - robot_simulator.start_time

    return jsonify({
        'uptime_sec':    round(uptime),
        'uptime_str':    f"{int(uptime//60)}м {int(uptime%60)}с",
        'dist_px':       round(dist_total),
        'dist_m':        round(dist_total / 100, 1),
        'path_points':   len(path),
        'coverage_pct':  coverage,
        'cells_visited': len(cells),
        'humans_found':  len(robot_simulator.found_humans),
        'faces_now':     len(robot_simulator.detected_faces),
        'autopilot':     state_obj.autopilot.get_status() if state_obj.autopilot else {},
        'session_id':    state_obj.logger.session_id if state_obj.logger else '—',
        'log_dir':       state_obj.logger.session_dir if state_obj.logger else '—',
    })


@bp.route('/api/map/export')
def export_map():
    """Экспорт карты в PNG высокого качества"""
    state_obj = current_app.state
    map_img = draw_map(state_obj.robot_simulator, state_obj.sonar_sensor,
                        state_obj.heatmap, state_obj.autopilot)
    export_img = map_img.resize(
        (robot_config.map_width * 2, robot_config.map_height * 2),
        Image.NEAREST
    )
    img_io = io.BytesIO()
    export_img.save(img_io, 'PNG')
    img_io.seek(0)
    return current_app.response_class(
        response=img_io.getvalue(),
        mimetype='image/png',
        headers={'Content-Disposition': 'attachment; filename=mars_map.png'}
    )


@bp.route('/api/report')
def get_report():
    """Генерация HTML отчёта"""
    state_obj = current_app.state
    map_img = draw_map(state_obj.robot_simulator, state_obj.sonar_sensor,
                        state_obj.heatmap, state_obj.autopilot)
    html = state_obj.report_generator.generate(state_obj.robot_simulator, state_obj.logger, map_img)
    if state_obj.logger:
        state_obj.logger.log_event('ОТЧЁТ', 'Сформирован отчёт PDF')
    return current_app.response_class(response=html, mimetype='text/html; charset=utf-8')


@bp.route('/api/motion')
def get_motion():
    """Данные детектора движения"""
    motion_detector = current_app.state.motion_detector
    if not motion_detector:
        return jsonify({'level': 0, 'detected': False, 'percent': 0})
    return jsonify(motion_detector.get_status())


@bp.route('/api/heatmap')
def get_heatmap():
    """Тепловая карта покрытия"""
    state_obj = current_app.state
    if not state_obj.heatmap:
        return jsonify({'coverage': 0})
    map_img = draw_map(state_obj.robot_simulator, state_obj.sonar_sensor,
                        state_obj.heatmap, state_obj.autopilot)
    img_io = io.BytesIO()
    map_img.save(img_io, 'JPEG', quality=65)
    img_io.seek(0)
    return current_app.response_class(response=img_io.getvalue(), mimetype='image/jpeg')


@bp.route('/api/graphs')
def get_graphs():
    """Данные для графиков"""
    state_obj = current_app.state
    path = list(state_obj.robot_simulator.path_history) if state_obj.robot_simulator else []

    speeds = []
    for i in range(1, len(path)):
        dx = path[i][0] - path[i-1][0]
        dy = path[i][1] - path[i-1][1]
        speeds.append(round(math.sqrt(dx*dx + dy*dy), 1))

    return jsonify({
        'path_x':    [round(p[0], 1) for p in path[::5]],
        'path_y':    [round(p[1], 1) for p in path[::5]],
        'speeds':    speeds[::5],
        'coverage':  state_obj.heatmap.get_coverage() if state_obj.heatmap else 0,
        'motion':    state_obj.motion_detector.get_status() if state_obj.motion_detector else {},
    })
