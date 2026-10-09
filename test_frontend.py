"""Проверка скриптов страниц: они должны быть syntactically живыми.

Случилось так: при переносе ссылок на префикс события потерялась
закрывающая скобка в двух шаблонах, и файлы перестали парситься целиком.
Сервер отдавал 200, проверки API проходили, а гости ничего не могли
отправить: страница молчала в консоль браузера.

Такие ошибки видны только глазами браузера, поэтому здесь мы ищем их
сами: у каждого вызова ev( должен быть закрыт вызов, а у каждой шаблонной
подстановки ${...} — закрыта скобка.

Запуск без сервера: python test_frontend.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
FAILURES: list[str] = []

# вёрстка помечена как ${ev(`...`)} — если после закрытия шаблона нет
# закрывающей скобки, файл не парсится
CALL_IN_TEMPLATE = re.compile(r"\$\{\s*ev\(`[^`]*`\)\s*\}")

# вызовы вида ev(`...`) вне подстановки
CALL_PLAIN = re.compile(r"(?<!\$\{)ev\(`[^`]*`\)")

# незакрытая подстановка: ${ без пары } до конца строки
BROKEN_CALL = re.compile(r"\$\{\s*ev\(`[^`]*`\}")


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def balance_outside_strings(text: str) -> tuple[int, int]:
    """Считает скобки () и {}, не заходя внутрь строк, шаблонов и регулярок.

    Нужен грубый, но надёжный признак битого файла: незакрытая скобка
    сдвигает баланс, и это видно без полноценного разбора JavaScript.
    """
    depth_paren = 0
    depth_brace = 0
    quote = None       # ' " `
    prev_significant = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            elif ch == "$" and quote == "`" and i + 1 < n and text[i + 1] == "{":
                # подстановка внутри шаблона: считаем её скобки
                inner, used = _scan_substitution(text, i + 1)
                depth_paren += inner[0]
                depth_brace += inner[1]
                i += used
                continue
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif ch == "/" and _regex_can_start(prev_significant):
            # регулярное выражение: внутри него тоже бывают кавычки,
            # например /[&<>"']/ — без отдельной обработки баланс сбило бы
            j = i + 1
            in_class = False
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                elif text[j] == "\n":
                    break
                j += 1
            i = j + 1
            prev_significant = "/"
            continue
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        if not ch.isspace():
            prev_significant = ch
        i += 1
    return depth_paren, depth_brace


def _regex_can_start(prev: str) -> bool:
    """После каких символов допустимо начало регулярного выражения."""
    return prev in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-", "*", "%", "<", ">", "~", "^")


def _scan_substitution(text: str, start: int) -> tuple[tuple[int, int], int]:
    """Считает скобки внутри ${...}, начиная с открывающей фигурной."""
    paren = brace = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                return (paren, brace), i - start + 1
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
        i += 1
    return (paren, brace), i - start


print("\n=== Закрытость вызовов ev() ===")
for path in sorted(STATIC.glob("*.js")):
    text = path.read_text(encoding="utf-8")
    total = len(CALL_IN_TEMPLATE.findall(text)) + len(CALL_PLAIN.findall(text))
    broken = BROKEN_CALL.findall(text)
    check(f"{path.name}: все вызовы ev() закрыты", not broken,
          f"найдено битых: {broken[:2]}" if broken else f"вызовов {total}")

print("\n=== Скобки сбалансированы ===")
for path in sorted(STATIC.glob("*.js")):
    text = path.read_text(encoding="utf-8")
    paren, brace = balance_outside_strings(text)
    check(f"{path.name}: скобки сходятся", paren == 0 and brace == 0,
          f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

print("\n=== Скрипты, которые есть на страницах ===")
html_files = sorted(STATIC.glob("*.html"))
for path in html_files:
    text = path.read_text(encoding="utf-8")
    for ref in re.findall(r'src="(/static/[A-Za-z0-9._-]+)"', text):
        target = path.parent / ref.replace("/static/", "")
        check(f"{path.name} → {ref} существует", target.exists())

print("\n=== Префикс события есть на страницах события ===")
for name in ("feed.html", "send.html", "screen.html", "moderator.html", "join.html", "login.html"):
    text = (STATIC / name).read_text(encoding="utf-8")
    check(f"{name}: подключает common.js", "/static/common.js" in text)
    check(f"{name}: скрипт в конце страницы",
          text.rstrip().endswith("</html>"))

print("\n=== На экране нет служебной подписи ===")
screen_html = (STATIC / "screen.html").read_text(encoding="utf-8")
for gone in ("screen-hud", "hudTitle", "hudCounter", "hudDot", "hud-item"):
    check(f"screen.html: нет {gone}", gone not in screen_html)
screen_js = (STATIC / "screen.js").read_text(encoding="utf-8")
for gone in ("hudTitle", "hudCounter", "hudDot", "updateHud"):
    check(f"screen.js: нет {gone}", gone not in screen_js)
screen_css = (STATIC / "screen.css").read_text(encoding="utf-8")
check("screen.css: нет .screen-hud", ".screen-hud" not in screen_css)

print("\n=== Заставка на весь экран ===")
mod_html = (STATIC / "moderator.html").read_text(encoding="utf-8")
check("moderator.html: кнопка «Заставка» есть", 'id="splashBtn"' in mod_html)
check("moderator.html: поле выбора файла есть", 'id="splashFile"' in mod_html)
check("moderator.html: блок загрузки внизу страницы",
      'id="splashUpload"' in mod_html and 'id="splashClear"' in mod_html,
      "загрузка должна быть внизу панели")
check("moderator.html: поле для картинки принимает только изображения",
      re.search(r'id="splashFile"[^>]*accept="image/', mod_html) is not None)
check("screen.html: слой заставки есть", 'id="splash"' in screen_html)
check("screen.html: картинка заставки есть", 'id="splashImg"' in screen_html)
check("screen.html: заставка не спрятана атрибутом hidden",
      re.search(r'class="splash"[^>]*id="splash"[^>]*hidden', screen_html) is None)

mod_js = (STATIC / "moderator.js").read_text(encoding="utf-8")
check("moderator.js: кнопка переключает заставку",
      "'splash'" in mod_js and "splash_visible" in mod_js)
check("moderator.js: загрузка идёт через FormData", "new FormData()" in mod_js)
check("moderator.js: загрузка сбрасывает input", "splashFile.value = ''" in mod_js)
paren, brace = balance_outside_strings(mod_js)
check("moderator.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

check("screen.js: заставка ставится по состоянию показа",
      "screenState.splash_visible" in screen_js)
check("screen.js: нет обращения к несуществующему $()",
      not re.search(r"(?<![\w$])\$\(", screen_js),
      "в common.js помощника $ нет — страница падает целиком")
check("screen.js: картинка заставки ставится из splashUrl, а не из url",
      "splashImg.src = splashUrl" in screen_js and not re.search(r"splashImg\.src = url\b", screen_js))
check("screen.js: адрес заставки берётся из конфига", "cfg.splash" in screen_js)
paren, brace = balance_outside_strings(screen_js)
check("screen.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

screen_css = (STATIC / "screen.css").read_text(encoding="utf-8")
check("screen.css: заставка перекрывает весь экран",
      re.search(r"\.splash\s*\{[^}]*position:\s*fixed", screen_css) is not None
      and re.search(r"\.splash\s*\{[^}]*inset:\s*0", screen_css) is not None)
check("screen.css: плавное появление и уход заданы переходом",
      re.search(r"\.splash\s*\{[^}]*transition:[^}]*opacity", screen_css) is not None,
      "нет перехода opacity")
on_rule = re.search(r"\.splash\.on\s*\{([^}]*)\}", screen_css)
on_body = on_rule.group(1) if on_rule else ""
on_transition = re.search(r"transition:[^;]*", on_body)
check("screen.css: при появлении видимость включается сразу",
      on_transition is not None
      and re.search(r"visibility\s+0s\s+linear\s+0s", on_transition.group(0)) is not None,
      "заставка будет запаздывать на всё время перехода"
      if on_transition else "нет перехода у .splash.on")

print("\n=== Админка ===")
for name in ("admin.html", "admin-login.html"):
    text = (STATIC / name).read_text(encoding="utf-8")
    check(f"{name}: подключает common.js", "/static/common.js" in text)
admin_js = (STATIC / "admin.js").read_text(encoding="utf-8")
check("admin.js: не использует ev() — он вне события", "ev(" not in admin_js,
      "найдено" if "ev(" in admin_js else "")
check("admin.js: копирование через execCommand, а не только буфер обмена",
      "execCommand" in admin_js,
      "нет запасного пути для http")
paren, brace = balance_outside_strings(admin_js)
check("admin.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Скрипты страниц в порядке.")