"""4.0.1: код приглашения вводится руками.

Человек может не суметь отсканировать QR-код: камера не тянет, бумаги под
рукой нет, ссылку не переслали. Витрина закрыта, поэтому раньше такой
человек упирался в тупик — войти можно было только по коду из адреса.

Проверяем ровно то, что делает браузер: одна банка cookie на всю сессию,
сценарий идёт от чистой страницы входа до отправки своего сообщения.

Запуск: поднять сервер, затем
    python test_code_entry.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from testlib import DATA_DIR, ROOT, _KeepsSecureCookies, NoRedirect, prepare, remove  # noqa: E402

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


def visitor():
    """Чистый посетитель: своя банка cookie, редиректы не следуются.

    Банку держим отдельно и отдаём вместе с opener: в списке обработчиков
    её позиция не гарантирована, и однажды на первом месте оказался
    ProxyHandler без поля cookies.
    """
    jar = _KeepsSecureCookies()
    op = urllib.request.build_opener(jar, NoRedirect())
    return jar, op


def get(op, path):
    try:
        r = op.open(urllib.request.Request(ROOT + path, method="GET"), timeout=20)
        return r.status, r.headers.get("Location", ""), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read()


def post(op, path, fields):
    req = urllib.request.Request(
        ROOT + path, data=urllib.parse.urlencode(fields).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        r = op.open(req, timeout=20)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def fingerprint() -> str:
    return urllib.request.urlopen(
        f"{ROOT}/static/join.html", timeout=20).read().decode("utf-8")


print("\n=== Подготовка ===")
ev = prepare("Ввод кода")
print(f"  событие: {ev.slug}, код: {ev.code}")
base = f"/e/{ev.slug}"

try:
    # ------------------------------------------------------------------
    # Сервер отдаёт страницу как есть; проверяем, что именно эту версию
    # и видит браузер, а не закешированную. _STATIC_VERSION считается один
    # раз при старте, и без перезапуска правка в исходнике в браузере
    # не видна — на этом уже спотывались дважды.
    print("\n=== Страница входа отдаётся свежей ===")
    served = fingerprint()
    check("страница приходит с кодовым полем", 'id="codeInput"' in served)
    check("и с чисткой введённого",
          "function cleanCode" in served)
    check("и с разблокировкой кнопки после отказа",
          re.search(r"if \(!useCode\) \{[\s\S]{0,300}?btn\.disabled = false;",
                    served) is not None,
          "иначе после первой неудачи вторую попытку не сделать")
    check("разметка не с закешированной версии",
          'id="codeInput"' in fingerprint() and
          re.search(r"function cleanCode", fingerprint()) is not None,
          "если сервер отдаёт старое — перезапусти его")

    # ------------------------------------------------------------------
    print("\n=== Страница входа без кода ===")
    v, op = visitor()
    st, _, body = get(op, base + "/join")
    check("страница входа открыта", st == 200, str(st))
    check("и это вход, а не витрина", b"joinForm" in body)
    check("поле для кода на месте", b'id="codeInput"' in body,
          "человек без камеры должен суметь войти руками")
    check("подпись зовёт ввести код", "Введите код приглашения".encode() in body)

    # ------------------------------------------------------------------
    print("\n=== Ввод кода руками ===")
    st, res = post(op, base + "/api/join",
                   {"name": "Борис", "invite": ev.code.lower()})
    check("код строчными пускает", st == 200, str(st))
    check("имя сохранено", (res or {}).get("name") == "Борис", str(res)[:80])
    check("сессия выдана",
          any(k.startswith("lf_pid_") for k in v.cookies),
          ", ".join(v.cookies))

    st, _, _ = get(op, base + "/send")
    check("страница отправки открылась сразу", st == 200, str(st))

    st, res = post(op, base + "/api/messages", {"body": "Пишу без QR-кода"})
    check("и отправка работает", st == 200, str(st))

    # ------------------------------------------------------------------
    print("\n=== Код не должен приниматься «почти» ===")
    v2, op2 = visitor()
    st, _ = post(op2, base + "/api/join", {"name": "Кто-то", "invite": ev.code[:-1]})
    check("код без последней буквы не пускает", st == 403, str(st))
    st, _ = post(op2, base + "/api/join", {"name": "Кто-то", "invite": ev.code + "X"})
    check("код с лишней буквой не пускает", st == 403, str(st))
    st, _ = post(op2, base + "/api/join", {"name": "Кто-то", "invite": "ХЕРНЯ"})
    check("чужой код не пускает", st == 403, str(st))
    st, _ = post(op2, base + "/api/join", {"name": "Кто-то", "invite": ""})
    check("пустой код не пускает", st == 403, str(st))
    st, _ = post(op2, base + "/api/join", {"name": "Кто-то", "invite": " " + ev.code})
    check("код с пробелом слева пускает — человек так вводит с телефона",
          st == 200, str(st))
    st, _ = post(op2, base + "/api/join", {"name": "Кто-то", "invite": "  "})
    check("а одни пробелы — это не код", st == 403, str(st))

    # ------------------------------------------------------------------
    print("\n=== Смена кода гасит старый ===")
    v3, op3 = visitor()
    st, _ = post(op3, base + "/api/join", {"name": "Вера", "invite": ev.code})
    check("вошёл по действующему коду", st == 200, str(st))

    lead_jar, lead = visitor()
    st, _ = post(lead, base + "/api/moderator/login",
                 {"password": ev.mod_password})
    check("ведущий вошёл", st == 200, str(st))
    st, res = post(lead, base + "/api/moderator/rotate-invite", {})
    new_code = res.get("code", "")
    check("код сменён", st == 200 and new_code and new_code != ev.code, new_code)

    st, _, _ = get(op3, base + "/send")
    check("уже вошедший не вылетел", st == 200, str(st))

    v4, op4 = visitor()
    st, _ = post(op4, base + "/api/join", {"name": "Опаздывающий", "invite": ev.code})
    check("прежний код больше не пускает", st == 403, str(st))
    st, _ = post(op4, base + "/api/join", {"name": "Опаздывающий", "invite": new_code})
    check("новый код пускает", st == 200, str(st))

    # ------------------------------------------------------------------
    print("\n=== События изолированы ===")
    ev2 = prepare("Второе")
    v5, op5 = visitor()
    st, _ = post(op5, base + "/api/join", {"name": "Чужой", "invite": ev2.code})
    check("код чужого события не пускает в это", st == 403, str(st))
    st, _ = post(op5, f"/e/{ev2.slug}/api/join", {"name": "Свой", "invite": ev2.code})
    check("а в своё событие пускает", st == 200, str(st))
    remove(ev2.slug)

finally:
    remove(ev.slug)

print(f"\n=== Итог: {passed} OK, {failed} FAIL ===")
sys.exit(1 if failed else 0)
