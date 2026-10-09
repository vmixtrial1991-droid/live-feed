"""Доступ: админка, кабинет ведущего и регистрация отправителей.

Cookie подписываются секретом и назначением. В прошлой версии cookie
ведущего просто проверялась на существование, то есть её можно было
нарисовать в браузере и войти. Теперь значение — подпись, которую без
секрета события не подделать.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import RedirectResponse

from . import config
from .db import EventStore
from .events import Event

# Защита от перебора пароля. Счётчики лежат в SQLite, а не в памяти:
# при перезапуске сервиса или при нескольких воркерах иначе лимит
# обнуляется и перебор становится бесконечным.
_MAX_FAILS = 8
_WINDOW = 300.0

ADMIN_STORE = EventStore(config.DATA_DIR / "admin-attempts.db")


def _ensure_attempts_table(store: EventStore) -> None:
    with store.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  key TEXT NOT NULL,"
            "  at REAL NOT NULL)"
        )


def _db_fails(store: EventStore) -> dict[str, list[float]]:
    now = time.time()
    _ensure_attempts_table(store)
    with store.connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE at < ?", (now - _WINDOW,))
        rows = conn.execute("SELECT key, at FROM login_attempts").fetchall()
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(r["key"], []).append(r["at"])
    return out


def _record_attempt(store: EventStore, key: str) -> None:
    _ensure_attempts_table(store)
    with store.connect() as conn:
        # каждая попытка — отдельная строка, иначе счётчик не растёт
        conn.execute(
            "INSERT INTO login_attempts (key, at) VALUES (?, ?)", (key, time.time())
        )


def _clear_attempts(store: EventStore, key: str) -> None:
    _ensure_attempts_table(store)
    with store.connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE key = ?", (key,))


@dataclass
class Participant:
    id: int
    token: str
    name: str


# --------------------------------------------------------------------------
# Общее
# --------------------------------------------------------------------------
def _secure_cookie() -> bool:
    """Ставим Secure только когда сайт реально работает по https.

    Флаг Secure при обычном http браузер проигнорирует и cookie не сохранит:
    гость решит, что регистрация слетела, и уйдёт по второму кругу.
    """
    return config.COOKIE_SECURE and config.https_ready()


def client_ip(request: Request) -> str:
    """Реальный адрес клиента.

    За обратным прокси request.client — это сам прокси. Верим заголовку
    X-Forwarded-For только если соединение пришло от одного из TRUSTED_PROXIES,
    иначе клиент подставил бы себе адрес по выбору и обошёл лимит входа.
    """
    peer = request.client.host if request.client else ""
    if config.is_trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # X-Forwarded-For: client, proxy1, proxy2 — клиент первый
            return forwarded.split(",")[0].strip()[:45]
    return peer or "?"


def _key(store: EventStore, request: Request, scope: str) -> str:
    return f"{scope}:{client_ip(request)}"


# --------------------------------------------------------------------------
# Админка
# --------------------------------------------------------------------------
def admin_secret() -> str:
    return str(config.admin_settings().get("admin_secret", ""))


def check_admin_password(password: str) -> bool:
    data = config.admin_settings()
    return config.verify_password(
        password or "", str(data.get("admin_salt", "")), str(data.get("admin_hash", ""))
    )


def set_admin_cookie(response) -> None:
    response.set_cookie(
        config.ADMIN_COOKIE,
        config.sign(admin_secret(), "admin"),
        max_age=config.ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        path="/",
    )


def clear_admin_cookie(response) -> None:
    response.delete_cookie(config.ADMIN_COOKIE, path="/")


def is_admin(request: Request) -> bool:
    return config.signature_matches(
        admin_secret(), "admin", request.cookies.get(config.ADMIN_COOKIE)
    )


def admin_rate_limited(request: Request) -> bool:
    key = _key(ADMIN_STORE, request, "admin")
    return len(_db_fails(ADMIN_STORE).get(key, [])) >= _MAX_FAILS


def admin_record_fail(request: Request) -> None:
    _record_attempt(ADMIN_STORE, _key(ADMIN_STORE, request, "admin"))


def admin_clear_fails(request: Request) -> None:
    _clear_attempts(ADMIN_STORE, _key(ADMIN_STORE, request, "admin"))


# --------------------------------------------------------------------------
# Кабинет ведущего события
# --------------------------------------------------------------------------
def check_moderator_password(event: Event, password: str) -> bool:
    return event.check_moderator_password(password)


def set_moderator_cookie(response, event: Event) -> None:
    response.set_cookie(
        event.moderator_cookie_name(),
        event.moderator_cookie_value(),
        max_age=config.MODERATOR_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        path="/",
    )


def clear_moderator_cookie(response, event: Event) -> None:
    response.delete_cookie(event.moderator_cookie_name(), path="/")


def is_moderator(request: Request, event: Event) -> bool:
    """Подпись должна совпасть и по секрету события, и по его адресу."""
    return config.signature_matches(
        event.secret,
        f"moderator:{event.slug}",
        request.cookies.get(event.moderator_cookie_name()),
    )


def moderator_rate_limited(request: Request, event: Event) -> bool:
    key = _key(event.store, request, f"mod:{event.slug}")
    return len(_db_fails(event.store).get(key, [])) >= _MAX_FAILS


def moderator_record_fail(request: Request, event: Event) -> None:
    _record_attempt(event.store, _key(event.store, request, f"mod:{event.slug}"))


def moderator_clear_fails(request: Request, event: Event) -> None:
    _clear_attempts(event.store, _key(event.store, request, f"mod:{event.slug}"))


# --------------------------------------------------------------------------
# Отправители
# --------------------------------------------------------------------------
def set_participant_cookie(response, event: Event, token: str) -> None:
    """Cookie участника именуется по событию.

    Имя с адресом события нужно, чтобы в одном браузере можно было
    участвовать в двух ивентах сразу: иначе второй вход затирал бы
    регистрацию первого.
    """
    response.set_cookie(
        event.participant_cookie_name(),
        token,
        max_age=config.PARTICIPANT_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        path="/",
    )


def clear_participant_cookie(response, event: Event) -> None:
    response.delete_cookie(event.participant_cookie_name(), path="/")


def current_participant(request: Request, event: Event) -> Participant | None:
    token = request.cookies.get(event.participant_cookie_name())
    row = event.store.participant_by_token(token)
    if row is None:
        return None
    event.store.touch_participant(row["id"])
    return Participant(id=row["id"], token=row["token"], name=row["name"])


def invite_is_valid(event: Event, token: str | None) -> bool:
    if not token:
        return False
    expected = str(event.get("invite_token", ""))
    # compare_digest падает на не-ASCII строках, а код приглашения приходит
    # прямо из адреса: без перевода в байты запрос вида /join?t=что-то
    # ронял бы страницу с ошибкой 500
    return secrets.compare_digest(str(token).encode(), expected.encode())


def join(event: Event, name: str, token: str) -> Participant:
    """Регистрирует или обновляет отправителя; сессия живёт весь праздник."""
    name = " ".join(name.split())[:40] or "Гость"
    existing = event.store.participant_by_token(token)
    if existing:
        event.store.rename_participant(existing["id"], name)
        return Participant(existing["id"], existing["token"], name)
    new_token = secrets.token_urlsafe(24)
    row = event.store.create_participant(new_token, name)
    return Participant(row["id"], row["token"], row["name"])


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------
def _is_auth_path(path: str) -> bool:
    """Страницы и запросы, ответы на которые нельзя кешировать."""
    if path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin"):
        return True
    # кабинеты ведущих: /e/<адрес>/moderator и /e/<адрес>/api/moderator/...
    return "/moderator" in path


class NoStoreForAuthMiddleware:
    """Запрещаем кешировать страницы и ответы входа.

    Отдельный middleware, а не заголовок в обработчике: при отказе пароля
    ответ уходит через HTTPException, и заголовок из тела обработчика
    до него не доедет.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _is_auth_path(scope.get("path", "")):
            async def send_with_no_store(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers") or [])
                    if not any(k.lower() == b"cache-control" for k, _ in headers):
                        headers.append((b"cache-control", b"no-store"))
                    # заменяем список целиком: Starlette отдаёт его дальше
                    # по ссылке, и append выше мог бы не попасть в ответ
                    message = {**message, "headers": headers}
                return await send(message)

            await self.app(scope, receive, send_with_no_store)
            return
        await self.app(scope, receive, send)


class HttpsRedirectMiddleware:
    """Перекидываем http на https, когда сервер стоит за прокси с TLS.

    Редирект делаем только когда прокси явно сказал X-Forwarded-Proto: http.
    Иначе проверка запроса на сам uvicorn ломала бы локальный запуск.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        # сертификата может ещё не быть: редирект в таком случае только вредит
        if scope["type"] == "http" and config.FORCE_HTTPS and config.https_ready():
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            peer = scope.get("client")
            peer_host = peer[0] if peer else ""
            if config.is_trusted_proxy(peer_host):
                forwarded = headers.get("x-forwarded-proto", "")
                scheme = forwarded.split(",")[0].strip().lower() if forwarded else ""
                if scheme == "http":
                    host = headers.get("host", "")
                    path = scope.get("path", "/") or "/"
                    query = scope.get("query_string", b"").decode()
                    target = f"https://{host}{path}" + (f"?{query}" if query else "")
                    await RedirectResponse(target, status_code=301)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def install_middleware(application) -> None:
    """Ставим middleware. Последний добавленный оказывается внешним."""
    if config.TRUSTED_HOSTS:
        application.add_middleware(TrustedHostMiddleware, allowed_hosts=config.TRUSTED_HOSTS)
    if config.FORCE_HTTPS:
        application.add_middleware(HttpsRedirectMiddleware)
    application.add_middleware(NoStoreForAuthMiddleware)