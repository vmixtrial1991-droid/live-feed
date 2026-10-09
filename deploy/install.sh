#!/usr/bin/env bash
# Установка «Живой ленты» на чистый VPS: Ubuntu 22.04 / 24.04, Debian 12.
#
#   sudo bash deploy/install.sh example.com
#
# Скрипт ставит nginx, сертификат, python-окружение и сервис systemd.
# Пароль администратора генерируется и печатается в конце.

set -euo pipefail

DOMAIN="${1:-}"
SRC_DIR="${2:-$PWD}"
APP_DIR="/opt/live-feed"
APP_USER="livefeed"
ACME_EMAIL="${ACME_EMAIL:-admin@$DOMAIN}"

if [[ -z "$DOMAIN" ]]; then
    echo "Укажите домен: sudo bash install.sh example.com" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "Запускать от root: sudo bash install.sh $DOMAIN" >&2
    exit 1
fi

if [[ "$DOMAIN" == *"://"* || "$DOMAIN" == */* ]]; then
    echo "Домен передаётся без https:// и без пути, например: example.ru" >&2
    exit 1
fi

echo "==> Домен: $DOMAIN"
echo "==> Исходники: $SRC_DIR"

# ---------------------------------------------------------------- пакеты ----
echo "==> Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx curl

# ------------------------------------------------------------------ код -----
echo "==> Разворачиваю приложение в $APP_DIR"
if [[ ! -d "$SRC_DIR/app" || ! -d "$SRC_DIR/static" ]]; then
    echo "Не найдены app/ и static/ в $SRC_DIR — укажите путь вторым аргументом" >&2
    exit 1
fi
mkdir -p "$APP_DIR/data/events"
# rsync умеет обновлять на месте и не трогает data/ при повторном запуске
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude data --exclude venv --exclude deploy --exclude '__pycache__' \
        "$SRC_DIR/app" "$SRC_DIR/static" "$SRC_DIR/run.py" "$SRC_DIR/requirements.txt" \
        "$APP_DIR/"
else
    rm -rf "$APP_DIR/app" "$APP_DIR/static"
    cp -r "$SRC_DIR/app" "$SRC_DIR/static" "$APP_DIR/"
    cp "$SRC_DIR/run.py" "$SRC_DIR/requirements.txt" "$APP_DIR/"
fi

# ------------------------------------------------------------ окружение ------
echo "==> Создаю виртуальное окружение"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ------------------------------------------------------------- сервис ------
echo "==> Завожу пользователя $APP_USER и systemd-юнит"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

# при повторном запуске пароль сохраняем, чтобы модератора не выкинуло из входа
if [[ -f /etc/live-feed.env ]] && grep -q '^ADMIN_PASSWORD=' /etc/live-feed.env; then
    ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' /etc/live-feed.env | head -1)"
    echo "==> Пароль администратора сохранён из прошлой установки"
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

cp "$SRC_DIR/deploy/live-feed.service" /etc/systemd/system/live-feed.service

# код читает сервис, пишет только в data/
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
systemctl --no-pager --lines=8 status live-feed.service || true

# --------------------------------------------------------------- nginx ------
echo "==> Настраиваю nginx"
cp "$SRC_DIR/deploy/nginx-websocket.conf" /etc/nginx/conf.d/live-feed-websocket.conf
sed "s/example\.com/$DOMAIN/g" "$SRC_DIR/deploy/nginx.conf" \
    > /etc/nginx/sites-available/live-feed
ln -sf /etc/nginx/sites-available/live-feed /etc/nginx/sites-enabled/live-feed
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/html

# ---------------------------------------------------------------- TLS ------
echo "==> Получаю сертификат Let's Encrypt"
CERT_OK=0
if [[ -x /root/.acme.sh/acme.sh ]]; then
    curl -fsSL https://acme.sh/install.sh | sh -s -- home=/root >/dev/null 2>&1 || true
fi
if [[ ! -x /root/.acme.sh/acme.sh ]]; then
    apt-get install -y -qq ssl-cert >/dev/null 2>&1 || true
    mkdir -p /root/.acme.sh
    curl -fsSL https://acme.sh/install.sh -o /root/.acme.sh/acme.sh 2>/dev/null || true
    chmod +x /root/.acme.sh/acme.sh 2>/dev/null || true
    /root/.acme.sh/acme.sh --install-cronjob >/dev/null 2>&1 || true
fi

if [[ -x /root/.acme.sh/acme.sh ]]; then
    if /root/.acme.sh/acme.sh --issue --server letsencrypt \
        --webroot /var/www/html -d "$DOMAIN" --keylength ec-256 \
        --register-account "mailto:$ACME_EMAIL" 2>&1 | tail -5; then
        mkdir -p "/etc/letsencrypt/live/$DOMAIN"
        if /root/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
            --key-file "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
            --fullchain-file "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
            --reloadcmd "systemctl reload nginx" 2>&1 | tail -3; then
            CERT_OK=1
        fi
    fi
fi

if [[ "$CERT_OK" != "1" ]]; then
    echo "!! Сертификат получить не удалось." >&2
    echo "   Чаще всего это закрытый порт 80 или DNS, ещё не указывающий на $DOMAIN." >&2
    echo "   Приложение работает на http://$DOMAIN — им можно пользоваться уже сейчас." >&2
    echo "   Повторить позже:" >&2
    echo "     /root/.acme.sh/acme.sh --issue --server letsencrypt --webroot /var/www/html" >&2
    echo "       -d $DOMAIN --keylength ec-256 --force" >&2
    # без сертификата конфиг с https не поднимется — ставим только редирект и http
    sed -i "s|https://$DOMAIN|http://$DOMAIN|g" /etc/nginx/sites-available/live-feed
fi

if nginx -t 2>/dev/null; then
    systemctl reload nginx || systemctl restart nginx
else
    echo "!! nginx не прошёл проверку конфигурации:" >&2
    nginx -t >&2 || true
    exit 1
fi

sleep 1
echo
echo "Готово: https://$DOMAIN/admin"
echo "Пароль администратора: $ADMIN_PASSWORD"
echo "Он же сохранён в /etc/live-feed.env"
echo "События заводятся в админке: там появятся три ссылки и QR"
echo "Логи: journalctl -u live-feed -f"
