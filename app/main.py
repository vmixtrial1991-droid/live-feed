"""FastAPI-приложение: админка, страницы событий, API и WebSocket.

Приложение многособытийное: у каждого события своя папка, своя база и свои
ссылки вида /e/<адрес>/... Поэтому почти каждый обработчик начинается с
поиска события по адресу, и уже у него — своя база и свои подписчики.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import secrets
import time
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import archive, auth, config, events, images
from .events import Event, EventError
from .realtime import EventRuntime, keepalive, registry

app = FastAPI(
    title="Живая лента — несколько событий",
    docs_url=None,
    redoc_url=None,
    # за обратным прокси нужно доверять заголовкам от самого прокси
    forwarded_allow_ips=(
        ",".join(sorted(config.TRUSTED_PROXY_HOSTS | {str(n) for n in config.TRUSTED_PROXY_NETS}))
        or None
    ),
)

# События, чью базу мы уже открывали в этом процессе
_prepared: set[str] = set()


# --------------------------------------------------------------------------
# Подготовка
# --------------------------------------------------------------------------
def _event_or_404(slug: str) -> Event:
    event = events.get_event(slug)
    if event is None:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    return event


def _prepare(event: Event) -> Event:
    """Готовит событие к работе: база и очередь показа.

    База открывается один раз на событие за процесс: PRAGMA и executescript
    на каждый запрос — лишняя работа в горячем пути отправки сообщений.

    4.0: больше **не вызываем** cleanup_orphans. Он удалял файлы, на которые
    нет ссылок в базе, а с 4.0 фотографии живут независимо от ленты: файл
    должен пережить удаление сообщения, иначе «удалил неудачный кадр» стирал
    бы оригинал навсегда. Стирает ведущий — кнопкой в панели.
    """
    if event.slug not in _prepared:
        event.store.init()
        # Настройки показа подтягиваем из файла события. Раньше они
        # применялись только при старте сервера, и событие, созданное позже,
        # жило на значениях по умолчанию: в конфиге могло быть 15 секунд,
        # а показ шёл по 12. Рабочий объект события берёт дефолт из
        # конструктора, а не из config.json.
        try:
            data = event.load()
            screen = registry.get(event.slug).screen
            screen.interval = int(data.get("interval", 12) or 12)
            screen.slideshow_interval = int(data.get("slideshow_interval", 15) or 15)
            screen.reset_deadline()
        except (EventError, OSError, ValueError):
            pass
        _prepared.add(event.slug)
    return event


def _runtime(event: Event) -> EventRuntime:
    """Живые объекты события: подписчики и очередь показа.

    Очередь подтягиваем каждый раз: сообщения могли прийти или уйти,
    а экран должен показывать то, что есть в базе. То же для галереи
    слайдшоу — её набор меняется галочками ведущего.
    """
    runtime = registry.get(event.slug)
    runtime.screen.sync_items(event.store.live_ids())
    runtime.screen.sync_slideshow(event.store.slideshow_ids())
    return runtime


_STATIC_VERSION: str | None = None
_STATIC_URL_RE = re.compile(r"(/static/[A-Za-z0-9._-]+)")


def _static_version() -> str:
    """Отметка версии статики: меняется, когда меняются файлы.

    Нужна, чтобы браузер после обновления сервера не продолжал
    полгода держать старые скрипты. Если ссылка на файл не изменилась,
    кеш браузера живёт неделями, а код на сервере давно другой: страница
    падает с «такая функция не определена», и гость ничего не может
    отправить. С отметкой версии в адресе после обновления браузер
    приходит за файлами заново.
    """
    global _STATIC_VERSION

    if _STATIC_VERSION is None:
        newest = 0.0
        try:
            for path in config.STATIC_DIR.glob("*"):
                if path.is_file():
                    newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
        _STATIC_VERSION = str(int(newest))
    return _STATIC_VERSION


def _page(name: str, base: str = "") -> HTMLResponse:
    """Отдаёт страницу и говорит ей, какой у неё адрес.

    Страницы событий живут по адресу /e/<адрес>/..., поэтому скриптам
    нужно знать этот префикс: иначе запросы уйдут в корень сайта, где
    ничего нет.
    """
    path = config.STATIC_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Страница не найдена")
    html = path.read_text(encoding="utf-8")
    # отметка версии в ссылках на скрипты и стили: см. _static_version
    html = _STATIC_URL_RE.sub(rf"\g<1>?v={_static_version()}", html)
    if base:
        inject = f"<script>window.LF={{base:{json.dumps(base)}}};</script>"
        html = html.replace("</head>", f"{inject}\n</head>", 1)
    return HTMLResponse(html)


# --------------------------------------------------------------------------
# Админка
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def page_root():
    """Корень — вход в админку. Событие без события не открыть."""
    return RedirectResponse("/admin", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
async def page_admin(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    return _page("admin.html")


@app.get("/admin/login", response_class=HTMLResponse)
async def page_admin_login(request: Request):
    if auth.is_admin(request):
        return RedirectResponse("/admin", status_code=302)
    response = _page("admin-login.html")
    response.headers["Cache-Control"] = "no-store"
    return response


def _require_admin(request: Request) -> None:
    if not auth.is_admin(request):
        raise HTTPException(status_code=403, detail="Нужен вход в админку")


@app.post("/api/admin/login")
async def api_admin_login(request: Request, password: str = Form("")):
    if auth.admin_rate_limited(request):
        raise HTTPException(
            status_code=429, detail="Слишком много попыток. Подождите 5 минут."
        )
    if not auth.check_admin_password(password):
        auth.admin_record_fail(request)
        raise HTTPException(status_code=401, detail="Неверный пароль")
    auth.admin_clear_fails(request)
    response = JSONResponse({"ok": True})
    response.headers["Cache-Control"] = "no-store"
    auth.set_admin_cookie(response)
    return response


@app.post("/api/admin/logout")
async def api_admin_logout():
    response = JSONResponse({"ok": True})
    auth.clear_admin_cookie(response)
    return response


@app.get("/api/admin/events")
async def api_admin_events(request: Request, base: str = ""):
    _require_admin(request)
    root = (base or config.PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")
    return {
        "events": events.list_events(),
        "base": root,
        "password": config.admin_settings().get("admin_password", ""),
    }


@app.post("/api/admin/events")
async def api_admin_create(request: Request):
    _require_admin(request)
    form = await request.form()
    try:
        event = events.create_event(
            title=str(form.get("title", "")),
            subtitle=str(form.get("subtitle", "")),
            password=str(form.get("password", "") or "") or None,
            slug=str(form.get("slug", "") or "") or None,
        )
    except EventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось создать папку: {exc}")

    root = events.base_url_from(request)
    return {"ok": True, "event": event.summary(root), "links": event.links(root)}


@app.get("/api/admin/events/{slug}")
async def api_admin_event(slug: str, request: Request):
    _require_admin(request)
    event = _event_or_404(slug)
    root = events.base_url_from(request)
    return {
        "event": event.summary(root),
        "links": event.links(root),
        "code": event.get("invite_token", ""),
        "password": event.get("mod_password", "") or "",
        "size": events.event_size(event),
    }


@app.post("/api/admin/events/{slug}/update")
async def api_admin_update(slug: str, request: Request):
    _require_admin(request)
    event = _event_or_404(slug)
    form = await request.form()
    data: dict = {}

    title = str(form.get("title", "")).strip()
    if title:
        data["title"] = " ".join(title.split())[:80]
    if "subtitle" in form:
        data["subtitle"] = " ".join(str(form.get("subtitle", "")).split())[:160]

    password = str(form.get("password", "") or "")
    if password:
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="Пароль — от 6 символов")
        salt = secrets.token_bytes(16).hex()
        data["mod_salt"] = salt
        data["mod_hash"] = config.hash_password(password, salt)
        data["mod_password"] = password
    if form.get("clear_password"):
        data["mod_salt"] = secrets.token_bytes(16).hex()
        data["mod_hash"] = ""
        data["mod_password"] = ""

    if form.get("rotate_invite"):
        data["invite_token"] = config.invite_token(8)

    if data:
        event.update(**data)
    return {"ok": True, "event": event.summary(events.base_url_from(request))}


@app.post("/api/admin/events/{slug}/delete")
async def api_admin_delete(slug: str, request: Request):
    _require_admin(request)
    event = _event_or_404(slug)
    _prepared.discard(event.slug)

    # сперва рвём соединения: иначе экран продолжит показывать удалённое событие
    runtime = registry.drop(slug)
    if runtime is not None:
        await runtime.hub.close_all()
    try:
        removed = events.delete_event(slug)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось удалить: {exc}")
    if not removed:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    return {"ok": True, "slug": slug}


@app.get("/api/admin/events/{slug}/qr.svg")
async def api_admin_qr(slug: str, request: Request):
    _require_admin(request)
    event = _event_or_404(slug)
    return _qr_svg(event.invite_link(events.base_url_from(request)))


# --------------------------------------------------------------------------
# Страницы события
# --------------------------------------------------------------------------
@app.get("/e/{slug}/", response_class=HTMLResponse)
async def page_feed(slug: str, request: Request):
    """Общая витрина события.

    Отправителю, который уже вошёл через QR, лента не показывается:
    он уводится на свою страницу отправки. Иначе сообщения, задуманные
    для экрана в зале, видны всем подряд и сюрприз пропадает.
    Ведущий может открыть витрину — у него свой вход.
    """
    event = _prepare(_event_or_404(slug))
    if auth.current_participant(request, event) is not None:
        if not auth.is_moderator(request, event):
            return RedirectResponse(f"/e/{event.slug}/send", status_code=302)
    return _page("feed.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/screen", response_class=HTMLResponse)
async def page_screen(slug: str):
    event = _prepare(_event_or_404(slug))
    return _page("screen.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/join", response_class=HTMLResponse)
async def page_join(slug: str, request: Request, t: str = ""):
    event = _prepare(_event_or_404(slug))
    if not auth.invite_is_valid(event, t) and auth.current_participant(request, event) is None:
        return RedirectResponse(f"/e/{event.slug}", status_code=302)
    return _page("join.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/send", response_class=HTMLResponse)
async def page_send(slug: str, request: Request):
    event = _prepare(_event_or_404(slug))
    if auth.current_participant(request, event) is None:
        return RedirectResponse(f"/e/{event.slug}/join", status_code=302)
    return _page("send.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/moderator", response_class=HTMLResponse)
async def page_moderator(slug: str, request: Request):
    event = _prepare(_event_or_404(slug))
    if not auth.is_moderator(request, event):
        return RedirectResponse(f"/e/{event.slug}/moderator/login", status_code=302)
    return _page("moderator.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/moderator/login", response_class=HTMLResponse)
async def page_moderator_login(slug: str, request: Request):
    event = _prepare(_event_or_404(slug))
    if auth.is_moderator(request, event):
        return RedirectResponse(f"/e/{event.slug}/moderator", status_code=302)
    response = _page("login.html", base=f"/e/{event.slug}")
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------
# Публичный API события
# --------------------------------------------------------------------------
@app.get("/api/health")
async def api_health() -> dict:
    """Проверка живости для Docker HEALTHCHECK и балансировщика."""
    return {"ok": True, "events": len(events.list_events())}


@app.get("/e/{slug}/api/config")
async def api_config(slug: str):
    event = _event_or_404(slug)
    return {
        "slug": event.slug,
        "event_title": event.get("title", "Живая лента"),
        "event_subtitle": event.get("subtitle", ""),
        # заставку видит и экран, поэтому она не под паролем
        "splash": event.splash_url(),
        # лимит текста отдаём клиенту, чтобы страница отправки не держала
        # своё число: сервер — единственный источник правды
        "max_text_len": config.MAX_TEXT_LEN,
    }


@app.post("/e/{slug}/api/join")
async def api_join(slug: str, request: Request, name: str = Form(""), invite: str = Form("")):
    event = _prepare(_event_or_404(slug))
    if not auth.invite_is_valid(event, invite):
        raise HTTPException(status_code=403, detail="Ссылка-приглашение недействительна")
    if not name.strip():
        raise HTTPException(status_code=400, detail="Введите имя")
    person = auth.join(event, name, invite)
    response = JSONResponse(
        {"ok": True, "name": person.name, "token": person.token,
         "stats": event.store.stats()}
    )
    auth.set_participant_cookie(response, event, person.token)
    return response


@app.get("/e/{slug}/api/me")
async def api_me(slug: str, request: Request):
    event = _prepare(_event_or_404(slug))
    person = auth.current_participant(request, event)
    if person is None:
        return {"joined": False}
    return {
        "joined": True,
        "name": person.name,
        "token": person.token,
        "messages": event.store.participant_messages(person.id),
        "stats": event.store.stats(),
    }


@app.post("/e/{slug}/api/messages")
async def api_send(
    slug: str,
    request: Request,
    body: str = Form(""),
    image: UploadFile | None = File(None),
):
    event = _prepare(_event_or_404(slug))
    person = auth.current_participant(request, event)
    if person is None:
        raise HTTPException(status_code=401, detail="Сначала отсканируйте QR и введите имя")

    body = (body or "").strip()
    # Длинный текст обрезать молча нельзя: гость будет думать, что на экране
    # показано всё, а покажут первые 56 символов. Лучше отказать сразу.
    if len(body) > config.MAX_TEXT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Текст длиннее {config.MAX_TEXT_LEN} символов не помещается "
                   f"на экран. Сократите на {len(body) - config.MAX_TEXT_LEN}.",
        )

    stored: str | None = None

    if image is not None and image.filename:
        data = await image.read()
        try:
            stored = images.save_upload(data, event.uploads_dir, image.filename)
        except images.ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if not body and not stored:
        raise HTTPException(status_code=400, detail="Напишите текст или приложите фото")

    message = event.store.create_message(person.id, person.name, body, stored)

    # модерации нет: сообщение сразу уходит в эфир и в очередь показа
    runtime = _runtime(event)
    runtime.screen.sync_items(event.store.live_ids())
    if runtime.screen.current_id is None:
        runtime.screen.index = 0
    payload = {
        "type": "new",
        "message": message,
        "messages": event.store.live_messages(),
        "screen": runtime.screen.state(),
    }
    await runtime.hub.to_display(payload)
    await runtime.hub.to_moderator(payload)
    await runtime.hub.to_participant(person.token, {"type": "refresh"})
    return {"ok": True, "message": message}


@app.get("/e/{slug}/api/messages")
async def api_messages(slug: str, limit: int = 300):
    event = _prepare(_event_or_404(slug))
    return {
        "messages": event.store.live_messages(limit=min(limit, 500)),
        "stats": event.store.stats(),
        "screen": _runtime(event).screen.state(),
    }


@app.get("/e/{slug}/api/stats")
async def api_stats(slug: str) -> dict:
    event = _prepare(_event_or_404(slug))
    return event.store.stats()


@app.get("/e/{slug}/media/{name}")
async def media(slug: str, name: str):
    event = _event_or_404(slug)
    safe = Path(name).name
    uploads = event.uploads_dir.resolve()
    path = (uploads / safe).resolve()
    try:
        path.relative_to(uploads)
    except ValueError:
        raise HTTPException(status_code=404, detail="Не найдено")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Не найдено")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/e/{slug}/splash.jpg")
async def splash_file(slug: str):
    """Заставка события. Открытая: экран получает её сам."""
    event = _event_or_404(slug)
    if not event.splash_path.exists():
        raise HTTPException(status_code=404, detail="Заставка не загружена")
    return FileResponse(
        event.splash_path, headers={"Cache-Control": "no-cache"}
    )


def _qr_svg(link: str) -> Response:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(
        buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
    )


@app.get("/e/{slug}/api/invite-link")
async def api_invite_link(slug: str, request: Request) -> dict:
    """Ссылка-приглашение для показа на экране.

    Открытая по назначению: QR висит на проекторе перед залом,
    гости должны получить адрес, не входя в кабинет.
    """
    event = _event_or_404(slug)
    return {
        "link": event.invite_link(events.base_url_from(request)),
        "code": event.get("invite_token", ""),
    }


@app.get("/e/{slug}/api/qr.svg")
async def qr_svg(slug: str, request: Request):
    event = _event_or_404(slug)
    return _qr_svg(event.invite_link(events.base_url_from(request)))


# --------------------------------------------------------------------------
# Кабинет ведущего
# --------------------------------------------------------------------------
def _require_moderator(request: Request, event: Event) -> None:
    if not auth.is_moderator(request, event):
        raise HTTPException(status_code=403, detail="Только для модератора")


@app.post("/e/{slug}/api/moderator/login")
async def api_login(slug: str, request: Request, password: str = Form("")):
    event = _prepare(_event_or_404(slug))
    if auth.moderator_rate_limited(request, event):
        raise HTTPException(
            status_code=429, detail="Слишком много попыток. Подождите 5 минут."
        )
    if not auth.check_moderator_password(event, password):
        auth.moderator_record_fail(request, event)
        raise HTTPException(status_code=401, detail="Неверный пароль")
    auth.moderator_clear_fails(request, event)
    response = JSONResponse({"ok": True})
    # ответ на вход нельзя отдавать из кеша: иначе сам факт входа
    # и cookie останутся в кеше браузера и в общем прокси
    response.headers["Cache-Control"] = "no-store"
    auth.set_moderator_cookie(response, event)
    return response


@app.post("/e/{slug}/api/moderator/logout")
async def api_logout(slug: str, request: Request):
    event = _event_or_404(slug)
    response = JSONResponse({"ok": True})
    auth.clear_moderator_cookie(response, event)
    return response


@app.post("/e/{slug}/api/moderator/password")
async def api_change_password(slug: str, request: Request, new_password: str = Form("")):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    new_password = new_password.strip()
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль от 6 символов")
    salt = secrets.token_bytes(16).hex()
    event.update(
        mod_salt=salt,
        mod_hash=config.hash_password(new_password, salt),
        mod_password="",
    )
    return {"ok": True}


async def _refresh(event: Event) -> dict:
    """Пересобираем очередь показа и рассылаем всем актуальное состояние."""
    runtime = _runtime(event)
    runtime.screen.sync_items(event.store.live_ids())
    payload = {
        "type": "refresh",
        "messages": event.store.live_messages(),
        "stats": event.store.stats(),
        "screen": runtime.screen.state(),
    }
    await runtime.hub.to_display(payload)
    await runtime.hub.to_moderator(payload)
    return payload


async def _mark_shown(event: Event, message_id: int | None) -> None:
    """Отметить показанное сообщение и сообщить об этом всем."""
    if not message_id:
        return
    runtime = registry.get(event.slug)
    try:
        event.store.mark_shown(message_id)
    except Exception:
        return
    await runtime.hub.broadcast_all(
        {"type": "shown", "id": message_id, "shown_at": time.time(),
         "screen": runtime.screen.state()}
    )


@app.post("/e/{slug}/api/moderator/delete")
async def api_delete(slug: str, request: Request, message_id: int = Form(...)):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    token_before = event.store.author_token(message_id)
    # 4.0: удаляем строку из ленты, но НЕ стираем файл фотографии.
    # Раньше здесь был images.delete_upload — и ведущий, убирая неудачный
    # кадр из очереди, затирал оригинал навсегда. Фотография остаётся
    # в архиве, её можно забрать кнопкой «сохранить и скачать все».
    event.store.delete_message(message_id)
    result = await _refresh(event)
    if token_before:
        runtime = registry.get(event.slug)
        with contextlib.suppress(Exception):
            await runtime.hub.to_participant(token_before, {"type": "refresh"})
    return {"ok": True, **result}


@app.post("/e/{slug}/api/moderator/edit")
async def api_edit(
    slug: str,
    request: Request,
    message_id: int = Form(...),
    body: str = Form(""),
    author: str = Form(""),
):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    body = (body or "").strip()
    if len(body) > config.MAX_TEXT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Текст длиннее {config.MAX_TEXT_LEN} символов не помещается "
                   f"на экран. Сократите на {len(body) - config.MAX_TEXT_LEN}.",
        )
    event.store.edit_message(
        message_id,
        body=body,
        author=(author or "").strip()[:40] or None,
    )
    result = await _refresh(event)
    return {"ok": True, **result}


@app.post("/e/{slug}/api/moderator/move")
async def api_move(slug: str, request: Request, message_id: int = Form(...),
                   direction: str = Form(...)):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    event.store.move_message(message_id, direction)
    result = await _refresh(event)
    return {"ok": True, **result}


@app.post("/e/{slug}/api/moderator/splash")
async def api_splash(slug: str, request: Request):
    """Загрузка или удаление заставки события."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    form = await request.form()

    if str(form.get("remove", "")).lower() in ("1", "true", "on"):
        event.remove_splash()
        url = None
    else:
        image = form.get("image")
        if image is None or not getattr(image, "filename", ""):
            raise HTTPException(status_code=400, detail="Выберите картинку")
        data = await image.read()
        try:
            event.save_splash(data, image.filename)
        except images.ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        url = event.splash_url()

    # сообщаем экрану новый адрес: заставка могла смениться или исчезнуть,
    # а страница экрана могла быть открыта всё это время
    runtime = _runtime(event)
    await runtime.hub.broadcast_all(
        {"type": "screen", "screen": runtime.screen.state(), "splash": url}
    )
    return {"ok": True, "splash": url}


