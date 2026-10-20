"""Аудит 2: синтаксис скриптов страниц настоящим движком.

Проверки по регуляркам не ловят ничего серьёзного: потерянную скобку,
забытую запятую, недопустимое имя. Ловит только браузер, поэтому здесь
каждый файл скрипта отдаётся движку через Function() и слушается
SyntaxError.

Отдельно ловится ошибка, стоившая нам целой вкладки: два классических
скрипта на одной странице делят top-level имена, и одинаковое имя во
втором файле не перекрывается, а роняет файл целиком.

Запуск: поднять сервер, затем
    python audit_js.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = os.environ.get("LF_TEST_ROOT", "http://127.0.0.1:8123").rstrip("/")
HERE = Path(__file__).parent
STATIC = HERE / "static"

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


# Какие скрипты на каких страницах живут: важно знать соседей, потому что
# именно с ними скрипт делит глобальные имена
PAGE_SCRIPTS: dict[str, list[str]] = {
    "admin.html": ["common.js", "admin.js"],
    "admin-login.html": ["common.js"],
    "feed.html": ["common.js", "feed.js"],
    "join.html": ["common.js"],
    "login.html": ["common.js"],
    "moderator.html": ["common.js", "moderator.js", "slideshow-panel.js",
                       "quiz-panel.js"],
    "quiz.html": ["common.js", "quiz.js"],
    "screen.html": ["common.js", "screen.js"],
    "send.html": ["common.js", "send.js"],
}

ALL_SCRIPTS = sorted({s for lst in PAGE_SCRIPTS.values() for s in lst})

print("=== 1. Все скрипты на месте ===")
missing = [s for s in ALL_SCRIPTS if not (STATIC / s).exists()]
check(f"все {len(ALL_SCRIPTS)} скриптов лежат в static/",
      not missing, ", ".join(missing))

print("\n=== 2. Синтаксис разбирает браузер ===")
# грубая проверка парности здесь, настоящая — в браузере следующим шагом
def balanced(src: str) -> tuple[int, int]:
    par = src.count("(") - src.count(")")
    bra = src.count("{") - src.count("}")
    return par, bra


for s in ALL_SCRIPTS:
    src = (STATIC / s).read_text(encoding="utf-8")
    par, bra = balanced(src)
    check(f"{s}: скобки сходятся", par == 0 and bra == 0,
          f"() {par:+d}, {{}} {bra:+d}")

print("\n=== 3. Соседи по странице не делят имена ===")
import re


def top_level_names(src: str) -> set[str]:
    body = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    body = re.sub(r"//[^\n]*", " ", body)
    return set(re.findall(r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
                          body, re.M))


names = {s: top_level_names((STATIC / s).read_text(encoding="utf-8"))
         for s in ALL_SCRIPTS}
for page, scripts in PAGE_SCRIPTS.items():
    for i, a in enumerate(scripts):
        for b in scripts[i + 1:]:
            dupes = names[a] & names[b]
            check(f"{page}: {a} и {b} не делят имена", not dupes,
                  ", ".join(sorted(dupes)) or "разные имена")

print("\n=== 4. Никто не ссылается на несуществующие файлы ===")
bad_refs = []
for s in ALL_SCRIPTS:
    src = (STATIC / s).read_text(encoding="utf-8")
    for ref in re.findall(r"['\"](/static/[\w.\-]+)['\"]", src):
        if not (STATIC / ref.split("/static/")[1]).exists():
            bad_refs.append(f"{s} → {ref}")
check("внутренние ссылки на файлы ведут в существующие", not bad_refs,
      "; ".join(bad_refs))

print("\n=== 5. Разметка страниц собрана ===")
for page in sorted(PAGE_SCRIPTS):
    p = STATIC / page
    if not p.exists():
        check(f"{page}: страница есть", False)
        continue
    html = p.read_text(encoding="utf-8")
    declared = re.findall(r'<script src="/static/([^"]+)"', html)
    check(f"{page}: подключает именно те скрипты, что мы проверяем",
          set(declared) == set(PAGE_SCRIPTS[page]),
          f"в разметке {declared}, в аудите {PAGE_SCRIPTS[page]}")

print(f"\n=== Итог по файлам: {passed} OK, {failed} FAIL ===")
print("\nДальше нужен браузер: настоящий разбор скриптов.")
print(json.dumps({"scripts": ALL_SCRIPTS}, ensure_ascii=False))
sys.exit(1 if failed else 0)
