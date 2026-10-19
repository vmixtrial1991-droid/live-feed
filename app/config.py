"""Общие настройки приложения: пути, ограничения, окружение, пароли.

Настройки конкретных событий здесь не живут — они в app/events.py,
у каждого события свой файл. Здесь только то, что одинаково для всех.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# LF_DATA_DIR позволяет тестам развернуть приложение во временной папке
DATA_DIR = Path(os.environ.get("LF_DATA_DIR") or (BASE_DIR / "data"))
EVENTS_DIR = DATA_DIR / "events"
STATIC_DIR = BASE_DIR / "static"
ADMIN_CONFIG_PATH = DATA_DIR / "config.json"

INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих символов (0/O, 1/I)


def _env_int(name: str, default: int, low: int = 1, high: int = 100_000) -> int:
    """Целое из окружения. Мусор в переменной не должен ломать запуск."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        return default

# Ограничения
# Текст на экране должен читаться с последнего ряда, поэтому длина жёсткая:
# 56 символов — это примерно четыре строки на большом экране. Меняется
# переменной LF_MAX_TEXT_LEN, чтобы править не код.
MAX_TEXT_LEN = _env_int("LF_MAX_TEXT_LEN", 56, low=10, high=2000)
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 МБ исходник
IMAGE_MAX_SIDE = 1600
IMAGE_QUALITY = 82

# Насколько можно увеличить маленькое фото на экране. Растягивать сильнее
# бессмысленно: пикселей больше не станет, будет каша.
MAX_PHOTO_UPSCALE = 3.0

# «Безопасная зона»: проекторы и телевизоры обрезают края кадра, поэтому
# содержимое держим внутри этих отступов.
SAFE_ZONE_X = 4.0   # процентов ширины экрана
SAFE_ZONE_Y = 5.0   # процентов высоты экрана

ADMIN_COOKIE = "lf_admin"
PARTICIPANT_COOKIE_PREFIX = "lf_pid_"
MODERATOR_COOKIE_PREFIX = "lf_mod_"
SCREEN_COOKIE_PREFIX = "lf_screen_"

# Сессия отправителя теперь — билет на один вечер: адрес события публичен,
# а право смотреть ленту и фото даёт код приглашения. Держим cookie до конца
# вечера (8 часов), а не 90 дней: ссылка с фотками не должна жить в чатах
# вечно, и после события её можно отозвать rotate-invite.
PARTICIPANT_COOKIE_MAX_AGE = 8 * 3600
MODERATOR_COOKIE_MAX_AGE = 12 * 3600
ADMIN_COOKIE_MAX_AGE = 12 * 3600
# Экран проектора живёт весь праздник — он в зале, не в кармане гостя.
SCREEN_COOKIE_MAX_AGE = 24 * 3600
# Короткая подписанная ссылка на фото: 10 минут, чтобы утёкшее имя файла
# не стало вечной ссылкой на чужое лицо.
MEDIA_SIG_TTL = 10 * 60


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


# --- работа за обратным прокси ------------------------------------------------
def parse_trusted_networks(
    raw: str,
) -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    """Разбирает список доверенных адресов: точное совпадение или CIDR-подсеть.

    Точный список нужен рядом с приложением, подсети — когда прокси
    за Docker: там адрес контейнера меняется при каждом пересоздании.
    """
    hosts: set[str] = set()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "/" in item:
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                continue
        else:
            hosts.add(item)
    return hosts, nets


TRUSTED_PROXY_HOSTS, TRUSTED_PROXY_NETS = parse_trusted_networks(
    os.environ.get("TRUSTED_PROXIES", "")
)


def is_trusted_proxy(host: str) -> bool:
    """Адрес считается доверенным и при точном совпадении, и по подсети."""
    if not host:
        return False
    if host in TRUSTED_PROXY_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXY_NETS)


