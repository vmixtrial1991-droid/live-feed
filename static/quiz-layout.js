/* Движок раскладки экранного квиза — по ТЗ «Вёрстка экрана квиза».

   Задача: любой вопрос с 2–4 ответами и картинкой любой ориентации должен
   читаться с дальних рядов, а короткий ответ не должен растягиваться на
   пол-экрана. Один шаблон этого не даёт: размеры зависят от текста, картинки
   и пропорций холста.

   Устройство:

     quizLayoutConfig   — все числа в одном месте (см. раздел 6 ТЗ);
     quizTextMeasurer   — реальные метрики через canvas.measureText;
     computeFrame       — виртуальный холст, поля, область контента;
     classifyImage      — landscape / square / portrait;
     chooseLayout       — A / B / C по разделу 4;
     chooseAnswersLayout— сколько колонок у ответов при этой схеме;
     planQuizFrame      — кегли, карточки, картинка, подгонка (раздел 7);
     validateQuizPlan   — проверки области контента, кеглей и строк.

   Движок не знает про DOM экрана: получает текст, размеры фото и viewport,
   возвращает план. Применяет план screen.js. Так раскладку можно проверить
   стендом и тестами без вёрстки.

   Всё считается в виртуальных пикселях: ширина холста всегда 1920, на более
   высоком экране растёт только высота. Поэтому кегли от пропорции экрана не
   зависят — 16:9, 16:10, 5:3 и 4:3 дают одинаковые шрифты. */

/* ------------------------------------------------------------------ */
/* Конфигурация                                                        */
/* ------------------------------------------------------------------ */

const quizLayoutConfig = {
  referenceWidth: 1920,
  referenceHeight: 1080,

  /* Типографика. Числа — виртуальные px, стартовая точка из таблиц
     раздела 6; окончательный размер даёт подгонка (раздел 7). */
  typography: {
    question: {
      weight: 700,
      lineHeight: 1.2,
      min: 32,
      /* [максимум символов, кегль] — первая подходящая строка таблицы. */
      table: [[40, 64], [80, 56], [140, 48], [220, 40], [320, 34], [Infinity, 32]],
      withImage: [[40, 56], [80, 48], [140, 42], [220, 36], [320, 32], [Infinity, 32]],
    },
    answer: {
      weight: 600,
      lineHeight: 1.2,
      min: 28,
      table: [[15, 44], [30, 40], [50, 36], [80, 32], [Infinity, 28]],
      /* Если ответов четыре и есть картинка — на 4 px меньше. */
      fourWithImagePenalty: 4,
      /* Кегль ответа всегда не больше 0.9 кегля вопроса. */
      maxOfQuestion: 0.9,
    },
    /* Подпись «ответили» под карточками. Она есть в кадре всегда, поэтому
       её высота обязана входить в расчёт: без неё содержимое вылезало за
       нижнюю границу ровно на её высоту. */
    caption: { font: 24, lineHeight: 1.4 },
  },

  /* Карточка ответа. Все размеры в em от кегля ответа — так карточка
     масштабируется вместе с текстом и её внутренности не «плывут».
     overhead = 0.7*2 + 1.4 + 0.6 + 0.6 + 3.2 = 7.2em (раздел 5.1). */
  answerCard: {
    paddingX: 0.7,
    paddingY: 0.5,
    numberColumn: 1.4,
    numberToTextGap: 0.6,
    textToPercentGap: 0.6,
    percentColumn: 3.2,
    minTextEm: 8,
    minHeightEm: 2.2,
  },

  /* Композиция и пределы раскладок. */
  layout: {
    /* Поля по 5% с каждой стороны дают область контента 90% кадра. Это
       запасной расчёт: обычно экран присылает фактическую область, а CSS
       задаёт поля сам (см. LAYOUT.md, раздел 2). */
    contentShare: 0.9,
    /* Шире 16:9 контент не разъезжается: 1920 виртуальных px по центру. */
    contentMaxWidth: 1920,

    gapBase: 32,
    gapSlope: 0.04,
    gapMax: 48,
    cardGap: 20,
    cardGapGrid: 24,
    sideGap: 64,

    /* Вопрос. */
    questionMaxWidthA: 0.75,
    questionMaxShareA: 0.30,
    questionMaxShareB: 0.16,
    questionMaxLinesB: 3,

    /* Ответы: предел ширины группы в схемах A и B. */
    answersMaxWidth: 0.70,

    /* Картинка. */
    imageUpscaleMax: 1.5,
    imageWidthShareB: 0.80,
    panoramaRatio: 2.2,
    imageMinShareB: 0.30,
    imageMaxShareSquareB: 0.45,
    imagePortraitWidthShare: 0.30,
    imageSquareWidthShare: 0.36,

    /* Квадрат уходит в «две колонки» только на достаточно широком холсте. */
    squareSideRatio: 1.55,

    /* Лимиты строк: при крупном кегле строк меньше — мельче кегль,
       строк можно больше (раздел 6). */
    questionLines: { big: 4, small: 6, bigFrom: 40 },
    answerLines: { big: 2, small: 3, bigFrom: 36 },

    /* Подгонка: шаг и число итераций (раздел 7). */
    fontStep: 2,
    maxIterations: 20,
    tolerance: 1,

    /* Рост шрифта в схеме A. По ТЗ (§1, §4.4) шрифты выше таблицы не
       растут, и кадр просто центрируется. На практике без картинки это
       даёт пустой экран: короткий вопрос занимает четверть высоты, и зал
       с дальних рядов видит мелкий текст. Поэтому в A шрифты растут — но
       только до потолка и только по содержимому.

       Рост считается по эталонной области (1920×1080, Ch = 972), а не по
       фактической: иначе на 4:3, где места больше, шрифт вышел бы крупнее,
       чем на 16:9, а ТЗ требует одинаковых кеглей на 16:9, 16:10, 5:3 и
       4:3. Виртуальная ширина области везде одна и та же (1728), так что
       от холста результат не зависит.

       Заполнение 0.75 от эталонной высоты — с запасом: фактическая высота
       области на 16:9 меньше эталонной (шапка вопроса), и без запаса
       подгонка тут же ужимала бы шрифт обратно. */
    grow: {
      layouts: ["A"],
      max: 1.4,
      step: 2,
      maxSteps: 60,
      referenceHeight: 972,
      fill: 0.75,
    },
  },
};

