/* Админка: список событий, создание, карточка со ссылками */

const $ = (id) => document.getElementById(id);

let events = [];
let base = '';
let current = null;   // открытое событие

// ---------------------------------------------------------------- список ---
async function load() {
  try {
    const data = await api('/api/admin/events');
    events = data.events || [];
    base = data.base || '';
    $('baseHint').textContent = base;
    render();
  } catch (ex) {
    if (ex.message.includes('403') || ex.message.includes('Нужен вход')) {
      location.href = '/admin/login';
      return;
    }
    showToast(ex.message || 'Не удалось загрузить список', 'warn');
  }
}

function render() {
  const list = $('list');
  list.innerHTML = '';
  $('empty').hidden = events.length > 0;

  for (const ev of events) {
    list.appendChild(card(ev));
  }
}

function card(ev) {
  const el = document.createElement('div');
  el.className = 'ev-card';

  const stats = document.createElement('div');
  stats.className = 'ev-stats';
  stats.innerHTML =
    `<span class="badge ${ev.messages ? 'ok' : 'zero'}">${ev.messages} ${plural(ev.messages, 'сообщение', 'сообщения', 'сообщений')}</span>` +
    `<span class="badge zero">${ev.participants} ${plural(ev.participants, 'участник', 'участника', 'участников')}</span>`;

  const main = document.createElement('div');
  main.className = 'ev-main';
  main.innerHTML =
    `<div class="ev-title">${escapeHTML(ev.title)}</div>` +
    (ev.subtitle ? `<div class="ev-sub">${escapeHTML(ev.subtitle)}</div>` : '') +
    `<div class="ev-slug">адрес: <code>/e/${escapeHTML(ev.slug)}</code></div>`;

  const actions = document.createElement('div');
  actions.className = 'ev-actions';
  const open = button('Ссылки и QR', 'btn small', () => openDetail(ev.slug));
  const feed = button('Открыть', 'btn ghost small', () => window.open(`${base}/e/${ev.slug}/`, '_blank'));
  actions.append(open, feed);

  el.append(main, stats, actions);
  return el;
}

function button(text, cls, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = cls;
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

// -------------------------------------------------------------- создание ---
$('createForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('createBtn');
  btn.disabled = true;
  btn.textContent = 'Создаём…';
  try {
    const res = await apiForm('/api/admin/events', {
      title: $('fTitle').value,
      subtitle: $('fSubtitle').value,
      password: $('fPassword').value,
      slug: $('fSlug').value,
    });
    $('createForm').reset();
    showToast(`Событие «${res.event.title}» готово`);
    await load();
    openDetail(res.event.slug);
  } catch (ex) {
    showToast(ex.message || 'Не получилось создать', 'warn');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Создать событие';
  }
});

$('fTitle').addEventListener('input', () => {
  if (!$('fSlug').dataset.touched) {
    $('fSlug').placeholder = slugifyPlaceholder($('fTitle').value);
  }
});

$('fSlug').addEventListener('input', () => { $('fSlug').dataset.touched = '1'; });

/** Подсказка адреса: латиницей, как это делает сервер. */
function slugifyPlaceholder(title) {
  const map = {
    а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'y',к:'k',л:'l',
    м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'ts',ч:'ch',
    ш:'sh',щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya',
  };
  let out = '';
  for (const ch of (title || '').toLowerCase()) out += map[ch] ?? ch;
  return out.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48) || 'event';
}

// -------------------------------------------------------- карточка события ---
async function openDetail(slug) {
  try {
    const data = await api(`/api/admin/events/${encodeURIComponent(slug)}`);
    current = data;
    const { event: ev, links, password } = data;

    $('dTitle').textContent = ev.title;
    $('dSlug').textContent = `${base}/e/${ev.slug} · созданно ${new Date(ev.created_at * 1000).toLocaleDateString('ru-RU')} · сообщений: ${ev.messages}`;
    $('dJoin').textContent = links.join;
    $('dMod').textContent = links.moderator;
    $('dScreen').textContent = links.screen;
    $('dFeed').textContent = links.feed;
    $('dPass').textContent = password || 'не задан';
    $('dQr').src = `/api/admin/events/${encodeURIComponent(ev.slug)}/qr.svg`;
    $('eTitle').value = ev.title;
    $('eSubtitle').value = ev.subtitle || '';

    $('detail').showModal();
  } catch (ex) {
    showToast(ex.message || 'Не удалось открыть событие', 'warn');
  }
}

$('dClose').addEventListener('click', () => $('detail').close());

for (const [id, key] of [
  ['dCopyJoin', 'join'], ['dCopyMod', 'moderator'],
  ['dCopyScreen', 'screen'], ['dCopyFeed', 'feed'],
]) {
  $(id).addEventListener('click', async () => {
    const ok = await copy(current.links[key]);
    showToast(ok ? 'Ссылка скопирована' : 'Не вышло — скопируйте вручную', ok ? 'ok' : 'warn');
  });
}

$('dOpenMod').addEventListener('click', () => window.open(current.links.moderator, '_blank'));
$('dOpenScreen').addEventListener('click', () => window.open(current.links.screen, '_blank'));
$('dOpenFeed').addEventListener('click', () => window.open(current.links.feed, '_blank'));

$('dPrintQr').addEventListener('click', () => {
  const ev = current.event;
  $('pTitle').textContent = ev.title;
  $('pSub').textContent = ev.subtitle || 'Отсканируйте, чтобы отправить текст или фото';
  $('pQr').src = `/api/admin/events/${encodeURIComponent(ev.slug)}/qr.svg`;
  $('detail').close();
  $('printQr').showModal();
});

$('eRotate').addEventListener('click', async () => {
  if (!confirm('Сменить код входа для гостей? Старый QR перестанет работать.')) return;
  await saveEvent({ rotate_invite: '1' });
  showToast('Код входа изменён');
  openDetail(current.event.slug);
});

$('editForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  await saveEvent({
    title: $('eTitle').value,
    subtitle: $('eSubtitle').value,
    password: $('ePassword').value,
  });
  $('ePassword').value = '';
  showToast('Сохранено');
  await load();
  openDetail(current.event.slug);
});

async function saveEvent(fields) {
  try {
    await apiForm(`/api/admin/events/${encodeURIComponent(current.event.slug)}/update`, fields);
  } catch (ex) {
    showToast(ex.message || 'Не получилось сохранить', 'warn');
  }
}

$('dDelete').addEventListener('click', async () => {
  const ev = current.event;
  const word = 'Удалить';
  if (!confirm(`${word} событие «${ev.title}»?\n\nСообщения (${ev.messages}), фотографии и участники исчезнут безвозвратно.`)) return;
  if (!confirm(`Точно удалить? Отменить будет нельзя.`)) return;
  try {
    await postForm(`/api/admin/events/${encodeURIComponent(ev.slug)}/delete`, {});
    $('detail').close();
    current = null;
    showToast(`Событие «${ev.title}» удалено`);
    await load();
  } catch (ex) {
    showToast(ex.message || 'Не получилось удалить', 'warn');
  }
});

// ----------------------------------------------------------------- прочее ---
// Копирование (copy/copyFallback) переехало в common.js: кнопка «Скопировать
// ссылку экрана» нужна и в кабинете ведущего.

$('logoutBtn').addEventListener('click', async () => {
  await postForm('/api/admin/logout', {});
  location.href = '/admin/login';
});

load();