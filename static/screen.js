/* Полноэкранный показ: одно сообщение за раз, сервер решает когда переключать */

const stage = document.getElementById('stage');
const slide = document.getElementById('slide');
const slideInner = document.getElementById('slideInner');
const photoBox = document.getElementById('slidePhoto');
const textBox = document.getElementById('slideText');
const authorBox = document.getElementById('slideAuthor');
const overlay = document.getElementById('overlay');
const overlayTitle = document.getElementById('overlayTitle');
const overlayText = document.getElementById('overlayText');
const overlayHint = document.getElementById('overlayHint');
const qrWindow = document.getElementById('qrWindow');
const screenQr = document.getElementById('screenQr');
const screenQrLink = document.getElementById('screenQrLink');
const splash = document.getElementById('splash');
const splashImg = document.getElementById('splashImg');
const splashText = document.getElementById('splashText');
const splashTitle = document.getElementById('splashTitle');
const splashSubtitle = document.getElementById('splashSubtitle');
let splashUrl = null;   // загруженная картинка; null — покажем название события

/* Названия события и номера слайдов на экране нет: проектор показывает
   только сами сообщения, без служебной подписи внизу. */
api(ev('/api/config')).then((cfg) => {
  overlayTitle.textContent = cfg.event_title || 'Экран готов';
  overlayText.textContent = cfg.event_subtitle || 'Здесь появятся сообщения и фото.';
  // если заставку не загружали, показываем название события
  splashTitle.textContent = cfg.event_title || '';
  splashSubtitle.textContent = cfg.event_subtitle || '';
  splashText.hidden = !cfg.event_title && !cfg.event_subtitle;
  splashUrl = cfg.splash || null;
  // конфиг приходит отдельным запросом и может обогнать состояние показа,
  // поэтому картинку ставим здесь и сразу показываем, если заставка уже включена
  applySplashImage();
  if (screenState.splash_visible) setSplash(true);
});

/** Надевает картинку заставки. Без файла показываем название события. */
function applySplashImage() {
  if (splashUrl) {
    splashImg.src = splashUrl;
    splashImg.hidden = false;
    splash.classList.add('has-img');
  } else {
    splashImg.hidden = true;
    splash.classList.remove('has-img');
  }
}

/** Показать или спрятать заставку. Плавность перехода делает CSS. */
function setSplash(visible) {
  applySplashImage();
  splash.classList.toggle('on', !!visible);
  if (visible) wake();
}

/* QR-код виден всем, поэтому ссылку на вход берём из открытого конфига */
api(ev('/api/invite-link')).then((d) => {
  screenQr.src = ev(`/api/qr.svg?v=${encodeURIComponent(d.code)}`);
  screenQrLink.textContent = d.link;
}).catch(() => { screenQrLink.textContent = ''; });

/* Проверяем, вошёл ли модератор — тогда работает управление с клавиатуры */
api(ev('/api/moderator/state')).then(() => { modCookie = true; }).catch(() => { modCookie = false; });

function setQrVisible(visible) {
  qrWindow.hidden = !visible;
  if (visible) wake();
}

const messages = new Map();   // id -> сообщение ленты
const gallery = new Map();    // id -> снимок из галереи слайдшоу
let order = [];               // id в порядке показа
let shownId = null;
let screenState = {
  playing: true, interval: 12, total: 0, index: 0,
  // 4.0: поля слайдшоу. Значения по умолчанию — чтобы на старте
  // код не спотыкался о состояние, ещё не пришедшее от сервера
  slideshow_visible: false, slideshow_current_id: null, slideshow_total: 0,
};
let modCookie = null;         // есть ли доступ к управлению экраном с этой страницы

function byPosition(ids) {
  return ids
    .filter((id) => messages.has(id))
    .sort((a, b) => (messages.get(a).position - messages.get(b).position)
                 || (a - b));
}