/* Потолок роста в виртуальных px: стартовый кегль из таблицы, умноженный
   на предел роста. Считается от таблицы, а не от «сколько влезет», поэтому
   не зависит от пропорции экрана. */
function growCap(tableFont) {
  return tableFont * quizLayoutConfig.layout.grow.max;
}

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

  /* Ширина строки. */
  function width(text, px, weight) {
    ctx.font = font(px, weight);
    return ctx.measureText(text).width;
  }

  /* Разбить текст на строки по максимальной ширине. Слова не рвём; слово,
     которое само шире строки, остаётся отдельной строкой — CSS его перенесёт
     внутри, и это ловит validateQuizPlan по числу строк. */
  function wrap(text, maxWidth, px, weight) {
    ctx.font = font(px, weight);
    const words = String(text || "").split(/\s+/).filter(Boolean);
    if (!words.length) return [""];
    const lines = [];
    let line = words[0];
    for (let i = 1; i < words.length; i++) {
      const next = line + " " + words[i];
      if (ctx.measureText(next).width <= maxWidth) line = next;
      else { lines.push(line); line = words[i]; }
    }
    lines.push(line);
    return lines;
  }

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
/* Холст                                                               */
/* ------------------------------------------------------------------ */

/* Масштаб вывода: k = min(W / 1920, H / 1080).

   На 16:9 и более узком холсте решает ширина, и виртуальная ширина всегда
   1920. На экране шире 16:9 опорной становится высота: иначе кегли выросли
   бы вместе с шириной, а контент всё равно ограничен 1920 px по центру
   (ТЗ, раздел 2.2). */
function computeScale(width, height) {
  const cfg = quizLayoutConfig;
  const ratio = width / height;
  const reference = cfg.referenceWidth / cfg.referenceHeight;
  const k = Math.min(width / cfg.referenceWidth, height / cfg.referenceHeight);
  return { k, wide: ratio > reference + 1e-6, ratio };
}

