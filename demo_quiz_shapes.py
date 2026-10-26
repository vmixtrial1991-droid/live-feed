"""Тестовое событие: все пропорции картинок и разная длина текстов.

Заводит квиз, в котором по одному вопросу на каждый случай раскладки:

  * 16:9  — широкая картинка (фото сверху, ответы сеткой);
  * 9:16  — вертикаль (фото слева, ответы столбиком);
  * 1:1   — квадрат (та же боковая раскладка);
  * 4:3   — почти широкая, граница переключения раскладок;
  * 3:4   — почти вертикальная, вторая сторона той же границы;
  * без картинки — для сравнения.

В каждом случае есть и короткие, и длинные вопросы, и короткие ответы,
и ответы во всю допустимую длину. Вопросы помечены префиксом вроде
«[16:9]», чтобы при просмотре было видно, какой случай на экране.

Картинки рисуются на месте: бинарники в репозитории не держим.
Событие остаётся на диске, гонять можно сколько нужно.

    python demo_quiz_shapes.py            # сервер на 127.0.0.1:8123
    python demo_quiz_shapes.py --port 9000
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from testlib import ROOT, admin_get, admin_post, remove

MOD_PASSWORD = "proverka-raskladok-1"
TITLE = "Проверка раскладок"
QUIZ_TITLE = "Раскладки: 16:9, 9:16, 1:1, 4:3, 3:4"

# Пределы сервера (app/main.py): с картинкой строже, чем без неё.
Q_WITH_SHOT = 90
Q_NO_SHOT = 200
O_WITH_SHOT = 40
O_NO_SHOT = 80

SHOT_16_9 = (1600, 900)
SHOT_9_16 = (900, 1600)
SHOT_1_1 = (1200, 1200)
SHOT_4_3 = (1600, 1200)
SHOT_3_4 = (1200, 1600)

# label — что именно проверяем; text — вопрос; options — варианты;
# correct — индекс верного; seconds — отсчёт (0 — без отсчёта);
# shot — (ширина, высота) картинки или None.
QUESTIONS = [
    # --- широкая 16:9: фото сверху, ответы сеткой 2x2 -------------------
    {
        "label": "16:9 · короткий вопрос, 2 коротких ответа",
        "text": "[16:9] Какая фигура?",
        "options": ["Круг", "Квадрат"],
        "correct": 0,
        "seconds": 0,
        "shot": SHOT_16_9,
    },
    {
        "label": "16:9 · 4 ответа, один во всю длину",
        "text": "[16:9] Какой из вариантов описан на картинке полностью и без ошибок?",
        "options": [
            "Самый длинный вариант во всю карточку",
            "Круг",
            "Квадрат",
            "Ещё один",
        ],
        "correct": 0,
        "seconds": 20,
        "shot": SHOT_16_9,
    },
    {
        "label": "16:9 · 3 ответа (третий по центру)",
        "text": "[16:9] Три варианта: как они встанут?",
        "options": ["Первый", "Второй", "Третий"],
        "correct": 1,
        "seconds": 15,
        "shot": SHOT_16_9,
    },
    # --- вертикаль 9:16: фото слева, ответы столбиком -------------------
    {
        "label": "9:16 · короткий вопрос, 4 коротких ответа",
        "text": "[9:16] Кто здесь?",
        "options": ["Вася", "Петя", "Гоша", "Дима"],
        "correct": 2,
        "seconds": 15,
        "shot": SHOT_9_16,
    },
    {
        "label": "9:16 · длинный вопрос, длинные ответы",
        "text": "[9:16] Какой ответ верный, если вопрос длинный и рядом вертикальная картинка?",
        "options": [
            "Первый длинный вариант на всю строку",
            "Второй, тоже длинный",
            "Третий",
            "Четвёртый вариант",
        ],
        "correct": 1,
        "seconds": 25,
        "shot": SHOT_9_16,
    },
    # --- квадрат 1:1: тоже боковая раскладка ----------------------------
    {
        "label": "1:1 · короткий вопрос, 3 ответа",
        "text": "[1:1] Сколько сторон?",
        "options": ["Одна", "Две", "Много"],
        "correct": 2,
        "seconds": 0,
        "shot": SHOT_1_1,
    },
    {
        "label": "1:1 · длинный вопрос, 2 длинных ответа",
        "text": "[1:1] Что произойдёт с ответом, если он не помещается в одну строку?",
        "options": [
            "Переносится на вторую строку",
            "Он обрезается",
        ],
        "correct": 0,
        "seconds": 20,
        "shot": SHOT_1_1,
    },
    # --- 4:3: чуть шире квадрата, широкая раскладка ---------------------
    {
        "label": "4:3 · короткий вопрос, 4 разных ответа",
        "text": "[4:3] Что на снимке?",
        "options": [
            "Круг",
            "Очень длинный вариант ответа",
            "Квадрат",
            "Точка",
        ],
        "correct": 0,
        "seconds": 15,
        "shot": SHOT_4_3,
    },
    # --- 3:4: чуть уже квадрата, боковая раскладка ----------------------
    {
        "label": "3:4 · длинный вопрос, длинные ответы",
        "text": "[3:4] Как выглядит вертикальная картинка три на четыре рядом с длинными ответами?",
        "options": [
            "Ответы стоят столбиком и не рвут слова",
            "Ответы в две колонки и мелкие",
            "Картинка уезжает вниз",
            "Текст обрезан",
        ],
        "correct": 0,
        "seconds": 25,
        "shot": SHOT_3_4,
    },
    # --- без картинки: для сравнения ------------------------------------
    {
        "label": "без фото · длинный вопрос, длинные ответы",
        "text": (
            "[без фото] Этот вопрос намеренно длинный, чтобы проверить, как "
            "сжимается кегль и сколько строк занимает вопрос на экране без "
            "картинки: места здесь заметно больше, чем рядом с фотографией."
        ),
        "options": [
            "Первый вариант почти во всю допустимую длину строки, чтобы проверить перенос",
            "Второй вариант короче, но тоже не помещается в одну строку целиком",
            "Третий",
            "Четвёртый",
        ],
        "correct": 0,
        "seconds": 30,
        "shot": None,
    },
    {
        "label": "без фото · короткий вопрос, короткие ответы",
        "text": "[без фото] Да или нет?",
        "options": ["Да", "Нет"],
        "correct": 0,
        "seconds": 0,
        "shot": None,
    },
]


def _font(px: int):
    """Шрифт покрупнее встроенного: подпись должна читаться на проекторе."""
    from PIL import ImageFont

    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _figure(w: int, h: int, path: Path) -> Path:
    """Рисует картинку нужных пропорций с подписью размера.

    Подпись и рамка нужны, чтобы глазами видеть, где кончается снимок:
    без них чёрная картинка на тёмном фоне сливается с экраном.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (22, 30, 56))
    d = ImageDraw.Draw(img)

    # сетка: по ней видно, как масштабируется снимок
    step = max(40, min(w, h) // 10)
    line = max(2, step // 24)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(40, 54, 96), width=line)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(40, 54, 96), width=line)

    # рамка по краю: граница кадра видна даже на тёмном фоне
    inset = max(4, min(w, h) // 60)
    d.rectangle([inset, inset, w - inset, h - inset],
                outline=(120, 170, 255), width=max(2, min(w, h) // 150))

    # круг в центре — заметный ориентир
    r = min(w, h) // 4
    d.ellipse([w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r],
              fill=(120, 170, 255))

    # подпись с пропорциями и размером
    ratio = round(w / h, 2)
    font = _font(max(24, min(w, h) // 12))
    caption = f"{w}x{h}  ·  {ratio}:1"
    box = d.textbbox((0, 0), caption, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad = max(8, th // 3)
    tx, ty = (w - tw) // 2, (h - th) // 2 - r - th - pad
    d.rectangle([tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
                fill=(12, 16, 30))
    d.text((tx, ty), caption, font=font, fill=(220, 232, 255))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path


def _check_limits() -> None:
    for q in QUESTIONS:
        limit_q = Q_WITH_SHOT if q["shot"] else Q_NO_SHOT
        limit_o = O_WITH_SHOT if q["shot"] else O_NO_SHOT
        assert len(q["text"]) <= limit_q, (
            f"вопрос {len(q['text'])} > {limit_q}: {q['label']}")
        assert 2 <= len(q["options"]) <= 4, f"вариантов {len(q['options'])}"
        for o in q["options"]:
            assert len(o) <= limit_o, (
                f"ответ {len(o)} > {limit_o}: {o[:40]}")
        if q["correct"] >= 0:
            assert 0 <= q["correct"] < len(q["options"]), "не отмечен верный"


def main() -> int:
    ap = argparse.ArgumentParser(description="завести квиз со всеми пропорциями")
    ap.add_argument("--port", type=int, help="порт вместо того, что в LF_TEST_ROOT")
    args = ap.parse_args()
    root = ROOT if not args.port else f"http://127.0.0.1:{args.port}"
    _check_limits()

    tmp = Path(os.environ.get("TEMP", ".")) / "opencode"

    # старое событие с тем же названием убираем: иначе копится мусор
    _, listing = admin_get("/api/admin/events")
    for e in (listing.get("events") or []):
        if e.get("title") == TITLE and e.get("slug"):
            remove(e["slug"])
            print(f"  убрано прежнее: {e['slug']}")

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
    jar = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    opener = urllib.request.build_opener(jar)

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
            root + path, data=urllib.parse.urlencode(fields).encode(), method="POST")
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

    st, _ = call(f"{base}/api/moderator/quiz/title", {"title": QUIZ_TITLE})
    print(f"  {'OK ' if st == 200 else 'FAIL'} название игры: {QUIZ_TITLE}")

    for n, q in enumerate(QUESTIONS, 1):
        fields = {
            "text": q["text"], "options": json.dumps(q["options"], ensure_ascii=False),
            "correct": str(q["correct"]), "scored": "1",
            "seconds": str(q["seconds"]),
        }
        if q["shot"]:
            w, h = q["shot"]
            img = _figure(w, h, tmp / f"shapes-{w}x{h}.jpg")
            st, body = call_file(f"{base}/api/moderator/quiz/add", fields, img)
        else:
            st, body = call(f"{base}/api/moderator/quiz/add", fields)
        mark = "OK " if st == 200 else "FAIL"
        print(f"  {mark} {n:>2}. {q['label']}")
        if st != 200:
            print(f"        {st}: {body}")

    print("\n=== Адреса ===")
    for label, key in (("гости (лента + игра)", "join"),
                       ("игра отдельно", "quiz"),
                       ("экран/проектор", "screen"),
                       ("кабинет ведущего", "moderator")):
        print(f"  {label:<24} {links.get(key, '—')}")
    print(f"\n  пароль ведущего: {MOD_PASSWORD}")
    print(f"  папка события:   {os.path.join('data', 'events', slug)}")
    print(f"\n  вопросов: {len(QUESTIONS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
