#!/bin/sh
# Выпуск и продление сертификата, затем запуск nginx.
#
# Скрипт не имеет права упасть: если сертификат выпустить не удалось,
# nginx всё равно должен подняться и показать сайт по http.
#
# Пока сертификата нет, сайт просто работает по http: nginx отдаёт
# приложение как обычно, а приложение не ставит флаг Secure на cookie.
# Редирект на https появляется сам, как только появится файл CERT_FILE.

DOMAIN="${DOMAIN:-}"
EMAIL="${ACME_EMAIL:-}"
ACME_DIR="/etc/acme.sh"
WEBROOT="/var/www/certbot"
# Тянем сам скрипт acme.sh, а не запускаем его install.sh: установщик
# рассчитан на обычную машину (cron, профиль, алиасы) и в контейнере
# спотыкается. Standalone-скрипт — это то, что acme.sh рекомендует для
# docker-образов, и он работает сразу после chmod.
ACME_URL="https://raw.githubusercontent.com/acmesh-official/acme.sh/master/acme.sh"
# Путь без домена: приложение смотрит только сюда, не зная имени домена
APP_CERT="/etc/letsencrypt/cert"
NGINX_CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"

log() { echo "[сертификаты] $*"; }

start_renew_loop() {
    # acme.sh продлевает сертификат сам, но только если его кто-то позовёт.
    # В контейнере cron не запущен, поэтому пускаем свой цикл: он умрёт
    # вместе с контейнером и перезапустится вместе с ним.
    (
        while :; do
            sleep 43200
            "$ACME_DIR/acme.sh" --cron --home "$ACME_DIR" >/dev/null 2>&1 || true
        done
    ) &
    log "продление сертификата: раз в 12 часов"
}

if [ -z "$DOMAIN" ]; then
    log "DOMAIN не задан — работаем только по http, сертификат не нужен."
else
    if [ ! -x "$ACME_DIR/acme.sh" ]; then
        log "ставлю acme.sh..."
        # openssl в образе nginx нет, а acme.sh без него не создаёт ключи
        apk add --no-cache curl ca-certificates openssl >/dev/null 2>&1 || true
        mkdir -p "$ACME_DIR"
        curl -fsSL -o "$ACME_DIR/acme.sh" "$ACME_URL" >/dev/null 2>&1 || true
        chmod +x "$ACME_DIR/acme.sh" 2>/dev/null || true
    fi

    if [ ! -x "$ACME_DIR/acme.sh" ]; then
        log "!! acme.sh не поставился — сайт останется на http."
    else
        "$ACME_DIR/acme.sh" --set-default-ca --server letsencrypt >/dev/null 2>&1 || true
        start_renew_loop

        # Есть ли чем раскладывать: сертификат с прошлого раза либо выпустили только что
        issued=0

        # -s, а не -f: после неудачной попытки acme.sh оставляет файл
        # нулевого размера, и проверка «файл есть» заблокировала бы
        # все следующие попытки до ручного удаления
        if [ -s "$NGINX_CERT" ]; then
            issued=1
        else
            log "пробую выпустить сертификат для $DOMAIN..."
            mkdir -p "$WEBROOT"

            # Почта нужна, чтобы вернуть доступ к сертификату. Команду
            # запускаем отдельно и ошибку игнорируем: Let's Encrypt
            # аккаунт без почты регистрирует спокойно, а вот --issue
            # параметра --register-account с почтой не принимает вовсе.
            if [ -n "$EMAIL" ]; then
                "$ACME_DIR/acme.sh" --server letsencrypt --update-account -m "$EMAIL" \
                    >/dev/null 2>&1 || true
            fi

            # Пишем в файл, а не в конвейер: у конвейера статус берётся
            # у последней команды, и tail всегда успешен — так мы бы
            # решили, что сертификат выпущен, даже когда это не так.
            if "$ACME_DIR/acme.sh" --issue --server letsencrypt -w "$WEBROOT" \
                  -d "$DOMAIN" --keylength ec-256 \
                  > "$WEBROOT/acme-issue.log" 2>&1; then
                log "выпущен, раскладываю по местам"
                issued=1
            else
                log "!! Не выдали. Обычно это закрытый порт 80 или домен ещё не смотрит на этот сервер."
                tail -4 "$WEBROOT/acme-issue.log" | sed 's/^/[сертификаты]   /'
            fi
        fi

        # Раскладывать имеет смысл только когда выпуск удался: иначе
        # --install-cert создаст пустые файлы и заблокирует будущие попытки
        if [ "$issued" = "1" ]; then
            mkdir -p "/etc/letsencrypt/live/$DOMAIN" "$APP_CERT"
            # nginx читает сертификат по имени домена из Host, и проверить файл
            # при старте не может — поэтому путь остаётся с $host. Приложению
            # имя домена неизвестно, и оно смотрит на постоянный путь.
            "$ACME_DIR/acme.sh" --install-cert -d "$DOMAIN" \
                --key-file "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
                --fullchain-file "$NGINX_CERT" \
                --reloadcmd "nginx -s reload" >/dev/null 2>&1 || true
            "$ACME_DIR/acme.sh" --install-cert -d "$DOMAIN" \
                --key-file "$APP_CERT/privkey.pem" \
                --fullchain-file "$APP_CERT/fullchain.pem" >/dev/null 2>&1 || true

            # полная цепочка публична, ключ закрыт: приложению нужен только fullchain
            chmod 644 "$APP_CERT/fullchain.pem" 2>/dev/null || true
            if [ -s "$APP_CERT/fullchain.pem" ]; then
                log "сертификат готов: $APP_CERT/fullchain.pem"
            else
                log "!! Файла $APP_CERT/fullchain.pem нет — редирект и Secure-cookie останутся выключены."
            fi
        fi
    fi
fi

log "запускаю nginx"
exec /docker-entrypoint.sh nginx -g 'daemon off;'