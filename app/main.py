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
from . import quiz as quizmod
from .events import Event, EventError
from .realtime import EventRuntime, keepalive, registry

# ВАЖНО: FastAPI этот аргумент не принимает. Раньше конструктор молча
# глотал неизвестные kwargs (поэтому locally не падало), а с fastapi 0.152
# незнакомый kwarg — TypeError при старте. Доверие к X-Forwarded-For
# обеспечивает auth.client_ip(): сам проверяет, от какого адреса пришло
# соединение (config.TRUSTED_PROXY_HOSTS / TRUSTED_PROXY_NETS).
app = FastAPI(
    title="Живая лента — несколько событий",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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
            # 5.0: квиз переживает перезапуск сервера. Ведущий открывал
            # вопрос, uvicorn перезагрузился — зал продолжает отвечать,
            # а экран показывает ту же стадию, а не пустоту.
            phase = str(data.get("quiz_phase") or "idle")
            if phase in quizmod.PHASES:
                screen.quiz_phase = phase
            try:
                screen.quiz_question = int(data.get("quiz_question") or 0) or None
            except (TypeError, ValueError):
                screen.quiz_question = None
            screen.quiz_visible = bool(data.get("quiz_visible"))
            screen.quiz_final = bool(data.get("quiz_final"))
            # момент открытия вопроса: по нему экран достраивает отсчёт
            # после перезапуска, а не начинает его заново
            opened = data.get("quiz_opened_at")
            try:
                screen.quiz_opened_at = float(opened) if opened else None
            except (TypeError, ValueError):
                screen.quiz_opened_at = None
            # стадия, из которой вышли на промежуточную таблицу: без неё
            # перезапуск посреди таблицы отнимал бы у ведущего возврат
            prev = data.get("quiz_prev_phase")
            screen.quiz_prev_phase = prev if prev in quizmod.BACK_PHASES else None
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
    runtime.screen.sync_slideshow(event.store.gallery_ids())
    return runtime


async def _quiz_frame(event: Event, runtime: EventRuntime) -> None:
    """Отправляет текущий кадр квиза экрану и сигнал страницам.

    Экран получает целую картину (вопрос, распределение, рейтинг) — он
    один на всех. Страницам гостей — только сигнал обновиться: каждый
    спросит СВОЮ картину у своего API, чтобы чужой ответ не разбегался
    по залу.
    """
    payload = quizmod.display_payload(event, runtime.screen, event.store)
    await runtime.hub.to_display({"type": "quiz", "quiz": payload})
    await runtime.hub.to_quiz({"type": "quiz_update"})


def _quiz_persist(event: Event, screen) -> None:
    """Стадия квиза живёт в конфиге события.

    Перезапуск сервера посреди вечера не должен стирать квиз из зала:
    ведущий откроет страницу/экран, а стадия на месте. В idle ключи
    убираем — конфиг не обрастает мусором после каждого квиза.
    """
    if screen.quiz_phase == "idle":
        event.update(quiz_phase=None, quiz_question=None, quiz_visible=None,
                     quiz_final=None, quiz_opened_at=None, quiz_prev_phase=None)
    else:
        event.update(
            quiz_phase=screen.quiz_phase,
            quiz_question=screen.quiz_question,
            quiz_visible=screen.quiz_visible,
            quiz_final=screen.quiz_final,
            quiz_opened_at=screen.quiz_opened_at,
            quiz_prev_phase=screen.quiz_prev_phase,
        )


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


def _page(name: str, base: str = "", extra: str = "") -> HTMLResponse:
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
        inject = f"<script>window.LF={{base:{json.dumps(base)}{extra}}};</script>"
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


def _require_participant(request: Request, event: Event) -> None:
    """Билет на просмотр: без cookie участника — 403.

    4.1: адрес события публичен, а право видеть ленту и фото даёт код
    приглашения. Cookie ставится только после его проверки (на /join), так
    что её отсутствие означает: код не вводили — значит, и смотреть нечего.
    """
    if auth.current_participant(request, event) is None:
        raise HTTPException(status_code=403, detail="Нужен код приглашения")


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
async def api_admin_qr(slug: str, request: Request, kind: str = ""):
    _require_admin(request)
    event = _event_or_404(slug)
    # 5.0: kind=quiz — отдельный QR на квиз; ссылка строится тем же кодом
    link = (event.quiz_link if kind == "quiz" else event.invite_link)
    return _qr_svg(link(events.base_url_from(request)))


# --------------------------------------------------------------------------
# Страницы события
# --------------------------------------------------------------------------
@app.get("/e/{slug}/", response_class=HTMLResponse)
async def page_feed(slug: str, request: Request, t: str = ""):
    """Витрина события. Открыта только администратору и ведущему.

    Раньше страницу видел всякий, кто знал адрес: проверялась лишь сессия
    отправителя, а тому, у кого её не было, показывалась лента целиком.
    Сообщения, задуманные для экрана в зале, висели в открытом доступе.

    Теперь:
      * администратор и ведущий смотрят витрину — им это нужно;
      * вошедший отправитель уходит на свою страницу отправки;
      * остальные — на страницу входа, и код приглашения не теряется.
    """
    event = _prepare(_event_or_404(slug))
    if auth.is_admin(request) or auth.is_moderator(request, event):
        # витрина живёт на ws/display, а тот открывается только с ключом
        # экрана — передаём ключ странице так же, как странице экрана
        return _page("feed.html", base=f"/e/{event.slug}",
                     extra=f",screenKey:{json.dumps(event.screen_key)}")
    if auth.current_participant(request, event) is not None:
        return RedirectResponse(f"/e/{event.slug}/send", status_code=302)
    target = f"/e/{event.slug}/join"
    if t:
        target += f"?t={t}"
    return RedirectResponse(target, status_code=302)


@app.get("/e/{slug}/screen", response_class=HTMLResponse)
async def page_screen(slug: str, request: Request, d: str = ""):
    event = _prepare(_event_or_404(slug))
    # 4.1: ключ экрана. Без него экран не открывается — адрес проектора
    # публичен, и без ключа любой мог бы смотреть фото и конфиг события.
    # Ключ прячется из адресной строки после загрузки (см. screen.js),
    # поэтому при обновлении страницы F5 ключа в URL уже нет — тогда
    # пускаем по экранному cookie, который поставили при первом заходе.
    if not (auth.screen_key_valid(event, d) or auth.is_screen(request, event)):
        raise HTTPException(status_code=403, detail="Нужен ключ экрана")
    # ключ прячем из адресной строки после загрузки (см. screen.js), но
    # страницу отдаем с ним в window.LF, чтобы скрипты могли тянуть фото.
    response = _page("screen.html", base=f"/e/{event.slug}",
                     extra=f",screenKey:{json.dumps(event.screen_key)}")
    auth.set_screen_cookie(response, event)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'"
    )
    return response


