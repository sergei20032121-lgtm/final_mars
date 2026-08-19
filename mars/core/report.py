"""Генератор HTML-отчёта о сессии поиска."""
import base64
import io
import math
import time


class ReportGenerator:
    def generate(self, robot_sim, logger_inst, map_img):
        ts     = time.strftime('%d.%m.%Y %H:%M:%S')
        uptime = time.time() - robot_sim.start_time
        path   = list(robot_sim.path_history)

        dist_total = sum(
            math.sqrt((path[i][0]-path[i-1][0])**2 + (path[i][1]-path[i-1][1])**2)
            for i in range(1, len(path))
        )

        photos_html = ""
        for h in robot_sim.found_humans:
            if h.get("photo"):
                ts_h = time.strftime("%H:%M:%S", time.localtime(h["timestamp"]))
                photos_html += f"""
                <div class="photo-card">
                    <img src="data:image/jpeg;base64,{h["photo"]}">
                    <div class="photo-info">
                        <strong>Человек #{h["id"]}</strong><br>
                        {ts_h} · ({h["x"]:.0f}, {h["y"]:.0f})
                    </div>
                </div>"""

        map_b64 = ""
        try:
            buf = io.BytesIO()
            map_img.save(buf, "JPEG", quality=85)
            map_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass

        events_html = ""
        if logger_inst:
            for e in logger_inst.events:
                cls = " found" if "НАЙДЕН" in e["type"] else ""
                events_html += f'<div class="event{cls}">[{e["time"]}] <strong>{e["type"]}</strong> — {e["details"]}</div>\n'

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>М.А.Р.С. — Отчёт {ts}</title>
<style>
    body{{font-family:Arial,sans-serif;margin:30px;color:#222;}}
    h1{{color:#006644;border-bottom:3px solid #006644;padding-bottom:10px;}}
    h2{{color:#004488;margin-top:30px;}}
    .header{{display:flex;justify-content:space-between;align-items:flex-start;}}
    .logo{{font-size:48px;font-weight:900;color:#006644;letter-spacing:4px;}}
    .meta{{text-align:right;color:#666;font-size:13px;}}
    .stats-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin:20px 0;}}
    .stat-box{{background:#f0f8f4;border:1px solid #c0ddd0;border-radius:8px;padding:15px;text-align:center;}}
    .stat-num{{font-size:28px;font-weight:bold;color:#006644;}}
    .stat-lbl{{font-size:12px;color:#666;margin-top:4px;}}
    .map-img{{width:100%;border:2px solid #006644;border-radius:8px;margin:10px 0;}}
    .photo-grid{{display:flex;flex-wrap:wrap;gap:15px;margin:15px 0;}}
    .photo-card{{border:1px solid #ddd;border-radius:8px;overflow:hidden;width:200px;}}
    .photo-card img{{width:100%;display:block;}}
    .photo-info{{padding:8px;font-size:12px;background:#f9f9f9;}}
    .event{{padding:6px 12px;margin:4px 0;border-left:3px solid #006644;background:#f8f8f8;font-size:12px;}}
    .event.found{{border-color:#cc3333;background:#fff5f5;}}
    .footer{{margin-top:40px;padding-top:15px;border-top:1px solid #ddd;font-size:11px;color:#999;text-align:center;}}
    @media print{{@page{{margin:20mm;}}button{{display:none;}}}}
</style>
</head>
<body>
<div class="header">
    <div>
        <div class="logo">М.А.Р.С.</div>
        <div style="color:#666;font-size:13px;margin-top:4px;">Мобильный Автоматизированный Робот Спасатель</div>
    </div>
    <div class="meta">
        <strong>Отчёт о сессии поиска</strong><br>
        Дата: {ts}<br>
        Сессия: {logger_inst.session_id if logger_inst else "—"}
    </div>
</div>
<h1>Результаты поиска</h1>
<div class="stats-grid">
    <div class="stat-box"><div class="stat-num">{len(robot_sim.found_humans)}</div><div class="stat-lbl">Найдено людей</div></div>
    <div class="stat-box"><div class="stat-num">{int(uptime//60)}м {int(uptime%60)}с</div><div class="stat-lbl">Время поиска</div></div>
    <div class="stat-box"><div class="stat-num">{dist_total/100:.1f} м</div><div class="stat-lbl">Пройдено</div></div>
    <div class="stat-box"><div class="stat-num">{len(path)}</div><div class="stat-lbl">Точек маршрута</div></div>
    <div class="stat-box"><div class="stat-num">{int(robot_sim.battery_percent)}%</div><div class="stat-lbl">Батарея</div></div>
    <div class="stat-box"><div class="stat-num">{"GPIO" if robot_sim.motors.enabled else "СИМ"}</div><div class="stat-lbl">Режим</div></div>
</div>
<h2>Карта маршрута</h2>
{"<img class=\"map-img\" src=\"data:image/jpeg;base64," + map_b64 + "\">" if map_b64 else "<p>Карта недоступна</p>"}
<h2>Обнаруженные люди ({len(robot_sim.found_humans)})</h2>
{"<div class=\"photo-grid\">" + photos_html + "</div>" if photos_html else "<p>Людей не обнаружено</p>"}
<h2>Журнал событий</h2>
<div>{events_html}</div>
<button onclick="window.print()" style="margin-top:20px;padding:12px 24px;background:#006644;color:white;border:none;border-radius:6px;font-size:14px;cursor:pointer;">🖨️ Печать / Сохранить PDF</button>
<div class="footer">М.А.Р.С. v3.0 · Orange Pi PC H3 · {time.strftime("%Y")}</div>
</body></html>"""
