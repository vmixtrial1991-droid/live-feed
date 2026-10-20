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


def strip_js_comments(text: str) -> str:
    """Убирает /* ... */ и // ..., чтобы поиск по коду не цеплялся за текст.

    Без этого проверка «prompt() вычищены» ловила само слово в комментарии
    «Раньше стояли два системных prompt() подряд» и падала на коде, который
    как раз и почищен.
    """
    out = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    out = re.sub(r"//[^\n]*", " ", out)
    # строковые литералы тоже убираем: 'показать сейчас' не должно ловиться
    out = re.sub(r"'([^'\\\n]|\\.)*'", "''", out)
    out = re.sub(r'"([^"\\\n]|\\.)*"', '""', out)
    return out


def strip_js_comments_only(text: str) -> str:
    """Только комментарии. Строки остаются на месте.

    Нужен там, где проверяется именно литерал в коде: префикс 'g' перед id
    галереи нельзя найти, если чистка съела все кавычки.
    """
    out = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", out)


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

print("\n=== Экран и витрина не слепые (4.1.1) ===")
check("screen.js: ключ экрана находится и в ?d= адреса, не только в window.LF",
      "URLSearchParams" in screen_js and ".get('d')" in screen_js)
common_js = (STATIC / "common.js").read_text(encoding="utf-8")
check("common.js: префикс события берётся и из адреса страницы",
      "location.pathname.match" in common_js and "[^/]" in common_js,
      "если CSP съест инлайн window.LF, запросы всё равно пойдут в событие")
feed_js = (STATIC / "feed.js").read_text(encoding="utf-8")
check("feed.js: эфир витрины открывается с ключом экрана",
      "withKey(ev('/ws/display'))" in feed_js and "screenKey" in feed_js)
check("feed.js: ключ витрине даёт сервер",
      "screenKey" in feed_js and "(window.LF && window.LF.screenKey)" in feed_js)
