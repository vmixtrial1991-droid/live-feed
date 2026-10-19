"""Проверка реального времени и разграничения доступа через WebSocket.

Запуск: сервер на порту 8123, затем `python test_realtime.py`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
import urllib.request
import http.cookiejar

import websockets

# --- 3.0: у каждой проверки своё событие ---------------------------------
# Адреса теперь живут под адресом события, поэтому проверка заводит
# своё событие и работает уже по нему.
from testlib import prepare, remove, DATA_DIR  # noqa: E402

CTX = prepare("Проверка реального времени")
SLUG = CTX.slug
BASE = CTX.base
TOKEN = CTX.code
PASS = CTX.mod_password
import json as _j, os as _os
SCREEN_KEY = _j.load(open(_os.path.join(DATA_DIR, "events", SLUG, "config.json"), encoding="utf-8")).get("screen_key", "")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://")
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def load_config() -> dict:
    with open("data/config.json", encoding="utf-8") as f:
        return json.load(f)


class Client:
    """HTTP-клиент со своей банкой cookie — отдельный «браузер»."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path: str, fields: dict) -> dict:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(BASE + path, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, "detail": json.loads(e.read()).get("detail")}

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)


def cookies_for(client: Client) -> str:
    return client.cookie_header()


async def collect(ws, want: str, timeout: float = 6.0, where=None) -> dict | None:
    """Ждём событие типа want; where — дополнительное условие по содержимому.

    На сокете может лежать пачка предыдущих событий, поэтому одного
    совпадения по типу мало: ищем именно то, которое нужно.
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
    token = TOKEN
    password = PASS

    print("\n=== Доступ к WebSocket ===")
    # гость без cookie
    try:
        async with websockets.connect(f"{WS}/ws/moderator") as ws:
            check("гость не попадает в /ws/moderator", False, "соединение открылось")
    except Exception:
        check("гость не попадает в /ws/moderator", True)

    try:
        async with websockets.connect(f"{WS}/ws/participant") as ws:
            check("гость не попадает в /ws/participant", False, "соединение открылось")
    except Exception:
        check("гость не попадает в /ws/participant", True)

    try:
        async with websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as ws:
            init = json.loads(await ws.recv())
            check("витрина открыта для всех и сразу отдаёт ленту",
                  init.get("type") == "init" and "messages" in init)
    except Exception as exc:
        check("витрина открыта для всех", False, str(exc))

    print("\n=== Реальное время: гость → модератор ===")
    anon = Client()   # без сессии: проверяем открытые эндпоинты
    guest = Client()
    guest.post("/api/join", {"name": "Ольга", "invite": token})
    mod = Client()
    mod.post("/api/moderator/login", {"password": password})

    async with (
        websockets.connect(f"{WS}/ws/moderator", additional_headers={"Cookie": cookies_for(mod)}) as modws,
        websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as displayws,
        websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as guestws,
        websockets.connect(f"{WS}/ws/participant", additional_headers={"Cookie": cookies_for(guest)}) as pws,
    ):
        await collect(modws, "init")
        await collect(displayws, "init")
        await collect(guestws, "init")
        await collect(pws, "init")

        # Ольга отправляет — модерации нет, всё уходит в эфир сразу
        sent = guest.post("/api/messages", {"body": "Ольга приветствует зал"})
        mid = sent.get("message", {}).get("id")
        check("отправка сразу в эфире", sent.get("message", {}).get("status") == "approved")

        ev = await collect(modws, "new",
                           where=lambda m: any(x.get("id") == mid for x in (m.get("added") or [])))
        check("модератор получает отправку без перезагрузки", ev is not None, f"id={mid}")

        scr = await collect(guestws, "new",
                            where=lambda m: any(x.get("id") == mid for x in (m.get("added") or [])))
        check("витрина гостя обновилась в реальном времени", scr is not None)

        scr2 = await collect(displayws, "new",
                             where=lambda m: any(x.get("id") == mid for x in (m.get("added") or [])))
        check("второй экран тоже обновился", scr2 is not None)

        # автору подтверждаем приём, модерация ему не нужна
        upd = await collect(pws, "refresh", timeout=3.0)
        check("автору пришло уведомление о приёме", upd is not None)

        print("\n=== Порядок показа ===")
        extra = guest.post("/api/messages", {"body": "Второе для порядка"})
        extra_mid = extra["message"]["id"]
        await collect(modws, "new", where=lambda m: any(x.get("id") == extra_mid for x in (m.get("added") or [])))

        state = json.loads(mod.opener.open(BASE + "/api/moderator/state").read())
        order_now = sorted(state.get("messages", []), key=lambda m: m["position"])
        target = order_now[-1]["id"]      # последнее в показе
        first_before = order_now[0]["id"]
        check("до перестановки в начале другой слайд", first_before != target,
              f"first={first_before} target={target}")

        after = mod.post("/api/moderator/move", {"message_id": target, "direction": "first"})
        order = sorted(after.get("messages", []), key=lambda m: m["position"])
        check("«в начало» поднимает сообщение наверх",
              len(order) > 1 and order[0]["id"] == target,
              str([(m["id"], m["position"]) for m in order]))
        check("позиции после перестановки уникальны",
              len({m["position"] for m in order}) == len(order))

        after2 = mod.post("/api/moderator/move", {"message_id": target, "direction": "last"})
        order2 = sorted(after2.get("messages", []), key=lambda m: m["position"])
        check("«в конец» опускает сообщение вниз", order2[-1]["id"] == target,
              str([(m["id"], m["position"]) for m in order2]))

        mod.post("/api/moderator/move", {"message_id": target, "direction": "first"})

        st = mod.post("/api/moderator/screen", {"action": "show", "value": str(target)})
        check("экран показывает выбранный слайд", st["screen"]["current_id"] == target)
        ev = await collect(displayws, "screen", timeout=2.5,
                           where=lambda m: m["screen"]["current_id"] == target)
        check("изменение экрана дошло до подписчиков", ev is not None)

        print("\n=== Отметка «показано» ===")
        ev = await collect(displayws, "shown", timeout=4.0,
                           where=lambda m: m.get("id") == target)
        check("пришло событие «показано»", ev is not None)
        st = json.loads(mod.opener.open(BASE + "/api/moderator/state").read())
        check("в списке видно отметку",
              any(m["id"] == target and m["shown_at"] for m in st.get("messages", [])))
        mod.post("/api/moderator/reset-shown", {})
        st = json.loads(mod.opener.open(BASE + "/api/moderator/state").read())
        check("«заново» сбросило отметки",
              all(not m["shown_at"] for m in st.get("messages", [])))

        print("\n=== QR на экране ===")
        st = mod.post("/api/moderator/screen", {"action": "qr", "value": "1"})
        check("QR показан", st["screen"]["qr_visible"] is True)
        ev = await collect(displayws, "screen", timeout=4.0,
                           where=lambda m: m["screen"].get("qr_visible") is True)
        check("экран узнал про QR", ev is not None)
        st = mod.post("/api/moderator/screen", {"action": "qr", "value": "0"})
        check("QR скрыт", st["screen"]["qr_visible"] is False)
        link = json.loads(anon.opener.open(BASE + "/api/invite-link?d=" + SCREEN_KEY).read())
        check("ссылка для QR открыта для всех", bool(link.get("link")))

        print("\n=== Автопрокрутка по кругу ===")
        mod.post("/api/moderator/screen", {"action": "interval", "value": "3"})
        mod.post("/api/moderator/screen", {"action": "play"})
        got = await collect(displayws, "screen", timeout=10.0,
                            where=lambda m: m["screen"]["current_id"] != target)
        check("слайд сменился сам по таймеру", got is not None,
              f"current={got['screen']['current_id'] if got else '—'}")

        print("\n=== Удаление ===")
        mod.post("/api/moderator/delete", {"message_id": mid})
        stat = json.loads(guest.opener.open(BASE + "/api/messages").read())
        check("удаление убирает сообщение с витрины",
              all(m["id"] != mid for m in stat["messages"]))
        me2 = json.loads(guest.opener.open(BASE + "/api/me").read())
        check("удалённое исчезло и у автора",
              all(m["id"] != mid for m in me2["messages"]))

    print("\n=== Итог ===")
    if FAILURES:
        print(f"Провалено проверок: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("Все проверки реального времени пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
