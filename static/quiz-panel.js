/* Квиз (5.0) — вкладка ведущего: вопросы, стадии игры, ссылка с QR.

   Игра начинается с одной кнопки «Запустить квиз»: экран плавно уходит на
   холл с названием игры и QR, зал подключается, а вопросы к этому моменту
   готовы не всегда — дописать их можно прямо в холле. Дальше игру ведёт
   одна шаговая кнопка: она подписана по текущей стадии («Открыть первый
   вопрос → Закрыть приём → Показать ответ → Дальше»), и после последнего
   вопроса экран сам уходит на финальную таблицу. Экран один — на время
   игры лента встаёт, ведущий видит это здесь же. Живые полосы вариантов
   видит только ведущий: зал узнаёт распределение не раньше закрытия
   приёма. */

const quizList = document.getElementById('quizList');
const quizEmpty = document.getElementById('quizEmpty');
const quizPhaseText = document.getElementById('quizPhaseText');
const quizBoardHost = document.getElementById('quizBoardHost');
const tabQuizBadge = document.getElementById('tabQuizBadge');

const quizStartBtn = document.getElementById('quizStartBtn');
const quizStepBtn = document.getElementById('quizStepBtn');
const quizStepGlyph = document.getElementById('quizStepGlyph');
const quizStepLabel = document.getElementById('quizStepLabel');
const quizBoardBtn = document.getElementById('quizBoardBtn');
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
  lobby: 'Квиз запущен: на экране название игры и QR, зал подключается',
  open: 'Вопрос открыт, зал отвечает',
  closed: 'Приём закрыт, зал видит распределение',
  reveal: 'Ответ показан, зал видит правильный',
  board: 'На экране таблица лидеров',
  board_final: 'Игра окончена, на экране финальная таблица',
};

/* Один шаг на каждую стадию: подпись кнопки и есть подсказка, что будет
   дальше. Пока квиз не запущен, шаговой кнопки нет — есть «Запустить квиз».
   Промежуточная таблица — пауза, поэтому с неё шаг ведёт обратно к вопросу,
   туда же, где игра стояла. Финальная таблица возврата не имеет: последний
   вопрос прожит, и остатка игры за ним нет. */
const STEP = {
  idle:   { action: '',       glyph: '·', label: 'Квиз не запущен' },
  lobby:  { action: 'open',   glyph: '▶', label: 'Открыть первый вопрос' },
  open:   { action: 'close',  glyph: '⏹', label: 'Закрыть приём' },
  closed: { action: 'reveal', glyph: '★', label: 'Показать ответ' },
  reveal: { action: 'next',   glyph: '›', label: 'Дальше' },
  board:  { action: '',       glyph: '·', label: 'Финальная таблица' },
  board_back: { action: 'back', glyph: '↩', label: 'Вернуться к вопросу' },
};

// отсчёт на проекторе живёт на сервере, но панель тикает сама между
// сигналами: иначе цифра стоит на месте, пока кто-нибудь не ответит.
// Живёт отдельно от renderQuizControls: перерисовка не имеет права
// пересчитывать дедлайн от старого серверного числа, иначе каждая секунда
// начинала отсчёт заново и цифра замирала
let quizDeadline = null;

function armQuizDeadline() {
  quizDeadline = (quizState.time_left != null)
    ? Date.now() + quizState.time_left * 1000 : null;
}

/* Куда ведёт текущий шаг. На промежуточной таблице это возврат к вопросу,
   и решает сервер: он знает стадию, из которой ведущий ушёл на лидеров. */
function currentStep() {
  const st = quizState;
  if (st.phase === 'board' && st.back_phase) return STEP.board_back;
  return STEP[st.phase] || STEP.idle;
}

function renderQuizControls() {
  const st = quizState;
  const order = st.questions.map((q) => q.id);
  const cur = order.indexOf(st.current);
  const total = order.length;

  const step = currentStep();
  const stepOn = !!step.action && total > 0;
  // пока игра не запущена, шаговой кнопки не видно: на её месте «Запустить
  // квиз». Запущенная игра, наоборот, прячет запуск — двух главных кнопок
  // у ведущего не бывает
  quizStartBtn.hidden = st.phase !== 'idle';
  quizStepBtn.hidden = st.phase === 'idle';
  quizStepBtn.disabled = !stepOn;
  quizStepGlyph.textContent = step.glyph;
  // почему шаг недоступен — три разные причины, и «добавьте вопросы» на
  // финальной таблице читается как поломка: там игра уже прожита
  if (stepOn) quizStepLabel.textContent = step.label;
  else if (!total) quizStepLabel.textContent = 'Сначала добавьте вопросы';
  else if (st.phase === 'board' && st.final) quizStepLabel.textContent = 'Игра окончена';
  else quizStepLabel.textContent = step.label || 'Шага нет';

  // «Лидеры на экран» включается когда есть что показывать: в холле ответов
  // ещё нет, и пустая таблица залу не нужна. На финальной таблице кнопка
  // выключена: нажатие снимало с неё флаг финала и превращало итог вечера
  // в промежуточную таблицу, которая листает всех
  const onFinalBoard = st.phase === 'board' && st.final;
  quizBoardBtn.disabled = !st.answers_total || onFinalBoard;
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
  // ведущий мог уйти на лидеров посреди вопроса: подсказка говорит, что
  // шаг не тупик, а возврат туда, где игра стояла
  if (st.phase === 'board' && st.back_phase) {
    line += ' · шаг вернёт к вопросу';
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
    armQuizDeadline();
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
    armQuizDeadline();
    renderQuizControls();
    renderBoard();
    if (quizEditor.hidden) renderQuestions();
  } catch (ex) {
    showToast(ex.message || 'Не вышло', 'warn');
  }
}

