"""Логирование маршрута, событий и скриншотов."""
import os
import time


class Logger:
    """Логирование маршрута, событий и скриншотов"""

    def __init__(self):
        self.log_dir = os.path.expanduser('~/mapc_logs')
        os.makedirs(self.log_dir, exist_ok=True)

        # Текущая сессия
        self.session_id = time.strftime('%Y%m%d_%H%M%S')
        self.session_dir = os.path.join(self.log_dir, self.session_id)
        os.makedirs(self.session_dir, exist_ok=True)

        self.log_file = os.path.join(self.session_dir, 'events.log')
        self.path_file = os.path.join(self.session_dir, 'path.csv')
        self.events = []

        # CSV заголовок для маршрута
        with open(self.path_file, 'w') as f:
            f.write('timestamp,x,y,angle,command,distance_cm\n')

        self.log_event('SESSION_START', f'Сессия {self.session_id}')
        print(f"[✓] Logger: {self.session_dir}")
        # TODO: добавить ротацию логов чтобы не забивало диск

    def log_event(self, event_type, details=''):
        """Записать событие"""
        ts = time.strftime('%H:%M:%S')
        line = f"[{ts}] {event_type}: {details}"
        self.events.append({'time': ts, 'type': event_type, 'details': details})

        with open(self.log_file, 'a') as f:
            f.write(line + '\n')

    def log_path(self, x, y, angle, command, distance_cm):
        """Записать точку маршрута"""
        ts = time.time()
        with open(self.path_file, 'a') as f:
            f.write(f'{ts:.2f},{x:.1f},{y:.1f},{angle:.1f},{command},{distance_cm:.1f}\n')

    def save_screenshot(self, image, label='detection'):
        """Сохранить скриншот"""
        try:
            filename = f'{label}_{time.strftime("%H%M%S")}.jpg'
            path = os.path.join(self.session_dir, filename)
            image.save(path, 'JPEG', quality=85)
            self.log_event('SCREENSHOT', filename)
            return filename
        except Exception:
            return None

    def get_recent_events(self, n=20):
        """Получить последние N событий"""
        return self.events[-n:]

    def get_stats(self):
        """Статистика сессии"""
        return {
            'session_id': self.session_id,
            'log_dir': self.session_dir,
            'events_count': len(self.events),
            'uptime': time.strftime('%H:%M:%S'),
        }
