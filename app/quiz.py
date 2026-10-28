"""Квиз (5.0): машина состояний и сборки ответов для экрана и страниц.

Машина простая и живёт на сервере, поэтому все экраны и телефоны одного
события видят одну и ту же стадию:

    idle   — квиза нет, на экране лента;
    lobby  — квиз запущен, вопроса ещё нет: на экране название игры и QR,
             зал подключается, лента на это время встаёт;
    open   — вопрос открыт, зал отвечает (можно передумать до закрытия);
    closed — приём закрыт, показываем распределение, правильный ещё нет;
    reveal — показываем и распределение, и правильный ответ;
    board  — таблица лидеров (промежуточная или финальная).

Ведущий ведёт игру одной цепочкой: «Запустить квиз → Открыть → Закрыть
приём → Показать ответ → Дальше»; «Дальше» после последнего вопроса уводит
экран сразу на финальную таблицу. С промежуточной таблицы («Лидеры на
экран») можно вернуться туда, где игра стояла: стадию помним в
ScreenController.

Здесь только чтение и сборка: записи делает EventStore, рассылкой занимается
main.py. Правильный номер ответа зрителям не отдаётся до стадии reveal —
до неё подсмотреть ответ нельзя даже через DevTools.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from .db import EventStore
from .events import Event
from .realtime import ScreenController

PHASES = ("idle", "lobby", "open", "closed", "reveal", "board")

# стадии, к которым возвращается шаг «назад» с промежуточной таблицы
BACK_PHASES = ("lobby", "open", "closed", "reveal")

# Цепочка вперёд: «Открыть → Закрыть приём → Показать ответ → Дальше».
# По ней же считается шаг назад для кнопки «←»: ведущий промахнулся
# кнопкой и хочет вернуть предыдущую стадию того же вопроса.
STEP_BACK = {"open": "lobby", "closed": "open", "reveal": "closed"}


# --------------------------------------------------------------------------
# Лимиты длины текста (ТЗ «Вёрстка экрана квиза», раздел 12)
# --------------------------------------------------------------------------
# У вопроса и ответа не один предел, а два:
#
#   recommended — до него экран укладывается штатно, крупным кеглем;
#   hard        — за ним текст уже не помещается никак, редактор обязан
#                 не дать сохранить.
#
# Между пределами текст принимается: подгонка уменьшит кегль до минимумов
# (вопрос 32, ответ 28), а редактор предупредит автора, что шрифт будет
# мельче. Выше жёсткого предела — отказ, а если такой текст всё же оказался
# на экране, рендер обрезает его с «…» и пишет в лог.
#
# Пределы зависят от двух вещей, и обе — про геометрию, а не про вкус:
#
#   есть ли картинка — с ней вопросу достаётся меньше места (в схеме B
#                      только 16% области, в схеме C — узкая правая колонка);
#   сколько ответов    — четыре встают сеткой 2×2, и колонка вдвое уже.
#
# Таблица одна на весь проект: сервер проверяет по ней, редактор показывает
# по ней же счётчик и предупреждение. Разойдясь, они обещали бы разное.

QUIZ_QUESTION_LIMITS = {
    # False — картинки нет, True — есть.
    False: {"recommended": 300, "hard": 350},
    True: {"recommended": 200, "hard": 240},
}

# Ключ — (есть картинка, число ответов).
QUIZ_OPTION_LIMITS = {
    (False, 2): {"recommended": 100, "hard": 120},
    (False, 3): {"recommended": 100, "hard": 120},
    (False, 4): {"recommended": 80, "hard": 100},
    (True, 2): {"recommended": 70, "hard": 85},
    (True, 3): {"recommended": 70, "hard": 85},
    (True, 4): {"recommended": 60, "hard": 75},
}


def question_limits(has_image: bool) -> dict:
    """Пределы вопроса: {recommended, hard}."""
    return QUIZ_QUESTION_LIMITS[bool(has_image)]


def option_limits(has_image: bool, count: int) -> dict:
    """Пределы ответа для вопроса с таким числом вариантов.

    Считаем по фактическому числу заполненных вариантов: пустые строки
    сервер отбрасывает, и вопрос «на четыре» с тремя заполненными на экране
    будет колонкой, а не сеткой 2×2 — значит, и предел у него мягче.
    """
    n = min(4, max(2, int(count or 2)))
    return QUIZ_OPTION_LIMITS[(bool(has_image), n)]


def text_limits(has_image: bool, option_count: int = 2) -> dict:
    """Все пределы кадра одной структурой — её отдают и API, и редактор.

    {
      "question": {"recommended": .., "hard": ..},
      "answer":   {"recommended": .., "hard": ..},
      "has_image": bool,
      "answers": n,
    }
    """
    return {
        "question": question_limits(has_image),
        "answer": option_limits(has_image, option_count),
        "has_image": bool(has_image),
        "answers": min(4, max(2, int(option_count or 2))),
    }


def text_limits_table() -> dict:
    """Вся таблица пределов — для редактора.

    Редактору нужны все комбинации сразу: автор ставит картинку, меняет
    число вариантов и должен видеть предел без похода на сервер. Поэтому
    отдаём таблицу целиком, а не один расчёт: она маленькая, а лишний
    запрос на каждое нажатие «+ вариант» был бы лишним.

    Формат: {"question": {"none": {...}, "image": {...}},
             "answer": {"none": {"2": {...}, "3": {...}, "4": {...}},
                        "image": {...}}}
    """
    return {
        "question": {
            "none": dict(QUIZ_QUESTION_LIMITS[False]),
            "image": dict(QUIZ_QUESTION_LIMITS[True]),
        },
        "answer": {
            "none": {str(n): dict(QUIZ_OPTION_LIMITS[(False, n)])
                     for n in (2, 3, 4)},
            "image": {str(n): dict(QUIZ_OPTION_LIMITS[(True, n)])
                      for n in (2, 3, 4)},
        },
    }


def prev_phase(screen: ScreenController, store: EventStore) -> str | None:
    """Куда ведёт кнопка «←» — на один шаг назад, или None.

    Отдельно от back_phase(): тот про возврат с таблицы лидеров, а этот —
    про обычную цепочку стадий. Ведущий нажимает «Закрыть приём» случайно
    и хочет вернуть приём; нажал «Дальше» не на тот вопрос — хочет
    вернуться к прошлому.

    Что можно:
      open   → lobby (вопрос ещё не открывали) — если это первый вопрос,
               иначе к прошлому вопросу в стадии reveal;
      closed → open  (снова принимаем ответы, отсчёт идёт с начала);
      reveal → closed (прячем правильный ответ);
      board  → стадия, с которой ушли на таблицу (как back_phase);
    Нельзя: из lobby (квиз только запущен) и из финальной таблицы.
    """
    phase = screen.quiz_phase
    if phase == "board":
        return back_phase(screen)
    if phase == "lobby" or phase == "idle":
        return None
    if phase == "open":
        order = [q["id"] for q in store.quiz_list_questions()]
        if screen.quiz_question in order and order.index(screen.quiz_question) > 0:
            return "prev_question"   # особый случай: предыдущий вопрос
        return "lobby"
    return STEP_BACK.get(phase)


def back_phase(screen: ScreenController) -> str | None:
    """Куда ведёт возврат с таблицы лидеров, или None — если возврата нет.

    Промежуточная таблица — это пауза в игре: зал попросили посмотреть
    лидеров, а потом хотят снова открыть приём или показать ответ. Поэтому
    стадию, из которой ведущий ушёл на таблицу, помнят (и в конфиге тоже).
    Финальная таблица возврата не имеет: там вопроса уже нет.
    """
    if screen.quiz_phase != "board" or screen.quiz_final:
        return None
    prev = screen.quiz_prev_phase
    if prev not in BACK_PHASES:
        return None
    if prev != "lobby" and not screen.quiz_question:
        # вопрос мог удалиться, пока зал смотрел лидеров: не к чему возвращать
        return None
    return prev


def question_public(q: dict, with_image: bool = False) -> dict:
    """Вопрос для зрителей: текст, варианты, зачётность — без правильного.

    seconds сюда не входит намеренно: отсчёт видит только проектор,
    телефоны не должны превращать игру в гонку.

    Картинка — только для проектора (with_image=True). На телефоне гость
    видит вопрос и варианты без неё: экран в руке маленький, фото рядом с
    текстом отнимает место у кнопок, а рассматривать его зал всё равно
    будет на большом экране. Ключ экрана к ссылке добавляет screen.js
    (withKey): /media без пропуска не отдаётся.
    """
    data = {
        "id": q["id"],
        "text": q["text"],
        "options": q["options"],
        "scored": bool(q["scored"]),
    }
    image = q.get("image")
    if with_image and image:
        data["image"] = "/media/" + quote(str(image))
    return data


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


def quiz_title(event: Event) -> str:
    """Название игры для холла: своё, если ведущий его задал, иначе событие.

    У события название общее — «Свадьба Маши и Саши» или «Новый год в
    офисе», — а игра внутри вечера может называться иначе. Ведущий вводит
    его на вкладке «Квиз»; пустое поле значит «как называется событие»,
    чтобы не заставлять писать одно и то же дважды.
    """
    return str(event.get("quiz_title", "")).strip() or str(event.get("title", "")).strip()


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
    if phase == "lobby":
        # игра запущена, но вопроса ещё нет: зал подключается по QR, а ведущий
        # в это время может спокойно дописывать вопросы
        return {
            "phase": "lobby",
            # название игры: своё, если ведущий его задал, иначе событие
            "title": quiz_title(event) or event.slug,
            "subtitle": event.get("subtitle", ""),
            "participants": store.stats()["participants"],
            # путь до QR: экран подставит свой ключ и покажет картинку
            "qr": "/api/qr.svg?kind=quiz",
        }
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
        # картинку получает только проектор: на телефоне она не нужна
        "question": question_public(q, with_image=True),
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
    # Считаем очки только по оглашённым вопросам: до «Показать ответ»
    # прибавка в телефоне выдавала верный ответ раньше проектора.
    data: dict = {"phase": phase,
                  "my_score": store.quiz_score(participant_id, store.quiz_revealed_ids())}

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


def host_state(event: Event, store: EventStore, screen: ScreenController) -> dict:
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
        # «вернуться к вопросу»: панель подписывает шаговую кнопку по этой
        # стадии; у финальной таблицы поля нет — возвращаться некуда
        "back_phase": back_phase(screen),
        # куда ведёт кнопка «←» (на шаг назад по цепочке), или None
        "prev_phase": prev_phase(screen, store),
        "questions": questions,
        "board": _board_public(store),
        "participants": store.stats()["participants"],
        "answers_total": store.quiz_total_answers(),
        # своё название игры — как записано, без подстановки: пустое поле в
        # панели значит «как называется событие», и подставлять туда название
        # события значением нельзя, иначе ведущий не отличит своё от чужого
        "quiz_title": str(event.get("quiz_title", "")).strip(),
        "event_title": event.get("title", ""),
        # Пределы длины текста одной таблицей — по ним редактор рисует
        # счётчик и предупреждение. Считаются на месте (раздел 12 ТЗ):
        # дублировать числа во фронтенде значит обещать автору не то,
        # что примет сервер.
        "text_limits": text_limits_table(),
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
