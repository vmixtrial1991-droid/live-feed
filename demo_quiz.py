"""Боевой тест: демо-событие со всеми режимами квиза сразу.

Создаёт событие «Квиз на пример» с вопросами всех видов:
  * текстовые — короткий, длинный, со ссылкой без пробелов, опрос без очков;
  * с картинкой — 16:9, 4:3, 1:1, 3:4, 9:16 (проверка раскладки);
  * с отсчётом и без, зачётные и «просто интересно».

Печатает адреса. Событие остаётся на диске: гоняйте сколько нужно.

    python demo_quiz.py                  # сервер на 127.0.0.1:8123
    python demo_quiz.py --port 9000
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

MOD_PASSWORD = "kviz-proba-1"
TITLE = "Квиз на пример"
SUBTITLE = "Новогодний вечер · 19:00 · конференц-зал"
QUIZ_TITLE = "Новогодний квиз «Ёлка 2027»"

# текст, варианты, верный, зачёт, секунды, (ширина, высота) картинки или None
QUESTIONS = [
    ("Сколько будет 7 × 8?", ["48", "56"], 1, 1, 0, None),
    ("Столица Австралии?", ["Сидней", "Канберра", "Мельбурн", "Перт"], 1, 1, 15, None),
    # картинка 16:9 — фото во всю ширину под вопросом
    ("Какая фигура нарисована?", ["Круг", "Квадрат", "Треугольник", "Звезда"],
     0, 1, 20, (1600, 900)),
    # картинка 4:3 — та же раскладка, фото выше
    ("Что на этом снимке?", ["Круг", "Ромб", "Овал", "Квадрат"],
     0, 1, 20, (1600, 1200)),
    # квадрат — фото крупно справа
    ("Сколько сторон у фигуры?", ["Одна", "Две", "Ни одной", "Много"],
     2, 1, 15, (1200, 1200)),
    # вертикаль 3:4 — фото справа во всю высоту
    ("Фигура симметрична?", ["Да", "Нет", "Только сверху", "Только снизу"],
     0, 1, 15, (900, 1200)),
    # вертикаль 9:16 — самое высокое фото
    ("Это круг или овал?", ["Круг", "Овал", "Квадрат", "Линия"],
     0, 1, 20, (720, 1280)),
    # «просто интересно», с картинкой: правильного нет
    ("На что это похоже больше?", ["На солнце", "На луну", "На мяч"],
     -1, 0, 0, (1600, 900)),
    # длинный текст без картинки — проверка сжатия
    ("Что произойдёт с фотографией, которую гость пришлёт в ленту с "
     "телефона, если в файле записаны координаты съёмки и модель камеры, а "
     "ведущий потом поставит этот снимок в слайдшоу на проекторе?",
     ["Все данные останутся в файле, и координаты сможет прочитать любой желающий",
      "Снимок ужмётся до 1600 пикселей, а координаты и модель камеры будут стёрты",
      "Файл сохранится как есть, но координаты спрячутся в отдельной папке архива",
      "Ничего не изменится: сервер хранит оригинал, пока ведущий не удалит событие"],
     1, 1, 30, None),
    # опрос без очков
    ("Какой напиток в этом зале любимее?", ["Чай", "Кофе", "Вода"], -1, 0, 0, None),
    # два длинных варианта
    ("Что для гостя важнее всего на таком вечере?",
     ["Увидеть себя и своих друзей на большом экране и забрать фотографии потом",
      "Просто хорошо провести время и не думать, что где-то нужно успеть ответить"],
     0, 1, 0, None),
    # неразрывная строка
    ("Какая строка похожа на адрес страницы квиза?",
     ["https://example.com/e/novogodniy-kviz-dlya-otdela-prodazh/quiz?t=KKTB6RQW",
      "/e/novogodniy-kviz/quiz?t=KKTB6RQW",
      "квиз.пример/игра",
      "просто «Квиз»"],
     1, 1, 20, None),
]


def _figure(w: int, h: int, path: Path) -> Path:
    """Рисуем картинку нужных пропорций: в репозитории не держим бинарники."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (22, 30, 56))
    d = ImageDraw.Draw(img)
    step = max(40, min(w, h) // 12)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(40, 54, 96), width=max(2, step // 20))
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(40, 54, 96), width=max(2, step // 20))
    r = min(w, h) // 3
    d.ellipse([w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r], fill=(120, 170, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path


def _check_limits() -> None:
    for text, opts, correct, scored, _sec, shot in QUESTIONS:
        text_limit = 90 if shot else 200
        opt_limit = 40 if shot else 80
        assert len(text) <= text_limit, f"вопрос {len(text)} > {text_limit}: {text[:30]}"
        assert 2 <= len(opts) <= 4, f"вариантов {len(opts)}"
        for o in opts:
            assert len(o) <= opt_limit, f"вариант {len(o)} > {opt_limit}"
        if scored:
            assert 0 <= correct < len(opts), "не отмечен правильный"


def main() -> int:
    ap = argparse.ArgumentParser(description="завести демо-событие с квизом")
    ap.add_argument("--port", type=int, help="порт вместо того, что в LF_TEST_ROOT")
    args = ap.parse_args()
    root = ROOT if not args.port else f"http://127.0.0.1:{args.port}"
    _check_limits()
    tmp = Path(os.environ.get("TEMP", ".")) / "opencode"

    st, listing = admin_get("/api/admin/events")
    for e in (listing.get("events") or []):
        if e.get("title") == TITLE and e.get("slug"):
            remove(e["slug"])
            print(f"  убрано прежнее демо: {e['slug']}")

    st, created = admin_post("/api/admin/events",
                             {"title": TITLE, "password": MOD_PASSWORD,
                              "subtitle": SUBTITLE})
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

    for n, (text, opts, correct, scored, seconds, shot) in enumerate(QUESTIONS, 1):
        fields = {
            "text": text, "options": json.dumps(opts, ensure_ascii=False),
            "correct": str(correct), "scored": str(scored),
            "seconds": str(seconds),
        }
        if shot:
            img = _figure(shot[0], shot[1], tmp / f"demo-{shot[0]}x{shot[1]}.jpg")
            st, _ = call_file(f"{base}/api/moderator/quiz/add", fields, img)
            mark_shot = f" · фото {shot[0]}×{shot[1]}"
        else:
            st, _ = call(f"{base}/api/moderator/quiz/add", fields)
            mark_shot = ""
        mark = "OK " if st == 200 else "FAIL"
        timer = f" · {seconds} с" if seconds else ""
        kind = "опрос" if not scored else "зачёт"
        print(f"  {mark} {n:>2}. {text[:46]}{'…' if len(text) > 46 else ''}"
              f"  [{kind}, {len(opts)} вар.{timer}{mark_shot}]")

    print("\n=== Адреса для внешнего браузера ===")
    for label, key in (("гости (лента + игра)", "join"),
                       ("игра отдельно", "quiz"),
                       ("экран/проектор", "screen"),
                       ("кабинет ведущего", "moderator")):
        print(f"  {label:<24} {links.get(key, '—')}")
    print(f"\n  пароль ведущего: {MOD_PASSWORD}")
    print(f"  папка события:   {os.path.join('data', 'events', slug)}")
    print("\nСценарий прогона: войти в кабинет → вкладка «Квиз» → «Запустить квиз».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
