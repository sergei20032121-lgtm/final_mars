"""Роуты управления роботом: движение, скорость, режим, E-STOP, автопилот.

Все state-mutating роуты защищены require_token (см. mars/web/auth.py) —
раньше это API было полностью открыто любому в локальной WiFi-сети.
"""
from flask import Blueprint, current_app, jsonify, request

from mars.hardware.gpio_compat import GPIO, GPIO_AVAILABLE
from mars.web.auth import require_token

bp = Blueprint('robot', __name__)


@bp.route('/api/robot/state')
def robot_state_api():
    state_obj = current_app.state
    state = state_obj.robot_simulator.get_state_dict()
    if state_obj.sonar_sensor:
        sonar = state_obj.sonar_sensor.get_status()
        state['sonar_dist']    = sonar['distance_cm']
        state['sonar_obstacle']= sonar['obstacle']
        state['sonar_enabled'] = sonar['enabled']
        state['sonar_active']  = sonar['active']
        state['sonar_status']  = sonar['status']
        state['sweep_angle']   = sonar['sweep_angle']
        state['radar_points']  = sonar['radar_points']
        state['estop']         = state_obj.safety.estop_active
    else:
        state['sonar_dist']    = 999
        state['sonar_obstacle']= False
        state['sonar_enabled'] = False
        state['sweep_angle']   = 0
    return jsonify({'mode': 'SIMULATION', 'state': state})


@bp.route('/api/robot/command', methods=['POST'])
@require_token
def robot_command():
    state_obj = current_app.state
    data = request.get_json()
    cmd = data.get('cmd')
    state_obj.safety.touch()

    if state_obj.safety.estop_active and cmd != 'stop':
        return jsonify({'success': False, 'reason': 'estop_active'})

    if cmd == 'forward':
        state_obj.robot_simulator.move_forward()
    elif cmd == 'backward':
        state_obj.robot_simulator.move_backward()
    elif cmd == 'left':
        state_obj.robot_simulator.turn_left()
    elif cmd == 'right':
        state_obj.robot_simulator.turn_right()
    elif cmd == 'stop':
        state_obj.robot_simulator.stop()

    return jsonify({'success': True, 'command': cmd})


@bp.route('/api/robot/keepalive', methods=['POST'])
@require_token
def robot_keepalive():
    """Heartbeat от браузера — сбрасывает watchdog"""
    current_app.state.safety.touch()
    return jsonify({'ok': True})


@bp.route('/api/robot/estop', methods=['POST'])
@require_token
def robot_estop():
    """Аварийная остановка — рубит всё: моторы, автопилот, миссию"""
    data = request.get_json() or {}
    activate = data.get('activate', True)
    current_app.state.safety.set_estop(activate)
    return jsonify({'ok': True, 'estop': current_app.state.safety.estop_active})


@bp.route('/api/robot/estop/status')
def estop_status():
    return jsonify({'estop': current_app.state.safety.estop_active})


@bp.route('/api/robot/speed', methods=['POST'])
@require_token
def set_robot_speed():
    data = request.get_json()
    speed = data.get('speed', 150)
    current_app.state.robot_simulator.set_speed(speed)
    return jsonify({'success': True, 'speed': speed})


@bp.route('/api/robot/clear_path', methods=['POST'])
@require_token
def clear_path():
    """Очистить историю пути"""
    state_obj = current_app.state
    if state_obj.robot_simulator:
        state_obj.robot_simulator.path_history.clear()
        state_obj.robot_simulator.path_history.append(
            (state_obj.robot_simulator.x, state_obj.robot_simulator.y))
        if state_obj.logger:
            state_obj.logger.log_event('PATH_CLEARED', 'История пути очищена')
    return jsonify({'success': True})


