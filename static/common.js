/* Общие мелочи: запросы к API, WebSocket с переподключением, форматирование */

/**
 * Адрес внутри события. Страница лежит по /e/<адрес>/..., поэтому запросы
 * к API, WebSocket и фотографиям собираются с этим префиксом.
 * Префикс проставляет сервер в window.LF при отдаче страницы.
 */
function ev(path) {
  return ((window.LF && window.LF.base) || '') + path;
}

async function api(url, options = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...options });
  let data = null;
  try { data = await res.json(); } catch (_) { /* пустой ответ */ }
  if (!res.ok) {
    const msg = (data && data.detail) || `Ошибка ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function apiForm(url, fields) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v ?? '');
  return api(url, { method: 'POST', body: fd });
}

function postForm(url, fields) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v ?? '');
  return api(url, { method: 'POST', body: fd });
}

function postJSON(url, fields) {
  return api(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}

/** WebSocket с автопереподключением и колбэком состояния соединения. */
function connectWS(path, { onMessage, onStatus } = {}) {
  let ws = null;
  let closed = false;
  let retry = 0;
  let timer = null;

  function open() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}${path}`);

    ws.onopen = () => {
      retry = 0;
      onStatus?.('online');
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === 'ping') { ws.send('pong'); return; }
      onMessage?.(msg);
    };
    ws.onclose = () => {
      onStatus?.('offline');
      if (closed) return;
      // 4401 — нет сессии отправителя, 4403 — нет прав модератора
      if (ws && (ws.closeCode === 4401 || ws.closeCode === 4403)) {
        onMessage?.({ type: 'unauthorized' });
        return;
      }
      retry = Math.min(retry + 1, 6);
      timer = setTimeout(open, 400 * Math.pow(1.7, retry));
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }

  open();
  window.addEventListener('beforeunload', () => {
    closed = true;
    clearTimeout(timer);
    try { ws?.close(); } catch (_) {}
  });

  return {
    send(data) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data)); },
    close() { closed = true; clearTimeout(timer); try { ws?.close(); } catch (_) {} },
  };
}

function setPill(el, state, text) {
  if (!el) return;
  el.dataset.state = state;
  el.textContent = text ?? state;
}

function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0] || '').join('').toUpperCase() || '?';
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function fmtAgo(ts) {
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return 'только что';
  const m = Math.floor(diff / 60);
  if (m < 60) return `${m} ${plural(m, 'минуту', 'минуты', 'минут')} назад`;
  const h = Math.floor(m / 60);
  return `${h} ${plural(h, 'час', 'часа', 'часов')} назад`;
}

function showToast(text, kind = 'ok', ms = 4000) {
  let box = document.getElementById('toastBox');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toastBox';
    box.style.cssText =
      'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:100;' +
      'display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.textContent = text;
  el.style.cssText =
    'padding:12px 20px;border-radius:12px;font:inherit;font-weight:600;font-size:.95rem;' +
    'box-shadow:0 10px 40px rgba(0,0,0,.5);max-width:90vw;text-align:center;' +
    (kind === 'ok'
      ? 'background:#33c98a;color:#04231a'
      : kind === 'warn'
        ? 'background:#ffb648;color:#1a1205'
        : 'background:#ff6b7a;color:#2b0409');
  box.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, ms);
}

/**
 * Копирование текста. Буфер обмена браузера доступен только в защищённом
 * контексте, а сервер часто живёт по http и по адресу — тогда он недоступен.
 * Поэтому сначала пробуем его, а если не вышло, копируем через скрытое
 * поле: работает везде, включая http.
 */
async function copy(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    // пробуем запасной путь
  }
  return copyFallback(text);
}

function copyFallback(text) {
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(field);
  field.select();
  field.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (_) {
    ok = false;
  }
  document.body.removeChild(field);
  if (!ok) {
    // последний способ: показать текст, чтобы человек скопировал сам
    showToast('Скопируйте вручную: ' + text, 'warn', 12000);
  }
  return ok;
}

/**
 * Проставляет адреса ссылкам, которые в разметке помечены id.
 * В разметке стоит href="#", потому что префикс события известен только
 * после загрузки страницы.
 */
function wireEventLinks() {
  const links = {
    toSend: '/send',
    toSendBig: '/send',
    toScreen: '/screen',
  };
  // Ссылки на витрину события убраны из интерфейса: в кабинете ведущего
  // она ничего не давала, а зрителю на странице отправки уводила от
  // отправки. Сама страница по адресу открывается — если её и оттуда
  // закрывать надо, это отдельное решение.
  for (const [id, path] of Object.entries(links)) {
    const el = document.getElementById(id);
    if (!el) continue;
    let href = ev(path);
    // 4.1: экран открывается только с ключом. Страница получает его в
    // window.LF.screenKey, и кнопка «Экран» обязана отнести его с собой.
    if (path === '/screen' && window.LF && window.LF.screenKey) {
      href += '?d=' + encodeURIComponent(window.LF.screenKey);
    }
    el.href = href;
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', wireEventLinks);
} else {
  wireEventLinks();
}