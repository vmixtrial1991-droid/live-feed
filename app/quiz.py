"""Квиз (5.0): машина состояний и сборки ответов для экрана и страниц.

Машина простая и живёт на сервере, поэтому все экраны и телефоны одного
события видят одну и ту же стадию:

    idle   — квиза нет, на экране лента;
    open   — вопрос открыт, зал отвечает (можно передумать до закрытия);
    closed — ответ закрыт, показываем распределение и правильный ответ;
    board  — таблица лидеров.

Здесь только чтение и сборка: записи делает EventStore, рассылкой занимается
main.py. Правильный номер ответа зрителям не отдаётся никогда — до закрытия
вопроса подсмотреть ответ нельзя даже через DevTools.
"""

from __future__ import annotations

from .db import EventStore
from .events import Event
from .realtime import ScreenController

PHASES = ("idle", "open", "closed", "board")


def question_public(q: dict) -> dict:
    """Вопрос для зрителей: текст, варианты, зачётность — без правильного."""
    return {
        "id": q["id"],
        "text": q["text"],
        "options": q["options"],
        "scored": bool(q["scored"]),
    }


def _position_of(store: EventStore, question_id: int) -> tuple[int, int]:
    """Номер вопроса (1) и всего их количество — для подписи «3 из 10»."""
    ids = [q["id"] for q in store.quiz_list_questions()]
    total = len(ids)
    if question_id in ids:
        return ids.index(question_id) + 1, total
    return 0, total


def _board_public(store: EventStore) -> list[dict]:
    """Лидеры для показа: имя, балл, сколько отвечал. Служебные поля — в базе."""
    return [
        {"name": row["name"], "score": row["score"], "answered": row["answered"]}
        for row in store.quiz_leaderboard(limit=30)
    ]


def display_payload(event: Event, screen: ScreenController, store: EventStore) -> dict | None:
    """Что рисовать на экране вместо ленты. None — лента вернулась.

    Экран может быть выключен из квиза (кнопка «лента»), даже когда фаза
    всё ещё открыта: ведущий оставляет зал отвечать, а на проекторе крутятся
    фотографии. Поэтому решает флаг quiz_visible, а не фаза.
    """
    if not screen.quiz_visible or screen.quiz_phase == "idle":
        return None

    phase = screen.quiz_phase
    if phase == "board":
        return {
            "phase": "board",
            "board": _board_public(store),
            "scored_total": store.quiz_scored_count(),
        }

    q = store.quiz_get_question(screen.quiz_question) if screen.quiz_question else None
    if q is None:
        # вопрос удалили, пока был открыт: держим экран на ленте,
        # ведущему об этом скажет его панель
        return None

    no, total = _position_of(store, q["id"])
    payload: dict = {
        "phase": phase,
        "question": question_public(q),
        "question_no": no,
        "questions_total": total,
        "answered_total": store.quiz_answered_total(q["id"]),
    }
    if phase == "closed":
        payload["counts"] = store.quiz_counts(q["id"], len(q["options"]))
        # правильный показываем только на зачётных вопросах; у «хитрых»
        # его нет — смысл вопроса именно в разбросе мнений
        payload["correct"] = q["correct"] if q["scored"] else None
    return payload


def participant_view(store: EventStore, screen: ScreenController, participant_id: int) -> dict:
    """Ответ страницы квиза для гостя.

    Гость видит ту же стадию, что и экран, но отвечает даже когда проектор
    вернулся к ленте: ведущий может открыть вопрос заранее.
    """
    phase = screen.quiz_phase if screen.quiz_phase in PHASES else "idle"
    data: dict = {"phase": phase, "my_score": store.quiz_score(participant_id)}

    if phase in ("open", "closed") and screen.quiz_question:
        q = store.quiz_get_question(screen.quiz_question)
        if q is not None:
            no, total = _position_of(store, q["id"])
            data["question"] = question_public(q)
            data["question_no"] = no
            data["questions_total"] = total
            data["my_answer"] = store.quiz_get_answer(q["id"], participant_id)
            data["answered_total"] = store.quiz_answered_total(q["id"])
            if phase == "closed":
                data["counts"] = store.quiz_counts(q["id"], len(q["options"]))
                data["correct"] = q["correct"] if q["scored"] else None
    elif phase == "board":
        data["board"] = _board_public(store)
        data["scored_total"] = store.quiz_scored_count()
    return data


def host_state(store: EventStore, screen: ScreenController) -> dict:
    """Сводка для панели ведущего: вопросы, счётчики ответивших, стадия."""
    questions = store.quiz_list_questions()
    for q in questions:
        q["answered"] = store.quiz_answered_total(q["id"])
    data: dict = {
        "phase": screen.quiz_phase,
        "visible": screen.quiz_visible,
        "current": screen.quiz_question,
        "questions": questions,
        "board": _board_public(store),
        "participants": store.stats()["participants"],
        "answers_total": store.quiz_total_answers(),
    }
    if screen.quiz_phase == "closed" and screen.quiz_question:
        q = store.quiz_get_question(screen.quiz_question)
        if q is not None:
            data["counts"] = store.quiz_counts(q["id"], len(q["options"]))
    return data
