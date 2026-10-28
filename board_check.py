"""Проверка таблицы лидеров на живых участниках.

Заводит событие, добавляет вопросы, отвечает от имени нескольких гостей,
открывает и закрывает приём, выводит таблицу на экран и снимает кадр.

    python board_check.py            # на 127.0.0.1:8123
    python board_check.py --keep     # событие не удалять
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import testlib
from sweep_quiz import CDP, find_chrome, free_port, wait_json

BASE = "http://127.0.0.1:8123"
NAMES = [
    "Аня Петрова", "Борис Ким", "Вера Луговая", "Глеб Орлов",
    "Даша Смирнова", "Егор Волков", "Женя Соколова", "Захар Мороз",
    "Инна Крылова", "Костя Лебедев", "Лена Павлова", "Миша Титов",
]


def post(op, path, fields):
    """path — полный адрес (testlib.prepare отдаёт base с хостом)."""
    url = path if path.startswith("http") else BASE + path
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with op.open(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def get(op, path):
    url = path if path.startswith("http") else BASE + path
    with op.open(url, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--gender", action="store_true")
    args = ap.parse_args()

    ctx = testlib.prepare("Лидеры")
    base = ctx.base
    mod = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    post(mod, f"{base}/api/moderator/login",
         {"password": ctx.mod_password})

    # Пять зачётных вопросов по два варианта — простые, чтобы отвечать легко.
    qids = []
    for i in range(5):
        st, r = post(mod, f"{base}/api/moderator/quiz/add", {
            "text": f"Проверочный вопрос номер {i + 1}?",
            "options": json.dumps(["Верно", "Неверно"], ensure_ascii=False),
            "correct": "0", "scored": "1", "seconds": "0",
        })
        if st == 200:
            qids.append(r["question"]["id"])

    # Гости входят в игру: у каждого своя банка cookie.
    guests = []
    for name in NAMES:
        guest = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        st, _ = post(guest, f"{base}/api/join",
                     {"name": name, "invite": ctx.code})
        if st == 200:
            guests.append(guest)
    print(f"гостей вошло: {len(guests)}")

    # Проводим вопросы по цепочке: открыть → все отвечают → закрыть →
    # показать ответ → дальше. Отвечать надо, пока приём открыт, иначе
    # сервер ответ не примет и таблица останется пустой.
    for qi, qid in enumerate(qids):
        post(mod, f"{base}/api/moderator/quiz/control",
             {"action": "open", "question_id": str(qid)})
        for n, guest in enumerate(guests):
            # у каждого гостя своё число верных: места в таблице разные
            right = qi < (len(guests) - n) % (len(qids) + 1)
            post(guest, f"{base}/api/quiz/answer",
                 {"question_id": qid, "option": "0" if right else "1"})
        post(mod, f"{base}/api/moderator/quiz/control", {"action": "close"})
        post(mod, f"{base}/api/moderator/quiz/control", {"action": "reveal"})
        post(mod, f"{base}/api/moderator/quiz/control", {"action": "next"})

    # Сначала открываем вопрос и закрываем приём: на блоке остаются
    # переменные вопроса, и видно, подтягивает ли таблица его вёрстку.
    post(mod, f"{base}/api/moderator/quiz/control",
         {"action": "open", "question_id": str(qids[0])})
    time.sleep(0.1)
    post(mod, f"{base}/api/moderator/quiz/control", {"action": "close"})
    time.sleep(0.1)
    post(mod, f"{base}/api/moderator/quiz/control", {"action": "board"})
    time.sleep(0.3)

    h = get(mod, f"{base}/api/moderator/quiz")
    print("на экране:", h.get("phase"), "участников:", h.get("participants"))
    for row in (h.get("board") or [])[:5]:
        print("  ", row.get("rank"), row.get("name"), row.get("score"))

    cfg = json.load(open(os.path.join("data", "events", ctx.slug, "config.json"),
                        encoding="utf-8"))
    key = cfg["screen_key"]
    url = f"{base}/screen?d={key}"

    chrome = find_chrome()
    port = free_port()
    profile = Path(os.environ.get("TEMP", ".")) / "opencode" / "chrome-board3"
    proc = subprocess.Popen([chrome, "--headless=new",
                             f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run",
                             "--disable-gpu", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = Path("layout-stand")
    out.mkdir(exist_ok=True)
    try:
        wait_json(port, "/json/version")
        page = next(t for t in wait_json(port, "/json/list")
                    if t.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        # Без отключения кеша скриншот снимается со старым CSS/JS.
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", cacheDisabled=True)
        for w, hgt, tag in ((1920, 1080, "16x9"), (1280, 720, "1280x720"),
                            (1440, 1080, "4x3")):
            cdp.send("Emulation.setDeviceMetricsOverride", width=w, height=hgt,
                     deviceScaleFactor=1, mobile=False)
            cdp.send("Page.navigate", url=url)
            time.sleep(1.5)
            data = cdp.send("Page.captureScreenshot", format="png") \
                .get("result", {}).get("data")
            if data:
                name = f"board-{tag}.png"
                (out / name).write_bytes(base64.b64decode(data))
                print("снято", name)
    finally:
        proc.terminate()

    if not args.keep:
        testlib.remove(ctx.slug)
    else:
        print("событие оставлено:", ctx.slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