/* Геометрия холста: виртуальные размеры, поля и область контента. */
function computeFrame(viewport, usableHeight, warnings, usableWidth) {
  const cfg = quizLayoutConfig;
  const width = Math.max(1, viewport.width);
  const height = Math.max(1, viewport.height);
  const { k, wide, ratio } = computeScale(width, height);

  const Wv = width / k;
  const Hv = height / k;

  /* Область контента. Экран присылает её фактическими размерами — так
     расчёт совпадает с тем, что реально видно: поля, шапку вопроса и
     отступы блока считает CSS, и повторять их здесь в уме значит
     разойтись с ним на несколько пикселей.

     Если экран размеров не прислал, берём их из ТЗ: поля 5%, ширина не
     больше 1920 виртуальных px. */
  const fallbackW = Math.min(Wv * cfg.layout.contentShare,
    cfg.layout.contentMaxWidth);
  const fallbackH = Hv * cfg.layout.contentShare;
  const Cw = usableWidth > 0 ? Math.min(fallbackW, usableWidth / k) : fallbackW;
  const Ch = usableHeight > 0 ? Math.min(fallbackH, usableHeight / k) : fallbackH;

  const gap = Math.min(cfg.layout.gapMax,
    cfg.layout.gapBase + (Hv - cfg.referenceHeight) * cfg.layout.gapSlope);

  /* Пропорция вне 4:3 – 16:9: работаем, но говорим об этом. */
  if (ratio < 4 / 3 - 1e-3 || ratio > 16 / 9 + 1e-3) {
    warnings.push(`пропорция холста ${ratio.toFixed(2)} вне 4:3–16:9`);
  }
  if (wide) {
    warnings.push(`холст шире 16:9: контент ${Math.round(Cw)} px по центру`);
  }

  return { k, wide, ratio, Wv, Hv, Cw, Ch, gap, viewport: { width, height } };
}

/* ------------------------------------------------------------------ */
/* Картинка                                                            */
/* ------------------------------------------------------------------ */

/* Тип снимка по пропорции (раздел 3). */
function classifyImage(image) {
  if (!image || !image.width || !image.height) return "none";
  const r = image.width / image.height;
  if (r >= 1.2) return "landscape";
  if (r <= 0.83) return "portrait";
  return "square";
}

/* Схема кадра по разделу 4. */
function chooseLayout(image, frame) {
  const cfg = quizLayoutConfig.layout;
  const kind = classifyImage(image);
  if (kind === "none") return "A";
  if (kind === "landscape") return "B";
  if (kind === "portrait") return "C";
  /* Квадрат: на 16:9, 16:10 и 5:3 — две колонки, на 4:3 — стек. */
  return frame.ratio >= cfg.squareSideRatio ? "C" : "B";
}

/* Сколько колонок у ответов в этой схеме (раздел 4).
   A: 2–3 — колонка, 4 — сетка 2×2;
   B: 2 — в один ряд, 3 — колонка, 4 — сетка 2×2;
   C: всегда колонка. */
function chooseAnswersLayout(layout, count) {
  if (layout === "C") return { mode: "column", cols: 1 };
  if (layout === "B" && count === 2) return { mode: "row", cols: 2 };
  if (count === 4) return { mode: "grid2", cols: 2 };
  return { mode: "column", cols: 1 };
}

/* Вписать снимок в бокс с сохранением пропорций.
   Больше чем в 1.5× от оригинала не увеличиваем — иначе картинка мылится.
   Возвращает размеры в виртуальных px. */
function fitImage(image, boxW, boxH, k) {
  const maxW = Math.min(boxW,
    quizLayoutConfig.layout.imageUpscaleMax * image.width / k);
  const maxH = Math.min(boxH,
    quizLayoutConfig.layout.imageUpscaleMax * image.height / k);
  const r = image.width / image.height;
  let w = Math.min(maxW, maxH * r);
  let h = w / r;
  if (h > maxH) { h = maxH; w = h * r; }
  return { width: Math.max(0, w), height: Math.max(0, h), ratio: r };
}

/* ------------------------------------------------------------------ */
/* Кегли                                                               */
/* ------------------------------------------------------------------ */

/* Кегль по таблице «символы → кегль». */
function tableFont(table, textLength) {
  for (const [limit, size] of table) {
    if (textLength <= limit) return size;
  }
  return table[table.length - 1][1];
}

/* Стартовые кегли из таблиц раздела 6 с поправками. */
function startFonts(question, answers, image) {
  const typo = quizLayoutConfig.typography;
  const hasImage = classifyImage(image) !== "none";
  const qLen = String(question || "").length;
  const aLen = answers.reduce((m, t) => Math.max(m, String(t || "").length), 0);

  const qTable = hasImage ? typo.question.withImage : typo.question.table;
  let q = tableFont(qTable, qLen);
  let a = tableFont(typo.answer.table, aLen);
  if (hasImage && answers.length === 4) {
    a -= typo.answer.fourWithImagePenalty;
  }
  q = Math.max(q, typo.question.min);
  a = Math.max(a, typo.answer.min);
  a = Math.min(a, q * typo.answer.maxOfQuestion);
  return { q, a };
}

