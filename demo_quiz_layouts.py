"""Тестовый квиз под новую раскладку: все схемы, все пределы длины.

Заводит событие «Раскладки квиза», в котором вопросы подобраны так, чтобы
проверить каждый случай ТЗ «Вёрстка экрана квиза»:

  A — без картинки: 2, 3 и 4 ответа, короткий и длинный вопрос, короткие,
      средние и почти предельные ответы, «хитрый» вопрос без очков;
  B — горизонтальная картинка: 16:9, 4:3, панорама 21:9, снимок с текстом
      внутри, совсем маленький снимок (предел увеличения 1.5×);
  C — квадрат и вертикаль: 1:1, 3:4, 9:16, вытянутая 1:2.

Внутри каждой схемы есть вопросы с длинным и коротким текстом: от этого
зависит кегль, а на четырёх ответах с картинкой он ещё и на 4 px меньше.

Пределы длины проверяются той же таблицей, что и на сервере
(app/quiz.py, раздел 12 ТЗ): вопрос до 350 знаков без картинки и до 240
с ней, ответ — до 120 при двух-трёх вариантах и до 100 при четырёх.

Картинки рисуются на месте, бинарники в репозитории не держим. Событие
остаётся на диске: гонять можно сколько нужно.

    python demo_quiz_layouts.py              # завести и напечатать ссылки
    python demo_quiz_layouts.py --dry-run    # только проверить тексты
    python demo_quiz_layouts.py --verify     # ещё и снять раскладку кадров
"""

from __future__ import annotations

import argparse
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

from testlib import ROOT, admin_get, admin_post, remove

sys.path.insert(0, str(Path(__file__).parent))
from app import quiz as quizmod

MOD_PASSWORD = "raskladki-proverka-1"
TITLE = "Раскладки квиза"
QUIZ_TITLE = "Квиз: проверка раскладок"

