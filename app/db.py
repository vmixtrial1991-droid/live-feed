"""Хранилище одного события: SQLite со схемой и функциями работы.

Каждое событие живёт в своей базе (app/events/<slug>/app.db). Общей базы
нет намеренно: события не должны видеть сообщения друг друга, а удаление
события должно уносить с собой ровно его файлы.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER REFERENCES participants(id) ON DELETE SET NULL,
    author         TEXT NOT NULL DEFAULT 'Аноним',
    body           TEXT NOT NULL DEFAULT '',
    image          TEXT,
    -- модерации больше нет: всё уходит в эфир сразу,
    -- status остался для совместимости со старыми базами
    status         TEXT NOT NULL DEFAULT 'approved',
    reject_reason  TEXT,
    position       REAL NOT NULL DEFAULT 0,   -- порядок показа
    shown_at       REAL,                      -- когда последний раз показывали
    created_at     REAL NOT NULL,
    moderated_at   REAL,
    -- 4.0: галочка «в слайдшоу». Значение осмысленно только при image:
    -- в слайдшоу идут фотографии и фотографии с текстом
    in_slideshow   INTEGER NOT NULL DEFAULT 0
);

-- Галерея слайдшоу — отдельный набор фотографий, а не флаг у строки ленты.
-- Так две вещи перестали быть связаны: удаление сообщения из ленты больше
-- не выбрасывает снимок из слайдшоу, а снятие галочки не трогает ленту.
-- Раньше слайдшоу жил флагом in_slideshow у сообщения, и удаление из ленты
-- уносило строку вместе с флагом — снимок исчезал из обоих мест сразу.
-- Файл здесь только имя: само изображение лежит в uploads/ и живёт столько,
-- сколько ведущий не нажмёт «удалить все фото».
CREATE TABLE IF NOT EXISTS gallery (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    image      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    author     TEXT NOT NULL DEFAULT '',
    -- строка ленты, из которой снимок попал в галерею. Нужна, чтобы правка
    -- текста в ленте доезжала и в слайдшоу: тот же снимок не должен
    -- показываться на экране с двумя разными надписями. NULL — снимок
    -- остался в галерее, а его сообщение из ленты удалили; тогда текст
    -- остаётся тем, что был при отметке.
    source_id  INTEGER,
    position   REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gallery_image ON gallery(image);
CREATE INDEX IF NOT EXISTS idx_gallery_position ON gallery(position);

-- 5.0: квиз. Вопросы живут вместе с событием, ответы — привязаны к
-- вопросу и к участнику (тот же участник, что пишет в ленту: один гость —
-- одна запись, поэтому квиз узнаёт того, кто уже что-то отправлял).
-- options — JSON-список вариантов; correct — индекс правильного, -1 у
-- «хитрых» вопросов (scored=0): по ним показываем распределение, очков нет.
-- seconds — время на ответ в секундах, NULL значит «без таймера»: ведущий
-- заводит отсчёт сам, экран торопит зал, но по истечении ответа всё равно
-- принимаются, пока ведущий не закроет приём.
-- ON DELETE CASCADE: удалил вопрос — ответы уходят следом, хвостов нет.
CREATE TABLE IF NOT EXISTS quiz_questions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    options    TEXT NOT NULL,
    correct    INTEGER NOT NULL DEFAULT -1,
    scored     INTEGER NOT NULL DEFAULT 1,
    position   REAL NOT NULL DEFAULT 0,
    seconds    INTEGER,
    image      TEXT,
    revealed_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS quiz_answers (
    question_id    INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    option_index   INTEGER NOT NULL,
    answered_at    REAL NOT NULL,
    PRIMARY KEY (question_id, participant_id)
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# «параметр не приходил» — не то же самое, что «пришёл как None»:
# для seconds None законное значение («без таймера»).
_UNSET = object()


def _migrate(conn: sqlite3.Connection) -> None:
    """Догоняем базу, созданную прошлыми версиями.

    CREATE TABLE IF NOT EXISTS не добавляет новые колонки в уже
    существующую таблицу, поэтому недостающие досоздаём отдельно.
    """
    have = _columns(conn, "messages")
    if "shown_at" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN shown_at REAL")

    # 4.0: галочка слайдшоу. DEFAULT нужен, иначе ALTER на таблице с
    # записями упадёт по NOT NULL, а у старых событий флаги должны быть 0.
    have = _columns(conn, "messages")
    if "in_slideshow" in have:
        # Старая схема держала слайдшоу флагом у сообщения: переносим
        # отмеченное в отдельную таблицу галереи, один раз. Порядок берём
        # тот же, что был в ленте, — иначе галерея после обновления шла бы
        # вразнобой с тем, что ведущий уже наставил. INSERT OR IGNORE
        # страхует от повторного запуска миграции.
        rows = conn.execute(
            "SELECT image, body, author, position, created_at FROM messages "
            "WHERE in_slideshow = 1 AND image IS NOT NULL ORDER BY position, id"
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO gallery (image, body, author, position, created_at) "
                "VALUES (?,?,?,?,?)",
                (r["image"], r["body"] or "", r["author"] or "",
                 r["position"], r["created_at"]),
            )
        conn.execute("DROP INDEX IF EXISTS idx_messages_slideshow")

    # 4.0.1: галерея помнит, из какой строки ленты взялся снимок
    gal_cols = _columns(conn, "gallery")
    if "source_id" not in gal_cols:
        conn.execute("ALTER TABLE gallery ADD COLUMN source_id INTEGER")

    # 5.0: время ответа у вопроса. NULL — без таймера; у заведённых раньше
    # событий колоны просто не было.
    if "seconds" not in _columns(conn, "quiz_questions"):
        conn.execute("ALTER TABLE quiz_questions ADD COLUMN seconds INTEGER")

    # 5.0: картинка к вопросу. NULL — вопрос текстовый, как раньше.
    if "image" not in _columns(conn, "quiz_questions"):
        conn.execute("ALTER TABLE quiz_questions ADD COLUMN image TEXT")

    # 5.1: момент, когда правильный ответ огласили. До него очки гостю не
    # начисляем: иначе прибавка в телефоне выдавала верный ответ раньше
    # проектора. NULL — ответ ещё не показан.
    if "revealed_at" not in _columns(conn, "quiz_questions"):
        conn.execute("ALTER TABLE quiz_questions ADD COLUMN revealed_at REAL")

    # модерация убрана, но в старых базах остались её следы
    if "reject_reason" in have:
        conn.execute("UPDATE messages SET status = 'approved' WHERE status = 'pending'")
        conn.execute("UPDATE messages SET status = 'approved' WHERE status = 'rejected'")


def message_to_dict(row: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(row)
    return {
        "id": d["id"],
        "author": d["author"],
        "body": d["body"] or "",
        "image": d["image"],
        "status": d["status"],
        "position": d["position"],
        "shown_at": d.get("shown_at"),
        "created_at": d["created_at"],
    }


def _question_to_dict(row: sqlite3.Row | dict) -> dict[str, Any]:
    """Строка вопроса: options из JSON обратно в список."""
    d = dict(row)
    try:
        options = json.loads(d["options"])
    except (TypeError, ValueError):
        options = []
    if not isinstance(options, list):
        options = []
    return {
        "id": d["id"],
        "text": d["text"],
        "options": [str(o) for o in options],
        "correct": int(d["correct"]),
        "scored": bool(d["scored"]),
        "position": d["position"],
        "seconds": int(d["seconds"]) if d.get("seconds") else None,
        # имя файла картинки вопроса или None. Сам файл лежит в uploads
        # события, рядом с фотографиями ленты
        "image": d.get("image"),
        "created_at": d["created_at"],
    }


class EventStore:
    """База одного события. Путь фиксируется при создании объекта."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        """Создаёт или обновляет базу события."""
        # WAL переключаем до миграции: смена режима журнала невозможна
        # внутри уже начатой транзакции, а executescript её открывает
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)

    # --- сообщения ------------------------------------------------------
    def live_messages(self, limit: int = 500, ascending: bool = False) -> list[dict]:
        """Всё, что попадает на экран, в порядке показа или обратном."""
        order = "ASC" if ascending else "DESC"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE status != 'rejected' "
                f"ORDER BY position {order}, id {order} LIMIT ?",
                (limit,),
            ).fetchall()
        return [message_to_dict(r) for r in rows]

    def live_ids(self) -> list[int]:
        """ID в порядке показа — для полноэкранного режима."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM messages WHERE status != 'rejected' "
                "ORDER BY position ASC, id ASC"
            ).fetchall()
        return [r["id"] for r in rows]

    def mark_shown(self, message_id: int) -> None:
        """Пометить, что сообщение только что было на экране."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET shown_at = ? WHERE id = ?", (time.time(), message_id)
            )

    def get_message(self, message_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return message_to_dict(row) if row else None

    def create_message(
        self, participant_id: int, author: str, body: str, image: str | None
    ) -> dict:
        """Новое сообщение сразу встаёт в конец очереди показа."""
        now = time.time()
        with self.connect() as conn:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE status != 'rejected'"
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO messages (participant_id, author, body, image, status, position, created_at) "
                "VALUES (?,?,?,?, 'approved', ?, ?)",
                (participant_id, author, body, image, nxt, now),
            )
            mid = cur.lastrowid
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        return message_to_dict(row)

    def edit_message(
        self, message_id: int, body: str | None = None, author: str | None = None
    ) -> dict | None:
        sets, params = [], []
        if body is not None:
            sets.append("body = ?")
            params.append(body)
        if author is not None:
            sets.append("author = ?")
            params.append(author)
        if not sets:
            return self.get_message(message_id)
        params.append(message_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_message(message_id)

    def delete_message(self, message_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        # Галерею намеренно не трогаем: снимки живут своей жизнью. Раньше
        # слайдшоу был флагом у строки, и удаление из ленты уносило снимок
        # из обоих мест сразу — ведущий терял то, что сам же отметил
        return True

    def move_message(self, message_id: int, direction: str) -> bool:
        """direction: 'up' | 'down' | 'first' | 'last' — меняет порядок показа.

        Позиции пересобираются заново (1, 2, 3…), поэтому порядок всегда однозначен.
        """
        with self.connect() as conn:
            ids = self._ordered_ids(conn)
            if message_id not in ids:
                return False

            i = ids.index(message_id)
            if direction == "up":
                target = max(0, i - 1)
            elif direction == "down":
                target = min(len(ids) - 1, i + 1)
            elif direction == "first":
                target = 0
            elif direction == "last":
                target = len(ids) - 1
            else:
                return False

            if i == target:
                return False

            ids.insert(target, ids.pop(i))  # вынимаем и вставляем на новое место
            _write_positions(conn, ids)
        return True

    def move_to(self, message_id: int, target_index: int) -> bool:
        """Поставить сообщение на указанное место в очереди показа (0 — первым)."""
        with self.connect() as conn:
            ids = self._ordered_ids(conn)
            if message_id not in ids:
                return False
            target_index = max(0, min(target_index, len(ids) - 1))
            i = ids.index(message_id)
            if i == target_index:
                return False
            ids.insert(target_index, ids.pop(i))
            _write_positions(conn, ids)
        return True

    @staticmethod
    def _ordered_ids(conn: sqlite3.Connection) -> list[int]:
        return [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM messages WHERE status != 'rejected' "
                "ORDER BY position ASC, id ASC"
            )
        ]

    def reset_shown(self) -> None:
        """Снять отметки «уже показывалось» — например, перед новым кругом."""
        with self.connect() as conn:
            conn.execute("UPDATE messages SET shown_at = NULL WHERE shown_at IS NOT NULL")

    # --- галерея слайдшоу (4.0) -------------------------------------
    # Галерея хранит фотографии сама по себе и ни с чем в ленте не связана.
    # Раньше слайдшоу был флагом in_slideshow у сообщения, и удаление
    # сообщения из ленты уносило снимок из слайдшоу вместе с флагом —
    # ведущий терял то, что сам же и отметил.

    def gallery_items(self) -> list[dict]:
        """Все снимки галереи в порядке показа.

        source_id отдаётся только если строка ленты на месте. Сообщение могли
        удалить, а номер остался: ведущий увидел бы «текст поправится и в
        ленте» про текст, который в ленте уже не показывается.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT g.id, g.image, g.body, g.author, g.position, g.created_at, "
                "       CASE WHEN p.image IS NOT NULL THEN g.source_id END AS source_id "
                "FROM gallery g LEFT JOIN messages p ON p.id = g.source_id "
                "ORDER BY g.position ASC, g.id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def gallery_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM gallery ORDER BY position ASC, id ASC"
            ).fetchall()
        return [r["id"] for r in rows]

    def gallery_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM gallery").fetchone()[0]

    def gallery_has_image(self, image: str | None) -> bool:
        """Есть ли этот снимок в галерее — галочка в ленте показывает это."""
        if not image:
            return False
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM gallery WHERE image = ?", (image,)
            ).fetchone() is not None

    def gallery_add(
        self, image: str, body: str, author: str, source_id: int | None = None
    ) -> dict | None:
        """Добавить фотографию в галерею. None — если это не фотография.

        Один и тот же снимок дважды не добавляется: галочка должна быть
        переключателем, а не кнопкой «добавить ещё раз».
        """
        if not image:
            return None
        now = time.time()
        with self.connect() as conn:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM gallery"
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO gallery "
                "(image, body, author, source_id, position, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (image, body or "", author or "", source_id, nxt, now),
            )
            row = conn.execute(
                "SELECT * FROM gallery WHERE image = ?", (image,)
            ).fetchone()
        return dict(row) if row else None

    def gallery_remove(self, gallery_id: int) -> bool:
        """Убрать снимок из галереи. Файл в uploads/ при этом остаётся."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM gallery WHERE id = ?", (gallery_id,))
            return bool(cur.rowcount)

    def gallery_remove_image(self, image: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM gallery WHERE image = ?", (image,))
            return bool(cur.rowcount)

    def gallery_source(self, gallery_id: int) -> int | None:
        """Из какой строки ленты взят снимок, если она ещё есть."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT g.source_id, p.image FROM gallery g "
                "LEFT JOIN messages p ON p.id = g.source_id WHERE g.id = ?",
                (gallery_id,),
            ).fetchone()
        if row is None or row["source_id"] is None:
            return None
        return row["source_id"] if row["image"] else None

    def gallery_follow(self, image: str, body: str, author: str) -> int:
        """Обновить текст всех снимков галереи с этим файлом.

        Вызывается после правки сообщения в ленте. Снимок один и тот же, и
        показывать его с двумя разными надписями нельзя: ведущий правит
        опечатку в ленте, а на экране остаётся старая.
        """
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE gallery SET body = ?, author = ? WHERE image = ?",
                (body or "", author or "", image),
            )
            return cur.rowcount or 0

    def gallery_edit(self, gallery_id: int, body: str) -> dict | None:
        """Правка текста у снимка в галерее. Имя автора не трогаем."""
        with self.connect() as conn:
            conn.execute("UPDATE gallery SET body = ? WHERE id = ?", (body, gallery_id))
            row = conn.execute("SELECT * FROM gallery WHERE id = ?", (gallery_id,)).fetchone()
        return dict(row) if row else None

    def gallery_clear(self) -> int:
        """Опустошить галерею. Файлы фотографий остаются в архиве."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM gallery")
            return cur.rowcount or 0

    def delete_photo_messages(self) -> int:
        """Убрать из ленты все сообщения с фотографией. Возвращает число.

        Галерея при этом остаётся: снимки живут в архиве и в галерее сами по
        себе, а из ленты они просто уходят. Иначе на экране остались бы
        битые картинки.
        """
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE image IS NOT NULL")
            return cur.rowcount or 0

    def photo_message_images(self) -> list[str]:
        """Имена файлов всех сообщений с фотографией."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT image FROM messages WHERE image IS NOT NULL"
            ).fetchall()
        return [r["image"] for r in rows]

    # --- квиз (5.0) ------------------------------------------------------
    # Один гость — одна запись участника: тот же, что пишет в ленту. Поэтому
    # квиз «узнаёт» человека, который уже что-то отправлял: повторного входа
    # не просит, а правка имени обновляет ту же запись.

    def quiz_add_question(
        self, text: str, options: list[str], correct: int, scored: bool,
        seconds: int | None = None, image: str | None = None,
    ) -> dict:
        """Новый вопрос встаёт в конец очереди вопросов."""
        now = time.time()
        with self.connect() as conn:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM quiz_questions"
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO quiz_questions "
                "(text, options, correct, scored, position, seconds, image, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (text, json.dumps(options, ensure_ascii=False),
                 correct if scored else -1, 1 if scored else 0, nxt, seconds,
                 image, now),
            )
            row = conn.execute(
                "SELECT * FROM quiz_questions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _question_to_dict(row)

    def quiz_update_question(
        self,
        question_id: int,
        text: str | None = None,
        options: list[str] | None = None,
        correct: int | None = None,
        scored: bool | None = None,
        seconds: Any = _UNSET,
        image: Any = _UNSET,
    ) -> dict | None:
        """Правка вопроса. Ответы не трогаем: кто отвечал на прежнюю
        редакцию — того ведущий оценивает сам (сбросом или принятием).

        seconds отдельный случай: NULL — законное «без таймера», поэтому
        «не менять» отличается от «стереть» только пришёл параметр или нет
        (_UNSET). Для text/options такой разницы нет: пустого текста или
        пустого списка вариантов не бывает."""
        sets, params = [], []
        if text is not None:
            sets.append("text = ?")
            params.append(text)
        if options is not None:
            sets.append("options = ?")
            params.append(json.dumps(options, ensure_ascii=False))
        if correct is not None:
            sets.append("correct = ?")
            params.append(correct)
        if scored is not None:
            sets.append("scored = ?")
            params.append(1 if scored else 0)
        if seconds is not _UNSET:
            sets.append("seconds = ?")
            params.append(seconds)
        # image — как seconds: None значит «убрать картинку», а «не трогать»
        # отличается только тем, что параметр не пришёл (_UNSET)
        if image is not _UNSET:
            sets.append("image = ?")
            params.append(image)
        if not sets:
            return self.quiz_get_question(question_id)
        params.append(question_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE quiz_questions SET {', '.join(sets)} WHERE id = ?", params
            )
        return self.quiz_get_question(question_id)

    def quiz_get_question(self, question_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_questions WHERE id = ?", (question_id,)
            ).fetchone()
        return _question_to_dict(row) if row else None

    def quiz_list_questions(self) -> list[dict]:
        """Все вопросы в порядке проведения."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_questions ORDER BY position ASC, id ASC"
            ).fetchall()
        return [_question_to_dict(r) for r in rows]

    def quiz_delete_question(self, question_id: int) -> bool:
        """Удалить вопрос; ответы уходят каскадом вместе с ним."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM quiz_questions WHERE id = ?", (question_id,))
            return bool(cur.rowcount)

    def quiz_move_question(self, question_id: int, direction: str) -> bool:
        """Порядок вопросов: up | down | first | last — как в очереди ленты."""
        with self.connect() as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM quiz_questions ORDER BY position ASC, id ASC"
                )
            ]
            if question_id not in ids:
                return False
            i = ids.index(question_id)
            target = {
                "up": max(0, i - 1),
                "down": min(len(ids) - 1, i + 1),
                "first": 0,
                "last": len(ids) - 1,
            }.get(direction)
            if target is None or target == i:
                return False
            ids.insert(target, ids.pop(i))
            conn.executemany(
                "UPDATE quiz_questions SET position = ? WHERE id = ?",
                [(float(pos), qid) for pos, qid in enumerate(ids, start=1)],
            )
        return True

    def quiz_duplicate_question(self, question_id: int) -> dict | None:
        """Копия вопроса сразу за оригиналом: копия получает position на
        пол-шага больше соседнего — ряды и так упорядочены по REAL, а
        пересобирать всю очередь из-за одной вставки не нужно."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_questions WHERE id = ?", (question_id,)
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "INSERT INTO quiz_questions "
                "(text, options, correct, scored, position, seconds, image, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (row["text"], row["options"], row["correct"], row["scored"],
                 row["position"] + 0.5, row["seconds"], row["image"], time.time()),
            )
            new = conn.execute(
                "SELECT * FROM quiz_questions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _question_to_dict(new)

    def quiz_answer(self, question_id: int, participant_id: int, option_index: int) -> None:
        """Записать или заменить ответ: до закрытия вопроса можно передумать."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO quiz_answers (question_id, participant_id, option_index, answered_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(question_id, participant_id) DO UPDATE SET "
                "option_index = excluded.option_index, answered_at = excluded.answered_at",
                (question_id, participant_id, option_index, time.time()),
            )

    def quiz_get_answer(self, question_id: int, participant_id: int) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT option_index FROM quiz_answers WHERE question_id = ? AND participant_id = ?",
                (question_id, participant_id),
            ).fetchone()
        return row["option_index"] if row else None

    def quiz_counts(self, question_id: int, n_options: int) -> list[int]:
        """Сколько голосов на каждом варианте — для распределения."""
        counts = [0] * max(0, int(n_options))
        if not counts:
            return counts
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT option_index, COUNT(*) AS n FROM quiz_answers "
                "WHERE question_id = ? GROUP BY option_index",
                (question_id,),
            ).fetchall()
        for r in rows:
            if 0 <= r["option_index"] < len(counts):
                counts[r["option_index"]] = r["n"]
        return counts

    def quiz_answered_total(self, question_id: int) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM quiz_answers WHERE question_id = ?",
                (question_id,),
            ).fetchone()[0]

    def quiz_leaderboard(self, limit: int = 20) -> list[dict]:
        """Таблица лидеров: балл за верный ответ на зачётный вопрос.

        Хитрые вопросы (scored=0) очков не дают. Порядок: больше балл —
        раньше ответил первым из своих — потом по имени, чтобы таблица
        не менялась от перезапроса к перезапросу.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT p.name AS name, "
                "       SUM(CASE WHEN qa.option_index = q.correct THEN 1 ELSE 0 END) AS score, "
                "       COUNT(qa.question_id) AS answered, "
                "       MIN(qa.answered_at) AS first_at "
                "FROM quiz_answers qa "
                "JOIN quiz_questions q ON q.id = qa.question_id AND q.scored = 1 AND q.correct >= 0 "
                "JOIN participants p ON p.id = qa.participant_id "
                "GROUP BY qa.participant_id, p.name "
                "ORDER BY score DESC, first_at ASC, p.name ASC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def quiz_score(self, participant_id: int, revealed_ids: set[int] | None = None) -> int:
        """Балл одного участника (для надписи «у вас N очков»).

        revealed_ids — вопросы, чей правильный ответ уже оглашён. Если
        список передан, считаем только их: иначе гость видел прибавку
        очков сразу после своего ответа, ещё до «Показать ответ» на
        проекторе, и правильный ответ угадывался по счётчику.

        None значит «считать все» — так пользуется таблица лидеров, где
        очки нужны целиком.
        """
        with self.connect() as conn:
            sql = (
                "SELECT COALESCE(SUM(CASE WHEN qa.option_index = q.correct THEN 1 ELSE 0 END), 0) "
                "FROM quiz_answers qa "
                "JOIN quiz_questions q ON q.id = qa.question_id AND q.scored = 1 AND q.correct >= 0 "
                "WHERE qa.participant_id = ?"
            )
            params: list = [participant_id]
            if revealed_ids is not None:
                if not revealed_ids:
                    return 0
                sql += f" AND q.id IN ({','.join('?' * len(revealed_ids))})"
                params.extend(sorted(revealed_ids))
            return conn.execute(sql, params).fetchone()[0]

    def quiz_scored_count(self) -> int:
        """Сколько зачётных вопросов уже задано — для подписей на экране."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM quiz_questions WHERE scored = 1 AND correct >= 0"
            ).fetchone()[0]

    def quiz_mark_revealed(self, question_id: int) -> None:
        """Отметить, что правильный ответ этого вопроса огласили.

        С этого момента его очки идут в личный счёт гостя. До оглашения
        прибавка в телефоне выдавала верный ответ раньше проектора.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE quiz_questions SET revealed_at = ? WHERE id = ? AND revealed_at IS NULL",
                (time.time(), question_id),
            )

    def quiz_revealed_ids(self) -> set[int]:
        """Вопросы, чей правильный ответ уже показан залу."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM quiz_questions WHERE revealed_at IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows}

    def quiz_clear_revealed(self) -> None:
        """Снять отметки об оглашении — при сбросе ответов.

        Без этого на повторном прогоне очки зачлись бы сразу: отметка
        осталась бы с прошлого раза.
        """
        with self.connect() as conn:
            conn.execute("UPDATE quiz_questions SET revealed_at = NULL")

    def quiz_clear_answers(self) -> int:
        """Сбросить все ответы — провести тот же набор заново."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM quiz_answers")
            return cur.rowcount or 0

    def quiz_total_answers(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM quiz_answers").fetchone()[0]

    # --- сообщения ------------------------------------------------------
    def participant_by_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM participants WHERE token = ?", (token,)
            ).fetchone()
        return dict(row) if row else None

    def create_participant(self, token: str, name: str) -> dict:
        now = time.time()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO participants (token, name, created_at, last_seen) VALUES (?,?,?,?)",
                (token, name, now, now),
            )
            pid = cur.lastrowid
            row = conn.execute("SELECT * FROM participants WHERE id = ?", (pid,)).fetchone()
        return dict(row)

    def rename_participant(self, participant_id: int, name: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE participants SET name = ? WHERE id = ?", (name, participant_id))

    def touch_participant(self, participant_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE participants SET last_seen = ? WHERE id = ?", (time.time(), participant_id)
            )

    def participant_messages(self, participant_id: int, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE participant_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (participant_id, limit),
            ).fetchall()
        return [message_to_dict(r) for r in rows]

    def author_token(self, message_id: int) -> str | None:
        """Токен сессии автора сообщения (или None, если автор уже не в базе)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT p.token AS token FROM messages m "
                "LEFT JOIN participants p ON p.id = m.participant_id "
                "WHERE m.id = ?",
                (message_id,),
            ).fetchone()
        return row["token"] if row else None

    # --- сводка ----------------------------------------------------------
    def stats(self) -> dict:
        with self.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE status != 'rejected'"
            ).fetchone()[0]
            people = conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        return {"total": total, "participants": people}


def _write_positions(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany(
        "UPDATE messages SET position = ? WHERE id = ?",
        [(float(pos), mid) for pos, mid in enumerate(ids, start=1)],
    )