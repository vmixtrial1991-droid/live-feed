#!/usr/bin/env bash
# Установка «Живой ленты» одной командой, запускается из корня проекта.
#
#   ssh root@IP 'bash -s' ваш-домен.ru < deploy/remote.sh
#
# Либо через scp, если так привычнее:
#   scp -r app static run.py requirements.txt deploy root@IP:/tmp/livefeed
#   ssh root@IP 'bash /tmp/livefeed/deploy/install.sh ваш-домен.ru /tmp/livefeed'
#
# Переменные окружения (необязательно):
#   ACME_EMAIL=почта@домен    — для регистрации в Let's Encrypt

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/live-feed"
APP_USER="livefeed"

# Домен приходит аргументом после команды: ssh ... 'bash -s' example.ru
DOMAIN="${DOMAIN:-${1:-}}"
ACME_EMAIL="${ACME_EMAIL:-admin@$DOMAIN}"

if [[ -z "$DOMAIN" ]]; then
    echo "Не передан домен. Пример: ssh root@IP 'bash -s' example.ru < deploy/remote.sh" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Запускать от root" >&2
    exit 1
fi
if [[ ! -d "$SRC_DIR/app" || ! -d "$SRC_DIR/static" ]]; then
    echo "Скрипт ожидает app/ и static/ рядом с собой. Запускайте из корня проекта." >&2
    exit 1
fi

echo "==> Домен: $DOMAIN"
echo "==> Исходники: $SRC_DIR"

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

# при повторном запуске пароль сохраняем
if [[ -f /etc/live-feed.env ]] && grep -q '^ADMIN_PASSWORD=.' /etc/live-feed.env; then
    ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' /etc/live-feed.env | head -1)"
    echo "==> Пароль администратора взят из прошлой установки"
else
    ADMIN_PASSWORD="$(python3 -c 'import secrets,string; a=string.ascii_uppercase+string.digits; print("".join(secrets.choice(a) for _ in range(12)))')"
fi

cat > /etc/live-feed.env <<EOF
ADMIN_PASSWORD=$ADMIN_PASSWORD
PUBLIC_BASE_URL=https://$DOMAIN
TRUSTED_PROXIES=127.0.0.1,::1
FORCE_HTTPS=true
COOKIE_SECURE=true
TRUSTED_HOSTS=$DOMAIN
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
systemctl is-active --quiet live-feed.service || {
    echo "!! Сервис не поднялся, последние строки лога:" >&2
    journalctl -u live-feed --no-pager -n 20 >&2
    exit 1
}

echo "==> Настраиваю nginx"
cp "$SRC_DIR/deploy/nginx-websocket.conf" /etc/nginx/conf.d/live-feed-websocket.conf
sed "s/example\.com/$DOMAIN/g" "$SRC_DIR/deploy/nginx.conf" \
    > /etc/nginx/sites-available/live-feed
ln -sf /etc/nginx/sites-available/live-feed /etc/nginx/sites-enabled/live-feed
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/html

echo "==> Получаю сертификат Let's Encrypt"
mkdir -p /root/.acme.sh
if [[ ! -x /root/.acme.sh/acme.sh ]]; then
    curl -fsSL https://acme.sh/install.sh -o /root/.acme.sh/acme.sh
    chmod +x /root/.acme.sh/acme.sh
fi
/root/.acme.sh/acme.sh --install-cronjob >/dev/null 2>&1 || true

CERT_OK=0
if /root/.acme.sh/acme.sh --issue --server letsencrypt \
     --webroot /var/www/html -d "$DOMAIN" --keylength ec-256 \
     --register-account "mailto:$ACME_EMAIL" 2>&1 | tail -4; then
    mkdir -p "/etc/letsencrypt/live/$DOMAIN"
    if /root/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
         --key-file "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
         --fullchain-file "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
         --reloadcmd "systemctl reload nginx" 2>&1 | tail -2; then
        CERT_OK=1
    fi
fi

if [[ "$CERT_OK" != "1" ]]; then
    echo "!! Сертификат не выдали — обычно закрыт порт 80 или DNS ещё не смотрит на $DOMAIN." >&2
    echo "   Приложение доступно по http://$DOMAIN и работает." >&2
    echo "   Повторить выпуск сертификата позже:" >&2
    echo "     /root/.acme.sh/acme.sh --issue --server letsencrypt --webroot /var/www/html \\" >&2
    echo "       -d $DOMAIN --keylength ec-256 --force" >&2
    # конфиг с https без сертификата не поднимется — оставляем рабочий http
    sed -i "s|https://$DOMAIN|http://$DOMAIN|g" /etc/nginx/sites-available/live-feed
fi

nginx -t
systemctl reload nginx || systemctl restart nginx

sleep 1
echo
echo "Готово: https://$DOMAIN/admin"
echo "Пароль администратора: $ADMIN_PASSWORD"
echo "События заводятся в админке: там появятся три ссылки и QR"
echo "Логи: journalctl -u live-feed -f"