/* ------------------------------------------------------------------ */
/* Карточки ответов                                                    */
/* ------------------------------------------------------------------ */

/* Сколько пикселей занимает всё, кроме текста: паддинги, бейдж, зазоры,
   процент и рамка.

   Рамка входит в border-box, поэтому её ширина отнимает место у текста
   ровно так же, как паддинг. Без неё расчёт давал зону текста на 2px шире
   настоящей, и слово, помещавшееся «впритык», в браузере уходило на вторую
   строку — карточка вырастала уже после расчёта. */
const CARD_BORDER_PX = 2;

function cardOverhead(fontPx) {
  const c = quizLayoutConfig.answerCard;
  return (c.paddingX * 2 + c.numberColumn + c.numberToTextGap
    + c.textToPercentGap + c.percentColumn) * fontPx + CARD_BORDER_PX;
}

/* Карточка при заданном кегле и пределе ширины (раздел 5.1).

   cardW = overhead + clamp(ширина самого длинного ответа в одну строку,
                            8em, maxTextW)

   То есть короткое «Да» даёт компактную карточку, а длинный ответ упирается
   в предел группы и переносится по словам. */
function measureCards(texts, fontPx, maxGroupW, cols) {
  const cfg = quizLayoutConfig.answerCard;
  const typo = quizLayoutConfig.typography.answer;
  const overhead = cardOverhead(fontPx);

  /* Предел одной карточки: если колонок две — половина группы минус зазор. */
  const maxCardW = cols > 1
    ? (maxGroupW - quizLayoutConfig.layout.cardGapGrid * (cols - 1)) / cols
    : maxGroupW;

  let widestLine = 0;
  for (const t of texts) {
    widestLine = Math.max(widestLine,
      quizTextMeasurer.width(String(t || ""), fontPx, typo.weight));
  }
  /* Запас на перенос: canvas отдаёт ширину строки, а браузер решает по
     своей сетке и с `text-wrap: pretty` может увести слово на строку ниже.
     Без запаса ответ ровно в ширину зоны давал две строки в кадре и одну
     в расчёте — карточка вырастала и вытесняла подпись.

     Запас именно добавляется к нужной ширине и остаётся в текстовой зоне:
     cardW → зона текста, а строки считаются по этой зоне. Раньше он
     вычитался из ширины при подсчёте строк, то есть съедался самим
     расчётом и ни от чего не защищал. */
  const wrapSafety = Math.max(2, fontPx * 0.06);
  const wantTextW = Math.max(widestLine + wrapSafety, cfg.minTextEm * fontPx);
  const cardW = Math.min(maxCardW, overhead + wantTextW);
  const innerTextW = Math.max(1, cardW - overhead);
  const maxTextW = Math.max(0, maxCardW - overhead);

  const lines = texts.map((t) => quizTextMeasurer.lineCount(
    t, innerTextW, fontPx, typo.weight));
  /* Высота карточки: строки текста, вертикальные паддинги и рамка 2px.
     Рамка входит в border-box, и без неё расчёт был на 2px ниже каждой
     карточки — на трёх ответах это давало 5px переполнения кадра. */
  const heights = lines.map((n) => Math.max(
    cfg.minHeightEm * fontPx,
    n * fontPx * typo.lineHeight + cfg.paddingY * 2 * fontPx + 2));

  return {
    cardW, textW: innerTextW, lines, heights, overhead, maxCardW,
  };
}

/* Собрать группу карточек: строки, высоты рядов, общая высота.

   По ТЗ (§5.1) по высоте выравнивается ряд: в сетке 2×2 карточки одной
   строки становятся одной высоты, а в колонке каждая карточка — свой ряд и
   растёт по своему тексту. Поэтому высоту задаёт grid, а не общая цифра:
   одна высота на всю колонку резала ответ, который браузер переносил на
   строку больше расчётного. */
function buildGroup(cards, count, cols) {
  const cfg = quizLayoutConfig.layout;
  const rows = Math.ceil(count / cols);
  const gapX = cols > 1 ? cfg.cardGapGrid : 0;
  const gapY = cfg.cardGap;
  const rowHeights = [];
  for (let r = 0; r < rows; r++) {
    let h = 0;
    for (let c = 0; c < cols; c++) {
      const i = r * cols + c;
      if (i < count) h = Math.max(h, cards.heights[i]);
    }
    rowHeights.push(h);
  }
  const groupH = rowHeights.reduce((s, h) => s + h, 0) + gapY * (rows - 1);
  const groupW = cards.cardW * cols + gapX * (cols - 1);
  return {
    rows, cols, gapX, gapY, rowHeights, groupH, groupW,
    cardW: cards.cardW, textW: cards.textW, lines: cards.lines,
    heights: cards.heights,
  };
}

