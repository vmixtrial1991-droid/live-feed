"""Проверка многособытийности: изоляция, доступ, удаление.

Главное, ради чего всё затевалось:
  * два события работают одновременно и не видят сообщений друг друга;
  * у события свой код приглашения, свой пароль ведущего и свой QR;
  * после удаления от события не остаётся ничего.

Запуск: поднять сервер, затем
    python test_events.py [http://127.0.0.1:8123]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123").rstrip("/")
DATA_DIR = Path(os.environ.get("LF_DATA_DIR") or "data")

# Проверка может идти по живому серверу с чужими событиями, поэтому адреса
# делаем уникальными: иначе следующий прогон споткнулся бы о наши же
# следы и проверка «одинаковое название не collide» стала бы ложной
RUN = uuid.uuid4().hex[:6]

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
            urllib.request.HTTPCookieProcessor(self.jar),
            _NoRedirect(),
        )

    def request(self, method: str, path: str, fields: dict | None = None):
        url = path if path.startswith("http") else BASE + path
        data = None
        headers = {}
        if fields is not None:
            data = urllib.parse.urlencode(fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, fields: dict | None = None):
        return self.request("POST", path, fields if fields is not None else {})

    def get_json(self, path: str):
        st, _, body = self.get(path)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def post_json(self, path: str, fields: dict):
        st, _, body = self.post(path, fields)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "status": st, "raw": body[:200].decode("utf-8", "replace")}

    def cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def set_cookie(self, name: str, value: str) -> None:
        self.jar.set_cookie(_raw_cookie(name, value))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _raw_cookie(name: str, value: str):
    import http.cookiejar

    c = http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain="", domain_specified=False, domain_initial_dot=False, path="/",
        path_specified=True, secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )
    return c


def admin_password() -> str:
    with open(DATA_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)["admin_password"]


# --------------------------------------------------------------------------
print("\n=== Вход в админку ===")
PASSWORD = admin_password()
admin = Client()

st, _, _ = admin.get("/api/admin/events")
check("без входа список событий закрыт", st in (401, 403), str(st))

st, _, _ = admin.post("/api/admin/login", {"password": "точно неверно"})
check("неверный пароль отклонён", st == 401, str(st))

st, _, _ = admin.post("/api/admin/login", {"password": PASSWORD})
check("вход по паролю", st == 200, str(st))
check("cookie админа выставлена", admin.cookie("lf_admin") is not None)
check("cookie админа не пустая", bool(admin.cookie("lf_admin")))
check("cookie не является простой отметкой", admin.cookie("lf_admin") != "1")

# --------------------------------------------------------------------------
print("\n=== Создание события ===")
base_slug = f"prazdnik-{RUN}"
res = admin.post_json("/api/admin/events", {
    "title": f"Свадьба Маши и Саши {RUN}",
    "subtitle": "Отправьте фото",
    "password": "parol-vedushchego",
    "slug": base_slug,
})
slug = (res.get("event") or {}).get("slug")
check("событие создано", res.get("ok") is True and bool(slug), str(res)[:120])
check("адрес взят из названия латиницей", slug == base_slug, str(slug))

# транслитерация проверяем на названии без явного адреса
auto = admin.post_json("/api/admin/events", {"title": f"Свадьба Маши и Саши {RUN}"})
auto_slug = (auto.get("event") or {}).get("slug", "")
check("название переведено в латиницу",
      auto_slug.startswith("svadba-mashi-i-sashi-"), auto_slug)
slug_auto = auto_slug

links = res.get("links") or {}
check("три ссылки в ответе", all(links.get(k) for k in ("join", "screen", "moderator")))
check("ссылка ведущего содержит адрес события",
      f"/e/{slug}/moderator" in links.get("moderator", ""), links.get("moderator", ""))
check("ссылка экрана содержит адрес события",
      f"/e/{slug}/screen" in links.get("screen", ""), links.get("screen", ""))
check("QR ведёт на вход с кодом",
      f"/e/{slug}/join?t=" in links.get("join", ""), links.get("join", ""))
check("ссылка гостям сразу содержит код события",
      links.get("feed", "") == links.get("join", ""), links.get("feed", ""))

detail = admin.get_json(f"/api/admin/events/{slug}") or {}
code_a = detail.get("code", "")
check("код приглашения выдан", len(code_a) == 8, code_a)
check("пароль ведущего показан в карточке", detail.get("password") == "parol-vedushchego")

res = admin.post_json("/api/admin/events", {"title": "Дубль", "slug": base_slug})
slug2 = (res.get("event") or {}).get("slug")
check("занятый адрес получил суффикс", slug2 == f"{base_slug}-2", str(slug2))

res = admin.post_json("/api/admin/events", {"title": "День рождения", "password": "123"})
check("короткий пароль отклонён", res.get("ok") is not True, str(res)[:100])
res = admin.post_json("/api/admin/events", {"title": "", "password": "abcdef"})
check("пустое название отклонено", res.get("ok") is not True, str(res)[:100])
res = admin.post_json("/api/admin/events", {"title": "Юбилей", "slug": "admin"})
check("занятый адрес приложения отклонён", res.get("ok") is not True, str(res)[:100])
res = admin.post_json("/api/admin/events", {"title": "Юбилей", "slug": "Плохой Адрес"})
check("русский адрес отклонён", res.get("ok") is not True, str(res)[:100])

res = admin.post_json("/api/admin/events", {
    "title": f"Корпоратив {RUN}", "password": "parol-vedushchego", "slug": f"korpor-{RUN}",
})
slug3 = (res.get("event") or {}).get("slug")
check("третье событие создано", slug3 == f"korpor-{RUN}", str(slug3))

detail3 = admin.get_json(f"/api/admin/events/{slug3}") or {}
code_c = detail3.get("code", "")
check("у разных событий разные коды приглашения", code_a and code_c and code_a != code_c,
      f"{code_a} и {code_c}")

# --------------------------------------------------------------------------
print("\n=== Страницы события ===")
anon = Client()
# витрины в списке нет: она закрыта для всех, кроме администратора
# и ведущего, проверяется это отдельным разделом ниже
for path, name in (
    (f"/e/{slug}/screen", "экран"),
    (f"/e/{slug}/join?t={code_a}", "вход по QR"),
    (f"/e/{slug}/moderator", "кабинет ведущего"),
):
    st, _, body = anon.get(path)
    check(f"{name} открывается", st == 200, str(st))
    if st == 200 and b"window.LF" in body:
        check(f"  {name} знает свой адрес", f"/e/{slug}".encode() in body)
        break

st, _, _ = anon.get("/e/" + urllib.parse.quote("нетакого") + "/")
check("чужое событие — 404", st == 404, str(st))

st, _, body = anon.get(f"/e/{slug}/join?t=" + urllib.parse.quote("НЕВЕРНЫЙКОД"))
check("вход по чужому коду отдаёт страницу входа, а не витрину",
      st == 200 and b"joinForm" in body, str(st))
check("и витрины на ней нет", st == 200 and b"feed.js" not in body, str(st))

# Гость без сессии, пришедший по ссылке на отправку, обязан попасть на
# страницу входа, а не на витрину. Раньше код приглашения терялся на
# редиректе: /send?t=КОД → /join (без кода) → витрина. То есть сервер
# сам выкидывал отправителя на витрину — ровно то, что мы убираем.
print("\n=== Отправитель не должен попадать на витрину ===")
# Гость без сессии, пришедший по ссылке на отправку, обязан дойти до входа,
# а не до витрины. Раньше код приглашения терялся на редиректе:
# /send?t=КОД → /join (без кода) → витрина. То есть сервер сам выкидывал
# отправителя на витрину — ровно то, что мы убираем кнопкой.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _page_body(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.read()


def hop(url: str) -> tuple[int, str]:
    """Один шаг без следования редиректам: (код, куда ведёт)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(urllib.request.Request(url), timeout=20)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


