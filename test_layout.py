"""Проверка движка раскладки квиза (static/quiz-layout.js).

Движок считает вёрстку кадра без DOM, поэтому его можно проверить
отдельно от браузера — см. tools/check-layout.js. Здесь только обёртка:
Node нужен лишь для этой проверки, остальные тесты проекта на Python.

Если Node на машине нет, проверка честно говорит об этом и не притворяется
успешной.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SCRIPT = ROOT / "tools" / "check-layout.js"


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("Node не найден — проверка движка раскладки пропущена.")
        print("Она нужна, чтобы ловить ошибки геометрии до съёмки стенда.")
        return 1
    if not SCRIPT.exists():
        print(f"Нет файла проверки: {SCRIPT}")
        return 1
    proc = subprocess.run([node, str(SCRIPT)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())
    print("\n=== Итог ===")
    if proc.returncode == 0:
        print("Раскладка считается верно.")
    else:
        print("Движок раскладки не прошёл проверку.")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
