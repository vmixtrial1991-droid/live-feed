"""События: отдельная папка, своя база и свои настройки.

Событие — это каталог в data/events/<slug>/:

    config.json   настройки, пароль ведущего, код приглашения
    app.db        сообщения и участники
    uploads/      фотографии

Отдельного реестра событий нет и он не нужен: список событий — это просто
содержимое data/events. Поэтому удаление события не может «оставить хвосты»
в реестре: удалили папку — и ничего не осталось. Два события не пересекаются
тем, что у них физически разные базы и папки с фотографиями.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Iterator

from . import config
from . import images
from .db import EventStore

# Адрес события попадает в путь и в cookie, поэтому набор символов строгий
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Занятые адреса приложения: такое имя события завело бы в никуда
RESERVED_SLUGS = {
    "admin", "api", "static", "media", "ws", "e", "health",
    "favicon.ico", "robots.txt", "events",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class EventError(ValueError):
    """Событие нельзя создать или изменить."""


def slugify(title: str) -> str:
    """Из названия делает адрес: «Свадьба Маши и Саши» → «svadba-mashi-i-sashi»."""
    out: list[str] = []
    for ch in (title or "").lower():
        out.append(_TRANSLIT.get(ch, ch))
    slug = re.sub(r"[^a-z0-9]+", "-", "".join(out)).strip("-")
    return slug[:48] or "event"


def validate_slug(slug: str) -> str:
    """Проверяет адрес события. Бросает EventError, если такой использовать нельзя."""
    value = (slug or "").strip().lower()
    if not value:
        raise EventError("Адрес не может быть пустым")
    if not SLUG_RE.match(value):
        raise EventError(
            "Адрес может содержать только латиницу, цифры и дефис, "
            "и начинаться с латинской буквы или цифры"
        )
    if value in RESERVED_SLUGS:
        raise EventError(f"Адрес «{value}» занят приложением")
    return value


def unique_slug(base: str) -> str:
    """Добавляет -2, -3… пока адрес не станет свободным."""
    root = validate_slug(base)
    if not (config.EVENTS_DIR / root).exists():
        return root
    for n in range(2, 1000):
        candidate = f"{root[:44]}-{n}"
        if not (config.EVENTS_DIR / candidate).exists():
            return candidate
    raise EventError("Слишком много событий с похожим названием")


class Event:
    """Одно событие: адрес, папка, настройки и база."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.dir = config.EVENTS_DIR / slug
        self.store = EventStore(self.dir / "app.db")

    # --- пути -----------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    @property
    def uploads_dir(self) -> Path:
        return self.dir / "uploads"

    @property
    def splash_path(self) -> Path:
        """Заставка события: одна картинка на весь показ."""
        return self.dir / "splash.jpg"

    def splash_url(self) -> str:
        """Адрес заставки для экрана или None, если её нет."""
        if not self.splash_path.exists():
            return None
        # отметка меняется при загрузке, чтобы браузер не держал старую
        return f"/e/{self.slug}/splash.jpg?v={int(self.splash_path.stat().st_mtime)}"

    def save_splash(self, data: bytes, filename: str) -> str:
        """Заменяет заставку события. Возвращает адрес файла."""
        return images.save_upload(
            data, self.dir, filename, name=self.splash_path.name
        )

    def remove_splash(self) -> bool:
        try:
            self.splash_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    @property
    def db_path(self) -> Path:
        return self.dir / "app.db"

    def exists(self) -> bool:
        return self.config_path.exists()

    # --- настройки ------------------------------------------------------
    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.config_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            raise EventError(f"Настройки события «{self.slug}» не читаются")

    def save(self, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def update(self, **values: Any) -> dict[str, Any]:
        """Меняет настройки и пишет файл. Значение None удаляет ключ."""
        data = self.load()
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        self.save(data)
        return data

    # --- подписи и ссылки -----------------------------------------------
    @property
    def secret(self) -> str:
        return str(self.get("secret", ""))

    def moderator_cookie_name(self) -> str:
        return f"{config.MODERATOR_COOKIE_PREFIX}{self.slug}"

    def participant_cookie_name(self) -> str:
        return f"{config.PARTICIPANT_COOKIE_PREFIX}{self.slug}"

    def moderator_cookie_value(self) -> str:
        return config.sign(self.secret, f"moderator:{self.slug}")

    def check_moderator_password(self, password: str) -> bool:
        return config.verify_password(
            password or "",
            str(self.get("mod_salt", "")),
            str(self.get("mod_hash", "")),
        )

    def invite_link(self, base_url: str) -> str:
        return join_link(base_url, self.slug, str(self.get("invite_token", "")))

    def links(self, base_url: str) -> dict[str, str]:
        """Три ссылки, которые админ отдаёт организатору события.

        Ссылка гостям — это и есть адрес из QR-кода, с кодом приглашения
        внутри: её можно отправить в чат, и человек сразу попадёт на
        страницу ввода имени, а не куда-то в общий список.
        """
        base = (base_url or "").rstrip("/")
        return {
            "join": self.invite_link(base),
            "screen": f"{base}/e/{self.slug}/screen",
            "moderator": f"{base}/e/{self.slug}/moderator",
            "feed": self.invite_link(base),
        }

    def summary(self, base_url: str = "") -> dict[str, Any]:
        """Краткая карточка события для списка в админке."""
        data = self.load()
        stats = self.store.stats()
        return {
            "slug": self.slug,
            "title": data.get("title") or self.slug,
            "subtitle": data.get("subtitle", ""),
            "created_at": data.get("created_at", 0),
            "messages": stats["total"],
            "participants": stats["participants"],
        }


def join_link(base_url: str, slug: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/e/{slug}/join?t={token}"


def base_url_from(request) -> str:
    """Адрес, под которым событие видно снаружи — для QR-кода и ссылок.

    Если у объекта нет base_url (например, это обёртка WebSocket), отдаём
    пустую строку: лучше ссылка без адреса, чем падение на ровном месте.
    """
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    base = getattr(request, "base_url", None)
    return str(base).rstrip("/") if base else ""


def get_event(slug: str) -> Event | None:
    """Событие по адресу или None, если его нет."""
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        return None
    event = Event(slug)
    return event if event.exists() else None


def iter_events() -> Iterator[Event]:
    """Все события, новые сверху."""
    if not config.EVENTS_DIR.exists():
        return
    for path in sorted(config.EVENTS_DIR.iterdir(), reverse=True):
        if not path.is_dir() or path.name.startswith("."):
            continue
        event = Event(path.name)
        if event.exists():
            yield event


def list_events() -> list[dict[str, Any]]:
    """Список событий для админки."""
    out = []
    for event in iter_events():
        try:
            out.append(event.summary())
        except (EventError, OSError):
            # битое событие не должно ронять весь список
            continue
    return out


def create_event(
    title: str,
    subtitle: str = "",
    password: str | None = None,
    slug: str | None = None,
) -> Event:
    """Создаёт событие с настройками, базой и паролем ведущего."""
    title = " ".join((title or "").split())[:80]
    if not title:
        raise EventError("Введите название события")

    password = (password or "").strip()
    if password and len(password) < 6:
        raise EventError("Пароль ведущего — от 6 символов")

    event = Event(unique_slug(slug or slugify(title)))
    if event.dir.exists():
        raise EventError("Событие с таким адресом уже есть")

    event.dir.mkdir(parents=True)
    event.uploads_dir.mkdir(parents=True, exist_ok=True)

    # пустой пароль — ведущий не сможет войти, пока не задаст его в админке
    salt = secrets.token_bytes(16).hex()
    event.save(
        {
            "slug": event.slug,
            "title": title,
            "subtitle": " ".join((subtitle or "").split())[:160],
            "invite_token": config.invite_token(8),
            "mod_salt": salt,
            "mod_hash": config.hash_password(password, salt) if password else "",
            "mod_password": password,
            "secret": secrets.token_hex(32),
            "interval": 12,
            "autoplay": True,
            "created_at": time.time(),
        }
    )
    event.store.init()
    return event


def delete_event(slug: str) -> bool:
    """Удаляет событие целиком: папку с базой и фотографиями.

    Возвращает False, если такого события нет. Путь проверяется на то,
    что он внутри data/events: иначе можно было бы удалить что угодно.
    """
    event = get_event(slug)
    if event is None:
        return False

    root = config.EVENTS_DIR.resolve()
    target = event.dir.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    if target == root:
        return False

    shutil.rmtree(target, ignore_errors=False)
    return not event.dir.exists()


def event_size(event: Event) -> int:
    """Размер папки события в байтах — показываем в админке."""
    total = 0
    for path in event.dir.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def init_all() -> None:
    """Готовим хранилище при старте приложения."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    config.admin_settings()


def delete_data_dir() -> None:
    """Полный сброс хранилища. Нужен тестам, в приложении не вызывается."""
    shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)