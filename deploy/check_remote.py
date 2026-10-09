"""Проверка развёрнутого сервера: HTTP и WebSocket через nginx.

Запускается на самом VPS из виртуального окружения приложения:

    /opt/live-feed/venv/bin/python check_remote.py http://2.27.11.50
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1").rstrip("/")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://")

# Адрес временного события: к нему привязываются все проверки
EVENT = ""


def EV(path: str) -> str:
    """Путь внутри события: /e/<адрес>/..."""
    return f"/e/{EVENT}" + ("" if path == "/" else path)


def WS_EV(path: str) -> str:
    """WebSocket-адрес события."""
    return WS + f"/e/{EVENT}" + path

# Пути разные при запуске без Docker и внутри контейнера, поэтому ищем
# первый существующий, а не задаём один жёстко.
CANDIDATE_DIRS = ["/opt/live-feed", "/app"]
ENV_FILES = ["/etc/live-feed.env"]

FAILURES: list[str] = []


def _find(candidates: list[str]) -> str | None:
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _first_existing(candidates: list[str]) -> str:
    found = _find(candidates)
    if found:
        return found
    return candidates[0]


APP_DIR = _first_existing(CANDIDATE_DIRS)
ENV_FILE = _first_existing(ENV_FILES)
CFG_FILE = f"{APP_DIR}/data/config.json"


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def read_env(key: str) -> str:
    """Значение из файла настроек, а при его отсутствии — из окружения."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    return os.environ.get(key, "")


