"""Общее для тестов: завести событие и получить от него адреса.

Проверки идут против работающего сервера. Событие заводится через админ-API,
поэтому тестам не нужно знать ни пароль админа, ни файлы на диске.

    from testlib import prepare

    ctx = prepare("Проверка API")
    ctx.base      # http://127.0.0.1:8123/e/<адрес>  — уже с событием
    ctx.code      # код приглашения для гостей
    ctx.mod_password
"""

from __future__ import annotations

import atexit
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

ROOT = os.environ.get("LF_TEST_ROOT", "http://127.0.0.1:8123").rstrip("/")
DATA_DIR = os.environ.get("LF_DATA_DIR", "data")
# В режиме проверки за прокси сервер жёстко проверяет Host, поэтому и
# служебные запросы (вход, создание события) должны идти с тем же Host
HOST = os.environ.get("LF_TEST_HOST", "")

MOD_PASSWORD = os.environ.get("LF_TEST_MOD_PASSWORD", "parol-vedushchego-1")

# События, заведённые проверками. Убираются при выходе из программы, даже
# если проверка упала раньше: иначе на сервере остаётся мусор.
_created: list[str] = []


def _cleanup_all() -> None:
    while _created:
        remove(_created.pop())


atexit.register(_cleanup_all)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _KeepsSecureCookies(urllib.request.HTTPCookieProcessor):
    """CookieProcessor, который не выбрасывает cookie с Secure.

    В режиме проверки за прокси cookie помечена Secure, а соединение идёт
    по http — обычный банк её не сохранит, и служебные запросы разбились бы
    об отсутствие входа. Поэтому храним cookie в словаре и подставляем их
    в заголовок сами; пустая настоящая банка нужна лишь потому, что urllib
    обращается к ней из http_request.
    """

    def __init__(self) -> None:
        super().__init__(CookieJar())
        self.cookies: dict[str, str] = {}
        # сырые заголовки Set-Cookie целиком: по словарю имя -> значение
        # не видно срока действия, а он и есть предмет проверки сессии
        self.seen: list[str] = []

    def http_response(self, request, response):
        # переопределять extract_cookies бессмысленно: urllib вызывает его
        # у банки cookiejar, а не у обработчика
        for value in response.headers.get_all("Set-Cookie") or []:
            self.seen.append(value)
            name, _, rest = value.partition("=")
            self.cookies[name.strip()] = rest.split(";")[0]
        return super().http_response(request, response)

    def http_request(self, request):
        if self.cookies:
            # убираем прежний заголовок, иначе при повторе он бы дублировался
            request.headers = {
                k: v for k, v in request.headers.items() if k.lower() != "cookie"
            }
            request.add_unredirected_header(
                "Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            )
        return super().http_request(request)


def admin_password() -> str:
    with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)["admin_password"]


def admin_post(path: str, fields: dict) -> tuple[int, dict]:
    """Запрос от имени администратора: входит и делает вызов."""
    opener = urllib.request.build_opener(_KeepsSecureCookies(), NoRedirect())
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    if HOST:
        form["Host"] = HOST

    # вход отдельным запросом и отдельным паролем: пароль события,
    # который лежит в fields, в админку не подходит
    req = urllib.request.Request(
        ROOT + "/api/admin/login",
        data=urllib.parse.urlencode({"password": admin_password()}).encode(),
        method="POST",
        headers=form,
    )
    with opener.open(req, timeout=20) as resp:
        resp.read()

    req = urllib.request.Request(
        ROOT + path, data=urllib.parse.urlencode(fields).encode(),
        method="POST", headers=form,
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def admin_get(path: str) -> tuple[int, dict]:
    """То же, что admin_post, но чтение. Список событий — GET."""
    opener = urllib.request.build_opener(_KeepsSecureCookies(), NoRedirect())
    form = {"Host": HOST} if HOST else {}

    req = urllib.request.Request(
        ROOT + "/api/admin/login",
        data=urllib.parse.urlencode({"password": admin_password()}).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", **form},
    )
    with opener.open(req, timeout=20) as resp:
        resp.read()

    req = urllib.request.Request(ROOT + path, method="GET", headers=form)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


class EventContext:
    def __init__(self, slug: str, data: dict, password: str) -> None:
        self.slug = slug
        self.base = f"{ROOT}/e/{slug}"
        self.code = data["code"]
        self.links = data["links"]
        self.mod_password = password


def prepare(title: str = "Проверка", password: str = MOD_PASSWORD,
            password_given: bool = True) -> EventContext:
    """Создаёт событие и возвращает адреса для проверок."""
    fields = {"title": f"{title} {uuid.uuid4().hex[:6]}"}
    if password_given:
        fields["password"] = password

    st, created = admin_post("/api/admin/events", fields)
    if st != 200 or not created.get("event"):
        raise SystemExit(f"не удалось создать событие: {st} {created}")

    slug = created["event"]["slug"]

    # Код приглашения и пароль лежат в настройках события. Карточка в API
    # требует входа админа, а из файла они читаются проще и всегда свежие.
    cfg_path = os.path.join(DATA_DIR, "events", slug, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    data = {
        "links": created.get("links", {}),
        "code": cfg["invite_token"],
    }
    _created.append(slug)
    return EventContext(slug, data, password)


def remove(slug: str) -> None:
    """Убирает событие за собой. Ошибки не важны: тест уже всё проверил."""
    if slug in _created:
        _created.remove(slug)
    try:
        admin_post(f"/api/admin/events/{slug}/delete", {})
    except Exception:
        pass