status, where = hop(f"{BASE}/e/{slug}/send?t={code_a}")
check("страница отправки с кодом ведёт на вход", status in (302, 307), str(status))
check("и ведёт именно на вход, а не на витрину",
      "/join" in where, str(where))
check("код приглашения не теряется на редиректе",
      f"t={code_a}" in where, str(where))

status2, _ = hop(BASE + where)
check("а по этому адресу открывается страница входа", status2 == 200, str(status2))
check("и это не витрина, а форма входа",
      status2 == 200 and b"sendForm" not in _page_body(BASE + where),
      "на странице входа не должно быть ленты чужих сообщений")

status3, where3 = hop(f"{BASE}/e/{slug}/send")
check("отправка без кода и сессии тоже не ведёт на витрину",
      "/join" in where3 or status3 == 200, f"{status3} {where3}")

# --------------------------------------------------------------------------
print("\n=== Отправка и изоляция ===")
def join_guest(client: Client, slug: str, code: str, name: str) -> bool:
    st, _, _ = client.post(f"/e/{slug}/api/join", {"name": name, "invite": code})
    return st == 200


guest_a = Client()
guest_b = Client()
check("гость вошёл в первое событие", join_guest(guest_a, slug, code_a, "Аня"))
check("гость вошёл в второе событие", join_guest(guest_b, slug3, code_c, "Борис"))

