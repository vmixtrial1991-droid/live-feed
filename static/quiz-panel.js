/* Квиз (5.0) — вкладка ведущего: вопросы, стадии игры, ссылка с QR.

   Ведущий держит игру теми же кнопками, что и показ ленты: открыть
   вопрос, закрыть и показать распределение, следующий, лидеры. Экран
   один — на время игры лента встаёт, ведущий видит это здесь же. */

const quizList = document.getElementById('quizList');
const quizEmpty = document.getElementById('quizEmpty');
const quizPhaseText = document.getElementById('quizPhaseText');
const quizBoardHost = document.getElementById('quizBoardHost');
const tabQuizBadge = document.getElementById('tabQuizBadge');

const quizOpenBtn = document.getElementById('quizOpenBtn');
const quizCloseBtn = document.getElementById('quizCloseBtn');
const quizNextBtn = document.getElementById('quizNextBtn');
const quizBoardBtn = document.getElementById('quizBoardBtn');
const quizShowBtn = document.getElementById('quizShowBtn');
const quizFeedBtn = document.getElementById('quizFeedBtn');
const quizResetBtn = document.getElementById('quizResetBtn');
const quizStopBtn = document.getElementById('quizStopBtn');

const quizEditor = document.getElementById('quizEditor');
const qText = document.getElementById('qText');
const qTextCount = document.getElementById('qTextCount');
const qOptions = document.getElementById('qOptions');
const qAddOption = document.getElementById('qAddOption');
const qNoPoints = document.getElementById('qNoPoints');
const qSave = document.getElementById('qSave');
const qCancel = document.getElementById('qCancel');
const qStatus = document.getElementById('qStatus');

const quizLinkText = document.getElementById('quizLinkText');
const quizLinkCopy = document.getElementById('quizLinkCopy');
const quizLinkOpen = document.getElementById('quizLinkOpen');
const quizQrImg = document.getElementById('quizQrImg');
const quizQrDownload = document.getElementById('quizQrDownload');

let quizState = { phase: 'idle', questions: [], board: [] };
let editingId = null;          // вопрос, который правят; null — новый

/* ---------------- Стадия и кнопки ---------------- */
const PHASE_LABEL = {
  idle: 'Квиз не идёт',
  open: 'Вопрос открыт, зал отвечает',
  closed: 'Ответ закрыт, показываем распределение',
  board: 'На экране таблица лидеров',
};

function renderQuizControls() {
  const st = quizState;
  const order = st.questions.map((q) => q.id);
  const cur = order.indexOf(st.current);
  const total = order.length;

  const hasNext = cur >= 0 && cur + 1 < total;
  quizOpenBtn.disabled = st.phase !== 'idle' || !total;
  quizCloseBtn.disabled = st.phase !== 'open';
  quizNextBtn.disabled = !(st.phase === 'open' || st.phase === 'closed') || !hasNext;
  quizBoardBtn.disabled = st.phase === 'idle';
  quizShowBtn.disabled = st.phase === 'idle' || st.visible;
  quizFeedBtn.disabled = !(st.phase !== 'idle' && st.visible);
  quizStopBtn.disabled = st.phase === 'idle';
  quizResetBtn.disabled = !st.answers_total;

  let line = PHASE_LABEL[st.phase] || PHASE_LABEL.idle;
  if ((st.phase === 'open' || st.phase === 'closed') && cur >= 0) {
    const answered = st.questions[cur].answered || 0;
    line += ` · вопрос ${cur + 1} из ${total} · ответили ${answered} из ${st.participants}`;
  }
  if (st.phase !== 'idle') {
    line += st.visible ? ' · на экране: квиз' : ' · на экране: лента (зал отвечает)';
  }
  quizPhaseText.textContent = line;

  if (st.phase !== 'idle' && cur >= 0) {
    tabQuizBadge.hidden = false;
    tabQuizBadge.textContent = String(st.questions[cur].answered || 0);
  } else {
    tabQuizBadge.hidden = true;
  }
}

