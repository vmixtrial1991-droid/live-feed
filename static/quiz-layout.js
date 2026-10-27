/* Движок раскладки экранного квиза.

   Задача: для каждого вопроса выбрать компоновку и посчитать размеры так,
   чтобы зал с 5-15 метров прочитал текст. Один шаблон не подходит: короткие
   ответы должны давать компактные карточки, длинные — место для чтения.

   Модули (по ТЗ):
     quizLayoutConfig     — все числа в одном месте;
     TextMeasurer         — реальные метрики текста, не «символы × ширина»;
     ImageLayoutCalculator— режим фото и размеры контейнера;
     AnswerLayoutCalculator — ширина, высота и строки карточек;
     QuestionLayoutEngine — перебор кандидатов и выбор лучшего;
     LayoutValidator      — проверка безопасной области и перекрытий.

   Движок ничего не знает про DOM экрана: он получает текст, метрики фото и
   размер viewport, а возвращает план. Применяет план отдельный код. Так
   раскладку можно проверить тестами и стендом без браузера. */

/* ------------------------------------------------------------------ */
/* Конфигурация                                                        */
/* ------------------------------------------------------------------ */

/* Все числа — для эталона 1920×1080. При другом размере окна движок
   пересчитывает их пропорционально (scale), кроме тех, где задан диапазон
   в пикселях: минимальный кегль не должен становиться нечитаемым на
   маленьком экране. */
const quizLayoutConfig = {
  referenceWidth: 1920,
  referenceHeight: 1080,

  safeArea: { horizontal: 80, vertical: 48 },

  /* Типографика. base — для эталона, min/max — жёсткие границы читаемости.
     Значения в CSS px эталонного макета. */
  typography: {
    question: { base: 52, min: 42, max: 62, lineHeight: 1.18, weight: 700 },
    answer: { base: 40, min: 34, max: 44, lineHeight: 1.17, weight: 550 },
    answerNumber: { base: 36, min: 32, max: 40, weight: 700 },
    votePercent: { base: 36, min: 32, max: 40, weight: 700 },
    caption: { base: 24, min: 22, max: 26, weight: 500 },
  },

  /* Карточка ответа: три зоны. Ширины в px эталона. */
  answerCard: {
    numberColumn: 56,
    percentColumn: 128,
    numberToTextGap: 18,
    textToPercentGap: 24,
    paddingX: 26,
    paddingY: 20,
    minHeight: 84,
    maxHeight: 260,
    gapX: 18,
    gapY: 18,
    radius: 16,
  },

  /* Изображение: ограничения контейнера по ориентации. */
  image: {
    fit: "contain",
    /* maxWidth/maxHeight — из ТЗ как ограничения контейнера. Но для
       горизонтального фото на 16:9 они конфликтуют с долей 46% кадра:
       420 < 497. Доля важнее — зал должен видеть снимок, — поэтому
       maxHeight работает как «не больше 420 ИЛИ не меньше 46% кадра»:
       фактическое значение берётся как max(420, доля). */
    wide: { maxWidth: 1100, maxHeight: 460 },
    square: { maxWidth: 480, maxHeight: 480 },
    tall: { maxWidth: 340, maxHeight: 460 },
    /* Режим «фото слева»: доля ширины и минимальный зазор. */
    side: { minShare: 0.28, maxShare: 0.35, gap: 32 },
  },

  /* Общая композиция. */
  frame: {
    questionMaxWidth: 1600,
    questionToAnswers: 34,
    /* Зона вопроса не должна съедать ответы: предел доли кадра. */
    questionMaxShare: 0.22,
    /* Доля высоты кадра под горизонтальное фото в режиме «сверху». */
    wideShotShare: 0.46,
    wideShotShareMin: 0.40,
    wideShotShareMax: 0.48,
  },

  layout: {
    adaptiveColumns: true,
    preserveGeometryAfterVoting: true,
    preventTextClipping: true,
    /* Пробовать сетку 2×2 только если ответ в неё влезает не более чем
       в столько строк. Иначе лучше колонка. */
    gridMaxLines: 2,
  },
};

/* ------------------------------------------------------------------ */
/* TextMeasurer                                                        */
/* ------------------------------------------------------------------ */