@app.post("/e/{slug}/api/moderator/screen")
async def api_screen_control(slug: str, request: Request, action: str = Form(...),
                             value: str = Form("")):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    runtime = _runtime(event)
    screen = runtime.screen

    if action == "play":
        await screen.set_playing(True)
    elif action == "pause":
        await screen.set_playing(False)
    elif action == "next":
        await screen.next()
    elif action == "prev":
        await screen.prev()
    elif action == "reset":
        # начать круг заново и снять отметки «уже показывалось»
        event.store.reset_shown()
        screen.index = 0
        screen.reset_deadline()
    elif action == "interval":
        await screen.set_interval(int(value or 12))
        event.update(interval=screen.interval)
    elif action == "slideshow-interval":
        # 4.0: длительность снимка в слайдшоу настраивается отдельно от ленты.
        # Снимки гости держат дольше, а лента идёт бодрее — одна настройка
        # на оба режима заставляла выбирать что-то одно.
        await screen.set_slideshow_interval(int(value or 15))
        event.update(slideshow_interval=screen.slideshow_interval)
    elif action == "show":
        await screen.go_to(int(value))
    elif action == "qr":
        # QR поверх сообщений: показ встаёт, пока окно не скрыто
        screen.qr_visible = str(value).lower() in ("1", "true", "on", "show")
        screen.reset_deadline()
    elif action == "splash":
        # заставка на весь экран: показ тоже встаёт
        screen.splash_visible = str(value).lower() in ("1", "true", "on", "show")
        screen.reset_deadline()
    elif action == "slideshow":
        # 4.0: галерея слайдшоу занимает экран, обычная лента на это время
        # встаёт — так же, как заставка. Пустую галерею запускать смысла нет:
        # ведущий увидит пустой экран и решит, что всё сломалось.
        want = str(value).lower() in ("1", "true", "on", "show")
        if want and not screen.slideshow_items:
            raise HTTPException(
                status_code=400,
                detail="В слайдшоу пока нет ни одной фотографии.",
            )
        screen.slideshow_visible = want
        screen.reset_deadline()
    elif action == "slideshow-show":
        # показать снимок из галереи. Если слайдшоу не запущено — запускаем:
        # иначе ведущий нажал «показать сейчас», а на экране обычная лента
        if not screen.slideshow_visible:
            if not screen.slideshow_items:
                raise HTTPException(
                    status_code=400,
                    detail="В слайдшоу пока нет ни одной фотографии.",
                )
            screen.slideshow_visible = True
        if int(value or 0) not in screen.slideshow_items:
            raise HTTPException(status_code=400, detail="Этот снимок не в галерее.")
        await screen.slideshow_go_to(int(value))
    else:
        raise HTTPException(status_code=400, detail="Неизвестное действие")

    state = screen.state()
    await runtime.hub.broadcast_all({"type": "screen", "screen": state})
    await _mark_shown(event, screen.current_id)
    return {"ok": True, "screen": state}


