"""Flask app factory + инициализация системы + фоновые потоки.

initialize_system()/create_app() не запускают видео-поток и watchdog —
это делает run() (вызывается из entrypoint robot_rescue_demo.py), чтобы
create_app() можно было безопасно использовать в тестах (Flask test
client) без побочных эффектов в виде вечных фоновых потоков.
"""
import io
import os
import threading
import time

from flask import Flask, render_template
from PIL import Image

# templates/ и static/ живут в корне репозитория, не рядом с mars/app.py —
# Flask(__name__) по умолчанию искал бы их внутри пакета mars/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from mars.config import camera_config, robot_config
from mars.core.autopilot import AutoPilot
from mars.core.frame_state import set_frame
from mars.core.logger import Logger
from mars.core.report import ReportGenerator
from mars.core.safety import SafetyGuard
from mars.core.simulator import RobotSimulator
from mars.hardware.camera import CameraManager
from mars.hardware.sonar import SonarSensor
from mars.state import AppState
from mars.vision.detector import SimpleFaceDetector, combined_detect
from mars.vision.heatmap import HeatMap
from mars.vision.motion import MotionDetector
from mars.web.auth import get_or_create_token


def initialize_system() -> AppState:
    """Аналог initialize_system() из v30 — создаёт все компоненты и явно
    связывает их зависимости (вместо неявных module-level globals)."""
    state = AppState()

    state.robot_simulator = RobotSimulator()
    state.camera_manager  = CameraManager(camera_config)
    state.face_detector   = SimpleFaceDetector(camera_config)

    state.sonar_sensor = SonarSensor()
    state.sonar_sensor.start()

    state.logger = Logger()

    # Досвязка зависимостей, которые не могли существовать в момент
    # конструирования (sonar/simulator создаются раньше logger)
    state.sonar_sensor.robot_simulator = state.robot_simulator
    state.sonar_sensor.logger = state.logger

    state.autopilot = AutoPilot(state.robot_simulator, state.sonar_sensor, state.logger)
    state.robot_simulator.autopilot = state.autopilot

    state.motion_detector = MotionDetector()
    state.heatmap = HeatMap(robot_config.map_width, robot_config.map_height)
    state.report_generator = ReportGenerator()
    state.safety = SafetyGuard(state.robot_simulator, state.autopilot, state.logger)

    threading.Thread(target=_path_logger_loop, args=(state,), daemon=True).start()
    return state


def _path_logger_loop(state: AppState):
    """Фоновый поток записи маршрута каждые 2 секунды"""
    while True:
        try:
            if state.robot_simulator and state.logger:
                dist = state.sonar_sensor.distance_cm if state.sonar_sensor else 999
                state.logger.log_path(
                    state.robot_simulator.x, state.robot_simulator.y,
                    state.robot_simulator.angle,
                    state.robot_simulator.current_command,
                    dist
                )
        except Exception as e:
            print(f"[✗] path_logger: {e}")
        time.sleep(2)


def capture_and_process_video(state: AppState):
    """Захват видео с комбинированным детектором (движение + skin-color)."""
    frame_count = 0
    while True:
        try:
            frame_bytes = state.camera_manager.get_frame()
            if frame_bytes:
                img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
                set_frame(img)

                frame_count += 1

                # Комбинированный детектор (каждый 3-й кадр)
                if frame_count % 3 == 0:
                    confidence, bboxes, motion_level = combined_detect(img, state.motion_detector)

                    if state.robot_simulator:
                        state.robot_simulator.detected_faces = [
                            {'x': b[0], 'y': b[1], 'size': max(b[2], b[3]),
                             'left': b[0], 'top': b[1],
                             'right': b[0]+b[2], 'bottom': b[1]+b[3],
                             'confidence': round(confidence, 2)}
                            for b in bboxes
                        ] if bboxes else []

                        if confidence > 0.5 and bboxes:
                            state.robot_simulator.add_human_detection(
                                state.robot_simulator.x,
                                state.robot_simulator.y,
                                img
                            )

                # Pigo детектор лиц (каждый 5-й кадр, если доступен)
                if frame_count % 5 == 0 and state.face_detector and state.face_detector.detector.available:
                    faces = state.face_detector.detect_async(img)
                    if faces and state.robot_simulator:
                        state.robot_simulator.detected_faces = faces
                        state.robot_simulator.add_human_detection(
                            state.robot_simulator.x, state.robot_simulator.y, img)

                # Тепловая карта (каждые 10 кадров)
                if frame_count % 10 == 0 and state.heatmap and state.robot_simulator:
                    state.heatmap.update(state.robot_simulator.x, state.robot_simulator.y)

            time.sleep(0.04)
        except Exception as e:
            print(f"[✗] video_loop: {e}")
            time.sleep(0.5)


def create_app(state: AppState = None) -> Flask:
    """Flask app factory. Если state не передан — создаёт новый через
    initialize_system() (побочные эффекты: GPIO-инициализация, старт
    потока сонара и path-логгера)."""
    if state is None:
        state = initialize_system()

    app = Flask(
        __name__,
        template_folder=os.path.join(_REPO_ROOT, 'templates'),
        static_folder=os.path.join(_REPO_ROOT, 'static'),
    )
    app.state = state
    app.config['MARS_TOKEN'] = get_or_create_token()

    from mars.web.routes_robot import bp as robot_bp
    from mars.web.routes_sensors import bp as sensors_bp
    from mars.web.routes_data import bp as data_bp
    from mars.web.routes_swarm import bp as swarm_bp

    app.register_blueprint(robot_bp)
    app.register_blueprint(sensors_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(swarm_bp)

    @app.route('/')
    def index():
        return render_template('index.html', mars_token=app.config['MARS_TOKEN'])

    return app


def run():
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║  М.А.Р.С. — Мобильный Автоматизированный Робот Спасатель ║
    ║  v3.0 · Orange Pi PC H3 · Armbian · ВКР 2026             ║
    ║  Пины: L_FWD=11 L_BWD=13 R_FWD=15 R_BWD=3               ║
    ║  Логика: LOW=движение HIGH=стоп (инвертированная)         ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    print("\n[*] Инициализация системы...")
    state = initialize_system()
    app = create_app(state)

    print(f"[✓] Камера: {state.camera_manager.backend_name}")
    print(f"[✓] Разрешение: {camera_config.width}x{camera_config.height}")
    print(f"[✓] FPS: {camera_config.fps}")

    print("[*] Запуск захвата видео...")
    video_thread = threading.Thread(target=capture_and_process_video, args=(state,), daemon=True)
    video_thread.start()

    print("[*] Запуск watchdog...")
    state.safety.start()

    print("\n[✓] Система готова! 🚀")
    print(f"[→] Открой браузер: http://localhost:5000")
    print(f"[→] Или с другого ПК: http://<IP_ORANGE_PI>:5000")
    print(f"\n[!] GPIO моторы: {'ВКЛЮЧЕНЫ' if state.robot_simulator.motors.enabled else 'СИМУЛЯЦИЯ'}")
    print("\n[Press Ctrl+C to stop]\n")

    try:
        app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\n[*] Выключение...")
    finally:
        if state.robot_simulator:
            state.robot_simulator.motors.cleanup()
        print("[✓] Готово!")
