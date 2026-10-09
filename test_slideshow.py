"""Проверка слайдшоу и архива фотографий (4.0).

Два обещания, которые дал интерфейс ведущему:

  * галочкой у фотографии она переезжает в отдельную галерею «слайдшоу»,
    оттуда её можно запустить на экран, и обычная лента на это время встаёт;
  * удаление фотографии из ленты **не стирает файл**: оригиналы живут в
    архиве, который можно забрать себе одним файлом или стереть целиком.

Проверяем и то, что обещание держится: файла нет после удаления из ленты не
должно быть, а после «удалить все фото» — должен быть.

Запуск: поднять сервер, затем
    python test_slideshow.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123").rstrip("/")
DATA_DIR = Path(os.environ.get("LF_DATA_DIR") or "data")
RUN = uuid.uuid4().hex[:6]

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        failures.append(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """Отдельный браузер: свой набор cookie. Ими же шлём запросы."""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )

    def _run(self, method: str, url: str, body: bytes | None, headers: dict):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get(self, path: str):
        url = path if path.startswith("http") else BASE + path
        return self._run("GET", url, None, {})

    def get_json(self, path: str):
        st, _, raw = self.get(path)
        try:
            return st, json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return st, None

    def post(self, path: str, fields: dict | None = None):
        url = path if path.startswith("http") else BASE + path
        data = urllib.parse.urlencode(fields or {}).encode()
        return self._run(
            "POST", url, data,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def post_json(self, path: str, fields: dict | None = None):
        st, _, raw = self.post(path, fields)
        try:
            return st, json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return st, None

    def post_file(self, path: str, fields: dict, filename: str, data: bytes,
                  mime: str = "image/jpeg"):
        url = path if path.startswith("http") else BASE + path
        boundary = "----lf4Xk9"
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n').encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        return self._run(
            "POST", url, body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


def jpeg_bytes(w: int = 320, h: int = 240) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h), (40, 80, 120))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def admin_password() -> str:
    with open(DATA_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)["admin_password"]


MOD_PASSWORD = "parol-vedushchego-4"

print("\n=== Подготовка: событие и вход ===")
admin = Client()
st, _ = admin.post_json("/api/admin/login", {"password": admin_password()})
check("админ вошёл", st == 200, str(st))

slug = f"slajdsho-{RUN}"
st, res = admin.post_json("/api/admin/events", {
    "title": f"Слайдшоу {RUN}",
    "password": MOD_PASSWORD,
    "slug": slug,
})
check("событие создано", st == 200 and bool((res or {}).get("event")), str(res)[:140])

cfg_path = DATA_DIR / "events" / slug / "config.json"
with open(cfg_path, encoding="utf-8") as f:
    invite = json.load(f)["invite_token"]

mod = Client()
st, _ = mod.post_json(f"/e/{slug}/api/moderator/login", {"password": MOD_PASSWORD})
check("ведущий вошёл", st == 200, str(st))

guest = Client()
st, _ = guest.post_json(f"/e/{slug}/api/join",
                        {"name": "Аня Петрова", "invite": invite})
check("гость вошёл по коду", st == 200, str(st))


def send_text(body: str = "") -> dict:
    st, res = guest.post_json(f"/e/{slug}/api/messages", {"body": body} if body else {})
    return (res or {}).get("message") or {}


def send_photo(body: str = "") -> dict:
    st, _, raw = guest.post_file(
        f"/e/{slug}/api/messages", {"body": body}, "foto.jpg", jpeg_bytes()
    )
    try:
        return json.loads(raw).get("message") or {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def screen_state() -> dict:
    # состояние раздаётся GET'ом: POST на него даёт 405 и пустой разбор,
    # из-за чего все проверки слайдшоу «падали» с None
    st, res = mod.get_json(f"/e/{slug}/api/moderator/state")
    return (res or {}).get("screen") or {}


try:
    print("\n=== Отправка: фото, фото с текстом, текст ===")
    p1 = send_photo()
    p2 = send_photo()
    p3 = send_photo("Вот мы и на свадьбе")
    only_text = send_text("просто текст")
    check("фото без текста принято", bool(p1.get("image")), str(p1)[:100])
    check("второе фото принято", bool(p2.get("image")))
    check("фото с текстом принято", bool(p3.get("image")) and bool(p3.get("body")))
    check("текст без фото принят", not only_text.get("image"))

    print("\n=== Галочка слайдшоу ===")
    scr = screen_state()
    check("в галерее пока пусто", scr.get("slideshow_total") == 0, str(scr.get("slideshow_total")))
    check("в состоянии есть флаг показа", scr.get("slideshow_visible") is False)

    st, res = mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                            {"message_id": p1["id"], "on": "1"})
    check("галочка ставится фотографии", st == 200, str(res)[:120])
    scr = screen_state()
    check("в галерее одна фотография", scr.get("slideshow_total") == 1,
          str(scr.get("slideshow_total")))

    st, res = mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                            {"message_id": only_text["id"], "on": "1"})
    check("текст без фото в слайдшоу не пускается", st == 400, f"статус {st}")
    detail = (res or {}).get("detail", "")
    check("ведущий получает понятное объяснение", "фотограф" in detail.lower(), detail[:140])
    check("после отказа галерея не изменилась",
          screen_state().get("slideshow_total") == 1)

    for m in (p2, p3):
        st, _ = mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                              {"message_id": m["id"], "on": "1"})
        check(f"галочка ставится ещё одной ({'с текстом' if m is p3 else 'чистой'})",
              st == 200, str(st))
    scr = screen_state()
    check("в галерее три снимка, включая фото с текстом",
          scr.get("slideshow_total") == 3, str(scr.get("slideshow_total")))

    print("\n=== Снять галочку — снимок уходит из галереи ===")
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                          {"message_id": p2["id"], "on": "0"})
    check("галочка снимается", st == 200, str(st))
    check("в галерее снова два", screen_state().get("slideshow_total") == 2)
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                          {"message_id": p2["id"], "on": "1"})
    check("и возвращается обратно", screen_state().get("slideshow_total") == 3)

    print("\n=== Запуск слайдшоу на экране ===")
    st, res = mod.post_json(f"/e/{slug}/api/moderator/screen",
                            {"action": "slideshow", "value": "1"})
    check("слайдшоу запускается", st == 200, str(res)[:140])
    scr = screen_state()
    gallery = {p1["id"], p2["id"], p3["id"]}
    check("экран переключился на слайдшоу", scr.get("slideshow_visible") is True)
    check("на экране снимок из галереи", scr.get("active_id") in gallery,
          str(scr.get("active_id")))
    check("и это не то, что было бы в обычной ленте",
          scr.get("active_id") is not None and scr.get("slideshow_current_id") == scr.get("active_id"))

    st, res = mod.post_json(f"/e/{slug}/api/moderator/screen",
                            {"action": "slideshow", "value": "0"})
    check("возврат в обычную ленту работает", st == 200)
    scr = screen_state()
    check("слайдшоу выключено", scr.get("slideshow_visible") is False)
    check("на экране снова слайд ленты", scr.get("active_id") == scr.get("current_id"))

    print("\n=== Пустую галерею не запускаем ===")
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/slideshow-clear", {})
    check("галерея очищается", st == 200, str(st))
    check("и действительно пуста", screen_state().get("slideshow_total") == 0)
    st, res = mod.post_json(f"/e/{slug}/api/moderator/screen",
                            {"action": "slideshow", "value": "1"})
    check("запуск пустого слайдшоу отклонён", st == 400, f"статус {st}")
    check("ведущему сказано, что галерея пуста",
          "фотограф" in (res or {}).get("detail", "").lower(),
          str((res or {}).get("detail"))[:140])
    check("экран остался в ленте", screen_state().get("slideshow_visible") is False)

    print("\n=== Файл переживает удаление из ленты ===")
    for m in (p1, p2, p3):
        mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                      {"message_id": m["id"], "on": "1"})

    kept = p1["image"]
    st, res = mod.post_json(f"/e/{slug}/api/moderator/delete",
                            {"message_id": p1["id"]})
    check("сообщение удаляется из ленты", st == 200, str(res)[:120])

    st, info = mod.get_json(f"/e/{slug}/api/moderator/photos")
    check("архив не сдулся после удаления из ленты",
          st == 200 and (info or {}).get("count", 0) >= 3, str(info)[:140])

    st, _, raw = guest.get(f"/e/{slug}/media/{kept}")
    check("файл всё ещё отдаётся по ссылке", st == 200, str(st))

    scr = screen_state()
    check("удалённый из ленты снимок ушёл и из галереи",
          scr.get("slideshow_total") == 2, str(scr.get("slideshow_total")))

    print("\n=== Скачивание архива ===")
    st, headers, raw = mod.get(f"/e/{slug}/api/moderator/photos.zip")
    check("архив скачивается", st == 200, str(st))
    check("отдаётся как zip", "zip" in headers.get("Content-Type", ""),
          headers.get("Content-Type", ""))
    check("браузеру предложено имя файла",
          f"photos-{slug}" in headers.get("Content-Disposition", ""),
          headers.get("Content-Disposition", ""))
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = zf.namelist()
        check("архив не битый", zf.testzip() is None)
        check("внутри есть фотографии", len(names) >= 3, str(len(names)))
        check("внутри папка с именем события",
              all(n.startswith(f"photos-{slug}/") for n in names), str(names[:2]))
        check("удалённая из ленты фотография тоже в архиве",
              any(kept in n for n in names), str(names[:4]))
    except zipfile.BadZipFile as e:
        check("архив не битый", False, str(e))

    print("\n=== Удаление всех фото ===")
    st, res = mod.post_json(f"/e/{slug}/api/moderator/photos-delete-all", {})
    check("кнопка отработала", st == 200, str(res)[:140])
    check("файлы стёрты", (res or {}).get("removed_files", 0) >= 3,
          str((res or {}).get("removed_files")))
    # p1 мы уже удалили из ленты выше, поэтому в базе осталось два снимка
    check("сообщения с фото убраны из ленты",
          (res or {}).get("removed_messages", 0) == 2,
          str((res or {}).get("removed_messages")))
    check("в ленте не осталось фотографий",
          not [m for m in (res or {}).get("messages", []) if m.get("image")])

    st, info = mod.get_json(f"/e/{slug}/api/moderator/photos")
    check("архив пуст", st == 200 and (info or {}).get("count") == 0, str(info)[:120])

    st, _, _ = guest.get(f"/e/{slug}/media/{kept}")
    check("файл больше не отдаётся", st == 404, str(st))

    scr = screen_state()
    check("слайдшоу после стирания пустое", scr.get("slideshow_total") == 0)
    st, feed = guest.get_json(f"/e/{slug}/api/messages")
    check("текстовое сообщение уцелело",
          any((m.get("body") or "").startswith("просто текст")
              for m in ((feed or {}).get("messages") or [])))

    print("\n=== Гость без ведущего ничего не может ===")
    st, _ = guest.post_json(f"/e/{slug}/api/moderator/photos-delete-all", {})
    check("гость не стирает архив", st in (401, 403), str(st))
    st, _ = guest.post_json(f"/e/{slug}/api/moderator/slideshow",
                            {"message_id": p3["id"], "on": "1"})
    check("гость не ставит галочки", st in (401, 403), str(st))
    st, _, _ = guest.get(f"/e/{slug}/api/moderator/photos.zip")
    check("гость не скачивает архив", st in (401, 403), str(st))
finally:
    admin.post(f"/api/admin/events/{slug}/delete", {})

print("\n=== Итог ===")
if failures:
    print(f"Провалено проверок: {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("Слайдшоу и архив фотографий работают.")