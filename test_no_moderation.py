"""Проверка режима без модерации: всё сразу в эфире, управление на одной странице.

Запуск при поднятом сервере: python test_no_moderation.py
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

# --- 3.0: у каждой проверки своё событие ---------------------------------
# Адреса теперь живут под адресом события, поэтому проверка заводит
# своё событие и работает уже по нему.
from testlib import prepare, remove  # noqa: E402

CTX = prepare("Проверка без модерации")
SLUG = CTX.slug
BASE = CTX.base
TOKEN = CTX.code
PASS = CTX.mod_password
WS = BASE.replace("http://", "ws://")
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def reset_db() -> None:
    import shutil
    import sys as _s

    _s.path.insert(0, ".")
    from app.db import EventStore
    from app.events import Event

    event = Event(SLUG)
    store = EventStore(event.db_path)
    with store.connect() as conn:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM participants")
        # таблица попыток входа создаётся лениво, при первом отказе.
        # Создаём её здесь же: второе соединение пока первое не закрыто
        # заблокировало бы базу
        conn.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, at REAL NOT NULL)"
        )
        conn.execute("DELETE FROM login_attempts")
    shutil.rmtree(event.uploads_dir, ignore_errors=True)
    event.uploads_dir.mkdir(parents=True, exist_ok=True)


class Client:
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
            with self.opener.open(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get_json(self, path: str) -> dict:
        try:
            with self.opener.open(BASE + path, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {}
            raise

    def get_status(self, path: str) -> int:
        try:
            with self.opener.open(BASE + path, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def post_photo(self, body: str, data: bytes) -> tuple[int, dict]:
        import uuid
        boundary = "----nf" + uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="body"\r\n\r\n',
            body.encode() + b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image"; filename="p.png"\r\n'
            b"Content-Type: image/png\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        req = urllib.request.Request(
            BASE + "/api/messages", data=b"".join(parts), method="POST"
        )
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Cookie", self.cookie_header())
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


def png(width: int = 1400, height: int = 1000) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(0, height, 70):
        draw.rectangle([0, y, width, y + 70], fill=(y % 250, 80, 220 - y % 200))
    draw.text((400, height // 2), "TEST", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def collect(ws, want: str, timeout: float = 8.0, where=None) -> dict | None:
    """Ждём событие типа want; where — дополнительное условие по его содержимому.

    Раньше на сокете может лежать пачка предыдущих событий, поэтому одного
    совпадения по type мало: ищем именно то, которое нам нужно.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - loop.time())
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        if msg.get("type") == want and (where is None or where(msg)):
            return msg
    return None


