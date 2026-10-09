"""Архив фотографий события (4.0).

Загруженные фотографии лежат в папке события и живут своей жизнью: удаление
сообщения из ленты или из слайдшоу убирает строку из базы, но **не трогает
файл**. Иначе ведущий, разбирая очередь и убирая неудачные кадры, затирал бы
оригиналы, и собрать фотографии мероприятия было бы уже нельзя.

Отсюда три операции:

* `summary()`     — сколько фотографий и сколько они занимают;
* `zip_stream()` — отдать всё одним архивом для сохранения себе;
* `wipe()`        — стереть архив целиком, необратимо.

Папка события и её содержимое — единственное хранилище: копий «на всякий
случай» не делаем, иначе фотографии съедали бы место вдвое. Страховка от
потери здесь сознательная: перед стиранием ведущий подтверждает действие.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterator

ARCHIVE_NAME = "photos.zip"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _files(uploads_dir: str | Path) -> list[Path]:
    """Фотографии в архиве, по имени — порядок одинаковый на всех машинах."""
    root = Path(uploads_dir)
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda p: p.name,
    )


def summary(uploads_dir: str | Path) -> dict:
    """Сколько фотографий накопилось и сколько они занимают."""
    files = _files(uploads_dir)
    total = 0
    oldest = None
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        total += st.st_size
        if oldest is None or st.st_mtime < oldest:
            oldest = st.st_mtime
    return {
        "count": len(files),
        "bytes": total,
        "oldest_at": oldest,
        "names": [p.name for p in files],
    }


def zip_stream(uploads_dir: str | Path, prefix: str = "") -> Iterator[bytes]:
    """Отдаёт содержимое архива по кускам, не собирая его целиком в памяти.

    Фотографий может быть много, а памяти на сервере мало: zip собирается
    на лету и отдаётся потоком. Префиксом кладём файлы в папку внутри архива,
    чтобы при распаковке не рассыпались прямо в «Загрузки».
    """
    buffer = _ZipBuffer()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in _files(uploads_dir):
            try:
                zf.write(path, arcname=f"{prefix}{path.name}" if prefix else path.name)
            except OSError:
                continue   # файл могли стереть прямо сейчас — просто пропускаем
    yield from buffer.chunks()


class _ZipBuffer:
    """Пишущая обёртка: zipfile пишет по кускам, отдаём их же наружу."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def chunks(self) -> Iterator[bytes]:
        for part in self._parts:
            if part:
                yield part


def wipe(uploads_dir: str | Path) -> int:
    """Стирает архив целиком. Возвращает число удалённых файлов."""
    removed = 0
    for path in _files(uploads_dir):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed