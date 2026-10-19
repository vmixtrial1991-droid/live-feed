"""Проверка основных сценариев через HTTP (запуск при поднятом сервере)."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request

# --- 3.0: у каждой проверки своё событие ---------------------------------
# Адреса теперь живут под адресом события, поэтому проверка заводит
# своё событие и работает уже по нему.
from testlib import prepare, remove  # noqa: E402

CTX = prepare("Проверка API")
SLUG = CTX.slug
BASE = CTX.base
TOKEN = CTX.code
PASS = CTX.mod_password
import json as _j
_cfg = _j.load(open(os.path.join("data", "events", SLUG, "config.json"), encoding="utf-8"))
SCREEN_KEY = _cfg.get("screen_key", "")
COOKIES: dict[str, str] = {}


def reset_state() -> None:
    """Чистим базу и загрузки события напрямую, чтобы тест был повторяемым."""
    sys.path.insert(0, ".")
    import shutil

    from app.db import EventStore
    from app.events import Event

    event = Event(SLUG)
    store = EventStore(event.db_path)
    with store.connect() as conn:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM participants")
    # папку снимаем целиком: событие своё, а файлы могли остаться от проверок
    shutil.rmtree(event.uploads_dir, ignore_errors=True)
    event.uploads_dir.mkdir(parents=True, exist_ok=True)


reset_state()



class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)
failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        failures.append(name)


def call(method: str, url: str, fields: dict | None = None, files: dict | None = None,
         expect: int | None = None):
    if files:
        boundary = "----test7Xk9"
        body = b""
        for k, v in (fields or {}).items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fname, data, mime) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
            body += data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(BASE + url, data=body, method=method)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif fields is not None:
        from urllib.parse import urlencode
        data = urlencode(fields).encode()
        req = urllib.request.Request(BASE + url, data=data, method=method)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        req = urllib.request.Request(BASE + url, method=method)

    if COOKIES:
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in COOKIES.items()))

    try:
        with opener.open(req) as resp:
            raw = resp.read()
            status = resp.status
            for h in resp.headers.get_all("Set-Cookie") or []:
                if "=" in h:
                    k, v = h.split("=", 1)
                    COOKIES[k.strip()] = v.split(";")[0]
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        for h in exc.headers.get_all("Set-Cookie") or []:
            if "=" in h:
                k, v = h.split("=", 1)
                COOKIES[k.strip()] = v.split(";")[0]

    if expect is not None and status != expect:
        check(f"{method} {url}", False, f"статус {status}, ждали {expect}: {raw[:160]!r}")
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, raw


def png_bytes(w: int = 2400, h: int = 1800) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h))
    for y in range(0, h, 60):
        for x in range(0, w, 60):
            img.paste((x % 255, y % 255, 160), (x, y, min(x + 60, w), min(y + 60, h)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


print("\n=== Страницы ===")
# Витрины в списке нет: она закрыта для всех, кроме администратора и
# ведущего. Постороннему она отвечает редиректом на страницу входа,
# и это проверяется отдельно — тут просто не ждём 200.
st, _ = call("GET", "/send")
check("/send без сессии -> редирект на /join", st in (301, 302, 303, 307))

for path, name in [(f"/screen?d={SCREEN_KEY}", "экран"),
                   (f"/join?t={TOKEN}", "вход"), ("/moderator/login", "вход модератора")]:
    st, body = call("GET", path)
    check(f"{name} открывается ({st})", st == 200 and b"<html" in bytes(body).lower())

st, body = call("GET", "/")
check("витрина постороннему не отдаётся",
      st in (301, 302, 303, 307), str(st))

st, _ = call("GET", "/moderator")
check("/moderator без пароля -> редирект на логин", st in (301, 302, 303, 307))

print("\n=== Вход по QR-коду ===")
_, cfg = call("GET", "/api/config")
token = TOKEN

st, _ = call("POST", "/api/join", {"name": "Взломщик", "invite": "WRONGCODE"}, expect=403)
check("чужой код приглашения отклонён", True)
st, _ = call("POST", "/api/join", {"name": "  ", "invite": token}, expect=400)
check("пустое имя отклонено", True)

st, res = call("POST", "/api/join", {"name": "  Аня   Петрова ", "invite": token}, expect=200)
check("регистрация по QR работает", res.get("ok") and res.get("name") == "Аня Петрова",
      res.get("name", "?"))
check("cookie отправителя выставлена", f"lf_pid_{SLUG}" in COOKIES)

_, me = call("GET", "/api/me")
check("сессия живёт: /api/me узнаёт Аню", me.get("joined") and me.get("name") == "Аня Петрова")

print("\n=== Отправка ===")
st, _ = call("POST", "/api/messages", {"body": ""}, expect=400)
check("пустая отправка отклонена", True)
st, _ = call("POST", "/api/messages",
             {"body": "тест"}, files={"image": ("x.png", b"not-an-image", "image/png")}, expect=400)
check("битое изображение отклонено", True)

st, res = call("POST", "/api/messages", {"body": "Привет с праздника!"}, expect=200)
mid1 = res["message"]["id"]
check("текст сразу в эфире", res["message"]["status"] == "approved")
check("отметки «показано» нет", res["message"]["shown_at"] is None)

st, res = call("POST", "/api/messages", {"body": "Фото сверху"},
               files={"image": ("big.png", png_bytes(), "image/png")}, expect=200)
mid2 = res["message"]["id"]
check("фото принято", bool(res["message"]["image"]), res["message"]["image"] or "")
# сервер сжимает и перекодирует в JPEG
size = len(call("GET", f"/media/{res['message']['image']}")[1]) if res["message"]["image"] else 0
check("большое фото сжато на сервере", 0 < size < 900_000, f"{size} байт из ~5 МБ PNG")

print("\n=== Кабинет по паролю ===")
st, _ = call("POST", "/api/moderator/login", {"password": "неверный"}, expect=401)
check("неверный пароль отклонён", True)
st, _ = call("GET", "/api/moderator/state", expect=403)
check("состояние кабинета закрыто", True)

st, _ = call("POST", "/api/moderator/login", {"password": PASS}, expect=200)
check("вход по паролю", True)
check("cookie выставлена", f"lf_mod_{SLUG}" in COOKIES)

_, mod = call("GET", "/api/moderator/state")
check("в кабинете весь список", len(mod["messages"]) == 2, str(len(mod["messages"])))
check("сводки нет: только всего и участников",
      set(mod["stats"].keys()) == {"total", "participants"}, str(mod["stats"]))

_, feed = call("GET", "/api/messages")
ids_in_feed = [m["id"] for m in feed["messages"]]
check("без модерации обе отправки в общей ленте", ids_in_feed == [mid2, mid1], str(ids_in_feed))

st, _ = call("POST", "/api/moderator/moderate", {"message_id": mid1, "action": "approve"}, expect=404)
check("эндпоинт модерации больше не существует", True)

_, me = call("GET", "/api/me")
mine = {m["id"]: m for m in me["messages"]}
check("автор видит свои отправки в эфире", mine[mid1]["status"] == "approved")

print("\n=== Порядок показа ===")
st, res = call("POST", "/api/messages", {"body": "Третье"}, expect=200)
mid3 = res["message"]["id"]
_, feed = call("GET", "/api/messages")
pos = {m["id"]: m["position"] for m in feed["messages"]}
check("порядок по позициям", pos[mid1] < pos[mid3], f"{pos}")

call("POST", "/api/moderator/move", {"message_id": mid3, "direction": "first"})
_, feed = call("GET", "/api/messages")
asc = sorted(feed["messages"], key=lambda m: m["position"])
check("«в начало» двигает наверх", asc[0]["id"] == mid3)

call("POST", "/api/moderator/move", {"message_id": mid3, "direction": "last"})
_, feed = call("GET", "/api/messages")
asc = sorted(feed["messages"], key=lambda m: m["position"])
check("«в конец» двигает вниз", asc[-1]["id"] == mid3)

call("POST", "/api/moderator/move", {"message_id": mid3, "direction": "first"})
_, feed = call("GET", "/api/messages")
asc = sorted(feed["messages"], key=lambda m: m["position"])
check("повторное «в начало» даёт тот же порядок", asc[0]["id"] == mid3)
call("POST", "/api/moderator/move", {"message_id": mid3, "direction": "up"})
_, feed = call("GET", "/api/messages")
asc = sorted(feed["messages"], key=lambda m: m["position"])
check("«вверх» с первого места ничего не ломает", asc[0]["id"] == mid3)
check("после перестановок все позиции уникальны",
      len({m["position"] for m in feed["messages"]}) == len(feed["messages"]),
      str([m["position"] for m in feed["messages"]]))

print("\n=== Экран ===")
_, res = call("POST", "/api/moderator/screen", {"action": "interval", "value": "5"})
check("интервал применён", res["screen"]["interval"] == 5, str(res["screen"]["interval"]))
_, res = call("POST", "/api/moderator/screen", {"action": "pause"})
check("пауза включилась", res["screen"]["playing"] is False)
_, res = call("POST", "/api/moderator/screen", {"action": "show", "value": str(mid1)})
check("показать конкретный слайд", res["screen"]["current_id"] == mid1)
_, res = call("POST", "/api/moderator/screen", {"action": "next"})
check("переключение вперёд", res["screen"]["current_id"] != mid1)
_, res = call("POST", "/api/moderator/screen", {"action": "play"})
check("воспроизведение продолжено", res["screen"]["playing"] is True)
st, _ = call("POST", "/api/moderator/screen", {"action": "ерунда"}, expect=400)
check("неизвестное действие отклонено", True)

print("\n=== Правка и удаление ===")
st, res = call("POST", "/api/moderator/edit",
               {"message_id": mid1, "body": "Исправленный текст", "author": "Аня П."})
edited = next((m for m in res["messages"] if m["id"] == mid1), None)
check("текст и имя правятся",
      edited and edited["body"] == "Исправленный текст" and edited["author"] == "Аня П.",
      str(edited and (edited["body"], edited["author"])))

st, res = call("POST", "/api/messages", {"body": "Фото для проверки"},
               files={"image": ("pic.png", png_bytes(3000, 2000), "image/png")}, expect=200)
mid4 = res["message"]["id"]
img = res["message"]["image"]

st, body = call("GET", f"/media/{img}")
check("фото отдаётся по /media как JPEG", st == 200 and bytes(body)[:2] == b"\xff\xd8",
      f"статус {st}")

_, res = call("POST", "/api/moderator/screen", {"action": "show", "value": str(mid1)})
check("показ помечает слайд как показанный", res["screen"]["current_id"] == mid1)
_, st_mod = call("GET", "/api/moderator/state")
shown_ids = [m["id"] for m in st_mod["messages"] if m["shown_at"]]
check("в состоянии видна отметка «показано»", mid1 in shown_ids, str(shown_ids))
_, res = call("POST", "/api/moderator/reset-shown", {})
check("«заново» сбросило отметки",
      all(not m["shown_at"] for m in res["messages"]),
      str([m["id"] for m in res["messages"] if m["shown_at"]]))

img2 = next((m["image"] for m in res["messages"]
             if m["id"] == mid2 and m["image"]), None)
check("у mid2 была фотография для проверки удаления файла", bool(img2))
st, _ = call("POST", "/api/moderator/delete", {"message_id": mid2})
_, feed = call("GET", "/api/messages")
check("удаление убирает из ленты", all(m["id"] != mid2 for m in feed["messages"]))
if img2:
    st, _ = call("GET", f"/media/{img2}")
    # 4.0: удаление из ленты больше не стирает оригинал. Фотография живёт
    # в архиве события, её забирает кнопка «сохранить и скачать все»
    check("файл удалённого из ленты сообщения остаётся в архиве",
          st == 200, str(st))
st, _ = call("GET", f"/media/{img}")
check("фото соседнего сообщения не пострадало", st == 200, str(st))

st, _ = call("GET", "/media/../app/db.py", expect=404)
check("обход каталога не проходит", True)

print("\n=== Формат ответа кабинета ===")
_, sent5 = call("POST", "/api/messages", {"body": "для проверки ответа"}, expect=200)
mid5 = sent5["message"]["id"]
_, feed5 = call("GET", "/api/messages")
check("новая отправка сразу в ленте", mid5 in [m["id"] for m in feed5["messages"]])
_, res = call("POST", "/api/moderator/delete", {"message_id": mid5})
check("после удаления список уменьшился",
      not any(m["id"] == mid5 for m in res["messages"]))

print("\n=== Приглашения и выход ===")
_, inv = call("GET", "/api/moderator/invite")
check("ссылка-приглашение содержит код", inv["code"] in inv["link"])
st, body = call("GET", "/api/qr.svg")
check("QR отдаётся как SVG", st == 200 and b"<svg" in bytes(body)[:200])

call("POST", "/api/moderator/rotate-invite")
_, inv2 = call("GET", "/api/moderator/invite")
check("код приглашения меняется", inv2["code"] != inv["code"])
st, _ = call("POST", "/api/join", {"name": "Старый QR", "invite": inv["code"]}, expect=403)
check("старый QR перестал работать", True)
st, _ = call("POST", "/api/join", {"name": "Новый гость", "invite": inv2["code"]}, expect=200)
check("новый QR работает", True)

call("POST", "/api/moderator/logout")
st, _ = call("GET", "/api/moderator/state", expect=403)
check("после выхода модератор закрыт", True)

print("\n=== Итог ===")
if failures:
    print(f"Провалено проверок: {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("Все проверки пройдены.")