function setOverlay(visible, title, text, hint) {
  if (visible) {
    if (title) overlayTitle.textContent = title;
    if (text) overlayText.textContent = text;
    overlayHint.textContent = hint || '';
    overlay.hidden = false;
  } else {
    overlay.hidden = true;
  }
}

/* ---------------- Подгонка под экран ----------------
   Экран бывает любой: проектор 4:3, телевизор 16:9, телефон в портретной
   ориентации. Единая мера у всех — «безопасная зона»: всё, что осталось
   от .screen-stage после его отступов. Всё внутри неё и держим.

Что делится между фото, текстом и подписью — высота; ширина в этом не
   участвует, каждый элемент свою пропорцию сохраняет.

   Высота делится между тремя частями заранее и по долям, а не «сколько
   осталось»:

     * подпись занимает ровно AUTHOR_SHARE высоты зоны — всегда, на любом
       слайде, с фото и без, поэтому кадр от слайда к слайду не прыгает;
     * фото с текстом занимает ровно PHOTO_SHARE высоты зоны — независимо
       от того, сколько строк занял текст;
     * всё остальное достаётся тексту, и он под это место подстраивается.

   Только фото или только текст — как раньше: одна часть забирает всё. */

const MAX_UPSCALE = 3;         // во сколько раз можно увеличить маленькое фото
const TEXT_GROW_MAX = 3;       // во сколько раз короткий текст может вырасти
const TEXT_MIN_PX = 18;        // ниже этого текст уже никто не прочитает
const PHOTO_SHARE = 0.64;      // доля высоты зоны под фото, когда с ним текст
const AUTHOR_SHARE = 0.06;     // доля высоты зоны под подпись — всегда одна
const AUTHOR_MIN_PX = 9;       // ниже этого подпись перестаёт читаться
// запас на округление высот: line-height в долях даёт доли пикселя, и без
// запаса слайд на маленьком экране выходил за безопасную зону на 1–2 px
const SAFETY_PX = 4;
// Тот же запас, но внутри самого текста: offsetHeight округляется вниз,
// scrollHeight вверх, и без него текст в 488 px при лимите 490 px обрезался
// снизу на два пикселя.
const TEXT_SLACK_PX = 2;

/** Полезная область экрана: всё внутри отступов .screen-stage. */
function safeArea() {
  const cs = getComputedStyle(stage);
  return {
    w: Math.max(0, stage.clientWidth
      - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
    h: Math.max(0, stage.clientHeight
      - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)),
  };
}

/** Зазор между фото, текстом и подписью берём из вёрстки, а не дублируем. */
function slideGap() {
  return parseFloat(getComputedStyle(slideInner).rowGap) || 0;
}

/**
 * Подгоняет кегль текста под отведённую высоту.
 *
 * Короткий текст растёт до потолка TEXT_GROW_MAX, длинный ужимается,
 * но не ниже TEXT_MIN_PX. Считаем не «во сколько раз поместится», а меряем
 * по-настоящему: при увеличении шрифта текст начинает переноситься на
 * больше строк, и высота растёт быстрее кегля. Один расчёт по базовому
 * размеру давал текст, который вылезал за кадр, поэтому здесь цикл:
 * поставили кегль — измерили — поправили.
 */
