"""Проверка режима «за nginx»: заголовки прокси, https, защита Host.

Запуск: python test_proxy.py
Сервер должен быть поднят так:

    TRUSTED_PROXIES=127.0.0.1,::1 FORCE_HTTPS=true COOKIE_SECURE=true \
    TRUSTED_HOSTS=example.com python run.py --port 8123
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# --- 3.0: у каждой проверки своё событие ---------------------------------
# Адреса теперь живут под адресом события, поэтому проверка заводит
# своё событие и работает уже по нему.
from testlib import prepare, remove  # noqa: E402

CTX = prepare("Проверка за прокси")
SLUG = CTX.slug
BASE = CTX.base
TOKEN = CTX.code
PASS = CTX.mod_password
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Recorder(urllib.request.HTTPCookieProcessor):
    """CookieProcessor, который запоминает cookie без учёта флага Secure.

    Обычный http.cookiejar отбрасывает cookie с Secure, когда соединение
    не по https, и мы бы не увидели флагов в ответе. Здесь нужен сырой вид.
    """

    def add_cookie_header(self, request):
        """Не подставляем cookie обратно в запросы — каждый вызов самодостаточен."""

    def extract_cookies(self, response, request):
        for value in response.headers.get_all("Set-Cookie") or []:
            name, _, rest = value.partition("=")
            self.jar[name.strip()] = rest.split(";")[0]


opener = urllib.request.build_opener(Recorder(), NoRedirect)


def call(method: str, url: str, fields: dict | None = None, headers: dict | None = None):
    if fields is not None:
        from urllib.parse import urlencode
        req = urllib.request.Request(
            BASE + url, data=urlencode(fields).encode(), method=method
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        req = urllib.request.Request(BASE + url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with opener.open(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def all_headers(headers, name: str) -> str:
    return "; ".join(headers.get_all(name) or [])


def load_config() -> dict:
    with open("data/config.json", encoding="utf-8") as f:
        return json.load(f)


PASSWORD = PASS
TOKEN = CTX.code

print("\n=== Редирект на https ===")
st, headers, _ = call("GET", "/", headers={"Host": "example.com",
                                           "X-Forwarded-Proto": "http"})
check("http перенаправляется на https", st in (301, 308), str(st))
location = headers.get("Location", "")
check("в Location именно https",
      location.startswith("https://example.com/"), location or "—")

# 4.1: витрина / для анонима — всегда 302 на /join, поэтому «проходит без
# редиректа» и «свой домен» проверяем на странице входа — она открыта всем.
st, _, _ = call("GET", "/join", headers={"Host": "example.com",
                                          "X-Forwarded-Proto": "https"})
check("https проходит без редиректа", st == 200, str(st))

print("\n=== Защита Host ===")
st, _, _ = call("GET", "/join", headers={"Host": "example.com",
                                          "X-Forwarded-Proto": "https"})
check("свой домен принимается", st == 200, str(st))
st, _, _ = call("GET", "/", headers={"Host": "attacker.example",
                                      "X-Forwarded-Proto": "https"})
check("чужой Host отклоняется", st == 400, str(st))

print("\n=== Cookie и пароль через прокси ===")
st, headers, _ = call("POST", "/api/moderator/login", {"password": "неверный"},
                      {"Host": "example.com", "X-Forwarded-Proto": "https"})
check("неверный пароль отклонён", st == 401, str(st))

st, headers, body = call("POST", "/api/join",
                         {"name": "Гость", "invite": TOKEN},
                         {"Host": "example.com", "X-Forwarded-Proto": "https"})
cookie = all_headers(headers, "Set-Cookie")
check("гость зарегистрирован", st == 200, str(st))
check("cookie имеет флаг Secure", "Secure" in cookie, cookie[:90])
check("cookie HttpOnly", "HttpOnly" in cookie, cookie[:90])
check("cookie SameSite=Lax", "SameSite=lax" in cookie or "SameSite=Lax" in cookie,
      cookie[:90])

print("\n=== Лимит входа считается по реальному IP клиента ===")
# Девять неудачных попыток с одного подделанного адреса
for _ in range(9):
    call("POST", "/api/moderator/login", {"password": "неверный"},
         {"Host": "example.com", "X-Forwarded-Proto": "https",
          "X-Forwarded-For": "203.0.113.77"})

st, _, body = call("POST", "/api/moderator/login", {"password": PASSWORD},
                   {"Host": "example.com", "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "203.0.113.77"})
check("заблокированный адрес не пускает даже с верным паролем", st == 429,
      f"статус {st}")

st, _, _ = call("POST", "/api/moderator/login", {"password": PASSWORD},
                {"Host": "example.com", "X-Forwarded-Proto": "https",
                 "X-Forwarded-For": "198.51.100.9"})
check("другой адрес не заблокирован", st == 200, f"статус {st}")

print("\n=== Кеширование форм авторизации ===")
PROXY = {"Host": "example.com", "X-Forwarded-Proto": "https"}

st, headers, _ = call("GET", "/moderator/login", headers=PROXY)
cache = headers.get("Cache-Control", "—")
check("страница входа не кешируется",
      "no-store" in cache or "no-cache" in cache, cache)

st, headers, _ = call("POST", "/api/moderator/login", {"password": "x"}, PROXY)
cache = headers.get("Cache-Control", "—")
check("ответ на вход не кешируется",
      "no-store" in cache or "no-cache" in cache, cache)

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Все проверки режима прокси пройдены.")
