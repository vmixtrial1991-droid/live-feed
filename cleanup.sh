#!/bin/bash
# Уборка места на сервере. Безопасно повторять: рабочий образ, тома с
# событиями и код не трогаются.
set -uo pipefail

echo "==> До"
df -h / | tail -1 | sed 's/^/  /'

echo
echo "==> Кеш сборки Docker (главный виновник: после двух-трёх сборок
    разрастается до полутора гигабайт)"
docker builder prune -af 2>&1 | tail -1 | sed 's/^/  /'

echo
echo "==> Висячие образы — сборки, которые уже никем не используются"
docker image prune -f 2>&1 | tail -1 | sed 's/^/  /'

echo
echo "==> Кэш пакетов apt (при следующем apt-get update скачается заново)"
apt-get clean 2>/dev/null
rm -rf /var/lib/apt/lists/* 2>/dev/null
echo "  почищено"

echo
echo "==> Журналы системы: оставляем последние 20 МБ"
journalctl --vacuum-size=20M 2>&1 | grep -i freed | sed 's/^/  /' || echo "  подрезано"

echo
echo "==> Итог"
df -h / | tail -1 | sed 's/^/  /'
echo "  образы:"
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
echo "  место в Docker:"
docker system df | tail -n +2 | sed 's/^/    /'

echo
echo "==> Сайт жив?"
curl -fsS http://127.0.0.1/api/health | sed 's/^/  /'