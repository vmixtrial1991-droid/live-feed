"""Готовит демо-событие с текстами разной длины и фото разного размера.

Нужно, чтобы посмотреть в браузере, как ведёт себя показ: короткий текст
должен вырасти, длинный — ужаться, фото — занять остаток, и всё должно
остаться внутри безопасной зоны при любом разрешении.

Запуск при поднятом сервере: python demo_setup.py
Убрать: python demo_setup.py --remove <адрес-события>
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from testlib import admin_post

ROOT = os.environ.get("LF_TEST_ROOT", "http://127.0.0.1:8123").rstrip("/")
DEMO_PASSWORD = "654321"
LIMIT = 56

# Тексты от самого короткого до предельного, и обязательно многострочный.
TEXTS = [
    "Да!",
    "С днём рождения!",
    "Пусть исполнятся все желания",
    "Лучший вечер в году",
    "Спасибо за тёплые слова, очень приятно",
    "Желаю счастья, здоровья\nи много смеха",
]

# Размеры подобраны так, чтобы были видны все случаи: крошечное фото,
# которое надо увеличивать, вертикальное, крупное и очень вытянутое.
# text — подпись госта к снимку: пустая строка означает «фото без текста»,
# их в галерее тоже полезно иметь, чтобы было видно разницу.
# slide — ставить ли снимок в галерею слайдшоу сразу при создании демо.
PHOTOS = [
    dict(w=140,  h=105,  colour=(220, 60, 90),  label="140x105",
         text="", slide=True),
    dict(w=480,  h=640,  colour=(60, 140, 90),  label="480x640",
         text="Вот мы и на свадьбе", slide=True),
    dict(w=900,  h=300,  colour=(200, 160, 40), label="900x300",
         text="", slide=False),
    dict(w=3000, h=1800, colour=(90, 90, 200),  label="3000x1800",
         text="Тот самый закат", slide=True),
    dict(w=1024, h=768,  colour=(30, 110, 120), label="1024x768",
         text="", slide=False),
]


def png(width: int, height: int, colour: tuple[int, int, int], text: str) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), colour)
    draw = ImageDraw.Draw(img)
    # полосы видно и на крошечном фото — сразу понятно, что его растянули
    step = max(1, height // 14)
    for y in range(0, height, step):
        draw.rectangle([0, y, width, y + max(1, step // 3)],
                       fill=tuple(min(255, c + 25) for c in colour))
    draw.text((width // 10, height // 2), text, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def event_token(slug: str) -> str:
    with open(os.path.join("data", "events", slug, "config.json"),
              encoding="utf-8") as f:
        return json.load(f)["invite_token"]


def send(opener, url: str, fields: bytes, content_type: str = "") -> dict:
    """Запрос от имени браузера: cookie подставляет сам opener.

    Через urlopen нельзя — он не знает про банку cookie, и все запросы
    уходят без входа.
    """
    req = urllib.request.Request(url, data=fields, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with opener.open(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"raw": raw[:120].decode("utf-8", "replace")}


def form(fields: list[tuple[str, str | bytes, str | None]]) -> tuple[bytes, str]:
    """Собирает multipart из полей. Третье значение — имя файла, если есть."""
    boundary = "----demo3f6c"
    parts: list[bytes] = []
    for name, value, filename in fields:
        head = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            head += f'; filename="{filename}"'
        parts.append(f"--{boundary}\r\n{head}\r\n\r\n".encode())
        parts.append(value if isinstance(value, bytes) else str(value).encode())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--remove":
        print("удалено:", admin_post(f"/api/admin/events/{sys.argv[2]}/delete", {}))
        return

    st, created = admin_post("/api/admin/events",
                             {"title": "Демо показа", "password": DEMO_PASSWORD})
    if st != 200:
        raise SystemExit(f"не удалось завести событие: {st} {created}")

    slug = created["event"]["slug"]
    code = event_token(slug)

    def new_browser():
        """Отдельный набор cookie — отдельный гость.

        Cookie участника одна на событие, поэтому три входа подряд в одном
        браузере оставили бы автора последнего: в галерее у всех снимков
        было бы одно и то же имя.
        """
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    mod = new_browser()
    send(mod, f"{ROOT}/e/{slug}/api/moderator/login",
         urllib.parse.urlencode({"password": DEMO_PASSWORD}).encode(),
         "application/x-www-form-urlencoded")

    fields, ctype = form([("image", png(1920, 1080, (26, 32, 58), "ZASTAVKA"), "z.png")])
    print("заставка:", send(mod, f"{ROOT}/e/{slug}/api/moderator/splash", fields, ctype))

    # по браузеру на гостя: в галерее видно разных авторов, и заодно
    # проверяется длинное имя — подпись переносится на две строки
    guests = {}
    for who in ("Аня", "Игорь", "Александра Константиновна Петрова"):
        g = new_browser()
        send(g, f"{ROOT}/e/{slug}/api/join",
             urllib.parse.urlencode({"name": who, "invite": code}).encode(),
             "application/x-www-form-urlencoded")
        guests[who] = g
    who_list = list(guests)
    opener = guests[who_list[0]]

    # тексты без фото — на них смотреть рост кегля
    for i, t in enumerate(TEXTS):
        assert len(t) <= LIMIT, f"текст длиннее предела: {len(t)}"
        res = send(guests[who_list[i % len(who_list)]],
                   f"{ROOT}/e/{slug}/api/messages",
                   urllib.parse.urlencode({"body": t}).encode(),
                   "application/x-www-form-urlencoded")
        who = (res.get("message") or {}).get("author", "?")
        print(f"текст {len(t):>3} симв. от {who[:18]:<18}: {res.get('message', res)}")

    # фото разного размера: часть с текстом, часть без
    gallery = []
    for i, ph in enumerate(PHOTOS):
        body = ph["text"] if ph["text"] else f"Фото {ph['label']}"
        fields, ctype = form([
            ("body", body, None),
            ("image", png(ph["w"], ph["h"], ph["colour"], ph["label"]), f"foto{i}.png"),
        ])
        # снимки от разных гостей: в галерее сразу видно, что они разные
        res = send(guests[who_list[i % len(who_list)]],
                   f"{ROOT}/e/{slug}/api/messages", fields, ctype)
        mid = (res.get("message") or {}).get("id")
        kind = "фото с текстом" if ph["text"] else "фото без текста"
        print(f"фото {ph['label']:>10} ({kind}): {(res.get('message') or {}).get('image') or res}")
        if ph["slide"] and mid:
            gallery.append(mid)

    # наполняем галерею слайдшоу, чтобы вкладка открылась не пустой
    for mid in gallery:
        send(mod, f"{ROOT}/e/{slug}/api/moderator/slideshow",
             urllib.parse.urlencode({"message_id": mid, "on": "1"}).encode(),
             "application/x-www-form-urlencoded")
    print(f"в галерею слайдшоу добавлено снимков: {len(gallery)}")

    print()
    print(f"код события: {slug}")
    print(f"код гостей:  {code}")
    print(f"пароль:      {DEMO_PASSWORD}")
    print()
    print(f"кабинет  {ROOT}/e/{slug}/moderator")
    print(f"экран    {ROOT}/e/{slug}/screen")
    print(f"гость    {ROOT}/e/{slug}/send?t={code}")
    print()
    print("что смотреть в кабинете:")
    print("  вкладка «Лента»     — галочка напротив фотографии; у текстовых")
    print("                          строк её нет и быть не может")
    print("  вкладка «Слайдшоу»   — галерея уже наполнена, есть «убрать из")
    print("                          слайдшоу» и «показать сейчас»")
    print("  «Запустить слайдшоу» — экран занимает галерея, обычная лента")
    print("                          стоит; вернуть её — та же кнопка")
    print("  «Архив фотографий»   — скачать все одним файлом или стереть")
    print()
    print(f"убрать:  python demo_setup.py --remove {slug}")


main()