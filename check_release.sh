#!/bin/bash
# Целостность комплекта развёртывания: скрипты разбираются, compose верен,
# всё нужное лежит рядом. Запуск без сервера и без сети.
#
#     bash check_release.sh
set -uo pipefail

FAILURES=0

# ok — условие истинно (0 — истина), no — ложно
report() {
    if [ "$2" = "ok" ]; then
        printf '  OK   %s\n' "$1"
    else
        printf '  FAIL %s\n' "$1"
        FAILURES=$((FAILURES + 1))
    fi
}

# есть ли рядом файл или каталог
has() { [ -e "$1" ] && echo ok || echo no; }

# разбирается ли скрипт
parses() { bash -n "$1" 2>/dev/null && echo ok || echo no; }

echo "=== Скрипты разбираются ==="
for s in deploy.sh verify.sh check_release.sh; do
    report "$s не сломан" "$(parses "$s")"
done

echo
echo "=== Рядом есть всё, что нужно установщику ==="
for f in app static deploy run.py requirements.txt Dockerfile \
         docker-compose.yml .env.example verify.sh check_release.sh; do
    report "есть $f" "$(has "$f")"
done

n=0
for t in test_*.py; do n=$((n + 1)); done
[ "$n" -ge 8 ] && report "проверки в комплекте есть ($n шт.)" ok \
               || report "проверки в комплекте есть (нашли $n, ждали 8)" no
report "есть testlib.py" "$(has testlib.py)"
report "есть check_admin_page.py" "$(has check_admin_page.py)"
report "есть DEPLOY.md с инструкцией" "$(has DEPLOY.md)"

echo
echo "=== Установщик переносит то, что обещает ==="
# установщик печатает в конце «bash verify.sh» — значит он обязан этот файл
# положить на сервер. Раньше не копировал, и там оставался от прошлой версии.
for f in verify.sh check_release.sh deploy.sh DEPLOY.md CONTEXT.md README.md; do
    grep -qw -- "$f" deploy.sh \
        && report "deploy.sh переносит $f" ok \
        || report "deploy.sh переносит $f" no
done

echo

echo
echo "=== Unix-концы строк в скриптах и конфигах ==="
# CRLF ломает bash на сервере. Комплект собирают на Windows, где файлы в
# рабочем дереве с CRLF, и если они попадут в архив как есть, скрипт
# разваливается на сервере — а verify.sh без проверки кода выхода принимал
# это за «провалов нет».
crlf_bad=0
while IFS= read -r f; do
    if grep -qU "$(printf '\r')" "$f" 2>/dev/null; then
        report "unix-концы строк: $f" no
        crlf_bad=$((crlf_bad + 1))
    fi
done < <(find . -type f \( -name '*.sh' -o -name '*.conf' -o -name 'Dockerfile' \
                 -o -name 'docker-compose.yml' -o -name '.env.example' \) \
             -not -path './.git/*' -not -path './dist/*')
if [ "$crlf_bad" -eq 0 ]; then
    report "во всех скриптах и конфигах unix-концы строк" ok
else
    report "unix-концы строк: файлов с CRLF ($crlf_bad)" no
fi

echo "=== docker-compose.yml ==="
# версию образа читаем из самого файла, а не пишем здесь: проверка с зашитой
# версией расходилась с compose и падала на каждом обновлении
APP_IMAGE="$(sed -n 's/.*image: \(livefeed:[0-9][0-9.]*\).*/\1/p' docker-compose.yml | head -1)"
if [ -n "$APP_IMAGE" ]; then
    report "образ приложения помечен версией ($APP_IMAGE)" ok
else
    report "образ приложения помечен версией" no
fi
grep -q 'image: nginx:' docker-compose.yml \
    && report "образ nginx указан" ok || report "образ nginx указан" no
grep -q '80:80' docker-compose.yml \
    && report "порт http открыт наружу" ok || report "порт http открыт наружу" no
grep -q '443:443' docker-compose.yml \
    && report "порт https открыт наружу" ok || report "порт https открыт наружу" no
grep -q 'app-data:/app/data' docker-compose.yml \
    && report "данные в отдельном томе" ok || report "данные в отдельном томе" no

# наружу должен торчать только nginx
grep -qE '^\s+- "8000:8000"' docker-compose.yml \
    && report "порт приложения не торчит наружу" no \
    || report "порт приложения не торчит наружу" ok

echo
echo "=== Установщик обещает то, что действительно делает ==="
# каждый путь, который копируется, должен существовать рядом с deploy.sh
grep -oE '"\$SRC/[A-Za-z0-9_./*-]+"' deploy.sh | tr -d '"' | sed 's|\$SRC/||' | sort -u \
| while IFS= read -r p; do
    # shellcheck disable=SC2086
    if compgen -G "$p" >/dev/null 2>&1 || [ -e "$p" ]; then
        printf '  OK   deploy.sh копирует %s\n' "$p"
    else
        printf '  FAIL deploy.sh копирует %s — рядом такого нет\n' "$p"
        exit 1
    fi
done

echo
echo "=== Архивы развёртывания ==="
# на сервере папки dist/ нет — это не поломка, а норма для установленного
# комплекта. Проверяем, только если архивы положили рядом.
REL_VER="${APP_IMAGE##livefeed:}"
ARCHIVE="${IMAGE_ARCHIVE:-dist/livefeed-$REL_VER-images.tar.gz}"
if [ -d dist ] || [ -f "$ARCHIVE" ]; then
    if [ -f "$ARCHIVE" ]; then
        report "архив образа на месте ($(du -h "$ARCHIVE" | cut -f1))" ok
        gzip -t "$ARCHIVE" 2>/dev/null \
            && report "архив образа не побит" ok \
            || report "архив образа не побит" no
    else
        report "архив образа на месте" no
    fi
    [ -f "dist/livefeed-$REL_VER-source.tar.gz" ] \
        && report "архив исходников на месте" ok \
        || report "архив исходников на месте" no
else
    printf '  --   архивы не проверяю: папки dist/ нет (на сервере так и надо)\n'
fi

echo
if [ "$FAILURES" -gt 0 ]; then
    echo "Провалено проверок: $FAILURES"
    exit 1
fi
echo "Комплект развёртывания в порядке."