"""Реальное время: хаб подписчиков, состояние показа и реестр событий.

На каждое событие приходится свой хаб и свой контроллер показа: сообщения
одного ивента не должны попадать на экран другого. Контроллеры заводятся
лениво, когда событие впервые понадобилось, и уходят вместе с удалением.

Переключение слайдов крутит один общий цикл, а не по задаче на событие:
иначе сотня событий держала бы сотню вечных задач.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Callable, Iterable

from fastapi import WebSocket

TICK_INTERVAL = 0.4


class Hub:
    """Рассылает события подписчикам трёх каналов одного события."""

    def __init__(self) -> None:
        self.display: set[WebSocket] = set()      # витрина и полный экран
        self.moderator: set[WebSocket] = set()    # кабинет модератора
        self.participants: dict[str, set[WebSocket]] = {}  # токен -> сокеты

    # --- подписка -------------------------------------------------------
    def add_display(self, ws: WebSocket) -> None:
        self.display.add(ws)

    def add_moderator(self, ws: WebSocket) -> None:
        self.moderator.add(ws)

    def add_participant(self, ws: WebSocket, token: str) -> None:
        self.participants.setdefault(token, set()).add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.display.discard(ws)
        self.moderator.discard(ws)
        # список копируем: чистка одного сокета меняет сам словарь
        for token, socks in list(self.participants.items()):
            socks.discard(ws)
            if not socks:
                self.participants.pop(token, None)

    def all_sockets(self) -> set[WebSocket]:
        out = set(self.display) | set(self.moderator)
        for socks in self.participants.values():
            out |= socks
        return out

    async def close_all(self, code: int = 4004) -> None:
        """Обрывает все соединения события — нужно при удалении события."""
        for ws in self.all_sockets():
            with contextlib.suppress(Exception):
                # close() на уже закрытом сокете бросает, поэтому гасим ошибку
                await ws.close(code=code)
        self.display.clear()
        self.moderator.clear()
        self.participants.clear()

    # --- рассылка -------------------------------------------------------
    @staticmethod
    async def _send_many(websockets: Iterable[WebSocket], payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        for ws in list(websockets):
            try:
                await ws.send_text(data)
            except Exception:
                pass

    async def to_display(self, payload: dict[str, Any]) -> None:
        await self._send_many(self.display, payload)

    async def to_moderator(self, payload: dict[str, Any]) -> None:
        await self._send_many(self.moderator, payload)

    async def to_participant(self, token: str, payload: dict[str, Any]) -> None:
        await self._send_many(self.participants.get(token, ()), payload)

    async def broadcast_all(self, payload: dict[str, Any]) -> None:
        await self._send_many(set(self.display) | set(self.moderator), payload)

    @property
    def online(self) -> dict[str, int]:
        return {
            "display": len(self.display),
            "moderator": len(self.moderator),
            "participants": sum(len(v) for v in self.participants.values()),
        }


class ScreenController:
    """Состояние полноэкранного показа: что сейчас на экране и когда переключать.

    Живёт на сервере, поэтому все экраны одного события показывают
    одинаковую картинку.
    """

    def __init__(self, interval: int = 12, autoplay: bool = True) -> None:
        self.interval = interval
        self.playing = autoplay
        self.items: list[int] = []
        self.index = 0
        self._deadline = time.monotonic() + interval
        self._lock = asyncio.Lock()
        self.qr_visible = False   # QR-код поверх сообщений
        self.splash_visible = False   # заставка на весь экран
        self._wrapped = False     # прошли полный круг — пора сбросить отметки «показано»

    @property
    def current_id(self) -> int | None:
        if not self.items:
            return None
        return self.items[min(self.index, len(self.items) - 1)]

    def sync_items(self, ids: list[int]) -> bool:
        """Обновляет список слайдов, удерживая текущий. True — если изменился."""
        if ids == self.items:
            return False
        current = self.current_id
        self.items = ids
        if current in ids:
            self.index = ids.index(current)
        else:
            self.index = 0
            self._deadline = time.monotonic() + self.interval
        return True

    async def next(self) -> None:
        async with self._lock:
            self._step(1)

    async def prev(self) -> None:
        async with self._lock:
            self._step(-1)

    async def go_to(self, message_id: int) -> None:
        async with self._lock:
            if message_id in self.items:
                self.index = self.items.index(message_id)
                self._deadline = time.monotonic() + self.interval

    async def set_playing(self, playing: bool) -> None:
        async with self._lock:
            self.playing = playing
            self._deadline = time.monotonic() + self.interval

    async def set_interval(self, seconds: int) -> None:
        async with self._lock:
            self.interval = max(3, min(300, int(seconds)))
            self._deadline = time.monotonic() + self.interval

    def _step(self, delta: int) -> None:
        if not self.items:
            return
        new_index = self.index + delta
        # отмечаем, что круг пройден: список «уже показанных» пора сбросить
        if new_index >= len(self.items) or new_index < 0:
            self._wrapped = True
        self.index = new_index % len(self.items)
        self._deadline = time.monotonic() + self.interval

    def due(self) -> bool:
        """Пора ли переключать слайд. Проверка без блокировки — только чтение."""
        if not self.playing or self.qr_visible or self.splash_visible:
            return False
        if len(self.items) <= 1:
            return False
        return time.monotonic() >= self._deadline

    async def step_if_due(self) -> bool:
        """Переключает слайд, если время пришло. True — если переключили."""
        if not self.due():
            return False
        async with self._lock:
            # пока ждали блокировку, время могло ещё не наступить
            if not self.playing or self.qr_visible or self.splash_visible:
                return False
            if len(self.items) <= 1 or time.monotonic() < self._deadline:
                return False
            self._step(1)
        return True

    def take_wrapped(self) -> bool:
        """True один раз, когда показ прошёл полный круг."""
        if self._wrapped:
            self._wrapped = False
            return True
        return False

    def state(self) -> dict[str, Any]:
        return {
            "playing": self.playing,
            "interval": self.interval,
            "current_id": self.current_id,
            "total": len(self.items),
            "index": min(self.index, len(self.items) - 1) if self.items else 0,
            "qr_visible": self.qr_visible,
            "splash_visible": self.splash_visible,
        }


class EventRuntime:
    """Всё живое по одному событию: подписчики и состояние показа."""

    def __init__(self, slug: str, interval: int = 12, autoplay: bool = True) -> None:
        self.slug = slug
        self.hub = Hub()
        self.screen = ScreenController(interval=interval, autoplay=autoplay)


class Registry:
    """Живые объекты всех событий. Создаётся один раз при старте."""

    def __init__(self) -> None:
        self._items: dict[str, EventRuntime] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._on_advance: Callable[[EventRuntime], Any] | None = None

    def set_advance_handler(self, handler: Callable[[EventRuntime], Any]) -> None:
        """Что делать, когда показ переключил слайд (рассылка, отметки)."""
        self._on_advance = handler

    def get(self, slug: str) -> EventRuntime:
        """Хаб и контроллер события, заводятся при первом обращении."""
        runtime = self._items.get(slug)
        if runtime is None:
            runtime = EventRuntime(slug)
            self._items[slug] = runtime
        return runtime

    def peek(self, slug: str) -> EventRuntime | None:
        return self._items.get(slug)

    def drop(self, slug: str) -> EventRuntime | None:
        """Забывает событие: соединения закрываются, состояние.showed теряется."""
        return self._items.pop(slug, None)

    def slugs(self) -> list[str]:
        return list(self._items)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            for slug, runtime in list(self._items.items()):
                try:
                    await self._tick_one(runtime)
                except Exception:
                    # одно сломанное событие не должно останавливать остальные
                    continue

    async def _tick_one(self, runtime: EventRuntime) -> None:
        if not await runtime.screen.step_if_due():
            return
        handler = self._on_advance
        if handler is not None:
            await handler(runtime)


registry = Registry()


async def keepalive(ws: WebSocket, every: float = 25.0) -> None:
    """Периодический ping, чтобы прокси и браузеры не рвали соединение."""
    try:
        while True:
            await asyncio.sleep(every)
            await ws.send_json({"type": "ping"})
    except asyncio.CancelledError:
        raise
    except Exception:
        pass