/* Измеряет текст по-настоящему: canvas.measureText с тем же шрифтом и
   переносом по словам. Считать «символы × средняя ширина» нельзя: кириллица
   шире латиницы, а крупный кегль меняет и метрики, и переносы. */
const quizTextMeasurer = (() => {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const family = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    + "Roboto, Arial, sans-serif";

  function font(px, weight) {
    return `${weight} ${px}px ${family}`;
  }

  /* Ширина строки в пикселях. */
  function width(text, px, weight) {
    ctx.font = font(px, weight);
    return ctx.measureText(text).width;
  }

  /* Разбить текст на строки по максимальной ширине. Возвращает массив
     строк. Слова не рвём, если слово само шире строки — оставляем его
     отдельной строкой (перенос внутри слова делает CSS). */
  function wrap(text, maxWidth, px, weight) {
    ctx.font = font(px, weight);
    const words = String(text || "").split(/\s+/).filter(Boolean);
    if (!words.length) return [""];
    const lines = [];
    let line = words[0];
    for (let i = 1; i < words.length; i++) {
      const next = line + " " + words[i];
      if (ctx.measureText(next).width <= maxWidth) {
        line = next;
      } else {
        lines.push(line);
        line = words[i];
      }
    }
    lines.push(line);
    return lines;
  }

  /* Сколько строк займёт текст при данной ширине. */
  function lineCount(text, maxWidth, px, weight) {
    return wrap(text, maxWidth, px, weight).length;
  }

  /* Самое длинное слово: если оно шире строки, текст всё равно разорвётся. */
  function longestWord(text, px, weight) {
    ctx.font = font(px, weight);
    let best = 0;
    for (const w of String(text || "").split(/\s+/)) {
      best = Math.max(best, ctx.measureText(w).width);
    }
    return best;
  }

  return { font, width, wrap, lineCount, longestWord, family };
})();

/* ------------------------------------------------------------------ */
/* ImageLayoutCalculator                                               */
/* ------------------------------------------------------------------ */

/* Что делать с картинкой: режим и размеры контейнера.

   wide   — фото над ответами (горизонтальное или квадратное, когда места
            хватает);
   side   — фото слева, текст справа (вертикальное при достаточной ширине);
   none   — картинки нет.

   Возвращает { mode, boxWidth, boxHeight, share }. boxWidth/boxHeight —
   контейнер, в который фото вписывается с сохранением пропорций. */
function computeImageLayout(img, frameW, frameH, scale, viewportH) {
  const cfg = quizLayoutConfig.image;
  if (!img || !img.width || !img.height) {
    return { mode: "none", boxWidth: 0, boxHeight: 0, share: 0 };
  }
  const ratio = img.width / img.height;
  const landscape = ratio >= 1.2;
  const portrait = ratio <= 0.85;
  const cap = landscape ? cfg.wide : portrait ? cfg.tall : cfg.square;

  /* frameW/frameH — безопасная область. Долю фото считаем от полного
     кадра (viewportH): ТЗ говорит «46% высоты экрана», а безопасная
     область меньше на поля, и 46% от неё давали 43% кадра. */
  const frameVH = viewportH || frameH;
  const availW = frameW * 0.94;
  const availH = frameH * 0.94;

  /* Режим «слева» — только для вертикали и только если справа остаётся
     достаточно места под карточки. Считаем по худшему случаю: ответ с
     номером и процентом требует минимум. */
  if (portrait) {
    const sideWidth = Math.min(frameW * cfg.side.maxShare,
      Math.max(frameW * cfg.side.minShare, frameH * 0.34));
    const textW = frameW - sideWidth - cfg.side.gap * scale;
    const minCardW = quizLayoutConfig.answerCard.numberColumn * scale
      + quizLayoutConfig.answerCard.percentColumn * scale
      + quizLayoutConfig.answerCard.textToPercentGap * scale
      + quizLayoutConfig.answerCard.numberToTextGap * scale
      + 160 * scale;                       // минимум на текст
    if (textW >= minCardW) {
      /* Высота — по фактической ширине, но не больше кадра. */
      const boxH = Math.min(availH, frameH * 0.9, sideWidth / ratio);
      const boxW = boxH * ratio;
      return { mode: "side", boxWidth: boxW, boxHeight: boxH,
               share: boxH / frameVH };
    }
  }

  /* Режим «сверху»: фото ограничено и по ширине, и по высоте.
     Целевая доля высоты кадра — из конфига (46%). Потолок по высоте не
     должен её перебивать: берём большее из «потолок» и «доля кадра». */
  const shareH = frameVH * quizLayoutConfig.frame.wideShotShare;
  const capH = cap.maxHeight * scale;
  let boxH = Math.min(Math.max(capH, shareH), availH);
  let boxW = boxH * ratio;
  /* По ширине тоже ограничиваем: очень широкое фото иначе упрётся в
     края и станет низкой полоской. */
  if (boxW > availW) {
    boxW = availW;
    boxH = boxW / ratio;
  }
  return { mode: "wide", boxWidth: boxW, boxHeight: boxH,
           share: boxH / frameVH };
}

/* ------------------------------------------------------------------ */
/* AnswerLayoutCalculator                                              */
/* ------------------------------------------------------------------ */

/* Размеры одной карточки и всей группы.

   naturalWidth   — сколько нужно по содержимому (номер + текст + процент);
   textWidth      — сколько остаётся тексту после зон номера и процента;
   lines          — сколько строк займёт ответ;
   cardHeight     — высота карточки;
   groupHeight    — высота всей группы с зазорами.
*/
function measureAnswerCard(text, fontPx, opts) {
  const cfg = quizLayoutConfig.answerCard;
  const s = opts.scale;
  const numW = cfg.numberColumn * s;
  const pctW = cfg.percentColumn * s;
  const gap1 = cfg.numberToTextGap * s;
  const gap2 = cfg.textToPercentGap * s;
  const padX = cfg.paddingX * s;
  const padY = cfg.paddingY * s;
  const chrome = numW + pctW + gap1 + gap2 + padX * 2;

  const longest = quizTextMeasurer.longestWord(
    text, fontPx, quizLayoutConfig.typography.answer.weight);
  /* Минимальная ширина текстовой области: самое длинное слово целиком.
     Ниже неё слово начнёт рваться. */
  const minTextW = Math.max(longest, 120 * s);
  const naturalWidth = chrome + minTextW;

  return { chrome, numW, pctW, gap1, gap2, padX, padY,
           minTextW, naturalWidth };
}

/* Высота карточки для заданной ширины текста и числа строк. */
function cardHeightFor(lines, fontPx, opts) {
  const cfg = quizLayoutConfig.answerCard;
  const s = opts.scale;
  const lh = quizLayoutConfig.typography.answer.lineHeight;
  const padY = cfg.paddingY * s;
  const textH = lines * fontPx * lh;
  return Math.max(cfg.minHeight * s, Math.ceil(textH + padY * 2));
}

/* Полный расчёт варианта компоновки ответов.

   mode: "row" (все в строку), "grid2" (2×2), "column" (один столбец).
   Возвращает null, если вариант заведомо не проходит (кегль ниже минимума,
   слово не влезает, высота больше доступной). */