@app.get("/e/{slug}/api/moderator/state")
async def api_moderator_state(slug: str, request: Request):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    runtime = _runtime(event)
    return {
        "messages": event.store.live_messages(),
        "stats": event.store.stats(),
        "screen": runtime.screen.state(),
        "online": runtime.hub.online,
        "splash": event.splash_url(),
    }


@app.get("/e/{slug}/api/moderator/invite")
async def api_invite(slug: str, request: Request):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    return {
        "link": event.invite_link(events.base_url_from(request)),
        "code": event.get("invite_token", ""),
    }


@app.post("/e/{slug}/api/moderator/reset-shown")
async def api_reset_shown(slug: str, request: Request):
    """Сбросить отметки «уже показывалось» — начало нового круга."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    event.store.reset_shown()
    payload = {
        "type": "refresh",
        "messages": event.store.live_messages(),
        "stats": event.store.stats(),
        "screen": _runtime(event).screen.state(),
    }
    await registry.get(event.slug).hub.broadcast_all(payload)
    return {"ok": True, **payload}


@app.post("/e/{slug}/api/moderator/rotate-invite")
async def api_rotate_invite(slug: str, request: Request):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    new_token = config.invite_token(8)
    event.update(invite_token=new_token)
    return {"ok": True, "code": new_token}


# --------------------------------------------------------------------------
# Слайдшоу (4.0): галочка у фотографии, галерея и запуск на экран
# --------------------------------------------------------------------------
@app.post("/e/{slug}/api/moderator/slideshow")
async def api_slideshow_toggle(
    slug: str,
    request: Request,
    message_id: int = Form(...),
    on: str = Form("0"),
):
    """Поставить или снять галочку «в слайдшоу».

    В галерею идут только фотографии: чистые и с текстом. Текст без картинки
    поставить нельзя — на экране слайдшоу снимок занимает почти весь кадр, и
    строка текста там ничего не добавляет. Об этом ведущий узнаёт из ответа,
    а не из молчаливого «ничего не вышло».
    """
    event = _event_or_404(slug)
    _require_moderator(request, event)
    wanted = str(on).lower() in ("1", "true", "on", "yes", "show")

    updated = event.store.set_slideshow(message_id, wanted)
    if updated is None:
        raise HTTPException(
            status_code=400,
            detail="В слайдшоу идут только фотографии и фотографии с текстом.",
        )

    result = await _refresh(event)
    return {"ok": True, "message": updated, **result}


@app.post("/e/{slug}/api/moderator/slideshow-clear")
async def api_slideshow_clear(slug: str, request: Request):
    """Опустошить галерею слайдшоу. Фотографии при этом остаются в архиве."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    cleared = event.store.clear_slideshow()
    result = await _refresh(event)
    return {"ok": True, "cleared": cleared, **result}


