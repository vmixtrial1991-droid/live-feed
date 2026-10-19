"""Проверка заставки на весь экран: загрузка, переключение, изоляция событий.

Заставка — одна картинка на событие. Ведущий грузит её один раз внизу своей
панели, дальше просто жмёт кнопку: на экране она появляется плавно, повторное
нажатие убирает её так же плавно и возвращает показ отправленного.

Запуск при поднятом сервере: python test_splash.py
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

from testlib import prepare, DATA_DIR  # noqa: E402

CTX = prepare("Проверка заставки")
SLUG = CTX.slug
BASE = CTX.base
# корень сервера без адреса события: заставку сервер отдаёт по адресу,
# который сам вернул, и он уже начинается с /e/<событие>/
SERVER = BASE[: BASE.index("/e/")]
TOKEN = CTX.code
PASS = CTX.mod_password
import json as _j, os as _os
SCREEN_KEY = _j.load(open(_os.path.join(DATA_DIR, "events", SLUG, "config.json"), encoding="utf-8")).get("screen_key", "")
WS = BASE.replace("http://", "ws://")
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


class Client:
    """Отдельный браузер: свой набор cookie."""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(self, method: str, path: str, fields=None, raw: bytes | None = None,
                content_type: str = ""):
        url = path if path.startswith("http") else BASE + path
        data = None
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type
        elif fields is not None:
            data = urllib.parse.urlencode(fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def post(self, path: str, fields: dict):
        st, body = self.request("POST", path, fields)
        try:
            return st, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return st, {}

    def post_json(self, path: str, fields: dict):
        return self.post(path, fields)

    def get_json(self, path: str):
        st, body = self.request("GET", path)
        try:
            return json.loads(body or b"{}")
        except json.JSONDecodeError:
            return {}

    def get(self, path: str):
        st, body = self.request("GET", path)
        return st, body

    def upload(self, path: str, field: str, filename: str, data: bytes,
               mime: str = "image/png") -> tuple[int, dict]:
        """Отправляет файл multipart — так же, как форма в браузере."""
        import uuid
        boundary = "----splash" + uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        st, body = self.request(
            "POST", path, raw=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            return st, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return st, {}

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)


def png(width: int = 1920, height: int = 1080) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (18, 22, 40))
    draw = ImageDraw.Draw(img)
    draw.ellipse([width * .2, height * .2, width * .8, height * .8],
                 fill=(240, 200, 90))
    draw.text((width * .38, height * .5), "SPLASH", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def wait_for(ws, want: str, timeout: float = 8.0, where=None) -> dict | None:
    """Ждёт событие типа want. На сокете может лежать пачка предыдущих."""
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

    print("\n=== До загрузки ===")
    guest = Client()
    st, joined = await asyncio.to_thread(
        guest.post, "/api/join", {"name": "Гость", "invite": TOKEN}
    )
    check("гость зашёл", st == 200, str(st))

    st, body = await asyncio.to_thread(guest.get, "/api/config")
    cfg = json.loads(body)
    check("в конфиге заставки нет", cfg.get("splash") is None, str(cfg.get("splash")))

    st, _ = await asyncio.to_thread(guest.get, "/splash.jpg")
    check("заставки нет — 404", st == 404, str(st))

    st, res = await asyncio.to_thread(
        guest.upload, "/api/moderator/splash", "image", "zastavka.png", png()
    )
    check("гость не может грузить заставку", st == 403, str(st))

    print("\n=== Загрузка модератором ===")
    mod = Client()
    st, _ = await asyncio.to_thread(mod.post, "/api/moderator/login", {"password": PASS})
    check("вход по паролю", st == 200, str(st))

    st, res = await asyncio.to_thread(
        mod.upload, "/api/moderator/splash", "image", "zastavka.png", png()
    )
    url = res.get("splash")
    check("заставка загружена", st == 200 and url, f"{st} {url}")
    check("адрес заставки с отметкой свежести", url and "?v=" in url, str(url))

    # url уже полный адрес внутри приложения — приклеиваем адрес события,
    # иначе получилось бы /e/slug/e/slug/splash.jpg
    st, body = await asyncio.to_thread(guest.get, SERVER + url)
    check("файл отдаётся и он JPEG", st == 200 and body[:2] == b"\xff\xd8",
          f"{st} {body[:2]!r}")
    check("картинку видно и без входа модератора", st == 200)

    st, body = await asyncio.to_thread(guest.get, "/api/config")
    check("в конфиге заставка появилась", json.loads(body).get("splash"), "")

    print("\n=== Без файла ===")
    st, res = await asyncio.to_thread(mod.post, "/api/moderator/splash", {})
    check("пустая загрузка отклонена", st == 400, str(st))
    st, res = await asyncio.to_thread(
        mod.upload, "/api/moderator/splash", "image", "zamok.png",
        "не картинка".encode("utf-8"),
    )
    check("мусор вместо картинки отклонён", st == 400, str(st))

    print("\n=== Переключение на экране ===")
    async with websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as ws:
        await wait_for(ws, "init")

        st, res = await asyncio.to_thread(
            mod.post, "/api/moderator/screen", {"action": "splash", "value": "1"}
        )
        screen = res.get("screen", {})
        check("заставка включилась", st == 200 and screen.get("splash_visible") is True,
              str(screen.get("splash_visible")))

        msg = await wait_for(ws, "screen",
                             where=lambda m: m["screen"].get("splash_visible") is True)
        check("экран узнал про заставку", msg is not None)

        st, res = await asyncio.to_thread(
            mod.post, "/api/moderator/screen", {"action": "splash", "value": "0"}
        )
        check("заставка выключилась",
              res.get("screen", {}).get("splash_visible") is False, str(res.get("screen")))

        msg = await wait_for(ws, "screen",
                             where=lambda m: m["screen"].get("splash_visible") is False)
        check("экран узнал об уходе заставки", msg is not None)

    print("\n=== Заставка останавливает показ ===")
    async with websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as ws:
        await wait_for(ws, "init")
        await asyncio.to_thread(
            mod.post, "/api/moderator/screen", {"action": "interval", "value": "2"}
        )
        # три сообщения: пока заставка поднята, индекс обязан стоять
        for i in range(3):
            await asyncio.to_thread(
                guest.post, "/api/messages", {"body": f"сообщение номер {i}"}
            )
        await asyncio.to_thread(
            mod.post, "/api/moderator/screen", {"action": "splash", "value": "1"}
        )
        index_before = (await asyncio.to_thread(
            mod.get_json, "/api/moderator/state"))["screen"].get("index")
        await asyncio.sleep(5)

        held = await asyncio.to_thread(mod.get_json, "/api/moderator/state")
        check("показ стоит, пока заставка", held["screen"].get("index") == index_before,
              f"{index_before} → {held['screen'].get('index')}")
        check("состояние про заставку видно и в состоянии",
              held["screen"].get("splash_visible") is True)

        await asyncio.to_thread(
            mod.post, "/api/moderator/screen", {"action": "splash", "value": "0"}
        )
        await asyncio.sleep(5)
        back = await asyncio.to_thread(mod.get_json, "/api/moderator/state")
        check("после ухода заставки показ пошёл дальше",
              back["screen"].get("index") != index_before,
              f"{index_before} → {back['screen'].get('index')}")
        check("все сообщения на месте", len(back["messages"]) >= 3,
              str(len(back["messages"])))

    print("\n=== Замена и удаление на живом экране ===")
    async with websockets.connect(f"{WS}/ws/display?d={SCREEN_KEY}") as ws:
        await wait_for(ws, "init")

        st, res = await asyncio.to_thread(
            mod.upload, "/api/moderator/splash", "image", "drugaia.png", png(800, 800)
        )
        check("заставка заменилась", st == 200 and res.get("splash"), str(st))
        check("адрес сменился, чтобы браузер не держал старую картинку",
              res.get("splash") != url, f"{url} → {res.get('splash')}")

        msg = await wait_for(ws, "screen",
                             where=lambda m: m.get("splash") == res.get("splash"))
        check("экран узнал о новой заставке без перезагрузки", msg is not None)

        st, _ = await asyncio.to_thread(guest.get, SERVER + res["splash"])
        check("новая заставка отдаётся", st == 200)

        st, res = await asyncio.to_thread(mod.post, "/api/moderator/splash",
                                           {"remove": "1"})
        check("заставка удалена", st == 200 and res.get("splash") is None, str(res))
        msg = await wait_for(ws, "screen",
                             where=lambda m: "splash" in m and m["splash"] is None)
        check("экран узнал, что заставки больше нет", msg is not None)

    st, _ = await asyncio.to_thread(guest.get, "/splash.jpg")
    check("файла больше нет", st == 404, str(st))
    st, body = await asyncio.to_thread(guest.get, "/api/config")
    check("в конфиге снова пусто", json.loads(body).get("splash") is None)

    print("\n=== Уборка ===")
    total = sum(m["body"] == "сообщение номер 0"
                for m in (await asyncio.to_thread(mod.get_json, "/api/messages"))["messages"])
    check("наши сообщения на месте до уборки", total == 1, str(total))

    print("\n=== Итог ===")
    if FAILURES:
        print(f"Провалено проверок: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("Заставка работает.")


if __name__ == "__main__":
    asyncio.run(main())