function fitText(budget) {
  textBox.style.fontSize = '';
  if (budget <= 0) return;

  const base = parseFloat(getComputedStyle(textBox).fontSize) || 32;
  const ceiling = base * TEXT_GROW_MAX;
  let size = base;
  // Последний кегль, который померился и поместился. Ставим именно его:
  // размер, посчитанный в конце итерации, ещё не проверялся, а у границы
  // цикл раскачивается между двумя соседними кеглями и на выходе оставлял
  // как раз непроверенный — текст срезался снизу на 2-3 px.
  let best = null;

  for (let i = 0; i < 8; i++) {
    textBox.style.fontSize = `${Math.round(size)}px`;
    // обрежется текст или нет — говорит scrollHeight, а не offsetHeight:
    // глифы бывают выше строки, блок меряется по ней, и разница в 2-4 px
    // как раз и съедала нижнюю строку
    const sh = textBox.scrollHeight;
    if (!sh) break;
    if (sh + TEXT_SLACK_PX <= budget) {
      best = Math.round(size);
      if (size >= ceiling) break;
      const next = Math.min(ceiling, size * ((budget - TEXT_SLACK_PX) / sh));
      if (next - size < 1) break;     // упёрлись: расти больше некуда
      size = next;
    } else {
      // коэффициент берём по scrollHeight, а не по offsetHeight: при блоке
      // 240 px и глифах 244 px отношение по offsetHeight давало единицу,
      // цикл решал, что ужимать нечего, и выходил с обрезанным текстом
      const next = Math.max(TEXT_MIN_PX, size * ((budget - TEXT_SLACK_PX) / sh));
      if (next >= size) break;        // упёрлись в минимальный кегль
      size = next;
    }
  }
  textBox.style.fontSize = `${best ?? Math.round(size)}px`;
}
/** Подгоняет фото, текст и подпись под текущий размер окна. */
function layoutSlide() {
  const area = safeArea();
  if (!area.w || !area.h) return;

  const gap = slideGap();
  const img = photoBox.firstElementChild;
  const hasPhoto = !photoBox.hidden && !!img;
  const hasText = !!textBox.textContent.trim();

  // фото, текст и подпись стоят в одном блоке: зазоров столько, сколько
  // соседств — фото–текст и текст–подпись
  const gapsTotal = gap * ((hasPhoto ? 1 : 0) + 1);
  const innerH = Math.max(0, area.h - SAFETY_PX);

  // Подпись занимает свою долю высоты всегда — с фото, без фото, с текстом
  // и без: кадр от слайда к слайду не прыгает
  const authorH = fitAuthor(innerH * AUTHOR_SHARE);

  if (!hasPhoto || !hasText) {
    // только фото или только текст: как было — одна часть забирает всё
    const rest = innerH - authorH - gapsTotal;
    fitText(rest);
    if (hasPhoto) setPhoto(img, area, rest);
    return;
  }

  // фото с текстом: ровно своя доля высоты, независимо от длины текста
  const photoH = innerH * PHOTO_SHARE;
  fitText(Math.max(0, innerH - photoH - authorH - gapsTotal));
  setPhoto(img, area, photoH);
}

/** Ставит фото заданной высоты, сохраняя пропорции и границы кадра. */
function setPhoto(img, area, roomH) {
  const natW = img.naturalWidth;
  const natH = img.naturalHeight;
  if (!natW || !natH) return;   // картинка ещё не загрузилась, подождём

  const scale = Math.max(0.05, Math.min(
    area.w / natW,
    Math.max(0, roomH) / natH,
    MAX_UPSCALE,
  ));

  // пропорции не трогаем: иначе фото растянется по горизонтали или вертикали
  photoBox.style.width = `${Math.round(natW * scale)}px`;
  photoBox.style.height = `${Math.round(natH * scale)}px`;
}

/** Обычный кегль подписи — тот, что задан в вёрстке, без всяких ужиманий. */
function authorBasePx() {
  authorBox.style.fontSize = '';
  return parseFloat(getComputedStyle(authorBox).fontSize) || 16;
}

/**
 * Отдаёт подписи её долю высоты — budget пикселей — на любом слайде и любом
 * экране. Кегль ищется от высоты строки: одна строка ровно в свою долю,
 * а если имя не влезает в одну строку, берём самый крупный кегль, при
 * котором текст ещё помещается в отведённую высоту (тогда долю делят
 * две-три строки, и подпись по-прежнему занимает её целиком).
 *
 * Высота текста убывает с ростом кегля — мельче шрифт, короче строки и
 * ниже блок, — поэтому наибольший подходящий кегль ищем делением отрезка,
 * а не «подогнать по числу строк»: тот способ ужимал подпись втрое там,
 * где она спокойно уместилась в две строки.
 *
 * Возвращает занятую высоту: на неё дальше считают остальным.
 */
