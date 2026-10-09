"""Прогон экрана через кучу разрешений: ничего не должно вылезать за кадр.

Страницу экрана грузим в iframe нужного размера и меряем геометрию внутри.
Так проверяются и проектор 4:3, и телевизор 16:9, и телефон в портретной
ориентации — а не только то окно, в котором смотрим сами.

Запуск: открыть demo_setup.py, потом в браузере выполнить sweep() с адресом
события. Скрипт отдаёт функцию для консоли браузера:

    window.sweep('/e/demo-pokaza/screen')
"""

RESOLUTIONS = [
    # (ширина, высота, название) — типичные проекторы, телевизоры и телефоны
    (640, 480, "проектор 4:3, мелкий"),
    (1024, 768, "проектор 4:3, обычный"),
    (1280, 720, "телевизор 16:9"),
    (1600, 900, "ноутбук 16:9"),
    (1920, 1080, "Full HD"),
    (2560, 1440, "2K"),
    (3840, 2160, "4K"),
    (800, 1280, "телефон в портретной"),
    (1366, 768, "старый ноутбук"),
    (720, 720, "квадрат"),
]

SWEEP_JS = r"""
async function sweep(path) {
  const out = [];
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:-99999px;top:0;';
  document.body.appendChild(host);

  for (const [w, h, name] of %(RESOLUTIONS)s) {
    const frame = document.createElement('iframe');
    frame.width = w;
    frame.height = h;
    frame.style.cssText = `width:${w}px;height:${h}px;border:0`;
    host.appendChild(frame);

    const doc = await new Promise((res) => {
      frame.onload = () => res(frame.contentDocument);
      frame.src = path;
    });

    // ждём, пока придёт список слайдов по сокету
    const win = frame.contentWindow;
    for (let i = 0; i < 60 && !(win.order && win.order.length); i++) {
      await new Promise((r) => setTimeout(r, 100));
    }

    const stage = doc.getElementById('stage');
    const cs = win.getComputedStyle(stage);
    const sb = stage.getBoundingClientRect();
    const padL = parseFloat(cs.paddingLeft);
    const padR = parseFloat(cs.paddingRight);
    const padT = parseFloat(cs.paddingTop);
    const padB = parseFloat(cs.paddingBottom);

    const slides = [];
    for (const id of win.order) {
      win.paint(id);
      const img = doc.querySelector('#slidePhoto img');
      if (img && !img.complete) {
        await new Promise((res) => img.addEventListener('load', res, { once: true }));
      }
      win.layoutSlide();
      await new Promise((r) => setTimeout(r, 60));

      const slide = doc.getElementById('slide').getBoundingClientRect();
      const t = doc.getElementById('slideText');
      const box = doc.getElementById('slidePhoto');
      const a = doc.getElementById('slideAuthor');
      const m = win.messages.get(id);
      slides.push({
        текст: (m.body || 'ФОТО').slice(0, 14).replace(/\n/g, '|'),
        кегль: Math.round(parseFloat(win.getComputedStyle(t).fontSize)),
        высотаТекста: t.offsetHeight,
        текстОбрезан: t.scrollHeight > t.offsetHeight + 1,
        фото: box.hidden ? 0 : Math.round(parseFloat(box.style.width)),
        фотоВысота: box.hidden ? 0 : Math.round(parseFloat(box.style.height)),
        наружу: {
          слева: Math.round(sb.left + padL - slide.left),
          справа: Math.round(slide.right - (sb.right - padR)),
          сверху: Math.round(sb.top + padT - slide.top),
          снизу: Math.round(slide.bottom - (sb.bottom - padB)),
        },
      });
    }

    frame.remove();
    out.push({ имя: `${w}x${h} ${name}`, слайдов: slides.length, слайды: slides });
  }
  host.remove();
  return out;
}
"""


def build_js() -> str:
    rows = ", ".join(f"({w}, {h}, {name!r})".replace("'", '"')
                     for w, h, name in RESOLUTIONS)
    return SWEEP_JS % {"RESOLUTIONS": rows}


if __name__ == "__main__":
    print("Вставьте в консоль браузера на открытой странице:")
    print()
    print(build_js())
    print()
    print("и вызовите:  await sweep('/e/<событие>/screen')")