/* ---------------- Список вопросов ---------------- */
function renderQuestions() {
  quizEmpty.hidden = quizState.questions.length > 0;
  quizList.replaceChildren(...quizState.questions.map((q, i) => {
    const el = document.createElement('div');
    el.className = 'quiz-row' + (q.id === quizState.current ? ' is-current' : '');
    const opts = q.options.map((o, k) => {
      const right = q.scored && q.correct === k;
      return `<span class="quiz-chip${right ? ' is-right' : ''}">${escapeHTML(o)}</span>`;
    }).join('');
    el.innerHTML = `
      <div class="quiz-row-top">
        <span class="quiz-pos">${i + 1}</span>
        <span class="quiz-qtext">${escapeHTML(q.text)}</span>
        ${q.scored ? '' : '<span class="quiz-chip quiz-chip-free">без очков</span>'}
        <span class="quiz-answered" title="Сколько гостей ответили">${q.answered || 0} отв.</span>
        <span class="or-actions">
          <button class="icon-btn" data-qact="up" data-qid="${q.id}" title="Выше">↑</button>
          <button class="icon-btn" data-qact="down" data-qid="${q.id}" title="Ниже">↓</button>
          <button class="icon-btn" data-qact="edit" data-qid="${q.id}" title="Править">✎</button>
          <button class="icon-btn danger" data-qact="delete" data-qid="${q.id}" title="Удалить">✕</button>
        </span>
      </div>
      <div class="quiz-chips">${opts}</div>`;
    return el;
  }));
}

function renderBoard() {
  const medals = ['🥇', '🥈', '🥉'];
  const rows = quizState.board || [];
  quizBoardHost.replaceChildren(...rows.map((r, i) => {
    const el = document.createElement('div');
    el.className = 'quiz-board-row';
    el.innerHTML = `<span class="qb-pos">${medals[i] || i + 1}</span>
      <span class="qb-name">${escapeHTML(r.name)}</span>
      <span class="qb-score">${r.score}</span>`;
    return el;
  }));
  if (!rows.length) {
    quizBoardHost.innerHTML =
      '<p class="muted small">Пока никто не отвечал на зачётные вопросы.</p>';
  }
}

async function reload() {
  // во время правки вопроса список не перерисовываем: ведущий печатает,
  // а сигналы об ответах гостей прилетают часто
  try {
    quizState = await api(ev('/api/moderator/quiz'));
    renderQuizControls();
    renderBoard();
    if (quizEditor.hidden) renderQuestions();
  } catch (ex) {
    /* панель не открыта или вход истёк — молчим */
  }
}

/* ---------------- Управление игрой ---------------- */
async function quizControl(action, fields = {}) {
  try {
    quizState = await postForm(ev('/api/moderator/quiz/control'), { action, ...fields });
    renderQuizControls();
    renderBoard();
    if (quizEditor.hidden) renderQuestions();
  } catch (ex) {
    showToast(ex.message || 'Не вышло', 'warn');
  }
}

quizOpenBtn.addEventListener('click', () => quizControl('open'));
quizCloseBtn.addEventListener('click', () => quizControl('close'));
quizNextBtn.addEventListener('click', () => quizControl('next'));
quizBoardBtn.addEventListener('click', () => quizControl('board'));
quizShowBtn.addEventListener('click', () => quizControl('show'));
quizFeedBtn.addEventListener('click', () => quizControl('feed'));
quizStopBtn.addEventListener('click', async () => {
  if (!confirm('Завершить квиз?\n\nЭкран снова начнёт показывать ленту.')) return;
  await quizControl('stop');
});
quizResetBtn.addEventListener('click', async () => {
  if (!confirm('Стереть все ответы гостей?\n\nВопросы останутся, игра начнётся с чистого листа.')) return;
  const before = quizState.answers_total || 0;
  await quizControl('reset');
  if (quizState.answers_total === 0) showToast(`Стерто ответов: ${before}`, 'ok', 2500);
});

quizList.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-qact]');
  if (!btn) return;
  const qid = Number(btn.dataset.qid);
  const act = btn.dataset.qact;
  if (act === 'up' || act === 'down') {
    try {
      await postForm(ev('/api/moderator/quiz/move'), { question_id: qid, direction: act });
      reload();
    } catch (ex) { showToast(ex.message, 'warn'); }
  } else if (act === 'delete') {
    if (!confirm('Удалить вопрос? Ответы гостей на него тоже удалятся.')) return;
    try {
      await postForm(ev('/api/moderator/quiz/delete'), { question_id: qid });
      reload();
    } catch (ex) { showToast(ex.message, 'warn'); }
  } else if (act === 'edit') {
    openEditor(quizState.questions.find((q) => q.id === qid));
  }
});