# --- есть ли https ----------------------------------------------------------
# Путь к сертификату. Пока файла нет, сайт обязан работать по http:
# редирект на https увёл бы посетителя в никуда, а браузер перестал бы
# принимать cookie с флагом Secure. Пустое значение означает «https работает,
# проверять нечего» — так ведут себя обычные установки без Docker.
CERT_FILE = os.environ.get("CERT_FILE", "").strip()

_cert_cache: tuple[float, bool] | None = None
_CERT_TTL = 10.0  # секунд: файл появляется во время работы приложения


def https_ready() -> bool:
    """Можно ли уже гонять редирект на https и ставить флаг Secure.

    Проверяем не просто существование файла, а что он непустой: при
    неудачном выпуске сертификата acme.sh оставляет файл нулевого
    размера, и сайт с редиректом на https стал бы недоступен целиком.
    Всё проверяется на лету, а не один раз при старте: сертификат
    появляется позже, и после этого всё должно заработать само.
    """
    global _cert_cache

    if not CERT_FILE:
        return True

    import time as _time

    now = _time.monotonic()
    if _cert_cache is not None and now - _cert_cache[0] < _CERT_TTL:
        return _cert_cache[1]

    try:
        ready = os.path.getsize(CERT_FILE) > 0
    except OSError:
        ready = False
    _cert_cache = (now, ready)
    return ready


FORCE_HTTPS = env_bool("FORCE_HTTPS")                   # редирект http → https
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
TRUSTED_HOSTS = env_list("TRUSTED_HOSTS")               # если задано — Host проверяется

# Флаг Secure на cookie ставим, когда сайт живёт по https: по http такой cookie
# браузер просто не примет, и гость «потеряет» регистрацию.
_default_secure = FORCE_HTTPS or PUBLIC_BASE_URL.startswith("https://")
COOKIE_SECURE = env_bool("COOKIE_SECURE", default=_default_secure)


# --- пароли и подписи --------------------------------------------------------
def invite_token(n: int = 8) -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(n))


def random_password(n: int = 10) -> str:
    # без символов, которые легко спутать при вводе с телефона
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(n))


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000
    ).hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    if not password or not salt or not expected:
        return False
    return secrets.compare_digest(hash_password(password, salt), expected)


def sign(secret: str, purpose: str) -> str:
    """Подпись для cookie: доказывает, что cookie выдал сервер.

    Значение подписывается назначением (purpose), поэтому cookie одного
    события не подойдёт к другому, даже если имена совпали бы.
    """
    return hmac.new(
        secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def signature_matches(secret: str, purpose: str, value: str | None) -> bool:
    if not secret or not value:
        return False
    # compare_digest умеет только ASCII-строки, а значение приходит из
    # cookie, где могут оказаться любые символы. Сравниваем байты.
    return secrets.compare_digest(sign(secret, purpose).encode(), value.encode())


def local_ip() -> str | None:
    """IP-адрес этого компьютера в локальной сети (для QR-кода)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


# --- настройки админки --------------------------------------------------------
def _read_admin_file() -> dict:
    if not ADMIN_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(ADMIN_CONFIG_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_admin_file(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ADMIN_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ADMIN_CONFIG_PATH)


def admin_settings() -> dict:
    """Настройки админки: пароль и секрет для подписи cookie.

    Создаются при первом обращении. Пароль из переменной окружения важнее
    файла: иначе перезапуск с новым ADMIN_PASSWORD не сработал бы.
    """
    data = _read_admin_file()

    if "admin_salt" not in data:
        data["admin_salt"] = secrets.token_bytes(16).hex()
    if "admin_secret" not in data:
        data["admin_secret"] = secrets.token_hex(32)

    env_password = os.environ.get("ADMIN_PASSWORD")
    if env_password:
        data["admin_password"] = env_password
        data["admin_hash"] = hash_password(env_password, data["admin_salt"])
    elif "admin_hash" not in data:
        data["admin_password"] = random_password(12)
        data["admin_hash"] = hash_password(data["admin_password"], data["admin_salt"])

    data.setdefault("base_url", PUBLIC_BASE_URL)

    _write_admin_file(data)
    return data