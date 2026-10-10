"""Записывает контрольные суммы комплекта в dist/.

Файл переехал по сети, и перед развёртыванием где-нибудь ещё раз стоит
убедиться, что он не пострадал. sha256 делает это одной командой на любой
машине, где есть Python.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DIST = Path(__file__).parent / "dist"
VER = "4.0"

FILES = [
    DIST / f"livefeed-{VER}-images.tar.gz",
    DIST / f"livefeed-{VER}-source.tar.gz",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


lines = [
    f"# Контрольные суммы комплекта развёртывания {VER}",
    "# Проверка после копирования на другую машину:",
    "#   python -c \"import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())\" файл",
    "",
]
for p in FILES:
    if not p.exists():
        lines.append(f"# ОТСУТСТВУЕТ: {p.name}")
        continue
    size = p.stat().st_size
    lines.append(f"{sha256(p)}  {p.name}  ({size / 1024 / 1024:.1f} МБ)")

out = DIST / "SHA256SUMS.txt"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
out.write_bytes(out.read_bytes().replace(b"\r\n", b"\n"))

print(out.read_text(encoding="utf-8"))
print("записано:", out)