@bp.route('/api/motors/sim', methods=['POST'])
@require_token
def set_motors_sim():
    """Включить/выключить симуляцию моторов"""
    state_obj = current_app.state
    data = request.get_json()
    sim = data.get('sim', True)
    if state_obj.robot_simulator:
        state_obj.robot_simulator.motors.set_sim_mode(sim)
        if state_obj.logger:
            state_obj.logger.log_event('МОТОРЫ_РЕЖ', 'Симуляция' if sim else 'GPIO реальный')
    return jsonify({'success': True, 'sim_mode': sim})


@bp.route('/api/robot/mode', methods=['POST'])
@require_token
def set_robot_mode():
    """Переключить режим: sim / real"""
    state_obj = current_app.state
    data = request.get_json()
    mode = data.get('mode', 'sim')

    if mode == 'sim':
        state_obj.robot_simulator.robot_mode = 'sim'
        # В симуляции отключаем GPIO моторов
        if state_obj.robot_simulator:
            state_obj.robot_simulator.motors.enabled = False
        if state_obj.logger:
            state_obj.logger.log_event('РЕЖИМ', 'Симуляция движения')
        return jsonify({'success': True, 'mode': 'sim'})

    elif mode == 'real':
        if not GPIO_AVAILABLE:
            return jsonify({'success': False, 'message': 'GPIO недоступен'})
        if not state_obj.robot_simulator or not state_obj.robot_simulator.motors.enabled:
            # Пробуем переинициализировать GPIO
            try:
                GPIO.setmode(GPIO.BOARD)
                GPIO.setwarnings(False)
                m = state_obj.robot_simulator.motors
                for pin in [m.PIN_FORWARD, m.PIN_BACKWARD, m.PIN_LEFT, m.PIN_RIGHT]:
                    GPIO.setup(pin, GPIO.OUT)
                    GPIO.output(pin, GPIO.LOW)
                state_obj.robot_simulator.motors.enabled = True
                state_obj.robot_simulator.robot_mode = 'real'
                if state_obj.logger:
                    state_obj.logger.log_event('РЕЖИМ', 'Реальное управление GPIO')
                return jsonify({'success': True, 'mode': 'real'})
            except Exception as e:
                return jsonify({'success': False, 'message': str(e)})
        else:
            state_obj.robot_simulator.robot_mode = 'real'
            return jsonify({'success': True, 'mode': 'real'})

    return jsonify({'success': False, 'message': 'Неверный режим'})


@bp.route('/api/autopilot/start', methods=['POST'])
@require_token
def autopilot_start():
    state_obj = current_app.state
    if state_obj.autopilot:
        state_obj.autopilot.start()
        # ИСПРАВЛЕНО (v30): было autopilot.mode — такого атрибута нет,
        # роут падал с AttributeError при каждом вызове.
        return jsonify({'success': True, 'mode': state_obj.autopilot.режим})
    return jsonify({'success': False})


@bp.route('/api/autopilot/stop', methods=['POST'])
@require_token
def autopilot_stop():
    state_obj = current_app.state
    if state_obj.autopilot:
        state_obj.autopilot.stop()
        return jsonify({'success': True})
    return jsonify({'success': False})


@bp.route('/api/robot/mission', methods=['POST'])
@require_token
def robot_mission():
    """Запуск / остановка автономной миссии поиска"""
    state_obj = current_app.state
    data = request.get_json() or {}
    action = data.get('action', 'start')
    if action == 'start':
        if state_obj.safety.estop_active:
            return jsonify({'success': False, 'reason': 'estop_active'})
        if state_obj.autopilot:
            state_obj.autopilot.start()
        if state_obj.logger:
            state_obj.logger.log_event('МИССИЯ_СТАРТ', 'Автономная миссия запущена')
        return jsonify({'success': True, 'status': 'started'})
    elif action == 'stop':
        if state_obj.autopilot:
            state_obj.autopilot.stop()
        if state_obj.logger:
            state_obj.logger.log_event('МИССИЯ_СТОП', 'Миссия остановлена оператором')
        return jsonify({'success': True, 'status': 'stopped'})
    return jsonify({'success': False, 'reason': 'unknown_action'})