/* ------------------------------------------------------------------ */
/* Один вариант кадра                                                  */
/* ------------------------------------------------------------------ */

/* Посчитать кадр при заданных кеглях. Возвращает геометрию и проблемы:
   пустой список проблем значит «влезает».

   Здесь же считается картинка: в схеме B ей достаётся всё, что осталось
   по высоте (не меньше 30% Ch), в схеме C — левый бокс по лимитам раздела
   4.3, в схеме A её нет. */
function evaluateFrame(input, fonts, frame) {
  const cfg = quizLayoutConfig;
  const L = cfg.layout;
  const hasImage = input.image && input.image.width && input.image.height;
  const layout = chooseLayout(input.image, frame);
  const answers = (input.answers || []).filter((t) => String(t || "").trim());
  const count = answers.length || 1;
  const arrangement = chooseAnswersLayout(layout, count);
  const problems = [];

  /* Ширина вопроса и ответов. Схема C отдаёт всю ширину правой колонке:
     ширина полосы под картинку считается ниже, но вопрос и ответы живут
     справа, поэтому считаем от полного контента минус минимум под снимок. */
  let qWidth = frame.Cw;
  let answersMaxW = frame.Cw;
  let imageBox = null;

  if (layout === "A") {
    qWidth = Math.min(frame.Cw, frame.Wv * L.questionMaxWidthA);
    answersMaxW = Math.min(frame.Cw, frame.Wv * L.answersMaxWidth);
  } else if (layout === "B") {
    /* Ответы во всю ширину контента, если они в ряд: двум карточкам нужна
       ширина. Колонкой — предел 70%, иначе короткий ответ растянется. */
    qWidth = frame.Cw;
    answersMaxW = arrangement.cols > 1
      ? frame.Cw
      : Math.min(frame.Cw, frame.Wv * L.answersMaxWidth);
  } else {
    /* Первая оценка полосы под снимок — по лимиту ширины. Точная высота
       картинки от текста не зависит, поэтому считаем её сразу. */
    const kind = classifyImage(input.image);
    const widthShare = kind === "square"
      ? L.imageSquareWidthShare : L.imagePortraitWidthShare;
    const boxW = frame.Wv * widthShare;
    const fitted = fitImage(input.image, boxW, frame.Ch, frame.k);
    imageBox = { ...fitted, boxW, boxH: frame.Ch };
    qWidth = Math.max(frame.Cw * 0.35,
      frame.Cw - imageBox.width - L.sideGap);
    answersMaxW = qWidth;
  }

  /* Вопрос. */
  const typo = cfg.typography.question;
  const qLines = quizTextMeasurer.wrap(input.question, qWidth,
    fonts.q, typo.weight);
  const qHeight = qLines.length * fonts.q * typo.lineHeight;

  /* Предел строк вопроса: крупный кегль — 4 строки, мелкий — 6. */
  const qLineLimit = fonts.q >= L.questionLines.bigFrom
    ? L.questionLines.big : L.questionLines.small;
  if (qLines.length > qLineLimit) {
    problems.push(`строк вопроса ${qLines.length} > ${qLineLimit}`);
  }
  if (layout === "A" && qHeight > frame.Ch * L.questionMaxShareA + L.tolerance) {
    problems.push("вопрос выше 30% области");
  }
  if (layout === "B") {
    if (qHeight > frame.Ch * L.questionMaxShareB + L.tolerance) {
      problems.push("вопрос выше 16% области");
    }
    if (qLines.length > L.questionMaxLinesB) {
      problems.push(`строк вопроса ${qLines.length} > ${L.questionMaxLinesB}`);
    }
  }

  /* Ответы. */
  const cards = measureCards(answers, fonts.a, answersMaxW,
    arrangement.cols);
  const group = buildGroup(cards, count, arrangement.cols);
  const aLineLimit = fonts.a >= L.answerLines.bigFrom
    ? L.answerLines.big : L.answerLines.small;
  const worstLines = Math.max(...group.lines);
  if (worstLines > aLineLimit) {
    problems.push(`строк ответа ${worstLines} > ${aLineLimit}`);
  }
  /* Слово шире текстовой зоны — оно порвётся внутри. Это видно в кадре,
     поэтому считаем ошибкой раскладки, а не мелочью. */
  const brokenWord = answers.some((t) => quizTextMeasurer.longestWord(
    t, fonts.a, cfg.typography.answer.weight) > group.textW + 1);
  if (brokenWord) problems.push("слово ответа шире карточки");

  /* Картинка и итоговая высота. Подпись «ответили» стоит под карточками и
     входит в сумму: без неё расчёт «влезает» сходился, а на деле подпись
     уезжала за нижнюю границу.

     В схеме C вопрос и ответы живут в правой колонке отдельно от снимка,
     поэтому её высота считается сама по себе, а общая — как большая из
     двух: колонки стоят рядом, а не друг под другом. */
  const captionH = cfg.typography.caption.font * cfg.typography.caption.lineHeight;
  const textColumnH = qHeight + frame.gap + group.groupH + frame.gap + captionH;
  let imageHeight = 0;
  let totalHeight = 0;
  if (layout === "A") {
    totalHeight = textColumnH;
  } else if (layout === "B") {
    const free = frame.Ch - qHeight - group.groupH - captionH - frame.gap * 3;
    const maxW = input.image.width / input.image.height > L.panoramaRatio
      ? frame.Cw : frame.Cw * L.imageWidthShareB;
    const fitted = fitImage(input.image, maxW, Math.max(0, free), frame.k);
    imageHeight = fitted.height;
    /* Квадрат на 4:3 не должен занимать больше 45% области: под ним ещё
       ответы. */
    if (classifyImage(input.image) === "square"
      && imageHeight > frame.Ch * L.imageMaxShareSquareB + L.tolerance) {
      const capped = frame.Ch * L.imageMaxShareSquareB;
      imageHeight = capped;
      imageBox = { ...fitted, height: capped, width: capped * fitted.ratio };
    } else {
      imageBox = fitted;
    }
    imageBox.boxH = Math.max(0, free);
    if (free + L.tolerance < frame.Ch * L.imageMinShareB) {
      problems.push("картинке остаётся меньше 30% области");
    }
    totalHeight = qHeight + frame.gap + imageHeight + frame.gap
      + group.groupH + frame.gap + captionH;
  } else {
    imageHeight = imageBox.height;
    totalHeight = Math.max(imageHeight, textColumnH);
    if (imageHeight > frame.Ch + L.tolerance) {
      problems.push("фото выше области контента");
    }
    if (textColumnH > frame.Ch + L.tolerance) {
      problems.push("правая колонка выше области контента");
    }
  }

  if (totalHeight > frame.Ch + L.tolerance) {
    problems.push(`содержимое ${Math.round(totalHeight)} > области `
      + `${Math.round(frame.Ch)}`);
  }
  if (group.groupW > frame.Cw + L.tolerance) {
    problems.push("ответы шире области контента");
  }

  return {
    layout, arrangement, problems,
    question: {
      font: fonts.q, lines: qLines, width: qWidth, height: qHeight,
      textWidth: qWidth,
    },
    answers: {
      ...group, font: fonts.a, count,
      maxWidth: answersMaxW,
    },
    image: imageBox,
    imageHeight,
    totalHeight,
    contentWidth: layout === "C"
      ? imageBox.width + L.sideGap + group.groupW
      : Math.max(qWidth, group.groupW),
  };
}

