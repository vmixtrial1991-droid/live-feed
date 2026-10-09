"""Меряет, сколько места занимает текст и сколько картинка в одном сообщении.

Считаем три вещи:
  * на диске — байты, которые реально лежат в базе и в папке загрузок;
  * в самой строке базы — длина текста против длины всей записи;
  * в разметке показа — какую долю кадра занимает текст, а какую фото.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from testlib import prepare

ROOT = os.environ.get("LF_TEST_ROOT", "http://127.0.0.1:8123").rstrip("/")

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def send(path: str, fields: dict):
    req = urllib.request.Request(
        ROOT + path, data=urllib.parse.urlencode(fields).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read())


def send_photo(path: str, body: str, data: bytes, filename: str) -> dict:
    boundary = "----measure7f"
    payload = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="body"\r\n\r\n',
        body.encode() + b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(ROOT + path, data=payload, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read())


def phone_photo(width: int, height: int) -> bytes:
    """Похоже на снимок с телефона: немного шума, чтобы не сжималось идеально."""
    import random

    from PIL import Image

    random.seed(width * 31 + height)
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            c = (
                120 + random.randint(-45, 45),
                120 + random.randint(-45, 45),
                140 + random.randint(-45, 45),
            )
            for dy in range(4):
                for dx in range(4):
                    if x + dx < width and y + dy < height:
                        px[x + dx, y + dy] = c
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def main() -> None:
    keep = "--keep" in sys.argv
    ctx = prepare("Замер текста и фото")
    slug = ctx.slug
    if keep:
        # событие нужно для осмотра в браузере — снимаем его с уборки
        import testlib

        if slug in testlib._created:
            testlib._created.remove(slug)
        print(f"событие оставлено: {slug}")
    send(f"/e/{slug}/api/moderator/login", {"password": ctx.mod_password})
    send(f"/e/{slug}/api/join", {"name": "Аня", "invite": ctx.code})

    text = "Поздравляю с днём рождения, пусть всё сложится!"
    print(f"Текст: {len(text)} символов, {len(text.encode('utf-8'))} байт в UTF-8\n")

    cases = [
        ("снимок 12 Мп, 4032x3024", 4032, 3024),
        ("снимок 8 Мп, 3264x2448", 3264, 2448),
        ("снимок 2 Мп, 1920x1440", 1920, 1440),
        ("скриншот 1440x2560", 1440, 2560),
    ]

    ev = Path("data/events") / slug
    rows = []
    for label, w, h in cases:
        raw = phone_photo(w, h)
        res = send_photo(f"/e/{slug}/api/messages", text, raw, "photo.jpg")
        msg = res["message"]
        stored = ev / "uploads" / msg["image"]
        size = stored.stat().st_size

        with sqlite3.connect(ev / "app.db") as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM messages WHERE id = ?", (msg["id"],)).fetchone()
        row_len = len(sqlite3.Binary(b"\0" * 0)) if False else None
        # сколько байт в самой строке базы: текст плюс всё остальное
        with sqlite3.connect(ev / "app.db") as c:
            body_bytes = c.execute(
                "SELECT length(body), length(author), length(status), "
                "length(coalesce(image,'')), length(coalesce(shown_at,'')), "
                "length(coalesce(created_at,'')), id, position "
                "FROM messages WHERE id = ?", (msg["id"],)).fetchone()

        text_share = len(text.encode("utf-8")) / (len(text.encode("utf-8")) + size)
        rows.append((label, raw and len(raw), size, text_share, msg["id"]))
        print(f"{label:26} исходник {len(raw)/1024:8.0f} КБ   "
              f"на сервере {size/1024:7.0f} КБ   "
              f"текст {text_share*100:6.2f} %   картинка {(1-text_share)*100:6.2f} %")

    print()
    print("=== Что лежит в строке базы ===")
    with sqlite3.connect(ev / "app.db") as c:
        for row in c.execute(
                "SELECT id, length(body), length(author), length(coalesce(image,'')) "
                "FROM messages ORDER BY id"):
            mid, lb, la, li = row
            print(f"  сообщение {mid}: текст {lb} байт, автор {la}, имя файла {li}")
        print()
        page = c.execute("PRAGMA page_size").fetchone()[0]
        pages = c.execute("PRAGMA page_count").fetchone()[0]
        print(f"  размер страницы базы: {page} байт, страниц {pages}, "
              f"итого {page*pages/1024:.0f} КБ на все сообщения")

    print()
    print("=== Что занимает папка события ===")
    total = sum(f.stat().st_size for f in ev.rglob("*") if f.is_file())
    up = sum(f.stat().st_size for f in (ev / "uploads").rglob("*") if f.is_file())
    print(f"  всё целиком:      {total/1024:8.0f} КБ")
    print(f"  из этого фотографии: {up/1024:8.0f} КБ "
          f"({up*100/max(total,1):.1f} %)")
    print(f"  база:            {(ev/'app.db').stat().st_size/1024:8.0f} КБ")
    print(f"  конфиг:          {(ev/'config.json').stat().st_size/1024:8.0f} КБ")

    print()
    print(f"=== Одно сообщение целиком (текст {len(text)} символов + фото) ===")
    avg = sum(r[2] for r in rows) / len(rows)
    tb = len(text.encode("utf-8"))
    print(f"  фото в среднем:   {avg/1024:8.0f} КБ")
    print(f"  текст:            {tb:8d} байт")
    print(f"  итого:            {(avg+tb)/1024:8.1f} КБ")
    print(f"  доля текста:      {tb*100/(avg+tb):8.3f} %")
    print(f"  доля картинки:    {avg*100/(avg+tb):8.3f} %")


if __name__ == "__main__":
    main()