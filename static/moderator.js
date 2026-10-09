/* Управление показом: кнопки, ползунок, список пришедшего.
   Модерации нет — всё, что приходит, сразу попадает в эфир. */

const onairBox = document.getElementById('onair');
const onairEmpty = document.getElementById('onairEmpty');
const nowThumb = document.getElementById('nowThumb');
const nowText = document.getElementById('nowText');
const nowMeta = document.getElementById('nowMeta');
const playBtn = document.getElementById('playBtn');
const playGlyph = document.getElementById('playGlyph');
const playLabel = document.getElementById('playLabel');
const intervalRange = document.getElementById('intervalRange');
const intervalVal = document.getElementById('intervalVal');
const qrBtn = document.getElementById('qrBtn');
const splashBtn = document.getElementById('splashBtn');
const modSub = document.getElementById('modSub');

// столько символов помещается на экран; уточняем с сервера при загрузке
let MAX_TEXT = 56;

const state = {
  messages: [],   // id -> сообщение
  order: [],      // id в порядке показа
  screen: {},
  online: {},
  // 4.0: имена файлов, которые лежат в галерее слайдшоу. Отдельный набор,
  // а не флаг у сообщения: удаление из ленты не должно выбрасывать снимок
  // из галереи, и наоборот
  galleryImages: new Set(),
  gallery: [],    // снимки галереи в порядке показа
};

const byId = (id) => state.messages.find((m) => m.id === id);

/* ---------------- Список ---------------- */
function renderList() {
  const shownCount = state.order.filter((id) => byId(id)?.shown_at).length;

  onairEmpty.hidden = state.order.length > 0;
  onairBox.replaceChildren(...state.order.map((id, i) => {
    const m = byId(id);
    if (!m) return document.createElement('span');
    const el = document.createElement('div');
    const isCurrent = id === state.screen.current_id;
    el.className = 'onair-row'
      + (m.shown_at ? ' is-shown' : ' is-new')
      + (isCurrent ? ' is-current' : '');
    el.dataset.id = id;

    const photo = m.image
      ? `<img src="${ev(`/media/${encodeURIComponent(m.image)}`)}" alt="" loading="lazy">`
      : '';
    const text = m.body
      ? `<span class="or-text" title="${escapeHTML(m.body)}">${escapeHTML(m.body)}</span>`
      : '<span class="or-text muted">📷 фото</span>';
    const badge = m.shown_at
      ? '<span class="or-badge or-badge-shown">показано</span>'
      : '<span class="or-badge or-badge-new">новое</span>';

    // Галочка есть только у фотографий: в слайдшоу идут фотографии и
    // фотографии с текстом, а текст без картинки туда попасть не может
    const inGallery = state.galleryImages.has(m.image || '');
    const check = m.image
      ? `<label class="or-check" title="${inGallery
          ? 'Убрать из слайдшоу' : 'Добавить в слайдшоу'}">
           <input type="checkbox" data-act="slideshow" data-id="${id}"
                  ${inGallery ? 'checked' : ''} aria-label="В слайдшоу">
         </label>`
      : '';

    el.innerHTML = `<span class="or-pos">${i + 1}</span>${check}${photo}${text}
      <span class="or-author">${escapeHTML(m.author)}</span>
      ${badge}
      <span class="or-actions">
        <button class="icon-btn" data-act="up" data-id="${id}" title="Выше">↑</button>
        <button class="icon-btn" data-act="down" data-id="${id}" title="Ниже">↓</button>
        <button class="icon-btn" data-act="show" data-id="${id}" title="Показать сейчас">▶</button>
        <button class="icon-btn" data-act="edit" data-id="${id}" title="Править текст">✎</button>
        <button class="icon-btn danger" data-act="delete" data-id="${id}" title="Удалить">✕</button>
      </span>`;
    return el;
  }));

  modSub.textContent = shownCount === state.order.length && state.order.length
    ? `все ${state.order.length} показаны, идёт новый круг`
    : `показано ${shownCount} из ${state.order.length}`;
}

/* ---------------- Полоса «сейчас на экране» ---------------- */
function renderNow() {
  // Пока идёт слайдшоу, полоса показывает снимок из галереи, а не сообщение
  // ленты: «сейчас на экране» должно значить то, что на экране и есть.
  const inShow = !!state.screen.slideshow_visible;
  const item = inShow
    ? (state.gallery || []).find((g) => g.id === state.screen.slideshow_current_id)
    : null;
  const m = item || byId(state.screen.current_id);
  if (!m) {
    nowText.textContent = '—';
    nowMeta.textContent = inShow
      ? 'Слайдшоу выключено, на экране лента'
      : 'Слайд 0 из 0';
    nowThumb.hidden = true;
    return;
  }
  nowText.textContent = m.body || '📷 фото';
  if (m.image) {
    nowThumb.hidden = false;
    nowThumb.innerHTML = `<img src="${ev(`/media/${encodeURIComponent(m.image)}`)}" alt="">`;
  } else {
    nowThumb.hidden = true;
    nowThumb.replaceChildren();
  }
  if (inShow) {
    const pos = (state.screen.slideshow_index || 0) + 1;
    nowMeta.textContent =
      `Галерея: снимок ${pos} из ${(state.gallery || []).length} · ${m.author || 'без подписи'}`;
    return;
  }
  const idx = state.order.indexOf(m.id);
  nowMeta.textContent = `Слайд ${idx + 1} из ${state.order.length} · ${m.author}`;
}

