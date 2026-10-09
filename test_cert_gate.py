"""Проверка того, что https включается только вместе с сертификатом.

Логика простая, но ошибка в ней выглядит как «сайт не открывается вообще»:
редирект на https без сертификата уводит в никуда, а cookie с флагом Secure
браузер по http не сохраняет — гость теряет регистрацию и не понимает почему.

Запуск без сервера: python test_cert_gate.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def load(cert_file: str | None, *, force_https: str = "true", cookie_secure: str = "true"):
    """Перечитывает приложение с новым окружением.

    Модули сбрасываем целиком вместе с пакетом app: иначе импортируется
    уже загруженный app.config и настройки останутся прежними.
    """
    os.environ["LF_DATA_DIR"] = tempfile.mkdtemp(prefix="lf-cert-gate-")
    os.environ["FORCE_HTTPS"] = force_https
    os.environ["COOKIE_SECURE"] = cookie_secure
    os.environ["TRUSTED_PROXIES"] = "127.0.0.1"
    if cert_file is None:
        os.environ.pop("CERT_FILE", None)
    else:
        os.environ["CERT_FILE"] = cert_file

    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    from app import auth, config  # noqa: E402

    # проверка на лету: иначе пришлось бы ждать настоящие 10 секунд
    config._CERT_TTL = 0.0
    config._cert_cache = None
    return config, auth


async def _redirect_status(auth, scheme: str) -> int | None:
    """Прогоняет запрос через HttpsRedirectMiddleware, возвращает статус."""
    reached_inner = []

    async def inner(scope, receive, send):
        reached_inner.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"host", b"example.com"), (b"x-forwarded-proto", scheme.encode())],
        "client": ("127.0.0.1", 12345),
    }
    status = None

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await auth.HttpsRedirectMiddleware(inner)(scope, None, send)
    # None означает «запрос дошёл до приложения» — редиректа не было
    return None if reached_inner else status


def redirect_seen(auth, scheme: str) -> int | None:
    return asyncio.run(_redirect_status(auth, scheme))


print("\n=== Путь не задан: считаем, что https работает ===")
config, auth = load(None)
check("https_ready истинно", config.https_ready())
check("Secure-cookie ставится", auth._secure_cookie())
check("http перенаправляется", redirect_seen(auth, "http") in (301, 308))

print("\n=== Сертификата ещё нет ===")
missing = os.path.join(tempfile.mkdtemp(prefix="lf-cert-none-"), "fullchain.pem")
config, auth = load(missing)
check("https_ready ложно", not config.https_ready())
check("Secure-cookie не ставится", not auth._secure_cookie())
check("http НЕ перенаправляется", redirect_seen(auth, "http") is None)
check("https тоже проходит", redirect_seen(auth, "https") is None)

print("\n=== Сертификат появился ===")
present = os.path.join(tempfile.mkdtemp(prefix="lf-cert-has-"), "fullchain.pem")
with open(present, "w", encoding="utf-8") as f:
    f.write("-----BEGIN CERTIFICATE-----\n")
config, auth = load(present)
check("https_ready истинно без перезапуска", config.https_ready())
check("Secure-cookie ставится", auth._secure_cookie())
check("http перенаправляется на https", redirect_seen(auth, "http") in (301, 308))
check("https проходит без редиректа", redirect_seen(auth, "https") is None)

print("\n=== Переменные выключены вручную ===")
config, auth = load(present, force_https="false", cookie_secure="false")
check("FORCE_HTTPS=false уважается", redirect_seen(auth, "http") is None)
check("COOKIE_SECURE=false уважается", not auth._secure_cookie())

print("\n=== Файл появился во время работы ===")
late = os.path.join(tempfile.mkdtemp(prefix="lf-cert-late-"), "fullchain.pem")
config, auth = load(late)
check("до появления файла https выключен", not config.https_ready())
with open(late, "w", encoding="utf-8") as f:
    f.write("-----BEGIN CERTIFICATE-----\n")
check("после появления файла https включился сам", config.https_ready())

print("\n=== Пустой файл — это ещё не сертификат ===")
empty = os.path.join(tempfile.mkdtemp(prefix="lf-cert-empty-"), "fullchain.pem")
open(empty, "w", encoding="utf-8").close()
config, auth = load(empty)
check("пустой файл не считается сертификатом", not config.https_ready())
check("Secure-cookie не ставится", not auth._secure_cookie())
check("http НЕ перенаправляется", redirect_seen(auth, "http") is None)

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Включение https по сертификату работает.")