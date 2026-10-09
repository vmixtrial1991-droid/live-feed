"""Запуск приложения.

    python run.py                 # обычный запуск
    python run.py --port 9000     # другой порт
    python run.py --host 0.0.0.0  # доступ из локальной сети (по умолчанию)
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

import uvicorn

from app import config, events


def local_ips() -> list[str]:
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if not ip.startswith("127.") and ":" not in ip:
                ips.add(ip)
    except OSError:
        pass
    ip = config.local_ip()
    if ip:
        ips.add(ip)
    return sorted(ips)


def _fix_console() -> None:
    """Консоль Windows часто в cp1251 — принудительно UTF-8, иначе рамка не рисуется."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Python 3.7+
        except (AttributeError, ValueError, OSError):
            pass


def banner(port: int) -> None:
    """Приветствие: событий ещё нет, поэтому показываем вход в админку."""
    admin = config.admin_settings()
    ips = local_ips()
    public = config.PUBLIC_BASE_URL
    main_host = public.replace("https://", "").replace("http://", "") or (
        "localhost" if not ips else ips[0]
    )
    scheme = "https" if public.startswith("https") else "http" if public else "http"
    address = f"{scheme}://{main_host}" + ("" if public else f":{port}")

    line = "─" * 62
    print(f"\n┌{line}┐")
    print(f"│  Живая лента — события")
    print(f"│")
    print(f"│  Админка:            {address}/admin")
    print(f"│  Пароль админа:      {admin.get('admin_password', '')}")
    print(f"│")
    print(f"│  Событие заводится в админке: там появятся три ссылки —")
    print(f"│  для ведущего, для экрана и для гостей с QR-кодом.")
    if not public and len(ips) > 1:
        print(f"│")
        for other in ips[1:]:
            print(f"│  Другой адрес в сети: http://{other}:{port}/admin")
    print(f"└{line}┘\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Живая лента: приём сообщений и фото")
    # внутри контейнера адрес и порт приходят из переменных окружения
    parser.add_argument("--host", default=os.environ.get("LF_HOST", "0.0.0.0"),
                        help="адрес прослушивания")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LF_PORT", "8000")),
                        help="порт")
    parser.add_argument("--reload", action="store_true", help="перезагрузка кода (для разработки)")
    args = parser.parse_args()

    _fix_console()
    events.init_all()
    banner(args.port)

    # за WebSocket отвечает uvicorn сам, отдельный сервер не нужен
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
        access_log=False,
        ws_ping_interval=30,
        ws_ping_timeout=60,
        # приложение само гасит WebSocket-соединения при остановке
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        sys.exit(0)