@app.get("/e/{slug}/join", response_class=HTMLResponse)
async def page_join(slug: str, request: Request, t: str = ""):
    """Страница входа. Конечная точка, а не перепутье.

    Раньше человек без верного кода уходил отсюда на витрину — то есть
    витрина была не «закрыта для всех», а просто чуть дальше по ссылке.
    Теперь страница входа всегда последняя: сама она объясняет, что
    открыть её надо по QR-коду приглашения.

    4.1: адрес события публичен, а право смотреть ленту и фото — билетом.
    Если код приглашения из ?t= годится, выдаём билет прямо здесь: создаём
    запись участника (имя он впишет дальше) и ставим cookie. Без кода
    cookie не будет, и дальше страница отправки не откроется.
    """
    event = _prepare(_event_or_404(slug))
    if auth.current_participant(request, event) is not None:
        return RedirectResponse(f"/e/{event.slug}/send", status_code=302)
    ok = auth.invite_is_valid(event, t)
    reason = "" if ok else ("no_code" if not t else "bad_code")
    return _page("join.html", base=f"/e/{event.slug}",
                 extra=f",inviteOk:{json.dumps(ok)},inviteReason:{json.dumps(reason)}")


@app.get("/e/{slug}/send", response_class=HTMLResponse)
async def page_send(slug: str, request: Request, t: str = ""):
    event = _prepare(_event_or_404(slug))
    if auth.current_participant(request, event) is None:
        # Код приглашения надо передать дальше. Раньше он тут терялся, и
        # цепочка выглядела так: /send?t=КОД → /join (без кода) → витрина.
        # То есть страница отправки сама выкидывала гостя на витрину —
        # ровно то, чего мы добиваемся, убрав оттуда кнопку.
        target = f"/e/{event.slug}/join"
        if t:
            target += f"?t={t}"
        return RedirectResponse(target, status_code=302)
    return _page("send.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/quiz", response_class=HTMLResponse)
async def page_quiz(slug: str, request: Request, t: str = ""):
    """Страница квиза (5.0). Адрес публичный, содержимое — по билету.

    Отдельного входа нет: гость, который уже отмечался в ленте, приходит
    сюда со своим cookie и сразу видит вопросы. Новый гость вводит тот же
    код приглашения — страница сама покажет форму, когда API скажет «нет
    билета». Ссылка из QR несёт ?t=, чтобы код был подставлен заранее.
    """
    event = _prepare(_event_or_404(slug))
    return _page("quiz.html", base=f"/e/{event.slug}")


@app.get("/e/{slug}/moderator", response_class=HTMLResponse)
async def page_moderator(slug: str, request: Request):
    event = _prepare(_event_or_404(slug))
    if not auth.is_moderator(request, event):
        return RedirectResponse(f"/e/{event.slug}/moderator/login", status_code=302)
    # 4.1: кнопка «Экран» в кабинете обязана нести ключ экрана — без ?d=
    # экран отвечает «Нужен ключ экрана», и ведущий не смог бы открыть
    # свою же ссылку в соседней вкладке.
    return _page("moderator.html", base=f"/e/{event.slug}",
                 extra=f",screenKey:{json.dumps(event.screen_key)}")


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
    """Проверка живости для Docker HEALTHCHECK и балансировщика.

    4.1: список событий убран — он раскрывал, сколько и какие мероприятия
    идут на сервере. Здоровье сервера не зависит от числа событий.
    """
    return {"ok": True}


