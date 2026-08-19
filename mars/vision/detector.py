"""Обнаружение людей: Pigo (лица, CLI-бинарник) + HSV skin-color (без нейросети).

skin_color_detect и combined_detect переписаны относительно v30:
- HSV-конверсия и маска векторизованы на numpy вместо попиксельного
  Python-цикла (19200 пикселей на кадр — было главным тормозом на
  Cortex-A7).
- Вместо одного bbox на весь кадр (min/max по всем "кожным" пикселям)
  делается грубая grid-кластеризация — несколько разнесённых объектов
  дают отдельные bbox вместо одного слипшегося прямоугольника.
- Убран мёртвый параметр prev_gray (никогда не использовался —
  MotionDetector ведёт свой prev_gray сам).
"""
import io
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import ImageDraw


class PigoCliDetector:
    """Обёртка над CLI-бинарником Pigo (обнаружение лиц)"""
    def __init__(self, config):
        self.config = config
        self.binary = self._find_pigo_binary()
        self.cascade = self._find_cascade()
        self.tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()

    def _find_pigo_binary(self):
        import shutil
        val = os.environ.get("FACE_PIGO_BIN")
        if val and Path(val).exists():
            return val
        return shutil.which("pigo")

    def _find_cascade(self):
        val = os.environ.get("FACE_PIGO_CASCADE")
        if val and Path(val).exists():
            return val
        candidates = [
            Path(__file__).resolve().parent / "cascade" / "facefinder",
            Path("/usr/local/share/pigo/cascade/facefinder"),
            Path("/usr/share/pigo/cascade/facefinder"),
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    @property
    def available(self):
        return bool(self.binary and self.cascade)

    def detect_bytes(self, jpeg_bytes):
        if not self.available:
            return []
        fd, path = tempfile.mkstemp(prefix="pigo_", suffix=".jpg", dir=self.tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(jpeg_bytes)
            return self.detect_file(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def detect_file(self, image_path):
        cmd = [
            self.binary,
            "-in", image_path,
            "-out", "empty",
            "-cf", self.cascade,
            "-json", "-",
            "-min", str(self.config.pigo_min_size),
            "-max", str(self.config.pigo_max_size),
            "-iou", str(self.config.pigo_iou),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=8)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pigo error")
        return self._parse_json(result.stdout)

    def _parse_json(self, output):
        output = (output or "").strip()
        if not output:
            return []
        start = output.find("[")
        end = output.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        detections = json.loads(output[start:end+1])
        boxes = []
        for item in detections:
            if not isinstance(item, dict):
                continue
            face = item.get("face") if isinstance(item.get("face"), dict) else item
            x = int(face.get("x", 0))
            y = int(face.get("y", 0))
            size = int(face.get("size", face.get("width", 0)))
            if size <= 0:
                continue
            boxes.append({"x": x, "y": y, "size": size,
                          "left": x, "top": y,
                          "right": x + size, "bottom": y + size})
        return boxes


class SimpleFaceDetector:
    """Обёртка над PigoCliDetector с асинхронным вызовом и рисованием bbox"""
    def __init__(self, config):
        self.config = config
        self.detector = PigoCliDetector(config)
        self.last_detections = []
        self.last_jpeg = None
        self.detect_thread = None
        self.last_detect_time = 0
        self.detect_interval = config.detect_interval  # на слабом железе лучше не меньше 0.5
        self.lock = threading.Lock()

        if self.detector.available:
            print(f"[✓] Pigo: {self.detector.binary}")
            print(f"[✓] Cascade: {self.detector.cascade}")
        else:
            print(f"[⚠️] Pigo не найден — обнаружение лиц отключено")
            print(f"     binary: {self.detector.binary}")
            print(f"     cascade: {self.detector.cascade}")

    def detect_async(self, image):
        """Принимает PIL Image, запускает детекцию в фоне"""
        now = time.time()
        if now - self.last_detect_time < self.detect_interval:
            return self.last_detections
        if self.detect_thread and self.detect_thread.is_alive():
            return self.last_detections

        self.last_detect_time = now

        # Конвертируем PIL Image в JPEG bytes для Pigo
        try:
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=75)
            self.last_jpeg = buf.getvalue()
        except Exception:
            return self.last_detections

        self.detect_thread = threading.Thread(target=self._worker, daemon=True)
        self.detect_thread.start()
        return self.last_detections

    def _worker(self):
        if not self.last_jpeg:
            return
        try:
            faces = self.detector.detect_bytes(self.last_jpeg)
            with self.lock:
                self.last_detections = faces
        except Exception as e:
            print(f"[✗] Pigo: сбой детекции — {e}")

    def draw_on_frame(self, img):
        """Рисует зелёные прямоугольники — точно как в оригинале"""
        with self.lock:
            faces = list(self.last_detections)

        if not faces:
            return img

        draw = ImageDraw.Draw(img)
        for face in faces:
            left   = max(0, face["left"])
            top    = max(0, face["top"])
            right  = min(img.width - 1, face["right"])
            bottom = min(img.height - 1, face["bottom"])

            # Двойной прямоугольник как в оригинале
            for offset in range(2):
                draw.rectangle(
                    [left - offset, top - offset, right + offset, bottom + offset],
                    outline=(0, 255, 0),
                )
        return img


# ============================================================================
# HSV skin-color детектор (без нейросети — YOLOv8n отклонён как слишком
# медленный на Cortex-A7 без аппаратного ускорителя, см. диплом п.3.4)
# ============================================================================

_SMALL_W, _SMALL_H = 160, 120
_GRID_CELL = 8                # размер ячейки грубой сетки, px в уменьшенном кадре
_MIN_PIXELS_PER_CELL = 3      # порог "занятости" ячейки
_MIN_CLUSTER_PIXELS = 20      # отсечь шумовые кластеры из пары пикселей
_MIN_BBOX_AREA_FULL = 400     # порог площади bbox в масштабе исходного кадра


def _rgb_to_hsv_np(rgb_float):
    """rgb_float: HxWx3 float64 в [0,1] -> (hue[0,360), sat[0,1], val[0,1])"""
    r, g, b = rgb_float[..., 0], rgb_float[..., 1], rgb_float[..., 2]
    mx = rgb_float.max(axis=-1)
    mn = rgb_float.min(axis=-1)
    df = mx - mn
    df_safe = np.where(df == 0, 1.0, df)

    hue = np.zeros_like(mx)
    mask_r = (mx == r) & (df != 0)
    mask_g = (mx == g) & (df != 0) & ~mask_r
    mask_b = (mx == b) & (df != 0) & ~mask_r & ~mask_g

    hue = np.where(mask_r, (60 * ((g - b) / df_safe)) % 360, hue)
    hue = np.where(mask_g, 60 * ((b - r) / df_safe) + 120, hue)
    hue = np.where(mask_b, 60 * ((r - g) / df_safe) + 240, hue)

    mx_safe = np.where(mx == 0, 1.0, mx)
    sat = np.where(mx == 0, 0.0, df / mx_safe)
    val = mx
    return hue, sat, val


def _cluster_mask(mask):
    """Грубая grid-кластеризация булевой маски (H,W) -> список bbox (x,y,w,h)
    в тех же координатах, что и mask. Без scipy — простой BFS по сетке."""
    h, w = mask.shape
    gh, gw = h // _GRID_CELL, w // _GRID_CELL
    if gh == 0 or gw == 0:
        return []

    occupied = np.zeros((gh, gw), dtype=bool)
    counts = np.zeros((gh, gw), dtype=int)
    for gy in range(gh):
        for gx in range(gw):
            block = mask[gy*_GRID_CELL:(gy+1)*_GRID_CELL, gx*_GRID_CELL:(gx+1)*_GRID_CELL]
            c = int(block.sum())
            counts[gy, gx] = c
            occupied[gy, gx] = c >= _MIN_PIXELS_PER_CELL

    visited = np.zeros_like(occupied)
    boxes = []
    for gy in range(gh):
        for gx in range(gw):
            if not occupied[gy, gx] or visited[gy, gx]:
                continue
            stack = [(gy, gx)]
            visited[gy, gx] = True
            cells = []
            pixel_total = 0
            while stack:
                cy, cx = stack.pop()
                cells.append((cy, cx))
                pixel_total += counts[cy, cx]
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < gh and 0 <= nx < gw and occupied[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))

            if pixel_total < _MIN_CLUSTER_PIXELS:
                continue

            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            bx, by = min(xs) * _GRID_CELL, min(ys) * _GRID_CELL
            bw = (max(xs) + 1) * _GRID_CELL - bx
            bh = (max(ys) + 1) * _GRID_CELL - by
            boxes.append((bx, by, bw, bh))

    return boxes


def skin_color_detect(img):
    """Детектор кожного цвета — HSV-анализ без нейросети.
    Возвращает список (x, y, w, h) в координатах исходного img."""
    try:
        w, h = img.size
        small = img.resize((_SMALL_W, _SMALL_H))
        arr = np.asarray(small, dtype=np.float64) / 255.0  # (120,160,3)
        hue, sat, val = _rgb_to_hsv_np(arr)

        skin_mask = (
            ((hue <= 25) | ((hue >= 170) & (hue <= 180)))
            & (sat >= 0.12) & (sat <= 1.0)
            & (val >= 0.2) & (val <= 1.0)
        )

        clusters = _cluster_mask(skin_mask)

        scale_x = max(1, w // _SMALL_W)
        scale_y = max(1, h // _SMALL_H)
        bboxes = []
        for (bx, by, bw, bh) in clusters:
            fx, fy = bx * scale_x, by * scale_y
            fw, fh = bw * scale_x, bh * scale_y
            if fw * fh > _MIN_BBOX_AREA_FULL:
                bboxes.append((fx, fy, fw, fh))
        return bboxes
    except Exception:
        return []


def combined_detect(img, motion_detector):
    """Комбинированный детектор: движение + skin-color.
    Возвращает (confidence 0-1, bboxes, motion_level)"""
    confidence = 0.0
    bboxes = []

    motion_level = 0.0
    if motion_detector:
        motion_level = motion_detector.process(img)

    skin_boxes = skin_color_detect(img)

    if skin_boxes:
        confidence += 0.6
        bboxes = skin_boxes
    if motion_level > 0.05:
        confidence += 0.4

    confidence = min(1.0, confidence)
    return confidence, bboxes, motion_level
