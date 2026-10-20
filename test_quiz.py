"""Проверка квиза (5.0): вход тем же билетом, вопросы ведущего, ответы
гостей, стадии игры, экран, QR и восстановление после перезапуска.

Сценарий идёт против поднятого сервера (как остальные наборы):
    python test_quiz.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

from testlib import DATA_DIR, prepare, screen_key

try:  # ws-проверки нужны только там, где есть библиотека (на сервере ставят)
    import websockets
except ImportError:
    websockets = None

CTX = prepare("Проверка квиза")
SLUG = CTX.slug
BASE = CTX.base
TOKEN = CTX.code
PASS = CTX.mod_password
SCREEN_KEY = screen_key(SLUG)
WS = BASE.replace("http", "ws")
CONFIG_PATH = os.path.join(DATA_DIR, "events", SLUG, "config.json")

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        failures.append(name)


def call(method: str, url: str, fields: dict | None = None,
         expect: int | None = None, jar: dict | None = None):
    """Запрос с указанной банкой cookies. jar=None — общая банка COOKIES."""
    jar = COOKIES if jar is None else jar
    if fields is not None:
        req = urllib.request.Request(BASE + url,
                                     data=urlencode(fields).encode(), method=method)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        req = urllib.request.Request(BASE + url, method=method)
    if jar:
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in jar.items()))
    status, raw = None, b""
    try:
        with urllib.request.urlopen(req) as resp:
            raw, status = resp.read(), resp.status
            for h in resp.headers.get_all("Set-Cookie") or []:
                if "=" in h:
                    k, v = h.split("=", 1)
                    jar[k.strip()] = v.split(";")[0]
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
        for h in exc.headers.get_all("Set-Cookie") or []:
            if "=" in h:
                k, v = h.split("=", 1)
                jar[k.strip()] = v.split(";")[0]
    if expect is not None and status != expect:
        check(f"{method} {url}", False, f"статус {status}, ждали {expect}: {raw[:160]!r}")
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, raw


COOKIES: dict[str, str] = {}   # гость №1 + ведущий: имена cookie разные
GUEST2: dict[str, str] = {}    # второй зал — своя банка, свой билет


def config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


print("\n=== Страница и вход ===")
st, body = call("GET", "/quiz")
check("страница квиза открывается без билета (форма внутри)",
      st == 200 and b"<html" in bytes(body).lower())
st, _ = call("GET", f"/quiz?t={TOKEN}")
check("квиз открывается по QR-ссылке с кодом", st == 200)

st, _ = call("GET", "/api/quiz", jar={})
check("API квиза без билета закрыт", st == 403, str(st))
st, _ = call("POST", "/api/quiz/answer", {"question_id": 1, "option": 0}, jar={})
check("ответ без билета принят не будет", st == 403, str(st))
st, _ = call("POST", "/api/quiz/name", {"name": "Призрак"}, jar={})
check("имя без билета не меняют", st == 403, str(st))

st, _ = call("POST", "/api/join", {"name": "Аня", "invite": "НЕ-ТУТ"}, jar={})
check("чужой код не открывает квиз", st == 403, str(st))

st, res = call("POST", "/api/join", {"name": "Аня", "invite": TOKEN})
check("гость вошёл в ленту — тот же билет открывает и квиз", st == 200)
st, quiz = call("GET", "/api/quiz")
check("квиз видит гостя без всякого второго входа",
      st == 200 and quiz.get("phase") == "idle" and quiz.get("my_score") == 0,
      str(quiz)[:120])

st, res = call("POST", "/api/quiz/name", {"name": "  Аня   Петрова "})
check("имя правится на месте", st == 200 and res.get("name") == "Аня Петрова",
      str(res))
st, _ = call("POST", "/api/quiz/name", {"name": "   "}, expect=400)
check("пустое имя отклонено", True)

st, _ = call("GET", "/api/moderator/quiz")
check("панель квиза без пароля ведущего закрыта", st == 403, str(st))
st, _ = call("POST", "/api/moderator/login", {"password": PASS})
check("ведущий вошёл", st == 200)

print("\n=== Вопросы ведущего ===")
st, h = call("GET", "/api/moderator/quiz")
check("панель стартует с пустой колоды",
      h.get("phase") == "idle" and h.get("questions") == [], str(h)[:120])

st, r = call("POST", "/api/moderator/quiz/add", {
    "text": "Сколько будет 2 + 2?",
    "options": json.dumps(["3", "4", "5"], ensure_ascii=False),
    "correct": "1", "scored": "1"})
q1 = r.get("question", {}).get("id")
check("зачётный вопрос добавлен", st == 200 and q1 is not None, str(r)[:120])

st, r = call("POST", "/api/moderator/quiz/add", {
    "text": "Пицца с ананасами — это вкусно?",
    "options": json.dumps(["да", "нет"], ensure_ascii=False),
    "correct": "-1", "scored": "0"})
q2 = r.get("question", {}).get("id")
check("«хитрый» вопрос без очков добавлен", st == 200 and q2 is not None)
check("у хитрого вопроса правильного нет",
      r.get("question", {}).get("correct") == -1, str(r.get("question"))[:120])

st, _ = call("POST", "/api/moderator/quiz/add",
             {"text": "А тут ответ забыли", "options": '["a", "b"]',
              "correct": "-1", "scored": "1"}, expect=400)
check("зачётный без правильного варианта отклонён", True)
st, _ = call("POST", "/api/moderator/quiz/add",
             {"text": "Мало вариантов", "options": '["один"]',
              "correct": "0", "scored": "1"}, expect=400)
check("вопрос с одним вариантом отклонён", True)
st, _ = call("POST", "/api/moderator/quiz/add",
             {"text": "   ", "options": '["a", "b"]', "scored": "0"}, expect=400)
check("пустой вопрос отклонён", True)
many = json.dumps([f"в{i}" for i in range(7)])
st, _ = call("POST", "/api/moderator/quiz/add",
             {"text": "Семь вариантов", "options": many, "scored": "0"}, expect=400)
check("больше шести вариантов нельзя", True)

_, h = call("GET", "/api/moderator/quiz")
check("в колоде ровно два вопроса", len(h["questions"]) == 2, str(len(h["questions"])))

st, r = call("POST", "/api/moderator/quiz/update",
             {"question_id": q2, "scored": "1", "correct": "0"})
check("хитрый вопрос можно сделать зачётным",
      st == 200 and r["question"]["scored"] and r["question"]["correct"] == 0)
st, r = call("POST", "/api/moderator/quiz/update", {"question_id": q2, "scored": "0"})
check("обратно в хитрый — правильный снимается сам",
      st == 200 and r["question"]["correct"] == -1, str(r["question"])[:120])
st, r = call("POST", "/api/moderator/quiz/update",
             {"question_id": q1, "text": "Сколько будет 2+2 ?"})
check("правка текста не трогает остальные поля",
      st == 200 and r["question"]["text"] == "Сколько будет 2+2 ?"
      and r["question"]["correct"] == 1)

st, _ = call("POST", "/api/moderator/quiz/move",
             {"question_id": q2, "direction": "up"})
_, h = call("GET", "/api/moderator/quiz")
check("порядок меняется кнопкой", h["questions"][0]["id"] == q2,
      str([q["id"] for q in h["questions"]]))
call("POST", "/api/moderator/quiz/move", {"question_id": q1, "direction": "up"})

print("\n=== Игра: открытие и ответы ===")
st, h = call("POST", "/api/moderator/quiz/control", {"action": "open"})
check("открыт первый вопрос (q1)",
      h.get("phase") == "open" and h.get("current") == q1, str(h)[:120])
check("стадия квиза переживает перезапуск (leжит в конфиге)",
      config().get("quiz_phase") == "open" and config().get("quiz_question") == q1,
      str({k: config().get(k) for k in ("quiz_phase", "quiz_question")}))

st, quiz = call("GET", "/api/quiz")
q = quiz.get("question") or {}
check("гость видит открытый вопрос",
      quiz.get("phase") == "open" and q.get("id") == q1)
check("правильный ответ гостю не отдаётся до закрытия",
      "correct" not in quiz and q.get("text"), str(quiz)[:160])
check("номер и размер колоды на месте",
      quiz.get("question_no") == 1 and quiz.get("questions_total") == 2)

st, _ = call("POST", "/api/quiz/answer",
             {"question_id": q1, "option": 9}, expect=400)
check("несуществующий вариант отклонён", True)
st, _ = call("POST", "/api/quiz/answer",
             {"question_id": q2, "option": 0}, expect=409)
check("ответ на не открытый сейчас вопрос отклонён", True)

st, _ = call("POST", "/api/join", {"name": "Борис", "invite": TOKEN}, jar=GUEST2)
st, _ = call("POST", "/api/quiz/answer", {"question_id": q1, "option": 0}, jar=GUEST2)
check("второй зал отвечает своим билетом", st == 200)
_, quiz2 = call("GET", "/api/quiz", jar=GUEST2)
check("второй зал — свой вопрос и свой ответ",
      quiz2.get("my_answer") == 0 and quiz2.get("question", {}).get("id") == q1)

st, r = call("POST", "/api/quiz/answer", {"question_id": q1, "option": 0})
check("ответ принят", st == 200 and r.get("answered_total") == 2, str(r))
st, r = call("POST", "/api/quiz/answer", {"question_id": q1, "option": 1})
check("до закрытия ответ меняется этим же участником",
      st == 200 and r.get("answered_total") == 2)
_, quiz = call("GET", "/api/quiz")
check("новый вариант стал своим", quiz.get("my_answer") == 1)

st, h = call("POST", "/api/moderator/quiz/control", {"action": "close"})
check("вопрос закрыт", h.get("phase") == "closed")
_, quiz = call("GET", "/api/quiz")
check("после закрытия видно распределение", quiz.get("counts") == [1, 1, 0],
      str(quiz.get("counts")))
check("правильный показан только после закрытия", quiz.get("correct") == 1)
check("верный ответ принёс очко", quiz.get("my_score") == 1, str(quiz.get("my_score")))
_, quiz2 = call("GET", "/api/quiz", jar=GUEST2)
check("неверный ответа очка не дал", quiz2.get("my_score") == 0)
st, _ = call("POST", "/api/quiz/answer", {"question_id": q1, "option": 2}, expect=409)
check("после закрытия ответ не принимается", True)

st, h = call("POST", "/api/moderator/quiz/control", {"action": "next"})
check("следующий вопрос открыт (тот что ниже по списку)",
      h.get("phase") == "open" and h.get("current") == q2, str(h)[:120])
call("POST", "/api/quiz/answer", {"question_id": q2, "option": 0}, jar=GUEST2)
call("POST", "/api/quiz/answer", {"question_id": q2, "option": 0})
st, h = call("POST", "/api/moderator/quiz/control", {"action": "close"})
_, quiz = call("GET", "/api/quiz")
check("хитрый вопрос: распределение есть, правильного нет",
      quiz.get("counts") == [2, 0] and quiz.get("correct") is None,
      str({k: quiz.get(k) for k in ("counts", "correct")}))
check("хитрый вопрос очков не даёт", quiz.get("my_score") == 1)

print("\n=== Экран и стадии ===")
st, h = call("POST", "/api/moderator/quiz/control", {"action": "board"})
check("вывели лидеров", h.get("phase") == "board")
_, quiz = call("GET", "/api/quiz")
names = [row["name"] for row in quiz.get("board", [])]
check("лидер — переименованный гость с очком",
      names and names[0] == "Аня Петрова" and quiz["board"][0]["score"] == 1,
      str(names))
_, quiz2 = call("GET", "/api/quiz", jar=GUEST2)
check("второй зал тоже видит таблицу", quiz2.get("phase") == "board")

st, h = call("POST", "/api/moderator/quiz/control", {"action": "feed"})
check("лента возвращена на экран, фаза не тронута",
      h.get("phase") == "board" and h.get("visible") is False)
st, h = call("POST", "/api/moderator/quiz/control", {"action": "show"})
check("квиз вернули на экран", h.get("visible") is True)

st, body = call("GET", f"/api/qr.svg?d={SCREEN_KEY}")
check("QR ленты по ключу экрана", st == 200 and b"<svg" in bytes(body)[:64])
st, _ = call("GET", "/api/qr.svg", jar={})
check("QR без ключа экрана и без сессии закрыт", st == 403, str(st))
st, body = call("GET", f"/api/qr.svg?d={SCREEN_KEY}&kind=quiz")
check("отдельный QR квиза делается", st == 200 and b"<svg" in bytes(body)[:64])
st, _ = call("GET", "/api/qr.svg?kind=quiz", jar={})
check("QR квиза тоже требует ключ экрана", st == 403, str(st))
# а вот ведущий со своей сессии QR получает без всякого ключа — так панель
# живёт в кабинете и ничего не просит у администратора
st, body = call("GET", "/api/qr.svg?kind=quiz")
check("ведущему QR квиза выдаётся по его сессии",
      st == 200 and b"<svg" in bytes(body)[:64], str(st))

if websockets is not None:
    async def ws_checks() -> None:
        cookie = "; ".join(f"{k}={v}" for k, v in COOKIES.items())
        async def recv(ws, timeout=4):
            return json.loads(await asyncio.wait_for(ws.recv(), timeout))

        async with websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as dws:
            init = await recv(dws)
            check("экран получает кадр квиза прямо в init",
                  init.get("type") == "init"
                  and (init.get("quiz") or {}).get("phase") == "board",
                  str(init.get("quiz"))[:120])

            async with websockets.connect(f"{WS}/ws/quiz",
                                          additional_headers={"Cookie": cookie}) as qws:
                init = await recv(qws)
                check("канал квиза здоровается своим снимком",
                      init.get("type") == "init" and "quiz" in init)

                call("POST", "/api/moderator/quiz/control",
                     {"action": "open", "question_id": q1})
                msg = await recv(qws)
                check("гостям сигналит «изменилось», а не чужие ответы",
                      msg.get("type") == "quiz_update", str(msg)[:80])

                msg = await recv(dws)
                while msg.get("type") != "quiz":   # между кадрами мог пройти refresh
                    msg = await recv(dws)
                check("экран получил новый кадр с открытым вопросом",
                      (msg.get("quiz") or {}).get("phase") == "open"
                      and msg["quiz"]["question"]["id"] == q1,
                      str(msg.get("quiz"))[:160])

    asyncio.run(ws_checks())
else:
    print("  OK   ws-каналы квиза (websockets не установлен — пропущено)")

print("\n=== Хозяйственные операции ===")
st, h = call("POST", "/api/moderator/quiz/control", {"action": "reset"})
check("сброс ответов стёр результат",
      st == 200 and h.get("answers_total") == 0, str(h)[:120])
_, quiz = call("GET", "/api/quiz")
check("после сброса у гостя снова ноль очков", quiz.get("my_score") == 0)

st, h = call("POST", "/api/moderator/quiz/control",
             {"action": "open", "question_id": q1})
check("вопрос снова открыт", h.get("phase") == "open" and h.get("current") == q1)
st, _ = call("POST", "/api/moderator/quiz/delete", {"question_id": q1})
_, h = call("GET", "/api/moderator/quiz")
check("удаление открытого вопроса останавливает игру",
      h.get("phase") == "idle" and h.get("current") is None, str(h)[:120])
check("конфиг очищен от стадии квиза",
      "quiz_phase" not in config(), str(config().get("quiz_phase")))
_, quiz = call("GET", "/api/quiz")
check("гости увидели конец игры", quiz.get("phase") == "idle")
st, _ = call("POST", "/api/quiz/answer", {"question_id": q1, "option": 0}, expect=409)
check("ответ на удалённый вопрос невозможен", True)

_, h = call("GET", "/api/moderator/quiz")
check("ответы удалённого вопроса ушли каскадом", h.get("answers_total") == 0)
st, _ = call("POST", "/api/moderator/quiz/delete", {"question_id": q2})
check("второй вопрос удаляется", st == 200)
st, _ = call("POST", "/api/moderator/quiz/delete", {"question_id": 999999}, expect=404)
check("чужого вопроса нет", True)
st, _ = call("POST", "/api/moderator/quiz/control", {"action": "кашу", }, expect=400)
check("неизвестное действие управляющего отклонено", True)

st, _ = call("POST", "/api/moderator/quiz/control", {"action": "close"}, expect=400)
check("закрывать нечего — честно отказано", True)
st, h = call("POST", "/api/moderator/quiz/control", {"action": "board"})
if st == 200:  # board при idle сервер допускает — экран просто показывает пустую
    check("board при idle не ломает игру", h.get("phase") == "board")
    call("POST", "/api/moderator/quiz/control", {"action": "stop"})

print("\n=== Итог ===")
if failures:
    print(f"Провалено проверок: {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("Квиз работает как задумано.")
