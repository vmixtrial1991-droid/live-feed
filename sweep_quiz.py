"""Прогон экрана квиза через реальные размеры окна.

Зачем отдельный скрипт: инструменты агента не умеют менять размер вьюпорта,
а экран квиза считает кегли от vh/vw и от объёма содержимого. Проверять
его в одном фиксированном окне бессмысленно — на 16:9 и 3:4 раскладка
разная.

Chrome запускается headless с Debugging-портом, размер вьюпорта задаётся
через Emulation.setDeviceMetricsOverride (это честный размер, а не zoom
и не transform). Скриншоты складываются рядом, чтобы можно было посмотреть
глазами.

    python sweep_quiz.py                        # все вопросы, основные экраны
    python sweep_quiz.py --only 6,9             # выбранные вопросы
    python sweep_quiz.py --sizes 1280x720,1024x768
    python sweep_quiz.py --screen               # только разрешения

Отчёт печатается таблицей; колонка «доля» — сколько высоты кадра заняло
горизонтальное фото (правило: не меньше 50%). Колонка «проблемы» перечисляет
нарушения: фото мельче половины, карточки разной высоты, номера разного
размера, таймер наехал на вопрос, содержимое вылезло за кадр.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# Основные экраны: 16:9, 16:10 и 3:4. Плюс крайние случаи, чтобы видеть,
# что вёрстка не держится на одном разрешении.
SIZES = [
    (1280, 720, "16:9 720p"),
    (1920, 1080, "16:9 FullHD"),
    (2560, 1440, "16:9 2K"),
    (1680, 1050, "16:10"),
    (1920, 1200, "16:10 WUXGA"),
    (1024, 768, "3:4 XGA"),
    (1536, 2048, "3:4 iPad портрет"),
]

EVENT = "proverka-raskladok"
SCREEN_KEY = "2WZTF3ZTS"
MOD_PASSWORD = "proverka-raskladok-1"
BASE = "http://127.0.0.1:8123"


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    sys.exit("Chrome/Edge не найден — укажите путь в CHROME_CANDIDATES")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CDP:
    """Минимальный клиент Chrome DevTools Protocol: без зависимостей."""

    def __init__(self, ws_url: str) -> None:
        from websockets.sync.client import connect

        self.ws = connect(ws_url, max_size=64 * 1024 * 1024, legacy=True)
        self.id = 0

    def send(self, method: str, **params) -> dict:
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        end = time.time() + 25
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv(timeout=1))
            except TimeoutError:
                continue
            if msg.get("id") == self.id:
                return msg
        raise RuntimeError(f"CDP не ответил на {method}")

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"])
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def wait_json(port: int, path: str, timeout: float = 30.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"Chrome не ответил на {path}")


def measure_js(qid: int) -> str:
    """Меряем кадр вопроса: что где стоит и не вылезло ли за кадр."""
    return f"""
