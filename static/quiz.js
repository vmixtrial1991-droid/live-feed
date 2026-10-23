/* Квиз (5.0) — страница гостя.

   Билет здесь тот же, что у ленты: кто уже отправлял сообщения, тот
   попадает в игру сразу, без второго входа. Если билета нет — страница
   показывает форму с тем же кодом приглашения.

   Состояние рисует GET /api/quiz, а WebSocket только сигналит
   «изменилось». Так чужие ответы не гуляют по залу: каждый видит своё. */

const params = new URLSearchParams(location.search);

const joinBox = document.getElementById('joinBox');
const joinForm = document.getElementById('joinForm');
const joinBtn = document.getElementById('joinBtn');
const joinError = document.getElementById('joinError');
const codeField = document.getElementById('codeField');
const codeInput = document.getElementById('codeInput');
const nameInput = document.getElementById('nameInput');

const gameBox = document.getElementById('gameBox');
const myName = document.getElementById('myName');
const myScore = document.getElementById('myScore');
const renameBtn = document.getElementById('renameBtn');
const renameRow = document.getElementById('renameRow');
const renameInput = document.getElementById('renameInput');

const waitBox = document.getElementById('waitBox');
const waitTitle = document.getElementById('waitTitle');
const waitHint = document.getElementById('waitHint');
const questionBox = document.getElementById('questionBox');
const qnLabel = document.getElementById('qnLabel');
const questionText = document.getElementById('questionText');
const optionsBox = document.getElementById('optionsBox');
const answerHint = document.getElementById('answerHint');

const boardBox = document.getElementById('boardBox');
const boardList = document.getElementById('boardList');
const boardMe = document.getElementById('boardMe');

const connPill = document.getElementById('connPill');

let ws = null;            // канал открыт только вошедшему — иначе 4401 и шум
let lastPhase = null;     // чтобы не перерисовывать вопрос на каждом сигнале

/* ---------------- Код приглашения ---------------- */
function cleanCode(raw) {
  return String(raw || '').toUpperCase().replace(/[^A-Z2-9]/g, '').slice(0, 12);
}
codeInput.value = cleanCode(params.get('t') || '');
if (codeInput.value) codeField.hidden = true;   // код пришёл из QR-ссылки
codeInput.addEventListener('input', () => {
  const clean = cleanCode(codeInput.value);
  if (clean !== codeInput.value) codeInput.value = clean;
});

joinForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  joinError.hidden = true;
  joinBtn.disabled = true;
  try {
    await apiForm(ev('/api/join'),
      { name: nameInput.value, invite: cleanCode(codeInput.value) || (params.get('t') || '') });
    startLive();
  } catch (ex) {
    joinError.textContent = ex.message || 'Не получилось. Проверьте код.';
    joinError.hidden = false;
    codeField.hidden = false;
    if (!codeInput.value) codeInput.focus();
  } finally {
    joinBtn.disabled = false;
  }
});

/* ---------------- Игра ---------------- */
function show(el, on) { el.hidden = !on; }

function renderQuiz(data) {
  if (!data) return;
  if (data.name) myName.textContent = data.name;

  const scored = Number(data.my_score || 0);
  myScore.hidden = scored <= 0;
  myScore.textContent = `очков: ${scored}`;

  // холл игры (lobby) — это тоже ожидание: ведущий запустил квиз, но первого
  // вопроса ещё нет, и телефон просто ждёт, подписав себя по-другому
  const waiting = data.phase === 'idle' || data.phase === 'lobby';
  show(waitBox, waiting);
  show(questionBox, data.phase === 'open' || data.phase === 'closed' || data.phase === 'reveal');
  show(boardBox, data.phase === 'board');

  if (waiting) {
    waitTitle.textContent = data.phase === 'lobby' ? 'Квиз начался' : 'Квиз ещё не начался';
    waitHint.textContent = data.phase === 'lobby'
      ? 'Ведущий задаёт первый вопрос — он появится здесь сам, ничего обновлять не нужно.'
      : 'Ведущий откроет вопрос — и он появится здесь сам, ничего обновлять не нужно.';
    lastPhase = null;
    answerHint.textContent = '';
    return;
  }

  if (data.phase === 'board') {
    lastPhase = null;
    renderBoard(data);
    return;
  }

  const q = data.question;
  if (!q) return;   // фаза есть, а вопроса нет: ведущий его удалил
  qnLabel.textContent = data.questions_total
    ? `Вопрос ${data.question_no} из ${data.questions_total}`
    : `Вопрос ${data.question_no}`;
  questionText.textContent = q.text;

  // перерисовываем варианты только когда сменился вопрос или стадия:
  // на каждом сигнале перерисовывать кнопку под пальцем гостя — зло
  if (lastPhase !== `${q.id}:${data.phase}`) {
    lastPhase = `${q.id}:${data.phase}`;
    if (data.phase === 'open') renderOpen(q, data.my_answer);
    else renderClosed(q, data);
  } else if (data.phase === 'open') {
    answerHint.textContent = hintAnswered(data);
  }
}

function hintAnswered(data) {
  const n = data.answered_total || 0;
  if (!n) return 'Пока никто не ответил — начнём вы';
  return `Уже ответили: ${n}`;
}

function renderOpen(q, myAnswer) {
  optionsBox.replaceChildren(...q.options.map((text, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'quiz-option' + (myAnswer === i ? ' is-picked' : '');
    btn.textContent = text;
    btn.addEventListener('click', () => sendAnswer(q.id, i, btn));
    return btn;
  }));
  answerHint.textContent = myAnswer == null
    ? 'Нажмите вариант — до закрытия вопроса его можно поменять.'
    : 'Ваш ответ учтён. До закрытия вопроса его можно поменять.';
}

