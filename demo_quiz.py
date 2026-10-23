"""Демо-событие с готовым квизом, чтобы прокликать игру руками.

    python demo_quiz.py            # против сервера на 127.0.0.1:8123
    python demo_quiz.py --port 9000

Событие остаётся на диске (тестовые проверки так не делают): его можно
удалять и заводить заново, пока игра не станет привычной. Скрипт печатает
четыре адреса события и пароль ведущего.

Вопросы нарочно разные: короткие и на двести знаков, с двумя и четырьмя
вариантами, по одному слову и по восемьдесят символов, с отсчётом и без,
зачётные и «просто интересно». Так на одном прогоне видно, как экран
справляется с любым содержимым, а не только с аккуратными строками.
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
SUBTITLE = "Новогодний вечер · 19:00 · конференц-зал"
QUIZ_TITLE = "Новогодний квиз «Ёлка 2027»"

QUESTIONS = [
    # текст, варианты, верный, зачёт, время на ответ (0 — без отсчёта)
    # 1. самое короткое из возможного: два варианта, оба в два знака
    ("Сколько будет 7 × 8?", ["48", "56"], 1, 1, 0),

    # 2. короткий вопрос, четыре коротких варианта, отсчёт
    ("Столица Австралии?", ["Сидней", "Канберра", "Мельбурн", "Перт"], 1, 1, 15),

    # 3. длинный вопрос и четыре длинных варианта — проверка сжатия
    ("Что произойдёт с фотографией, которую гость пришлёт в ленту с телефона, "
     "если в файле записаны координаты съёмки и модель камеры, а ведущий потом "
     "поставит этот снимок в слайдшоу на проекторе?",
     ["Все данные останутся в файле, и координаты сможет прочитать любой желающий",
      "Снимок ужмётся до 1600 пикселей, а координаты и модель камеры будут стёрты",
      "Файл сохранится как есть, но координаты спрячутся в отдельной папке архива",
      "Ничего не изменится: сервер хранит оригинал, пока ведущий не удалит событие"],
     1, 1, 30),

    # 4. «просто интересно»: правильного нет, вариантов три, без отсчёта
    ("Какой напиток в этом зале любимее?", ["Чай", "Кофе", "Вода"], -1, 0, 0),

    # 5. два очень длинных варианта: экран должен ужать их, а не обрезать
    ("Что для гостя важнее всего на таком вечере, если выбирать только одно?",
     ["Увидеть себя и своих друзей на большом экране и забрать фотографии потом",
      "Просто хорошо провести время и не думать, что где-то нужно успеть ответить"],
     0, 1, 0),

    # 6. одно слово без пробелов на всю ширину варианта: проверка переноса
    ("Какая строка похожа на адрес страницы квиза?",
     ["https://example.com/e/novogodniy-kviz-dlya-otdela-prodazh/quiz?t=KKTB6RQW",
      "/e/novogodniy-kviz/quiz?t=KKTB6RQW",
      "квиз.пример/игра",
      "просто «Квиз»"],
     1, 1, 20),

    # 7. вопрос почти на двести знаков, и первый вариант тоже длинный
    ("Представьте: ведущий открыл вопрос, зал уже отвечает, а на проекторе кто-то "
     "случайно нажал Escape и вышел из полноэкранного режима. Что увидит зал, пока "
     "экран возвращается на место?",
     ["Окно станет обычным, вопрос останется на экране, ведущий вернёт полный экран",
      "Игра встанет на паузу, и тот же вопрос придётся открыть заново",
      "Экран вернётся к ленте, а ответы зала за это время потеряются",
      "Страница перезагрузится и сразу покажет финальную таблицу"],
     0, 1, 60),

    # 8. короткий опрос с отсчётом: четыре варианта, без очков
    ("Куда поедем следующим корпоративом?",
     ["В горы", "На море", "В загородный отель", "Останемся в офисе"], -1, 0, 20),
]


def _check_limits() -> None:
    """Ловит опечатку в демо до того, как её увидит зал на проекторе."""
    for text, opts, correct, scored, seconds in QUESTIONS:
        assert len(text) <= 200, f"вопрос длиннее 200: {len(text)}"
        assert 2 <= len(opts) <= 4, f"вариантов {len(opts)}"
        for o in opts:
            assert len(o) <= 80, f"вариант длиннее 80: {len(o)}"
        if scored:
            assert 0 <= correct < len(opts), "не отмечен правильный вариант"


def main() -> int:
    ap = argparse.ArgumentParser(description="завести демо-событие с квизом")
    ap.add_argument("--port", type=int, help="порт вместо того, что в LF_TEST_ROOT")
    args = ap.parse_args()
    root = ROOT if not args.port else f"http://127.0.0.1:{args.port}"
    _check_limits()

    # демо за демоном: прежнее демо-событие убираем, чтобы список админки
    # не обрастал одинаковыми «Квиз на пример» с ответами прошлых прогонов
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

    # название игры: оно стоит на холле над QR, отдельно от названия события
    st, _ = call(f"{base}/api/moderator/quiz/title", {"title": QUIZ_TITLE})
    print(f"  {'OK ' if st == 200 else 'FAIL'} название игры: {QUIZ_TITLE}")

    for n, (text, opts, correct, scored, seconds) in enumerate(QUESTIONS, 1):
        st, _ = call(f"{base}/api/moderator/quiz/add", {
            "text": text, "options": json.dumps(opts, ensure_ascii=False),
            "correct": str(correct), "scored": str(scored),
            "seconds": str(seconds),
        })
        mark = "OK " if st == 200 else "FAIL"
        timer = f", отсчёт {seconds} c" if seconds else ""
        kinds = len(opts)
        print(f"  {mark} {n}. {text[:44]}{'…' if len(text) > 44 else ''} "
              f"({kinds} вар., {len(text)} зн.{timer})")

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
