"""Держатель всех shared-инстансов приложения.

Раньше (v30) все эти объекты были голыми module-level globals, которые
роуты и хелперы закрывали напрямую. AppState заменяет это одним явным
объектом, который создаётся в mars.app.initialize_system() и кладётся
в app.state — блюпринты читают его через flask.current_app.state.
"""


class AppState:
    def __init__(self):
        self.robot_simulator = None
        self.camera_manager = None
        self.face_detector = None
        self.sonar_sensor = None
        self.logger = None
        self.autopilot = None
        self.motion_detector = None
        self.heatmap = None
        self.report_generator = None
        self.safety = None