/* ---------------- Кнопки управления ---------------- */
function renderControls() {
  const playing = !!state.screen.playing;
  const qr = !!state.screen.qr_visible;

  playLabel.textContent = playing ? 'Пауза' : 'Продолжить';
  playGlyph.textContent = playing ? '❚❚' : '▶';
  playBtn.classList.toggle('is-paused', !playing);

  qrBtn.classList.toggle('active', qr);
  qrBtn.textContent = qr ? 'Скрыть QR' : 'QR на экране';

  const splash = !!state.screen.splash_visible;
  splashBtn.classList.toggle('active', splash);
  splashBtn.textContent = splash ? 'Убрать заставку' : 'Заставка';

  intervalRange.value = state.screen.interval || 12;
  intervalVal.textContent = state.screen.interval || 12;
}

function renderAll() {
  renderList();
  renderNow();
  renderControls();
  // 4.0: галерея слайдшоу живёт в отдельном файле и подключается скриптом
  // ниже. Проверяем наличие: страница может быть открыта со старой разметкой,
  // и тогда вкладки просто не будет — падать из-за этого нельзя.
  if (typeof renderGallery === 'function') {
    renderGallery();
    renderSlideshowBar();
  }
}

function applyState(payload) {
  if (!payload) return;
  // Заставка приезжает в состоянии показа. Раньше она обновлялась только
  // после загрузки или удаления в этой же вкладке: вышел ведущий из сессии,
  // вошёл снова — панель писала «заставка не загружена», хотя на экране
  // висела картинка. Теперь панель показывает ровно то, что на экране.
  if ('splash' in payload) renderSplash(payload.splash);
  if (payload.messages) {
    state.messages = payload.messages;
    // порядок показа: позиция возрастает
    state.order = payload.messages
      .slice()
      .sort((a, b) => a.position - b.position)
      .map((m) => m.id);
  }
  if (payload.gallery) {
    state.gallery = payload.gallery;
    state.galleryImages = new Set(payload.gallery.map((g) => g.image));
  }
  if (payload.screen) state.screen = payload.screen;
  if (payload.online) state.online = payload.online;
  renderAll();
}

function applyStateFrom(promise) {
  Promise.resolve(promise).then(applyState).catch(() => {});
}