paren, brace = balance_outside_strings(feed_js)
check("feed.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")
paren, brace = balance_outside_strings(common_js)
check("common.js: скобки сходятся", paren == 0 and brace == 0,
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
check("common.js: копирование через execCommand, а не только буфер обмена",
      "execCommand" in (STATIC / "common.js").read_text(encoding="utf-8"),
      "нет запасного пути для http — копирование живёт в common.js")
check("moderator.html: кнопка «Ссылка экрана» есть",
      'id="copyScreenLink"' in mod_html,
      "ведущему нужно отнести ссылку на проектор, а не только открыть её")
check("moderator.js: кнопка копирует ссылку с ключом экрана",
      "copyScreenLink" in mod_js and "window.LF.screenKey" in mod_js
      and "await copy(" in mod_js,
      "ссылка без ключа на экране бесполезна")
paren, brace = balance_outside_strings(admin_js)
check("admin.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

print("\n=== Слайдшоу и архив в кабинете (4.0) ===")
# Вкладки: обе должны быть в разметке, и каждая со своим контейнером,
# иначе переключатель прячет или показывает не то
check("moderator.html: вкладка «Лента»", 'id="tabFeed"' in mod_html)
check("moderator.html: вкладка «Слайдшоу»", 'id="tabSlideshow"' in mod_html)
check("moderator.html: счётчик снимков на вкладке", 'id="tabSlideshowCount"' in mod_html)
check("moderator.html: страница вкладки «Лента»", 'id="pageFeed"' in mod_html)
check("moderator.html: страница вкладки «Слайдшоу»", 'id="pageSlideshow"' in mod_html)
check("moderator.html: лента видна сразу, слайдшоу спрятано",
      re.search(r'class="tab-page is-active" id="pageFeed"', mod_html) is not None
      and re.search(r'class="tab-page" id="pageSlideshow"', mod_html) is not None)
check("moderator.html: подключён скрипт слайдшоу",
      "/static/slideshow-panel.js" in mod_html,
      "без него вкладка будет пустой: карточек нет, кнопки не работают")

# Переключать, что показывает экран, нужно из обеих вкладок: ведущий чаще
# сидит в «Ленте» и не должен ходить за кнопкой на другую вкладку.
slide_js = (STATIC / "slideshow-panel.js").read_text(encoding="utf-8")
feed_bar = re.search(r'<section class="control-bar">(.*?)</section>', mod_html, re.S)
feed_bar_text = feed_bar.group(1) if feed_bar else ""
check("moderator.html: кнопка слайдшоу есть и в ленте",
      'id="slideshowRunBtnFeed"' in feed_bar_text,
      "переключать экран приходилось только после перехода на другую вкладку")
check("moderator.html: в ленте она рядом с «Заставка», «QR» и «Заново»",
      all(x in feed_bar_text for x in ('id="splashBtn"', 'id="qrBtn"',
                                       'id="resetBtn"',
                                       'id="slideshowRunBtnFeed"')))
check("moderator.html: обе кнопки запуска помечены одним действием",
      mod_html.count('data-act="slideshow-run"') == 2,
      "иначе одна из кнопок останется мёртвой")
check("moderator.html: у слайдшоу своя пауза",
      'id="slideshowPlayBtn"' in mod_html
      and 'id="slideshowPlayGlyph"' in mod_html
      and 'id="slideshowPlayLabel"' in mod_html,
      "общая пауза ленты останавливала бы и галерею, а ведущий этого не ждёт")
check("slideshow-panel.js: кнопки запуска берутся списком",
      "querySelectorAll('[data-act=\"slideshow-run\"]')" in slide_js,
      "один getElementById находит только первую из двух")
check("slideshow-panel.js: подсветка ставится обеим кнопкам",
      re.search(r"runBtns\.forEach\([^)]*\)\s*=>\s*\{[^}]*classList\.toggle\('active'",
                slide_js) is not None,
      "иначе в «Ленте» не видно, что на экране галерея")
check("slideshow-panel.js: пауза галереи шлёт своё действие",
      "'slideshow-play'" in slide_js)
check("slideshow-panel.js: пауза видна только при запущенном слайдшоу",
      re.search(r"ssPlayBtn\.hidden\s*=\s*!on", slide_js) is not None,
      "кнопка паузы при выключенном слайдшоу ничего не значит")

print("\n=== Полоса «Сейчас на экране» (4.0.1) ===")
screen_str_mod = strip_js_comments_only((STATIC / "moderator.js").read_text(encoding="utf-8"))
mod_css4 = (STATIC / "moderator.css").read_text(encoding="utf-8")
# Полоса обязана показывать ровно то, что видно на экране. Заставка и QR —
# тоже: ведущий нажимал «Заставка», а полоса продолжала показывать слайд,
# которого на экране давно нет.
check("moderator.html: у полосы есть подпись", 'id="nowLabel"' in mod_html)
check("moderator.html: у полосы есть режим", 'id="nowStrip"' in mod_html
      and 'data-mode="none"' in mod_html)
check("moderator.js: полоса знает про заставку",
      re.search(r"if \(scr\.splash_visible\)", screen_str_mod) is not None)
check("moderator.js: полоса знает про QR",
      re.search(r"if \(scr\.qr_visible\)", screen_str_mod) is not None)
# порядок как слои на экране: заставка (z-index 20) поверх QR (z-index 8)
i_splash = screen_str_mod.find("if (scr.splash_visible)")
i_qr = screen_str_mod.find("if (scr.qr_visible)")
i_gal = screen_str_mod.find("const inShow = !!scr.slideshow_visible")
check("moderator.js: заставка проверяется раньше QR",
      i_splash != -1 and i_qr != -1 and i_splash < i_qr,
      "на экране заставка лежит поверх QR")
check("moderator.js: и обе раньше галереи и ленты",
      i_qr != -1 and i_gal != -1 and i_qr < i_gal)
check("moderator.js: QR в полосе — тот же, что на экране",
      "api/qr.svg" in screen_str_mod and "state.inviteCode" in screen_str_mod)
check("moderator.js: заставка в полосе — та, что на сервере",
      "state.splash" in screen_str_mod)
# splash_url() уже отдаёт адрес с префиксом события. Обернём его в ev() —
# выйдет /e/slug/e/slug/splash.jpg, и картинка просто не загрузится.
# QR, наоборот, оборачивать надо: он приходит без префикса.
check("moderator.js: адрес заставки не оборачивается в ev()",
      re.search(r"setThumb\(\s*ev\(\s*(?:url|state\.splash)\b", screen_str_mod) is None,
      "splash_url() уже с префиксом события, ev() даст двойной путь")
check("moderator.js: а QR оборачивается — он приходит без префикса",
      re.search(r"setThumb\(\s*ev\(\s*`/api/qr\.svg", screen_str_mod) is not None)
check("moderator.css: у полосы есть вид под каждый режим",
      all(f'data-mode="{m}"' in mod_css4
          for m in ("splash", "qr", "gallery")))
check("moderator.css: заставка и QR не обрезаются",
      re.search(r'\.now-thumb\[data-fit="contain"\]', mod_css4) is not None,
      "обрезанный QR или заставка не узнаются")

print("\n=== Кнопка «Витрина» убрана из кабинета (4.0.1) ===")
check("moderator.html: кнопки «Витрина» в шапке нет",
      'id="toFeed"' not in mod_html,
      "ссылка на публичную страницу в кабинете ведущего ничего не делала")
check("moderator.html: кнопка «Экран» осталась",
      'id="toScreen"' in mod_html,
      "ведущему нужно открыть экран в соседней вкладке")
check("common.js: ссылки на витрину из интерфейса убраны",
      "toFeed" not in (STATIC / "common.js").read_text(encoding="utf-8"),
      "ссылка вела с отправки и из кабинета в сторону от дела")

# 4.0.1: со страницы отправки убрана кнопка «В начало»
send_html = (STATIC / "send.html").read_text(encoding="utf-8")
check("send.html: кнопки «В начало» нет",
      "В начало" not in send_html and "toSendFeed" not in send_html,
      "зритель на странице отправки должен отправлять, а не уходить смотреть")
check("send.html: шапка осталась", 'id="whoami"' in send_html)
check("common.js: на странице отправки больше нечего оборачивать в ev()",
      re.search(r"const links = \{[\s\S]*?\};",
                strip_js_comments_only(
                    (STATIC / "common.js").read_text(encoding="utf-8"))) is not None
      and "toSendFeed" not in (STATIC / "common.js").read_text(encoding="utf-8"))
check("common.js: рабочие ссылки на месте",
      all(x in (STATIC / "common.js").read_text(encoding="utf-8")
          for x in ("toSend: '/send'", "toSendBig: '/send'", "toScreen: '/screen'")),
      "убирая витрину, не снеси страницу отправки и экран")
check("common.js: кнопка «Экран» несёт ключ экрана (4.1)",
      "d=" in (STATIC / "common.js").read_text(encoding="utf-8")
      and "window.LF.screenKey" in (STATIC / "common.js").read_text(encoding="utf-8"),
      "экран без ?d= отвечает 403, ведущий не откроет свою ссылку")

print("\n=== Ввод кода руками (4.0.1) ===")
join_html = (STATIC / "join.html").read_text(encoding="utf-8")
base_css = (STATIC / "base.css").read_text(encoding="utf-8")
check("join.html: есть поле кода", 'id="codeInput"' in join_html)
check("join.html: поле называно по-человечески", "Код приглашения" in join_html)
check("join.html: у поля есть подсказка про регистр", "Регистр не важен" in join_html)
check("join.html: код нормализуется до отправки",
      re.search(r"function cleanCode\(", join_html) is not None
      and "toUpperCase()" in join_html)
# в поле запрещены символы, которых нет в алфавите кода
check("join.html: посторонние символы вычищаются",
      re.search(r"replace\(/\[\^A-Z2-9\]/g", join_html) is not None,
      "пробел от мобильной клавиатуры иначе тихо ломает код")
check("join.html: написанный код важнее адресного",
      "const useCode = typed || invite;" in join_html,
      "человек мог прийти по старой ссылке и вписать новый код")
check("join.html: без кода страница не отправляет пустую заявку",
      "Введите код приглашения." in join_html)
# Раньше кнопка гасилась до проверки кода и больше не включалась: человек
# вводил код, жал «Продолжить» и уходил, потому что кнопка серая навсегда
check("join.html: кнопка разблокируется после отказа",
      re.search(r"if \(!useCode\) \{[\s\S]{0,300}?btn\.disabled = false;", join_html)
      is not None,
      "иначе после первой неудачи вторую попытку сделать нельзя")
check("join.html: при годном коде поле скрыто",
      re.search(r"if \(ok\)\s*\{\s*codeField\.hidden = true", join_html) is not None,
      "код уже в адресе, вводить его второй раз незачем")
check("join.html: при негодном коде поле снова доступно",
      join_html.count("codeField.hidden = false") >= 2)
check("join.html: негодный код показывают, а не прячут форму",
      form_hidden_none := ("form.hidden = true" not in join_html),
      "иначе посетитель упирается в пустую карточку без выхода")
check("base.css: код набирается крупно и моноширинно",
      re.search(r"\.code-input\s*\{[^}]*monospace", base_css, re.S) is not None
      and "letter-spacing" in base_css)
check("base.css: код выровнен по центру", "text-align: center" in base_css)

print("\n=== Витрина закрыта, вход объясняет (4.0.1) ===")
join_html = (STATIC / "join.html").read_text(encoding="utf-8")
check("join.html: решение принимает сервер, а не догадка на клиенте",
      re.search(r"'inviteOk' in lf", join_html) is not None,
      "inviteReason при годном коде приходит пустой строкой, а она ложна: "
      "проверка по нему объявляла годный код негодным")
# Подпись события не должна блокироваться у того, кто пришёл с годным
# кодом: ему полагается видеть подпись, как и всем остальным. А тому, кому
# есть что объяснить, блокировка нужна — иначе конфиг её затрёт.
check("join.html: подпись блокируется только когда есть что объяснить",
      join_html.count("sub.dataset.locked = '1'") == 1
      and re.search(r"if \(ok\)\s*\{[\s\S]{0,200}?return false;[\s\S]{0,200}?"
                    r"sub\.dataset\.locked = '1'", join_html) is not None,
      "иначе гость с годным кодом не увидит подпись события")
check("join.html: объяснение не затирается настройками события",
      re.search(r"if \(!sub\.dataset\.locked\) sub\.textContent", join_html)
      is not None,
      "ответ с настройками приходил позже и затирал объяснение пустым значением")
check("join.html: при негодном коде говорит прямо",
      "не работает" in join_html)
check("join.html: при отсутствии кода зовёт открыть по QR",
      "QR-код" in join_html)
check("join.html: форма не прячется ни при каком коде",
      "form.hidden" not in join_html,
      "прятать форму больше нельзя: в ней и код, и имя, а иначе посетитель "
      "упирается в пустую карточку без выхода")
check("на странице входа нет загрузки ленты",
      "feed.js" not in join_html,
      "витрина закрыта, лента на странице входа не нужна")

print("\n=== Заставка (4.0.1) ===")
mod_js3 = (STATIC / "moderator.js").read_text(encoding="utf-8")
# Панель ведущего обязана показывать ровно ту заставку, что сейчас на экране.
# Раньше renderSplash звался только после загрузки или удаления в этой же
# вкладке: вышел ведущий из сессии, вошёл — «Заставка не загружена», хотя
# на экране висела картинка.
check("moderator.js: заставка берётся из состояния показа",
      re.search(r"if \('splash' in payload\)[\s\S]{0,160}?renderSplash\(",
                mod_js3) is not None,
      "после входа панель показывала бы «не загружена» при загруженной заставке")
check("moderator.js: повторный опрос не мигает картинкой",
      "dataset.url" in mod_js3,
      "состояние опрашивается каждые 15 секунд, картинка не должна мигать")
check("screen.js: заставка приезжает по сокету",
      "setSplashUrl(msg.splash)" in screen_js,
      "загрузили или убрали заставку — открытый экран обязан увидеть сразу")
check("screen.js: ушедшая заставка убирается, а не висит",
      re.search(r"function setSplashUrl\([^)]*\)[\s\S]{0,320}?applySplashImage\(\)",
                screen_js) is not None)
check("moderator.html: подпись о пустой заставке на месте",
      'id="splashNone"' in mod_html)

# moderator.js и slideshow-panel.js — классические скрипты, и top-level const
# у них попадает в один общий лексический объём. Одинаковое имя во втором
# файле не «перекрывается», а роняет весь файл: страница молча теряет
# половину разметки, а API-проверки этого не видят.
def top_level_names(src: str) -> set[str]:
    body = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    body = re.sub(r"//[^\n]*", " ", body)
    return set(re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)", body, re.M))


dupes = top_level_names((STATIC / "moderator.js").read_text(encoding="utf-8")) & \
        top_level_names((STATIC / "slideshow-panel.js").read_text(encoding="utf-8"))
check("moderator.js и slideshow-panel.js не делят имена",
      not dupes, "одинаковое имя во втором файле роняет его целиком: " + str(sorted(dupes)))

# Галерея и управление показом
check("moderator.html: контейнер галереи", 'id="gallery"' in mod_html)
check("moderator.html: пустое состояние галереи", 'id="galleryEmpty"' in mod_html)
check("moderator.html: кнопка запуска слайдшоу", 'id="slideshowRunBtn"' in mod_html)
check("moderator.html: кнопка очистки галереи", 'id="slideshowClearBtn"' in mod_html)
check("moderator.html: строка состояния показа", 'id="slideshowState"' in mod_html)

# Архив фотографий
check("moderator.html: счётчик архива", 'id="archiveCount"' in mod_html)
check("moderator.html: кнопка скачивания", 'id="archiveDownload"' in mod_html)
check("moderator.html: кнопка удаления всех фото", 'id="archiveWipe"' in mod_html)
check("moderator.html: кнопка удаления помечена как опасная",
      re.search(r'id="archiveWipe"[^>]*class="[^"]*bad', mod_html) is not None
      or 'class="btn bad" id="archiveWipe"' in mod_html,
      "стирание фотографий необратимо и не должно выглядеть как обычное действие")
check("moderator.html: скачивание отдаём файлом",
      re.search(r'id="archiveDownload"[^>]*download', mod_html) is not None)
check("moderator.html: сказано, что удаление из ленты не стирает файл",
      "не зависят от ленты" in mod_html,
      "ведущий должен понимать, зачем нужны две кнопки рядом")

slide_js = (STATIC / "slideshow-panel.js").read_text(encoding="utf-8")
check("slideshow-panel.js: галочка шлёт запрос с галочкой",
      "/api/moderator/slideshow" in slide_js and "message_id: id" in slide_js)
check("slideshow-panel.js: запуск идёт через действие показа",
      "'slideshow'" in slide_js and "control('slideshow'" in slide_js)
check("slideshow-panel.js: скачивание архива", "/api/moderator/photos.zip" in slide_js)
check("slideshow-panel.js: удаление всех фото",
      "/api/moderator/photos-delete-all" in slide_js)
check("slideshow-panel.js: перед удалением спросим",
      re.search(r"confirm\(", slide_js) is not None,
      "стирание необратимо — без подтверждения нельзя")
check("slideshow-panel.js: в подтверждении сказано про отмену",
      "Отменить это будет нельзя" in slide_js)
check("slideshow-panel.js: галерея — отдельный набор, а не флаг у строки",
      "state.gallery" in slide_js and "m.in_slideshow" not in slide_js,
      "иначе удаление из ленты снова уносит снимок из слайдшоу")
check("moderator.js: флага in_slideshow больше нет",
      "in_slideshow" not in (STATIC / "moderator.js").read_text(encoding="utf-8"),
      "флаг жил в строке ленты и умирал вместе с ней")
check("moderator.js: галочка спрашивает набор галереи",
      "state.galleryImages" in (STATIC / "moderator.js").read_text(encoding="utf-8"))
check("screen.js: в слайдшоу рисуется галерея, а не лента",
      "gallery.get(sid)" in screen_js)
check("screen.js: галерея приходит по сокету",
      "msg.gallery" in screen_js)

# id галереи и id ленты совпадают: обе таблицы нумеруются с единицы. Если
# искать снимок в общей карте, слайд галереи «1» рисуется сообщением ленты
# «1» — на экране появляется чужое фото вместо отмеченного.
screen_code = strip_js_comments(screen_js)
screen_str = strip_js_comments_only(screen_js)
check("screen.js: слайды ленты и галереи не ищутся в общей карте",
      "messages.get(id) || gallery.get(id)" not in screen_code
      and "messages.get(id) ||" not in screen_code,
      "id у ленты и галереи начинаются с единицы и пересекаются")
check("screen.js: галерея рисуется по данным, а не по id",
      re.search(r"function paintData\(\s*\w+\s*\)", screen_code) is not None
      and "paintData(item)" in screen_code)
check("screen.js: что показано, помечено источником",
      "shownKey" in screen_str and "'g' +" in screen_str and "'m' +" in screen_str,
      "иначе слайд галереи «1» считался бы уже показанным сообщением «1»")
check("screen.js: показ сверяется и по содержимому, не только по id",
      re.search(r"function signature\(", screen_str) is not None
      and "repaintSame" in screen_str,
      "иначе правка текста под фотографией не появилась бы на экране, "
      "пока ведущий не дождался бы следующего слайда")

print("\n=== Окно правки текста (4.0) ===")
mod_js2 = (STATIC / "moderator.js").read_text(encoding="utf-8")
check("moderator.html: окно правки есть", 'id="editModal"' in mod_html)
check("moderator.html: поле для текста", 'id="editInput"' in mod_html)
check("moderator.html: счётчик символов", 'id="editCount"' in mod_html)
check("moderator.html: кнопка сохранить", 'id="editSave"' in mod_html)
check("moderator.html: окно спрятано до первого нажатия",
      re.search(r'id="editModal"[^>]*hidden', mod_html) is not None)
check("moderator.css: есть стили окна",
      re.search(r"\.modal-card\s*\{", (STATIC / "moderator.css").read_text(encoding="utf-8")) is not None)
check("системные prompt() вычищены",
      "prompt(" not in strip_js_comments(mod_js2)
      and "prompt(" not in strip_js_comments(slide_js),
      "два окна подряд выглядели чужеродно, и поле автора было лишним")
check("поля для имени автора в окне нет",
      'id="editAuthor"' not in mod_html,
      "имя автора менять не нужно")
check("окно открывается функцией openEdit", "function openEdit(" in mod_js2)
check("в окне есть предел из конфига сервера",
      "editInput.maxLength = String(MAX_TEXT)" in mod_js2)
check("лишние символы не молча режутся, а подсвечиваются",
      "is-full" in mod_js2)
check("Enter сохраняет, Escape закрывает",
      "e.key === 'Escape'" in mod_js2 and "e.key === 'Enter'" in mod_js2)
check("правка галереи идёт тем же окном",
      "openEdit('gallery'" in slide_js)
check("правка текста шлёт только текст",
      re.search(r"postForm\(ev\(path\), fields\)", mod_js2) is not None)
mod_css = (STATIC / "moderator.css").read_text(encoding="utf-8")
check("поле подсвечивается у предела",
      re.search(r"#editInput\.is-full", mod_css) is not None)
check("slideshow-panel.js: галочка не попадает в общий обработчик",
      "if (btn.tagName === 'INPUT') return;" in (STATIC / "moderator.js").read_text(encoding="utf-8"),
      "иначе щёлчок по квадратику выполнит ещё и чужое действие")
paren, brace = balance_outside_strings(slide_js)
check("slideshow-panel.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

# Экран: слайдшоу занимает кадр целиком
check("screen.js: слайдшоу отрабатывает в showCurrent",
      "screenState.slideshow_visible" in screen_js)
check("screen.js: в слайдшоу показывается снимок из галереи",
      "screenState.slideshow_current_id" in screen_js)
check("screen.js: пустая галерея объясняется, а не показывается пустым кадром",
      re.search(r"Слайдшоу пустое", screen_js) is not None)
check("screen.js: обычная лента не поехала после слайдшоу",
      screen_js.count("showCurrent") >= 3,
      "showCurrent зовётся при init, refresh и screen")

# ---------------- Квиз (5.0) ----------------
quiz_html = (STATIC / "quiz.html").read_text(encoding="utf-8")
quiz_js = (STATIC / "quiz.js").read_text(encoding="utf-8")
quiz_panel_js = (STATIC / "quiz-panel.js").read_text(encoding="utf-8")
check("quiz.html: игра спрятана до входа", 'id="gameBox" hidden' in quiz_html)
check("quiz.html: вход по тому же коду приглашения", 'id="codeInput"' in quiz_html)
check("quiz.html: имя можно поменять", 'id="renameBtn"' in quiz_html)
check("quiz.js: состояние тянется из /api/quiz, канал — /ws/quiz",
      "ev('/api/quiz')" in quiz_js and "ev('/ws/quiz')" in quiz_js)
check("quiz.js: код из QR-ссылки (?t=) подставляется", "params.get('t')" in quiz_js)
check("quiz.js: ответ можно поменять до закрытия", "можно поменять" in quiz_js)
check("quiz.js: чужих ответов не показывает — только свои и распределение",
      "data.my_answer" in quiz_js and "counts" in quiz_js)
check("quiz-panel.js: ведущий управляет всеми стадиями",
      all(a in quiz_panel_js for a in
          ("'open'", "'close'", "'next'", "'board'", "'feed'", "'show'", "'stop'")))
check("quiz-panel.js: QR квиза запрашивается с kind=quiz", "kind=quiz" in quiz_panel_js)
check("quiz-panel.js: панель перезаряжается по сигналу из общего канала",
      "window.quizPanel" in quiz_panel_js)
check("moderator.html: вкладка «Квиз» есть", 'id="tabQuiz"' in mod_html)
check("moderator.html: страница вкладки «Квиз» есть", 'id="pageQuiz"' in mod_html)
check("moderator.html: скрипт квиза подключён", 'src="/static/quiz-panel.js"' in mod_html)
check("moderator.js: панель получает сигнал quiz_update", "case 'quiz_update'" in mod_js2)
check("slideshow-panel.js: переключатель вкладок знает про третью",
      "tabQuiz" in slide_js and "'quiz'" in slide_js)
check("screen.html: слой квиза есть", 'id="quizScreen"' in screen_html)
check("screen.js: экран рисует кадр игры и берёт его из init",
      "case 'quiz'" in screen_js and "msg.quiz" in screen_js)
screen_css = (STATIC / "screen.css").read_text(encoding="utf-8")
check("screen.css: слой квиза выше заставки (z-index > 20)",
      re.search(r"\.quiz-screen\s*\{[^}]*z-index:\s*(2[5-9]|[3-9]\d)", screen_css, re.S) is not None)
check("admin.html: строка ссылки на квиз есть", 'id="dQuiz"' in
      (STATIC / "admin.html").read_text(encoding="utf-8"))
check("admin.js: ссылку квиза можно скопировать",
      "['dCopyQuiz', 'quiz']" in (STATIC / "admin.js").read_text(encoding="utf-8"))
paren, brace = balance_outside_strings(quiz_js)
check("quiz.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")
paren, brace = balance_outside_strings(quiz_panel_js)
check("quiz-panel.js: скобки сходятся", paren == 0 and brace == 0,
      f"лишних ( {paren}, {{ {brace}" if (paren or brace) else "")

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Скрипты страниц в порядке.")