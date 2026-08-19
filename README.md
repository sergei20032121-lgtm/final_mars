# final_mars

М.А.Р.С. — Мобильный Автоматизированный Робот-Спасатель. Orange Pi PC H3 + Flask.

## Структура

- `mars/` — весь код (было два монолитных файла `robot_rescue_demo.py`/`robot_rescue_demo_v30.py`, теперь разбито на модули — `hardware/`, `vision/`, `core/`, `web/`). `robot_rescue_demo_v30.py` был источником истины (проверенная на роботе инвертированная логика L9110S и watchdog) и лёг в основу пакета.
- `robot_rescue_demo.py` — тонкий entrypoint (`from mars.app import run`), путь не менялся — `setup_autostart.sh` продолжает работать без правок.
- `templates/`, `static/` — фронтенд, вынесен из инлайновой Python-строки в нормальные `.html`/`.css`/`.js`.
- `tests/` — юнит- и smoke-тесты, гоняются без физического робота (`sim_mode`).
- `install_deps.sh` — обязательно первым, ставит системные пакеты и зависимости из `requirements.txt`.
- `API.md` — схема JSON-эндпоинтов, включая `/api/swarm/*` — задел под второго сетевого агента (коптер).
- `setup_autostart.sh` — автозапуск для Linux. WiFi SSID/пароль теперь передаются переменными окружения (`MARS_WIFI_SSID`, `MARS_WIFI_PASS`), не хардкодятся в файле.

## Запуск

```bash
./install_deps.sh
source ~/mapc_env/bin/activate
python3 robot_rescue_demo.py
```

Открыть `http://<IP>:5000`. Команды роботу (POST-роуты) требуют заголовок
`X-MARS-Token` — браузер получает его автоматически при рендере страницы,
внешним клиентам (см. `/api/swarm/*` в `API.md`) нужно передать его вручную
(значение — в `.mars_token`, создаётся при первом запуске).

## Тесты

```bash
pip install pytest
python -m pytest tests/
```
