#!/bin/bash
# M.A.P.C. - Установка зависимостей
# Orange Pi PC H3 / Armbian

echo "=== M.A.P.C. Setup ==="
echo ""

# Обновление
echo "[1] Обновление системы..."
sudo apt-get update -qq

# Системные пакеты
echo "[2] Установка системных пакетов..."
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    python3-venv \
    python3-pil \
    python3-serial \
    python3-numpy \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libopenjp2-7-dev \
    libtiff-dev \
    libwebp-dev \
    ffmpeg \
    v4l-utils \
    fswebcam \
    wget \
    ca-certificates \
    golang-go

# Виртуальное окружение с доступом к системным пакетам
echo "[3] Создание виртуального окружения..."
python3 -m venv ~/mapc_env --system-site-packages
source ~/mapc_env/bin/activate

# pip пакеты — версии зафиксированы в requirements.txt
echo "[4] Установка pip пакетов..."
pip install --upgrade pip --quiet
pip install -r "$(dirname "$0")/requirements.txt" --quiet || {
    echo "   ⚠️  Не всё поставилось из requirements.txt, пробуем по частям..."
    pip install flask    --quiet 2>/dev/null || sudo apt-get install -y python3-flask
    pip install pyserial  --quiet 2>/dev/null || sudo apt-get install -y python3-serial
    pip install OPi.GPIO --quiet 2>/dev/null || echo "   ⚠️  GPIO установить не удалось — моторы в симуляции"
}

deactivate

# GPIO права
echo "[5] Настройка прав..."
sudo usermod -aG gpio $USER 2>/dev/null || true
sudo chmod a+rw /dev/gpiomem 2>/dev/null || true

# Pigo
echo "[6] Установка Pigo (обнаружение лиц)..."
export GOPATH="${GOPATH:-$HOME/go}"
export PATH="$PATH:$GOPATH/bin"
export GO111MODULE=on

if command -v go &>/dev/null; then
    go install github.com/esimov/pigo/cmd/pigo@latest 2>/dev/null || true
    if [ -f "$GOPATH/bin/pigo" ]; then
        sudo install -m 0755 "$GOPATH/bin/pigo" /usr/local/bin/pigo
        sudo mkdir -p /usr/local/share/pigo/cascade
        sudo wget -q -O /usr/local/share/pigo/cascade/facefinder \
            https://raw.githubusercontent.com/esimov/pigo/master/cascade/facefinder || true
        echo "   ✓ Pigo установлен"
    fi
else
    echo "   ⚠️  Go не найден"
fi

# Скрипт запуска
echo "[7] Создание скрипта запуска..."
cat > ~/start_mapc.sh << 'LAUNCH'
#!/bin/bash
source ~/mapc_env/bin/activate
sudo ~/mapc_env/bin/python3 ~/robot_rescue_demo.py
LAUNCH
chmod +x ~/start_mapc.sh

# Итог
echo ""
echo "=== Проверка ==="
source ~/mapc_env/bin/activate
python -c "import flask;        print('✓ Flask')"      2>/dev/null || echo "✗ Flask"
python -c "from PIL import Image; print('✓ Pillow')"   2>/dev/null || echo "✗ Pillow"
python -c "import numpy;        print('✓ numpy')"     2>/dev/null || echo "✗ numpy"
python -c "import serial;       print('✓ PySerial')"   2>/dev/null || echo "✗ PySerial"
python -c "import OPi.GPIO;     print('✓ OPi.GPIO')"  2>/dev/null || echo "✗ OPi.GPIO (симуляция)"
command -v pigo >/dev/null      && echo "✓ Pigo"       || echo "✗ Pigo (без детекции лиц)"
ls /dev/video*  >/dev/null 2>&1 && echo "✓ Камера: $(ls /dev/video*)" || echo "✗ Камера не найдена"
deactivate

echo ""
echo "=== ГОТОВО ==="
echo ""
echo "Запуск:"
echo "  ./start_mapc.sh"
echo ""
echo "Или:"
echo "  source ~/mapc_env/bin/activate"
echo "  sudo ~/mapc_env/bin/python3 robot_rescue_demo.py"
echo ""
echo "Браузер: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