/* ------------------------------------------------------------------ */
/* Подгонка (раздел 7)                                                 */
/* ------------------------------------------------------------------ */

/* Обрезать самый длинный текст с «…» — шаг 3е: минимумы достигнуты, а кадр
   всё равно не сходится. Что именно обрезали, попадает в предупреждения. */
function truncateLongest(question, answers, warnings) {
  const q = String(question || "");
  const list = answers.map((t) => String(t || ""));
  const longest = list.reduce((m, t, i) => (t.length > list[m].length ? i : m), 0);
  if (list.length && list[longest].length > 3
    && list[longest].length >= q.length) {
    const t = list[longest];
    const cut = Math.max(1, Math.round(t.length * 0.12));
    list[longest] = t.slice(0, Math.max(1, t.length - cut)).trimEnd() + "…";
    warnings.push(`ответ ${longest + 1} обрезан до «${list[longest]}»`);
    return { question: q, answers: list, changed: true };
  }
  if (q.length > 3) {
    const cut = Math.max(1, Math.round(q.length * 0.12));
    const next = q.slice(0, Math.max(1, q.length - cut)).trimEnd() + "…";
    warnings.push(`вопрос обрезан до «${next}»`);
    return { question: next, answers: list, changed: true };
  }
  return { question: q, answers: list, changed: false };
}