# ---------------------------------------------------------------- вопросы
# (метка, текст, варианты, верный, зачёт, секунды, картинка (w, h) или None,
#  что проверяем)
QUESTIONS: list[tuple] = [
    # ---------------- A: без картинки ----------------
    ("A, 2 коротких", "Сколько будет 7 × 8?", ["48", "56"], 1, 1, 0, None,
     "две колонки, крупный кегль"),
    ("A, 2 длинных", "Где заканчивается та зона кадра, которую проектор может обрезать?",
     ["По краям: поля в пять процентов с каждой стороны уже отведены под обрезку "
      "и кривизну оптики проектора",
      "Только по нижнему краю: остальные три стороны проекторы показывают целиком"],
     0, 1, 15, None, "две колонки, ответы в две строки"),
    ("A, 3 коротких", "Что важнее на большом экране?",
     ["Читаемость", "Красота", "Скорость"], 0, 1, 10, None,
     "колонка из трёх"),
    ("A, 4 коротких", "Какая фигура нарисована в центре?",
     ["Круг", "Квадрат", "Ромб", "Овал"], 1, 1, 10, None,
     "сетка 2×2, компактные карточки"),
    ("A, 4 разных", "Как поступит экран, если ответ не влезает в карточку?",
     ["Сожмёт шрифт",
      "Перенесёт слова на вторую строку и выровняет весь ряд по самой высокой карточке",
      "Обрежет",
      "Ничего не сделает"], 1, 1, 15, None,
     "спорный случай: один ответ много длиннее остальных"),
    ("A, 4 на пределе", "Какие из этих утверждений про раскладку верны при четырёх вариантах?",
     ["Сетка два на два делит ширину кадра пополам, поэтому колонке достаётся вдвое меньше места",
      "Кегль ответа не может быть крупнее девяти десятых кегля вопроса ни при каких условиях",
      "Место под процент зарезервировано всегда, поэтому при показе результатов кадр не меняется",
      "Зона процента отдельная колонка сетки, а не слой поверх текста, поэтому наехать она не может"],
     2, 1, 20, None, "четыре ответа почти по 100 знаков"),
    ("A, длинный вопрос", "Короткий ответ на длинный вопрос",
     ["Да", "Нет"], 0, 1, 0, None, "вопрос около 300 знаков, ответы короткие"),
    ("A, вопрос на пределе", "Самый длинный вопрос без картинки",
     ["Первый", "Второй", "Третий"], 2, 1, 0, None,
     "вопрос на жёстком пределе 350 знаков"),
    ("A, слово без пробелов", "Какая строка похожа на адрес страницы квиза?",
     ["https://example.com/e/novogodniy-kviz-dlya-otdela-prodazh/quiz?t=KKTB6RQW",
      "/e/novogodniy-kviz/quiz?t=KKTB6RQW",
      "квиз.пример/игра"], 1, 1, 20, None,
     "длинное слово без пробелов не должно вылезти за карточку"),
    ("A, опрос без очков", "Какой напиток в этом зале любимее?",
     ["Чай", "Кофе", "Вода"], -1, 0, 0, None,
     "распределение без правильного ответа"),

    # ---------------- B: горизонтальная картинка ----------------
    ("B, 16:9, 2 ответа", "Какая фигура в центре снимка?",
     ["Круг", "Квадрат"], 0, 1, 15, (1600, 900),
     "фото сверху, две карточки в один ряд"),
    ("B, 16:9, 3 ответа", "Что нарисовано на этом снимке?",
     ["Круг", "Квадрат", "Ромб"], 0, 1, 15, (1600, 900),
     "фото сверху, ответы колонкой"),
    ("B, 16:9, 4 длинных", "Как поведёт себя кадр, если под горизонтальным снимком четыре длинных ответа?",
     ["Ответы ужмутся в кегле до минимума и всё равно останутся в кадре",
      "Снимок уменьшится, чтобы освободить высоту под карточки с текстом",
      "Вопрос обрежется многоточием, а ответы останутся прежними",
      "Экран перестроится в две колонки, как с вертикальным снимком"],
     1, 1, 20, (1600, 900), "четыре ответа и горизонтальный снимок"),
    ("B, 4:3, 4 ответа", "Что показывает снимок четыре на три?",
     ["Такой же круг, но кадр выше", "Квадрат", "Ромб", "Овал"],
     0, 1, 15, (1600, 1200), "4:3 остаётся схемой B"),
    ("B, панорама", "На что похожа панорама?",
     ["На широкую полосу", "На квадрат"], 0, 1, 10, (2100, 900),
     "панорама 21:9: ширина до ста процентов области"),
    ("B, текст на снимке", "Что написано на табличке?",
     ["ВХОД", "ВЫХОД"], 0, 1, 10, (1600, 900),
     "важный текст внутри снимка не должен обрезаться"),
    ("B, маленький снимок", "Совсем маленький исходник: до какого размера его увеличат?",
     ["До полуторакратного", "До трёхкратного"], 0, 1, 10, (400, 300),
     "предел увеличения 1.5× — иначе картинка мылится"),

    # ---------------- C: квадрат и вертикаль ----------------
    ("C, 1:1, 2 ответа", "Сколько сторон у фигуры на квадратном снимке?",
     ["Ни одной", "Четыре"], 0, 1, 10, (1200, 1200),
     "квадрат на 16:9 — две колонки, снимок слева"),
    ("C, 1:1, 4 длинных", "Как встанет кадр с квадратным снимком и четырьмя длинными ответами?",
     ["Снимок слева во всю высоту, справа вопрос и ответы одной колонкой",
      "Снимок сверху во всю ширину, ответы под ним сеткой два на два",
      "Снимок по центру, ответы вокруг него в два столбца",
      "Снимок уйдёт в фон, а текст займёт весь кадр"],
     0, 1, 20, (1200, 1200),
     "четыре ответа с картинкой: кегль ответа минус 4 px"),
    ("C, 3:4, 3 ответа", "Снимок три на четыре: что с раскладкой?",
     ["Снимок слева, ответы колонкой", "Снимок снизу", "Снимок справа"],
     0, 1, 15, (900, 1200), "вертикаль 3:4"),
    ("C, 9:16, 4 коротких", "Это круг или овал?",
     ["Круг", "Овал", "Квадрат", "Линия"], 0, 1, 15, (720, 1280),
     "самый высокий снимок: полоса уже, справа шире"),
    ("C, 1:2, 2 ответа", "Очень вытянутый снимок: где ему место?",
     ["Слева, во всю высоту", "Сверху, во всю ширину"], 0, 1, 10, (600, 1200),
     "снимок вдвое выше своей ширины"),
]


