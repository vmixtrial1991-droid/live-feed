"""Проверка галереи слайдшоу и архива фотографий (4.0).

Три обещания, которые дал интерфейс ведущему:

  * галочкой у фотографии она переезжает в отдельную галерею «слайдшоу»,
    оттуда её можно запустить на экран, и обычная лента на это время встаёт;
  * **лента и галерея не связаны**: удаление сообщения из ленты не выбрасывает
    снимок из галереи, снятие галочки не трогает ленту;
  * **любое удаление оставляет файл на сервере**: убрали из ленты, сняли
    галочку, стёрли из архива — оригинал жив, его можно забрать кнопкой
    «сохранить и скачать все фотографии». Файл исчезает только от «удалить
    все фото» и от удаления события.

Второе обещание раньше не выполнялось: слайдшоу держался флагом in_slideshow
у строки ленты, и удаление строки уносило снимок из обоих мест сразу.

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
EVENT_DIR = cfg_path.parent

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

    print("\n=== Длительность слайдшоу своя, не как у ленты ===")
    scr = screen_state()
    base_feed = scr.get("interval")
    base_show = scr.get("slideshow_interval")
    check("в состоянии есть длительность ленты", isinstance(base_feed, int), str(base_feed))
    check("в состоянии есть длительность слайдшоу",
          isinstance(base_show, int), str(base_show))

    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "interval", "value": "7"})
    check("длительность ленты меняется", screen_state().get("interval") == 7)
    check("и слайдшоу от этого не тронута",
          screen_state().get("slideshow_interval") == base_show,
          str(screen_state().get("slideshow_interval")))

    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow-interval", "value": "40"})
    check("длительность слайдшоу меняется",
          screen_state().get("slideshow_interval") == 40,
          str(screen_state().get("slideshow_interval")))
    check("а лента осталась при своей",
          screen_state().get("interval") == 7,
          str(screen_state().get("interval")))

    # настройка должна пережить перезапуск показа, а не жить только в памяти
    with open(cfg_path, encoding="utf-8") as f:
        saved = json.load(f)
    check("длительность слайдшоу записана в настройки события",
          int(saved.get("slideshow_interval", 0)) == 40,
          str(saved.get("slideshow_interval")))
    check("и длительность ленты — тоже",
          int(saved.get("interval", 0)) == 7, str(saved.get("interval")))

    # границы: ползунокallow 3..120, а сервер не должен доверять мусору
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow-interval", "value": "1"})
    check("слишком малое значение поднято до минимума",
          screen_state().get("slideshow_interval") == 3,
          str(screen_state().get("slideshow_interval")))
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow-interval", "value": "9999"})
    check("слишком большое значение урезано до максимума",
          screen_state().get("slideshow_interval") == 600,
          str(screen_state().get("slideshow_interval")))

    # вернём к середине, чтобы дальше тест шёл быстро
    mod.post_json(f"/e/{slug}/api/moderator/screen",
                  {"action": "slideshow-interval", "value": "15"})
    mod.post_json(f"/e/{slug}/api/moderator/screen",
                  {"action": "interval", "value": "12"})

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

    # ГЛАВНОЕ: лента и галерея не связаны. Раньше слайдшоу был флагом у строки
    # ленты, и удаление строки уносило снимок из галереи вместе с флагом.
    scr = screen_state()
    check("удаление из ленты НЕ выбрасывает снимок из галереи",
          scr.get("slideshow_total") == 3, str(scr.get("slideshow_total")))
    st, state_now = mod.get_json(f"/e/{slug}/api/moderator/state")
    names = [g["image"] for g in (state_now or {}).get("gallery", [])]
    check("и снимок по-прежнему в галерее",
          kept in names, str(names)[:160])
    check("при этом из ленты он ушёл",
          all(m.get("id") != p1["id"] for m in (state_now or {}).get("messages", [])))

    # и наоборот: снятие галочки не трогает ленту
    st, state_now = mod.get_json(f"/e/{slug}/api/moderator/state")
    gal = (state_now or {}).get("gallery", [])
    victim = next(g for g in gal if g["image"] != kept)
    mod.post_json(f"/e/{slug}/api/moderator/gallery/drop", {"gallery_id": victim["id"]})
    st, after = mod.get_json(f"/e/{slug}/api/moderator/state")
    check("снятие из галереи уменьшает галерею",
          screen_state().get("slideshow_total") == 2,
          str(screen_state().get("slideshow_total")))
    check("и не трогает ленту",
          any(m.get("id") == victim.get("source_id") for m in (after or {}).get("messages", []))
          or victim["image"] in {m.get("image") for m in (after or {}).get("messages", [])},
          "сообщение с этим снимком должно остаться в ленте")
    st, _, raw = guest.get(f"/e/{slug}/media/{victim['image']}")
    check("файл снятого из галереи остался на сервере", st == 200, str(st))

    # вернём снимок в галерею, чтобы дальше счётчики были понятны
    for m in (p2, p3):
        mod.post_json(f"/e/{slug}/api/moderator/slideshow",
                      {"message_id": m["id"], "on": "1"})

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

    print("\n=== Правка в ленте тянет за собой текст в слайдшоу ===")
    # Снимок один, а надпись должна быть одна. Ведущий правит опечатку под
    # фотографией в ленте и ждёт, что на экране тоже поправится. Раньше
    # галерея хранила копию текста и жила своей жизнью.
    st, state0 = mod.get_json(f"/e/{slug}/api/moderator/state")
    gal0 = {g["image"]: g for g in (state0 or {}).get("gallery", [])}
    # берём тот снимок, чьё сообщение живо: выше p1 из ленты удалили, и
    # такой снимок проверяется отдельно — как осиротевший
    linked = next((g for g in gal0.values() if g.get("source_id")), None)
    check("галерея помнит, из какой строки ленты взят снимок",
          linked is not None, str(list(gal0.values())[:1])[:140])
    # а у осиротевшего снимка связи быть не должно: иначе подсказка в окне
    # правки обещала бы изменения в ленте, которой уже нет
    check("у снимка без строки в ленте связи не остаётся",
          all(not g.get("source_id") or g.get("source_id") in
              {m["id"] for m in (state0 or {}).get("messages", [])}
              for g in gal0.values()),
          str([(g["id"], g.get("source_id")) for g in gal0.values()]))

    if linked is not None:
        st, _ = mod.post_json(f"/e/{slug}/api/moderator/edit",
                              {"message_id": linked["source_id"],
                               "body": "опечатка исправлена"})
        check("правка в ленте принята", st == 200, str(st))
        st, after = mod.get_json(f"/e/{slug}/api/moderator/state")
        gal = {g["image"]: g for g in (after or {}).get("gallery", [])}
        check("и текст в галерее сразу стал новым",
              (gal.get(linked["image"]) or {}).get("body") == "опечатка исправлена",
              str((gal.get(linked["image"]) or {}).get("body")))
        check("в ленте тоже новый текст",
              any(m.get("id") == linked["source_id"]
                  and m.get("body") == "опечатка исправлена"
                  for m in (after or {}).get("messages", [])))

        # и обратно: правка из галереи правит строку ленты, а не копию
        st, _ = mod.post_json(f"/e/{slug}/api/moderator/gallery/edit",
                              {"gallery_id": linked["id"], "body": "из галереи"})
        st, back = mod.get_json(f"/e/{slug}/api/moderator/state")
        check("правка из галереи меняет и строку ленты",
              any(m.get("id") == linked["source_id"] and m.get("body") == "из галереи"
                  for m in (back or {}).get("messages", [])),
              str([m.get("body") for m in (back or {}).get("messages", [])][:4]))
        gal = {g["image"]: g for g in (back or {}).get("gallery", [])}
        check("и сам снимок в галереи не разошёлся с лентой",
              (gal.get(linked["image"]) or {}).get("body") == "из галереи")

    # снимок, чьё сообщение удалили, правится сам по себе
    st, orphan_state = mod.get_json(f"/e/{slug}/api/moderator/state")
    orphan = next((g for g in (orphan_state or {}).get("gallery", [])
                   if not g.get("source_id")), None)
    if orphan is not None:
        st, _ = mod.post_json(f"/e/{slug}/api/moderator/gallery/edit",
                              {"gallery_id": orphan["id"], "body": "только тут"})
        st, o2 = mod.get_json(f"/e/{slug}/api/moderator/state")
        gal = {g["image"]: g for g in (o2 or {}).get("gallery", [])}
        check("снимок без строки в ленте правится сам по себе",
              (gal.get(orphan["image"]) or {}).get("body") == "только тут")

    print("\n=== У слайдшоу своя пауза ===")
    scr = screen_state()
    check("галерея идёт сразу после запуска",
          scr.get("slideshow_playing") is True, str(scr.get("slideshow_playing")))
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow-play", "value": "0"})
    check("пауза галереи принята", st == 200, str(st))
    check("галерея на паузе",
          screen_state().get("slideshow_playing") is False,
          str(screen_state().get("slideshow_playing")))
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "pause", "value": "1"})
    check("пауза ленты галерею не трогает",
          screen_state().get("slideshow_playing") is False,
          str(screen_state().get("slideshow_playing")))
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "pause", "value": "0"})
    check("и обратно: лента не трогает паузу галереи",
          screen_state().get("slideshow_playing") is False,
          str(screen_state().get("slideshow_playing")))

    # «Запустить слайдшоу» означает «показать сейчас»: сняли с паузы и
    # перезапустили — галерея поехала
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow", "value": "1"})
    check("перезапуск включает активный показ",
          screen_state().get("slideshow_playing") is True,
          str(screen_state().get("slideshow_playing")))
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow-play", "value": "0"})
    check("снова сняли с паузы", screen_state().get("slideshow_playing") is False)
    st, _ = mod.post_json(f"/e/{slug}/api/moderator/screen",
                          {"action": "slideshow", "value": "1"})
    check("и «запустить слайдшоу» паузу снял", st == 200, str(st))
    check("после запуска галерея крутится",
          screen_state().get("slideshow_playing") is True,
          str(screen_state().get("slideshow_playing")))

    st, _ = guest.post_json(f"/e/{slug}/api/moderator/screen",
                            {"action": "slideshow-play", "value": "0"})
    check("гость не может остановить галерею", st in (401, 403), str(st))

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

    check("галерея тоже очищена",
          (res or {}).get("removed_gallery", 0) >= 2,
          str((res or {}).get("removed_gallery")))
    scr = screen_state()
    check("слайдшоу после стирания пустое", scr.get("slideshow_total") == 0,
          str(scr.get("slideshow_total")))
    check("и слайдшоу выключено, а не показывает битые картинки",
          scr.get("slideshow_visible") is False)
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

print("\n=== Удаление события уносит фотографии с собой ===")
# обещание из требований: «при удалении события все фотографии тоже
# стираются». Событие мы стёрли в finally — проверяем, что папки не осталось
check("папка события удалена с диска", not EVENT_DIR.exists(), str(EVENT_DIR))

print("\n=== Итог ===")
if failures:
    print(f"Провалено проверок: {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("Слайдшоу и архив фотографий работают.")