@app.get("/e/{slug}/api/config")
async def api_config(slug: str, request: Request):
    event = _prepare(_event_or_404(slug))
    # Конфиг нужен и экрану (название, заставка), и участнику. Экран получает
    # свой билет при открытии /screen, участник — при проверке кода на /join.
    if not (auth.is_admin(request) or auth.is_moderator(request, event)
            or auth.current_participant(request, event) is not None
            or auth.is_screen(request, event)
            or auth.screen_key_valid(event, request.query_params.get("d"))):
        raise HTTPException(status_code=403, detail="Нужен код приглашения")
    return {
        "slug": event.slug,
        "event_title": event.get("title", "Живая лента"),
        "event_subtitle": event.get("subtitle", ""),
        # заставку видит и экран, поэтому она не под паролем
        "splash": event.splash_url(),
        "screen_key": event.screen_key,
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


# --------------------------------------------------------------------------
# Квиз (5.0): API гостя
# --------------------------------------------------------------------------
# Билет у квиза тот же, что у ленты: кто уже отмечался в зале, тот на
# квизе узнаётся автоматически, без второго входа. Имя правится на месте —
# участник остаётся тем же, меняются только следующие сообщения и ответы.

@app.get("/e/{slug}/api/quiz")
async def api_quiz(slug: str, request: Request):
    """Текущая стадия квиза для гостя: вопрос, свой ответ, очки."""
    event = _prepare(_event_or_404(slug))
    person = auth.current_participant(request, event)
    if person is None:
        raise HTTPException(status_code=403, detail="Нужен код приглашения")
    runtime = _runtime(event)
    data = quizmod.participant_view(event.store, runtime.screen, person.id)
    data["name"] = person.name
    return data


@app.post("/e/{slug}/api/quiz/answer")
async def api_quiz_answer(
    slug: str, request: Request, question_id: int = Form(...), option: int = Form(...)
):
    """Ответ гостя. До закрытия вопроса его можно передумать — замена."""
    event = _prepare(_event_or_404(slug))
    person = auth.current_participant(request, event)
    if person is None:
        raise HTTPException(status_code=403, detail="Нужен код приглашения")

    runtime = _runtime(event)
    screen = runtime.screen
    if screen.quiz_phase != "open" or screen.quiz_question != question_id:
        raise HTTPException(status_code=409, detail="Вопрос не открыт или уже закрыт")
    q = event.store.quiz_get_question(question_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    if not (0 <= option < len(q["options"])):
        raise HTTPException(status_code=400, detail="Нет такого варианта")

    event.store.quiz_answer(question_id, person.id, option)
    # счётчик ответивших уходит на экран и в панель ведущего; остальные
    # страницы молчат: свой ответ они уже знают
    await _quiz_frame(event, runtime)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    return {"ok": True, "answered_total": event.store.quiz_answered_total(question_id)}


@app.post("/e/{slug}/api/quiz/name")
async def api_quiz_name(slug: str, request: Request, name: str = Form("")):
    """Сменить имя. Тот же участник, те же очки — другое имя.

    Ранее отправленные сообщения остаются со старым автором: переписывать
    прошлое назад нельзя. Всё новое — лента, ответы, таблица лидеров —
    пойдёт уже под новым именем.
    """
    event = _prepare(_event_or_404(slug))
    person = auth.current_participant(request, event)
    if person is None:
        raise HTTPException(status_code=403, detail="Нужен код приглашения")
    clean = " ".join((name or "").split())[:40]
    if not clean:
        raise HTTPException(status_code=400, detail="Введите имя")
    event.store.rename_participant(person.id, clean)
    return {"ok": True, "name": clean}


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
        raise HTTPException(status_code=403, detail="Нужен код приглашения")

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
    # 4.1: дельта вместо полного снимка. За вечер уходило по JSON со всеми
    # фото вечера на каждое сообщение — на слабой сети экран отваливался.
    # Полный снимок остаётся только на подключении (init) и при ручном
    # обновлении (refresh).
    payload = {
        "type": "new",
        "added": [message],
        "screen": runtime.screen.state(),
        "stats": event.store.stats(),
        "splash": event.splash_url(),
    }
    await runtime.hub.to_display(payload)
    await runtime.hub.to_moderator(payload)
    await runtime.hub.to_participant(person.token, {"type": "refresh"})
    return {"ok": True, "message": message}


@app.get("/e/{slug}/api/messages")
async def api_messages(slug: str, request: Request, limit: int = 300):
    event = _prepare(_event_or_404(slug))
    # Лента и имена файлов — только по билету. Экран читает через WebSocket,
    # поэтому здесь билет участника (или ведущего/админа) обязателен.
    if not (auth.is_admin(request) or auth.is_moderator(request, event)
            or auth.current_participant(request, event) is not None
            or auth.is_screen(request, event)
            or auth.screen_key_valid(event, request.query_params.get("d"))):
        raise HTTPException(status_code=403, detail="Нужен код приглашения")

    # 4.1: архив (полный список сообщений и фото) — только модератору/админу.
    # Гостю с кодом — только то, что сейчас на показе, плюс отправка.
    is_mod = auth.is_admin(request) or auth.is_moderator(request, event)
    if is_mod:
        msgs = event.store.live_messages(limit=min(limit, 500))
        now = int(time.time())
        exp = now + config.MEDIA_SIG_TTL
        for m in msgs:
            if m.get("image"):
                m["image_url"] = (
                    f"/e/{event.slug}/media/{m['image']}?sig="
                    f"{auth.media_sig(event, m['image'], exp)}&exp={exp}"
                )
        return {
            "messages": msgs,
            "stats": event.store.stats(),
            "screen": _runtime(event).screen.state(),
        }
    # гость/экран: только текущий слайд
    return {
        "messages": [],
        "stats": {},
        "screen": _runtime(event).screen.state(),
    }


@app.get("/e/{slug}/api/stats")
async def api_stats(slug: str, request: Request) -> dict:
    event = _prepare(_event_or_404(slug))
    if not (auth.is_admin(request) or auth.is_moderator(request, event)
            or auth.current_participant(request, event) is not None):
        raise HTTPException(status_code=403, detail="Нужен код приглашения")
    return event.store.stats()


@app.get("/e/{slug}/media/{name}")
async def media(slug: str, name: str, request: Request, w: int = 0):
    event = _prepare(_event_or_404(slug))
    safe = Path(name).name
    uploads = event.uploads_dir.resolve()
    path = (uploads / safe).resolve()
    try:
        path.relative_to(uploads)
    except ValueError:
        raise HTTPException(status_code=404, detail="Не найдено")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Не найдено")

    # 4.1: фото — не вечная ссылка по имени файла. Доступ либо по билету
    # участника/ведущего/админа, либо по короткой подписанной ссылке
    # (5-10 минут), либо экрану проектора (свой билет).
    allowed = (
        auth.is_admin(request)
        or auth.is_moderator(request, event)
        or auth.current_participant(request, event) is not None
        or auth.is_screen(request, event)
        or auth.screen_key_valid(event, request.query_params.get("d"))
        or auth.media_sig_valid(event, safe, request.query_params.get("sig"),
                                request.query_params.get("exp"))
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Нужен код приглашения")

    # 4.1: миниатюры для сетки в кабинете ведущего. Полноразмерные файлы
    # в сетке — единственное место, где реально ложится устройство.
    if w and w > 0 and w <= 1024:
        import io as _io
        from PIL import Image as _Image
        thumb_name = f".thumb_{w}_{safe}"
        thumb_path = uploads / thumb_name
        if not thumb_path.exists():
            try:
                with _Image.open(path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((w, w), _Image.LANCZOS)
                    im.save(thumb_path, "JPEG", quality=70, optimize=True)
            except Exception:
                thumb_path = path  # не смогли — отдаём оригинал
        if thumb_path != path and thumb_path.exists():
            return FileResponse(thumb_path, media_type="image/jpeg",
                               headers={"Cache-Control": "private, max-age=86400"})

    # Подписанные ссылки короткоживущие — не кешируем надолго.
    sig = request.query_params.get("sig")
    cache = "private, max-age=60" if sig else "private, max-age=3600"
    return FileResponse(path, headers={"Cache-Control": cache})


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
async def api_invite_link(slug: str, request: Request, d: str = "") -> dict:
    """Ссылка-приглашение для показа на экране.

    4.1: ключ экрана обязателен — адрес проектора публичен, и без ключа
    любой мог бы получить код приглашения и смотреть фото.
    """
    event = _prepare(_event_or_404(slug))
    if not (auth.is_admin(request) or auth.is_moderator(request, event)
            or auth.screen_key_valid(event, d)
            or auth.screen_key_valid(event, request.query_params.get("d"))):
        raise HTTPException(status_code=403, detail="Нужен ключ экрана")
    return {
        "link": event.invite_link(events.base_url_from(request)),
        "code": event.get("invite_token", ""),
    }


@app.get("/e/{slug}/api/qr.svg")
async def qr_svg(slug: str, request: Request, d: str = "", kind: str = ""):
    event = _prepare(_event_or_404(slug))
    if not (auth.is_admin(request) or auth.is_moderator(request, event)
            or auth.screen_key_valid(event, d)
            or auth.screen_key_valid(event, request.query_params.get("d"))):
        raise HTTPException(status_code=403, detail="Нужен ключ экрана")
    # 5.0: kind=quiz — QR на страницу квиза (тот же код приглашения внутри)
    link = (event.quiz_link if kind == "quiz" else event.invite_link)
    return _qr_svg(link(events.base_url_from(request)))


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
        "gallery": event.store.gallery_items(),
        "stats": event.store.stats(),
        "screen": runtime.screen.state(),
        "splash": event.splash_url(),
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
    updated = event.store.edit_message(
        message_id,
        body=body,
        author=(author or "").strip()[:40] or None,
    )
    # 4.0.1: тот же снимок не должен ездить по экрану с двумя надписями.
    # Поправили текст в ленте — поправился и он в галерее слайдшоу.
    if updated and updated.get("image"):
        event.store.gallery_follow(updated["image"], updated["body"], updated["author"])
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
        if want:
            # «Запустить слайдшоу» означает «показать сейчас»: если перед этим
            # галерея была на паузе, запускаем её в активный показ, а не
            # возвращаем к застывшему кадру
            screen.slideshow_playing = True
        screen.reset_deadline()
    elif action == "slideshow-play":
        # пауза галереи: своя, ленту она не трогает
        await screen.set_slideshow_playing(
            str(value).lower() in ("1", "true", "on", "yes", "show")
        )
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
        "gallery": event.store.gallery_items(),
        "stats": event.store.stats(),
        "screen": runtime.screen.state(),
        "online": runtime.hub.online,
        "splash": event.splash_url(),
        "screen_key": event.screen_key,
        "screen_url": f"{events.base_url_from(request)}/e/{event.slug}/screen?d={event.screen_key}",
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
        "gallery": event.store.gallery_items(),
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
# Квиз (5.0): панель ведущего — вопросы и управление стадиями
# --------------------------------------------------------------------------
# Лимиты своих, отдельных от ленты, и это не придирка: вопрос читают с
# дальних рядов зала, поэтому текст длиннее 200 знаков уже теряется,
# а шесть вариантов — потолок, дальше экран не вмещает.

MAX_QUESTION_LEN = 200
MAX_OPTION_LEN = 80
MIN_OPTIONS = 2
MAX_OPTIONS = 6
MIN_TIMER = 5
MAX_TIMER = 600

# метка «поле не трогать» для формы правки: пустая строка слишком нужна
# для «стереть», а выдуманное слово никто случайно не пришлёт
FORM_KEEP = "__keep__"


def _quiz_seconds(raw: str) -> int | None:
    """Время на ответ из формы: 0 — «без отсчёта», прочее — 5..600.

    Пустая строка для формы значит «поле не присылали», поэтому стирать
    отсчёт приходится нулём: он и так не проходит проверку диапазона,
    место свободное.

    Верхняя граница не для строгости: таймер крупным числом стоит на
    проекторе, и «3600 секунд» зал читает как опечатку, а не как отсчёт.
    """
    raw = (raw or "").strip()
    if not raw or raw == "0":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Время ответа — целое число секунд")
    if not (MIN_TIMER <= value <= MAX_TIMER):
        raise HTTPException(
            status_code=400,
            detail=f"Время ответа — от {MIN_TIMER} до {MAX_TIMER} секунд",
        )
    return value


def _quiz_options(raw: str) -> list[str]:
    """Варианты из формы: список прилетает JSON-ом, пустые отсекаем."""
    try:
        parsed = json.loads(raw or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Варианты должны быть списком")
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail="Варианты должны быть списком")
    options = [" ".join(str(o).split())[:MAX_OPTION_LEN] for o in parsed]
    options = [o for o in options if o]
    if not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
        raise HTTPException(
            status_code=400,
            detail=f"Вариантов должно быть от {MIN_OPTIONS} до {MAX_OPTIONS}",
        )
    return options


@app.get("/e/{slug}/api/moderator/quiz")
async def api_mod_quiz(slug: str, request: Request):
    """Сводка для панели: вопросы, стадии, счётчики ответивших, лидеры."""
    event = _prepare(_event_or_404(slug))
    _require_moderator(request, event)
    runtime = _runtime(event)
    return quizmod.host_state(event.store, runtime.screen)


@app.post("/e/{slug}/api/moderator/quiz/add")
async def api_mod_quiz_add(
    slug: str,
    request: Request,
    text: str = Form(""),
    options: str = Form(""),
    correct: int = Form(-1),
    scored: str = Form("1"),
    seconds: str = Form(""),
):
    """Новый вопрос встаёт в конец очереди; порядок правится кнопками."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    text = " ".join((text or "").split())[:MAX_QUESTION_LEN]
    if not text:
        raise HTTPException(status_code=400, detail="Напишите текст вопроса")
    opts = _quiz_options(options)
    graded = str(scored).lower() in ("1", "true", "on", "yes")
    if graded and not (0 <= correct < len(opts)):
        raise HTTPException(status_code=400, detail="Отметьте правильный вариант")
    # у «хитрого» вопроса правильного нет намеренно: смысл в разбросе мнений
    question = event.store.quiz_add_question(
        text, opts, correct if graded else -1, graded, _quiz_seconds(seconds)
    )
    runtime = _runtime(event)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    return {"ok": True, "question": question}


@app.post("/e/{slug}/api/moderator/quiz/update")
async def api_mod_quiz_update(
    slug: str,
    request: Request,
    question_id: int = Form(...),
    text: str = Form(""),
    options: str = Form(""),
    correct: int = Form(-2),
    scored: str = Form(""),
    seconds: str = Form(FORM_KEEP),
):
    """Правка вопроса. Пустые поля не трогаем: форма присылает только то,
    что изменилось. -2 у correct означает «не менять» ( -1 сам по себе —
    законное значение «правильного нет» ).

    seconds — тот же фокус: «не менять» помечен специальным значением,
    а пустая строка для формы значит «поля нет вовсе». Стереть отсчёт —
    значит прислать 0."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    old = event.store.quiz_get_question(question_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")

    fields: dict = {}
    if text.strip():
        fields["text"] = " ".join(text.split())[:MAX_QUESTION_LEN]
    if options.strip():
        fields["options"] = _quiz_options(options)
    if scored != "":
        graded = str(scored).lower() in ("1", "true", "on", "yes")
        fields["scored"] = graded
        if not graded:
            fields["correct"] = -1
    if correct != -2:
        fields["correct"] = correct
    elif "scored" in fields and fields["scored"]:
        fields["correct"] = old["correct"]
    if seconds != FORM_KEEP:
        fields["seconds"] = _quiz_seconds(seconds)

    final_options = fields.get("options", old["options"])
    final_scored = fields.get("scored", old["scored"])
    final_correct = fields.get("correct", old["correct"])
    if final_scored and not (0 <= final_correct < len(final_options)):
        raise HTTPException(status_code=400, detail="Отметьте правильный вариант")

    question = event.store.quiz_update_question(question_id, **fields)
    runtime = _runtime(event)
    # меняли открытый/закрытый вопрос — экрану и гостям нужен новый кадр
    await _quiz_frame(event, runtime)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    return {"ok": True, "question": question}


@app.post("/e/{slug}/api/moderator/quiz/duplicate")
async def api_mod_quiz_duplicate(slug: str, request: Request, question_id: int = Form(...)):
    """Копия вопроса сразу за оригиналом.

    Копия пустая в смысле ответов: у неё свой id, поэтому прошлые ответы
    к ней не переходят. Ведущему это нужно, когда вопрос работает и его
    хочется повторить с другим текстом, не набирая варианты заново.
    """
    event = _event_or_404(slug)
    _require_moderator(request, event)
    question = event.store.quiz_duplicate_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    runtime = _runtime(event)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    await _quiz_frame(event, runtime)  # «3 из 10» на экране сдвинулось
    return {"ok": True, "question": question}


@app.post("/e/{slug}/api/moderator/quiz/delete")
async def api_mod_quiz_delete(slug: str, request: Request, question_id: int = Form(...)):
    """Удалить вопрос; ответы уходят каскадом.

    Если удаляли именно тот вопрос, который сейчас открыт, квиз останавли-
    вается: висящая фаза без вопроса — это дыра, экран показал бы пустоту.
    """
    event = _event_or_404(slug)
    _require_moderator(request, event)
    if not event.store.quiz_delete_question(question_id):
        raise HTTPException(status_code=404, detail="Вопрос не найден")

    runtime = _runtime(event)
    screen = runtime.screen
    if screen.quiz_question == question_id:
        screen.quiz_phase, screen.quiz_question, screen.quiz_visible = "idle", None, False
        screen.quiz_final, screen.quiz_opened_at = False, None
        _quiz_persist(event, screen)
    await _quiz_frame(event, runtime)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    return {"ok": True}


@app.post("/e/{slug}/api/moderator/quiz/move")
async def api_mod_quiz_move(
    slug: str, request: Request, question_id: int = Form(...), direction: str = Form("up")
):
    event = _event_or_404(slug)
    _require_moderator(request, event)
    if not event.store.quiz_move_question(question_id, direction):
        raise HTTPException(status_code=400, detail="Вопрос уже на своём месте")
    runtime = _runtime(event)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    await _quiz_frame(event, runtime)  # «3 из 10» на экране могло сдвинуться
    return {"ok": True}


@app.post("/e/{slug}/api/moderator/quiz/control")
async def api_mod_quiz_control(
    slug: str, request: Request, action: str = Form(...), question_id: int = Form(0)
):
    """Переключатель стадий квиза: тот же экран, что и у ленты, только режим.

    Ведущий ходит по цепочке одной кнопкой, и каждое действие — следующий
    её шаг:

    start  — запустить квиз: экран уходит на холл игры (название и QR),
             зал подключается, вопросы можно дописывать прямо сейчас;
    open   — вопрос открыт, зал отвечает (можно передумать до закрытия);
    close  — приём закрыт, показываем распределение, правильный ещё нет;
    reveal — добавляем правильный ответ;
    next   — следующий по порядку, а после последнего вопроса — финал;
    board  — промежуточная таблица лидеров (листает всех);
    back   — вернуться с промежуточной таблицы к вопросу, где игра стояла;
    feed   — убрать квиз с экрана, оставив зал отвечать (фаза не трогается);
    show   — вернуть экран к текущей стадии;
    stop   — закончить квиз, экран снова показывает ленту;
    reset  — стереть все ответы (тот же набор, чистый прогон).
    """
    event = _prepare(_event_or_404(slug))
    _require_moderator(request, event)
    runtime = _runtime(event)
    screen = runtime.screen
    store = event.store

    # сюда панель приходит только со своими стадиями; если в запросе чужая
    # строка — отказ, а не тихая подмена: иначе браузер с старым скриптом
    # начал бы рисовать то, чего сервер не показывал
    raw_phase = str((await request.form()).get("phase") or "").strip()
    if raw_phase and raw_phase not in quizmod.PHASES:
        raise HTTPException(status_code=400, detail="Такой стадии квиза нет")

    if action == "start":
        # запуск: экран плавно уходит на холл игры — название и QR, пока зал
        # подключается. Вопросы к этому моменту готовы не всегда, поэтому
        # требовать их здесь нельзя: ведущий дописывает колоду прямо в холле
        if screen.quiz_phase != "idle":
            raise HTTPException(status_code=400, detail="Квиз уже идёт")
        screen.quiz_phase, screen.quiz_question, screen.quiz_visible = "lobby", None, True
        screen.quiz_final, screen.quiz_opened_at, screen.quiz_prev_phase = False, None, None
    elif action == "open":
        qid = question_id or screen.quiz_question
        if not qid:
            first = store.quiz_list_questions()
            if not first:
                raise HTTPException(status_code=400, detail="Сначала добавьте вопросы")
            qid = first[0]["id"]
        if store.quiz_get_question(qid) is None:
            raise HTTPException(status_code=400, detail="Вопрос не найден")
        screen.quiz_phase, screen.quiz_question, screen.quiz_visible = "open", qid, True
        screen.quiz_final = False
        screen.quiz_prev_phase = None
        screen.quiz_opened_at = time.time()
    elif action == "close":
        if screen.quiz_phase != "open":
            raise HTTPException(status_code=400, detail="Вопрос сейчас не открыт")
        screen.quiz_phase = "closed"
        # отсчёт больше не нужен: приём закрыт, время вопроса кончилось
        screen.quiz_opened_at = None
    elif action == "reveal":
        if screen.quiz_phase != "closed":
            raise HTTPException(status_code=400, detail="Сначала закройте приём ответов")
        screen.quiz_phase = "reveal"
    elif action == "next":
        # дальше идут только после закрытого приёма: иначе зал успевает
        # ответить на прошлый вопрос, пока на экране уже новый
        if screen.quiz_phase not in ("closed", "reveal"):
            raise HTTPException(status_code=400, detail="Сначала закройте приём ответов")
        order = [q["id"] for q in store.quiz_list_questions()]
        if not order:
            raise HTTPException(status_code=400, detail="Сначала добавьте вопросы")
        i = order.index(screen.quiz_question) if screen.quiz_question in order else -1
        if i + 1 >= len(order):
            # последний вопрос прожит: экран сам уходит на финальную
            # таблицу, ведущему не нужно ничего искать кнопками
            screen.quiz_phase, screen.quiz_question = "board", None
            screen.quiz_visible, screen.quiz_final = True, True
            screen.quiz_opened_at, screen.quiz_prev_phase = None, None
        else:
            screen.quiz_phase, screen.quiz_question, screen.quiz_visible = "open", order[i + 1], True
            screen.quiz_final = False
            screen.quiz_prev_phase = None
            screen.quiz_opened_at = time.time()
    elif action == "board":
        if screen.quiz_phase == "idle":
            raise HTTPException(status_code=400, detail="Квиз сейчас не идёт")
        # финальная таблица — итог вечера, а не пауза: повторное «лидеры»
        # снимало с неё флаг финала, и итог превращался в промежуточную
        # таблицу, которая сама листает всех
        if screen.quiz_phase == "board" and screen.quiz_final:
            raise HTTPException(
                status_code=400,
                detail="Игра уже закончена — финальная таблица на экране",
            )
        # на таблицу уходят посмотреть лидеров и вернуться, поэтому помним,
        # откуда именно: шаг «назад» ведёт туда же, а не в начало вопроса
        if screen.quiz_phase != "board":
            screen.quiz_prev_phase = screen.quiz_phase
        screen.quiz_phase, screen.quiz_visible = "board", True
        screen.quiz_final = False
    elif action == "back":
        # возврат с промежуточной таблицы. Финал не откатываем: последний
        # вопрос прожит, и остатка игры за ним нет
        if screen.quiz_phase != "board":
            raise HTTPException(status_code=400, detail="Сейчас и так не таблица лидеров")
        if screen.quiz_final:
            raise HTTPException(
                status_code=400,
                detail="Финальную таблицу вернуть некуда — завершите квиз",
            )
        target = quizmod.back_phase(screen)
        if target is None or (target != "lobby"
                              and store.quiz_get_question(screen.quiz_question) is None):
            raise HTTPException(status_code=400, detail="Возвращаться некуда: вопрос уже прожит")
        screen.quiz_phase, screen.quiz_visible = target, True
        screen.quiz_prev_phase = None
    elif action == "feed":
        screen.quiz_visible = False
    elif action == "show":
        if screen.quiz_phase == "idle":
            raise HTTPException(status_code=400, detail="Квиз сейчас не идёт")
        screen.quiz_visible = True
    elif action == "stop":
        screen.quiz_phase, screen.quiz_question, screen.quiz_visible = "idle", None, False
        screen.quiz_final, screen.quiz_opened_at = False, None
        screen.quiz_prev_phase = None
    elif action == "reset":
        removed = store.quiz_clear_answers()
    else:
        raise HTTPException(status_code=400, detail="Неизвестное действие")

    _quiz_persist(event, screen)
    await _quiz_frame(event, runtime)
    await runtime.hub.to_moderator({"type": "quiz_update"})
    data = quizmod.host_state(store, screen)
    if action == "reset":
        data["removed"] = removed
    return data


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
    """Поставить или снять галочку «в слайдшоу» у фотографии в ленте.

    Галерея хранит фотографии сама по себе и не связана со строками ленты,
    поэтому снимок, отмеченный здесь, переживёт удаление сообщения из ленты.
    И наоборот: снятие галочки не трогает ленту.

    В галерею идут только фотографии: чистые и с текстом. Текст без картинки
    поставить нельзя — об этом ведущий узнаёт из ответа, а не из молчаливого
    «ничего не вышло».
    """
    event = _event_or_404(slug)
    _require_moderator(request, event)
    wanted = str(on).lower() in ("1", "true", "on", "yes", "show")

    message = event.store.get_message(message_id)
    if message is None or not message.get("image"):
        raise HTTPException(
            status_code=400,
            detail="В слайдшоу идут только фотографии и фотографии с текстом.",
        )

    if wanted:
        event.store.gallery_add(
            message["image"], message.get("body", ""), message.get("author", ""),
            source_id=message["id"],
        )
    else:
        event.store.gallery_remove_image(message["image"])

    result = await _refresh(event)
    return {"ok": True, **result}


@app.post("/e/{slug}/api/moderator/gallery/drop")
async def api_gallery_drop(slug: str, request: Request, gallery_id: int = Form(...)):
    """Убрать снимок из галереи. Файл в архиве остаётся."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    dropped = event.store.gallery_remove(gallery_id)
    result = await _refresh(event)
    return {"ok": dropped, **result}


@app.post("/e/{slug}/api/moderator/gallery/edit")
async def api_gallery_edit(
    slug: str, request: Request, gallery_id: int = Form(...), body: str = Form("")
):
    """Правка текста у снимка в галерее. Имя автора не трогаем."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    body = (body or "").strip()
    if len(body) > config.MAX_TEXT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Текст длиннее {config.MAX_TEXT_LEN} символов не поместится на экран.",
        )
    # Если снимок взят галочкой из строки ленты, правим именно её: у снимка
    # один текст, и две правки в двух местах разъедутся. Снимок, чьё
    # сообщение уже удалили из ленты, правится сам по себе.
    source_id = event.store.gallery_source(gallery_id)
    if source_id is not None:
        event.store.edit_message(source_id, body=body)
        message = event.store.get_message(source_id)
        if message:
            event.store.gallery_follow(message["image"], message["body"], message["author"])
    updated = event.store.gallery_edit(gallery_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="Снимка нет в галерее")
    result = await _refresh(event)
    return {"ok": True, "item": updated, **result}


@app.post("/e/{slug}/api/moderator/slideshow-clear")
async def api_slideshow_clear(slug: str, request: Request):
    """Опустошить галерею слайдшоу. Фотографии остаются в архиве и в ленте."""
    event = _event_or_404(slug)
    _require_moderator(request, event)
    cleared = event.store.gallery_clear()
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
        "in_gallery": event.store.gallery_count(),
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
    # Галерею тоже: её снимки ссылаются на стёртые файлы, и слайдшоу
    # показывал бы битые картинки
    removed_gallery = event.store.gallery_clear()
    removed_messages = event.store.delete_photo_messages()
    # показать слайдшоу, пока файлов нет, нельзя — возвращаем ленту
    runtime = registry.get(event.slug)
    runtime.screen.slideshow_visible = False
    runtime.screen.reset_deadline()
    result = await _refresh(event)
    return {
        "ok": True,
        "removed_files": removed_files,
        "removed_messages": removed_messages,
        "removed_gallery": removed_gallery,
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
async def ws_display(ws: WebSocket, slug: str, d: str = "") -> None:
    event = events.get_event(slug)
    if event is None:
        await ws.close(code=4404)
        return
    _prepare(event)
    # 4.1: ключ экрана. Без него канал не открывается — иначе любой, кто
    # знает адрес, смотрел бы весь эфир. Экранный cookie принимаем наравне
    # с ключом: страница сама прячет ?d= из адресной строки, и при F5
    # переподключение приходит без ключа, но с cookie из того же пути.
    if not (auth.screen_key_valid(event, d) or auth.is_screen(ws, event)):
        await ws.close(code=4403)
        return
    runtime = registry.get(event.slug)

    await ws.accept()
    runtime.hub.add_display(ws)
    try:
        await ws.send_json(
            {
                "type": "init",
                "messages": event.store.live_messages(),
                "gallery": event.store.gallery_items(),
                "stats": event.store.stats(),
                "screen": runtime.screen.state(),
                "splash": event.splash_url(),
                # 5.0: экран, открытый посреди квиза (F5, второй проектор),
                # получает текущую стадию сразу — без «покажи заново»
                "quiz": quizmod.display_payload(event, runtime.screen, event.store),
            }
        )
        await _pump(ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runtime.hub.remove(ws)


@app.websocket("/e/{slug}/ws/quiz")
async def ws_quiz(ws: WebSocket, slug: str) -> None:
    """Канал страниц квиза (5.0). Билет — тот же, что у ленты.

    По каналу летает только сигнал «изменилось». содержимое каждый гость
    забирает своим GET-ом: так его вопрос, его ответ и его очки не уезжают
    чужим телефонам.
    """
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
    runtime.hub.add_quiz(ws)
    try:
        await ws.send_json(
            {
                "type": "init",
                "quiz": quizmod.participant_view(event.store, runtime.screen, person.id),
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
                "gallery": event.store.gallery_items(),
                "stats": event.store.stats(),
                "screen": runtime.screen.state(),
                "splash": event.splash_url(),
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