def _fill(text: str, width: int) -> str:
    """Дотянуть текст до нужной длины словами, не выходя за неё.

    Нужно, чтобы вопросы «на пределе» действительно были у пределов, а не
    «примерно длинными»: иначе стенд проверяет не ту границу.
    """
    words = [
        "раскладка", "экран", "карточка", "вопрос", "ответ", "кадр",
        "высота", "ширина", "сетка", "колонка", "кегль", "строки",
    ]
    out = text
    i = 0
    while True:
        word = words[i % len(words)]
        candidate = f"{out} {word}"
        if len(candidate) > width:
            break
        out = candidate
        i += 1
    return out


def build_questions() -> list[dict]:
    """Развернуть список в готовые вопросы с точными текстами."""
    out = []
    for label, text, options, correct, scored, seconds, shot, note in QUESTIONS:
        out.append({
            "label": label, "text": text, "options": list(options),
            "correct": correct, "scored": scored, "seconds": seconds,
            "shot": shot, "note": note,
        })

    # Длинный вопрос и вопрос на пределе заполняем до нужной длины: в списке
    # выше стоят только заголовки, иначе таблицу вопросов не прочитать.
    for q in out:
        if q["label"] == "A, длинный вопрос":
            q["text"] = _fill(
                "Как раскладка решает, кому отдать высоту кадра, если "
                "вопрос оказался длинным, а ответы короткими:", 300)
        if q["label"] == "A, вопрос на пределе":
            q["text"] = _fill(
                "Перед вами вопрос на жёстком пределе длины: он нужен, чтобы "
                "проверить, что подгонка уменьшает кегль до минимума и всё "
                "равно укладывает текст в область контента, не обрезая его "
                "многоточием и не вылезая за нижнюю границу кадра:", 350)
    return out


def expected_layout(shot, canvas_ratio: float = 16 / 9) -> str:
    """Схема кадра по разделу 4 ТЗ — то, что должен показать экран."""
    if not shot:
        return "A"
    w, h = shot
    r = w / h
    if r >= 1.2:
        return "B"
    if r <= 0.83:
        return "C"
    return "C" if canvas_ratio >= 1.55 else "B"


def validate(questions: list[dict]) -> list[str]:
    """Сверить вопросы с таблицей пределов сервера. Возвращает ошибки."""
    problems = []
    for q in questions:
        has_img = bool(q["shot"])
        ql = quizmod.question_limits(has_img)
        ol = quizmod.option_limits(has_img, len(q["options"]))
        if len(q["text"]) > ql["hard"]:
            problems.append(
                f'{q["label"]}: вопрос {len(q["text"])} > {ql["hard"]}')
        if not (2 <= len(q["options"]) <= 4):
            problems.append(
                f'{q["label"]}: вариантов {len(q["options"])}')
        for o in q["options"]:
            if len(o) > ol["hard"]:
                problems.append(
                    f'{q["label"]}: ответ {len(o)} > {ol["hard"]}')
        if q["scored"] and not (0 <= q["correct"] < len(q["options"])):
            problems.append(f'{q["label"]}: не отмечен правильный')
    return problems


