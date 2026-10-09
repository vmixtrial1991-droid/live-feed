"""Хранилище одного события: SQLite со схемой и функциями работы.

Каждое событие живёт в своей базе (app/events/<slug>/app.db). Общей базы
нет намеренно: события не должны видеть сообщения друг друга, а удаление
события должно уносить с собой ровно его файлы.
"""

from __future__ import annotations

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

CREATE INDEX IF NOT EXISTS idx_messages_status  ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_participant ON messages(participant_id);
CREATE INDEX IF NOT EXISTS idx_messages_position ON messages(status, position);
-- Индекс по in_slideshow создаётся не здесь, а в _migrate: у базы,
-- созданной прошлой версией, этой колонки ещё нет, и CREATE INDEX на
-- несуществующей колонке роняет executescript до того, как миграция
-- успеет её добавить.
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


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
    if "in_slideshow" not in have:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN in_slideshow INTEGER NOT NULL DEFAULT 0"
        )
    # индекс создаём после ALTER, а не в SCHEMA: см. комментарий там
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_slideshow "
        "ON messages(in_slideshow, image)"
    )
    # клининг по фотографии: флаг без картинки смысла не имеет
    conn.execute("UPDATE messages SET in_slideshow = 0 WHERE image IS NULL")

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
        # 4.0: есть ли фотография в галерее слайдшоу
        "in_slideshow": bool(d.get("in_slideshow")),
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

    # --- слайдшоу (4.0) --------------------------------------------------
    # В галерею слайдшоу попадают только фотографии: и чистые, и с текстом.
    # Текст без картинки ставить нельзя — на экране слайдшоу фотография и
    # занимает почти весь кадр, а текстовая строка сама по себе там лишняя.
    def slideshow_messages(self, ascending: bool = True) -> list[dict]:
        order = "ASC" if ascending else "DESC"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE in_slideshow = 1 AND image IS NOT NULL "
                f"ORDER BY position {order}, id {order}"
            ).fetchall()
        return [message_to_dict(r) for r in rows]

    def slideshow_ids(self) -> list[int]:
        """ID фотографий слайдшоу в порядке показа."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM messages WHERE in_slideshow = 1 AND image IS NOT NULL "
                "ORDER BY position ASC, id ASC"
            ).fetchall()
        return [r["id"] for r in rows]

    def set_slideshow(self, message_id: int, on: bool) -> dict[str, Any] | None:
        """Поставить или снять галочку слайдшоу.

        None — если сообщения нет либо это не фотография: тогда галочку
        ставить нельзя, и ведущий должен знать об этом, а не получить
        молчаливое «ничего не произошло».
        """
        row = self.get_message(message_id)
        if row is None or not row.get("image"):
            return None
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET in_slideshow = ? WHERE id = ?",
                (1 if on else 0, message_id),
            )
        return self.get_message(message_id)

    def clear_slideshow(self) -> int:
        """Снять все галочки — например, перед «удалить все фото»."""
        with self.connect() as conn:
            cur = conn.execute("UPDATE messages SET in_slideshow = 0 WHERE in_slideshow = 1")
            return cur.rowcount or 0

    def photo_message_ids(self) -> list[str]:
        """Имена файлов всех сообщений с фотографией."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT image FROM messages WHERE image IS NOT NULL"
            ).fetchall()
        return [r["image"] for r in rows]

    def delete_photo_messages(self) -> int:
        """Убрать из ленты все сообщения с фотографией. Возвращает число.

        Слайдшоу при этом тоже очищается: фотографий в галерее больше нет,
        и держать флаги на удалённых строках незачем.
        """
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE image IS NOT NULL")
            return cur.rowcount or 0

    # --- участники -------------------------------------------------------
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