async function sendAnswer(questionId, option, btn) {
  optionsBox.querySelectorAll('.quiz-option').forEach((b) => b.classList.remove('is-picked'));
  btn.classList.add('is-picked');
  try {
    const res = await postForm(ev('/api/quiz/answer'),
      { question_id: questionId, option });
    answerHint.textContent = `Ответ отправлен. Уже ответили: ${res.answered_total}.`;
  } catch (ex) {
    // 409 — вопрос закрыли, пока палец летел к кнопке: просто обновимся
    if (/409|закрыт/i.test(ex.message || '')) refresh();
    else showToast(ex.message || 'Не отправилось', 'warn');
  }
}

function renderClosed(q, data) {
  const counts = data.counts || q.options.map(() => 0);
  const total = counts.reduce((a, b) => a + b, 0) || 1;
  // reveal — отдельная стадия: на closed распределение уже видно, а
  // правильного ещё нет, и писать «мимо» по такому кадру было бы враньём
  const revealed = data.phase === 'reveal';
  optionsBox.replaceChildren(...q.options.map((text, i) => {
    const row = document.createElement('div');
    const share = Math.round((counts[i] / total) * 100);
    let mark = '';
    if (revealed && data.correct === i) mark = '<span class="qo-mark qo-right">✓</span>';
    else if (revealed && q.scored && data.my_answer === i && data.correct !== i)
      mark = '<span class="qo-mark qo-wrong">✗</span>';
    row.className = 'quiz-bar'
      + (revealed && data.correct === i ? ' is-right' : '')
      + (data.my_answer === i ? ' is-mine' : '');
    row.innerHTML = `<div class="quiz-bar-fill" style="width:${share}%"></div>
      <span class="qo-text">${escapeHTML(text)}</span>
      ${mark}
      <span class="qo-num">${counts[i]} · ${share}%</span>`;
    return row;
  }));
  const base = `Ответили: ${data.answered_total || total}.`;
  if (!revealed) {
    answerHint.textContent = (data.my_answer == null ? 'Вы не ответили.' : 'Ваш ответ учтён.')
      + ` Приём закрыт, правильный ответ покажем чуть позже. ${base}`;
    return;
  }
  const mine = data.my_answer == null ? 'Вы не ответили.'
    : (q.scored && data.correct === data.my_answer) ? 'Верно!'
      : q.scored ? 'Мимо.' : 'Ваш вариант учтён.';
  answerHint.textContent = `${mine} ${base}`;
}

function renderBoard(data) {
  const medals = { 1: '🥇', 2: '🥈', 3: '🥉' };
  const rows = data.board || [];
  boardList.replaceChildren(...rows.map((row) => {
    const el = document.createElement('div');
    el.className = 'quiz-board-row' + (row.name === myName.textContent ? ' is-mine' : '');
    // место приехало с сервера: равный счёт = равное место, и у двух
    // лидеров на одном очке нет одного третьего места
    el.innerHTML = `<span class="qb-pos">${medals[row.rank] || row.rank}</span>
      <span class="qb-name">${escapeHTML(row.name)}</span>
      <span class="qb-score">${row.score}</span>`;
    return el;
  }));
  if (!rows.length) boardList.innerHTML = '<p class="muted">Никто не отвечал на зачётные вопросы.</p>';
  boardMe.textContent = data.my_score
    ? `У вас ${data.my_score} ${plural(data.my_score, 'очко', 'очка', 'очков')}.`
    : 'У вас пока нет очков.';
}

/* ---------------- Имя ---------------- */
renameBtn.addEventListener('click', () => {
  renameInput.value = myName.textContent;
  show(renameRow, true);
  renameInput.focus();
});
document.getElementById('renameCancel').addEventListener('click', () => show(renameRow, false));
renameRow.addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const res = await postForm(ev('/api/quiz/name'), { name: renameInput.value });
    myName.textContent = res.name;
    show(renameRow, false);
    showToast('Имя поменялось. Очки и ответы остались вашими.', 'ok', 3500);
  } catch (ex) {
    showToast(ex.message || 'Не вышло', 'warn');
  }
});

/* ---------------- Живой канал ---------------- */
async function refresh() {
  try {
    renderQuiz(await api(ev('/api/quiz')));
  } catch (ex) {
    if (/код приглашения/i.test(ex.message || '')) backToJoin();
  }
}

function backToJoin() {
  if (ws) { ws.close(); ws = null; }
  show(gameBox, false);
  show(joinBox, true);
}

function startLive() {
  joinForm.reset();
  if (codeInput.value) codeField.hidden = true;
  show(joinBox, false);
  show(gameBox, true);
  refresh();
  if (!ws) {
    ws = connectWS(ev('/ws/quiz'), {
      onStatus(st) {
        setPill(connPill, st, st === 'online' ? 'на связи' : st === 'offline' ? 'нет связи' : '…');
        if (st === 'online') refresh();   // за время обрыва стадия могла смениться
      },
      onMessage(msg) {
        if (msg.type === 'init') renderQuiz(msg.quiz);
        else if (msg.type === 'quiz_update') refresh();
        else if (msg.type === 'unauthorized') backToJoin();
      },
    });
  }
}

/* Первый вход: API сам скажет, есть ли билет. 403 — показываем форму. */
api(ev('/api/quiz')).then((data) => {
  api(ev('/api/config')).then((cfg) => {
    document.getElementById('eventTitle').textContent = cfg.event_title || 'Квиз';
    document.getElementById('eventSubtitle').textContent = cfg.event_subtitle || '';
  }).catch(() => {});
  startLive();
  renderQuiz(data);
}).catch(() => {
  show(joinBox, true);
  if (!codeInput.value) codeField.hidden = false;
});