def _figure(w: int, h: int, path: Path, caption: str = "") -> Path:
    """Снимок нужных пропорций с сеткой и кругом: видно границу кадра."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (w, h), (22, 30, 56))
    d = ImageDraw.Draw(img)
    step = max(20, min(w, h) // 10)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(40, 54, 96), width=max(1, step // 24))
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(40, 54, 96), width=max(1, step // 24))
    inset = max(2, min(w, h) // 60)
    d.rectangle([inset, inset, w - inset, h - inset],
                outline=(120, 170, 255), width=max(1, min(w, h) // 150))
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

    text = caption or f"{w}×{h}"
    f = font(max(12, min(w, h) // 12))
    box = d.textbbox((0, 0), text, font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad = max(4, th // 3)
    # Подпись сверху слева, как у настоящей таблички: по ней видно, что
    # снимок не обрезан ни с одного края.
    tx, ty = inset + pad, inset + pad
    d.rectangle([tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
                fill=(12, 16, 30))
    d.text((tx, ty), text, font=f, fill=(235, 240, 255))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="тестовый квиз раскладок")
    ap.add_argument("--port", type=int, help="порт вместо того, что в LF_TEST_ROOT")
    ap.add_argument("--dry-run", action="store_true",
                    help="только проверить тексты, событие не заводить")
    ap.add_argument("--verify", action="store_true",
                    help="снять раскладку каждого кадра в headless Chrome")
    args = ap.parse_args()

    root = ROOT if not args.port else f"http://127.0.0.1:{args.port}"
    questions = build_questions()
    problems = validate(questions)
    if problems:
        print("Вопросы не проходят проверку:")
        for p in problems:
            print("  -", p)
        return 1

    print(f"Вопросов: {len(questions)}")
    for q in questions:
        has_img = bool(q["shot"])
        ql = quizmod.question_limits(has_img)
        ol = quizmod.option_limits(has_img, len(q["options"]))
        shot = f"{q['shot'][0]}×{q['shot'][1]}" if q["shot"] else "нет"
        print(f"  {q['label']:<22} {expect_layout_str(q, has_img, ql, ol)}"
              f"  вопрос {len(q['text']):>3}/{ql['hard']}"
              f"  ответ ≤{max(len(o) for o in q['options']):>3}/{ol['hard']}"
              f"  фото {shot:<10} схема {expected_layout(q['shot'])}")

    if args.dry_run:
        print("\nПробный прогон: событие не заводилось.")
        return 0

    tmp = Path(os.environ.get("TEMP", ".")) / "opencode" / "demo-layouts"

    # Прежнее событие с тем же названием убираем: иначе вопросы задвоятся.
    _, listing = admin_get("/api/admin/events")
    for e in (listing.get("events") or []):
        if e.get("title") == TITLE and e.get("slug"):
            remove(e["slug"])
            print(f"  убрано прежнее событие: {e['slug']}")

    st, created = admin_post("/api/admin/events",
                             {"title": TITLE, "password": MOD_PASSWORD})
    if st != 200 or not created.get("event"):
        print(f"событие не завелось: {st} {created}")
        return 1
    event = created["event"]
    slug = event["slug"]
    links = created.get("links") or {}
    if not links:
        st, card = admin_post(f"/api/admin/events/{slug}")
        links = card.get("links") or {}

    base = f"/e/{slug}"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

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

    def call(path: str, fields: dict):
        req = urllib.request.Request(
            root + path, data=urllib.parse.urlencode(fields).encode(),
            method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        return _send(req)

    def call_file(path: str, fields: dict, file_path: Path):
        import uuid

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
        print(f"ведущий не вошёл: {st}")
        return 1
    call(f"{base}/api/moderator/quiz/title", {"title": QUIZ_TITLE})

    print("\nЗаведение вопросов:")
    ids = []
    for n, q in enumerate(questions, 1):
        fields = {
            "text": q["text"],
            "options": json.dumps(q["options"], ensure_ascii=False),
            "correct": str(q["correct"]),
            "scored": str(q["scored"]),
            "seconds": str(q["seconds"]),
        }
        if q["shot"]:
            w, h = q["shot"]
            caption = "ВХОД" if q["label"].endswith("текст на снимке") else ""
            img = _figure(w, h, tmp / f"{n:02d}-{w}x{h}.jpg", caption)
            st, body = call_file(f"{base}/api/moderator/quiz/add", fields, img)
        else:
            st, body = call(f"{base}/api/moderator/quiz/add", fields)
        qid = (body.get("question") or {}).get("id")
        ids.append(qid)
        mark = "OK  " if st == 200 else "FAIL"
        print(f"  {mark} {n:>2}. {q['label']:<22} {q['note']}")
        if st != 200:
            print(f"        {st} {body}")

    print("\n=== Ссылки ===")
    for label, key in (("гости (лента и игра)", "join"),
                       ("игра на телефоне", "quiz"),
                       ("экран / проектор", "screen"),
                       ("кабинет ведущего", "moderator")):
        print(f"  {label:<24} {links.get(key, '—')}")
    print(f"\n  пароль ведущего: {MOD_PASSWORD}")
    print(f"  код приглашения: {event.get('code', '—')}")
    print(f"  папка события:   data/events/{slug}")
    print("\nПорядок: войти в кабинет → вкладка «Квиз» → «Запустить квиз» →"
          "\nшаговая кнопка ведёт по вопросам. Схема каждого кадра —"
          "\nв таблице выше; на 4:3 квадратный снимок даёт B, а не C.")

    if args.verify:
        verify_layouts(links.get("screen"), questions, ids, root)

    return 0


def expect_layout_str(q: dict, has_img: bool, ql: dict, ol: dict) -> str:
    """Короткая пометка, влезает ли текст в рекомендуемый предел."""
    q_long = len(q["text"]) > ql["recommended"]
    o_long = any(len(o) > ol["recommended"] for o in q["options"])
    marks = []
    if q_long:
        marks.append("вопрос>рек.")
    if o_long:
        marks.append("ответ>рек.")
    return ", ".join(marks) or "в норме"


def verify_layouts(screen_url: str | None, questions: list[dict],
                   ids: list[int], root: str) -> None:
    """Снять раскладку каждого кадра в headless Chrome и сверить с планом.

    Пользователь всё равно будет смотреть сам, но если экран выбрал не ту
    схему, лучше узнать это до него: молча неверная раскладка выглядит как
    «вёрстка поехала».
    """
    if not screen_url:
        print("\nПроверка раскладки: ссылки на экран нет, пропускаю.")
        return
    try:
        from sweep_quiz import CDP, find_chrome, free_port, wait_json
    except Exception as exc:                              # noqa: BLE001
        print(f"\nПроверка раскладки недоступна: {exc}")
        return

    slug = screen_url.split("/e/")[1].split("/")[0]
    base = f"/e/{slug}"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(path: str, fields: dict):
        req = urllib.request.Request(
            root + path, data=urllib.parse.urlencode(fields).encode(),
            method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")

    post(f"{base}/api/moderator/login", {"password": MOD_PASSWORD})

    url = screen_url if screen_url.startswith("http") else root + screen_url
    if url.startswith("http://127.0.0.1") and root.startswith("http://"):
        url = url  # ссылка уже абсолютная
    port = free_port()
    profile = Path(os.environ.get("TEMP", ".")) / "opencode" / "chrome-demo"
    proc = subprocess.Popen([
        find_chrome(), "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
        "--hide-scrollbars", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("\n=== Проверка раскладки на 16:9 (1920×1080) ===")
    try:
        wait_json(port, "/json/version")
        page = next(t for t in wait_json(port, "/json/list")
                    if t.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", width=1920, height=1080,
                 deviceScaleFactor=1, mobile=False)

        bad = 0
        for q, qid in zip(questions, ids):
            if not qid:
                continue
            post(f"{base}/api/moderator/quiz/control",
                 {"action": "open", "question_id": str(qid)})
            cdp.send("Page.navigate", url=url)
            time.sleep(1.3)
            raw = cdp.js(
                "(() => { const p = (typeof quizPlayLayout !== 'undefined')"
                " ? quizPlayLayout.plan : null;"
                " if (!p) return null;"
                " return JSON.stringify({layout: p.layout, kind: p.mode,"
                "  warn: p.warnings,"
                "  q: Math.round(p.question.font),"
                "  a: Math.round(p.answers.font),"
                "  cols: p.answers.cols,"
                "  need: Math.round(p.totalHeight),"
                "  area: Math.round(p.area.height)}); })()")
            if not raw:
                print(f"  —    {q['label']:<22} план не посчитался")
                bad += 1
                continue
            got = json.loads(raw)
            want = expected_layout(q["shot"])
            ok = got["layout"] == want and got["need"] <= got["area"] + 1
            if not ok:
                bad += 1
            mark = "OK  " if ok else "FAIL"
            extra = ""
            if got["layout"] != want:
                extra = f"  схема {got['layout']}, ждали {want}"
            elif got["need"] > got["area"] + 1:
                extra = f"  не влезло {got['need']} > {got['area']}"
            if got["warn"]:
                extra += "  " + "; ".join(got["warn"])
            print(f"  {mark} {q['label']:<22} схема {got['layout']}"
                  f"  кегли {got['q']}/{got['a']}  колонок {got['cols']}"
                  f"  {got['need']}/{got['area']}{extra}")
        print(f"\nРасхождений: {bad} из {len(questions)}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:                                  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
