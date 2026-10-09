"""Сессия не должна обрываться сама (4.0.1).

Требование: человек, который вошёл, не вылетает из сессии ни при смене IP,
ни при повторном заходе через тот же браузер, и держится хотя бы 6–7 часов.

Что тут проверяется:

  * сессия отправителя переживает смену IP — идентификатор не привязан к
    адресу, только к токену в cookie;
  * тот же браузер возвращается и продолжает работать без входа заново,
    и имя остаётся прежним;
  * смена кода приглашения не выкидывает уже вошедших;
  * cookie всех трёх ролей живут заметно дольше семи часов;
  * витрина закрыта для всех, кроме администратора.

Запуск: поднять сервер, затем
    python test_session.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from testlib import (  # noqa: E402
    DATA_DIR, ROOT, _KeepsSecureCookies, NoRedirect, admin_post,
    admin_password, prepare, remove,
)

# 6–7 часов, о которых говорит заказчик, с запасом
MIN_SESSION_SECONDS = 7 * 3600

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}  [{extra}]")
    else:
        failed += 1
        print(f"  FAIL {name}  [{extra}]")


class Guest:
    """Гость с настоящей банкой cookie: так же, как в браузере."""

    def __init__(self) -> None:
        self.jar = _KeepsSecureCookies()
        self.jar.seen = []                       # сырые Set-Cookie
        self.opener = urllib.request.build_opener(self.jar, NoRedirect())

    def get(self, path: str, headers: dict | None = None) -> tuple[int, str]:
        req = urllib.request.Request(ROOT + path, method="GET",
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, resp.headers.get("Location", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Location", "")

    def get_json(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(ROOT + path, method="GET",
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def post(self, path: str, fields: dict, headers: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            ROOT + path,
            data=urllib.parse.urlencode(fields).encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     **(headers or {})},
        )
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def cookie_max_age(self, name: str) -> int:
        """Сколько секунд просил продержаться сервер в Set-Cookie.

        Порядок атрибутов сервер не гарантирует: у нас выходит
        «имя=значение; HttpOnly; Max-Age=…; Path=/; SameSite=lax», поэтому
        Max-Age ищем в любом месте строки, а не вторым атрибутом.
        """
        for header in self.jar.seen:
            if not header.lstrip().startswith(name + "="):
                continue
            m = re.search(r"Max-Age=(\d+)", header, re.I)
            if m:
                return int(m.group(1))
        return 0


def mod_login(ev) -> Guest:
    """Вход ведущим: возвращает клиента с подписанной cookie."""
    c = Guest()
    st, _ = c.post(f"/e/{ev.slug}/api/moderator/login",
                   {"password": ev.mod_password})
    if st != 200:
        raise SystemExit(f"ведущий не вошёл: {st}")
    return c


def admin_login() -> Guest:
    c = Guest()
    c.post("/api/admin/login", {"password": admin_password()})
    return c


def mod_rotate(ev) -> tuple[int, dict]:
    """Смена кода приглашения делается ведущим, не админом."""
    c = mod_login(ev)
    return c.post(f"/e/{ev.slug}/api/moderator/rotate-invite", {})


print("\n=== Подготовка ===")
ev = prepare("Сессия")
print(f"  событие: {ev.slug}, код: {ev.code}")

try:
    # ------------------------------------------------------------------
    print("\n=== Сессия отправителя переживает смену IP ===")
    g = Guest()
    st, res = g.post(f"/e/{ev.slug}/api/join",
                     {"name": "Аня", "invite": ev.code})
    check("гость вошёл", st == 200 and res.get("ok"), str(st))
    token_first = res.get("token", "")

    st, _ = g.get(f"/e/{ev.slug}/send")
    check("страница отправки открылась сразу", st == 200, str(st))

    # Тот же браузер, но адрес сменился: мобильный оператор, домашний wifi,
    # выход из-за провайдера. Меняем только заголовок — cookie та же.
    for ip in ("10.0.0.7", "95.24.181.3", "2001:db8::17"):
        st, res = g.get_json(f"/e/{ev.slug}/api/me",
                            {"X-Forwarded-For": ip, "X-Real-IP": ip})
        check(f"сессия жива с адреса {ip}",
              st == 200 and (res or {}).get("joined") is True, str(res)[:90])
        st2, _ = g.get(f"/e/{ev.slug}/send", {"X-Forwarded-For": ip})
        check("  и страница отправки открылась", st2 == 200, str(st2))

    # ------------------------------------------------------------------
    print("\n=== Повторный заход через тот же браузер ===")
    g2 = Guest()
    g2.jar.cookies = dict(g.jar.cookies)      # тот же набор cookie
    st, res = g2.get_json(f"/e/{ev.slug}/api/me")
    check("сессия пережила «новый заход»", st == 200 and res.get("joined"),
          str(res)[:90])
    check("и имя осталось прежним",
          (res or {}).get("name") == "Аня", str((res or {}).get("name")))
    st, _ = g2.get(f"/e/{ev.slug}/send")
    check("страница отправки открылась без входа заново", st == 200, str(st))

    st, res = g.post(f"/e/{ev.slug}/api/messages", {"body": "Сообщение из сессии"})
    check("отправка работает без повторного входа", st == 200, str(st))

    # ------------------------------------------------------------------
    print("\n=== Смена кода приглашения не выкидывает вошедших ===")
    st, res = g.post(f"/e/{ev.slug}/api/messages", {"body": "До смены кода"})
    check("до смены кода всё работает", st == 200, str(st))

    st, res = mod_rotate(ev)
    check("код приглашения сменён", st == 200, str(st)[:90])
    new_code = ""
    cfg_path = os.path.join(DATA_DIR, "events", ev.slug, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        new_code = json.load(f)["invite_token"]
    check("код действительно сменился", new_code != ev.code, new_code)

    st, res = g.get_json(f"/e/{ev.slug}/api/me")
    check("вошедший не вылетел после смены кода",
          st == 200 and res.get("joined") is True, str(res)[:90])
    st, _ = g.get(f"/e/{ev.slug}/send")
    check("и страница отправки по-прежнему открыта", st == 200, str(st))
    st_bad, _ = g.post(f"/e/{ev.slug}/api/join",
                       {"name": "Кто-то", "invite": ev.code})
    check("прежний код больше не пускает", st_bad == 403, str(st_bad))

    # ------------------------------------------------------------------
    print("\n=== Сессия ведущего ===")
    m = mod_login(ev)
    st, res = m.get_json(f"/e/{ev.slug}/api/moderator/state")
    check("ведущий вошёл", st == 200 and (res or {}).get("screen") is not None, str(st))
    st2, _ = m.get(f"/e/{ev.slug}/api/moderator/state",
                   {"X-Forwarded-For": "203.0.113.9"})
    check("ведущему смена IP нипочём", st2 == 200, str(st2))
    st3, _ = m.get(f"/e/{ev.slug}/api/moderator/state")
    check("повторный заход ведущего не выкидывает", st3 == 200, str(st3))

    # ------------------------------------------------------------------
    print("\n=== Сколько живут cookie ===")
    a = admin_login()
    ages = {
        "гость": g.cookie_max_age(f"lf_pid_{ev.slug}"),
        "ведущий": m.cookie_max_age(f"lf_mod_{ev.slug}"),
        "админ": a.cookie_max_age("lf_admin"),
    }
    for role, age in ages.items():
        check(f"{role} держится дольше 7 часов",
              age >= MIN_SESSION_SECONDS,
              f"{age} с ≈ {age / 3600:.1f} ч")

    # значения живут в конфиге, а не разбросаны по коду
    cfg_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "app", "config.py"), encoding="utf-8").read()
    for name in ("PARTICIPANT_COOKIE_MAX_AGE", "MODERATOR_COOKIE_MAX_AGE",
                 "ADMIN_COOKIE_MAX_AGE"):
        m2 = re.search(rf"^{name} = (.+)$", cfg_src, re.M)
        check(f"{name} хватает на смену", m2 is not None, m2.group(1) if m2 else "?")

    # ------------------------------------------------------------------
    # Человек может не суметь отсканировать QR-код: камера не тянет,
    # бумаги под рукой нет. Код набирают руками, и регистр значения не
    # имеет — восемь символов вводят не глядя.
    print("\n=== Код приглашения вводится руками ===")
    g3 = Guest()
    st, res = g3.post(f"/e/{ev.slug}/api/join",
                      {"name": "Борис", "invite": new_code.lower()})
    check("код в нижнем регистре пускает", st == 200, str(st))
    st, res = g3.post(f"/e/{ev.slug}/api/join",
                      {"name": "Борис", "invite": "  " + new_code + "  "})
    check("код с пробелами вокруг пускает", st == 200, str(st))
    st, res = g3.post(f"/e/{ev.slug}/api/join",
                      {"name": "Борис", "invite": "ХЕРНЯ"})
    check("а чужой код по-прежнему не пускает", st == 403, str(st))

    # ------------------------------------------------------------------
    print("\n=== Витрина закрыта для всех, кроме администратора ===")
    st, where = g.get(f"/e/{ev.slug}/")
    check("гость на витрину не попадает", st in (302, 307), f"{st} {where}")
    check("и его отправляет на страницу входа, а не на витрину",
          "/join" in where or "/send" in where, str(where))

    # Анонимный посетитель витрины — главный случай: раньше ему показывали
    # ленту целиком, потому что проверялась только сессия отправителя.
    anon = Guest()
    st, where = anon.get(f"/e/{ev.slug}/")
    check("анонимному витрина не показывается", st in (302, 307), f"{st} {where}")
    check("и его отправляет на вход, а не показывает чужое",
          "/join" in where, str(where))

    st, where = anon.get(f"/e/{ev.slug}/join")
    check("вход без кода — это страница входа, а не витрина",
          st == 200, str(st))

    st, where = anon.get(f"/e/{ev.slug}/join?t=" + urllib.parse.quote("НЕВЕРНЫЙКОД"))
    check("вход с чужим кодом — тоже страница входа", st == 200, str(st))

    st, where = anon.get(f"/e/{ev.slug}/?t={ev.code}")
    check("код с витрины доезжает до входа", f"t={ev.code}" in where, str(where))

    # ведущий смотрит витрину
    m4 = mod_login(ev)
    st, where = m4.get(f"/e/{ev.slug}/")
    check("ведущему витрина доступна", st == 200, f"{st} {where}")

    # администратору витрина остаётся доступна
    m3 = admin_login()
    st, where = m3.get(f"/e/{ev.slug}/")
    check("администратору витрина доступна", st == 200, f"{st} {where}")

finally:
    remove(ev.slug)

print(f"\n=== Итог: {passed} OK, {failed} FAIL ===")
sys.exit(1 if failed else 0)