async function control(action, value = '') {
  try {
    const res = await postForm(ev('/api/moderator/screen'), { action, value });
    state.screen = res.screen;
    renderAll();
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
}

playBtn.addEventListener('click', () => control(state.screen.playing ? 'pause' : 'play'));
document.getElementById('nextBtn').addEventListener('click', () => control('next'));
document.getElementById('prevBtn').addEventListener('click', () => control('prev'));
document.getElementById('resetBtn').addEventListener('click', async () => {
  if (!confirm('Начать показ заново?\n\nОтметки «показано» сбросятся, список не изменится.')) return;
  await control('reset');
  try { await postForm(ev('/api/moderator/reset-shown'), {}); } catch (_) {}
});

qrBtn.addEventListener('click', () => control('qr', state.screen.qr_visible ? '0' : '1'));
splashBtn.addEventListener('click', () =>
  control('splash', state.screen.splash_visible ? '0' : '1'));

/* ---------------- Заставка ---------------- */
const splashFile = document.getElementById('splashFile');
const splashThumb = document.getElementById('splashThumb');
const splashNone = document.getElementById('splashNone');
const splashClear = document.getElementById('splashClear');
const splashStatus = document.getElementById('splashStatus');

/** Показывает заставку события такой, какая она сейчас на сервере.

    Сравниваем с тем, что уже на экране: панель опрашивает состояние каждые
    15 секунд, и без сравнения картинка мигала бы заново при каждом опросе.
    */
function renderSplash(url) {
  if (splashThumb.getAttribute('data-url') === (url || '')) return;
  splashThumb.dataset.url = url || '';
  splashThumb.hidden = !url;
  splashNone.hidden = !!url;
  splashClear.hidden = !url;
  if (url) splashThumb.src = url;
  else splashThumb.removeAttribute('src');
}

document.getElementById('splashUpload').addEventListener('click', () => splashFile.click());

splashFile.addEventListener('change', async () => {
  const file = splashFile.files[0];
  if (!file) return;
  splashStatus.textContent = 'Загружаю…';
  const body = new FormData();
  body.append('image', file);
  try {
    const res = await api(ev('/api/moderator/splash'), { method: 'POST', body });
    renderSplash(res.splash);
    splashStatus.textContent = file.size > 4 * 1024 * 1024
      ? 'Картинка уменьшена, чтобы не тормозить показ'
      : 'Заставка готова';
  } catch (ex) {
    splashStatus.textContent = '';
    showToast(ex.message, 'warn');
  } finally {
    splashFile.value = '';
  }
});

splashClear.addEventListener('click', async () => {
  if (!confirm('Убрать заставку?\n\nНа экране будет просто название события.')) return;
  try {
    await postForm(ev('/api/moderator/splash'), { remove: '1' });
    renderSplash(null);
    splashStatus.textContent = 'Заставка убрана';
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
});

let intervalDebounce = null;
intervalRange.addEventListener('input', () => { intervalVal.textContent = intervalRange.value; });
intervalRange.addEventListener('change', () => {
  clearTimeout(intervalDebounce);
  intervalDebounce = setTimeout(() => control('interval', intervalRange.value), 150);
});

/* ---------------- Действия со списком ---------------- */
async function move(id, direction) {
  try {
    applyState(await postForm(ev('/api/moderator/move'), { message_id: id, direction }));
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
}

async function remove(id) {
  const m = byId(id);
  const preview = m ? (m.body || 'фото').slice(0, 80) : 'сообщение';
  if (!confirm(`Удалить навсегда?\n\n«${preview}»`)) return;
  try {
    applyState(await postForm(ev('/api/moderator/delete'), { message_id: id }));
    showToast('Удалено', 'ok', 2200);
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
}

/* ---------------- Правка текста ----------------
   Раньше стояли два системных prompt() подряд — сначала текст, потом имя
   автора. Имя менять не нужно, и системные окна на телефоне выглядят
   чужеродно. Теперь одно своё окно со счётчиком символов.

   Поддерживаются два вида правки: сообщение ленты и снимок галереи. Разница
   только в адресе запроса и в подсказке. */
const editModal = document.getElementById('editModal');
const editInput = document.getElementById('editInput');
const editCount = document.getElementById('editCount');
const editHint = document.getElementById('editHint');
const editSave = document.getElementById('editSave');
const editTitle = document.getElementById('editTitle');
const modalCount = editCount.parentElement;

let editTarget = null;   // {kind: 'message'|'gallery', id, limit}

function openEdit(kind, id, current, hint) {
  editTarget = { kind, id, limit: MAX_TEXT };
  editTitle.textContent = kind === 'gallery'
    ? 'Исправить текст снимка' : 'Исправить текст';
  editHint.textContent = hint || 'Имя автора не меняется.';
  editInput.value = current || '';
  // предел держим один на оба поля — тот, что прислал сервер
  editInput.maxLength = String(MAX_TEXT);
  updateEditCount();
  editModal.hidden = false;
  setTimeout(() => {
    editInput.focus();
    editInput.setSelectionRange(editInput.value.length, editInput.value.length);
  }, 30);
}

function closeEdit() {
  editModal.hidden = true;
  editTarget = null;
}

function updateEditCount() {
  const n = editInput.value.length;
  const limit = (editTarget && editTarget.limit) || MAX_TEXT;
  editCount.textContent = n;
  // подсветка только когда упёрлись: раньше лишнее обрезалось тихо
  modalCount.classList.toggle('is-full', n >= limit);
  editInput.classList.toggle('is-full', n >= limit);
  editSave.disabled = n > limit;
}

async function saveEdit() {
  if (!editTarget) return;
  const body = editInput.value.trim();
  if (body.length > ((editTarget.limit) || MAX_TEXT)) {
    showToast(`Текст длиннее ${MAX_TEXT} символов — на экране не поместится`, 'warn');
    return;
  }
  const { kind, id } = editTarget;
  const path = kind === 'gallery' ? '/api/moderator/gallery/edit' : '/api/moderator/edit';
  const fields = kind === 'gallery' ? { gallery_id: id, body } : { message_id: id, body };
  try {
    applyState(await postForm(ev(path), fields));
    closeEdit();
    showToast('Исправлено', 'ok', 2000);
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
}

editInput.addEventListener('input', updateEditCount);
document.getElementById('editCancel').addEventListener('click', closeEdit);
editSave.addEventListener('click', saveEdit);
editModal.addEventListener('click', (e) => { if (e.target === editModal) closeEdit(); });
document.addEventListener('keydown', (e) => {
  if (editModal.hidden) return;
  if (e.key === 'Escape') { e.preventDefault(); closeEdit(); }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEdit(); }
});

function editText(id) {
  const m = byId(id);
  if (!m) return;
  openEdit('message', id, m.body,
    m.image ? 'Текст под фотографией. Имя автора не меняется.'
            : 'Имя автора не меняется.');
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  // input у галочки: с ним работает отдельный обработчик change, иначе
  // щёлчок по квадратику попал бы ещё и в общий переключатель действий
  if (btn.tagName === 'INPUT') return;
  const id = Number(btn.dataset.id);
  switch (btn.dataset.act) {
    case 'up': move(id, 'up'); break;
    case 'down': move(id, 'down'); break;
    case 'show': control('show', String(id)); break;
    case 'delete': remove(id); break;
    case 'edit': editText(id); break;
  }
});

/* ---------------- Приглашение ---------------- */
function renderInvite(link, code) {
  if (!link) return;
  document.getElementById('qrLink').textContent = link;
  document.getElementById('qrOpen').href = link;
  const src = ev(`/api/qr.svg?v=${encodeURIComponent(code || '')}`);
  document.getElementById('qrImg').src = src;
  document.getElementById('qrDownload').href = src;
}

function loadInvite() {
  api(ev('/api/moderator/invite'))
    .then(({ link, code }) => renderInvite(link, code))
    .catch(() => { document.getElementById('qrLink').textContent = '—'; });
}

document.getElementById('rotateInvite').addEventListener('click', async () => {
  if (!confirm('Сменить код приглашения?\n\nСтарые QR-коды перестанут работать.')) return;
  try {
    const res = await postForm(ev('/api/moderator/rotate-invite'), {});
    loadInvite();
    showToast('Новый код готов', 'ok');
    if (res.code) document.getElementById('qrCode') && (document.getElementById('qrCode').textContent = res.code);
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
});

document.getElementById('logoutBtn').addEventListener('click', async () => {
  try { await postForm(ev('/api/moderator/logout'), {}); } catch (_) {}
  location.href = '/moderator/login';
});

/* ---------------- Клавиатура ---------------- */
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea') || e.metaKey || e.ctrlKey) return;
  const map = {
    ' ': () => (state.screen.playing ? 'pause' : 'play'),
    ArrowRight: 'next',
    ArrowLeft: 'prev',
    q: 'qr',  // Q / Й
    й: 'qr',
  };
  const action = map[e.key];
  if (!action) return;
  e.preventDefault();
  const name = typeof action === 'function' ? action() : action;
  control(name, name === 'qr' ? (state.screen.qr_visible ? '0' : '1') : '');
});

/* ---------------- Приём данных ---------------- */
connectWS(ev('/ws/moderator'), {
  onStatus(st) {
    setPill(document.getElementById('connPill'), st,
      st === 'online' ? 'на связи' : st === 'offline' ? 'нет связи' : 'соединяемся…');
    if (st !== 'offline') applyStateFrom(api(ev('/api/moderator/state')));
  },
  onMessage(msg) {
    switch (msg.type) {
      case 'init':
        applyState(msg);
        if (msg.invite) renderInvite(msg.invite, null);
        break;
      case 'new':
        // новое сообщение пришло — оно сразу в эфире и в списке
        applyState(msg);
        showToast(`Новое от ${msg.message.author}`, 'ok', 2200);
        break;
      case 'refresh':
        applyState(msg);
        break;
      case 'screen':
        // заставку ведущий мог сменить или убрать, пока эта вкладка открыта.
        // Через applyState, а не присваиванием: иначе вторая вкладка кабинета
        // продолжала бы показывать ту заставку, которой уже нет.
        applyState(msg);
        break;
      case 'shown': {
        const m = byId(msg.id);
        if (m) m.shown_at = msg.shown_at || Date.now() / 1000;
        renderAll();
        break;
      }
      case 'unshown':
        // начался новый круг: снимаем отметки «показано»
        state.messages.forEach((m) => { m.shown_at = null; });
        renderAll();
        break;
      case 'unauthorized':
        location.href = '/moderator/login';
        break;
      default:
        applyStateFrom(api(ev('/api/moderator/state')));
    }
  },
});

// лимит текста уточняем у сервера: он там один на всё приложение
api(ev('/api/config')).then((cfg) => {
  if (Number.isInteger(cfg.max_text_len) && cfg.max_text_len > 0) {
    MAX_TEXT = cfg.max_text_len;
  }
}).catch(() => { /* остаёмся на значении по умолчанию */ });

applyStateFrom(api(ev('/api/moderator/state')));
loadInvite();
setInterval(() => applyStateFrom(api(ev('/api/moderator/state'))), 15000);
