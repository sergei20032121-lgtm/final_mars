"""Рендер карты (используется и роутом /api/map, и генератором отчёта)."""
import math

from PIL import Image, ImageDraw

from mars.config import robot_config


def draw_map(robot_simulator, sonar_sensor, heatmap, autopilot):
    """Оптимизированная карта — слои"""
    W = robot_config.map_width
    H = robot_config.map_height

    img  = Image.new('RGB', (W, H), color='#060d0a')
    draw = ImageDraw.Draw(img)

    # Сетка (тонкая)
    for x in range(0, W, 60):
        draw.line([(x,0),(x,H)], fill=(15,28,18), width=1)
    for y in range(0, H, 60):
        draw.line([(0,y),(W,y)], fill=(15,28,18), width=1)

    # Граница зоны автопилота
    o = 60
    draw.rectangle([o,o,W-o,H-o], outline=(25,50,30), width=1)

    # Тепловая карта покрытия
    if heatmap:
        img = heatmap.draw_overlay(img)
        draw = ImageDraw.Draw(img)

    # Облако точек сонара
    if sonar_sensor:
        with sonar_sensor.lock:
            pts = list(sonar_sensor.radar_points)
        for pt in pts[::2]:  # Каждую вторую точку — быстрее
            if pt['dist'] < 260:
                px = pt['x'] + math.sin(math.radians(pt['angle'])) * pt['dist']
                py = pt['y'] - math.cos(math.radians(pt['angle'])) * pt['dist']
                if 0 <= px < W and 0 <= py < H:
                    draw.ellipse([px-2,py-2,px+2,py+2], fill=(160,80,20))

    # Луч сонара
    if sonar_sensor and robot_simulator:
        s = sonar_sensor.get_status()
        dist    = s['distance_cm']
        rx, ry  = robot_simulator.x, robot_simulator.y
        sweep   = s['sweep_angle']
        abs_ang = (robot_simulator.angle + sweep) % 360
        ang_r   = math.radians(abs_ang)
        cone    = min(dist, 180)

        # Конус (только центральный и ±15°)
        for a in [-15, 0, 15]:
            ar = math.radians(abs_ang + a)
            alpha = 30 if a == 0 else 12
            draw.line([(rx,ry),(rx+math.sin(ar)*cone, ry-math.cos(ar)*cone)],
                      fill=(0,alpha,0), width=1)

        # Луч
        draw.line([(rx,ry),(rx+math.sin(ang_r)*cone, ry-math.cos(ang_r)*cone)],
                  fill=(0,200,60), width=2)

        # Маркер препятствия
        if dist < 180:
            ox = rx + math.sin(ang_r)*dist
            oy = ry - math.cos(ang_r)*dist
            c  = (220,50,50) if dist < 30 else (220,140,0)
            draw.ellipse([ox-4,oy-4,ox+4,oy+4], fill=c)

    # История пути — упрощённо
    path = list(robot_simulator.path_history)
    if len(path) > 1:
        # Рисуем только каждую вторую точку
        pts2 = path[::2]
        total = len(pts2)
        for i in range(1, total):
            t = i / total
            g = int(60 + t * 140)
            draw.line([pts2[i-1], pts2[i]], fill=(0, g, int(g*0.4)), width=2)

    # База
    if robot_simulator:
        bx, by = robot_simulator.base_x, robot_simulator.base_y
        draw.ellipse([bx-14,by-14,bx+14,by+14], outline=(60,120,200), width=1)
        draw.ellipse([bx-5, by-5, bx+5, by+5],  fill=(60,120,200))

    # Найденные люди
    for h in robot_simulator.found_humans:
        x, y = h['x'], h['y']
        if 0 <= x < W and 0 <= y < H:
            draw.ellipse([x-10,y-10,x+10,y+10], fill=(180,50,50), outline=(255,120,80), width=2)
            draw.text((x-3,y-7), str(h['id']), fill=(255,255,255))

    # Робот
    rx = max(5, min(W-5, robot_simulator.x))
    ry = max(5, min(H-5, robot_simulator.y))
    robot_simulator.x, robot_simulator.y = rx, ry

    s    = robot_config.robot_size
    ang  = math.radians(robot_simulator.angle)
    ex   = rx + s*1.8*math.sin(ang)
    ey   = ry - s*1.8*math.cos(ang)

    draw.ellipse([rx-s,ry-s,rx+s,ry+s], fill=(40,180,80), outline=(0,230,60), width=2)
    draw.line([(rx,ry),(ex,ey)], fill=(0,180,255), width=3)

    # Автопилот режим
    if autopilot and autopilot.enabled:
        colors = {'ПОИСК':(0,200,80),'ОБЪЕЗД':(220,140,0),
                  'НАЙДЕН':(220,50,50),'ВОЗВРАТ':(80,120,255)}
        c = colors.get(autopilot.режим, (150,150,150))
        draw.text((W-110,8), f"АВТО:{autopilot.режим}", fill=c)

    # HUD
    draw.text((8,8),  f"({int(rx)},{int(ry)}) {int(robot_simulator.angle)}°",
              fill=(50,160,80))
    if heatmap:
        draw.text((8,24), f"Покрытие: {heatmap.get_coverage():.0f}%",
                  fill=(40,120,60))
    if sonar_sensor:
        dist = sonar_sensor.distance_cm
        c2 = (220,50,50) if dist < 30 else (160,230,80)
        draw.text((8,40), f"Сонар: {dist:.0f}см", fill=c2)

    return img
