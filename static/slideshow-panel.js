/* 4.0: вкладка «Слайдшоу» — галерея фотографий, запуск на экран, архив.
   Всё живёт в уже существующем списке сообщений: галочка — это флаг
   in_slideshow у фотографии, отдельного списка на клиенте не заводим. */

const gallery = document.getElementById('gallery');
const galleryEmpty = document.getElementById('galleryEmpty');
const tabFeed = document.getElementById('tabFeed');
const tabSlideshow = document.getElementById('tabSlideshow');
const pageFeed = document.getElementById('pageFeed');
const pageSlideshow = document.getElementById('pageSlideshow');
const tabCount = document.getElementById('tabSlideshowCount');
const runBtn = document.getElementById('slideshowRunBtn');
const clearBtn = document.getElementById('slideshowClearBtn');
const runState = document.getElementById('slideshowState');
const archiveCount = document.getElementById('archiveCount');
const archiveDownload = document.getElementById('archiveDownload');
const archiveWipe = document.getElementById('archiveWipe');
const archiveStatus = document.getElementById('archiveStatus');
const slideInterval = document.getElementById('slideshowInterval');
const slideIntervalVal = document.getElementById('slideshowIntervalVal');

/* ---------------- Вкладки ---------------- */
function switchTab(which) {
  const feed = which !== 'slideshow';
  tabFeed.classList.toggle('is-active', feed);
  tabSlideshow.classList.toggle('is-active', !feed);
  tabFeed.setAttribute('aria-selected', String(feed));
  tabSlideshow.setAttribute('aria-selected', String(!feed));
  pageFeed.classList.toggle('is-active', feed);
  pageSlideshow.classList.toggle('is-active', !feed);
  if (!feed) loadArchive();
}

tabFeed.addEventListener('click', () => switchTab('feed'));
tabSlideshow.addEventListener('click', () => switchTab('slideshow'));

/* ---------------- Галерея ---------------- */
function inSlideshow() {
  // порядок берём тот же, что у ленты, — так ведущий узнаёт свою свадьбу
  return state.order
    .map((id) => byId(id))
    .filter((m) => m && m.in_slideshow && m.image);
}

