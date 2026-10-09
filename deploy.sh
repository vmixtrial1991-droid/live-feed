#!/bin/bash
# Разворачивает «Живая лента» на пустом Ubuntu-сервере через Docker.
# Номер версии берётся из docker-compose.yml рядом со скриптом, подставьте
# его в имя архивов при сборке комплекта.
#
# Обычный запуск (нужен интернет, чтобы поставить Docker и собрать образ):
#     tar -xzf livefeed-<версия>-source.tar.gz -C /tmp/lf
#     ADMIN_PASSWORD=... bash /tmp/lf/deploy.sh
#
# Запуск без интернета (образ уже лежит рядом):
#     положите рядом livefeed-<версия>-images.tar.gz
#     tar -xzf livefeed-<версия>-source.tar.gz -C /tmp/lf
#     cp livefeed-<версия>-images.tar.gz /tmp/lf/
#     ADMIN_PASSWORD=... bash /tmp/lf/deploy.sh
#
# Скрипт ставит Docker, открывает порты, готовит .env и поднимает контейнеры.
# Он безопасен для повторного запуска: .env с паролем не перезаписывается.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/livefeed
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
# Версия образа живёт ровно в одном месте — в строке image: docker-compose.yml.
# Дублировать её здесь нельзя: скрипт сверяет по ней, загружен ли образ, и по
# устаревшему номеру решал «образ уже есть», пропускал docker load, а compose
# уходил в сборку — которой на сервере без интернета нет.
REL_VER="$(sed -n 's/.*image: \(livefeed:[0-9][0-9.]*\).*/\1/p' "$SRC/docker-compose.yml" | head -1)"
REL_VER="${REL_VER#livefeed:}"
[ -n "$REL_VER" ] || { printf 'Не нашёл версию образа в docker-compose.yml рядом со скриптом\n' >&2; exit 1; }
IMAGE_NAME="livefeed:$REL_VER"
IMAGE_ARCHIVE="${IMAGE_ARCHIVE:-$SRC/livefeed-$REL_VER-images.tar.gz}"

say() { printf '\n==> %s\n' "$1"; }
step() { printf '    %s\n' "$1"; }

say "1/8 Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates

say "2/8 Проверяю Docker"
if ! command -v docker >/dev/null 2>&1; then
    if ! docker compose version >/dev/null 2>&1; then
        step "ставлю Docker из официального скрипта"
        curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
        sh /tmp/get-docker.sh >/tmp/docker-install.log 2>&1
        rm -f /tmp/get-docker.sh
    fi
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker --version | sed 's/^/    /'
docker compose version | sed 's/^/    /'

say "3/8 Открываю порты 80 и 443"
if command -v ufw >/dev/null 2>&1; then
    ufw allow 80/tcp  >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
    ufw status | sed 's/^/    /'
else
    step "ufw не установлен — откройте порты в панели хостера"
fi
step "если сайт всё равно не открывается — проверьте Security Group в панели хостера"

say "4/8 Кладу образ"
if docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    step "образ $IMAGE_NAME уже есть — собирать не буду"
elif [ -f "$IMAGE_ARCHIVE" ]; then
    step "загружаю образы из $(basename "$IMAGE_ARCHIVE") (интернет не нужен)"
    docker load -i "$IMAGE_ARCHIVE" | sed 's/^/    /'
else
    step "готового образа нет — соберу из кода (нужен интернет)"
fi
docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 \
    && step "образ на месте: $IMAGE_NAME" \
    || step "образа пока нет — compose соберёт его сам"

say "5/8 Разворачиваю код в $APP_DIR"
mkdir -p "$APP_DIR"
for d in app static deploy; do
    rm -rf "$APP_DIR/$d"
    cp -r "$SRC/$d" "$APP_DIR/$d"
