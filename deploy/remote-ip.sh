#!/usr/bin/env bash
# Установка «Живой ленты» без домена — по IP, только http.
#
#   ssh root@IP 'bash -s' < deploy/remote-ip.sh
#
# Скрипт сам узнаёт внешний адрес сервера и подставит его в QR-код.
# Режим для теста: пароль администратора идёт открытым текстом, защита
# обеспечивается только лимитом на 8 попыток входа за 5 минут.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/live-feed"
APP_USER="livefeed"

if [[ $EUID -ne 0 ]]; then
    echo "Запускать от root" >&2
    exit 1
fi
if [[ ! -d "$SRC_DIR/app" || ! -d "$SRC_DIR/static" ]]; then
    echo "Ожидаю app/ и static/ рядом с deploy/. Запускайте из корня проекта." >&2
    exit 1
fi

echo "==> Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx curl

echo "==> Разворачиваю приложение в $APP_DIR"
mkdir -p "$APP_DIR/data/events"
rm -rf "$APP_DIR/app" "$APP_DIR/static"
cp -r "$SRC_DIR/app" "$SRC_DIR/static" "$APP_DIR/"
cp "$SRC_DIR/run.py" "$SRC_DIR/requirements.txt" "$APP_DIR/"
rm -rf "$APP_DIR/app/__pycache__"

echo "==> Создаю виртуальное окружение"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Завожу пользователя $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

# при повторном запуске пароль сохраняем, иначе модератора выкинет из входа
if [[ -f /etc/live-feed.env ]] && grep -q '^ADMIN_PASSWORD=.' /etc/live-feed.env; then
    ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' /etc/live-feed.env | head -1)"
    echo "==> Пароль администратора взят из прошлой установки"
else
    ADMIN_PASSWORD="$(python3 -c 'import secrets,string; a=string.ascii_uppercase+string.digits; print("".join(secrets.choice(a) for _ in range(12)))')"
fi

# --- узнаём адрес, по которому сервер виден снаружи -------------------------
SERVER_IP="${SERVER_IP:-}"
if [[ -z "$SERVER_IP" ]]; then
    SERVER_IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
fi
if [[ -z "$SERVER_IP" ]]; then
    SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [[ -z "$SERVER_IP" ]]; then
    echo "!! Не смог определить адрес сервера. Запустите с SERVER_IP=ваш.адрес" >&2
    exit 1
fi

cat > /etc/live-feed.env <<EOF
ADMIN_PASSWORD=$ADMIN_PASSWORD
PUBLIC_BASE_URL=http://$SERVER_IP
TRUSTED_PROXIES=127.0.0.1,::1
FORCE_HTTPS=false
COOKIE_SECURE=false
TRUSTED_HOSTS=$SERVER_IP
EOF
chown root:"$APP_USER" /etc/live-feed.env
chmod 640 /etc/live-feed.env

echo "==> Ставлю systemd-юнит"
cp "$SRC_DIR/deploy/live-feed.service" /etc/systemd/system/live-feed.service
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR/data" "$APP_DIR/data/events"
chown -R root:"$APP_USER" "$APP_DIR"
chmod -R g+rX "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"

systemctl daemon-reload
systemctl enable live-feed.service
# restart, а не enable --now: иначе при повторной установке уже
# работающий сервис продолжит держать в памяти прежний код
systemctl restart live-feed.service
sleep 3
if ! systemctl is-active --quiet live-feed.service; then
    echo "!! Сервис не поднялся, последние строки лога:" >&2
    journalctl -u live-feed --no-pager -n 20 >&2
    exit 1
fi

echo "==> Настраиваю nginx (http, без сертификата)"
cp "$SRC_DIR/deploy/nginx-websocket.conf" /etc/nginx/conf.d/live-feed-websocket.conf
cp "$SRC_DIR/deploy/nginx-http.conf" /etc/nginx/sites-available/live-feed
ln -sf /etc/nginx/sites-available/live-feed /etc/nginx/sites-enabled/live-feed
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx || systemctl restart nginx

sleep 1
echo
echo "Готово: http://$SERVER_IP/admin"
echo "Пароль администратора: $ADMIN_PASSWORD"
echo "Витрина:    http://$SERVER_IP/e/АДРЕС/"
echo "Экран:      http://$SERVER_IP/e/АДРЕС/screen"
echo "События заводятся в админке: там появятся три ссылки и QR"
echo
echo "Внимание: соединение без шифрования. Пароль администратора передаётся"
echo "открытым текстом — это годится для теста, но не для настоящего"
echo "мероприятия. Для публичного интернета нужен домен и сертификат:"
echo "  deploy/remote.sh с доменом вместо этого скрипта."
echo "Логи: journalctl -u live-feed -f"
