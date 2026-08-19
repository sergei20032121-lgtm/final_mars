"""skin_color_detect на синтетических изображениях — без камеры/железа."""
import numpy as np
from PIL import Image

from mars.vision.detector import skin_color_detect


def _solid_image(w, h, rgb):
    arr = np.full((h, w, 3), rgb, dtype=np.uint8)
    return Image.fromarray(arr, mode='RGB')


def test_empty_black_image_has_no_bbox():
    img = _solid_image(320, 240, (0, 0, 0))
    assert skin_color_detect(img) == []


def test_solid_blue_image_has_no_bbox():
    img = _solid_image(320, 240, (20, 40, 220))
    assert skin_color_detect(img) == []


def test_skin_colored_patch_is_detected():
    arr = np.zeros((240, 320, 3), dtype=np.uint8)
    arr[:, :] = (30, 30, 30)  # тёмный фон
    # Кожный оттенок (высокий R, средний G, низкий B) на приличной площади
    arr[60:180, 60:180] = (220, 170, 140)
    img = Image.fromarray(arr, mode='RGB')

    bboxes = skin_color_detect(img)
    assert len(bboxes) >= 1
    x, y, w, h = bboxes[0]
    # bbox должен пересекаться с областью патча
    assert x < 180 and x + w > 60
    assert y < 180 and y + h > 60


def test_two_separated_patches_yield_two_clusters():
    arr = np.zeros((240, 320, 3), dtype=np.uint8)
    arr[:, :] = (30, 30, 30)
    arr[10:60, 10:60]   = (220, 170, 140)
    arr[180:230, 260:310] = (220, 170, 140)
    img = Image.fromarray(arr, mode='RGB')

    bboxes = skin_color_detect(img)
    assert len(bboxes) == 2
