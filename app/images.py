"""Работа с изображениями: проверка, сжатие, сохранение.

Папку загрузок передаёт вызывающий: у каждого события она своя, поэтому
фото одного ивента не может случайно оказаться в другом.
"""

from __future__ import annotations

import io
import secrets
import time
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import IMAGE_MAX_SIDE, IMAGE_QUALITY, MAX_UPLOAD_BYTES

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP"}


class ImageError(ValueError):
    """Не удалось обработать файл."""


def save_upload(
    data: bytes,
    uploads_dir: str | Path,
    original_name: str = "",
    name: str | None = None,
) -> str:
    """Проверяет и сохраняет картинку в JPEG. Возвращает имя файла.

    С аргументом name файл называется заданным — так заставка события
    всегда лежит под одним именем и заменяет прежнюю.
    """
    if not data:
        raise ImageError("Пустой файл.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(
            f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ. "
            "Сожмите фото или уменьшите его."
        )

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            probe.verify()
        if fmt not in ALLOWED_FORMATS:
            raise ImageError("Поддерживаются только изображения (JPG, PNG, WEBP).")
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError):
        raise ImageError("Не удалось прочитать изображение — похоже, файл повреждён.")

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)  # учтём ориентацию из EXIF
            if img.mode in ("RGBA", "LA", "P"):
                # прозрачность убираем на белый фон
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.LANCZOS)

            name = name or f"{int(time.time() * 1000)}-{secrets.token_hex(4)}.jpg"
            out = Path(uploads_dir) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            img.save(out, "JPEG", quality=IMAGE_QUALITY, optimize=True, progressive=True)
    except ImageError:
        raise
    except OSError as exc:
        raise ImageError(f"Ошибка сохранения: {exc}")

    return name


def image_orientation(name: str, uploads_dir: str | Path) -> str:
    """Ориентация сохранённого файла: wide, square или tall.

    Нужна, чтобы выбрать лимиты текста для вопроса: горизонтальное фото
    стоит сверху и делит с текстом высоту, вертикальное — слева и оставляет
    справа всю ширину, поэтому лимиты у них разные.

    Файл уже пережат при сохранении, так что чтение заголовка дешёвое.
    При любой ошибке считаем фото горизонтальным: это самый строгий лимит
    по ширине, и лучше не дать написать слишком длинный текст, чем
    получить обрезанный кадр.
    """
    try:
        with Image.open(Path(uploads_dir) / name) as img:
            w, h = img.size
    except (OSError, ValueError):
        return "wide"
    if not w or not h:
        return "wide"
    ratio = w / h
    if ratio >= 1.2:
        return "wide"
    if ratio <= 0.85:
        return "tall"
    return "square"


def delete_upload(name: str | None, uploads_dir: str | Path) -> None:
    if not name:
        return
    root = Path(uploads_dir).resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return  # попытка выйти за пределы папки загрузок
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_orphans(uploads_dir: str | Path, store) -> int:
    """Удаляет файлы, на которые больше нет ссылок в базе события."""
    used = {m["image"] for m in store.live_messages(limit=100_000) if m.get("image")}
    root = Path(uploads_dir)
    if not root.exists():
        return 0
    removed = 0
    for path in root.glob("*"):
        if path.is_file() and path.name not in used:
            path.unlink(missing_ok=True)
            removed += 1
    return removed