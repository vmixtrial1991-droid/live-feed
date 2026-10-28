"""Проверка перехода вопрос → таблица лидеров → вопрос.

Главный риск: после таблицы на блоке остаётся её разметка, и следующий
вопрос не пересчитывается. Здесь это ловится по геометрии кадра.

    python board_scene_check.py
"""

from __future__ import annotations

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import testlib
from sweep_quiz import CDP, find_chrome, free_port, wait_json

BASE = "http://127.0.0.1:8123"

MEASURE = r"""
(() => {
  const R = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; };
  const play = document.getElementById('quizPlay');
  const q = document.getElementById('quizQText');
  const opts = document.getElementById('quizOpts');
  const plan = (typeof quizPlayLayout !== 'undefined') ? quizPlayLayout.plan : null;
  return JSON.stringify({
    layout: play.dataset.layout || null,
    board: {
      rowH: play.style.getPropertyValue('--board-row-h'),
      rowSize: play.style.getPropertyValue('--board-row-size'),
    },
    q: R(q), qFont: q ? parseFloat(getComputedStyle(q).fontSize) : 0,
    opts: R(opts),
    cards: [...opts.querySelectorAll('.quiz-s-opt')].map(R),
    rows: opts.querySelectorAll('.quiz-s-row').length,
    plan: plan ? {layout: plan.layout, a: Math.round(plan.answers.font),
                  q: Math.round(plan.question.font), cols: plan.answers.cols} : null,
    zoom: play.querySelector('.quiz-fit').style.zoom,
    title: q ? q.textContent.slice(0, 30) : '',
  });
})()
"""


def post(op, path, fields):
    url = path if path.startswith("http") else BASE + path
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with op.open(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def main() -> int:
    ctx = testlib.prepare("Сцены")
    mod = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    post(mod, f"{ctx.base}/api/moderator/login", {"password": ctx.mod_password})

    qids = []
    for i, (text, opts) in enumerate([
        ("Сколько будет два плюс два?", ["Три", "Четыре"]),
        ("Что важнее на большом экране?", ["Читаемость", "Красота", "Скорость"]),
        ("Какая фигура нарисована?", ["Круг", "Квадрат", "Ромб", "Овал"]),
    ]):
        st, r = post(mod, f"{ctx.base}/api/moderator/quiz/add", {
            "text": text,
            "options": json.dumps(opts, ensure_ascii=False),
            "correct": "0", "scored": "1", "seconds": "0"})
        if st == 200:
            qids.append(r["question"]["id"])

    guests = []
    for n in range(6):
        g = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        post(g, f"{ctx.base}/api/join",
             {"name": f"Гость {n + 1}", "invite": ctx.code})
        guests.append(g)

    # Отвечаем на первый вопрос и закрываем приём — так в таблице будут люди.
    post(mod, f"{ctx.base}/api/moderator/quiz/control",
         {"action": "open", "question_id": str(qids[0])})
    for g in guests:
        post(g, f"{ctx.base}/api/quiz/answer",
             {"question_id": qids[0], "option": "0"})
    post(mod, f"{ctx.base}/api/moderator/quiz/control", {"action": "close"})

    cfg = json.load(open(os.path.join("data", "events", ctx.slug, "config.json"),
                        encoding="utf-8"))
    url = f"{ctx.base}/screen?d={cfg['screen_key']}"

    chrome = find_chrome()
    port = free_port()
    profile = Path(os.environ.get("TEMP", ".")) / "opencode" / "chrome-scene"
    proc = subprocess.Popen([chrome, "--headless=new",
                             f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run",
                             "--disable-gpu", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = Path("layout-stand")
    out.mkdir(exist_ok=True)
    failures = []
    try:
        wait_json(port, "/json/version")
        page = next(t for t in wait_json(port, "/json/list")
                    if t.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", cacheDisabled=True)
        cdp.send("Emulation.setDeviceMetricsOverride", width=1920, height=1080,
                 deviceScaleFactor=1, mobile=False)

        def open_screen():
            cdp.send("Page.navigate", url=url)
            time.sleep(1.5)

        def shot(name):
            data = cdp.send("Page.captureScreenshot", format="png") \
                .get("result", {}).get("data")
            if data:
                (out / name).write_bytes(base64.b64decode(data))

        def measure(label):
            m = json.loads(cdp.js(MEASURE))
            print(f"  {label}: layout={m['layout']} rows={m['rows']} "
                  f"карточек={len(m['cards'])} кегли="
                  f"{m['plan']['q'] if m['plan'] else '—'}/"
                  f"{m['plan']['a'] if m['plan'] else '—'} "
                  f"board={m['board']['rowH'] or '—'}")
            return m

        # 1. Вопрос открыт.
        post(mod, f"{ctx.base}/api/moderator/quiz/control",
             {"action": "reopen"})
        open_screen()
        m1 = measure("вопрос")
        shot("scene-1-question.png")
        if m1["layout"] != "v3" or not m1["plan"]:
            failures.append("первый вопрос не разложен")
        if len(m1["cards"]) != 2:
            failures.append(f"у вопроса {len(m1['cards'])} карточек, ждали 2")

        # 2. Таблица лидеров.
        post(mod, f"{ctx.base}/api/moderator/quiz/control", {"action": "board"})
        time.sleep(0.4)
        m2 = measure("таблица")
        shot("scene-2-board.png")
        if m2["layout"] != "board":
            failures.append("таблица не переключила разметку")
        if m2["rows"] < 1:
            failures.append("в таблице нет строк")

        # 3. Возврат к вопросу — главная проверка.
        post(mod, f"{ctx.base}/api/moderator/quiz/control", {"action": "back"})
        time.sleep(0.5)

        # Третий вопрос: другой набор ответов, после таблицы.
        post(mod, f"{ctx.base}/api/moderator/quiz/control",
             {"action": "open", "question_id": str(qids[2])})
        time.sleep(0.5)
        m3 = measure("вопрос после таблицы")
        shot("scene-3-question-after-board.png")
        if m3["layout"] != "v3":
            failures.append(f"после таблицы разметка {m3['layout']}, ждали v3")
        if not m3["plan"]:
            failures.append("после таблицы план не посчитался")
        elif m3["plan"]["cols"] != 2:
            failures.append(
                f"у вопроса с четырьмя ответами {m3['plan']['cols']} колонки")
        if len(m3["cards"]) != 4:
            failures.append(f"карточек {len(m3['cards'])}, ждали 4")
        if m3["board"]["rowH"]:
            failures.append("после таблицы остались её размеры строк")
        if m1["qFont"] and m3["qFont"] < m1["qFont"] * 0.6:
            failures.append(f"кегль вопроса упал: {m3['qFont']} против {m1['qFont']}")
    finally:
        proc.terminate()
        testlib.remove(ctx.slug)

    print()
    if failures:
        for f in failures:
            print("  FAIL", f)
        return 1
    print("Сцены переключаются верно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