/* Полный план кадра.

   Вход:
     question — текст вопроса;
     answers  — тексты вариантов (пустые отбрасываются);
     image    — { width, height } в пикселях файла или null;
     viewport — { width, height } окна;
     usableHeight — высота под содержимое (кадр минус шапка), необязательно.

   Выход — план, который применяет applyQuizPlan() в screen.js. Размеры в
   плане уже умножены на k, то есть в пикселях экрана. */
function planQuizFrame(input) {
  const warnings = [];
  const frame = computeFrame(input.viewport, input.usableHeight, warnings,
    input.usableWidth);

  let question = String(input.question || "");
  let answers = (input.answers || [])
    .filter((t) => String(t || "").trim())
    .map((t) => String(t));
  if (!answers.length) answers = [""];

  const fonts = startFonts(question, answers, input.image);
  let fsQ = fonts.q;
  let fsA = fonts.a;

  let best = null;
  let result = evaluateFrame({ ...input, question, answers }, { q: fsQ, a: fsA },
    frame);
  let turn = "answers";

  for (let i = 0; i < quizLayoutConfig.layout.maxIterations; i++) {
    if (!result.problems.length) { best = result; break; }
    /* Запоминаем последний вариант, который хотя бы посчитался: если
       минимумы не помогли, покажем его с предупреждениями. */
    best = result;
    const step = quizLayoutConfig.layout.fontStep;
    if (turn === "answers") {
      const next = Math.max(quizLayoutConfig.typography.answer.min, fsA - step);
      if (next === fsA) { turn = "question"; continue; }
      fsA = next;
      turn = "question";
    } else {
      const next = Math.max(quizLayoutConfig.typography.question.min, fsQ - step);
      if (next === fsQ) { turn = "answers"; continue; }
      fsQ = next;
      turn = "answers";
    }
    /* Ответ никогда не крупнее 0.9 вопроса. */
    fsA = Math.min(fsA,
      fsQ * quizLayoutConfig.typography.answer.maxOfQuestion);
    result = evaluateFrame({ ...input, question, answers },
      { q: fsQ, a: fsA }, frame);
    if (!result.problems.length) { best = result; break; }
    best = result;
  }

  /* Минимумы достигнуты, а кадр не сходится — обрезаем текст, как требует
     раздел 7, шаг 3е, и пишем в лог. */
  let guard = 0;
  while (best.problems.length && guard < 40) {
    const atMinimums = fsQ <= quizLayoutConfig.typography.question.min
      && fsA <= quizLayoutConfig.typography.answer.min;
    if (!atMinimums) break;
    const cut = truncateLongest(question, answers, warnings);
    if (!cut.changed) break;
    question = cut.question;
    answers = cut.answers;
    best = evaluateFrame({ ...input, question, answers },
      { q: fsQ, a: fsA }, frame);
    guard += 1;
  }

  for (const p of best.problems) warnings.push(p);

  /* Рост шрифта в схеме A: подгонка умеет только ужимать, поэтому кадр без
     картинки оставался наполовину пустым. Растим оба кегля вместе, пока
     содержимое не займёт долю эталонной высоты или не упрётся в потолок —
     тот считается от табличного кегля, поэтому от пропорции экрана не
     зависит. */
  const grow = quizLayoutConfig.layout.grow;
  if (grow.layouts.includes(best.layout)) {
    const start = startFonts(question, answers, input.image);
    const maxQ = growCap(start.q);
    const maxA = growCap(start.a);
    const target = grow.referenceHeight * grow.fill;
    let gq = best.question.font;
    let ga = best.answers.font;
    for (let i = 0; i < grow.maxSteps; i++) {
      if (best.totalHeight >= target) break;
      if (gq >= maxQ - 0.5 && ga >= maxA - 0.5) break;
      const nextQ = Math.min(maxQ, gq + grow.step);
      const nextA = Math.min(maxA,
        Math.min(ga + grow.step, nextQ * quizLayoutConfig.typography.answer.maxOfQuestion));
      if (nextQ <= gq + 0.01 && nextA <= ga + 0.01) break;
      const grown = evaluateFrame({ ...input, question, answers },
        { q: nextQ, a: nextA }, frame);
      /* Растём только если кадр по-прежнему сходится: рост не имеет права
         вытолкнуть содержимое за область. */
      if (grown.problems.length) break;
      best = grown;
      gq = nextQ;
      ga = nextA;
    }
  }

  const k = frame.k;
  const answersPlan = best.answers;
  const questionPlan = best.question;
  const imagePlan = best.image;

  return {
    layout: best.layout,
    mode: best.layout === "A" ? "text-only"
      : best.layout === "B" ? "image-top" : "image-side",
    scale: k,
    frame: {
      Wv: frame.Wv, Hv: frame.Hv, Cw: frame.Cw, Ch: frame.Ch,
      gap: frame.gap, wide: frame.wide, ratio: frame.ratio,
    },
    question: {
      font: questionPlan.font * k,
      lines: questionPlan.lines,
      lineCount: questionPlan.lines.length,
      width: questionPlan.width * k,
      height: questionPlan.height * k,
      maxWidth: questionPlan.textWidth * k,
    },
    answers: {
      font: answersPlan.font * k,
      cols: answersPlan.cols,
      rows: answersPlan.rows,
      mode: best.arrangement.mode,
      cardWidth: answersPlan.cardW * k,
      textWidth: answersPlan.textW * k,
      groupWidth: answersPlan.groupW * k,
      groupHeight: answersPlan.groupH * k,
      rowHeights: answersPlan.rowHeights.map((h) => h * k),
      gapX: answersPlan.gapX * k,
      gapY: answersPlan.gapY * k,
      lines: answersPlan.lines,
      count: answersPlan.count,
    },
    image: imagePlan ? {
      width: imagePlan.width * k,
      height: imagePlan.height * k,
      ratio: imagePlan.ratio,
      boxWidth: (imagePlan.boxW || imagePlan.width) * k,
      boxHeight: (imagePlan.boxH || imagePlan.height) * k,
      naturalWidth: input.image ? input.image.width : 0,
      naturalHeight: input.image ? input.image.height : 0,
      upscale: input.image && input.image.width
        ? +((imagePlan.width * k) / input.image.width).toFixed(2) : 0,
    } : null,
    textColumnWidth: best.layout === "C"
      ? (frame.Cw - (best.image ? best.image.width : 0)
        - quizLayoutConfig.layout.sideGap) * k
      : 0,
    captionHeight: quizLayoutConfig.typography.caption.font
      * quizLayoutConfig.typography.caption.lineHeight * k,
    totalHeight: best.totalHeight * k,
    content: { width: best.contentWidth * k, height: best.totalHeight * k },
    area: { width: frame.Cw * k, height: frame.Ch * k },
    gap: frame.gap * k,
    answersText: answers,
    questionText: question,
    problems: [],
    warnings,
    sideGap: quizLayoutConfig.layout.sideGap * k,
  };
}