class Client:
    """HTTP-клиент со своей банкой cookie — отдельный «браузер»."""

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
            with self.opener.open(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
        except urllib.error.URLError as e:
            return getattr(e, "code", 0), {}

    def post_json(self, path: str, fields: dict) -> tuple[int, dict]:
        """post, который всегда отдаёт разобранный JSON."""
        return self.post(path, fields)

    def get_json(self, path: str) -> dict:
        try:
            with self.opener.open(BASE + path, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {}
            raise

    def get_status(self, path: str) -> int:
        try:
            with self.opener.open(BASE + path, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def post_photo(self, body: str, data: bytes) -> tuple[int, dict]:
        import uuid
        boundary = "----lf" + uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="body"\r\n\r\n',
            body.encode() + b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image"; filename="big.png"\r\n'
            b"Content-Type: image/png\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        req = urllib.request.Request(
            BASE + EV("/api/messages"), data=b"".join(parts), method="POST"
        )
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Cookie", self.cookie_header())
        try:
            with self.opener.open(req, timeout=90) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


def photo_bytes() -> bytes:
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (2400, 1600))
    draw = ImageDraw.Draw(img)
    for y in range(0, 1600, 80):
        draw.rectangle([0, y, 2400, y + 80], fill=(y % 255, 90, 200 - y % 200))
    draw.text((900, 780), "TEST PHOTO", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_event(admin: "Client", password: str) -> str:
    """Входит в админку, заводит временное событие и возвращает его адрес."""
    import uuid

    st, res = admin.post_json("/api/admin/login", {"password": password})
    if st != 200:
        print(f"!! Не удалось войти в админку: {st} {res}")
        print("   Проверьте ADMIN_PASSWORD в .env или /etc/live-feed.env")
        sys.exit(1)

    title = f"Проверка сервера {uuid.uuid4().hex[:6]}"
    st, res = admin.post_json("/api/admin/events", {"title": title, "password": "proverka-1"})
    if st != 200 or not res.get("event"):
        print(f"!! Не удалось завести событие: {st} {res}")
        sys.exit(1)
    slug = res["event"]["slug"]
    print(f"   временное событие: {slug}")
    return slug


def drop_event(admin: "Client", slug: str) -> None:
    """Убирает временное событие: проверка не должна оставлять мусор."""
    try:
        admin.post_json(f"/api/admin/events/{slug}/delete", {})
    except Exception as exc:
        print(f"   не удалось удалить событие {slug}: {exc}")


def event_config(slug: str) -> dict:
    """Читает настройки события прямо с диска: код приглашения и пароль.

    Путь ищем и в текущем каталоге (для локального прогона), и там, где
    обычно стоит приложение на сервере.
    """
    roots = []
    if os.environ.get("LF_DATA_DIR"):
        roots.append(Path(os.environ["LF_DATA_DIR"]))
    roots += [Path("."), Path(APP_DIR), Path("/opt/live-feed"), Path("/app")]

    for root in roots:
        for candidate in (root / "events" / slug / "config.json",
                          root / "data" / "events" / slug / "config.json"):
            if candidate.exists():
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)

    print(f"!! Не нашёл настройки события {slug} на диске.")
    print("   Проверьте, где лежит data/events, и задайте LF_DATA_DIR.")
    sys.exit(1)


async def collect(ws, want: str, timeout: float = 10.0, where=None) -> dict | None:
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

    admin_password = read_env("ADMIN_PASSWORD")
    if not admin_password:
        print("!! Не найден ADMIN_PASSWORD — проверьте .env или /etc/live-feed.env")
        sys.exit(1)

    admin = Client()
    global EVENT
    EVENT = make_event(admin, admin_password)
    cfg = event_config(EVENT)
    invite = cfg["invite_token"]
    password = cfg.get("mod_password") or ""
    if not password:
        print("!! У события нет пароля ведущего — проверка не сможет войти в кабинет")
        drop_event(admin, EVENT)
        sys.exit(1)

    print(f"\n=== Сервер: {BASE} ===")

    print("\n=== Страницы ===")
    for path, name in [(EV("/"), "витрина"), (EV("/screen"), "экран"),
                       (EV("/moderator/login"), "вход ведущего")]:
        check(f"{name} открывается", Client().get_status(path) == 200)

    print("\n=== Права доступа ===")
    anon = Client()
    check("гость: витрина доступна", anon.get_json(EV("/api/messages")) is not None)
    st, _ = await asyncio.to_thread(anon.post, EV("/api/moderator/rotate-invite"), {})
    check("гость: кабинет закрыт", st in (403, 405), f"статус {st}")
    st, _ = await asyncio.to_thread(anon.post, EV("/api/messages"), {"body": "взлом"})
    check("гость: отправка без регистрации отклонена", st == 401, f"статус {st}")
    try:
        async with websockets.connect(f"{WS_EV('/ws/moderator')}"):
            check("гость не попадает в /ws/moderator", False, "соединение открылось")
    except Exception:
        check("гость не попадает в /ws/moderator", True)

    print("\n=== Регистрация и вход ===")
    # id сообщений, которые создаёт проверка: в конце удалим ровно их,
    # чтобы не оставлять мусор в живой ленте
    created: list[int] = []
    guest = Client()
    st, joined = await asyncio.to_thread(
        guest.post, EV("/api/join"), {"name": "Гость", "invite": invite}
    )
    check("регистрация по коду приглашения", st == 200 and joined.get("name") == "Гость")
    check("cookie гостя выставлена", f"lf_pid_{EVENT}" in guest.cookie_header())

    lead = Client()
    st, _ = await asyncio.to_thread(lead.post, EV("/api/moderator/login"), {"password": password})
    check("вход ведущего по паролю", st == 200, f"статус {st}")
    st, _ = await asyncio.to_thread(lead.post, EV("/api/moderator/login"), {"password": "точно неверно"})
    check("неверный пароль отклонён", st == 401, f"статус {st}")

    print("\n=== Реальное время через прокси ===")
    async with (
        websockets.connect(f"{WS_EV('/ws/moderator')}",
                           additional_headers={"Cookie": lead.cookie_header()}) as modws,
        websockets.connect(f"{WS_EV('/ws/display')}") as screenws,
        websockets.connect(f"{WS_EV('/ws/display')}") as screen2,
        websockets.connect(f"{WS_EV('/ws/display')}") as feedws,
    ):
        for name, ws in (("ведущий", modws), ("экран 1", screenws),
                         ("экран 2", screen2), ("витрина", feedws)):
            init = json.loads(await asyncio.wait_for(ws.recv(), 10))
            check(f"{name}: соединение живое", init["type"] == "init")

        st, sent = await asyncio.to_thread(
            guest.post, EV("/api/messages"), {"body": "Проверка через интернет"}
        )
        mid = sent["message"]["id"]
        created.append(mid)
        check("отправка сразу в эфире, без проверки",
              st == 200 and sent["message"]["status"] == "approved")

        ev = await collect(modws, "new", where=lambda m: m["message"]["id"] == mid)
        check("ведущий увидел без перезагрузки", ev is not None)
        evs = await collect(screenws, "new",
                            where=lambda m: any(x["id"] == mid for x in m["messages"]))
        check("экран 1 получил сразу", evs is not None)
        evs2 = await collect(screen2, "new",
                             where=lambda m: any(x["id"] == mid for x in m["messages"]))
        check("экран 2 получил сразу", evs2 is not None)
        evf = await collect(feedws, "new",
                           where=lambda m: any(x["id"] == mid for x in m["messages"]))
        check("витрина получила сразу", evf is not None)

        feed = await asyncio.to_thread(anon.get_json, EV("/api/messages"))
        check("сообщение видно и без WebSocket",
              any(m["id"] == mid for m in feed["messages"]))

        print("\n=== Фото ===")
        st, ph = await asyncio.to_thread(guest.post_photo, "Фото с проверки", photo_bytes())
        mid_ph = ph["message"]["id"]
        created.append(mid_ph)
        check("фото принято и сразу в эфире",
              st == 200 and ph["message"]["status"] == "approved" and bool(ph["message"]["image"]))
        size = 0
        photo_path = EV("/media/" + ph["message"]["image"])
        with urllib.request.urlopen(f"{BASE}{photo_path}", timeout=30) as r:
            blob = r.read()
            size = len(blob)
        check("фото отдаётся через прокси как JPEG", blob[:2] == b"\xff\xd8",
              f"{size // 1024} КБ")

        print("\n=== Управление показом ===")
        st, res = await asyncio.to_thread(
            lead.post, EV("/api/moderator/screen"), {"action": "interval", "value": "6"})
        check("интервал применён", res["screen"]["interval"] == 6, str(res["screen"]["interval"]))
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "show", "value": str(mid)})
        check("показ конкретного слайда", res["screen"]["current_id"] == mid)
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "next"})
        check("вперёд", res["screen"]["current_id"] != mid)
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "prev"})
        check("назад", res["screen"]["current_id"] == mid)
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "pause"})
        check("пауза", res["screen"]["playing"] is False)
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "play"})
        check("продолжение", res["screen"]["playing"] is True)
        st, _ = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "ерунда"})
        check("неизвестное действие отклонено", st == 400, f"статус {st}")

        print("\n=== Отметка «показано» ===")
        await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "show", "value": str(mid)})
        ev = await collect(screenws, "shown", where=lambda m: m.get("id") == mid)
        check("пришло событие «показано»", ev is not None)
        state = await asyncio.to_thread(lead.get_json, EV("/api/moderator/state"))
        check("в списке кабинета видна отметка",
              any(m["id"] == mid and m["shown_at"] for m in state["messages"]))
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/reset-shown"), {})
        check("«заново» сбросило отметки", all(not m["shown_at"] for m in res["messages"]))

        print("\n=== QR на экране ===")
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "qr", "value": "1"})
        check("QR показан", res["screen"]["qr_visible"] is True)
        ev = await collect(screenws, "screen", where=lambda m: m["screen"].get("qr_visible") is True)
        check("экран узнал про QR", ev is not None)
        st, res = await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "qr", "value": "0"})
        check("QR скрыт", res["screen"]["qr_visible"] is False)
        link = await asyncio.to_thread(anon.get_json, EV("/api/invite-link"))
        check("ссылка для QR доступна без пароля",
              bool(link.get("link")) and bool(link.get("code")), str(link.get("code")))

        print("\n=== Порядок показа ===")
        state = await asyncio.to_thread(lead.get_json, EV("/api/moderator/state"))
        last = max(state["messages"], key=lambda m: m["position"])["id"]
        st, res = await asyncio.to_thread(
            lead.post, EV("/api/moderator/move"), {"message_id": last, "direction": "first"})
        check("«в начало» работает через прокси",
              min(res["messages"], key=lambda m: m["position"])["id"] == last)
        st, res = await asyncio.to_thread(
            lead.post, EV("/api/moderator/move"), {"message_id": last, "direction": "last"})
        check("«в конец» работает через прокси",
              max(res["messages"], key=lambda m: m["position"])["id"] == last)

        print("\n=== Правка и удаление ===")
        st, res = await asyncio.to_thread(
            lead.post, EV("/api/moderator/edit"),
            {"message_id": mid, "body": "Исправленный текст", "author": "Гость П."})
        edited = next((m for m in res["messages"] if m["id"] == mid), None)
        check("правка текста и имени",
              edited and edited["body"] == "Исправленный текст"
              and edited["author"] == "Гость П.")

        st, res = await asyncio.to_thread(
            lead.post, EV("/api/moderator/delete"), {"message_id": mid_ph})
        check("удаление убирает из списка", all(m["id"] != mid_ph for m in res["messages"]))
        check("файл удалённого из ленты сообщения остаётся в архиве (4.0)",
              Client().get_status(EV(f"/media/{ph['message']['image']}")) == 200)

    print("\n=== Автопрокрутка ===")
    # Свои сообменты для этой проверки: если брать оставшиеся от прошлых
    # разделов, их может не оказаться (их удаляли) или остаться ровно одно —
    # тогда показ ходит по кругу и current_id не меняется
    await asyncio.to_thread(guest.post, EV("/api/messages"), {"body": "Автопрокрутка 1"})
    await asyncio.to_thread(guest.post, EV("/api/messages"), {"body": "Автопрокрутка 2"})
    state = await asyncio.to_thread(lead.get_json, EV("/api/moderator/state"))
    pair = [m["id"] for m in state["messages"][:2]]
    created.extend(pair)
    check("есть что показывать", len(pair) == 2, f"сообщений всего {len(state['messages'])}")

    await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "interval", "value": "3"})
    await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "show", "value": str(pair[0])})
    before = await asyncio.to_thread(lead.get_json, EV("/api/moderator/state"))
    before_id = before["screen"]["current_id"]
    await asyncio.to_thread(lead.post, EV("/api/moderator/screen"), {"action": "play"})
    async with websockets.connect(f"{WS_EV('/ws/display')}") as w:
        await asyncio.wait_for(w.recv(), 10)
        got = await collect(
            w, "screen", timeout=15.0,
            where=lambda m: m["screen"]["current_id"] not in (None, before_id))
    check("слайд сменился сам по таймеру", got is not None,
          f"current={got['screen']['current_id'] if got else '—'}")
    if got:
        # показ идёт по всем сообщениям подряд, а не только по двум новым,
        # поэтому проверяем одно: слайд стал другим
        state = await asyncio.to_thread(lead.get_json, EV("/api/moderator/state"))
        known = [m["id"] for m in state["messages"]]
        check("на экране одно из существующих сообщений",
              got["screen"]["current_id"] in known,
              f"current={got['screen']['current_id']}, всего {len(known)}")

    print("\n=== Уборка ===")
    for mid_del in created:
        await asyncio.to_thread(lead.post, EV("/api/moderator/delete"),
                                {"message_id": mid_del})
    state = await asyncio.to_thread(lead.get_json, EV("/api/moderator/state"))
    left = [m["id"] for m in state["messages"] if m["id"] in created]
    check("проверка не оставила сообщений", not left,
          f"остались {left}" if left else f"удалено {len(created)}")

    # событие тоже убираем: иначе проверка оставила бы мусор на сервере
    drop_event(admin, EVENT)
    check("временное событие удалено", Client().get_status(EV("/")) == 404,
          f"событие {EVENT}")

    print("\n=== Итог ===")
    if FAILURES:
        print(f"Провалено проверок: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print(f"Всё работает: {BASE}")


if __name__ == "__main__":
    asyncio.run(main())
