"""Простой детектор движения — сравнивает соседние кадры (grayscale diff).

Векторизовано на numpy относительно v30 (был чистый Python-цикл по 4800
пикселям на каждый кадр — эта функция вызывается для каждого кадра видео,
не троттлится, так что имело смысл ускорить и её, не только skin-детектор).
Формула (сумма abs-разницы / (w*h*255)) не менялась — только реализация.
"""
import threading

import numpy as np


class MotionDetector:
    """Простой детектор движения — сравнивает кадры"""
    def __init__(self, threshold=0.08):
        self.threshold     = threshold
        self.prev_gray     = None
        self.motion_level  = 0.0
        self.motion_detected = False
        self.lock          = threading.Lock()
        print("[✓] Детектор движения: готов")

    def process(self, img):
        try:
            small = img.resize((80, 60)).convert('L')
            arr = np.asarray(small, dtype=np.int32)
            w, h = small.size

            if self.prev_gray is None:
                self.prev_gray = arr
                return 0.0

            diff = int(np.abs(arr - self.prev_gray).sum())
            level = diff / (w * h * 255)
            self.prev_gray = arr

            with self.lock:
                self.motion_level    = round(level, 3)
                self.motion_detected = level > self.threshold

            return level
        except Exception:
            return 0.0

    def get_status(self):
        with self.lock:
            return {
                'level':    self.motion_level,
                'detected': self.motion_detected,
                'percent':  round(self.motion_level * 100, 1),
            }