/* ------------------------------------------------------------------ */
/* LayoutValidator                                                     */
/* ------------------------------------------------------------------ */

/* Проверки по ТЗ, раздел 10.3: всё внутри области контента, кегли не ниже
   минимумов, ничего не обрезано, картинка не увеличена больше 1.5×.
   Работает по плану, без DOM. */
function validateQuizPlan(plan) {
  const cfg = quizLayoutConfig;
  const problems = [];
  if (!plan) return { ok: false, problems: ["плана нет"] };
  const k = plan.scale || 1;

  const minA = cfg.typography.answer.min * k;
  const minQ = cfg.typography.question.min * k;
  if (plan.answers && plan.answers.font < minA - 0.5) {
    problems.push(`кегль ответа ${plan.answers.font.toFixed(1)} < ${minA.toFixed(1)}`);
  }
  if (plan.question.font < minQ - 0.5) {
    problems.push(`кегль вопроса ${plan.question.font.toFixed(1)} < ${minQ.toFixed(1)}`);
  }
  if (plan.answers && plan.answers.groupWidth > plan.area.width + 1) {
    problems.push("ответы шире области контента");
  }
  if (plan.totalHeight > plan.area.height + 1) {
    problems.push("содержимое выше области контента");
  }
  if (plan.image && plan.image.upscale > cfg.layout.imageUpscaleMax + 0.01) {
    problems.push(`картинка увеличена в ${plan.image.upscale}×`);
  }
  if (plan.textColumnWidth && plan.textColumnWidth <= 0) {
    problems.push("правой колонке не осталось ширины");
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
  classifyImage,
  computeFrame,
};
