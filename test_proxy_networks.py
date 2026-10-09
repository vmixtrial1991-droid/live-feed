"""Проверка доверия прокси по подсетям CIDR.

Скрипт проверяет разбор TRUSTED_PROXIES: за Docker адрес прокси меняется
при каждом пересоздании, поэтому доверять нужно подсети, а не точному адресу.

Запуск без сервера: python test_proxy_networks.py
"""

from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILURES.append(name)


def load(raw: str):
    """Перечитывает app.config с новым TRUSTED_PROXIES.

    Пакет app тоже нужно выгрузить: иначе импортируется уже загруженный
    app.config и настройка останется прежней.
    """
    import os

    os.environ["TRUSTED_PROXIES"] = raw
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    from app import config  # noqa: E402
    return config


print("\n=== Разбор списка доверенных адресов ===")
cfg = load("127.0.0.1,::1,172.16.0.0/12,10.1.2.3")
check("точный адрес распознан", cfg.is_trusted_proxy("127.0.0.1"))
check("IPv6 распознан", cfg.is_trusted_proxy("::1"))
check("адрес из подсети распознан", cfg.is_trusted_proxy("172.20.0.5"))
check("точный адрес в списке распознан", cfg.is_trusted_proxy("10.1.2.3"))
check("чужой адрес не доверен", not cfg.is_trusted_proxy("203.0.113.9"))
check("соседняя подсеть не доверена", not cfg.is_trusted_proxy("172.32.0.1"))
check("localhost не доверен, если его нет в списке", not cfg.is_trusted_proxy("localhost"))
check("пустой адрес не доверен", not cfg.is_trusted_proxy(""))
check("мусор не доверен и не ломает разбор", not cfg.is_trusted_proxy("не адрес"))

print("\n=== Верхняя граница подсети ===")
cfg = load("172.16.0.0/12")
check("последний адрес 172.31.255.255 в подсети", cfg.is_trusted_proxy("172.31.255.255"))
check("172.32.0.0 уже вне подсети", not cfg.is_trusted_proxy("172.32.0.0"))

print("\n=== Один адрес без подсети ===")
cfg = load("192.168.1.5")
check("указанный адрес доверен", cfg.is_trusted_proxy("192.168.1.5"))
check("соседний не доверен", not cfg.is_trusted_proxy("192.168.1.6"))

print("\n=== Пустой список ===")
cfg = load("")
check("никто не доверен при пустом списке", not cfg.is_trusted_proxy("127.0.0.1"))
check("is_trusted_proxy возвращает bool", isinstance(cfg.is_trusted_proxy("1.2.3.4"), bool))

print("\n=== Строка для uvicorn ===")
cfg = load("127.0.0.1,172.16.0.0/12")
joined = ",".join(sorted(cfg.TRUSTED_PROXY_HOSTS | {str(n) for n in cfg.TRUSTED_PROXY_NETS}))
check("список непустой", bool(joined), joined)
check("точное значение попало в строку", "127.0.0.1" in joined)
check("CIDR попал в строку как есть", "172.16.0.0/12" in joined, joined)

print("\n=== Итог ===")
if FAILURES:
    print(f"Провалено проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("Доверие прокси по подсетям работает.")
