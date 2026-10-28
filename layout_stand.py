"""Стенд раскладки квиза по ТЗ «Вёрстка экрана квиза».

Заводит событие со сценариями и снимает кадры на холстах из раздела 10:

  • полная матрица на 16:9 (1920×1080): 3 количества ответов (2/3/4)
    × 6 вариантов картинки (нет / 16:9 / 4:3 / 1:1 / 3:4 / 9:16)
    × 2 объёма текста (короткий / длинный) = 36 кадров;
  • сокращённый набор на 1920×1200 (16:10), 1800×1080 (5:3),
    1440×1080 (4:3), 1280×720 — по 6 сценариев из раздела 10.2.

Что проверяется (раздел 10.3):
  • ничего не выходит за область контента;
  • текст не перекрывает бейдж и зону процента (показываем «100%»);
  • короткие тексты дают компактные карточки, а не на всю ширину;
  • картинка не обрезана, не искажена и увеличена не больше 1.5×;
  • кегли не ниже минимумов;
  • кегли в виртуальных px одинаковы на 16:10, 5:3 и 4:3;
  • квадратная картинка: на 4:3 схема B, на 16:9 — C.

Запуск (сервер должен быть поднят):

    python layout_stand.py                    # всё
    python layout_stand.py --formats 16x9     # только основная матрица
    python layout_stand.py --only 8,11        # выбранные сценарии

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
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sweep_quiz import CDP, find_chrome, free_port, wait_json

BASE = "http://127.0.0.1:8123"
EVENT_TITLE = "Стенд раскладки"
EVENT_SLUG = "stand-raskladki"
MOD_PASSWORD = "stand-proverka-1"

# Холсты из раздела 10. 16:9 — основная матрица, остальные — сокращённый
# набор. 1280×720 проверяет масштаб k.
FORMATS = [
    (1920, 1080, "16x9"),
    (1920, 1200, "16x10"),
    (1800, 1080, "5x3"),
    (1440, 1080, "4x3"),
    (1280, 720, "1280x720"),
]

# Пропорции картинок из раздела 10.1.
SHOTS = {
    "none": None,
    "16x9": (1600, 900),
    "4x3": (1200, 900),
    "1x1": (1200, 1200),
    "3x4": (900, 1200),
    "9x16": (900, 1600),
}

# Тексты из раздела 10.1. «Короткий» — вопрос около 30 знаков и ответы по
# 1–2 слова. «Длинный» — на пределе, который сегодня принимает сервер:
# лимиты длины из раздела 12 ТЗ пока не приняты, в кабинете действуют
# прежние (90/45 без картинки, 65/35 с картинкой), и стенд обязан
# проверять именно ту границу, которую автор вопроса реально увидит.
SHORT_Q = "Какая фигура нарисована в центре?"
LONG_Q = (
    "Перед вами длинный вопрос: он проверяет, как раскладка делит высоту "
    "кадра между текстом, картинкой и вариантами ответа, когда места "
    "заведомо меньше, чем хотелось бы"
)
SHORT_ANSWERS = {
    2: ["Да", "Нет"],
    3: ["Круг", "Квадрат", "Ромб"],
    4: ["Круг", "Квадрат", "Ромб", "Овал"],
}
LONG_ANSWERS = {
    2: [
        "Кегль уменьшится до минимума, а текст обрежется",
        "Карточка вырастет по высоте и вытеснит остальные",
    ],
    3: [
        "Кегль уменьшится до минимума, а текст обрежется",
        "Карточка вырастет по высоте и вытеснит остальные",
        "Сетка перестроится в колонку на всю ширину",
    ],
    4: [
        "Кегль уменьшится до минимума, а текст обрежется",
        "Карточка вырастет по высоте и вытеснит остальные",
        "Сетка перестроится в колонку на всю ширину",
        "Картинка сожмётся, освобождая место под текст",
    ],
}

def limits_for(shot, count: int) -> tuple[int, int]:
    """Пределы вопроса и ответа для этого сценария.

    Берём их из той же таблицы, что и сервер (app/quiz.py, раздел 12 ТЗ):
    копия чисел в стенде разошлась бы с приложением молча, и стенд начал
    бы снимать кадры, которые в кабинете не набираются. Импорт безопасный:
    app.quiz не тянет FastAPI, это чистые функции.
    """
    from app import quiz as quizmod

    has_image = bool(shot)
    q = quizmod.question_limits(has_image)["hard"]
    o = quizmod.option_limits(has_image, count)["hard"]
    return q, o


def fit_text(text: str, limit: int) -> str:
    """Уложить текст в предел сервера по границе слова."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut[:limit].strip()


