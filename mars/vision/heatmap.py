"""Тепловая карта покрытия территории."""
from PIL import ImageDraw


class HeatMap:
    CELL = 40

    def __init__(self, width, height):
        self.cols   = width  // self.CELL + 1
        self.rows   = height // self.CELL + 1
        self.visits = [[0] * self.cols for _ in range(self.rows)]
        print(f"[✓] Тепловая карта: {self.cols}x{self.rows} ячеек")

    def update(self, x, y):
        col = int(x // self.CELL)
        row = int(y // self.CELL)
        if 0 <= row < self.rows and 0 <= col < self.cols:
            self.visits[row][col] = min(255, self.visits[row][col] + 1)

    def get_coverage(self):
        visited = sum(1 for row in self.visits for v in row if v > 0)
        total   = self.cols * self.rows
        return round(visited / total * 100, 1) if total > 0 else 0.0

    def draw_overlay(self, img):
        draw = ImageDraw.Draw(img, 'RGBA')
        max_v = max((max(row) for row in self.visits), default=1) or 1

        for r in range(self.rows):
            for c in range(self.cols):
                v = self.visits[r][c]
                if v == 0:
                    continue
                t = v / max_v
                if t < 0.5:
                    red, green, blue = 0, int(t * 2 * 200), 150
                else:
                    red, green, blue = int((t - 0.5) * 2 * 200), 200, 0
                alpha = int(35 + t * 75)
                x0 = c * self.CELL
                y0 = r * self.CELL
                draw.rectangle(
                    [x0, y0, x0 + self.CELL - 1, y0 + self.CELL - 1],
                    fill=(red, green, blue, alpha)
                )
        return img