# сессия гостя не должна работать в чужом событии
st, _, _ = guest_a.post(f"/e/{slug3}/api/messages", {"body": "не должно пройти"})
check("сессия одного события не работает в другом", st == 401, str(st))

st, _, body = guest_a.post(f"/e/{slug}/api/messages", {"body": "Сообщение первого"})
check("сообщение принято", st == 200, str(st))
st, _, body = guest_b.post(f"/e/{slug3}/api/messages", {"body": "Сообщение второго"})
check("сообщение принято во втором", st == 200, str(st))

feed_a = guest_a.get_json(f"/e/{slug}/api/messages") or {}
feed_b = guest_b.get_json(f"/e/{slug3}/api/messages") or {}
bodies_a = [m["body"] for m in feed_a.get("messages", [])]
bodies_b = [m["body"] for m in feed_b.get("messages", [])]
check("в первом событии виден только его текст", bodies_a == ["Сообщение первого"], str(bodies_a))
check("во втором — только его", bodies_b == ["Сообщение второго"], str(bodies_b))
check("в первом событии один участник", feed_a.get("stats", {}).get("participants") == 1,
      str(feed_a.get("stats")))
check("во втором событии один участник", feed_b.get("stats", {}).get("participants") == 1,
      str(feed_b.get("stats")))

print("\n=== Отправителю витрина не показывается ===")
st, headers, _ = guest_a.get(f"/e/{slug}/")
check("гость уведён на отправку, а не на ленту", st in (301, 302, 303, 307), str(st))
location = headers.get("Location", "")
check("адрес перехода — страница отправки", location.endswith(f"/e/{slug}/send"),
      location or "—")

# Витрина закрыта для всех, кроме администратора и ведущего. Раньше
# сессия проверялась только у отправителя, и постороннему лента
# показывалась целиком.
st, headers, _ = anon.get(f"/e/{slug}/")
check("постороннему витрина не отдаётся", st in (301, 302, 303, 307), str(st))
check("и уводит на страницу входа",
      headers.get("Location", "").endswith(f"/e/{slug}/join"),
      headers.get("Location", ""))

st, headers, _ = Client().get(f"/e/{slug}/")
check("новому посетителю витрина не отдаётся", st in (301, 302, 303, 307), str(st))

st, headers, _ = anon.get(f"/e/{slug}/?t={code_a}")
check("код с витрины доезжает до входа",
      f"t={code_a}" in headers.get("Location", ""), headers.get("Location", ""))

st, _, _ = guest_a.post("/e/" + urllib.parse.quote("нетакого") + "/api/messages", {"body": "x"})
check("отправка в несуществующее событие — 404", st == 404, str(st))

# --------------------------------------------------------------------------
print("\n=== Кабинет ведущего ===")
lead_a = Client()
lead_c = Client()

st, _, _ = lead_a.post(f"/e/{slug}/api/moderator/login", {"password": "неверно"})
check("неверный пароль ведущего отклонён", st == 401, str(st))

st, _, _ = lead_a.post(f"/e/{slug}/api/moderator/login", {"password": "parol-vedushchego"})
check("вход ведущего в своё событие", st == 200, str(st))
check("cookie ведущего привязана к событию", lead_a.cookie(f"lf_mod_{slug}") is not None)

st, _, _ = lead_a.get(f"/e/{slug}/api/moderator/state")
check("кабинет своего события открыт", st == 200, str(st))

st, _, _ = lead_a.get(f"/e/{slug}/")
check("ведущий витрину своего события видит", st == 200, str(st))

st, _, _ = lead_a.get(f"/e/{slug3}/api/moderator/state")
check("в чужое событие кабинет не пускает", st in (401, 403), str(st))

st, _, _ = lead_c.post(f"/e/{slug3}/api/moderator/login", {"password": "parol-vedushchego"})
check("ведущий вошёл в своё событие", st == 200, str(st))
check("у него cookie именно его события", lead_c.cookie(f"lf_mod_{slug3}") is not None)
check("cookie чужого события у него нет", lead_c.cookie(f"lf_mod_{slug}") is None)

# подделка cookie: в прошлой версии проверялось только наличие
forged = Client()
forged.set_cookie(f"lf_mod_{slug}", "1")
st, _, _ = forged.get(f"/e/{slug}/api/moderator/state")
check("нарисованная вручную cookie не пускает", st in (401, 403), str(st))

