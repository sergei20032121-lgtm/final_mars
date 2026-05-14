#!/bin/bash
# М.А.Р.С. — Настройка автозапуска
# Orange Pi PC H3 / Armbian / пользователь komar

set -e

WIFI_SSID="rt-80"
WIFI_PASS="77599550570"
WIFI_IFACE="wlxa047d77359bf"
SCRIPT="/home/komar/Desktop/robot_rescue_demo.py"
VENV="/home/komar/mapc_env"
LOG="/var/log/mars.log"

echo "╔═══════════════════════════════════════════════════════╗"
echo "║  М.А.Р.С. — Настройка автозапуска                    ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# ── 1. WiFi пакеты ────────────────────────────────────────────────────────
echo "[1/5] Установка пакетов WiFi..."
sudo apt-get install -y wpasupplicant network-manager -qq 2>/dev/null || true
echo "   ✓ Готово"

# ── 2. Настройка WiFi через NetworkManager ────────────────────────────────
echo "[2/5] Настройка WiFi (${WIFI_SSID})..."

sudo mkdir -p /etc/NetworkManager/system-connections

UUID=$(cat /proc/sys/kernel/random/uuid)

sudo tee /etc/NetworkManager/system-connections/mars-wifi.nmconnection > /dev/null << EOF
[connection]
id=mars-wifi
uuid=${UUID}
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=${WIFI_SSID}

[wifi-security]
auth-alg=open
key-mgmt=wpa-psk
psk=${WIFI_PASS}

[ipv4]
method=auto

[ipv6]
addr-gen-mode=stable-privacy
method=auto
EOF

sudo chmod 600 /etc/NetworkManager/system-connections/mars-wifi.nmconnection
sudo systemctl restart NetworkManager 2>/dev/null || true
echo "   ✓ WiFi настроен"

# ── 3. Скрипт запуска М.А.Р.С. ───────────────────────────────────────────
echo "[3/5] Создание скрипта запуска..."

sudo tee /usr/local/bin/mars_start.sh > /dev/null << EOF
#!/bin/bash
# М.А.Р.С. — Автозапуск

LOG="${LOG}"
WIFI_IFACE="${WIFI_IFACE}"
WIFI_SSID="${WIFI_SSID}"
VENV="${VENV}"
SCRIPT="${SCRIPT}"

echo "=== М.А.Р.С. Запуск \$(date) ===" >> \$LOG

# Ждём WiFi адаптер (до 30 сек)
for i in \$(seq 1 30); do
    ip link show \$WIFI_IFACE &>/dev/null && break || sleep 1
done
echo "WiFi адаптер: \$(ip link show \$WIFI_IFACE 2>/dev/null | head -1)" >> \$LOG

# Поднимаем интерфейс
ip link set \$WIFI_IFACE up 2>/dev/null || true
sleep 2

# Подключаемся к WiFi
nmcli device wifi connect "\$WIFI_SSID" ifname \$WIFI_IFACE 2>>\$LOG || true
sleep 5

# Ждём IP (до 30 сек)
for i in \$(seq 1 30); do
    IP=\$(ip -4 addr show \$WIFI_IFACE 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    [ -n "\$IP" ] && break || sleep 1
done

echo "IP: \${IP:-не получен}" >> \$LOG
echo "\${IP:-}" > /tmp/mars_ip.txt

# Запуск М.А.Р.С.
echo "Запуск М.А.Р.С...." >> \$LOG
if [ -f "\$VENV/bin/python3" ]; then
    \$VENV/bin/python3 \$SCRIPT >> \$LOG 2>&1
else
    python3 \$SCRIPT >> \$LOG 2>&1
fi
EOF

sudo chmod +x /usr/local/bin/mars_start.sh
echo "   ✓ Скрипт создан"

# ── 4. Systemd сервис ─────────────────────────────────────────────────────
echo "[4/5] Создание systemd сервиса..."

sudo tee /etc/systemd/system/mars.service > /dev/null << EOF
[Unit]
Description=М.А.Р.С. — Мобильный Автоматизированный Робот Спасатель
After=network.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=root
ExecStartPre=/bin/sleep 8
ExecStart=/usr/local/bin/mars_start.sh
Restart=on-failure
RestartSec=15
StandardOutput=append:${LOG}
StandardError=append:${LOG}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mars.service
echo "   ✓ Сервис включён в автозапуск"

# ── 5. Sudo, автовход, show_ip ────────────────────────────────────────────
echo "[5/5] Финальная настройка..."

# sudo без пароля
if ! sudo grep -q "komar ALL=(ALL) NOPASSWD" /etc/sudoers 2>/dev/null; then
    echo "komar ALL=(ALL) NOPASSWD: ALL" | sudo tee -a /etc/sudoers > /dev/null
    echo "   ✓ sudo без пароля"
else
    echo "   ✓ sudo уже настроен"
fi

# Автовход LightDM
LDCONF="/etc/lightdm/lightdm.conf"
if [ -f "$LDCONF" ] && ! grep -q "autologin-user=komar" "$LDCONF" 2>/dev/null; then
    sudo tee -a "$LDCONF" > /dev/null << EOF

[Seat:*]
autologin-user=komar
autologin-user-timeout=0
EOF
    echo "   ✓ Автовход настроен"
else
    echo "   ✓ Автовход уже настроен / LightDM не найден"
fi

# show_ip.sh на рабочем столе
tee /home/komar/Desktop/show_ip.sh > /dev/null << EOF
#!/bin/bash
IP=\$(ip -4 addr show ${WIFI_IFACE} 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
if [ -n "\$IP" ]; then
    echo "✓ М.А.Р.С. запущен!"
    echo "  Открой: http://\$IP:5000"
    zenity --info --title="М.А.Р.С." \
           --text="Открой браузер:\nhttp://\$IP:5000" \
           --width=250 2>/dev/null || true
else
    echo "✗ WiFi не подключён"
    zenity --error --title="М.А.Р.С." \
           --text="WiFi не подключён!\nПроверь адаптер." 2>/dev/null || true
fi
EOF
chmod +x /home/komar/Desktop/show_ip.sh
echo "   ✓ show_ip.sh создан"

# ── Итог ──────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ ГОТОВО! Перезагрузи Orange Pi:"
echo ""
echo "   sudo reboot"
echo ""
echo "После загрузки:"
echo "   • WiFi подключится к '${WIFI_SSID}' автоматически"
echo "   • М.А.Р.С. запустится сам"
echo "   • show_ip.sh на рабочем столе покажет IP"
echo "   • Открой браузер: http://<IP>:5000"
echo ""
echo "Управление сервисом:"
echo "   sudo systemctl status mars"
echo "   sudo systemctl stop mars"
echo "   sudo systemctl restart mars"
echo "   cat ${LOG}"
echo "═══════════════════════════════════════════════════════"
