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
    """Рассылает события подписчикам четырёх каналов одного события."""

    def __init__(self) -> None:
        self.display: set[WebSocket] = set()      # витрина и полный экран
        self.moderator: set[WebSocket] = set()    # кабинет модератора
        self.participants: dict[str, set[WebSocket]] = {}  # токен -> сокеты
        # 5.0: зал, открывший страницу квиза. Билет участника тот же, что у
        # ленты, но канал отдельный: квиз меняется часто, а лента при этом
        # не обязана дёргать каждый телефон в зале.
        self.quiz: set[WebSocket] = set()

    # --- подписка -------------------------------------------------------
    def add_display(self, ws: WebSocket) -> None:
        self.display.add(ws)

    def add_moderator(self, ws: WebSocket) -> None:
        self.moderator.add(ws)

    def add_participant(self, ws: WebSocket, token: str) -> None:
        self.participants.setdefault(token, set()).add(ws)

    def add_quiz(self, ws: WebSocket) -> None:
        self.quiz.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.display.discard(ws)
        self.moderator.discard(ws)
        self.quiz.discard(ws)
        # список копируем: чистка одного сокета меняет сам словарь
        for token, socks in list(self.participants.items()):
            socks.discard(ws)
            if not socks:
                self.participants.pop(token, None)

    def all_sockets(self) -> set[WebSocket]:
        out = set(self.display) | set(self.moderator) | set(self.quiz)
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
        self.quiz.clear()

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

    async def to_quiz(self, payload: dict[str, Any]) -> None:
        """Рассылка по страницам квиза (5.0)."""
        await self._send_many(self.quiz, payload)

    async def broadcast_all(self, payload: dict[str, Any]) -> None:
        await self._send_many(set(self.display) | set(self.moderator), payload)

    @property
    def online(self) -> dict[str, int]:
        return {
            "display": len(self.display),
            "moderator": len(self.moderator),
            "participants": sum(len(v) for v in self.participants.values()),
            "quiz": len(self.quiz),
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
        # 4.0: галерея слайдшоу — свой список и своя позиция
        self.slideshow_items: list[int] = []
        self.slideshow_index = 0
        self.slideshow_visible = False
        # 4.0: у слайдшоу своя длительность показа, независимая от ленты.
        # Снимки в галерее ведущий ставит сам и держит их подольше, а
        # лента крутится быстро; одна настройка на оба не годилась.
        self.slideshow_interval = interval
        self._slideshow_deadline = time.monotonic() + self.slideshow_interval
        # 4.0: у галереи своя пауза, независимая от ленты. Запуск слайдшоу
        # всегда включает показ: кнопка «запустить» и означает «показать
        # сейчас», а не «восстановить то, что я остановил вчера».
        self.slideshow_playing = True
        # 5.0: квиз на экране. Режим тот же, что у заставки и QR: лента
        # на это время встаёт. Фаза живёт на сервере, поэтому каждый
        # подключившийся экран видит ту же стадию квиза, что и остальные:
        # idle | open | closed | reveal | board.
        self.quiz_visible = False
        self.quiz_phase = "idle"
        self.quiz_question: int | None = None
        # board бывает двух видов: промежуточный (листает всех) и финал
        # после последнего вопроса (топ-10, первый крупно, без движения).
        self.quiz_final = False
        # когда открыт вопрос, от этого момента экран считает отсчёт;
        # None — таймера нет или приём уже закрыт
        self.quiz_opened_at: float | None = None

    # У обычной ленты и у слайдшоу свои часы: переключение режима не должно
    # начинать отсчёт заново у того, что сейчас не показывается.
    def _due_at(self) -> float:
        return self._slideshow_deadline if self.slideshow_visible else self._deadline

    @property
    def running(self) -> bool:
        """Идёт ли показ в том режиме, который сейчас на экране.

        У ленты своя пауза, у галереи своя: кнопка паузы в ленте не должна
        останавливать снимки, и наоборот.
        """
        return self.slideshow_playing if self.slideshow_visible else self.playing

    def reset_deadline(self) -> None:
        """Отсчёт с нуля для того режима, который сейчас на экране."""
        if self.slideshow_visible:
            self._slideshow_deadline = time.monotonic() + self.slideshow_interval
        else:
            self._deadline = time.monotonic() + self.interval

    @property
    def current_id(self) -> int | None:
        if not self.items:
            return None
        return self.items[min(self.index, len(self.items) - 1)]

    @property
    def slideshow_current_id(self) -> int | None:
        if not self.slideshow_items:
            return None
        return self.slideshow_items[min(self.slideshow_index, len(self.slideshow_items) - 1)]

    @property
    def active_id(self) -> int | None:
        """Что сейчас на экране: слайдшоу, если оно запущено, иначе лента."""
        return self.slideshow_current_id if self.slideshow_visible else self.current_id

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

    def sync_slideshow(self, ids: list[int]) -> bool:
        """То же для галереи слайдшоу. Возвращает True, если список изменился."""
        if ids == self.slideshow_items:
            return False
        current = self.slideshow_current_id
        self.slideshow_items = ids
        if current in ids:
            self.slideshow_index = ids.index(current)
        else:
            self.slideshow_index = 0
            self._slideshow_deadline = time.monotonic() + self.slideshow_interval
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

    async def slideshow_go_to(self, message_id: int) -> None:
        """Показать снимок из галереи слайдшоу — отдельная кнопка у карточки."""
        async with self._lock:
            if message_id in self.slideshow_items:
                self.slideshow_index = self.slideshow_items.index(message_id)
                self._slideshow_deadline = time.monotonic() + self.slideshow_interval

    async def set_playing(self, playing: bool) -> None:
        async with self._lock:
            self.playing = playing
            self._deadline = time.monotonic() + self.interval
            self._slideshow_deadline = time.monotonic() + self.slideshow_interval

    async def set_interval(self, seconds: int) -> None:
        """Длительность снимка обычной ленты. У слайдшоу своя."""
        async with self._lock:
            self.interval = max(3, min(300, int(seconds)))
            self._deadline = time.monotonic() + self.interval

    async def set_slideshow_playing(self, playing: bool) -> None:
        """Пауза и продолжение показа галереи."""
        async with self._lock:
            self.slideshow_playing = bool(playing)
            self._slideshow_deadline = time.monotonic() + self.slideshow_interval

    async def set_slideshow_interval(self, seconds: int) -> None:
        """Длительность снимка в слайдшоу — настраивается отдельно от ленты."""
        async with self._lock:
            self.slideshow_interval = max(3, min(600, int(seconds)))
            self._slideshow_deadline = time.monotonic() + self.slideshow_interval

    def _step(self, delta: int) -> None:
        """Переключает слайд в том списке, который сейчас на экране."""
        if self.slideshow_visible:
            items, index = self.slideshow_items, self.slideshow_index
        else:
            items, index = self.items, self.index
        if not items:
            return
        new_index = index + delta
        # отмечаем, что круг пройден: список «уже показанных» пора сбросить.
        # В слайдшоу круг ленты не трогаем — сбрасывать там нечего
        if new_index >= len(items) or new_index < 0:
            if not self.slideshow_visible:
                self._wrapped = True
        new_index %= len(items)
        if self.slideshow_visible:
            self.slideshow_index = new_index
            self._slideshow_deadline = time.monotonic() + self.slideshow_interval
        else:
            self.index = new_index
            self._deadline = time.monotonic() + self.interval

    @property
    def _active_len(self) -> int:
        return len(self.slideshow_items if self.slideshow_visible else self.items)

    def due(self) -> bool:
        """Пора ли переключать слайд. Проверка без блокировки — только чтение."""
        if not self.running or self.qr_visible or self.splash_visible:
            return False
        # 5.0: квиз на экране — лента стоит, пока ведущий его не выключил
        if self.quiz_visible:
            return False
        if self._active_len <= 1:
            return False
        return time.monotonic() >= self._due_at()

    async def step_if_due(self) -> bool:
        """Переключает слайд, если время пришло. True — если переключили."""
        if not self.due():
            return False
        async with self._lock:
            # пока ждали блокировку, время могло ещё не наступить
            if not self.running or self.qr_visible or self.splash_visible:
                return False
            if self.quiz_visible:
                return False
            if self._active_len <= 1 or time.monotonic() < self._due_at():
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
            # 4.0: слайдшоу. active_id — что на самом деле видно на экране
            "slideshow_visible": self.slideshow_visible,
            "slideshow_playing": self.slideshow_playing,
            "slideshow_interval": self.slideshow_interval,
            "slideshow_current_id": self.slideshow_current_id,
            "slideshow_total": len(self.slideshow_items),
            "slideshow_index": (
                min(self.slideshow_index, len(self.slideshow_items) - 1)
                if self.slideshow_items else 0
            ),
            "active_id": self.active_id,
            # 5.0: квиз. Экран по этим полям понимает, что сейчас рисовать
            "quiz_visible": self.quiz_visible,
            "quiz_phase": self.quiz_phase,
            "quiz_question": self.quiz_question,
            "quiz_final": self.quiz_final,
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