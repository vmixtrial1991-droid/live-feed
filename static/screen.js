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

// 5.0: слой квиза. Сервер присылает готовую картинку стадии (холл с QR,
// вопрос, распределение, лидеры) — у экрана собственного мнения нет, и правильный
// ответ он узнаёт только когда ведущий покажет ответ
const quizScreen = document.getElementById('quizScreen');
const quizPlay = document.getElementById('quizPlay');
const quizLobby = document.getElementById('quizLobby');
const lobbyTitle = document.getElementById('lobbyTitle');
const lobbySub = document.getElementById('lobbySub');
const lobbyQr = document.getElementById('lobbyQr');
const lobbyHint = document.getElementById('lobbyHint');
const quizTag = document.getElementById('quizTag');
const quizTimer = document.getElementById('quizTimer');
const quizQText = document.getElementById('quizQText');
const quizFree = document.getElementById('quizFree');
const quizOpts = document.getElementById('quizOpts');
const quizCount = document.getElementById('quizCount');

/* Названия события и номера слайдов на экране нет: проектор показывает
   только сами сообщения, без служебной подписи внизу. */
/* 4.1: ключ экрана. Сервер отдал его в window.LF.screenKey (он же был в
   ?d= адресной строки). Прячем его из адресной строки сразу, чтобы он не
   утёк через Referer/историю, и дальше тянем все запросы с ним в query.
   Если инлайн-вставка window.LF не сработала (строгая CSP), ключ берём
   прямо из ?d= — страница открыта именно по этой ссылке. */
const screenKey = (window.LF && window.LF.screenKey)
  || (new URLSearchParams(location.search).get('d') || '');
if (screenKey && location.search.includes('d=')) {
  try { history.replaceState(null, '', location.pathname); } catch (_) {}
}
function withKey(url) {
  if (!screenKey) return url;
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + 'd=' + encodeURIComponent(screenKey);
}

