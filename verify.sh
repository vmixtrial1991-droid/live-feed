#!/bin/bash
# Прогоняет все проверки на развёрнутом сервере и убирает за собой.
#
#     cd /opt/livefeed && bash verify.sh
#
# Скрипт сам кладёт файлы проверок в контейнер, передаёт каждому набору
# нужный адрес и правильный рабочий каталог. Раньше эти три вещи забывали
# по очереди, и набор молча отчитывался нулём проверок.
set -uo pipefail

cd "$(dirname "$0")" || exit 1

IP="${VERIFY_HOST:-$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')}"
INSIDE="http://127.0.0.1:8000"     # адрес приложения внутри сети контейнеров
APP="${LF_APP_CONTAINER:-live-feed-app}"   # на стенде имя другое

PW="$(docker exec "$APP" python -c \
      'import json;print(json.load(open("/app/data/config.json"))["admin_password"])' 2>/dev/null)"
if [ -z "$PW" ]; then
    echo "!! Не нашёл пароль админа. Посмотрите вручную:"
    echo "   docker exec $APP cat /app/data/config.json"
    exit 1
fi

echo "=== Проверки на $IP ==="

echo "==> Готовлю контейнер"
docker compose exec -T app pip install -q websockets >/dev/null 2>&1

# наборы, которым нужен только адрес: кладём в /tmp
for f in deploy/check_remote.py test_splash.py test_events.py test_api.py \
         test_limits.py check_admin_page.py testlib.py \
         test_slideshow.py test_realtime.py test_no_moderation.py \
         test_migration.py test_proxy_networks.py test_cert_gate.py \
         test_session.py test_code_entry.py test_quiz.py; do
    docker cp "$f" "$APP:/tmp/$(basename "$f")" 2>/dev/null
done
# проверке скриптов страниц нужен рядом каталог static — поэтому в /app
docker cp test_frontend.py "$APP:/app/test_frontend.py" 2>/dev/null

TOTAL_OK=0
TOTAL_FAIL=0

# run НАБОР адрес скрипт [рабочий_каталог]
#
# Печатает строку таблицы и оставляет счётчик в LAST_OK.
#
# Набор, который не выполнился, обязан быть виден как провал. Раньше набор,
# падавший с ImportError, печатал «OK=0 FAIL=0» и молчал: его файлы лежат
# в /tmp, а они импортируют app из рабочего каталога, поэтому
# ModuleNotFoundError проглатывался, а в итоге сервер рапортовал «провалов
# нет» при двух наборах, которые не выполнили ни одной проверки.
# Поэтому нулевое число проверок — это провал, а не успех.
LAST_OK=0
EMPTY_SUITES=""
run() {
    local name="$1" url="$2" script="$3" workdir="${4:-}"
    local out bad
    if [ -n "$workdir" ]; then
        # python кладёт в sys.path каталог СКРИПТА, а не рабочий, поэтому
        # `from app import config` из /tmp не видит /app. Лечится PYTHONPATH.
        out="$(docker exec -w "$workdir" -e ADMIN_PASSWORD="$PW" \
               -e LF_TEST_ROOT="$url" -e LF_DATA_DIR=/app/data \
               -e PYTHONPATH=/app \
               "$APP" python "$script" "$url" 2>&1)"
    else
        out="$(docker exec -e ADMIN_PASSWORD="$PW" -e LF_TEST_ROOT="$url" \
               -e LF_DATA_DIR=/app/data "$APP" python "$script" "$url" 2>&1)"
    fi
    local rc=$?   # код именно запуска набора: следующие присваивания его сотрут
    LAST_OK="$(printf '%s' "$out" | grep -cE '^  OK')"
    bad="$(printf '%s' "$out" | grep -cE '^  FAIL')"
    # код возврата: набор мог упасть с исключением в середине — строк FAIL
    # при этом нет, и такой прогон выглядел бы успешным (в 4.1 наружный
    # сценарий именно так и «прошёл», хотя валился на первой же дельте)
    if [ "$rc" != "0" ] && [ "$bad" = "0" ]; then
        bad=1
        printf '        набор прервался с кодом %s без строк FAIL — считаем провалом\n' "$rc"
    fi
    TOTAL_OK=$((TOTAL_OK + LAST_OK))
    TOTAL_FAIL=$((TOTAL_FAIL + bad))
    printf "    %-22s OK=%-4s FAIL=%s\n" "$name" "$LAST_OK" "$bad"
    if [ "$LAST_OK" = "0" ]; then
        EMPTY_SUITES="$EMPTY_SUITES $name"
        printf '        ВНИМАНИЕ: набор не выполнил ни одной проверки\n'
    fi
    if [ "$LAST_OK" = "0" ] || [ "$bad" != "0" ]; then
        printf '%s\n' "$out" | tail -6 | sed 's/^/        /'
    fi
}

echo "==> Наборы"
# Живой сценарий идёт снаружи, через nginx. Из контейнера такой адрес виден не
# всегда: нестандартный опубликованный порт или сервер за NAT без hairpin
# соединение не пропускают. Поэтому сначала пробуем внешний адрес, а если он
# не сработал — внутренний, и честно пишем, что путь через nginx не проверен.
run "сценарий снаружи" "http://$IP" /tmp/check_remote.py
if [ "$LAST_OK" = "0" ]; then
    printf '    %s\n' "публичный адрес из контейнера не достаётся — пробуем внутренний"
    run "сценарий внутри" "$INSIDE" /tmp/check_remote.py
    if [ "$LAST_OK" != "0" ]; then
        printf '    %s\n' "!! путь через nginx в этом прогоне НЕ проверен"
        printf '    %s\n' "   проверьте вручную снаружи: docker exec -it $APP python /tmp/check_remote.py http://ВАШ_АДРЕС"
    fi
