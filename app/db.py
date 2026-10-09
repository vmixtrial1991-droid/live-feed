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