done
for f in run.py requirements.txt Dockerfile docker-compose.yml .env.example .dockerignore; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$APP_DIR/"
done
# проверяющие скрипты и сам установщик тоже ставятся: и verify.sh, который
# установщик потом печатает, и он сам — иначе на сервере лежат копии от
# прошлой версии и следующий запуск поднимает старый код
for f in verify.sh check_release.sh deploy.sh; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$APP_DIR/"
done
# инструкция и заметки — на сервере тоже нужны: разворачивающий должен иметь
# их под рукой, не возвращаясь к машине разработчика
for f in DEPLOY.md CONTEXT.md README.md; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$APP_DIR/"
done

# проверки кладём на сервер: по ним потом можно убедиться, что развёрнутое
# живое, не заходя на машину разработчика
for t in "$SRC"/test_*.py "$SRC/testlib.py" "$SRC/check_admin_page.py"; do
    [ -f "$t" ] && cp "$t" "$APP_DIR/"
done
# аудит статики туда же: он смотрит на развёрнутый комплект и ловит
# расхождение между сервером и машиной разработчика
for t in "$SRC/audit_static.py" "$SRC/audit_js.py"; do
    [ -f "$t" ] && cp "$t" "$APP_DIR/"
done

# концы строк: CRLF ломает bash и nginx. Скрипты верхнего уровня тоже —
# комплект собирают на Windows, и без этого check_release.sh на сервере
# падал целиком, а verify.sh принимал это за «провалов нет»
while IFS= read -r f; do
    [ -f "$APP_DIR/$f" ] && sed -i 's/\r$//' "$APP_DIR/$f"
done < <(
    cd "$APP_DIR"
    find deploy -type f \( -name '*.sh' -o -name '*.conf' \) -print
    find . -maxdepth 1 -type f -name '*.sh' -print
    printf '%s\n' Dockerfile docker-compose.yml .env.example
)

say "6/8 Готовлю .env"
if [ -f "$APP_DIR/.env" ]; then
    # .env держит пароль админа и настройки — при повторном запуске не трогаем
    step ".env уже есть, оставляю как есть"
    ADMIN_PASSWORD="$(grep -m1 '^ADMIN_PASSWORD=' "$APP_DIR/.env" | cut -d= -f2-)"
    if [ -n "$ADMIN_PASSWORD" ]; then
        step "текущий пароль админа: $ADMIN_PASSWORD"
    fi
else
    if [ -z "$ADMIN_PASSWORD" ]; then
        ADMIN_PASSWORD="$(python3 -c 'import secrets,string; a=string.ascii_uppercase+string.digits; print("".join(secrets.choice(a) for _ in range(12)))')"
        step "пароль сгенерирован"
    else
        step "пароль задан"
    fi
    sed -e "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$ADMIN_PASSWORD/" \
        "$APP_DIR/.env.example" > "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

say "7/8 Поднимаю"
cd "$APP_DIR"
docker compose config >/dev/null && step "compose валиден"
# --build не делаем: если образ уже загружен, он и должен использоваться
docker compose up -d 2>&1 | tail -3

say "8/8 Жду готовности и проверяю"
READY=0
for i in $(seq 1 90); do
    if curl -fsS -o /dev/null http://127.0.0.1/api/health 2>/dev/null; then
        step "отвечает через ${i} с"
        READY=1
        break
    fi
    sleep 1
done

docker compose ps | sed 's/^/    /'
if [ "$READY" = "1" ]; then
    step "health: $(curl -fsS http://127.0.0.1/api/health)"
else
    step "!! Сервер не отвечает. Смотрите: docker compose logs -f nginx"
fi

IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

cat <<EOF

────────────────────────────────────────────────────────
  Живая лента $REL_VER развёрнута

  Админка:   http://$IP/admin
  Пароль:    $ADMIN_PASSWORD

  Гостям и экрану раздавайте ссылки из карточки события
  в админке: там код приглашения и QR готовы.

  Дальше:
    cd $APP_DIR
    docker compose logs -f nginx      # журналы
    docker compose restart            # перезапуск
    docker compose down               # остановить
    docker compose up -d              # снова поднять
    bash verify.sh                    # прогнать все проверки

  Обновить на новую версию:
    новый архив → та же команда bash deploy.sh
    .env и данные (события, фото) при этом сохраняются
────────────────────────────────────────────────────────
EOF