fi

run "заставка"         "$INSIDE" /tmp/test_splash.py
run "события"          "$INSIDE" /tmp/test_events.py
run "сценарии HTTP"    "$INSIDE" /tmp/test_api.py
run "админ-панель"     "$INSIDE" /tmp/check_admin_page.py
run "пределы показа"   "$INSIDE" /tmp/test_limits.py
run "слайдшоу"          "$INSIDE" /tmp/test_slideshow.py
run "показ в реальном"  "$INSIDE" /tmp/test_realtime.py
run "без модерации"     "$INSIDE" /tmp/test_no_moderation.py
run "миграции базы"     "$INSIDE" /tmp/test_migration.py
run "сети прокси"       "$INSIDE" /tmp/test_proxy_networks.py /app
run "ворота сертификата" "$INSIDE" /tmp/test_cert_gate.py /app
run "сессии"            "$INSIDE" /tmp/test_session.py
run "ввод кода"         "$INSIDE" /tmp/test_code_entry.py
run "квиз"              "$INSIDE" /tmp/test_quiz.py

printf "    %-22s " "скрипты страниц"
out="$(docker exec -w /app "$APP" python /app/test_frontend.py 2>&1)"
ok="$(printf '%s' "$out" | grep -cE '^  OK')"
bad="$(printf '%s' "$out" | grep -cE '^  FAIL')"
TOTAL_OK=$((TOTAL_OK + ok))
TOTAL_FAIL=$((TOTAL_FAIL + bad))
printf "OK=%-4s FAIL=%s\n" "$ok" "$bad"
[ "$ok" = "0" ] && printf '%s\n' "$out" | tail -6 | sed 's/^/        /'

echo "==> Статика и разметка"
# Аудит проверяет исходники: разбор Python, кодировку, концы строк, теги,
# соответствие «скрипт ищет такой-то id — такой id в разметке». На сервере он
# смотрит на развёрнутый комплект, а не на машину разработчика, и ловит
# расхождение между ними.
if [ -f audit_static.py ]; then
    a_out="$(python3 audit_static.py 2>&1)"
    a_ok="$(printf '%s' "$a_out" | grep -cE '^  OK')"
    a_bad="$(printf '%s' "$a_out" | grep -cE '^  FAIL')"
    TOTAL_OK=$((TOTAL_OK + a_ok))
    TOTAL_FAIL=$((TOTAL_FAIL + a_bad))
    printf "    %-22s OK=%-4s FAIL=%s\n" "аудит статики" "$a_ok" "$a_bad"
    [ "$a_bad" != "0" ] && printf '%s\n' "$a_out" | grep -E '^  FAIL' | sed 's/^/        /'
else
    echo "    аудит статики: файла нет на сервере"
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
fi

echo "==> Комплект развёртывания"
# подробный вывод check_release.sh прячем: он длинный и забивает таблицу
# наборов. Показываем только провалы.
rel="$(bash check_release.sh 2>&1)"
rel_code=$?
rel_bad="$(printf '%s\n' "$rel" | grep -E 'FAIL|Провалено' || true)"
if [ -n "$rel_bad" ] || [ "$rel_code" -ne 0 ]; then
    printf '%s\n' "$rel_bad" | sed 's/^/    /'
    # Скрипт упал целиком (например из-за CRLF в нём самом) — строк FAIL он
    # не напечатал, и без проверки кода выхода это выглядело бы как успех.
    [ "$rel_code" -ne 0 ] && printf '    комплект развёртывания: check_release.sh упал (код %s)\n' "$rel_code"
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
else
    n_ok="$(printf '%s\n' "$rel" | grep -cE '^  OK')"
    TOTAL_OK=$((TOTAL_OK + n_ok))
    printf "    комплект развёртывания    OK=%-4s FAIL=0\n" "$n_ok"
fi

echo
echo "==> Убираю за собой"
docker exec -u root "$APP" rm -f /tmp/check_remote.py /tmp/test_splash.py \
    /tmp/test_events.py /tmp/test_api.py /tmp/test_limits.py \
    /tmp/check_admin_page.py /tmp/testlib.py /app/test_frontend.py \
    /tmp/test_slideshow.py /tmp/test_realtime.py /tmp/test_no_moderation.py \
    /tmp/test_migration.py /tmp/test_proxy_networks.py /tmp/test_cert_gate.py \
    /tmp/test_session.py /tmp/test_code_entry.py /tmp/test_quiz.py 2>/dev/null
# из контейнера убираем, а сам аудит с /opt/livefeed не трогаем: он часть
# комплекта, check_release.sh требует его рядом, и следующий прогон
# verify.sh без него не проверит статику вовсе

echo "==> Что осталось на сервере"
docker exec "$APP" ls -1 /app/data/events/ 2>/dev/null | sed 's/^/    событие: /' \
    || echo "    событий нет"
curl -fsS "http://$IP/api/health" | sed 's/^/    /'
docker compose ps --format '  {{.Name}}  {{.Status}}'

echo
if [ -n "$EMPTY_SUITES" ]; then
    echo "Наборы, не выполнившие ни одной проверки:$EMPTY_SUITES"
    echo "Это не «всё хорошо»: набор молча упал, а счётчики показали нули."
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
fi
if [ "$TOTAL_FAIL" -eq 0 ]; then
    echo "Итог: проверок пройдено $TOTAL_OK, провалено 0."
else
    echo "Итог: пройдено $TOTAL_OK, ПРОВАЛЕНО $TOTAL_FAIL."
    exit 1
fi