# Сценарии сокращённого набора (раздел 10.2).
SMALL_SET = [
    {"count": 4, "shot": "none", "long": True,  "label": "4 ответа, без картинки, длинный"},
    {"count": 2, "shot": "none", "long": False, "label": "2 ответа, без картинки, короткий"},
    {"count": 3, "shot": "16x9", "long": False, "label": "3 ответа, картинка 16:9"},
    {"count": 4, "shot": "4x3",  "long": True,  "label": "4 ответа, картинка 4:3, длинный"},
    {"count": 4, "shot": "1x1",  "long": True,  "label": "4 ответа, картинка 1:1"},
    {"count": 3, "shot": "9x16", "long": True,  "label": "3 ответа, картинка 9:16, длинный"},
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


def figure(w: int, h: int, path: Path) -> Path:
    """Нарисовать снимок нужных пропорций. Рамка и подпись — чтобы видеть
    границы кадра на тёмном фоне и поймать обрезку."""
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

    ratio = round(w / h, 2)
    f = font(max(20, min(w, h) // 14))
    caption = f"{w}×{h} · {ratio}:1"
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


def full_matrix() -> list[dict]:
    """36 сценариев раздела 10.1: 3 × 6 × 2."""
    out = []
    for count in (2, 3, 4):
        for shot_name, shot in SHOTS.items():
            for long in (False, True):
                answers = (LONG_ANSWERS if long else SHORT_ANSWERS)[count]
                out.append({
                    "name": f"{count}-{shot_name}-{'long' if long else 'short'}",
                    "label": f"{count} отв., фото {shot_name}, "
                             f"{'длинный' if long else 'короткий'}",
                    "text": LONG_Q if long else SHORT_Q,
                    "options": answers,
                    "shot": shot,
                    "long": long,
                    "count": count,
                    "shot_name": shot_name,
                    "small": False,
                })
    return out

def small_set() -> list[dict]:
    """6 сценариев раздела 10.2."""
    out = []
    for i, s in enumerate(SMALL_SET, 1):
        answers = (LONG_ANSWERS if s["long"] else SHORT_ANSWERS)[s["count"]]
        out.append({
            "name": f"s{i}-{s['count']}-{s['shot']}",
            "label": s["label"],
            "text": LONG_Q if s["long"] else SHORT_Q,
            "options": answers,
            "shot": SHOTS[s["shot"]],
            "long": s["long"],
            "count": s["count"],
            "shot_name": s["shot"],
            "small": True,
        })
    return out


def build_event(scenarios: list[dict]) -> dict:
    """Создать событие со сценариями и вернуть адреса."""
    root = BASE
    tmp = Path(os.environ.get("TEMP", ".")) / "opencode" / "stand-shots"

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

    call(f"{base}/api/moderator/quiz/title", {"title": "Стенд раскладки"})

    ids = []
    for n, s in enumerate(scenarios, 1):
        # Длинный текст подрезаем под пределы сервера: стенд проверяет
        # границу, которую автор вопроса реально увидит в кабинете.
        limit_q, limit_o = limits_for(s.get("shot"), len(s["options"]))
        fields = {
            "text": fit_text(s["text"], limit_q),
            # правильный вариант не важен: кадр снимаем до голосования
            "options": json.dumps(
                [fit_text(o, limit_o) for o in s["options"]], ensure_ascii=False),
            "correct": "0",
            "scored": "1",
            "seconds": "0",
        }
        shot = s.get("shot")
        if shot:
            w, h = shot
            img = figure(w, h, tmp / f"fig-{w}x{h}-{n}.jpg")
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
    /* «100%» — самая широкая надпись, которая вообще может попасть в зону.
       Ставим её на время замера и возвращаем прежний текст: если она
       влезает в свою колонку, влезет и любое другое значение. */
    let numFit = true;
    if (num) {
      const was = num.textContent;
      const hidden = getComputedStyle(num).visibility === 'hidden';
      num.style.visibility = 'visible';
      const keep = num.classList.contains('is-blank');
      num.classList.remove('is-blank');
      num.textContent = '100%';
      numFit = num.scrollWidth <= num.clientWidth + 1;
      num.textContent = was;
      if (keep) num.classList.add('is-blank');
      if (hidden) num.style.visibility = '';
    }
    if (ir && tr && rr(ir, tr)) overlaps.push('номер накладывается на текст');
    if (nr && tr && rr(nr, tr)) overlaps.push('процент накладывается на текст');
    items.push({
      rect: cr,
      idx: ir, text: tr, num: nr,
      font: txt ? parseFloat(getComputedStyle(txt).fontSize) : 0,
      lines: txt ? Math.round(txt.getBoundingClientRect().height
        / (parseFloat(getComputedStyle(txt).lineHeight) || 1)) : 0,
      clipped: txt ? txt.scrollHeight > txt.clientHeight + 1 : false,
      cardClipped: c.scrollHeight > c.clientHeight + 1,
      hasNum: num ? true : false,
      numFit,
      numText: num ? num.textContent : '',
    });
  }

  /* Область контента по ТЗ: 5% полей с каждой стороны и 1920 виртуальных
     px по центру, если холст шире 16:9. Считаем ту же геометрию, что и
     движок, и проверяем попадание в неё. */
  const vw = innerWidth, vh = innerHeight;
  const k = Math.min(vw / 1920, vh / 1080);
  const Cw = Math.min(vw * 0.9, 1920 * k);
  const Ch = vh * 0.9;
  const area = {
    l: Math.round((vw - Cw) / 2), r: Math.round((vw + Cw) / 2),
    t: Math.round((vh - Ch) / 2), b: Math.round((vh + Ch) / 2),
  };

  const outside = [];
  const check = (r, what) => {
    if (!r) return;
    if (r.l < -1 || r.t < -1 || r.r > vw + 1 || r.b > vh + 1) {
      outside.push(what + ' за краем экрана');
    } else if (r.l < area.l - 2 || r.r > area.r + 2
            || r.t < area.t - 2 || r.b > area.b + 2) {
      outside.push(what + ' за пределами области контента');
    }
  };
  check(R(q), 'вопрос');
  check(R(opts), 'ответы');
  if (shot && !shot.hidden && img.naturalWidth) check(R(img), 'изображение');
  if (count && count.textContent) check(R(count), 'подпись');
  for (const it of items) check(it.rect, 'карточка');

  return JSON.stringify({
    viewport: [vw, vh],
    scale: +k.toFixed(4),
    layoutV: play.dataset.layout || null,
    kind: body.dataset.kind || null,
    planLayout: (typeof quizPlayLayout !== 'undefined' && quizPlayLayout.plan)
      ? quizPlayLayout.plan.layout : null,
    questionFont: parseFloat(getComputedStyle(q).fontSize),
    questionClipped: q.scrollHeight > q.clientHeight + 1,
    image: (shot && !shot.hidden && img.naturalWidth)
      ? { rect: R(img), natural: [img.naturalWidth, img.naturalHeight],
          upscale: +(R(img).w / img.naturalWidth).toFixed(3),
          share: +(R(img).h / vh).toFixed(3) }
      : null,
    questionRect: R(q),
    opts: R(opts),
    count: R(count),
    cards: items,
    overlaps: [...new Set(overlaps)],
    outside: [...new Set(outside)],
    zoom: play.querySelector('.quiz-fit').style.zoom || '1',
    warnings: (typeof quizPlayLayout !== 'undefined' && quizPlayLayout.plan)
      ? (quizPlayLayout.plan.warnings || []) : [],
    cardWidth: items.length ? items[0].rect.w : 0,
  });
})()
"""


def check_frame(m: dict, scenario: dict | None = None,
                fmt: str | None = None) -> list[str]:
    """Проверки раздела 10.3."""
    problems = list(m["overlaps"]) + m["outside"]

    if m["layoutV"] != "v3":
        problems.append(f"раскладка {m['layoutV']}, а ожидалась v3")
    if m["questionClipped"]:
        problems.append("вопрос обрезан")
    if not m["cards"]:
        problems.append("нет карточек ответов")

    for i, c in enumerate(m["cards"], 1):
        if c["clipped"] or c["cardClipped"]:
            problems.append(f"ответ {i} обрезан")
        if not c["rect"]["w"] or not c["rect"]["h"]:
            problems.append(f"ответ {i} нулевого размера")
        # Проценты должны влезать в свою зону; до показа их место занято
        # невидимым «100%».
        if c["num"] and c["num"]["w"] < 1:
            problems.append(f"ответ {i}: нет зоны процента")
        # «100%» — самая широкая надпись. Если она шире своей колонки,
        # процент выедет на текст при любом реальном значении.
        if c.get("numFit") is False:
            problems.append(f"ответ {i}: «100%» не влезает в зону процента")

    if m["cards"]:
        fonts = [c["font"] for c in m["cards"]]
        k = m["scale"]
        if min(fonts) < 28 * k - 1:
            problems.append(
                f"кегль ответа {min(fonts):.1f} ниже минимума {28 * k:.1f}")
        if m["questionFont"] < 32 * k - 1:
            problems.append(
                f"кегль вопроса {m['questionFont']:.1f} ниже "
                f"{32 * k:.1f}")
        # Карточки одной группы одной ширины.
        widths = {c["rect"]["w"] for c in m["cards"]}
        if len(widths) > 1:
            problems.append("карточки разной ширины")
        # Компактность: короткий ответ не должен занимать пол-экрана.
        if m["cardWidth"] > m["viewport"][0] * 0.72:
            problems.append(
                f"карточка {m['cardWidth']} px — больше 70% ширины")

    if m["image"]:
        if m["image"]["upscale"] > 1.5 + 0.02:
            problems.append(f"картинка увеличена в {m['image']['upscale']}×")
        nw, nh = m["image"]["natural"]
        rw = m["image"]["rect"]["w"] / max(1, m["image"]["rect"]["h"])
        if abs(rw - nw / nh) > 0.03:
            problems.append("картинка искажена")

    for w in m["warnings"]:
        if "обрезан" in w or "вне 4:3" in w:
            problems.append(w)

    # Схема кадра по разделу 4: квадрат уходит в две колонки только на
    # достаточно широком холсте, на 4:3 остаётся стек. Это отдельный пункт
    # проверки 10.3, и он ловит сдвиг порога squareSideRatio.
    if scenario and scenario.get("shot_name") == "1x1":
        expect = "B" if fmt in ("4x3",) else "C"
        if m.get("planLayout") not in (None, expect):
            problems.append(
                f"квадрат дал схему {m['planLayout']}, ожидалась {expect}")
    return problems


def run_stand(event: dict, scenarios: list[dict], formats, only,
              out: Path) -> list[dict]:
    """Прогнать сценарии по холстам и собрать отчёт."""
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
        screen_url = f"{BASE}{event['base']}/screen?d=" \
                     f"{event['links']['screen'].split('d=')[-1]}"

        for w, h, fmt in formats:
            cdp.send("Emulation.setDeviceMetricsOverride",
                     width=w, height=h, deviceScaleFactor=1, mobile=False)
            (out / fmt).mkdir(parents=True, exist_ok=True)
            for idx, s in enumerate(scenarios):
                n = idx + 1
                if only and n not in only:
                    continue
                # Сокращённый набор — только на неосновных холстах.
                if s["small"] and fmt == "16x9":
                    continue
                if not s["small"] and fmt != "16x9":
                    continue
                qid = qids[idx] if idx < len(qids) else None
                if not qid:
                    rows.append({"fmt": fmt, "n": n, "name": s["name"],
                                 "label": s["label"], "error": "нет вопроса",
                                 "small": s["small"]})
                    continue
                post(f"{event['base']}/api/moderator/quiz/control",
                     {"action": "open", "question_id": str(qid)})
                cdp.send("Page.navigate", url=screen_url)
                time.sleep(1.4)
                raw = cdp.js(MEASURE)
                if not raw:
                    rows.append({"fmt": fmt, "n": n, "name": s["name"],
                                 "label": s["label"], "error": "нет данных",
                                 "small": s["small"]})
                    continue
                m = json.loads(raw)
                problems = check_frame(m, s, fmt)

                shot = cdp.send("Page.captureScreenshot", format="png")
                b64 = shot.get("result", {}).get("data")
                if b64:
                    (out / fmt / f"{s['name']}.png").write_bytes(
                        base64.b64decode(b64))

                # Распределение: показываем «100%» во всех карточках (раздел
                # 10.3 — процент не должен наезжать на текст) и убеждаемся,
                # что геометрия не поехала.
                after = None
                post(f"{event['base']}/api/moderator/quiz/control",
                     {"action": "close"})
                time.sleep(0.7)
                cdp.js("document.querySelectorAll('#quizOpts .quiz-s-num')"
                       ".forEach(n => { n.classList.remove('is-blank'); "
                       "n.textContent = '100%'; }); 0")
                time.sleep(0.4)
                after_raw = cdp.js(MEASURE)
                if after_raw:
                    after = json.loads(after_raw)
                    shifted = geometry_diff(m, after)
                    if shifted:
                        problems.append("после голосования: "
                                        + ", ".join(shifted))
                    shot2 = cdp.send("Page.captureScreenshot", format="png")
                    if shot2.get("result", {}).get("data"):
                        (out / fmt / f"{s['name']}-results.png").write_bytes(
                            base64.b64decode(shot2["result"]["data"]))
                post(f"{event['base']}/api/moderator/quiz/control",
                     {"action": "reset"})

                rows.append({
                    "fmt": fmt, "n": n, "name": s["name"],
                    "label": s["label"], "problems": problems,
                    "questionFont": m["questionFont"],
                    "answerFont": m["cards"][0]["font"] if m["cards"] else 0,
                    "scale": m["scale"],
                    "kind": m["kind"], "zoom": m["zoom"],
                    "cardWidth": m["cardWidth"],
                    "imageUpscale": m["image"]["upscale"] if m["image"] else None,
                    "cards": len(m["cards"]),
                    "warnings": m["warnings"],
                    "small": s["small"],
                })
                state = "ок" if not problems else ", ".join(problems)
                print(f"  {fmt:<9} {n:>2}. {s['label'][:44]:<44} {state}")
        return rows
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def geometry_diff(a: dict, b: dict) -> list[str]:
    """Что поехало между приёмом ответов и распределением."""
    diff = []
    if a["image"] and b["image"] and a["image"]["rect"] != b["image"]["rect"]:
        diff.append("изображение")
    if a["questionRect"] != b["questionRect"]:
        diff.append("вопрос")
    if a["opts"] != b["opts"]:
        diff.append("блок ответов")
    if [c["rect"] for c in a["cards"]] != [c["rect"] for c in b["cards"]]:
        diff.append("карточки")
    if [c["font"] for c in a["cards"]] != [c["font"] for c in b["cards"]]:
        diff.append("кегли")
    if a["zoom"] != b["zoom"]:
        diff.append("масштаб")
    return diff


def write_report(rows: list[dict], out: Path, event: dict) -> None:
    """Отчёт в HTML: таблица и ссылки на скриншоты."""
    formats = [f[2] for f in FORMATS if any(r["fmt"] == f[2] for r in rows)]
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
        img = f'<a href="{r["fmt"]}/{r["name"]}.png">скрин</a>' \
              f' · <a href="{r["fmt"]}/{r["name"]}-results.png">итог</a>'
        txt = f"{r['questionFont']:.0f}/{r['answerFont']:.0f}"
        note = " · ".join(bad) if bad else "ок"
        return (f'<td class="{cls}"><b>{note}</b><br>{img}<br>'
                f'кегли {txt} · карточка {r["cardWidth"]} px</td>')

    bad_count = sum(1 for r in rows if r.get("problems"))
    html = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>Стенд раскладки квиза</title>",
        "<style>",
        "body{font:15px/1.5 system-ui,sans-serif;background:#0f1320;color:#e8ecf8;"
        "margin:24px;max-width:1500px}",
        "h1{font-size:22px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #2a3350;padding:8px 10px;vertical-align:top;"
        "text-align:left}th{background:#182036}",
        "td.ok{background:#10271c}td.bad{background:#2b1418}",
        "a{color:#8ab4ff}.n{width:260px}",
        "</style>",
        f"<h1>Стенд раскладки: {len(rows)} кадров, "
        f"с замечаниями {bad_count}</h1>",
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
    ap.add_argument("--formats", help="холсты: 16x9,16x10,5x3,4x3,1280x720")
    ap.add_argument("--out", default="layout-stand", help="папка результатов")
    ap.add_argument("--rebuild", action="store_true",
                    help="пересоздать событие (иначе используется готовое)")
    args = ap.parse_args()

    only = [int(x) for x in args.only.split(",")] if args.only else []
    wanted = {x.strip() for x in args.formats.split(",")} if args.formats else None
    formats = [f for f in FORMATS if not wanted or f[2] in wanted]

    scenarios = full_matrix() + small_set()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Готовлю событие: {len(scenarios)} вопросов…")
    event = build_event(scenarios)
    print(f"\nСобытие: {event['slug']}, вопросов: {len(event['ids'])}")
    print(f"Экран: {event['links'].get('screen')}\n")

    print("Прогон:")
    rows = run_stand(event, scenarios, formats, only, out)
    write_report(rows, out, event)

    bad = [r for r in rows if r.get("problems")]
    print(f"\n{'=' * 60}")
    print(f"Всего кадров: {len(rows)}, с замечаниями: {len(bad)}")
    for r in bad:
        print(f"  {r['fmt']:<9} {r['n']:>2}. {r['label'][:40]}: "
              + "; ".join(r["problems"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
