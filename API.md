# API М.А.Р.С.

Все `POST`-роуты (кроме `/`) требуют заголовок `X-MARS-Token` — см. `mars/web/auth.py`.
GET-роуты состояния (`/api/robot/state`, `/api/sonar`, `/api/stats`, ...) открыты без токена.

## Робот (`mars/web/routes_robot.py`)

| Роут | Метод | Токен | Описание |
|---|---|---|---|
| `/api/robot/state` | GET | — | Полное состояние робота + сонар + estop |
| `/api/robot/command` | POST | ✓ | `{cmd: forward\|backward\|left\|right\|stop}` |
| `/api/robot/keepalive` | POST | ✓ | Heartbeat, сбрасывает watchdog |
| `/api/robot/estop` | POST | ✓ | `{activate: bool}` — аварийная остановка всего |
| `/api/robot/estop/status` | GET | — | Текущее состояние E-STOP |
| `/api/robot/speed` | POST | ✓ | `{speed: 0-255}` |
| `/api/robot/clear_path` | POST | ✓ | Очистить историю маршрута |
| `/api/motors/sim` | POST | ✓ | `{sim: bool}` — режим симуляции моторов |
| `/api/robot/mode` | POST | ✓ | `{mode: sim\|real}` |
| `/api/autopilot/start` \| `/stop` | POST | ✓ | Управление автопилотом |
| `/api/robot/mission` | POST | ✓ | `{action: start\|stop}` — автономная миссия |

## Сенсоры (`mars/web/routes_sensors.py`)

| Роут | Метод | Токен | Описание |
|---|---|---|---|
| `/api/map` | GET | — | JPEG карты (кэш 500мс) |
| `/api/camera/frame` | GET | — | Текущий кадр с камеры (JPEG) |
| `/api/cameras/list` | GET | — | Список `/dev/video*` |
| `/api/cameras/select` | POST | ✓ | `{device: "/dev/videoN"}` |
| `/api/sonar` | GET | — | Статус дальномера |
| `/api/sonar/mode` | POST | ✓ | `{mode: on\|off}` |
| `/api/sonar/diagnostic` | GET | — | Диагностика GPIO сонара |

## Данные (`mars/web/routes_data.py`)

| Роут | Метод | Токен | Описание |
|---|---|---|---|
| `/api/humans/photo/<id>` | GET | — | Фото найденного человека |
| `/api/humans/list` | GET | — | Список найденных (не используется текущим UI, рабочий резерв) |
| `/api/log/events` | GET | — | Последние 30 событий |
| `/api/log/screenshot` | POST | ✓ | Сохранить текущий кадр |
| `/api/stats` | GET | — | Статистика сессии |
| `/api/map/export` | GET | — | Карта в PNG x2 |
| `/api/report` | GET | — | HTML-отчёт о сессии |
| `/api/motion` | GET | — | Статус детектора движения |
| `/api/heatmap` | GET | — | JPEG тепловой карты (не используется текущим UI) |
| `/api/graphs` | GET | — | Данные для графиков (не используется текущим UI) |

## Рой (`mars/web/routes_swarm.py`) — задел под коптер, без роевой логики

Весь неймспейс требует токен — это единственное место, где заранее заложена
множественность клиентов.

| Роут | Метод | Описание |
|---|---|---|
| `/api/swarm/report_target` | POST | `{peer_id, x, y, confidence}` — второй агент сообщает о найденной цели. Только логируется, автопилот пока не реагирует. |
| `/api/swarm/peers` | GET | Список зарегистрированных узлов (in-memory, не переживает перезапуск) |
| `/api/swarm/peers` | POST | `{peer_id, kind}` — регистрация узла |

Дальнейшее развитие (не реализовано здесь): реакция автопилота на
`report_target`, вытеснение in-memory реестра чем-то персистентным,
собственно децентрализованный протокол координации — см. обсуждение
роевого интеллекта отдельно от этого рефакторинга.