/* Главный шаг игры: его делает и кнопка, и пробел на вкладке «Квиз».
   Куда идти дальше, решает стадия, а не ведущий — перепутать нельзя.
   Запуск игры пробелом не делается: на вкладке «Квиз» пробел — обычная
   клавиша прокрутки списка вопросов, и случайный запуск забрал бы у зала
   экран посреди события. Запуск — только кнопкой. */
function quizStep() {
  const step = currentStep();
  if (!step.action || quizStepBtn.disabled) return;
  quizControl(step.action);
}

quizStepBtn.addEventListener('click', quizStep);
quizStartBtn.addEventListener('click', () => quizControl('start'));
quizBoardBtn.addEventListener('click', () => quizControl('board'));
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
/* Потолок вариантов один на всю игру: четыре. Больше строк — мельче шрифт
   на проекторе, и зал перестаёт различать ответы; сервер держит тот же
   предел и отвечает 400, поэтому число здесь не «на глаз», а подпись того,
   что всё равно не примут. */
const MAX_OPTIONS = 4;

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
    syncOptionButtons();
  });
  qOptions.appendChild(row);
  syncOptionButtons();
  return row;
}

/* «+ вариант» гаснет на потолке: кнопка, которая молча ругается тостом,
   читается как поломка. Заодно крестики гаснут, когда ниже двух вариантов
   уже не опуститься, — это видно до нажатия, а не после отказа. */
function syncOptionButtons() {
  const count = qOptions.children.length;
  const full = count >= MAX_OPTIONS;
  qAddOption.disabled = full;
  qAddOption.title = full ? `Максимум ${MAX_OPTIONS} варианта` : 'Добавить вариант';
  [...qOptions.children].forEach((row) => {
    const del = row.querySelector('button');
    if (!del) return;
    del.disabled = count <= 2;
    del.title = count <= 2 ? 'Нужно хотя бы два варианта' : 'Убрать вариант';
  });
}

function openEditor(question) {
  editingId = question ? question.id : null;
  quizEditor.hidden = false;
  qStatus.textContent = '';
  qText.value = question ? question.text : '';
  qNoPoints.checked = question ? !question.scored : false;
  qSeconds.value = question && question.seconds ? String(question.seconds) : '';
  qOptions.replaceChildren();
  // новый вопрос начинается с одной строки: остальные добавляются кнопкой.
  // Пустой список выглядел бы как «вопрос без вариантов», хотя сервер
  // требует минимум два, и ведущий узнавал бы об этом только на сохранении
  const opts = question ? question.options : [''];
  opts.forEach((o) => addOptionRow(o));
  if (question && question.scored && question.correct >= 0) {
    const radios = qOptions.querySelectorAll('input[type=radio]');
    if (radios[question.correct]) radios[question.correct].checked = true;
  }
  updateQTextCount();
  syncOptionButtons();
  qText.focus();
}

function closeEditor() {
  quizEditor.hidden = true;
  editingId = null;
}

function updateQTextCount() { qTextCount.textContent = String(qText.value.length); }
qText.addEventListener('input', updateQTextCount);
qAddOption.addEventListener('click', () => {
  if (qOptions.children.length >= MAX_OPTIONS) return;   // кнопка и так погашена
  addOptionRow().querySelector('input[type=text]').focus();
});

qSave.addEventListener('click', async () => {
  qStatus.textContent = '';
  const rows = [...qOptions.children].map((row) => ({
    text: row.querySelector('.q-opt-input').value.trim(),
    checked: row.querySelector('input[type=radio]').checked,
  }));
  // пустые строки сервер отбросит сам — значит, и номер правильного надо
  // считать по заполненным: иначе галочка на третьей строке при пустой
  // второй уехала бы на чужой вариант или за пределы списка
  const kept = rows.filter((r) => r.text);
  const correct = kept.findIndex((r) => r.checked);
  if (!qText.value.trim()) {
    qStatus.textContent = 'Напишите текст вопроса';
    qText.focus();
    return;
  }
  if (kept.length < 2) {
    qStatus.textContent = 'Нужно хотя бы два варианта — добавьте второй';
    (qOptions.querySelector('.q-opt-input') || qText).focus();
    return;
  }
  if (!qNoPoints.checked && correct < 0) {
    qStatus.textContent = 'Отметьте правильный вариант кружком слева';
    return;
  }
  const fields = {
    text: qText.value.trim(),
    options: JSON.stringify(kept.map((r) => r.text)),
    scored: qNoPoints.checked ? '0' : '1',
    // поле всегда прилетает: 0 для сервера значит «отсчёта нет», поэтому
    // стереть таймер можно тем же способом, каким он ставится
    seconds: qSeconds.value.trim() || '0',
    correct: qNoPoints.checked ? -1 : correct,
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