function fitAuthor(budget) {
  const base = authorBasePx();   // начинаем от вёрстки, а не от прошлого слайда
  if (budget <= 0) return authorBox.offsetHeight;

  // вычисленный line-height приходит уже в пикселях, а нужна доля:
  // сколько высоты занимает строка при кегле в 1 px
  const lhPx = parseFloat(getComputedStyle(authorBox).lineHeight);
  const ratio = Number.isFinite(lhPx) && lhPx > 0 ? lhPx / base : 1.2;

  // сверху — кегль, при котором подпись заняла бы ровно свою долю одной
  // строкой; снизу — читаемый минимум
  const top = Math.max(AUTHOR_MIN_PX, budget / ratio);
  let lo = AUTHOR_MIN_PX;
  let hi = top;
  for (let i = 0; i < 9; i++) {
    const mid = (lo + hi) / 2;
    if (mid - lo < 0.5 || hi - mid < 0.5) break;
    authorBox.style.fontSize = `${Math.round(mid)}px`;
    // scrollHeight, а не offsetHeight: глифы выше строки обрезаются именно
    // по нему, и подпись теряла нижние пиксели своей доли
    if (authorBox.scrollHeight <= budget + 1) lo = mid; else hi = mid;
  }
  authorBox.style.fontSize = `${Math.round(lo)}px`;
  return Math.min(budget, authorBox.offsetHeight);
}

/* Подстановка содержимого слайда. renderToken гасит гонку: если слайд
   сменили, пока предыдущий ещё прятался, старый отложенный вызов не вставит
   своё содержимое поверх нового. */
function paint(id) {
  // форма одна и та же: и сообщение ленты, и снимок галереи — это id, image,
  // body, author
  const m = messages.get(id) || gallery.get(id);
  if (!m) return;

  if (m.image) {
    const url = ev(`/media/${encodeURIComponent(m.image)}`);
    const img = document.createElement('img');
    img.alt = '';
    // размер считаем после загрузки: до неё naturalWidth равен нулю
    img.addEventListener('load', layoutSlide, { once: true });
    img.src = url;
    photoBox.replaceChildren(img);
    photoBox.hidden = false;
    stage.classList.add('has-photo');
    stage.style.setProperty('--photo-bg', `url("${url}")`);
  } else {
    photoBox.replaceChildren();
    photoBox.hidden = true;
    photoBox.style.width = '';
    photoBox.style.height = '';
    stage.classList.remove('has-photo');
    stage.style.removeProperty('--photo-bg');
  }
  textBox.style.fontSize = '';
  textBox.textContent = m.body || '';
  authorBox.textContent = m.author ? `— ${m.author}` : '';
  layoutSlide();
}

let renderToken = 0;
let swapTimer = null;

/** Снимок из галереи: рисуем сразу, без задержки переключения ленты. */
function renderItem(item) {
  shownId = item.id;
  paint(item.id);
}

function render(id) {
  const m = messages.get(id) || gallery.get(id);
  if (!m) return;

  const token = ++renderToken;
  slide.classList.add('swap');
  shownId = id;
  clearTimeout(swapTimer);
  swapTimer = setTimeout(() => {
    if (token !== renderToken) return;   // слайд успели переключить — не вмешиваемся
    paint(id);
    slide.classList.remove('swap');
  }, 300);
}