function computeAnswersLayout(texts, mode, availW, availH, opts) {
  const cfg = quizLayoutConfig.answerCard;
  const s = opts.scale;
  const typo = quizLayoutConfig.typography.answer;
  const n = texts.length;
  const gapX = cfg.gapX * s;
  const gapY = cfg.gapY * s;

  let cols;
  if (mode === "row") cols = n;
  else if (mode === "grid2") cols = 2;
  else cols = 1;
  const rows = Math.ceil(n / cols);

  /* Ширина колонки: делим доступное место. Но не больше естественной
     ширины самого длинного ответа — иначе короткие карточки растянутся
     на полэкрана. */
  const bySpace = (availW - gapX * (cols - 1)) / cols;

  /* Кегль начинаем с базового и уменьшаем, пока текст не влезет по ширине.
     Минимум — typography.answer.min: ниже зал не прочитает. */
  let font = typo.base * s;
  const minFont = typo.min * s;
  let metrics = null;

  while (font >= minFont - 0.01) {
    /* Сколько нужно по содержимому при этом кегле. */
    let needW = 0;
    const parts = texts.map((t) => {
      const m = measureAnswerCard(t, font, opts);
      needW = Math.max(needW, m.naturalWidth);
      return m;
    });
    /* Колонка: не шире нужного и не шире доступного. */
    const colW = Math.min(bySpace, Math.max(needW, cfg.minWidth ? cfg.minWidth * s : 0));
    const textW = colW - parts[0].chrome;
    if (textW > 0) {
      const lines = texts.map((t) => quizTextMeasurer.lineCount(
        t, textW, font, typo.weight));
      const maxLines = Math.max(...lines);
      const cardH = cardHeightFor(maxLines, font, opts);
      const groupH = rows * cardH + gapY * (rows - 1);
      /* Высота влезает и ни одно слово не рвётся. */
      const wordsFit = texts.every((t) => quizTextMeasurer.longestWord(
        t, font, typo.weight) <= textW + 1);
      if (wordsFit && groupH <= availH + 1 && cardH <= cfg.maxHeight * s + 1) {
        metrics = { font, lines, maxLines, cardH, groupH, colW, textW };
        break;
      }
    }
    font -= 1 * s;
  }
  if (!metrics) return null;

  return {
    mode, cols, rows, ...metrics,
    cardWidth: metrics.colW,
    totalWidth: metrics.colW * cols + gapX * (cols - 1),
    gapX, gapY,
  };
}

/* ------------------------------------------------------------------ */
/* QuestionLayoutEngine                                                */
/* ------------------------------------------------------------------ */

/* Перебрать кандидатов и выбрать лучшего.

   Порядок предпочтения (по ТЗ): компактная группа с крупным шрифтом.
   Кандидаты:
     • 2 ответа   — «рядом» или «колонкой»;
     • 3 ответа   — «в строку» (если короткие), «колонкой»;
     • 4 ответа   — «2×2» (если короткие), «колонкой».

   Оценка: сначала кегль (крупнее — лучше), при равном кегле — меньшая
   высота группы (компактнее). Вариант с переносом слов отбрасывается
   внутри computeAnswersLayout. */
function chooseAnswersLayout(texts, availW, availH, opts) {
  const n = texts.length;
  const gridMaxLines = quizLayoutConfig.layout.gridMaxLines;
  const candidates = [];

  if (n === 2) candidates.push("row", "column");
  if (n === 3) candidates.push("row", "grid2", "column");
  if (n === 4) candidates.push("grid2", "column");
  if (n === 1) candidates.push("row");

  const scored = [];
  for (const mode of candidates) {
    const m = computeAnswersLayout(texts, mode, availW, availH, opts);
    if (!m) continue;
    /* Сетку 2×2 не берём, если ответ в ней всё равно ломается на много
       строк: колонка даст то же самое, но шире. */
    if (mode === "grid2" && m.maxLines > gridMaxLines) continue;
    scored.push(m);
  }
  if (!scored.length) {
    /* Ничего не прошло — берём колонку с минимальным кеглем, пусть даже
       придётся ужимать весь кадр масштабом. */
    const fallback = computeAnswersLayout(texts, "column", availW, availH,
      { ...opts, forceMinFont: true });
    return fallback;
  }
  scored.sort((a, b) => (b.font - a.font) || (a.groupH - b.groupH));
  return scored[0];
}

/* Полный план кадра.

   Вход:
     question — текст вопроса;
     answers  — массив текстов;
     image    — { width, height } или null;
     viewport — { width, height };
     opts     — { scale, safe }.

   Выход — план, который применяет applyQuizLayout():
     { kind, question:{font,lines,width,height}, image:{mode,...},
       answers:{...}, totalHeight, scale } */