api(withKey(ev('/api/config'))).then((cfg) => {
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

/** Принимает заставку из состояния показа.

    Заставку меняют на ходу: загрузили новую, убрали старую — открытый экран
    обязан увидеть это сразу, а не после перезагрузки страницы. Без файла
    показываем название события.
    */
function setSplashUrl(url) {
  const next = url || null;
  if (next === splashUrl) return;
  splashUrl = next;
  applySplashImage();
}

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
api(withKey(ev('/api/invite-link'))).then((d) => {
  screenQr.src = withKey(ev(`/api/qr.svg?v=${encodeURIComponent(d.code)}`));
  screenQrLink.textContent = d.link;
}).catch(() => { screenQrLink.textContent = ''; });

/* Проверяем, вошёл ли модератор — тогда работает управление с клавиатуры */
api(ev('/api/moderator/state')).then(() => { modCookie = true; }).catch(() => { modCookie = false; });

function setQrVisible(visible) {
  qrWindow.hidden = !visible;
  if (visible) wake();
}

/* ---------------- Квиз (5.0) ---------------- */
let quizFrame = null;   // последний кадр игры от сервера; null — лента

/* Отсчёт. Сервер присылает сколько секунд осталось на момент кадра,
   дальше экран ведёт счёт сам: кадры прилетают не по секундам, а когда
   кто-то ответит, и цифра стояла бы на месте. Кончаться может только
   время — приём закрывает ведущий, поэтому «Время вышло!» ничего не
   блокирует, а только напоминает залу поторопиться. */
let quizDeadline = null;
let quizTicker = null;

/* Таблица лидеров бывает двух видов: промежуточная листает весь зал
   пачками (вести её руками ведущему незачем), финальная — первая
   десятка, первое место крупно, и стоит неподвижно до конца вечера. */
const BOARD_PAGE = 10;
const BOARD_STEP_MS = 8000;
const FINAL_TOP = 10;
let boardPages = [];
let boardPage = 0;
let boardTimer = null;
let boardFinal = false;
let boardTotal = 0;

function stopQuizTicker() {
  if (quizTicker) { clearInterval(quizTicker); quizTicker = null; }
  quizDeadline = null;
  quizTimer.hidden = true;
  quizTimer.textContent = '';
  quizTimer.classList.remove('is-over');
}

function stopBoardScroll() {
  if (boardTimer) { clearInterval(boardTimer); boardTimer = null; }
  boardPages = [];
  boardPage = 0;
}

function tickQuizTimer() {
  if (quizDeadline === null) return;
  const left = Math.max(0, Math.ceil((quizDeadline - Date.now()) / 1000));
  quizTimer.hidden = false;
  if (left > 0) {
    // новый вопрос при работающем тикере наследует старое «Время вышло!»,
    // поэтому снимаем напоминание здесь — иначе цифра и вид разошлись бы
    quizTimer.classList.remove('is-over');
    quizTimer.textContent = String(left);
    return;
  }
  quizTimer.classList.add('is-over');
  quizTimer.textContent = 'Время вышло!';
  // тикать дальше не о чем: цифра кончилась, висит напоминание
  if (quizTicker) { clearInterval(quizTicker); quizTicker = null; }
}

function boardRow(r, hero) {
  const el = document.createElement('div');
  el.className = 'quiz-s-row' + (hero ? ' is-hero' : '');
  const medal = { 1: '🥇', 2: '🥈', 3: '🥉' }[r.rank] || r.rank;
  el.innerHTML = `<span class="quiz-s-pos">${medal}</span>
    <span class="quiz-s-name">${escapeHTML(r.name)}</span>
    <span class="quiz-s-score">${r.score}</span>`;
  return el;
}

function paintBoard() {
  if (!boardPages.length) {
    quizOpts.innerHTML =
      '<p class="quiz-s-note">Пока никто не отвечал на зачётные вопросы.</p>';
    quizCount.textContent = '';
    return;
  }
  if (boardPage >= boardPages.length) boardPage = 0;
  const rows = boardPages[boardPage];
  // крупно — только первый ряд первой пачки финала: при равных очках
  // первых может быть двое, и оба крупных уже не помещаются
  quizOpts.replaceChildren(
    ...rows.map((r, i) => boardRow(r, boardFinal && boardPage === 0 && i === 0))
  );
  if (boardFinal) {
    quizCount.textContent = boardTotal > rows.length
      ? `Показана первая десятка, всех участников — ${boardTotal}`
      : `Всего участников с ответами: ${boardTotal}`;
  } else if (boardPages.length > 1) {
    quizCount.textContent =
      `Участников: ${boardTotal} · страница ${boardPage + 1} из ${boardPages.length}`;
  } else {
    quizCount.textContent = `Участников: ${boardTotal}`;
  }
}

/* Слой игры ложится на ленту градиентом, а не включается щелчком: hidden
   здесь не подходит — браузерное display:none перебивает css-переход,
   и вместо плавного наложения вышло бы мигание */
let shownBlock = null;   // что занимает слой: 'lobby' | 'q<id>' | 'board'

function setQuizLayer(on) {
  quizScreen.classList.toggle('is-on', !!on);
  if (!on) {
    stopQuizTicker();
    stopBoardScroll();
    shownBlock = null;
  }
}

/* Содержимое слоя меняется (холл → вопрос → таблица), и новый блок въезжает
   сверху; градиент под ним при этом остаётся на месте, поэтому зал видит
   плавное наложение, а не мигающий фон. Возвращает true, если блок новый. */
function showQuizBlock(block, key) {
  const swap = shownBlock !== key;
  quizLobby.hidden = block !== quizLobby;
  quizPlay.hidden = block !== quizPlay;
  shownBlock = key;
  if (swap) {
    block.classList.remove('is-swap');
    void block.offsetWidth;   // без этого анимация не началась бы заново
    block.classList.add('is-swap');
  }
  return swap;
}

/* Холл игры: ведущий запустил квиз, а вопроса ещё нет. Зал читает название
   и сканирует QR, а ведущий в это время спокойно дописывает вопросы. */
function renderLobby(f) {
  stopQuizTicker();
  stopBoardScroll();
  setQuizLayer(true);
  const fresh = showQuizBlock(quizLobby, 'lobby');
  lobbyTitle.textContent = f.title || 'Квиз';
  lobbySub.textContent = f.subtitle || '';
  lobbySub.hidden = !f.subtitle;
  if (fresh) {
    // адрес QR отдаёт сервер, ключ экрана подставляет экран: картинка
    // генерируется на лету и не лежит в кэше
    lobbyQr.src = withKey(ev(f.qr || '/api/qr.svg?kind=quiz'));
  }
  lobbyHint.textContent = f.participants
    ? `Отсканируйте код — уже с нами: ${f.participants}`
    : 'Отсканируйте код — первый вопрос вот-вот начнётся';
}

function renderBoard(f) {
  stopQuizTicker();
  setQuizLayer(true);
  showQuizBlock(quizPlay, 'board');
  quizTag.textContent = 'Квиз';
  quizFree.hidden = true;

  // новый кадр прилетает с каждым ответом в зале, поэтому перематывать
  // страницу назад нельзя — при активном зале нулевая страница никогда бы
  // не сменилась. Таймер и страницу сохраняем, пока режим тот же и
  // количество страниц не изменилось
  const final = !!f.final;
  const keepScroll = !final && boardTimer !== null && boardFinal === false
    && Math.ceil((f.board || []).length / BOARD_PAGE) === boardPages.length;
  if (!keepScroll) stopBoardScroll();
  boardFinal = final;
  const all = f.board || [];
  boardTotal = all.length;

  if (boardFinal) {
    // финал после последнего вопроса: десятка целиком, без движения
    quizQText.textContent = 'Финальная таблица';
    boardPages = [all.slice(0, FINAL_TOP)];
    paintBoard();
    return;
  }

  quizQText.textContent = 'Таблица лидеров';
  boardPages = [];
  for (let i = 0; i < all.length; i += BOARD_PAGE) {
    boardPages.push(all.slice(i, i + BOARD_PAGE));
  }
  paintBoard();
  if (!keepScroll && boardPages.length > 1) {
    // пачка висит столько, сколько её успевают прочитать с дальних рядов
    boardTimer = setInterval(() => { boardPage += 1; paintBoard(); }, BOARD_STEP_MS);
  }
}

function renderQuiz() {
  const f = quizFrame;
  if (!f) {
    setQuizLayer(false);
    return;
  }

  if (f.phase === 'lobby') { renderLobby(f); return; }
  if (f.phase === 'board') { renderBoard(f); return; }

  const q = f.question;
  if (!q) {
    setQuizLayer(false);   // фаза есть, вопроса нет — ждём
    return;
  }
  setQuizLayer(true);
  stopBoardScroll();
  showQuizBlock(quizPlay, `q${q.id}`);
  quizTag.textContent = f.questions_total
    ? `Вопрос ${f.question_no} из ${f.questions_total}` : `Вопрос ${f.question_no}`;
  quizQText.textContent = q.text;
  quizFree.hidden = !!q.scored;

  if (f.phase === 'open') {
    if (f.seconds_left != null) {
      quizDeadline = Date.now() + f.seconds_left * 1000;
      if (!quizTicker) quizTicker = setInterval(tickQuizTimer, 250);
      tickQuizTimer();
    } else {
      stopQuizTicker();
    }
    quizOpts.replaceChildren(...q.options.map((text) => {
      const el = document.createElement('div');
      el.className = 'quiz-s-opt';
      el.textContent = text;
      return el;
    }));
    quizCount.textContent = f.answered_total
      ? `Уже ответили: ${f.answered_total}` : 'Ждём ответы зала…';
    return;
  }

  // closed — распределение, правильного ещё нет; reveal — уже с ним.
  // Что класть в кадр, решает сервер: экран не знает верный ответ заранее
  stopQuizTicker();
  const counts = f.counts || q.options.map(() => 0);
  const total = counts.reduce((a, b) => a + b, 0) || 1;
  quizOpts.replaceChildren(...q.options.map((text, i) => {
    const share = Math.round((counts[i] / total) * 100);
    const el = document.createElement('div');
    el.className = 'quiz-s-opt is-bar' + (f.correct === i ? ' is-right' : '');
    el.innerHTML = `<span class="quiz-s-fill" style="width:${share}%"></span>
      <span class="quiz-s-text">${escapeHTML(text)}${f.correct === i ? ' ✓' : ''}</span>
      <span class="quiz-s-num">${counts[i]} · ${share}%</span>`;
    return el;
  }));
  quizCount.textContent = f.phase === 'closed'
    ? `Ответили: ${f.answered_total ?? total} · приём закрыт`
    : `Ответили: ${f.answered_total ?? total}`;
}

const messages = new Map();   // id -> сообщение ленты
const gallery = new Map();    // id -> снимок из галереи слайдшоу
let order = [];               // id в порядке показа
// Что сейчас на экране: 'm<id>' — сообщение ленты, 'g<id>' — снимок галереи.
// Префикс обязателен: обе таблицы нумеруются с единицы, и без него слайд
// галереи «1» считался бы уже показанным сообщением ленты «1».
let shownKey = null;
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
/** Рисует слайд по данным. Форма одна и та же у сообщения ленты и у
    снимка галереи: id, image, body, author. */
function paintData(m) {

  if (m.image) {
    const url = withKey(ev(`/media/${encodeURIComponent(m.image)}`));
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

/** Подпись того, что на экране: источник, id и содержимое.

    Только id не годится: правка текста не меняет номер снимка, а экран
    должен показать новый текст сразу, не дожидаясь следующего слайда.
    Сравнивать приходится и содержимое. */
function signature(prefix, item) {
  return prefix + item.id + '|' + (item.image || '') + '|'
    + (item.body || '') + '|' + (item.author || '');
}

/** Тот же самый слайд, изменилось только содержимое — перерисовываем
    на месте, без анимации смены кадра: кадр ведь не менялся. */
function repaintSame(prefix, item) {
  shownKey = signature(prefix, item);
  paintData(item);
}

/** Снимок из галереи: рисуем сразу, без задержки переключения ленты. */
function renderItem(item) {
  shownKey = signature('g', item);
  paintData(item);
}

function paint(id) {
  const m = messages.get(id);
  if (!m) return;
  paintData(m);
}

function render(id) {
  if (!messages.has(id)) return;

  const token = ++renderToken;
  slide.classList.add('swap');
  shownKey = signature('m', messages.get(id));
  clearTimeout(swapTimer);
  swapTimer = setTimeout(() => {
    if (token !== renderToken) return;   // слайд успели переключить — не вмешиваемся
    paint(id);
    slide.classList.remove('swap');
  }, 300);
}

/** Предзагрузка следующей пары слайдов, чтобы переключение было мгновенным. */
function preloadNext() {
  if (screenState.slideshow_visible) return;
  if (!order.length) return;
  const idx = order.indexOf(screenState.current_id ?? order[0]);
  const nextIds = [order[(idx + 1) % order.length], order[(idx + 2) % order.length]];
  for (const id of nextIds) {
    const m = messages.get(id);
    if (m && m.image) {
      const img = new Image();
      img.src = withKey(ev(`/media/${encodeURIComponent(m.image)}`));
    }
  }
}

/** 4.1: при обрыве канала не чистим кадр — крутим последний снимок из
    localStorage, пока связь не восстановится. */
function saveSnapshot() {
  try {
    const id = screenState.current_id ?? (order.length ? order[0] : null);
    if (id != null) localStorage.setItem('lf_last_slide', JSON.stringify({ id, at: Date.now() }));
  } catch (_) {}
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
    const sig = signature('g', item);
    if (sig === shownKey) return;
    if (shownKey && shownKey.slice(0, shownKey.indexOf('|')) === 'g' + item.id) {
      repaintSame('g', item);
    } else {
      renderItem(item);
    }
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
  const sig = signature('m', messages.get(id));
  if (sig === shownKey) return;
  if (shownKey && shownKey.slice(0, shownKey.indexOf('|')) === 'm' + id) {
    repaintSame('m', messages.get(id));
  } else {
    render(id);
  }
}

connectWS(withKey(ev('/ws/display')), {
  onMessage(msg) {
    switch (msg.type) {
      case 'init':
        // init приходит и при переподключении — старые слайды надо забыть
        messages.clear();
        gallery.clear();
        setSplashUrl(msg.splash);
        (msg.messages || []).forEach((m) => messages.set(m.id, m));
        (msg.gallery || []).forEach((g) => gallery.set(g.id, g));
        order = byPosition([...messages.keys()]);
        screenState = msg.screen || screenState;
        shownKey = null;
        // 5.0: экран, открытый посреди игры (F5, второй проектор), получает
        // текущую стадию прямо в init — без «покажи заново» у ведущего
        quizFrame = msg.quiz || null;
        renderQuiz();
        showCurrent(); preloadNext(); saveSnapshot();
        break;

      case 'new': {
        // дельта: пришли только новые сообщения, старые не трогаем
        (msg.added || []).forEach((m) => messages.set(m.id, m));
        if (msg.splash !== undefined) setSplashUrl(msg.splash);
        order = byPosition([...messages.keys()]);
        screenState = msg.screen || screenState;
        showCurrent(); preloadNext(); saveSnapshot();
        break;
      }

      case 'refresh': {
        const fresh = new Set();
        (msg.messages || []).forEach((m) => { messages.set(m.id, m); fresh.add(m.id); });
        for (const id of [...messages.keys()]) if (!fresh.has(id)) messages.delete(id);
        setSplashUrl(msg.splash);
        // галерея приходит целиком: снимок мог уйти из ленты и остаться здесь
        gallery.clear();
        (msg.gallery || []).forEach((g) => gallery.set(g.id, g));
        order = byPosition([...messages.keys()]);
        screenState = msg.screen || screenState;
        showCurrent(); preloadNext(); saveSnapshot();
        break;
      }

      case 'quiz':
        // 5.0: кадр игры (смена стадии, новый вопрос, ответы зала).
        // Лента под слоем продолжает стоять — сервер её и так приостановил
        quizFrame = msg.quiz || null;
        renderQuiz();
        break;

      case 'screen':
        // заставку ведущий мог сменить или убрать, пока экран был открыт
        if ('splash' in msg) setSplashUrl(msg.splash);
        screenState = msg.screen || screenState;
        showCurrent(); preloadNext(); saveSnapshot();
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