"""Проверка миграции: база прошлой версии должна получить новую колонку.

    python test_migration.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# подменяем путь к базе до импорта app.config
TMP = Path(tempfile.mkdtemp(prefix="lf-migration-"))
os.environ["LF_DATA_DIR"] = str(TMP)

sys.path.insert(0, ".")
from app import config  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def make_old_database(path: Path) -> None:
    """База ровно такой, какой она была до отказа от модерации."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER REFERENCES participants(id) ON DELETE SET NULL,
            author TEXT NOT NULL DEFAULT 'Аноним',
            body TEXT NOT NULL DEFAULT '',
            image TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reject_reason TEXT,
            position REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            moderated_at REAL
        );
        """
    )
    # три записи в разных состояниях: ждёт, отклонено, в эфире
    conn.executemany(
        "INSERT INTO messages (author, body, status, reject_reason, position, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("Аня", "ждёт проверки", "pending", None, 1.0, 100.0),
            ("Игорь", "было отклонено", "rejected", "не подходит", 2.0, 200.0),
            ("Ольга", "уже в эфире", "approved", None, 3.0, 300.0),
        ],
    )
    conn.commit()
    conn.close()


def columns(path: Path, table: str = "messages") -> set[str]:
    conn = sqlite3.connect(path)
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return have


def rows(path: Path) -> list[tuple]:
    """Строки messages. shown_at берём по именам колонок, а не по позиции:
    в старой базе этой колонки ещё нет."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    have = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    shown = "shown_at" in have
    sql = "SELECT id, author, status, shown_at FROM messages ORDER BY id" if shown \
        else "SELECT id, author, status, NULL AS shown_at FROM messages ORDER BY id"
    out = [(r["id"], r["author"], r["status"], r["shown_at"])
           for r in conn.execute(sql)]
    conn.close()
    return out


print("\n=== Схема прошлой версии ===")
db_path = TMP / "app.db"
make_old_database(db_path)
check("в старой базе нет колонки shown_at", "shown_at" not in columns(db_path))
check("в старой базе есть записи в трёх состояниях", len(rows(db_path)) == 3)

print("\n=== Миграция при старте ===")
from app.db import EventStore  # noqa: E402

store = EventStore(db_path)
store.init()
check("колонка shown_at добавилась", "shown_at" in columns(db_path))
after = rows(db_path)
check("все записи сохранились", len(after) == 3, str(len(after)))
check("ожидавшие модерации стали обычными",
      all(r[2] == "approved" for r in after), str([(r[1], r[2]) for r in after]))
check("отклонённые вернулись в эфир — модерации больше нет",
      all(r[2] == "approved" for r in after))
check("shown_at изначально пуст", all(r[3] is None for r in after))

print("\n=== Работа с новой схемой ===")
store.mark_shown(1)
marked = rows(db_path)
check("mark_shown проставил отметку", marked[0][3] is not None, str(marked[0][3]))
store.reset_shown()
check("reset_shown снял отметки", all(r[3] is None for r in rows(db_path)))

listed = store.live_messages()
check("в списке эфира видны все записи", len(listed) == 3, str(len(listed)))
check("в списке есть поле shown_at", all("shown_at" in m for m in listed))
check("порядок показа по position",
      [m["id"] for m in store.live_messages(ascending=True)] == [1, 2, 3],
      str([m["id"] for m in store.live_messages(ascending=True)]))

print("\n=== Повторный запуск ===")
store.init()
check("повторная инициализация не ломает базу", len(rows(db_path)) == 3)
check("колонка не задвоилась", "shown_at" in columns(db_path))

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Миграция со старой схемы проходит без потери данных.")