/* ---------------- Редактор ---------------- */
function addOptionRow(text = '') {
  const row = document.createElement('div');
  row.className = 'quiz-opt-row';
  row.innerHTML = `
    <input type="radio" name="qCorrect" title="Правильный вариант">
    <input type="text" class="q-opt-input" maxlength="80" placeholder="Вариант ответа">
    <button class="icon-btn danger" type="button" title="Убрать вариант">✕</button>`;
  row.querySelector('input[type=text]').value = text;
  row.querySelector('button').addEventListener('click', () => {
    if (qOptions.children.length <= 2) {
      showToast('Нужно хотя бы два варианта', 'warn', 2500);
      return;
    }
    row.remove();
  });
  qOptions.appendChild(row);
}

function openEditor(question) {
  editingId = question ? question.id : null;
  quizEditor.hidden = false;
  qStatus.textContent = '';
  qText.value = question ? question.text : '';
  qNoPoints.checked = question ? !question.scored : false;
  qOptions.replaceChildren();
  const opts = question ? question.options : ['', ''];
  opts.forEach((o) => addOptionRow(o));
  if (question && question.scored && question.correct >= 0) {
    const radios = qOptions.querySelectorAll('input[type=radio]');
    if (radios[question.correct]) radios[question.correct].checked = true;
  }
  updateQTextCount();
  qText.focus();
}

function closeEditor() {
  quizEditor.hidden = true;
  editingId = null;
}

function updateQTextCount() { qTextCount.textContent = String(qText.value.length); }
qText.addEventListener('input', updateQTextCount);
qAddOption.addEventListener('click', () => {
  if (qOptions.children.length >= 6) {
    showToast('Больше шести вариантов экран не покажет', 'warn', 2500);
    return;
  }
  addOptionRow();
});

qSave.addEventListener('click', async () => {
  qStatus.textContent = '';
  const texts = [...qOptions.querySelectorAll('.q-opt-input')].map((i) => i.value.trim());
  const checked = qOptions.querySelector('input[type=radio]:checked');
  const fields = {
    text: qText.value.trim(),
    options: JSON.stringify(texts),
    scored: qNoPoints.checked ? '0' : '1',
    correct: qNoPoints.checked || !checked
      ? -1 : [...qOptions.querySelectorAll('input[type=radio]')].indexOf(checked),
  };
  try {
    if (editingId) {
      await postForm(ev('/api/moderator/quiz/update'), { question_id: editingId, ...fields });
    } else {
      await postForm(ev('/api/moderator/quiz/add'), fields);
    }
    closeEditor();
    reload();
  } catch (ex) {
    qStatus.textContent = ex.message || 'Не сохранилось';
  }
});
qCancel.addEventListener('click', closeEditor);
document.getElementById('quizAddBtn').addEventListener('click', () => openEditor(null));

/* ---------------- Ссылка и QR ---------------- */
function renderQuizLink(code) {
  const url = `${location.origin}${ev('/quiz')}${code ? `?t=${code}` : ''}`;
  quizLinkText.textContent = url;
  quizLinkOpen.href = url;
  const src = ev(`/api/qr.svg?kind=quiz&v=${encodeURIComponent(code || '')}`);
  quizQrImg.src = src;
  quizQrDownload.href = src;
}

quizLinkCopy.addEventListener('click', async () => {
  const ok = await copy(quizLinkText.textContent);
  showToast(ok ? 'Ссылка на квиз скопирована' : 'Скопируйте вручную', ok ? 'ok' : 'warn');
});

/* окно ведущего принимает сигнал quiz_update из общего канала (moderator.js) */
window.quizPanel = {
  reload,
  init() {
    api(ev('/api/moderator/invite'))
      .then(({ code }) => renderQuizLink(code))
      .catch(() => { quizLinkText.textContent = '—'; });
    reload();
  },
};
window.quizPanel.init();
