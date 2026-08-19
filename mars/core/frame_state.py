"""Общий последний кадр с камеры — единая точка доступа вместо голых
module-level globals `current_frame`/`frame_lock`, которые раньше
были раскиданы по всему файлу и читались/писались из разных потоков
напрямую."""
import threading

_frame = None
_lock = threading.Lock()


def set_frame(img):
    global _frame
    with _lock:
        _frame = img


def get_frame():
    """Текущий кадр (без копии) — для обработки внутри лока вызывающим кодом."""
    with _lock:
        return _frame


def get_frame_copy():
    """Копия текущего кадра — безопасна для использования вне лока."""
    with _lock:
        return _frame.copy() if _frame is not None else None
