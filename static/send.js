/* Отправка: текст + фото, предпросмотр, статус своих заявок */

const form = document.getElementById('sendForm');
const bodyInput = document.getElementById('bodyInput');
const charCount = document.getElementById('charCount');
const fileInput = document.getElementById('fileInput');
const dropzone = document.getElementById('dropzone');
const preview = document.getElementById('preview');
const dropHint = document.getElementById('dropHint');
const removePhotoBtn = document.getElementById('removePhoto');
const sendBtn = document.getElementById('sendBtn');
const sendError = document.getElementById('sendError');
const sendOk = document.getElementById('sendOk');
const myList = document.getElementById('myList');
const charLimit = document.getElementById('charLimit');
const bodyHint = document.getElementById('bodyHint');

let pickedFile = null;
let knownIds = new Set();

/* Лимит берём с сервера: он там один на всё приложение, а здесь число
   продублировано было бы в разных местах и рано или поздно разошлось бы
   с настоящим ограничением. */
let maxLen = parseInt(bodyInput.getAttribute('maxlength'), 10) || 56;

api(ev('/api/config')).then((cfg) => {
  if (Number.isInteger(cfg.max_text_len) && cfg.max_text_len > 0) {
    maxLen = cfg.max_text_len;
  }
  bodyInput.maxLength = maxLen;
  charLimit.textContent = maxLen;
  updateCounter();
}).catch(() => { /* конфиг не пришёл — работаем со значением из разметки */ });

function updateCounter() {
  const left = maxLen - bodyInput.value.length;
  charCount.textContent = bodyInput.value.length;
  const counter = charCount.parentElement;
  // предупреждаем заранее: цветом, а не только цифрой
  counter.classList.toggle('near-limit', left <= 10 && left >= 0);
  counter.classList.toggle('over-limit', left < 0);
  bodyHint.textContent = left < 0
    ? `Ещё ${-left} симв. лишних — сократите.`
    : (left <= 10
      ? `Осталось ${left} симв.`
      : 'Коротко: с дальнего ряда читается только короткая фраза.');
}

bodyInput.addEventListener('input', updateCounter);

function setFile(file) {
  if (!file) return;
  if (!file.type.startsWith('image/')) {
    sendError.textContent = 'Это не картинка. Выберите фото.';
    sendError.hidden = false;
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    sendError.textContent = 'Файл больше 15 МБ. Уменьшите фото или выберите другое.';
    sendError.hidden = false;
    return;
  }
  pickedFile = file;
  const url = URL.createObjectURL(file);
  preview.src = url;
  preview.hidden = false;
  dropHint.hidden = true;
  removePhotoBtn.hidden = false;
  sendError.hidden = true;
}

fileInput.addEventListener('change', () => setFile(fileInput.files[0]));

['dragenter', 'dragover'].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove('over'); }));

dropzone.addEventListener('drop', (e) => {
  const file = e.dataTransfer?.files?.[0];
  if (file) setFile(file);
});

removePhotoBtn.addEventListener('click', () => {
  pickedFile = null;
  fileInput.value = '';
  preview.hidden = true;
  preview.removeAttribute('src');
  dropHint.hidden = false;
  removePhotoBtn.hidden = true;
});

function renderMine(messages) {
  messages = messages || [];
  if (!messages.length) {
    myList.innerHTML = '<p class="muted">Пока пусто.</p>';
    return;
  }
  myList.replaceChildren(...messages.map((m) => {
    const el = document.createElement('div');
    el.className = 'my-item' + (knownIds.has(m.id) ? '' : ' is-new');
    const photo = m.image
      ? `<img src="${ev(`/media/${encodeURIComponent(m.image)}`)}" alt="" loading="lazy">`
      : '';
    const text = m.body ? escapeHTML(m.body) : (m.image ? '📷 Фото' : '');
    // модерации нет: сообщение уходит в эфир сразу, статус один
    const state = m.shown_at ? 'status approved' : 'status new';
    const label = m.shown_at ? 'Показано' : 'В эфире';
    el.innerHTML = `${photo}
      <span class="my-text" title="${escapeHTML(m.body || 'фото')}">${text}</span>
      <span class="my-meta">
        <span class="${state}">${label}</span>
        <span class="muted">${fmtAgo(m.created_at)}</span>
      </span>`;
    return el;
  }));
  messages.forEach((m) => knownIds.add(m.id));
}

async function loadMe() {
  try {
    const me = await api(ev('/api/me'));
    if (!me.joined) { location.href = '/join'; return; }
    document.getElementById('whoami').textContent = me.name;
    renderMine(me.messages);
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  sendError.hidden = true;
  sendOk.hidden = true;

  if (!bodyInput.value.trim() && !pickedFile) {
    sendError.textContent = 'Напишите текст или добавьте фото.';
    sendError.hidden = false;
    return;
  }

  const fd = new FormData();
  fd.append('body', bodyInput.value.trim());
  if (pickedFile) fd.append('image', pickedFile, pickedFile.name || 'photo.jpg');

  sendBtn.disabled = true;
  sendBtn.textContent = pickedFile ? 'Загружаем и сжимаем…' : 'Отправляем…';

  try {
    await api(ev('/api/messages'), { method: 'POST', body: fd });
    form.reset();
    charCount.textContent = '0';
    pickedFile = null;
    preview.hidden = true;
    preview.removeAttribute('src');
    dropHint.hidden = false;
    removePhotoBtn.hidden = true;
    sendOk.textContent = 'Отправлено! Уже на экране.';
    sendOk.hidden = false;
    loadMe();
  } catch (ex) {
    sendError.textContent = ex.message || 'Не получилось отправить. Попробуйте ещё раз.';
    sendError.hidden = false;
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = 'Отправить';
  }
});

/* Статус своих заявок обновляется в реальном времени */
connectWS(ev('/ws/participant'), {
  onMessage(msg) {
    if (msg.type === 'refresh' || msg.type === 'init') loadMe();
  },
  onStatus(state) {
    const pill = document.getElementById('whoami');
    pill.title = state === 'online' ? 'Статус обновляется в реальном времени' : 'Связь потеряна';
  },
});

loadMe();