(async () => {{
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  // ждём, пока экран нарисует нужный вопрос
  for (let i = 0; i < 80; i++) {{
    const b = document.getElementById('quizBody');
    const play = document.getElementById('quizPlay');
    const t = document.getElementById('quizQText');
    const opts = document.getElementById('quizOpts');
    if (b && play && !play.hidden && t && t.textContent && !play.classList.contains('is-swap')
        && opts && opts.children.length) break;
    await sleep(100);
  }}
  // подгонка идёт в несколько проходов (кегль, ширина, высота ряда):
  // ждём, пока размеры и кегли перестанут меняться
  let prev = '';
  for (let i = 0; i < 40; i++) {{
    const opts = document.getElementById('quizOpts');
    const cards = [...(opts ? opts.children : [])];
    const sig = cards.map(c => {{
      const r = c.getBoundingClientRect();
      return Math.round(r.width) + 'x' + Math.round(r.height)
        + '/' + Math.round(parseFloat(getComputedStyle(c).fontSize) * 10);
    }}).join('|');
    if (sig && sig === prev) break;
    prev = sig;
    await sleep(100);
  }}
  await sleep(250);

  const play = document.getElementById('quizPlay');
  const body = document.getElementById('quizBody');
  const head = document.getElementById('quizHead');
  const timer = document.getElementById('quizTimer');
  const shot = document.getElementById('quizShot');
  const img = document.getElementById('quizShotImg');
  const q = document.getElementById('quizQText');
  const opts = document.getElementById('quizOpts');
  const count = document.getElementById('quizCount');
  const inner = play.querySelector('.quiz-fit');
  const fit = inner.getBoundingClientRect();
  const R = (el) => {{ const r = el.getBoundingClientRect(); return {{
      l: Math.round(r.left), t: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height),
      b: Math.round(r.bottom), r: Math.round(r.right) }}; }};

  const cards = [...opts.children].filter(e => e.classList.contains('quiz-s-opt'));
  const rects = cards.map(R);
  const nums = cards.map(c => c.querySelector('.quiz-s-idx')).filter(Boolean).map(R);
  const texts = cards.map(c => c.querySelector('.quiz-s-text')).filter(Boolean);

  // переполнение: содержимое шире/выше кадра
  const vw = innerWidth, vh = innerHeight;
  const overflow = [];
  if (fit.t < -1) overflow.push('fit сверху ' + fit.t);
  if (fit.b > vh + 1) overflow.push('fit снизу ' + (fit.b - vh));
  for (const [i, r] of rects.entries()) {{
    if (r.l < 0 || r.r > vw) overflow.push('карточка ' + (i+1) + ' по ширине');
  }}
  const timerR = timer.hidden ? null : R(timer);
  const qR = R(q);
  const timerOverlap = timerR ? timerR.b > qR.t + 1 : false;

  return JSON.stringify({{
    viewport: [vw, vh],
    question: (q.textContent || '').slice(0, 60),
    hasImage: body.classList.contains('has-image'),
    shape: ['on-shot-wide','on-shot-square','on-shot-tall']
             .find(c => body.classList.contains(c)) || null,
    zoom: inner.style.zoom || '1',
    image: img && img.naturalWidth ? R(img) : null,
    questionBox: qR,
    headBottom: R(head).b,
    timer: timerR,
    timerOverlap,
    cardSizes: rects.map(r => [r.w, r.h]),
    cardHeightsEqual: new Set(rects.map(r => r.h)).size <= 1,
    idxSizes: nums.map(r => [r.w, r.h]),
    idxSame: new Set(nums.map(r => r.w + 'x' + r.h)).size <= 1,
    fonts: texts.map(t => Math.round(parseFloat(getComputedStyle(t).fontSize))),
    // правило: горизонтальное фото занимает не меньше половины кадра
    shotShare: (img && img.naturalWidth && body.classList.contains('on-shot-wide'))
      ? +(R(img).h / vh).toFixed(3) : null,
    questionFont: Math.round(parseFloat(getComputedStyle(q).fontSize)),
    overflow,
  }});
}})()
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="прогон экрана квиза по разрешениям")
    ap.add_argument("--only", help="номера вопросов через запятую")
    ap.add_argument("--screen", action="store_true", help="только разрешения, без вопросов")
    ap.add_argument("--sizes", help="размеры через запятую, например 1280x720,1680x1050,1024x768")
    ap.add_argument("--out", default="sweep-out", help="куда класть скриншоты")
    args = ap.parse_args()

    sizes = SIZES
    if args.sizes:
        wanted = {s.strip() for s in args.sizes.split(",")}
        sizes = [t for t in SIZES if f"{t[0]}x{t[1]}" in wanted]
        if not sizes:
            sys.exit(f"нет таких размеров в списке: {args.sizes}")

    chrome = find_chrome()
    port = free_port()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    profile = Path(os.environ.get("TEMP", ".")) / "opencode" / "chrome-sweep"

    proc = subprocess.Popen([
        chrome, "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--hide-scrollbars", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        wait_json(port, "/json/version")
        # берём именно вкладку страницы: в списке бывают служебные цели
        tabs = wait_json(port, "/json/list")
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if page is None:
            sys.exit("Chrome не открыл вкладку")
        ws_url = page["webSocketDebuggerUrl"]
        cdp = CDP(ws_url)

        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # вопросы берём прямо из базы события: API ведущего требует входа,
        # а для прогона раскладок достаточно списка id
        qids: list[int] = []
        if not args.screen:
            if args.only:
                qids = [int(x) for x in args.only.split(",")]
            else:
                import sqlite3

                db = Path("data") / "events" / EVENT / "app.db"
                with sqlite3.connect(db) as conn:
                    qids = [r[0] for r in conn.execute(
                        "SELECT id FROM quiz_questions ORDER BY position, id")]

        rows = []
        # открываем нужный вопрос через API ведущего: экран показывает ту
        # стадию, что на сервере, и подменять её иначе нельзя
        import http.cookiejar
        import urllib.parse

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        login = urllib.parse.urlencode({"password": MOD_PASSWORD}).encode()
        opener.open(f"{BASE}/e/{EVENT}/api/moderator/login", login, timeout=10).read()

        def set_question(qid: int) -> None:
            form = urllib.parse.urlencode(
                {"action": "open", "question_id": qid}).encode()
            opener.open(f"{BASE}/e/{EVENT}/api/moderator/quiz/control",
                        form, timeout=10).read()

        for w, h, name in sizes:
            cdp.send("Emulation.setDeviceMetricsOverride",
                     width=w, height=h, deviceScaleFactor=1, mobile=False)
            for qid in (qids or [0]):
                if qid:
                    try:
                        set_question(qid)
                    except Exception as exc:
                        print(f"  вопрос {qid} не открылся: {exc}")
                cdp.send("Page.navigate",
                         url=f"{BASE}/e/{EVENT}/screen?d={SCREEN_KEY}")
                time.sleep(1.4)
                val = cdp.js(measure_js(qid))
                if not val:
                    rows.append({"size": name, "qid": qid, "error": "нет данных"})
                    continue
                m = json.loads(val)
                m["size"] = name
                m["qid"] = qid
                rows.append(m)

                shot = cdp.send("Page.captureScreenshot", format="png")
                b64 = shot.get("result", {}).get("data")
                if b64:
                    import base64

                    fn = out / f"{name.replace(':', '-').replace(' ', '_')}-q{qid}.png"
                    fn.write_bytes(base64.b64decode(b64))

        (out / "report.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print_report(rows)
        return 0
    finally:
        cdp = None
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def print_report(rows: list[dict]) -> None:
    print(f"{'размер':<18} {'вопр':>4} {'форма':<11} {'zoom':>5} {'фото':>11} "
          f"{'карточки':>12} {'номера':>10} {'доля':>5} {'проблемы'}")
    for m in rows:
        if "error" in m:
            print(f"{m['size']:<18} {m['qid']:>4}  {m['error']}")
            continue
        img = m["image"]
        img_s = f"{img['w']}x{img['h']}" if img else "—"
        heights = {r[1] for r in m["cardSizes"]}
        widths = {r[0] for r in m["cardSizes"]}
        card_s = f"{min(widths)}-{max(widths)}x{min(heights)}"
        idx = m["idxSizes"]
        idx_s = f"{idx[0][0]}x{idx[0][1]}" if idx else "—"
        bad = list(m["overflow"])
        if not m["cardHeightsEqual"]:
            bad.append("высоты разные")
        if not m["idxSame"]:
            bad.append("номера разные")
        if m["timerOverlap"]:
            bad.append("таймер на вопросе")
        if m["shotShare"] is not None and m["shotShare"] < 0.5:
            bad.append(f"фото {round(m['shotShare']*100)}% кадра")
        share = f"{round(m['shotShare']*100)}%" if m["shotShare"] is not None else "—"
        print(f"{m['size']:<18} {m['qid']:>4} {str(m['shape'] or '—'):<11} "
              f"{m['zoom']:>5} {img_s:>11} {card_s:>12} {idx_s:>10} "
              f"{share:>5} {', '.join(bad) if bad else 'ок'}")
    print(f"\nскриншоты и report.json: {Path('sweep-out').resolve()}")


if __name__ == "__main__":
    raise SystemExit(main())
