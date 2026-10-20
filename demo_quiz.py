"""Демо-событие с готовым квизом, чтобы прокликать игру руками.

    python demo_quiz.py            # против сервера на 127.0.0.1:8123
    python demo_quiz.py --port 9000

Событие остаётся на диске (тестовые проверки так не делают): его можно
удалять и заводить заново, пока игра не станет привычной. Скрипт печатает
четыре адреса события и пароль ведущего.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from testlib import ROOT, admin_get, admin_post, remove

MOD_PASSWORD = "kviz-proba-1"
TITLE = "Квиз на пример"

QUESTIONS = [
    # текст, варианты, верный, зачёт, время на ответ (0 — без отсчёта)
    ("Сколько будет 7 × 8?", ["56", "54", "48", "64"], 0, 1, 30),
    ("Что гость может прислать в ленту?",
     ["Только текст", "Текст или фото", "Только фото", "Ничего"], 1, 1, 0),
    ("Какой напиток в этом зале любимее?",
     ["Чай", "Кофе", "Сок", "Вода"], -1, 0, 0),
    ("Сколько ссылок выдаёт событие?",
     ["Три", "Четыре", "Пять", "Шесть"], 2, 1, 15),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="завести демо-событие с квизом")
    ap.add_argument("--port", type=int, help="порт вместо того, что в LF_TEST_ROOT")
    args = ap.parse_args()
    root = ROOT if not args.port else f"http://127.0.0.1:{args.port}"

    # демо за демоном: прежнее демо-событие убираем, чтобы список админки
    # не обрастал одинаковыми «Квиз на пример» с ответами прошлых прогонов
    st, listing = admin_get("/api/admin/events")
    for e in (listing.get("events") or []):
        if e.get("title") == TITLE and e.get("slug"):
            remove(e["slug"])
            print(f"  убрано прежнее демо: {e['slug']}")

    st, created = admin_post("/api/admin/events",
                             {"title": TITLE, "password": MOD_PASSWORD})
    if st != 200 or not created.get("event"):
        print(f"событие не завелось: {st} {created}")
        return 1
    event = created["event"]
    slug = event["slug"]
    links = created.get("links") or {}
    if not links:
        # создание отдаёт ссылки рядом с событием; карточка — надёжный запасной путь
        st, card = admin_post(f"/api/admin/events/{slug}")
        links = card.get("links") or {}

    base = f"/e/{slug}"
    jar = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    opener = urllib.request.build_opener(jar)

    def call(path: str, fields: dict) -> tuple[int, dict]:
        req = urllib.request.Request(root + path,
                                     data=urllib.parse.urlencode(fields).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(req, timeout=30) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {}

    st, _ = call(f"{base}/api/moderator/login", {"password": MOD_PASSWORD})
    if st != 200:
        print(f"ведущий не вошёл: {st}")
        return 1

    for text, opts, correct, scored, seconds in QUESTIONS:
        st, _ = call(f"{base}/api/moderator/quiz/add", {
            "text": text, "options": json.dumps(opts, ensure_ascii=False),
            "correct": str(correct), "scored": str(scored),
            "seconds": str(seconds),
        })
        mark = "OK " if st == 200 else "FAIL"
        timer = f", отсчёт {seconds} c" if seconds else ""
        print(f"  {mark} вопрос: {text} ({'зачётный' if scored else 'без очков'}{timer})")

    print("\nАдреса демо-события:")
    print(f"  гости (лента + игра)   {links['join']}")
    print(f"  игра отдельно          {links['quiz']}")
    print(f"  экран (проектор)       {links['screen']}")
    print(f"  кабинет ведущего       {links['moderator']}")
    print(f"\nПароль ведущего: {MOD_PASSWORD}")
    print(f"Папка события:   {os.path.join('data', 'events', slug)}")
    print("Удалить событие можно из админки или кнопкой в списке событий.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