forged = Client()
forged.set_cookie(f"lf_mod_{slug}", lead_a.cookie(f"lf_mod_{slug}") or "")
st, _, _ = forged.get(f"/e/{slug3}/api/moderator/state")
check("cookie одного события не подходит другому", st in (401, 403), str(st))

# --------------------------------------------------------------------------
print("\n=== Управление показом не пересекается ===")
res = lead_a.post_json(f"/e/{slug}/api/moderator/screen", {"action": "interval", "value": "20"})
check("интервал своего показа меняется", res.get("screen", {}).get("interval") == 20, str(res)[:120])
res2 = lead_c.post_json(f"/e/{slug3}/api/moderator/screen", {"action": "interval", "value": "7"})
check("интервал чужого показа свой", res2.get("screen", {}).get("interval") == 7, str(res2)[:120])
res3 = lead_a.get_json(f"/e/{slug}/api/moderator/state") or {}
check("у первого показа интервал не сбился", res3.get("screen", {}).get("interval") == 20,
      str(res3.get("screen"))[:120])

# --------------------------------------------------------------------------
print("\n=== Удаление ===")
path_c = DATA_DIR / "events" / slug3
check("папка события на диске", path_c.exists(), str(path_c))

st, _, _ = Client().post(f"/api/admin/events/{slug3}/delete", {})
check("удалить без входа нельзя", st in (401, 403), str(st))

st, _, _ = admin.post(f"/api/admin/events/{slug3}/delete", {})
check("удаление прошло", st == 200, str(st))
check("папка события исчезла", not path_c.exists(), str(path_c))

st, _, _ = anon.get(f"/e/{slug3}/")
check("страницы удалённого события — 404", st == 404, str(st))
st, _, _ = anon.get(f"/e/{slug3}/screen")
check("экран удалённого события — 404", st == 404, str(st))
st, _, _ = anon.get(f"/e/{slug3}/api/messages")
check("API удалённого события — 404", st == 404, str(st))

st, _, _ = guest_b.post(f"/e/{slug3}/api/messages", {"body": "после удаления"})
check("отправка в удалённое событие — 404", st == 404, str(st))

st, _, _ = lead_c.get(f"/e/{slug3}/api/moderator/state")
check("кабинет удалённого события закрыт", st in (401, 403, 404), str(st))

listing = admin.get_json("/api/admin/events") or {}
remaining = [e["slug"] for e in listing.get("events", [])]
check("удалённого события нет в списке", slug3 not in remaining, str(remaining))
check("первое событие осталось целым", slug in remaining, str(remaining))

feed_a = guest_a.get_json(f"/e/{slug}/api/messages") or {}
check("сообщения первого события уцелели",
      [m["body"] for m in feed_a.get("messages", [])] == ["Сообщение первого"],
      str([m["body"] for m in feed_a.get("messages", [])]))

st, _, _ = admin.post(f"/api/admin/events/{slug3}/delete", {})
check("повторное удаление — 404", st == 404, str(st))

# --------------------------------------------------------------------------
print("\n=== Можно завести новое событие на том же месте ===")
res = admin.post_json("/api/admin/events", {
    "title": f"Корпоратив снова {RUN}", "password": "novyy-parol", "slug": f"korpor-{RUN}",
})
slug4 = (res.get("event") or {}).get("slug")
check("адрес освободился после удаления", slug4 == f"korpor-{RUN}", str(slug4))
detail4 = admin.get_json(f"/api/admin/events/{slug4}") or {}
check("новое событие пустое", detail4.get("event", {}).get("messages") == 0, str(detail4.get("event")))
check("у нового события другой код", detail4.get("code") not in ("", code_c), str(detail4.get("code")))

guest4 = Client()
check("в новое событие тоже можно войти", join_guest(guest4, slug4, detail4.get("code", ""), "Вера"))
st, _, _ = guest4.post(f"/e/{slug4}/api/messages", {"body": "Проверка"})
check("и отправка работает", st == 200, str(st))
st, _, _ = guest_a.post(f"/e/{slug4}/api/messages", {"body": "чужая сессия"})
check("старая сессия в новое событие не пускает", st == 401, str(st))

# --------------------------------------------------------------------------
print("\n=== Уборка ===")
# slug3 уже удалён выше, здесь убираем остальные
for s in (slug, slug2, slug4, slug_auto):
    admin.post(f"/api/admin/events/{s}/delete", {})
ours = [p.name for p in (DATA_DIR / "events").glob("*")] if (DATA_DIR / "events").exists() else []
leaked = [n for n in ours if RUN in n]
check("после уборки наших событий не осталось", not leaked, str(leaked))

# --------------------------------------------------------------------------
print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Все проверки многособытийности пройдены.")