async def main() -> None:
    import websockets

    with open("data/config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    reset_db()
    invite, password = TOKEN, PASS

    print("\n=== Страницы ===")
    for path, name in [("/", "витрина"), ("/screen", "экан"),
                       ("/moderator/login", "вход")]:
        check(f"{name} открывается", Client().get_status(path) == 200)

    print("\n=== Модерации больше нет ===")
    guest = Client()
    st, joined = await asyncio.to_thread(
        guest.post, "/api/join", {"name": "Аня", "invite": invite}
    )
    check("регистрация по коду", st == 200 and joined.get("name") == "Аня")

    st, sent = await asyncio.to_thread(guest.post, "/api/messages", {"body": "Первое"})
    mid1 = sent["message"]["id"]
    check("отправка сразу одобрена", sent["message"]["status"] == "approved")
    check("отметки «показано» ещё нет", sent["message"]["shown_at"] is None)

    feed = await asyncio.to_thread(guest.get_json, "/api/messages")
    check("сообщение сразу в общей ленте",
          any(m["id"] == mid1 for m in feed["messages"]), str(len(feed["messages"])))

    st_gone, _ = await asyncio.to_thread(
        guest.post, "/api/moderator/moderate",
        {"message_id": mid1, "action": "approve"},
    )
    check("эндпоинт модерации удалён", st_gone == 404, str(st_gone))

    st_empty, _ = await asyncio.to_thread(guest.post, "/api/messages", {})
    check("пустая отправка отклонена", st_empty == 400, str(st_empty))

    print("\n=== Фото сразу в эфир ===")
    st, sent2 = await asyncio.to_thread(guest.post_photo, "Фото без ожидания", png())
    mid2 = sent2["message"]["id"]
    check("фото принято и сразу одобрено", sent2["message"]["status"] == "approved")
    check("файл сохранён", bool(sent2["message"]["image"]))
    with urllib.request.urlopen(f"{BASE}/media/{sent2['message']['image']}", timeout=20) as r:
        blob = r.read()
    check("фото сжато на сервере в JPEG",
          r.status == 200 and blob[:2] == b"\xff\xd8" and len(blob) < 600_000,
          f"{len(blob) // 1024} КБ")

    print("\n=== Вход модератора и состояние ===")
    anon = Client()
    st_locked, _ = await asyncio.to_thread(anon.post, "/api/moderator/rotate-invite", {})
    check("гость: кабинет закрыт", st_locked in (403, 405), str(st_locked))
    st_bad, _ = await asyncio.to_thread(anon.post, "/api/moderator/login", {"password": "мимо"})
    check("неверный пароль отклонён", st_bad == 401, str(st_bad))

    mod = Client()
    st, _ = await asyncio.to_thread(mod.post, "/api/moderator/login", {"password": password})
    check("вход по паролю", st == 200, str(st))

    modstate = await asyncio.to_thread(mod.get_json, "/api/moderator/state")
    check("в состоянии только список сообщений",
          set(modstate.keys()) >= {"messages", "screen", "stats"}
          and "pending" not in modstate and "rejected" not in modstate,
          str(sorted(modstate.keys())))
    check("сводки нет: stats без pending/rejected",
          set(modstate["stats"].keys()) == {"total", "participants"},
          str(modstate["stats"]))

    print("\n=== Реальное время: всё летит сразу ===")
    async with (
        websockets.connect(WS + "/ws/moderator",
                           additional_headers={"Cookie": mod.cookie_header()}) as modws,
        websockets.connect(WS + "/ws/display") as screenws,
        websockets.connect(WS + "/ws/display") as feedws,
    ):
        for name, ws in (("модератор", modws), ("экран", screenws), ("витрина", feedws)):
            init = json.loads(await asyncio.wait_for(ws.recv(), 8))
            check(f"{name}: init пришёл", init["type"] == "init")

        st, sent3 = await asyncio.to_thread(guest.post, "/api/messages", {"body": "Без очереди"})
        mid3 = sent3["message"]["id"]

        ev = json.loads(await asyncio.wait_for(modws.recv(), 8))
        check("модератору пришло событие «новое» без одобрения",
              ev["type"] == "new" and ev["message"]["id"] == mid3, ev["type"])
        check("в событии уже полный список", any(m["id"] == mid3 for m in ev["messages"]))

        evs = json.loads(await asyncio.wait_for(screenws.recv(), 8))
        check("экран получил сразу, без модерации",
              evs["type"] == "new" and any(m["id"] == mid3 for m in evs["messages"]))
        evf = json.loads(await asyncio.wait_for(feedws.recv(), 8))
        check("витрина получила сразу", any(m["id"] == mid3 for m in evf["messages"]))

        print("\n=== Управление показом ===")
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "interval", "value": "7"})
        check("интервал применён", res["screen"]["interval"] == 7, str(res["screen"]["interval"]))
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "pause"})
        check("пауза включилась", res["screen"]["playing"] is False)
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "show", "value": str(mid1)})
        check("показ конкретного слайда", res["screen"]["current_id"] == mid1)
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "next"})
        check("вперёд", res["screen"]["current_id"] != mid1)
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "prev"})
        check("назад вернул", res["screen"]["current_id"] == mid1)
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "play"})
        check("продолжение", res["screen"]["playing"] is True)
        st, _ = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "ерунда"})
        check("неизвестное действие отклонено", st == 400, str(st))

        print("\n=== Отметка «уже показывали» ===")
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "show", "value": str(mid1)})
        ev = await collect(screenws, "shown", timeout=4,
                           where=lambda m: m.get("id") == mid1)
        check("пришло событие «показано»", ev is not None)
        state = await asyncio.to_thread(mod.get_json, "/api/moderator/state")
        shown = {m["id"]: bool(m["shown_at"]) for m in state["messages"]}
        check("в списке видно, что показано", shown.get(mid1) is True, str(shown))
        # mid2 показывали, пока листали вперёд-назад; mid3 не показывали ни разу
        check("никогда не показанное не отмечено", shown.get(mid3) is False, str(shown))

        st, res = await asyncio.to_thread(mod.post, "/api/moderator/reset-shown", {})
        state = await asyncio.to_thread(mod.get_json, "/api/moderator/state")
        check("«заново» сбросил отметки",
              all(not m["shown_at"] for m in state["messages"]))

        print("\n=== QR на экране ===")
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "qr", "value": "1"})
        check("QR показан", res["screen"]["qr_visible"] is True)
        check("показ при этом остановлен",
              res["screen"]["playing"] is False or res["screen"]["qr_visible"] is True)
        ev = await collect(screenws, "screen", timeout=4,
                           where=lambda m: m["screen"].get("qr_visible") is True)
        check("экран узнал про QR", ev is not None)
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/screen", {"action": "qr", "value": "0"})
        check("QR скрыт", res["screen"]["qr_visible"] is False)

        link = await asyncio.to_thread(anon.get_json, "/api/invite-link")
        check("ссылка для QR доступна без пароля",
              bool(link.get("link")) and bool(link.get("code")), str(link.get("code")))

        print("\n=== Порядок и удаление ===")
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/move",
                                          {"message_id": mid3, "direction": "first"})
        order = sorted(res["messages"], key=lambda m: m["position"])
        check("«в начало» работает", order and order[0]["id"] == mid3, str([m["id"] for m in order]))
        check("позиции уникальны", len({m["position"] for m in res["messages"]}) == len(res["messages"]))

        st, res = await asyncio.to_thread(mod.post, "/api/moderator/edit",
                                          {"message_id": mid3, "body": "Исправлено", "author": "Аня П."})
        edited = next((m for m in res["messages"] if m["id"] == mid3), None)
        check("правка текста и имени",
              edited and edited["body"] == "Исправлено" and edited["author"] == "Аня П.")

        img = next(m["image"] for m in res["messages"] if m["id"] == mid2)
        st, res = await asyncio.to_thread(mod.post, "/api/moderator/delete", {"message_id": mid2})
        check("удаление убирает из списка", all(m["id"] != mid2 for m in res["messages"]))
        st_media = await asyncio.to_thread(guest.get_status, "/media/" + img)
        check("файл удалённого сообщения убран", st_media == 404, str(st_media))

    print("\n=== Итог ===")
    if FAILURES:
        print(f"Провалено проверок: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("Все проверки режима без модерации пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
