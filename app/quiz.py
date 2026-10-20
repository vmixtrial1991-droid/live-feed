"""Квиз (5.0): машина состояний и сборки ответов для экрана и страниц.

Машина простая и живёт на сервере, поэтому все экраны и телефоны одного
события видят одну и ту же стадию:

    idle   — квиза нет, на экране лента;
    open   — вопрос открыт, зал отвечает (можно передумать до закрытия);
    closed — приём закрыт, показываем распределение, правильный ещё нет;
    reveal — показываем и распределение, и правильный ответ;
    board  — таблица лидеров (промежуточная или финальная).

Ведущий ведёт игру одной цепочкой: «Открыть → Закрыть приём → Показать
ответ → Дальше»; «Дальше» после последнего вопроса уводит экран сразу на
финальную таблицу.

Здесь только чтение и сборка: записи делает EventStore, рассылкой занимается
main.py. Правильный номер ответа зрителям не отдаётся до стадии reveal —
до неё подсмотреть ответ нельзя даже через DevTools.
"""

from __future__ import annotations

import time

from .db import EventStore
from .events import Event
from .realtime import ScreenController

PHASES = ("idle", "open", "closed", "reveal", "board")


def question_public(q: dict) -> dict:
    """Вопрос для зрителей: текст, варианты, зачётность — без правильного.

    seconds сюда не входит намеренно: отсчёт видит только проектор,
    телефоны не должны превращать игру в гонку.
    """
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


def _board_public(store: EventStore, limit: int = 100) -> list[dict]:
    """Лидеры для показа: имя, балл, сколько отвечал, место.

    Равный балл = равное место (1, 1, 3): два первых места не съедают
    третье, и зал не путается, почему у одинаковых очков разные места.
    Порядок внутри равного балла оставлен базой стабильным (раньше
    ответил — раньше в списке), чтобы таблица не прыгала от запроса к запросу.
    """
    rows = store.quiz_leaderboard(limit=limit)
    out: list[dict] = []
    rank, prev = 0, None
    for i, row in enumerate(rows, start=1):
        if row["score"] != prev:
            rank, prev = i, row["score"]
        out.append({
            "name": row["name"], "score": row["score"],
            "answered": row["answered"], "rank": rank,
        })
    return out


def _time_left(screen: ScreenController, q: dict) -> int | None:
    """Сколько секунд осталось по таймеру вопроса (экрану и панели).

    Отсчёт ведётся от момента открытия; экрану приезжает число на сейчас,
    дальше он тикает сам. Отрицательным не бывает: «время вышло» — это 0,
    а ответы принимаются, пока ведущий не закроет приём.
    """
    if not q.get("seconds") or screen.quiz_opened_at is None:
        return None
    return max(0, int(round(q["seconds"] - (time.time() - screen.quiz_opened_at))))


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
            # финал — после последнего вопроса: топ-10, первый крупно,
            # без автолистания; промежуточный борд экран сам листает пачками
            "final": bool(screen.quiz_final),
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
    if phase == "open":
        left = _time_left(screen, q)
        if left is not None:
            payload["seconds_left"] = left
    if phase in ("closed", "reveal"):
        payload["counts"] = store.quiz_counts(q["id"], len(q["options"]))
        # правильный показываем только на reveal и только на зачётных
        # вопросах; у «хитрых» его нет — смысл вопроса именно в разбросе
        # мнений
        payload["correct"] = (q["correct"] if q["scored"] else None) if phase == "reveal" else None
    return payload


def participant_view(store: EventStore, screen: ScreenController, participant_id: int) -> dict:
    """Ответ страницы квиза для гостя.

    Гость видит ту же стадию, что и экран, но отвечает даже когда проектор
    вернулся к ленте: ведущий может открыть вопрос заранее. Отсчёт гостю
    не приезжает — торопит зал только большая цифра на проекторе.
    """
    phase = screen.quiz_phase if screen.quiz_phase in PHASES else "idle"
    data: dict = {"phase": phase, "my_score": store.quiz_score(participant_id)}

    if phase in ("open", "closed", "reveal") and screen.quiz_question:
        q = store.quiz_get_question(screen.quiz_question)
        if q is not None:
            no, total = _position_of(store, q["id"])
            data["question"] = question_public(q)
            data["question_no"] = no
            data["questions_total"] = total
            data["my_answer"] = store.quiz_get_answer(q["id"], participant_id)
            data["answered_total"] = store.quiz_answered_total(q["id"])
            if phase in ("closed", "reveal"):
                data["counts"] = store.quiz_counts(q["id"], len(q["options"]))
                data["correct"] = (q["correct"] if q["scored"] else None) if phase == "reveal" else None
    elif phase == "board":
        data["board"] = _board_public(store)
        data["scored_total"] = store.quiz_scored_count()
    return data


def host_state(store: EventStore, screen: ScreenController) -> dict:
    """Сводка для панели ведущего: вопросы, счётчики, живые полосы, стадия."""
    questions = store.quiz_list_questions()
    for q in questions:
        q["answered"] = store.quiz_answered_total(q["id"])
        # живые полосы: только ведущий видит, куда тяготеет зал, и то
        # лишь по вопросам, где уже есть ответы; залу распределение
        # откроется не раньше закрытия приёма
        q["tally"] = store.quiz_counts(q["id"], len(q["options"])) if q["answered"] else None
    data: dict = {
        "phase": screen.quiz_phase,
        "visible": screen.quiz_visible,
        "current": screen.quiz_question,
        "final": bool(screen.quiz_final),
        "questions": questions,
        "board": _board_public(store),
        "participants": store.stats()["participants"],
        "answers_total": store.quiz_total_answers(),
    }
    if screen.quiz_phase == "open" and screen.quiz_question:
        q = store.quiz_get_question(screen.quiz_question)
        if q is not None:
            left = _time_left(screen, q)
            if left is not None:
                data["time_left"] = left
    if screen.quiz_phase in ("closed", "reveal") and screen.quiz_question:
        q = store.quiz_get_question(screen.quiz_question)
        if q is not None:
            data["counts"] = store.quiz_counts(q["id"], len(q["options"]))
    return data