function fmtBytes(n) {
  if (!n) return '0 КБ';
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} КБ`;
  return `${(n / 1048576).toFixed(1)} МБ`;
}

function renderGallery() {
  const items = inSlideshow();
  const current = state.screen.slideshow_current_id;

  galleryEmpty.hidden = items.length > 0;
  gallery.replaceChildren(...items.map((m, i) => {
    const el = document.createElement('div');
    el.className = 'gal-card' + (state.screen.slideshow_visible && m.id === current
      ? ' is-current' : '');
    el.dataset.id = m.id;
    const now = state.screen.slideshow_visible && m.id === current
      ? '<span class="gal-now">на экране</span>' : '';
    el.innerHTML = `
      <div class="gal-shot">
        <img src="${ev(`/media/${encodeURIComponent(m.image)}`)}" alt="" loading="lazy">
        <label class="gal-check" title="Убрать из слайдшоу">
          <input type="checkbox" checked data-gallery="${m.id}" aria-label="В слайдшоу">
        </label>
        ${now}
      </div>
      <div class="gal-meta">
        <span class="gal-text">${m.body ? escapeHTML(m.body) : '<span class="muted">📷 фото</span>'}</span>
        <span class="gal-author">${escapeHTML(m.author)} · ${i + 1} из ${items.length}</span>
      </div>
      <div class="gal-actions">
        <button class="icon-btn" data-gal="show" data-id="${m.id}" title="Показать сейчас">▶</button>
        <button class="icon-btn danger" data-gal="drop" data-id="${m.id}" title="Убрать из слайдшоу">✕</button>
      </div>`;
    return el;
  }));

  tabCount.textContent = items.length;
}

async function setSlideshow(id, on) {
  try {
    applyState(await postForm(ev('/api/moderator/slideshow'),
      { message_id: id, on: on ? '1' : '0' }));
  } catch (ex) {
    showToast(ex.message, 'warn');
    renderAll();   // вернуть галочку в исходное положение
  }
}

/* Снятие галочки и из галереи, и из строки ленты — один обработчик на оба */
document.addEventListener('change', (e) => {
  const box = e.target.closest('input[data-act="slideshow"], input[data-gallery]');
  if (!box) return;
  setSlideshow(Number(box.dataset.id || box.dataset.gallery), box.checked);
});

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-gal]');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  if (btn.dataset.gal === 'drop') setSlideshow(id, false);
  else if (btn.dataset.gal === 'show') {
    // сначала галерея должна быть запущена, иначе показывать нечего
    const go = state.screen.slideshow_visible
      ? Promise.resolve()
      : control('slideshow', '1');
    go.then(() => control('slideshow-show', String(id)))
      .catch(() => {});
  }
});

/* ---------------- Запуск и остановка ---------------- */
function renderSlideshowBar() {
  const total = inSlideshow().length;
  const on = !!state.screen.slideshow_visible;
  runBtn.classList.toggle('active', on);
  runBtn.textContent = on ? 'Вернуть обычную ленту' : 'Запустить слайдшоу на экране';
  runBtn.disabled = !on && total === 0;
  clearBtn.disabled = total === 0;
  runState.textContent = on
    ? `Слайдшоу идёт: снимок ${state.screen.slideshow_index + 1} из ${total}. Обычная лента ждёт.`
    : `Слайдшоу выключено, на экране обычная лента. В галерее ${total} ${plural(total, 'снимок', 'снимка', 'снимков')}.`;

  // Ползунок длительности: своя настройка у слайдшоу, у ленты она другая.
  // Пока пользователь тащит ползунок, не перерисовываем его из ответа —
  // иначе значение прыгает под пальцем.
  const sec = state.screen.slideshow_interval || 15;
  slideIntervalVal.textContent = sec;
  if (document.activeElement !== slideInterval && slideInterval.value !== String(sec)) {
    slideInterval.value = sec;
  }
}

let slideIntervalDebounce = null;
slideInterval.addEventListener('input', () => {
  slideIntervalVal.textContent = slideInterval.value;
});
slideInterval.addEventListener('change', () => {
  clearTimeout(slideIntervalDebounce);
  slideIntervalDebounce = setTimeout(() =>
    control('slideshow-interval', slideInterval.value), 150);
});

runBtn.addEventListener('click', () =>
  control('slideshow', state.screen.slideshow_visible ? '0' : '1'));

clearBtn.addEventListener('click', async () => {
  const total = inSlideshow().length;
  if (!confirm(`Убрать все снимки из галереи?\n\n${total} ${plural(total, 'снимок уйдёт', 'снимка уйдут', 'снимков уйдут')} из слайдшоу.\nФотографии останутся в архиве и в ленте.`)) return;
  try {
    applyState(await postForm(ev('/api/moderator/slideshow-clear'), {}));
    showToast('Галерея пуста', 'ok', 2200);
  } catch (ex) {
    showToast(ex.message, 'warn');
  }
});

/* ---------------- Архив фотографий ---------------- */
async function loadArchive() {
  archiveStatus.textContent = '';
  try {
    const info = await api(ev('/api/moderator/photos'));
    const n = info.count || 0;
    archiveCount.innerHTML = n
      ? `Накоплено <b>${n}</b> ${plural(n, 'фотография', 'фотографии', 'фотографий')}, <b>${fmtBytes(info.bytes)}</b>.`
      : 'Пока ни одной фотографии не было.';
    archiveDownload.href = ev('/api/moderator/photos.zip');
    archiveDownload.classList.toggle('is-disabled', !n);
    archiveWipe.disabled = !n;
  } catch (ex) {
    archiveStatus.textContent = 'Не удалось узнать размер архива';
  }
}

archiveWipe.addEventListener('click', async () => {
  let n = 0;
  try { n = (await api(ev('/api/moderator/photos'))).count || 0; } catch (_) {}
  if (!n) return;
  const ok = confirm(
    `Удалить все фотографии навсегда?\n\n` +
    `С сервера исчезнут ${n} ${plural(n, 'файл', 'файла', 'файлов')}, ` +
    `а сообщения с фотографиями уйдут из ленты.\n` +
    `Текстовые сообщения останутся.\n\n` +
    `Отменить это будет нельзя. Если фотографии нужны — сначала нажмите ` +
    `«Сохранить и скачать все фотографии».\n\nВсё равно удалить?`
  );
  if (!ok) return;
  archiveStatus.textContent = 'Удаляю…';
  try {
    const res = await postForm(ev('/api/moderator/photos-delete-all'), {});
    applyState(res);
    archiveStatus.textContent = 'Архив пуст, фотографии с экрана убраны';
    showToast(`Удалено файлов: ${res.removed_files}`, 'ok', 2600);
    loadArchive();
  } catch (ex) {
    archiveStatus.textContent = '';
    showToast(ex.message, 'warn');
  }
});

/* ---------------- Склейка с общим списком ---------------- */
/* Список, «сейчас на экране» и кнопки перерисовывает moderator.js: он зовёт
   renderGallery и renderSlideshowBar сам, если они есть. Отдельной обёртки
   здесь намеренно нет — подмена renderAll ломалась бы при первом же
   добавлении строки и выглядела бы как «перестало обновляться». */