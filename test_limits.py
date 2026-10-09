"""Проверка ограничений показа: длина текста и подгонка фото под экран.

3.6 добавила жёсткий предел текста (56 символов) и правило для маленьких фото:
их надо увеличивать, но не больше свободного места, не растягивая пропорции и
не выходя за безопасную зону кадра.

Проверка серверной части идёт против работающего сервера, разметки — по файлам.

Запуск: python test_limits.py [http://127.0.0.1:8123]
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from testlib import prepare  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


class Client:
    """Отдельный браузер: своя банка cookie."""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def post(self, path: str, fields: dict):
        req = urllib.request.Request(
            BASE + path, data=urllib.parse.urlencode(fields).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return e.code, {}

    def get_json(self, path: str):
        try:
            with self.opener.open(BASE + path, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError:
            return {}

    def html(self, path: str) -> str:
        try:
            with self.opener.open(BASE + path, timeout=20) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError:
            return ""


print(f"\n=== Предел текста ===")

CTX = prepare("Проверка пределов")
SLUG = CTX.slug
BASE = CTX.base
TOKEN = CTX.code
PASS = CTX.mod_password

guest = Client()
st, joined = guest.post("/api/join", {"name": "Гость", "invite": TOKEN})
check("гость зашёл", st == 200, str(st))

# Предел узнаём у самого приложения, а не импортом из app.config: проверка
# должна работать откуда угодно (на сервере её запускают из /tmp, где пакета
# app рядом нет) и проверять контракт, а не внутреннюю константу.
cfg = guest.get_json("/api/config")
LIMIT = int(cfg.get("max_text_len") or 0)
check("предел виден в открытом конфиге", LIMIT > 0, str(cfg.get("max_text_len")))
check("предел правдоподобный", 10 <= LIMIT <= 500, str(LIMIT))
print(f"    предел: {LIMIT} символов")

# ровно на пределе — принимаем
limit_text = "Я" * LIMIT
st, res = guest.post("/api/messages", {"body": limit_text})
check("ровно на пределе принимается", st == 200, str(st))
check("текст сохранён целиком, без обрезки",
      res.get("message", {}).get("body") == limit_text,
      f"{len(res.get('message', {}).get('body', ''))} симв.")

# на символ больше — отказ с понятным объяснением
over_text = "Я" * (LIMIT + 1)
st, res = guest.post("/api/messages", {"body": over_text})
check("на символ больше — отказ", st == 400, str(st))
detail = res.get("detail", "")
check("в отказе сказано про предел", str(LIMIT) in detail, detail[:70])
check("в отказе сказано, на сколько сократить", "1" in detail, detail[:70])
check("текст не обрезан молча", "message" not in res, str(list(res)))

# сильно длинный — тоже отказ, а не молчаливое усечение
st, res = guest.post("/api/messages", {"body": "Я" * (LIMIT + 500)})
check("заметно длиннее — тоже отказ", st == 400, str(st))

# фотография без текста по-прежнему принимается
st, res = guest.post("/api/messages", {"body": ""})
check("пустой текст без фото — отказ", st == 400, str(st))

# многострочный текст считаем посимвольно, как это делает браузер
multiline = "Первая строка\nВторая строка"
check("перевод строки считается одним символом",
      len(multiline) <= LIMIT and len(multiline.split("\n")[0]) < LIMIT)
st, res = guest.post("/api/messages", {"body": multiline})
check("многострочный текст в пределах принимается", st == 200, str(st))

# кириллица в браузере считается по code point, а не по UTF-16:
# символы вне BMP занимают в JS два элемента, Python считает их одним.
st, res = guest.post("/api/messages", {"body": "🎉" * (LIMIT - 1)})
check("эмодзи до предела принимаются", st == 200, str(st))

print("\n=== Предел у ведущего ===")
mod = Client()
st, _ = mod.post("/api/moderator/login", {"password": PASS})
check("вход ведущего", st == 200, str(st))

sent = [m for m in mod.get_json("/api/moderator/state").get("messages", [])
        if m.get("body") == limit_text]
check("найдено сообщение для правки", bool(sent), str(len(sent)))

if sent:
    mid = sent[0]["id"]
    st, res = mod.post("/api/moderator/edit", {
        "message_id": mid, "body": "Я" * (LIMIT + 1), "author": "Гость",
    })
    check("правка длиннее предела отклонена", st == 400, str(st))
    after = [m for m in mod.get_json("/api/moderator/state").get("messages", [])
             if m["id"] == mid]
    check("после отклонённой правки текст прежний",
          after and after[0]["body"] == limit_text,
          (after[0]["body"][:20] if after else "нет"))

    st, res = mod.post("/api/moderator/edit", {
        "message_id": mid, "body": "Исправлено", "author": "Гость",
    })
    check("короткая правка проходит", st == 200, str(st))

print("\n=== Страница отправки ===")
send_html = guest.html("/send")
check("страница отправки открывается", "<textarea" in send_html)
check("в разметке есть поле счётчика", 'id="charCount"' in send_html)
check("в разметке есть сам предел", re.search(r'id="charLimit"', send_html) is not None)
check("maxlength стоит на пределе", f'maxlength="{LIMIT}"' in send_html,
      re.search(r'maxlength="(\d+)"', send_html).group(1) if "maxlength" in send_html else "нет")
check("кнопка называется «Отправить»", "Отправить на модерацию" not in send_html)
check("слов про модерацию на странице нет", "модерац" not in send_html.lower(),
      "осталось упоминание" if "модерац" in send_html.lower() else "")

if not (STATIC / "send.js").exists():
    print("  --   проверки скриптов пропущены: static/ рядом нет")
    print("\n=== Итог ===")
    if FAILURES:
        print(f"Провалено проверок: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("Пределы показа работают (вёрстка не проверялась).")
    raise SystemExit(0)

send_js = (STATIC / "send.js").read_text(encoding="utf-8")
check("счётчик обновляется на лету", "updateCounter" in send_js)
check("предел подставляется с сервера", "max_text_len" in send_js)
check("кнопка возвращает верную надпись",
      "'Отправить'" in send_js and "Отправить на модерацию" not in send_js)
check("счётчик предупреждает заранее", "near-limit" in send_js)

# --- дальше идут проверки файлов вёрстки -----------------------------------
# на сервере набор запускается из /tmp, где папки static/ рядом нет. Раньше
# это приводило к падению с FileNotFoundError и весь набор молча не отчитывался.
# Теперь такой запуск честно говорит, что эти проверки пропущены.
if not (STATIC / "screen.js").exists():
    print("\n=== Вёрстка экрана ===")
    print("  --   пропущено: папки static/ рядом нет (на сервере так и есть)")
    print("       эти проверки нужно гонять из корня проекта")
    print("\n=== Итог ===")
    if FAILURES:
        print(f"Провалено проверок: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("Пределы показа работают (вёрстка не проверялась).")
    raise SystemExit(0)

print("\n=== Фото на экране ===")
screen_js = (STATIC / "screen.js").read_text(encoding="utf-8")
screen_css = (STATIC / "screen.css").read_text(encoding="utf-8")

check("фото подгоняет функция", "function layoutSlide" in screen_js)
check("кегль текста подбирает отдельная функция", "function fitText" in screen_js)
check("подгонка зовётся при показе слайда",
      # 4.0: рисование вынесено в paintData — она берёт сам слайд, а не id,
      # потому что у ленты и галереи id начинаются с единицы и пересекаются
      re.search(r"function paint(?:Data)?\([^)]*\)\s*\{[\s\S]*?layoutSlide\(\)",
                screen_js) is not None)
check("подгонка зовётся при изменении размера окна",
      re.search(r"addEventListener\('resize'[\s\S]*?layoutSlide", screen_js) is not None)
check("подгонка зовётся при полном экране",
      re.search(r"fullscreenchange[\s\S]*?layoutSlide", screen_js) is not None)
check("увеличение фото ограничено коэффициентом", "MAX_UPSCALE" in screen_js)
check("коэффициент фото разумный", re.search(r"MAX_UPSCALE\s*=\s*[1-9]", screen_js) is not None)
check("короткий текст растёт", re.search(r"TEXT_GROW_MAX\s*=\s*[1-9]", screen_js) is not None)
check("увеличение текста ограничено", "TEXT_GROW_MAX" in screen_js)
check("кегль не падает ниже читаемого", re.search(r"TEXT_MIN_PX\s*=\s*\d+", screen_js) is not None)
check("пропорции сохраняются: размеры из натуральных",
      re.search(r"natW\s*\*\s*scale", screen_js) is not None
      and re.search(r"natH\s*\*\s*scale", screen_js) is not None)
check("место считается от безопасной зоны", "safeArea" in screen_js)
check("картинка докладывается только после загрузки",
      re.search(r"addEventListener\('load'", screen_js) is not None)
check("обрезки длинных текстов на экране больше нет",
      "maxTextLen" not in screen_js,
      "предел держится на входе, обрезка на показе лишняя")
slack = re.search(r"const TEXT_SLACK_PX = (\d+)", screen_js)
check("запас на округление строк задан числом", slack is not None)
check("запас на округление не нулевой",
      slack is not None and int(slack.group(1)) >= 1,
      "на 4K текст в 488 px обрезался снизу на два пикселя")
check("запас участвует в проверке высоты текста",
      re.search(r"sh\s*\+\s*TEXT_SLACK_PX\s*<=\s*budget", screen_js) is not None,
      "константа есть, но в расчёте не участвует")
check("высота текста мерится по scrollHeight, а не offsetHeight",
      re.search(r"const sh = textBox\.scrollHeight;", screen_js) is not None
      and not re.search(r"const h = textBox\.offsetHeight;", screen_js),
      "глифы выше строки: при блоке 159 px и глифах 161 px решение «влезает» "
      "по offsetHeight было неверным, текст срезался")
check("коэффициент подгонки текста тоже по scrollHeight",
      len(re.findall(r"TEXT_SLACK_PX\s*\)\s*/\s*sh\)", screen_js)) >= 2,
      "иначе при высоте ровно в бюджет отношение даёт единицу и цикл "
      "выходит, не дожав")
check("подгонка заканчивается на проверенном кегле",
      re.search(r"best\s*=\s*Math\.round\(size\)", screen_js) is not None
      and re.search(r"best\s*\?\?", screen_js) is not None,
      "размер, посчитанный в конце итерации, ещё не проверялся — у границы "
      "цикл раскачивался между двумя кеглями и оставлял обрезанный текст")

check("у сцены есть отступы безопасной зоны",
      re.search(r"\.screen-stage\s*\{[^}]*padding:[^;]*--safe-", screen_css) is not None)
check("фото не растянуто по площади",
      re.search(r"\.slide-photo img\s*\{[^}]*object-fit:\s*contain", screen_css) is not None)
check("нет жёсткой высоты картинки в процентах",
      re.search(r"\.slide-photo img\s*\{[^}]*height:\s*\d+vh", screen_css) is None,
      "высота в vh ломает пропорции")
check("фоновое свечение тоже в безопасной зоне",
      re.search(r"::before\s*\{[^}]*inset:\s*var\(--safe", screen_css) is not None)
check("текст на экране обрезается, а не вылезает за кадр",
      re.search(r"\.slide-text\s*\{[^}]*overflow:\s*hidden", screen_css) is not None)

print("\n=== Слайд собран как один блок ===")
screen_html = (STATIC / "screen.html").read_text(encoding="utf-8")
check("в разметке есть общий блок слайда", "slideInner" in screen_html)
inner_start = screen_html.index('id="slideInner"')
inner_end = screen_html.index("</div>", screen_html.index('id="slideAuthor"'))
block = screen_html[inner_start:inner_end]
check("фото, текст и подпись внутри одного блока",
      all(f'id="{i}"' in block for i in ("slidePhoto", "slideText", "slideAuthor")),
      "подпись отдельно от текста разъезжается на коротких сообщениях")
check("слайд растянут по высоте, а не по содержимому",
      re.search(r"\.slide\s*\{[^}]*align-self:\s*stretch", screen_css) is not None,
      "иначе короткий текст прилипает к верху кадра")
check("блок центрируется по вертикали",
      re.search(r"\.slide-inner\s*\{[^}]*justify-content:\s*center", screen_css) is not None)
check("высота блока берётся от сцены",
      re.search(r"\.slide-inner\s*\{[^}]*height:\s*100%", screen_css) is not None)
check("в расчёте есть запас на округление", "SAFETY_PX" in screen_js,
      "без запаса слайд выходил за зону на 1–2 px")

print("\n=== Крупный текст и подпись ===")
text_rule = re.search(r"\.slide-text\s*\{([^}]*)\}", screen_css)
author_rule = re.search(r"\.slide-author\s*\{([^}]*)\}", screen_css)
check("правило для текста найдено", text_rule is not None)
check("правило для подписи найдено", author_rule is not None)

if text_rule:
    body_t = text_rule.group(1)
    check("кегль зависит и от ширины, и от высоты экрана",
          re.search(r"clamp\([^)]*min\(\s*\d[\d.]*vw", body_t) is not None,
          "только vw: на низком широком экране текст уедет")
    check("ширина текста удержана в зоне",
          re.search(r"max-width:\s*min\(\s*22ch,\s*100%\s*\)", body_t) is not None,
          "22ch при большом кегле шире экрана")
    # глифы этой гарнитуры занимают 1.19 em; при строке 1.18 em низ букв с
    # выносными элементами срезался на 1-2 px при любом кегле
    lh = re.search(r"line-height:\s*([\d.]+)", body_t)
    check("высота строки объявлена числом", lh is not None)
    check("строка выше глифов",
          lh is not None and float(lh.group(1)) >= 1.22,
          f"line-height {lh.group(1) if lh else '?'}: глифы 1.19 em, "
          "низ текста срезается")

if author_rule:
    body_a = author_rule.group(1)
    check("подпись не вылезет за кадр",
          "overflow: hidden" in body_a and "max-width: 100%" in body_a)
    check("у подписи задана своя высота строки",
          re.search(r"line-height:\s*[\d.]+", body_a) is not None,
          "кегль подписи считается от высоты строки")
    check("подпись по ширине зоны, а не по содержимому",
          re.search(r"align-self:\s*stretch", body_a) is not None
          and re.search(r"overflow-wrap:\s*anywhere", body_a) is not None,
          "иначе длинное имя уедет за кадр вбок")
    check("подпись выровнена по центру", "text-align: center" in body_a)

print("\n=== Доли высоты: подпись 6 %, фото 64 % ===")


def const(name: str):
    """Значение числовой константы из screen.js — доля высоты зоны."""
    m = re.search(rf"const {name} = ([\d.]+)", screen_js)
    return float(m.group(1)) if m else None


photo_share = const("PHOTO_SHARE")
author_share = const("AUTHOR_SHARE")

check("доля фото задана числом", photo_share is not None)
check("доля подписи задана числом", author_share is not None)
if photo_share is not None:
    check("фото с текстом занимает ровно 64 % высоты",
          abs(photo_share - 0.64) < 1e-9, str(photo_share))
if author_share is not None:
    check("подпись занимает ровно 6 % высоты",
          abs(author_share - 0.06) < 1e-9, str(author_share))
if photo_share is not None and author_share is not None:
    check("доли вместе с зазорами не съедают зону",
          photo_share + author_share < 0.9,
          f"{photo_share + author_share:.2%} — тексту почти не остаётся")
    check("подпись никогда не больше фото", author_share < photo_share)

check("высота подписи считается от доли",
      re.search(r"fitAuthor\(innerH\s*\*\s*AUTHOR_SHARE\)", screen_js) is not None,
      "подпись должна занимать свою долю на любом экране")
check("высота фото считается от доли, а не от остатка",
      re.search(r"innerH\s*\*\s*PHOTO_SHARE", screen_js) is not None,
      "иначе доля снова зависит от длины текста")
check("подпись получает свою долю раньше фото и текста",
      screen_js.index("fitAuthor(innerH * AUTHOR_SHARE)")
      < screen_js.index("innerH * PHOTO_SHARE"),
      "иначе подпись отнимет место, отведённое фото")
check("тексту достаётся остаток зоны",
      re.search(r"innerH\s*-\s*photoH\s*-\s*authorH\s*-\s*gapsTotal", screen_js) is not None,
      "текст должен подстраиваться под то, что осталось от фото и подписи")
check("фото получает заданную высоту, а не остаток",
      re.search(r"function setPhoto\(", screen_js) is not None
      and "setPhoto(img, area, photoH)" in screen_js)
check("подпись рассчитывает отдельная функция", "function fitAuthor(" in screen_js)

# кегль подписи ищется делением отрезка: высота текста убывает с ростом
# кегля, поэтому «подогнать по числу строк» ужимал её втрое
fit_author = re.search(r"function fitAuthor\(budget\)[\s\S]*?\n}", screen_js)
check("тело функции подписи разобралось", fit_author is not None)
if fit_author:
    body_fa = fit_author.group(0)
    check("кегль подписи ищется делением отрезка",
          re.search(r"const\s+mid\s*=\s*\(lo\s*\+\s*hi\)\s*/\s*2", body_fa) is not None,
          "иначе подпись либо не влезет, либо ужмётся втрое")
    check("высота подписи меряется по-настоящему",
          re.search(r"authorBox\.scrollHeight\s*<=\s*budget", body_fa) is not None,
          "считать надо мерой, а не долями от высоты строки")
    check("подпись мерится по scrollHeight, а не offsetHeight",
          "authorBox.offsetHeight <=" not in body_fa,
          "глифы выше строки обрезаются по scrollHeight — на offsetHeight "
          "подпись теряла нижние пиксели своей доли")
    check("потолок подписи — одна строка в свою долю",
          re.search(r"budget\s*/\s*ratio", body_fa) is not None)
    check("подпись не мельче читаемого", re.search(r"AUTHOR_MIN_PX", body_fa) is not None)
    check("подпись возвращает занятую высоту",
          re.search(r"return\s+Math\.min\(budget", body_fa) is not None,
          "на неё дальше считают фото и текст")

check("только фото или только текст считается отдельной веткой",
      re.search(r"if\s*\(\s*!hasPhoto\s*\|\|\s*!hasText\s*\)", screen_js) is not None,
      "одна часть забирает всё, как было")
check("в ветке-исключении тоже идут от доли подписи",
      re.search(r"const rest = innerH - authorH - gapsTotal", screen_js) is not None)
check("прежних долей больше нет",
      "PHOTO_TEXT_SHARE" not in screen_js and "AUTHOR_WITH_PHOTO" not in screen_js,
      "схема 65 % и 0.8 от подписи заменена на 64 % и 6 %")

check("страница учитывает динамическую высоту экрана",
      re.search(r"height:\s*100dvh", screen_css) is not None,
      "на телефоне с панелью 100vh больше видимой области")

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Пределы показа работают.")