# --------------------------------------------------------------------------
# Архив фотографий (4.0): он не зависит от ленты
# --------------------------------------------------------------------------
@app.get("/e/{slug}/api/moderator/photos")
async def api_photos_summary(slug: str, request: Request):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    info = archive.summary(event.uploads_dir)
    return {
        "ok": True,
        "count": info["count"],
        "bytes": info["bytes"],
        "in_slideshow": len(event.store.slideshow_ids()),
    }


@app.get("/e/{slug}/api/moderator/photos.zip")
async def api_photos_download(slug: str, request: Request):
    """Отдать весь архив одним файлом — ведущий сохраняет себе.

    Отдаём потоком: фотографий может быть много, а сервер маленький, и
    собирать zip целиком в памяти нельзя.
    """
    event = _event_or_404(slug)
    _require_moderator(request, event)
    folder = f"photos-{event.slug}"
    return StreamingResponse(
        archive.zip_stream(event.uploads_dir, prefix=f"{folder}/"),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{folder}.zip"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/e/{slug}/api/moderator/photos-delete-all")
async def api_photos_delete_all(slug: str, request: Request):
    """Стереть архив фотографий целиком. Необратимо.

    Файлы удаляются, и сообщения с фотографиями уходят из ленты: на экране
    остались бы битые картинки. Текстовые сообщения и слайдшоу не страдают.
    Если ведущему нужны фотографии — сначала «сохранить и скачать все».
    """
    event = _event_or_404(slug)
    _require_moderator(request, event)
    removed_files = archive.wipe(event.uploads_dir)
    removed_messages = event.store.delete_photo_messages()
    result = await _refresh(event)
    return {
        "ok": True,
        "removed_files": removed_files,
        "removed_messages": removed_messages,
        **result,
    }


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------
class _FakeRequest:
    """Минимальная обёртка, чтобы достать куки и адрес из WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self.cookies = dict(ws.cookies)
        self.client = ws.client
        # у WebSocket нет base_url, но адрес собирается из того же запроса.
        # Схему переводим: в ссылке приглашения нужен http, а не ws
        scheme = "https" if ws.url.scheme in ("wss", "https") else "http"
        self.base_url = f"{scheme}://{ws.url.netloc}/"


async def _pump(ws: WebSocket) -> None:
    """Держит соединение живым, отвечая на ping."""
    beat = asyncio.create_task(keepalive(ws))
    try:
        while True:
            await ws.receive_text()
    finally:
        beat.cancel()


@app.websocket("/e/{slug}/ws/display")
async def ws_display(ws: WebSocket, slug: str) -> None:
    event = events.get_event(slug)
    if event is None:
        await ws.close(code=4404)
        return
    _prepare(event)
    runtime = registry.get(event.slug)

    await ws.accept()
    runtime.hub.add_display(ws)
    try:
        await ws.send_json(
            {
                "type": "init",
                "messages": event.store.live_messages(),
                "stats": event.store.stats(),
                "screen": runtime.screen.state(),
            }
        )
        await _pump(ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runtime.hub.remove(ws)


@app.websocket("/e/{slug}/ws/participant")
async def ws_participant(ws: WebSocket, slug: str) -> None:
    event = events.get_event(slug)
    if event is None:
        await ws.close(code=4404)
        return
    _prepare(event)
    person = auth.current_participant(_FakeRequest(ws), event)
    if person is None:
        await ws.close(code=4401)
        return

    runtime = registry.get(event.slug)
    await ws.accept()
    runtime.hub.add_participant(ws, person.token)
    try:
        await ws.send_json(
            {"type": "init", "messages": event.store.participant_messages(person.id)}
        )
        await _pump(ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runtime.hub.remove(ws)


@app.websocket("/e/{slug}/ws/moderator")
async def ws_moderator(ws: WebSocket, slug: str) -> None:
    event = events.get_event(slug)
    if event is None:
        await ws.close(code=4404)
        return
    _prepare(event)
    # close с кодом 4403 браузер видит как onclose с этим кодом —
    # клиент на него не переподключается, а уходит на страницу входа
    if not auth.is_moderator(_FakeRequest(ws), event):
        await ws.close(code=4403)
        return

    runtime = registry.get(event.slug)
    await ws.accept()
    runtime.hub.add_moderator(ws)
    try:
        await ws.send_json(
            {
                "type": "init",
                "messages": event.store.live_messages(),
                "stats": event.store.stats(),
                "screen": runtime.screen.state(),
                "invite": event.invite_link(events.base_url_from(_FakeRequest(ws))),
                "online": runtime.hub.online,
            }
        )
        await _pump(ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runtime.hub.remove(ws)


# --------------------------------------------------------------------------
# Фоновые задачи
# --------------------------------------------------------------------------
async def _on_advance(runtime: EventRuntime) -> None:
    """Показ переключил слайд: сообщаем подписчикам и ставим отметку."""
    event = events.get_event(runtime.slug)
    if event is None:
        return
    state = runtime.screen.state()
    await runtime.hub.broadcast_all({"type": "screen", "screen": state})
    # прошёл полный круг — список «уже показанного» начинаем заново
    if runtime.screen.take_wrapped():
        with contextlib.suppress(Exception):
            event.store.reset_shown()
            await runtime.hub.broadcast_all({"type": "unshown"})
    # Отметку «показано» ставим только когда на экране шла обычная лента.
    # В слайдшоу показываются другие снимки, а current_id — это слайд ленты,
    # которого на экране не было: отметка на нём сбила бы счётчик у ведущего.
    if not runtime.screen.slideshow_visible:
        await _mark_shown(event, state.get("current_id"))


@app.on_event("startup")
async def on_startup() -> None:
    events.init_all()
    config.admin_settings()

    # поднимаем показ у уже существующих событий
    for event in events.iter_events():
        try:
            _prepare(event)
            data = event.load()
            runtime = registry.get(event.slug)
            runtime.screen.interval = int(data.get("interval", 12) or 12)
            runtime.screen.slideshow_interval = int(
                data.get("slideshow_interval", 15) or 15
            )
            runtime.screen.playing = bool(data.get("autoplay", True))
            runtime.screen.sync_items(event.store.live_ids())
        except Exception:
            # одно битое событие не должно мешать остальным
            continue

    registry.set_advance_handler(_on_advance)
    await registry.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await registry.stop()


# --------------------------------------------------------------------------
# Статика
# --------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")

auth.install_middleware(app)