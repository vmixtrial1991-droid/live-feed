/* Витрина: всё, что пришло, попадает сюда сразу — без ожидания проверки */

const grid = document.getElementById('grid');
const emptyBox = document.getElementById('empty');
const countPill = document.getElementById('countApproved');
const ticker = document.getElementById('ticker');
const tickerTrack = document.getElementById('tickerTrack');

const cards = new Map();   // id -> элемент
let currentId = null;
let currentIdFromScreen = false;

/* 4.1: живой эфир витрины приходит по ws/display, а он открывается только
   с ключом экрана. Сервер отдаёт ключ странице так же, как странице экрана. */
const screenKey = (window.LF && window.LF.screenKey) || '';
function withKey(url) {
  if (!screenKey) return url;
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + 'd=' + encodeURIComponent(screenKey);
}

/* В пустой состоянии кнопка ведёт на страницу входа — подставляем реальный код. */
api(ev('/api/config')).then((cfg) => {
  document.getElementById('eventTitle').textContent = cfg.event_title || 'Живая лента';
  document.getElementById('eventSubtitle').textContent = cfg.event_subtitle || '';
});

function cardHTML(m) {
  const photo = m.image
    ? `<img src="${ev(`/media/${encodeURIComponent(m.image)}`)}" alt="Фото от ${escapeHTML(m.author)}" loading="lazy">`
    : '';
  const text = m.body ? `<div class="item-text">${escapeHTML(m.body)}</div>` : '';
  return `${photo}<div class="item-body">${text}
    <div class="item-author">
      <span class="avatar">${escapeHTML(initials(m.author))}</span>
      <span>${escapeHTML(m.author)}</span>
      <span class="time">${fmtTime(m.created_at)}</span>
    </div></div>`;
}

function addCard(m, where = 'prepend') {
  if (cards.has(m.id)) return;
  const el = document.createElement('article');
  el.className = 'card-item';
  el.dataset.id = m.id;
  el.innerHTML = cardHTML(m);
  if (where === 'append') grid.appendChild(el);
  else grid.prepend(el);
  cards.set(m.id, el);
  if (emptyBox) emptyBox.hidden = true;
  markCurrent();
}

function removeCard(id) {
  const el = cards.get(id);
  if (!el) return;
  el.style.transition = 'opacity .25s, transform .25s';
  el.style.opacity = '0';
  el.style.transform = 'scale(.96)';
  setTimeout(() => el.remove(), 250);
  cards.delete(id);
}

function renderAll(messages) {
  cards.forEach((el) => el.remove());
  cards.clear();
  // сервер отдаёт от новых к старым (position DESC) — добавляем в конец,
  // чтобы порядок на экране совпадал с порядком в ответе
  messages.forEach((m) => addCard(m, 'append'));
  updateCount(messages.length);
  if (emptyBox) emptyBox.hidden = messages.length > 0;
}

function updateCount(n) {
  countPill.textContent = n;
}

function markCurrent() {
  cards.forEach((el, id) => el.classList.toggle('is-current', id === currentId));
}

function renderTicker(messages) {
  const texts = messages
    .filter((m) => m.body)
    .slice(0, 12)
    .map((m) => `<span>${escapeHTML(m.body)} <em class="t-author">— ${escapeHTML(m.author)}</em></span>`);
  if (!texts.length) { ticker.hidden = true; return; }
  ticker.hidden = false;
  // дублируем ряд, чтобы бесконечная прокрутка была бесшовной
  tickerTrack.innerHTML = texts.join('') + texts.join('');
}

connectWS(withKey(ev('/ws/display')), {
  onStatus(state) {
    setPill(document.getElementById('connPill'), state,
      state === 'online' ? 'в эфире' : state === 'offline' ? 'нет связи' : 'соединяемся…');
  },
  onMessage(msg) {
    switch (msg.type) {
      case 'init':
        renderAll(msg.messages || []);
        renderTicker(msg.messages || []);
        if (msg.screen) currentId = msg.screen.current_id;
        markCurrent();
        break;

      case 'new':
      case 'refresh':
        renderAll(msg.messages || []);
        renderTicker(msg.messages || []);
        if (msg.screen) currentId = msg.screen.current_id;
        markCurrent();
        break;

      case 'screen':
        currentId = msg.screen ? msg.screen.current_id : null;
        markCurrent();
        break;

      case 'stats':
        updateCount(cards.size);
        break;

      default:
        resync();
    }
  },
});

/* Сверка с сервером: страхует от пропущенных событий после обрыва связи */
function resync() {
  api(ev('/api/messages'))
    .then((d) => {
      renderAll(d.messages || []);
      renderTicker(d.messages || []);
      if (d.screen) currentId = d.screen.current_id;
      markCurrent();
    })
    .catch(() => {});
}
setInterval(resync, 15000);