/* Квиз (5.0) — вкладка ведущего: вопросы, стадии игры, ссылка с QR.

   Игру ведёт одна шаговая кнопка: она подписана по текущей стадии
   («Открыть вопрос → Закрыть приём → Показать ответ → Дальше»), и после
   последнего вопроса экран сам уходит на финальную таблицу. Экран один —
   на время игры лента встаёт, ведущий видит это здесь же. Живые полосы
   вариантов видит только ведущий: зал узнаёт распределение не раньше
   закрытия приёма. */

const quizList = document.getElementById('quizList');
const quizEmpty = document.getElementById('quizEmpty');
const quizPhaseText = document.getElementById('quizPhaseText');
const quizBoardHost = document.getElementById('quizBoardHost');
const tabQuizBadge = document.getElementById('tabQuizBadge');

const quizStepBtn = document.getElementById('quizStepBtn');
const quizStepGlyph = document.getElementById('quizStepGlyph');
const quizStepLabel = document.getElementById('quizStepLabel');
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
const qSeconds = document.getElementById('qSeconds');
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
  closed: 'Приём закрыт, зал видит распределение',
  reveal: 'Ответ показан, зал видит правильный',
  board: 'На экране таблица лидеров',
  board_final: 'Игра окончена, на экране финальная таблица',
};

/* Один шаг на каждую стадию: подпись кнопки и есть подсказка, что будет
   дальше. На таблице шага нет — там игра либо кончена, либо ведущий
   смотрит промежуточных лидеров. */
const STEP = {
  idle:   { action: 'open',   glyph: '▶', label: 'Открыть вопрос' },
  open:   { action: 'close',  glyph: '⏹', label: 'Закрыть приём' },
  closed: { action: 'reveal', glyph: '★', label: 'Показать ответ' },
  reveal: { action: 'next',   glyph: '›', label: 'Дальше' },
  board:  { action: '',       glyph: '·', label: 'Таблица лидеров' },
};

// отсчёт на проекторе живёт на сервере, но панель тикает сама между
// сигналами: иначе цифра стоит на месте, пока гость кто-нибудь не ответит
let quizDeadline = null;

function renderQuizControls() {
  const st = quizState;
  const order = st.questions.map((q) => q.id);
  const cur = order.indexOf(st.current);
  const total = order.length;

  quizDeadline = (st.time_left != null) ? Date.now() + st.time_left * 1000 : null;

  const step = STEP[st.phase] || STEP.idle;
  const stepOn = !!step.action && (st.phase !== 'idle' || total > 0);
  quizStepBtn.disabled = !stepOn;
  quizStepGlyph.textContent = step.glyph;
  quizStepLabel.textContent = stepOn ? step.label : 'Сначала добавьте вопросы';

  quizBoardBtn.disabled = st.phase === 'idle';
  quizShowBtn.disabled = st.phase === 'idle' || st.visible;
  quizFeedBtn.disabled = !(st.phase !== 'idle' && st.visible);
  quizStopBtn.disabled = st.phase === 'idle';
  quizResetBtn.disabled = !st.answers_total;

  let line = PHASE_LABEL[st.phase === 'board' && st.final ? 'board_final' : st.phase]
    || PHASE_LABEL.idle;
  if ((st.phase === 'open' || st.phase === 'closed' || st.phase === 'reveal') && cur >= 0) {
    const answered = st.questions[cur].answered || 0;
    line += ` · вопрос ${cur + 1} из ${total} · ответили ${answered} из ${st.participants}`;
    if (quizDeadline !== null) {
      const left = Math.max(0, Math.ceil((quizDeadline - Date.now()) / 1000));
      line += left > 0 ? ` · осталось ${left} с` : ' · время вышло';
    }
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

setInterval(() => {
  // тикаем только когда отсчёт есть: без таймера перерисовка не нужна
  if (quizDeadline !== null) renderQuizControls();
}, 1000);

/* ---------------- Список вопросов ---------------- */
function tallyRow(q) {
  // живые полосы: куда тяготеет зал. Залу это откроется только после
  // закрытия приёма, поэтому панель ведущего — единственное место, где
  // видно, что зал уже начал отвечать однотипно
  if (!q.tally || !q.answered) return '';
  const parts = q.tally.map((n, k) => {
    const pct = Math.round((n / q.answered) * 100);
    const right = q.scored && q.correct === k;
    return `<span class="qt-item${right ? ' is-right' : ''}" style="--w:${pct}%">`
      + `<i>${escapeHTML(q.options[k] || '')}</i><b>${n}</b></span>`;
  });
  return `<div class="quiz-tally" title="Куда тяготеет зал — это видите только вы">${parts.join('')}</div>`;
}

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
        ${q.seconds ? `<span class="quiz-chip quiz-chip-time" title="Отсчёт на проекторе">${q.seconds} с</span>` : ''}
        <span class="quiz-answered" title="Сколько гостей ответили">${q.answered || 0} отв.</span>
        <span class="or-actions">
          <button class="icon-btn" data-qact="up" data-qid="${q.id}" title="Выше">↑</button>
          <button class="icon-btn" data-qact="down" data-qid="${q.id}" title="Ниже">↓</button>
          <button class="icon-btn" data-qact="copy" data-qid="${q.id}" title="Дублировать копию сразу ниже">⧉</button>
          <button class="icon-btn" data-qact="edit" data-qid="${q.id}" title="Править">✎</button>
          <button class="icon-btn danger" data-qact="delete" data-qid="${q.id}" title="Удалить">✕</button>
        </span>
      </div>
      <div class="quiz-chips">${opts}</div>${tallyRow(q)}`;
    return el;
  }));
}

function renderBoard() {
  const medals = { 1: '🥇', 2: '🥈', 3: '🥉' };
  const rows = quizState.board || [];
  quizBoardHost.replaceChildren(...rows.map((r) => {
    const el = document.createElement('div');
    el.className = 'quiz-board-row';
    // место берём с сервера: равные очки = равное место, и у двух первых
    // нет одного третьего на двоих
    el.innerHTML = `<span class="qb-pos">${medals[r.rank] || r.rank}</span>
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

/* Главный шаг игры: его делает и кнопка, и пробел на вкладке «Квиз».
   Куда идти дальше, решает стадия, а не ведущий — перепутать нельзя. */
function quizStep() {
  const step = STEP[quizState.phase] || STEP.idle;
  if (!step.action || quizStepBtn.disabled) return;
  quizControl(step.action);
}

quizStepBtn.addEventListener('click', quizStep);
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
  } else if (act === 'copy') {
    try {
      await postForm(ev('/api/moderator/quiz/duplicate'), { question_id: qid });
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
  qSeconds.value = question && question.seconds ? String(question.seconds) : '';
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
    // поле всегда прилетает: 0 для сервера значит «отсчёта нет», поэтому
    // стереть таймер можно тем же способом, каким он ставится
    seconds: qSeconds.value.trim() || '0',
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

/* окно ведущего принимает сигнал quiz_update из общего канала (moderator.js).
   step и board вызывает клавиатура moderator.js: пробел ведёт цепочку,
   L выводит лидеров, и обе работают только на вкладке «Квиз» */
window.quizPanel = {
  reload,
  step: quizStep,
  board: () => quizControl('board'),
  init() {
    api(ev('/api/moderator/invite'))
      .then(({ code }) => renderQuizLink(code))
      .catch(() => { quizLinkText.textContent = '—'; });
    reload();
  },
};
window.quizPanel.init();