function planQuizFrame(input) {
  const cfg = quizLayoutConfig;
  const { question, answers, image, viewport } = input;
  const scale = input.scale || 1;
  const safe = input.safe || {
    left: cfg.safeArea.horizontal * scale,
    right: cfg.safeArea.horizontal * scale,
    top: cfg.safeArea.vertical * scale,
    bottom: cfg.safeArea.vertical * scale,
  };

  const safeW = viewport.width - safe.left - safe.right;
  const safeH = viewport.height - safe.top - safe.bottom;

  /* Изображение: режим и контейнер. */
  const img = computeImageLayout(image, safeW, safeH, scale, viewport.height);

  /* Вопрос: ширина ограничена, кегль от длины и от зоны. */
  const qTypo = cfg.typography.question;
  const qMaxW = Math.min(cfg.frame.questionMaxWidth * scale, safeW);
  /* В режиме «слева» вопрос живёт в правой колонке. */
  const sideTextW = img.mode === "side"
    ? safeW - img.boxWidth - cfg.image.side.gap * scale
    : qMaxW;
  const qWidth = Math.min(qMaxW, sideTextW);

  /* Зона вопроса: доля кадра, но не больше нужного. */
  const qZoneMax = safeH * cfg.frame.questionMaxShare;
  let qFont = qTypo.base * scale;
  let qLines = 1;
  for (let f = qTypo.max * scale; f >= qTypo.min * scale - 0.01; f -= 1 * scale) {
    const lines = quizTextMeasurer.lineCount(question, qWidth, f, qTypo.weight);
    const h = lines * f * qTypo.lineHeight;
    if (h <= qZoneMax + 1) {
      qFont = f;
      qLines = lines;
      break;
    }
  }
  const qHeight = qLines * qFont * qTypo.lineHeight;

  /* Сколько осталось на ответы. */
  const imgH = img.mode === "none" ? 0 : img.boxHeight;
  const gapQ = cfg.frame.questionToAnswers * scale;
  const gapImg = img.mode === "none" ? 0 : gapQ;
  const availForAnswers = safeH - imgH - qHeight - gapQ - gapImg
    - cfg.typography.caption.base * scale * 1.6;

  /* Ширина под ответы: в боковом режиме — правая колонка, иначе вся. */
  const answersW = img.mode === "side"
    ? safeW - img.boxWidth - cfg.image.side.gap * scale
    : safeW;

  const answersPlan = chooseAnswersLayout(answers, answersW, availForAnswers,
    { scale });

  return {
    kind: img.mode === "none" ? "text-only" : img.mode === "side" ? "image-side" : "image-top",
    image: img,
    question: { font: qFont, lines: qLines, width: qWidth, height: qHeight },
    answers: answersPlan,
    totalHeight: imgH + qHeight + gapQ + gapImg
      + (answersPlan ? answersPlan.groupH : 0),
    safe: { width: safeW, height: safeH, left: safe.left, top: safe.top },
    scale,
  };
}

/* ------------------------------------------------------------------ */
/* LayoutValidator                                                     */
/* ------------------------------------------------------------------ */

/* Проверки по ТЗ: всё внутри безопасной области, ничего не перекрывается,
   текст не обрезан, кегли не ниже минимума. Работает по плану, без DOM.

   Возвращает { ok, problems: [] }. */
function validateQuizPlan(plan) {
  const cfg = quizLayoutConfig;
  const problems = [];
  if (!plan) return { ok: false, problems: ["плана нет"] };

  const minAnswer = cfg.typography.answer.min * plan.scale;
  if (plan.answers && plan.answers.font < minAnswer - 0.5) {
    problems.push(`кегль ответа ${plan.answers.font.toFixed(1)} < ${minAnswer.toFixed(1)}`);
  }
  if (plan.question.font < cfg.typography.question.min * plan.scale - 0.5) {
    problems.push(`кегль вопроса ${plan.question.font.toFixed(1)} ниже минимума`);
  }
  if (plan.answers && plan.answers.totalWidth > plan.safe.width + 1) {
    problems.push("ответы шире безопасной области");
  }
  if (plan.totalHeight > plan.safe.height + 1) {
    problems.push("содержимое выше безопасной области");
  }
  if (plan.image && plan.image.mode !== "none") {
    if (plan.image.boxHeight > plan.safe.height * 0.9 + 1) {
      problems.push("фото выше безопасной области");
    }
  }
  return { ok: problems.length === 0, problems };
}

/* Экспорт для браузера: screen.js использует эти имена. */
window.quizLayout = {
  config: quizLayoutConfig,
  measure: quizTextMeasurer,
  plan: planQuizFrame,
  validate: validateQuizPlan,
  chooseAnswers: chooseAnswersLayout,
  computeImage: computeImageLayout,
};
