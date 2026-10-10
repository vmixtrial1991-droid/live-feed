"""Проверка локального комплекта развёртывания.

Файл передан по сети, и сеть иногда перерезает архив. Проверяем на
своей машине то же, что проверяли на сервере, но без Docker: целостность
gzip, состав архива и то, что в манифесте лежат именно те образы и та же
версия, что развёрнута сейчас.

Запуск без сервера: python verify_kit.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tarfile
from pathlib import Path

DIST = Path(__file__).parent / "dist"
VER = "4.0"
IMAGES = DIST / f"livefeed-{VER}-images.tar.gz"
SOURCE = DIST / f"livefeed-{VER}-source.tar.gz"

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}  [{extra}]")
    else:
        failed += 1
        print(f"  FAIL {name}  [{extra}]")


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} МБ"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


print("=== Комплект развёртывания 4.0 ===")
check("архив образов на месте", IMAGES.exists(), str(IMAGES.name))
check("архив исходников на месте", SOURCE.exists(), str(SOURCE.name))

if not IMAGES.exists():
    sys.exit(1)

size = IMAGES.stat().st_size
check("архив не пустой", size > 10 * 1024 * 1024, human(size))

print("\n=== Передача по сети ===")
# Читаем потоково: на недоверенном архиве это ещё и проверка границ gzip
try:
    total = 0
    with gzip.open(IMAGES, "rb") as gz:
        while chunk := gz.read(1 << 20):
            total += len(chunk)
    check("архив целый, gzip читается до конца", True, human(total))
    check("размер после распаковки совпадает", total > 0, f"{human(total)}")
except (gzip.BadGzipFile, EOFError, OSError) as exc:
    check("архив целый, gzip читается до конца", False, str(exc))
    sys.exit(1)

digest = sha256(IMAGES)
print(f"  sha256: {digest}")

print("\n=== Что внутри ===")
try:
    with tarfile.open(IMAGES, "r:gz") as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read())
except Exception as exc:  # noqa: BLE001
    check("архив открывается как tar", False, str(exc))
    sys.exit(1)

check("архив открывается как tar", True, f"{len(names)} записей")
check("внутри манифест", "manifest.json" in names)
check("слои на месте",
      any(n.startswith("blobs/sha256/") for n in names),
      f"{sum(1 for n in names if n.startswith('blobs/sha256/'))} слоёв")

tags: list[str] = []
for entry in manifest:
    tags += entry.get("RepoTags") or []
print(f"  образы в манифесте: {tags}")

check("в комплекте образ приложения", f"livefeed:{VER}" in tags, str(tags))
check("в комплекте образ nginx", "nginx:1.27-alpine" in tags, str(tags))
check("версия образа совпадает с развёрнутой", f"livefeed:{VER}" in tags)
check("старой версии в комплекте нет",
      f"livefeed:3.7" not in tags, "иначе на другой машине развернётся не то")

# Конфиг образа: без него контейнер не стартует как надо
try:
    cfg_name = manifest[0]["Config"]
    cfg = json.loads(tarfile.open(IMAGES, "r:gz").extractfile(cfg_name).read())
    env = cfg.get("config", {}).get("Env", [])
    check("в образе задан пользователь livefeed",
          any(u.startswith("USER=") and "root" not in u for u in env)
          or cfg.get("config", {}).get("User") == "livefeed",
          cfg.get("config", {}).get("User", "по умолчанию"))
    check("в образе прописано хранилище данных",
          "/app/data" in (cfg.get("config", {}).get("Volumes") or {}),
          str(cfg.get("config", {}).get("Volumes")))
except Exception as exc:  # noqa: BLE001
    check("конфигурация образа читается", False, str(exc))

print("\n=== Исходники ===")
with tarfile.open(SOURCE, "r:gz") as tar:
    src_names = tar.getnames()
    crlf = [m.name for m in tar.getmembers()
            if m.isfile() and b"\r\n" in tar.extractfile(m).read()]
check("исходники без CRLF", not crlf, ", ".join(crlf[:3]) or "чисто")
for need in ("deploy.sh", "docker-compose.yml", "app/main.py",
             "static/moderator.html", "verify.sh", "test_session.py"):
    check(f"есть {need}", need in src_names)

with tarfile.open(SOURCE, "r:gz") as tar:
    compose = tar.extractfile("docker-compose.yml").read().decode("utf-8")
check("версия в compose совпадает с образом",
      f"image: livefeed:{VER}" in compose)

print("\n=== Итог ===")
print(f"  образы:   {IMAGES.name}  {human(size)}")
print(f"            sha256 {digest}")
print(f"  исходники: {SOURCE.name}  {human(SOURCE.stat().st_size)}")
print(f"  {passed} OK, {failed} FAIL")
sys.exit(1 if failed else 0)
