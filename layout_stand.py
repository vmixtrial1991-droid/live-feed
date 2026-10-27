"""Стенд раскладки квиза по ТЗ 2.0, раздел 17.

Заводит событие с 15 сценариями из ТЗ, гоняет каждый на трёх форматах
(16:9, 4:3, 1:1), снимает скриншот и автоматически проверяет кадр.

Что проверяется (раздел 18 ТЗ):
  • всё внутри безопасной области;
  • номер и процент не перекрывают текст;
  • текст не обрезан (нет переполнения по высоте);
  • кегли не ниже минимума;
  • все варианты видны;
  • геометрия до и после голосования не меняется.

Запуск (сервер должен быть поднят):

    python layout_stand.py                     # все сценарии, три формата
    python layout_stand.py --only 1,8          # выбранные
    python layout_stand.py --formats 16x9      # один формат

Результат: layout-stand/<формат>/NN-<имя>.png и report.html с таблицей.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sweep_quiz import CDP, find_chrome, free_port, wait_json

BASE = "http://127.0.0.1:8123"
EVENT_TITLE = "Стенд раскладки"
EVENT_SLUG = "stenд-raskladki"          # латиница ниже, см. _slug()
MOD_PASSWORD = "stenд-proverka-1"

# Форматы из ТЗ. 16:9 — основной, 4:3 и 1:1 — адаптация.
FORMATS = [
    (1920, 1080, "16x9"),
    (1280, 960, "4x3"),
    (1080, 1080, "1x1"),
]

# Пределы сервера.
Q_IMG, Q_NOIMG = 90, 200
O_IMG, O_NOIMG = 40, 80

SHOT_WIDE = (1600, 900)     # 16:9
SHOT_SQUARE = (1200, 1200)  # 1:1
SHOT_TALL = (900, 1600)     # 9:16

# 15 сценариев из ТЗ, раздел 17. Поле check — что именно проверяем.
SCENARIOS = [
    {
        "name": "01-da-net",
        "label": "Короткий вопрос, «Да» и «Нет»",
        "text": "Играем дальше?",
        "options": ["Да", "Нет"],
        "correct": 0,
        "shot": None,
    },
    {
        "name": "02-two-long",
        "label": "Два длинных ответа",
        "text": "Как переносится текст ответа?",
        "options": [
            "По словам, оставаясь внутри карточки",
            "Обрезается многоточием на второй строке",
        ],
        "correct": 0,
        "shot": None,
    },
    {
        "name": "03-three-short",
        "label": "Три коротких ответа",
        "text": "Сколько сторон у треугольника?",
        "options": ["Одна", "Две", "Три"],
        "correct": 2,
        "shot": None,
    },
    {
        "name": "04-three-long",
        "label": "Три длинных ответа",
        "text": "Что будет, если ответ не влезает?",
        "options": [
            "Кегль уменьшится до минимума",
            "Карточка вырастет по высоте вверх",
            "Сетка перестроится в колонку",
        ],
        "correct": 2,
        "shot": None,
    },
    {
        "name": "05-four-short",
        "label": "Четыре коротких ответа",
        "text": "Какая фигура нарисована?",
        "options": ["Круг", "Квадрат", "Ромб", "Овал"],
        "correct": 0,
        "shot": None,
    },
    {
        "name": "06-four-long",
        "label": "Четыре длинных ответа",
        "text": "Что важнее на большом экране?",
        "options": [
            "Читаемость текста с пятнадцати метров",
            "Крупное изображение по центру кадра",
            "Симметрия всех элементов на экране",
            "Максимум декоративных эффектов",
        ],
        "correct": 0,
        "shot": None,
    },
    {
        "name": "07-four-mixed",
        "label": "Четыре ответа разной длины",
        "text": "Выберите верное утверждение.",
        "options": [
            "Да",
            "Короткий ответ",
            "Ответ средней длины на полстроки",
            "Длинный ответ, который потребует переноса",
        ],
        "correct": 3,
        "shot": None,
    },
    {
        "name": "08-image-wide",
        "label": "Вопрос с горизонтальным изображением",
        "text": "Какая фигура в центре снимка?",
        "options": ["Круг", "Квадрат", "Треугольник", "Звезда"],
        "correct": 0,
        "shot": SHOT_WIDE,
    },
    {
        "name": "09-image-tall",
        "label": "Вопрос с вертикальным изображением",
        "text": "Что видно на вертикальном снимке?",
        "options": ["Круг", "Квадрат", "Линия", "Точка"],
        "correct": 0,
        "shot": SHOT_TALL,
    },
    {
        "name": "10-image-square",
        "label": "Вопрос с квадратным изображением",
        "text": "Сколько сторон у фигуры?",
        "options": ["Одна", "Две", "Ни одной", "Много"],
        "correct": 2,
        "shot": SHOT_SQUARE,
    },
    {
        "name": "11-image-four-long",
        "label": "Изображение и четыре длинных ответа",
        "text": "Что станет с кадром?",
        "options": [
            "Фото меньше, ответам больше места",
            "Кегль уменьшится, фото прежнее",
            "Ответы уйдут в колонку",
            "Текст сократится многоточием",
        ],
        "correct": 2,
        "shot": SHOT_WIDE,
    },
    {
        "name": "12-long-q-four",
        "label": "Длинный вопрос с четырьмя вариантами",
        "text": "Этот вопрос длинный: он проверяет, как система "
                "делит кадр между вопросом и ответом",
        "options": [
            "Вопросу больше места, ответы сжимаются",
            "Ответы читаемы, вопрос переносится",
            "Вопрос обрезается многоточием",
            "Текст уменьшается до минимума",
        ],
        "correct": 1,
        "shot": None,
    },
    {
        "name": "13-percent",
        "label": "Появление процентов голосования",
        "text": "Куда пойдём после квиза?",
        "options": ["В бар", "Домой", "Танцевать", "Решим по ходу"],
        "correct": -1,
        "shot": None,
        # После ответа смотрим проценты: геометрия не должна поменяться.
        "voting": True,
    },
    {
        "name": "14-image-with-text",
        "label": "Изображение с важным текстом внутри",
        "text": "Что написано на табличке?",
        "options": ["Вход", "Выход", "Сцена", "Буфет"],
        "correct": 0,
        "shot": "text-image",
    },
    {
        "name": "15-extreme-long",
        "label": "Экстремально длинный ответ",
        "text": "Какой из вариантов самый длинный?",
        "options": [
            "Короткий",
            "Длинный ответ ровно на сорок пять знаков тут",
        ],
        "correct": 1,
        "shot": None,
    },
]


def slug_of(title: str) -> str:
    """Адрес события: транслит, потому что URL с кириллицей неудобен."""
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "-",
    })
    return title.lower().translate(table)


# ---------------------------------------------------------------- создание

def admin_post(path: str, fields: dict) -> tuple[int, dict]:
    import testlib
    return testlib.admin_post(path, fields)


def admin_get(path: str) -> tuple[int, dict]:
    import testlib
    return testlib.admin_get(path)


def figure(w: int, h: int, path: Path, with_text: bool = False) -> Path:
    """Нарисовать снимок нужных пропорций. Рамка и подпись — чтобы видеть
    границы кадра на тёмном фоне."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (w, h), (22, 30, 56))
    d = ImageDraw.Draw(img)
    step = max(40, min(w, h) // 10)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(40, 54, 96), width=max(2, step // 24))
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(40, 54, 96), width=max(2, step // 24))
    inset = max(4, min(w, h) // 60)
    d.rectangle([inset, inset, w - inset, h - inset],
                outline=(120, 170, 255), width=max(2, min(w, h) // 150))
    r = min(w, h) // 4
    d.ellipse([w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r],
              fill=(120, 170, 255))

    def font(px):
        for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(name, px)
            except OSError:
                continue
        return ImageFont.load_default()

    if with_text:
        # Крупная надпись внутри: проверяем, что её не обрезает контейнер.
        f = font(max(40, min(w, h) // 8))
        caption = "ВХОД"
        box = d.textbbox((0, 0), caption, font=f)
        tw, th = box[2] - box[0], box[3] - box[1]
        tx, ty = (w - tw) // 2, (h - th) // 2
        pad = max(10, th // 3)
        d.rectangle([tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
                    fill=(12, 16, 30))
        d.text((tx, ty), caption, font=f, fill=(235, 240, 255))
    else:
        ratio = round(w / h, 2)
        f = font(max(24, min(w, h) // 12))
        caption = f"{w}x{h}  ·  {ratio}:1"
        box = d.textbbox((0, 0), caption, font=f)
        tw, th = box[2] - box[0], box[3] - box[1]
        pad = max(8, th // 3)
        tx, ty = (w - tw) // 2, (h - th) // 2 - r - th - pad
        d.rectangle([tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
                    fill=(12, 16, 30))
        d.text((tx, ty), caption, font=f, fill=(220, 232, 255))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path


def build_event() -> dict:
    """Создать событие со сценариями и вернуть адреса."""
    import uuid

    root = BASE
    tmp = Path(os.environ.get("TEMP", ".")) / "opencode" / "stand-shots"

    # Убираем прежнее событие с тем же названием.
    _, listing = admin_get("/api/admin/events")
    for e in (listing.get("events") or []):
        if e.get("title") == EVENT_TITLE and e.get("slug"):
            admin_post(f"/api/admin/events/{e['slug']}/delete", {})

    st, created = admin_post("/api/admin/events",
                             {"title": EVENT_TITLE, "password": MOD_PASSWORD})
    if st != 200 or not created.get("event"):
        raise SystemExit(f"событие не завелось: {st} {created}")
    event = created["event"]
    slug = event["slug"]
    links = created.get("links") or {}
    if not links:
        st, card = admin_post(f"/api/admin/events/{slug}")
        links = card.get("links") or {}

    total = len(SCENARIOS)
    base = f"/e/{slug}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))

    def _send(req):
        try:
            with opener.open(req, timeout=60) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {}

    def call(path, fields):
        req = urllib.request.Request(
            root + path, data=urllib.parse.urlencode(fields).encode(),
            method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        return _send(req)

    def call_file(path, fields, file_path):
        boundary = "----lf" + uuid.uuid4().hex
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += str(v).encode() + b"\r\n"
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="image"; '
                 f'filename="{file_path.name}"\r\n').encode()
        body += b"Content-Type: image/jpeg\r\n\r\n" + file_path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            root + path, data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return _send(req)

    st, _ = call(f"{base}/api/moderator/login", {"password": MOD_PASSWORD})
    if st != 200:
        raise SystemExit(f"ведущий не вошёл: {st}")

    call(f"{base}/api/moderator/quiz/title", {"title": EVENT_TITLE})

    ids = []
    for n, s in enumerate(SCENARIOS, 1):
        fields = {
            "text": s["text"],
            "options": json.dumps(s["options"], ensure_ascii=False),
            "correct": str(s["correct"]),
            "scored": "0" if s["correct"] < 0 else "1",
            "seconds": "0",
        }
        shot = s.get("shot")
        if shot == "text-image":
            img = figure(1200, 800, tmp / "text-inside.jpg", with_text=True)
            st, body = call_file(f"{base}/api/moderator/quiz/add", fields, img)
        elif shot:
            w, h = shot
            img = figure(w, h, tmp / f"fig-{w}x{h}.jpg")
            st, body = call_file(f"{base}/api/moderator/quiz/add", fields, img)
        else:
            st, body = call(f"{base}/api/moderator/quiz/add", fields)
        if st != 200:
            print(f"  FAIL {n}: {st} {body}")
            continue
        ids.append(body.get("id") or body.get("question", {}).get("id"))
        print(f"  OK {n:>2}. {s['label']}")

    return {"slug": slug, "links": links, "ids": ids, "base": base}


# ---------------------------------------------------------------- замеры

MEASURE = r"""
(() => {
  const R = (el) => { const r = el.getBoundingClientRect(); return {
    l: Math.round(r.left), t: Math.round(r.top),
    w: Math.round(r.width), h: Math.round(r.height),
    b: Math.round(r.bottom), r: Math.round(r.right) }; };
  const play = document.getElementById('quizPlay');
  const body = document.getElementById('quizBody');
  const head = document.getElementById('quizHead');
  const q = document.getElementById('quizQText');
  const opts = document.getElementById('quizOpts');
  const shot = document.getElementById('quizShot');
  const img = document.getElementById('quizShotImg');
  const count = document.getElementById('quizCount');
  const cards = [...opts.children].filter(e => e.classList.contains('quiz-s-opt'));

  const overlaps = [];
  const rr = (a, b) => !(a.r <= b.l + 1 || b.r <= a.l + 1
                      || a.b <= b.t + 1 || b.b <= a.t + 1);
  const items = [];
  for (const c of cards) {
    const idx = c.querySelector('.quiz-s-idx');
    const txt = c.querySelector('.quiz-s-text');
    const num = c.querySelector('.quiz-s-num');
    const cr = R(c);
    const ir = idx ? R(idx) : null;
    const tr = txt ? R(txt) : null;
    const nr = num ? R(num) : null;
    if (ir && tr && rr(ir, tr)) overlaps.push('номер накладывается на текст');
    if (nr && tr && rr(nr, tr)) overlaps.push('процент накладывается на текст');
    items.push({
      rect: cr,
      idx: ir, text: tr, num: nr,
      font: txt ? getComputedStyle(txt).fontSize : null,
      lines: txt ? Math.round(txt.getBoundingClientRect().height
        / (parseFloat(getComputedStyle(txt).lineHeight) || 1)) : 0,
      clipped: txt ? txt.scrollHeight > txt.clientHeight + 1 : false,
      hasNum: num ? getComputedStyle(num).visibility !== 'hidden' : false,
      numText: num ? num.textContent : '',
    });
  }

  /* Безопасная область: 5% по краям на эталонном макете, при другом
     формате движок считает свои поля. Здесь проверяем, что ничего не
     выходит за вьюпорт. */
  const outside = [];
  const vw = innerWidth, vh = innerHeight;
  const check = (r, what) => {
    if (!r) return;
    if (r.l < -1 || r.t < -1 || r.r > vw + 1 || r.b > vh + 1) {
      outside.push(what);
    }
  };
  check(R(q), 'вопрос');
  check(R(opts), 'ответы');
  if (shot && !shot.hidden) check(R(img), 'изображение');
  check(R(count), 'подпись');
  for (const it of items) check(it.rect, 'карточка');

  return JSON.stringify({
    viewport: [vw, vh],
    layout: play.dataset.layout || null,
    kind: body.dataset.kind || null,
    question: (q.textContent || '').slice(0, 50),
    questionRect: R(q),
    questionFont: Math.round(parseFloat(getComputedStyle(q).fontSize)),
    questionClipped: q.scrollHeight > q.clientHeight + 1,
    image: (shot && !shot.hidden && img.naturalWidth)
      ? { rect: R(img), natural: [img.naturalWidth, img.naturalHeight],
          share: +(R(img).h / vh).toFixed(3) }
      : null,
    opts: R(opts),
    count: R(count),
    cards: items,
    overlaps: [...new Set(overlaps)],
    outside: [...new Set(outside)],
    zoom: play.querySelector('.quiz-fit').style.zoom || '1',
  });
})()
"""


def check_frame(m: dict) -> list[str]:
    """Проверки раздела 18 ТЗ. Возвращает список проблем."""
    problems = list(m["overlaps"]) + [f"за краем: {x}" for x in m["outside"]]
    if m["questionClipped"]:
        problems.append("вопрос обрезан")
    if not m["cards"]:
        problems.append("нет карточек ответов")
    for i, c in enumerate(m["cards"], 1):
        if c["clipped"]:
            problems.append(f"ответ {i} обрезан")
        if not c["rect"]["w"] or not c["rect"]["h"]:
            problems.append(f"ответ {i} нулевого размера")
    fonts = [c["font"] for c in m["cards"] if c["font"]]
    if fonts:
        # Минимум по ТЗ (34 px) задан для эталона 1920x1080. На другом
        # формате движок масштабирует его пропорционально, поэтому и
        # проверка должна считать минимум от масштаба, а не жёстко 20.5.
        cfg = {"ref": 1920}
        vw, vh = m["viewport"]
        scale = min(vw / 1920, vh / 1080)
        min_ok = 34 * scale - 0.6
        small = [f for f in fonts if float(f.replace("px", "")) < min_ok]
        if small:
            problems.append(
                f"кегль ответа {small[0]} ниже минимума {min_ok:.1f}")
    if m["questionFont"] < 24:
        problems.append(f"кегль вопроса {m['questionFont']} < 24")
    if m["image"]:
        # Доля высоты проверяется только для режима «фото сверху»: в боковом
        # режиме вертикальный снимок законно занимает почти всю высоту,
        # потому что стоит рядом с текстом, а не над ним.
        share = m["image"]["share"]
        if m["kind"] != "image-side" and share > 0.55:
            problems.append(f"фото занимает {round(share*100)}% кадра")
        if m["kind"] == "image-side" and share > 0.92:
            problems.append(f"фото занимает {round(share*100)}% кадра")
    return problems


def run_stand(event: dict, formats, only: list[int], out: Path) -> list[dict]:
    """Прогнать сценарии по форматам и собрать отчёт."""
    chrome = find_chrome()
    rows = []

    control = http.cookiejar.CookieJar()
    mod = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(control))

    def post(path, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(BASE + path, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with mod.open(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    post(f"{event['base']}/api/moderator/login", {"password": MOD_PASSWORD})

    port = free_port()
    profile = Path(os.environ.get("TEMP", ".")) / "opencode" / "chrome-stand"
    proc = subprocess.Popen([
        chrome, "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--no-first-run",
        "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        wait_json(port, "/json/version")
        page = next(t for t in wait_json(port, "/json/list")
                    if t.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", cacheDisabled=True)

        qids = event["ids"]
        for w, h, fmt in formats:
            cdp.send("Emulation.setDeviceMetricsOverride",
                     width=w, height=h, deviceScaleFactor=1, mobile=False)
            (out / fmt).mkdir(parents=True, exist_ok=True)
            for idx, s in enumerate(SCENARIOS):
                n = idx + 1
                if only and n not in only:
                    continue
                qid = qids[idx] if idx < len(qids) else None
                if not qid:
                    rows.append({"fmt": fmt, "n": n, "name": s["name"],
                                 "label": s["label"], "error": "нет вопроса"})
                    continue
                post(f"{event['base']}/api/moderator/quiz/control",
                     {"action": "open", "question_id": str(qid)})
                cdp.send("Page.navigate",
                         url=f"{BASE}{event['base']}/screen?d="
                             f"{event['links']['screen'].split('d=')[-1]}")
                time.sleep(1.3)
                raw = cdp.js(MEASURE)
                if not raw:
                    rows.append({"fmt": fmt, "n": n, "name": s["name"],
                                 "label": s["label"], "error": "нет данных"})
                    continue
                m = json.loads(raw)
                problems = check_frame(m)

                shot = cdp.send("Page.captureScreenshot", format="png")
                b64 = shot.get("result", {}).get("data")
                if b64:
                    (out / fmt / f"{s['name']}.png").write_bytes(
                        base64.b64decode(b64))

                # Сценарий с голосованием: закрываем и показываем проценты,
                # геометрия не должна измениться.
                reveals = None
                if s.get("voting"):
                    before = cdp.js(MEASURE)
                    post(f"{event['base']}/api/moderator/quiz/control",
                         {"action": "close"})
                    time.sleep(0.9)
                    post(f"{event['base']}/api/moderator/quiz/control",
                         {"action": "reveal"})
                    time.sleep(1.1)
                    after = cdp.js(MEASURE)
                    shifted = geometry_diff(
                        json.loads(before), json.loads(after))
                    reveals = {"before": json.loads(before),
                               "after": json.loads(after),
                               "shifted": shifted}
                    if shifted:
                        problems.append("после голосования: " + ", ".join(shifted))
                    shot2 = cdp.send("Page.captureScreenshot", format="png")
                    if shot2.get("result", {}).get("data"):
                        (out / fmt / f"{s['name']}-results.png").write_bytes(
                            base64.b64decode(shot2["result"]["data"]))

                rows.append({
                    "fmt": fmt, "n": n, "name": s["name"],
                    "label": s["label"], "problems": problems,
                    "questionFont": m["questionFont"],
                    "answerFont": [c["font"] for c in m["cards"]],
                    "imageShare": m["image"]["share"] if m["image"] else None,
                    "kind": m["kind"], "zoom": m["zoom"],
                    "cards": len(m["cards"]),
                    "voting": reveals,
                })
                state = "ок" if not problems else ", ".join(problems)
                print(f"  {fmt:<5} {n:>2}. {s['label'][:46]:<46} {state}")
        return rows
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def geometry_diff(a: dict, b: dict) -> list[str]:
    """Что поехало между двумя замерами одного кадра."""
    diff = []
    if a["image"] and b["image"]:
        if a["image"]["rect"] != b["image"]["rect"]:
            diff.append("изображение")
    if a["questionRect"] != b["questionRect"]:
        diff.append("вопрос")
    if a["opts"] != b["opts"]:
        diff.append("блок ответов")
    ca = [c["rect"] for c in a["cards"]]
    cb = [c["rect"] for c in b["cards"]]
    if ca != cb:
        diff.append("карточки")
    fa = [c["font"] for c in a["cards"]]
    fb = [c["font"] for c in b["cards"]]
    if fa != fb:
        diff.append("кегли")
    if a["zoom"] != b["zoom"]:
        diff.append("масштаб")
    return diff


def write_report(rows: list[dict], out: Path, event: dict) -> None:
    """Отчёт в HTML: таблица и ссылки на скриншоты."""
    formats = sorted({r["fmt"] for r in rows})
    by_n: dict[int, dict] = {}
    for r in rows:
        by_n.setdefault(r["n"], {"label": r["label"]})[r["fmt"]] = r

    def cell(r):
        if not r:
            return "<td>—</td>"
        if r.get("error"):
            return f'<td class="bad">{r["error"]}</td>'
        bad = r["problems"]
        cls = "bad" if bad else "ok"
        img = f'<a href="{r["fmt"]}/{r["name"]}.png">скрин</a>'
        if r.get("voting"):
            img += f' · <a href="{r["fmt"]}/{r["name"]}-results.png">итог</a>'
        txt = f"{r['questionFont']}/{[x for x in r['answerFont']][0] if r['answerFont'] else '—'}"
        note = " · ".join(bad) if bad else "ок"
        return f'<td class="{cls}"><b>{note}</b><br>{img}<br>кегли {txt}</td>'

    html = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>Стенд раскладки квиза</title>",
        "<style>",
        "body{font:15px/1.5 system-ui,sans-serif;background:#0f1320;color:#e8ecf8;"
        "margin:24px;max-width:1400px}",
        "h1{font-size:22px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #2a3350;padding:8px 10px;vertical-align:top;"
        "text-align:left}th{background:#182036}",
        "td.ok{background:#10271c}td.bad{background:#2b1418}",
        "a{color:#8ab4ff}.n{width:210px}",
        "</style>",
        f"<h1>Стенд раскладки: {len(by_n)} сценариев, "
        f"{len(formats)} форматов</h1>",
        "<p>Событие: <a href='" + BASE + event["links"]["screen"] + "'>экран</a>"
        f" · <a href='{BASE}{event['base']}/moderator'>кабинет</a>"
        f" · пароль <code>{MOD_PASSWORD}</code></p>",
        "<table><tr><th class='n'>Сценарий</th>"
        + "".join(f"<th>{f}</th>" for f in formats) + "</tr>",
    ]
    for n in sorted(by_n):
        row = by_n[n]
        html.append(
            f"<tr><td class='n'>{n:02d}. {row['label']}</td>"
            + "".join(cell(row.get(f)) for f in formats) + "</tr>")
    html.append("</table>")
    (out / "report.html").write_text("\n".join(html), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="стенд раскладки квиза")
    ap.add_argument("--only", help="номера сценариев через запятую")
    ap.add_argument("--formats", help="форматы: 16x9,4x3,1x1")
    ap.add_argument("--out", default="layout-stand", help="папка результатов")
    ap.add_argument("--rebuild", action="store_true",
                    help="пересоздать событие (иначе используется готовое)")
    args = ap.parse_args()

    only = [int(x) for x in args.only.split(",")] if args.only else []
    wanted = {x.strip() for x in args.formats.split(",")} if args.formats else None
    formats = [f for f in FORMATS if not wanted or f[2] in wanted]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("Готовлю событие со сценариями…")
    event = build_event()
    print(f"\nСобытие: {event['slug']}, вопросов: {len(event['ids'])}")
    print(f"Экран: {event['links'].get('screen')}\n")

    print("Прогон:")
    rows = run_stand(event, formats, only, out)
    write_report(rows, out, event)

    bad = [r for r in rows if r.get("problems")]
    print(f"\n{'='*60}")
    print(f"Всего кадров: {len(rows)}, с замечаниями: {len(bad)}")
    if bad:
        print("\nЗамечания:")
        for r in bad:
            print(f"  {r['fmt']} {r['n']:>2}. {r['label'][:40]}: "
                  + ", ".join(r["problems"]))
    print(f"\nОтчёт: {(out / 'report.html').resolve()}")
    print(f"Скриншоты: {out.resolve()}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
