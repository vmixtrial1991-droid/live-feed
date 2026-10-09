"""Быстрая проверка админ-панели глазами: страницы, создание, QR, удаление.

Всё делается одним «браузером» с cookie, иначе /admin отдаст форму входа
и создать событие не получится.

Запуск: python check_admin_page.py [http://127.0.0.1:8123]
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123").rstrip("/")
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def get(path: str) -> tuple[int, str]:
    try:
        with opener.open(BASE + path, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def post(path: str, fields: dict) -> tuple[int, bytes]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


print("\n=== Страница входа ===")
st, html = get("/admin/login")
check("открывается", st == 200, str(st))
check("есть поле пароля", 'id="passInput"' in html)
check("говорит про ADMIN_PASSWORD", "ADMIN_PASSWORD" in html)

print("\n=== Вход и панель ===")
password = json.load(open("data/config.json", encoding="utf-8"))["admin_password"]
st, _ = post("/api/admin/login", {"password": password})
check("вход по паролю", st == 200, str(st))

st, html = get("/admin")
check("панель открывается", st == 200, str(st))
check("форма создания события", 'id="createForm"' in html)
check("поле названия", 'id="fTitle"' in html)
check("поле пароля ведущего", 'id="fPassword"' in html)
check("карточка события", 'id="detail"' in html)
check("кнопка удаления", 'id="dDelete"' in html)
check("подключён admin.js", "admin.js" in html)

print("\n=== Создание события ===")
# адрес уникальный: проверка может идти по серверу, где такое событие уже есть
import uuid  # noqa: E402

mark = uuid.uuid4().hex[:6]
st, body = post("/api/admin/events", {"title": f"Проверка {mark}", "password": "proverka-1"})
res = json.loads(body or b"{}")
slug = (res.get("event") or {}).get("slug")
check("событие создано", st == 200 and bool(slug), str(slug))
check("адрес латиницей", slug == f"proverka-{mark}", str(slug))
check("три ссылки плюс витрина", len(res.get("links") or {}) == 4, str(list((res.get("links") or {}).keys())))
check("ссылка ведущего", f"/e/{slug}/moderator" in res["links"]["moderator"])
check("ссылка экрана", f"/e/{slug}/screen" in res["links"]["screen"])
check("QR с кодом", f"/e/{slug}/join?t=" in res["links"]["join"])

print("\n=== Карточка и QR ===")
st, body = get(f"/api/admin/events/{slug}")
check("карточка отдаётся", st == 200, str(st))
st, body = get(f"/api/admin/events/{slug}/qr.svg")
svg = body if isinstance(body, str) else ""
check("QR отдаётся как SVG", st == 200 and "<svg" in svg, str(st))

print("\n=== Список событий ===")
st, html = get("/admin")
check("панель по-прежнему открыта", st == 200, str(st))

bogus = urllib.parse.quote("нет-такого")
st, body = get(f"/api/admin/events/{bogus}")
check("карточка чужого события — 404", st == 404, str(st))

print("\n=== Удаление ===")
st, body = post(f"/api/admin/events/{slug}/delete", {})
check("удаление прошло", st == 200, str(st))
st, _ = get(f"/e/{slug}/")
check("страницы события исчезли", st == 404, str(st))
st, _ = get(f"/api/admin/events/{slug}/qr.svg")
check("QR больше не отдаётся", st == 404, str(st))

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Админ-панель работает.")