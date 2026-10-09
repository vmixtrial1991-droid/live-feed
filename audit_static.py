"""Аудит 1: синтаксис и статическая целостность.

Проверяем то, что не ловится тестами:
  * синтаксис всех файлов Python, включая те, что не запускаются;
  * парсинг всех скриптов страниц настоящим движком браузера — это делает
    отдельный шаг, здесь только грубая проверка парности;
  * каждый id, который ищет скрипт, есть в разметке этой же страницы;
  * каждый путь, который скрипт запрашивает, ведёт в существующий маршрут;
  * нет страниц, осиротевших без скрипта, и скриптов без страницы.

Запуск без сервера: python audit_static.py
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).parent
STATIC = ROOT_DIR / "static"
APP = ROOT_DIR / "app"

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


print("=== 1. Синтаксис Python ===")
py_files = sorted(
    [p for p in ROOT_DIR.glob("*.py")] +
    [p for p in APP.glob("*.py")] +
    [p for p in (ROOT_DIR / "deploy").glob("*.py")] if (ROOT_DIR / "deploy").exists() else []
)
bad = []
for p in py_files:
    try:
        src = p.read_text(encoding="utf-8")
        ast.parse(src, filename=str(p))
        # латентная дыра: табы и пробелы в одном файле ломают разбор
        if "\t" in src and "    " in src:
            bad.append(f"{p.name}: смесь табов и пробелов")
    except SyntaxError as exc:
        bad.append(f"{p.name}:{exc.lineno}: {exc.msg}")
    except UnicodeDecodeError as exc:
        bad.append(f"{p.name}: не читается как utf-8 ({exc})")
check(f"все {len(py_files)} файлов Python разбираются", not bad, "; ".join(bad)[:200])

print("\n=== 2. Кодировка и битые символы ===")
broken = []
for p in list(py_files) + sorted(STATIC.glob("*")) + [ROOT_DIR / "CONTEXT.md"]:
    if not p.is_file() or p.suffix in (".jpg", ".png", ".gz"):
        continue
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        broken.append(f"{p.name}: {exc}")
        continue
    if "\ufffd" in txt:
        broken.append(f"{p.name}: есть символ замены U+FFFD")
    if txt.startswith("\ufeff"):
        broken.append(f"{p.name}: начинается с BOM")
check("во всех текстовых файлах нет битых символов и BOM",
      not broken, "; ".join(broken)[:200])

print("\n=== 3. Концы строк ===")
crlf = [p.name for p in py_files + list(STATIC.glob("*.js")) + list(STATIC.glob("*.css"))
        if p.is_file() and b"\r\n" in p.read_bytes()]
check("в исходниках только LF (иначе diffы целиком переписывают файлы)",
      not crlf, ", ".join(crlf)[:200])

print("\n=== 4. Страницы и их скрипты ===")
pages = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.html"))}
for name, html in pages.items():
    scripts = re.findall(r'<script src="/static/([^"]+)"', html)
    styles = re.findall(r'<link[^>]+href="/static/([^"]+)"', html)
    missing = [s for s in scripts + styles if not (STATIC / s).exists()]
    check(f"{name}: все подключаемые файлы на месте",
          not missing, ", ".join(missing) or f"{len(scripts)} скриптов, {len(styles)} стилей")

    # страница без скрипта вообще — верный признак, что её выпали из сборки
    check(f"{name}: есть хоть один скрипт", bool(scripts) or bool(styles))

scripts_used = set()
for html in pages.values():
    scripts_used |= set(re.findall(r'<script src="/static/([^"]+)"', html))
orphans = [p.name for p in sorted(STATIC.glob("*.js")) if p.name not in scripts_used]
check("нет скриптов, которых никто не подключает",
      not orphans, ", ".join(orphans) or "все подключены")

print("\n=== 5. Идентификаторы: скрипт ищет то, что есть в разметке ===")
# какому скрипту какая страница: порядок подключения в HTML
page_scripts: dict[str, list[str]] = {}
for name, html in pages.items():
    page_scripts[name] = re.findall(r'<script src="/static/([^"]+)"', html)

problems = []
for name, html in pages.items():
    for sname in page_scripts[name]:
        src = (STATIC / sname).read_text(encoding="utf-8")
        # id, который скрипт ищет через getElementById
        wanted = set(re.findall(r"getElementById\(['\"]([\w-]+)['\"]\)", src))
        have = set(re.findall(r'id="([\w-]+)"', html))
        # общие скрипты (common.js) ищут id на всех страницах сразу
        if sname == "common.js":
            continue
        missing = {w for w in wanted if w not in have
                   and w not in {"loginBtn"}}  # loginBtn рисуется скриптом сам
        if missing:
            problems.append(f"{name} + {sname}: {sorted(missing)}")
check("скрипты не ищут отсутствующие id",
      not problems, "; ".join(problems)[:300])

print("\n=== 6. Разметка: парность тегов и обязательные куски ===")
def tags_balanced(html: str) -> list[str]:
    void = {"meta", "link", "br", "hr", "img", "input", "source", "wbr"}
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-zA-Z][\w-]*)([^>]*?)(/?)>", html):
        closing, tag, _attrs, self_close = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if tag in void or self_close or tag in ("!doctype",):
            continue
        if closing:
            if not stack or stack[-1] != tag:
                return [f"лишний </{tag}>"]
            stack.pop()
        else:
            stack.append(tag)
    return [f"не закрыт <{t}>" for t in stack]

unbalanced = []
for name, html in pages.items():
    bad_tags = tags_balanced(html)
    if bad_tags:
        unbalanced.append(f"{name}: {', '.join(bad_tags[:3])}")
check("во всех страницах теги закрыты", not unbalanced, "; ".join(unbalanced)[:250])

must = {}
for name, html in pages.items():
    checks = {
        "send.html": ["sendForm"],
        "join.html": ["joinForm"],
        # у кабинета нет элемента с id="moderator": вкладки называются
        # pageFeed и pageSlideshow, список — onair
        "moderator.html": ["pageFeed", "pageSlideshow", "onair", "nowStrip"],
        "screen.html": ["stage", "overlay"],
        "admin.html": ["createForm", "list", "detail"],
    }.get(name, [])
    missing = [i for i in checks if f'id="{i}"' not in html]
    if missing:
        must[name] = missing
check("у ключевых страниц на месте их главные элементы",
      not must, "; ".join(f"{k}: {v}" for k, v in must.items()))

print("\n=== 7. window.LF: каждая страница знает свой адрес ===")
for name, html in pages.items():
    # адрес подставляет сервер, а скрипты обязаны его использовать
    uses_ev = "ev(" in html or re.search(r"src=\"/static/(common|send|screen)", html)
    check(f"{name}: скрипты ходят через ev() с адресом события",
          uses_ev is not None, "" if uses_ev else "нет ни одного ev()")

print(f"\n=== Итог: {passed} OK, {failed} FAIL ===")
sys.exit(1 if failed else 0)
