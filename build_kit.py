"""Собирает комплект развёртывания.

Версия берётся из docker-compose.yml — того же места, откуда её читает
deploy.sh на сервере. Дублировать номер в скрипте нельзя: разойдутся, и
сервер решит, что образ загружен не тот.

В архив идут только те файлы, которые кладёт на сервер deploy.sh, плюс
проверки: развёрнутое должно уметь проверить себя само.

Концы строк приводим к LF: комплект собирается на Windows, и без этого
check_release.sh на сервере падал целиком, а verify.sh принимал это за
«провалов нет».
"""

from __future__ import annotations

import re
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"

compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
m = re.search(r"image:\s*(livefeed:[\d.]+)", compose)
if not m:
    raise SystemExit("Не нашёл строку image: livefeed:<версия> в docker-compose.yml")
VER = m.group(1).split("livefeed:")[1]
print(f"версия из docker-compose.yml: {VER}")

# Ровно то, что deploy.sh копирует в /opt/livefeed, плюс проверки
DIRS = ["app", "static", "deploy"]
ROOT_FILES = [
    "run.py", "requirements.txt", "Dockerfile", "docker-compose.yml",
    ".env.example", ".dockerignore",
    "verify.sh", "check_release.sh", "deploy.sh", "cleanup.sh",
    "DEPLOY.md", "CONTEXT.md", "README.md", "ABOUT.md",
    "testlib.py", "check_admin_page.py",
    "audit_static.py", "audit_js.py",
]
ROOT_FILES += sorted(p.name for p in ROOT.glob("test_*.py"))

missing = [f for f in ROOT_FILES if not (ROOT / f).exists()]
missing += [d for d in DIRS if not (ROOT / d).is_dir()]
if missing:
    raise SystemExit("Нет файлов: " + ", ".join(missing))
print(f"файлов: {len(DIRS)} папок, {len(ROOT_FILES)} в корне")

DIST.mkdir(exist_ok=True)
out = DIST / f"livefeed-{VER}-source.tar.gz"

n = 0
with tarfile.open(out, "w:gz") as tar:
    for d in DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            tar.add(p, arcname=p.relative_to(ROOT))
            n += 1
    for f in ROOT_FILES:
        p = ROOT / f
        if not p.is_file():
            continue
        raw = p.read_bytes()
        if b"\r\n" in raw:
            p.write_bytes(raw.replace(b"\r\n", b"\n"))
        tar.add(p, arcname=f)
        n += 1

size = out.stat().st_size
print(f"готово: {out.name}  {size / 1024:.0f} КБ, {n} файлов")

# Проверяем то, ради чего комплект и собирается
import io  # noqa: E402

with tarfile.open(out, "r:gz") as tar:
    names = tar.getnames()
    crlf = []
    for m2 in tar.getmembers():
        if not m2.isfile():
            continue
        data = tar.extractfile(m2).read()
        if b"\r\n" in data:
            crlf.append(m2.name)
    print("файлов в архиве:", len(names))
    print("с CRLF:", crlf if crlf else "нет")
    for need in ("deploy.sh", "docker-compose.yml", "app/main.py",
                 "static/moderator.html", "test_session.py", "audit_static.py"):
        print(f"  {'есть' if need in names else 'НЕТ'}: {need}")

stamp = time.strftime("%Y-%m-%d %H:%M")
(DIST / "BUILD.txt").write_text(
    f"livefeed {VER}\nсобрано: {stamp}\nфайлов: {n}\n"
    f"размер: {size / 1024:.0f} КБ\n", encoding="utf-8")
print("\nзаписано:", DIST / "BUILD.txt")