function showCurrent(state) {
  if (state) screenState = { ...screenState, ...state };
  // QR поверх всего: слайд под ним не трогаем, но и не перелистываем
  setQrVisible(!!screenState.qr_visible);
  // заставка — тоже отдельный слой поверх показа
  setSplash(!!screenState.splash_visible);

  // 4.0: слайдшоу занимает кадр целиком. Если галерея пуста, честно
  // говорим об этом, а не показываем пустой кадр: ведущий подумает, что
  // экран сломался
  if (screenState.slideshow_visible) {
    // 4.0: слайдшоу показывает галерею, а не ленту. Галерея живёт отдельно,
    // поэтому снимок остаётся на экране, даже если его сообщение из ленты
    // ведущий уже удалил
    const sid = screenState.slideshow_current_id;
    const item = sid != null ? gallery.get(sid) : null;
    if (!item) {
      setOverlay(true, 'Слайдшоу пустое',
        'В галерее пока нет ни одной фотографии.',
        'Откройте кабинет ведущего, вкладка «Слайдшоу»');
      return;
    }
    setOverlay(false);
    if (sid !== shownId) renderItem(item);
    return;
  }

  if (!order.length) {
    setOverlay(true, 'Пока пусто', 'Сообщения и фото появятся здесь автоматически.',
      'Отправьте первое — и оно сразу будет на экране');
    return;
  }
  const id = screenState.current_id ?? order[0];
  if (id == null || !messages.has(id)) {
    setOverlay(true, 'Пока пусто', 'Сообщения и фото появятся здесь автоматически.');
    return;
  }
  setOverlay(false);
  if (id !== shownId) render(id);
}

connectWS(ev('/ws/display'), {
  onMessage(msg) {
    switch (msg.type) {
      case 'init':
        // init приходит и при переподключении — старые слайды надо забыть
        messages.clear();
        gallery.clear();
        (msg.messages || []).forEach((m) => messages.set(m.id, m));
        (msg.gallery || []).forEach((g) => gallery.set(g.id, g));
        order = byPosition([...messages.keys()]);
        screenState = msg.screen || screenState;
        shownId = null;
        showCurrent();
        break;

      case 'new':
      case 'refresh': {
        const fresh = new Set();
        (msg.messages || []).forEach((m) => { messages.set(m.id, m); fresh.add(m.id); });
        for (const id of [...messages.keys()]) if (!fresh.has(id)) messages.delete(id);
        // галерея приходит целиком: снимок мог уйти из ленты и остаться здесь
        gallery.clear();
        (msg.gallery || []).forEach((g) => gallery.set(g.id, g));
        order = byPosition([...messages.keys()]);
        screenState = msg.screen || screenState;
        showCurrent();
        break;
      }

      case 'screen':
        // заставку ведущий мог сменить или убрать, пока экран был открыт
        if ('splash' in msg) splashUrl = msg.splash || null;
        screenState = msg.screen || screenState;
        showCurrent();
        break;
    }
  },
});

/* Управление клавишами работает, только если с этой вкладки вошёл модератор
   (иначе запрос вернёт 403, а экран просто продолжит листаться сам). */
document.addEventListener('keydown', async (e) => {
  const keys = {
    ArrowRight: 'next',
    ArrowLeft: 'prev',
    ' ': () => (screenState.playing ? 'pause' : 'play'),
  };
  const action = keys[e.key];
  if (!action || !modCookie) return;
  e.preventDefault();
  const name = typeof action === 'function' ? action() : action;
  try {
    await postForm(ev('/api/moderator/screen'), { action: name });
  } catch (ex) {
    modCookie = false;
    showToast('Клавиши работают только при входе модератора', 'warn');
  }
});

/* HUD и курсор прячем, если по экрану давно не двигали мышью */
let idleTimer = null;
function wake() {
  document.body.classList.add('show-cursor');
  document.body.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    document.body.classList.remove('show-cursor');
    document.body.classList.add('idle');
  }, 4000);
}
['mousemove', 'keydown', 'touchstart'].forEach((ev) => window.addEventListener(ev, wake));
wake();

/* Полный экран по клику — удобно для проектора */
stage.addEventListener('click', () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => {});
  else document.exitFullscreen?.();
});

/* Размер окна изменился — пересчитываем: проектор могли переключить
   с 16:9 на 4:3, или вкладку растянули на другом мониторе */
let relayoutTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(relayoutTimer);
  relayoutTimer = setTimeout(layoutSlide, 120);
});
document.addEventListener('fullscreenchange', () => {
  clearTimeout(relayoutTimer);
  relayoutTimer = setTimeout(layoutSlide, 200);
});