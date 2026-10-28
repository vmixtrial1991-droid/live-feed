/* Проверка движка раскладки без браузера.

   quiz-layout.js рассчитан на canvas.measureText и window, поэтому здесь
   подставляются минимальные заглушки: мера ширины — приближение
   «символы × 0.55em» (кириллица в Inter), canvas нужен только ради API.
   Так проверяется арифметика плана: схемы A/B/C, кегли, пределы, обрезка.

   Запуск: node tools/check-layout.js
*/
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "static", "quiz-layout.js");

/* Приблизительная ширина строки: 0.55em на символ, но по словам, чтобы
   перенос был похож на настоящий. */
function makeCtx() {
  let font = "600 40px sans";
  let ratio = 0.55;
  return {
    set font(value) {
      font = value;
      const m = /(\d+(?:\.\d+)?)px/.exec(value);
      ratio = m ? parseFloat(m[1]) * 0.55 : 22;
    },
    get font() { return font; },
    measureText(text) {
      const s = String(text);
      let w = 0;
      for (const ch of s) {
        if (ch === " ") w += ratio * 0.35;
        else if (".,:;!|'і".includes(ch)) w += ratio * 0.35;
        else w += ratio;
      }
      return { width: w };
    },
  };
}

const ctx = makeCtx();
const sandbox = {
  window: {},
  document: {
    createElement() {
      return { getContext() { return ctx; } };
    },
  },
  console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox, { filename: SRC });

const L = sandbox.window.quizLayout;
if (!L) {
  console.error("движок не выставил window.quizLayout");
  process.exit(1);
}

let failures = 0;
function check(name, ok, detail) {
  if (ok) {
    console.log("  ok   " + name);
  } else {
    failures += 1;
    console.log("  FAIL " + name + (detail ? "  — " + detail : ""));
  }
}

/* ---------------------------------------------------------------- схемы */

const VIEW = { width: 1920, height: 1080 };
const SHORT_Q = "Играем дальше?";
const LONG_Q = "Как вы думаете, что произойдёт с раскладкой, если вопрос "
  + "окажется очень длинным и не поместится в отведённую ему долю кадра "
  + "целиком, даже при минимальном кегле из таблицы?";
const SHORT_A = ["Да", "Нет"];
const LONG_A = [
  "Кегль уменьшится до минимума, а потом текст обрежется",
  "Карточка вырастет по высоте вверх и вытеснит остальные",
  "Сетка перестроится в колонку и займёт всю ширину",
  "Весь кадр ужмётся общим масштабом",
];

const IMAGES = {
  none: null,
  wide: { width: 1600, height: 900 },
  fourThree: { width: 1200, height: 900 },
  square: { width: 1200, height: 1200 },
  threeFour: { width: 900, height: 1200 },
  tall: { width: 900, height: 1600 },
  panorama: { width: 2400, height: 800 },
};

function plan(question, answers, image, viewport = VIEW) {
  return L.plan({ question, answers, image, viewport });
}

console.log("Схемы по разделу 4:");
for (const [name, image] of Object.entries(IMAGES)) {
  const p = plan(SHORT_Q, SHORT_A, image);
  const expect = {
    none: "A", wide: "B", fourThree: "B", square: "C",
    threeFour: "C", tall: "C", panorama: "B",
  }[name];
  check(`16:9, фото ${name} → схема ${expect}`, p.layout === expect, p.layout);
}
/* Квадрат на 4:3 обязан уйти в стек, а не в две колонки. */
const sq43 = plan(SHORT_Q, SHORT_A, IMAGES.square, { width: 1440, height: 1080 });
check("4:3, квадрат → схема B", sq43.layout === "B", sq43.layout);
const sq169 = plan(SHORT_Q, SHORT_A, IMAGES.square, { width: 1920, height: 1080 });
check("16:9, квадрат → схема C", sq169.layout === "C", sq169.layout);

console.log("\nРаскладка ответов по схеме:");
check("A, 2 ответа → колонка", plan(SHORT_Q, SHORT_A, null).answers.cols === 1);
check("A, 3 ответа → два сверху, один по центру снизу",
  plan(SHORT_Q, ["Да", "Нет", "Не знаю"], null).answers.cols === 2
  && plan(SHORT_Q, ["Да", "Нет", "Не знаю"], null).answers.lastRowCount === 1);
check("A, 4 ответа → сетка 2×2",
  plan(SHORT_Q, LONG_A, null).answers.cols === 2);
check("B, 2 ответа → один ряд",
  plan(SHORT_Q, SHORT_A, IMAGES.wide).answers.cols === 2);
check("B, 3 ответа → два сверху, один по центру снизу",
  plan(SHORT_Q, ["Да", "Нет", "Не знаю"], IMAGES.wide).answers.cols === 2
  && plan(SHORT_Q, ["Да", "Нет", "Не знаю"], IMAGES.wide).answers.mode
     === "grid2plus1");
check("C, 4 ответа → колонка",
  plan(SHORT_Q, LONG_A, IMAGES.tall).answers.cols === 1);

console.log("\nКегли не зависят от пропорции холста:");
const canvases = [
  [1920, 1080], [1920, 1200], [1800, 1080], [1440, 1080], [1280, 720],
];
const fonts = canvases.map(([w, h]) => {
  const p = plan(SHORT_Q, LONG_A, null, { width: w, height: h });
  return Math.round(p.question.font / p.scale);
});
check("вопрос 64 виртуальных px на всех холстах",
  fonts.every((f) => f === fonts[0]), fonts.join(", "));
const aFonts = canvases.map(([w, h]) => {
  const p = plan(SHORT_Q, LONG_A, null, { width: w, height: h });
  return Math.round(p.answers.font / p.scale);
});
check("ответ одного виртуального кегля на всех холстах",
  aFonts.every((f) => f === aFonts[0]), aFonts.join(", "));

console.log("\nКарточка: короткое «Да» не на пол-экрана:");
const short = plan(SHORT_Q, SHORT_A, null);
/* Сравнивать надо одну и ту же схему: короткие и длинные ответы в схеме A
   при двух вариантах — обе колонкой, отличие только в тексте. */
const longTwo = plan(SHORT_Q, [
  "Кегль уменьшится до минимума, а потом текст обрежется",
  "Карточка вырастет по высоте вверх и вытеснит остальные",
], null);
check("короткая карточка уже длинной",
  short.answers.cardWidth < longTwo.answers.cardWidth,
  `${Math.round(short.answers.cardWidth)} vs ${Math.round(longTwo.answers.cardWidth)}`);
check("короткая карточка не шире 70% холста",
  short.answers.groupWidth <= 1920 * 0.7 + 1,
  Math.round(short.answers.groupWidth));
check("карточка не ниже 2.2em",
  short.answers.rowHeights[0] >= short.answers.font * 2.2 - 1,
  `${short.answers.rowHeights[0]} при кегле ${short.answers.font}`);

/* Рост в схеме A: без картинки кадр не должен оставаться наполовину пустым.
   Потолок — 1.4 от табличного кегля, и он одинаков на всех холстах, иначе
   «Да/Нет» на 4:3 вышло бы крупнее, чем на 16:9. */
console.log("\nРост шрифта в схеме A:");
const shortA = plan(SHORT_Q, SHORT_A, null);

// Стартовый кегль берём из таблицы через сам движок: это то, от чего
// считается потолок роста.
const tableQ = (function () {
  const table = L.config.typography.question.table;
  for (const [limit, size] of table) if (SHORT_Q.length <= limit) return size;
  return table[table.length - 1][1];
})();
const capQ = tableQ * L.config.layout.grow.max;
check("короткий вопрос подрос выше табличного кегля",
  shortA.question.font > tableQ + 1,
  `${Math.round(shortA.question.font)} при табличном ${tableQ}`);
check("рост не вышел за потолок 1.4×",
  shortA.question.font <= capQ + 1,
  `${Math.round(shortA.question.font)} > ${Math.round(capQ)}`);
check("рост не вытолкнул содержимое за область",
  shortA.totalHeight <= shortA.area.height + 1,
  `${Math.round(shortA.totalHeight)} > ${Math.round(shortA.area.height)}`);
const growFonts = canvases.map(([w, h]) => {
  const p = plan(SHORT_Q, SHORT_A, null, { width: w, height: h });
  return Math.round(p.question.font / p.scale);
});
check("рост одинаков в виртуальных px на всех холстах",
  growFonts.every((f) => f === growFonts[0]), growFonts.join(", "));
/* В схеме C картинка стоит сбоку и высоту не делит, поэтому текст там
   растёт как в A; в B потолок ниже — снимок делит высоту с текстом. */
for (const name of ["wide", "square", "tall"]) {
  const p = plan(SHORT_Q, SHORT_A, IMAGES[name]);
  const table = L.config.typography.question.withImage;
  let base = table[table.length - 1][1];
  for (const [limit, size] of table) if (SHORT_Q.length <= limit) { base = size; break; }
  const want = p.layout === "B"
    ? base * L.config.layout.grow.maxByLayout.B
    : base * L.config.layout.grow.maxByLayout.C;
  check(`схема ${p.layout} (${name}): рост не выше потолка`,
    p.question.font / p.scale <= want + 1,
    `${Math.round(p.question.font / p.scale)} при потолке ${Math.round(want)}`);
}

console.log("\nМинимумы и подгонка:");
for (const [name, image] of Object.entries(IMAGES)) {
  for (const [qname, q, a] of [["короткий", SHORT_Q, SHORT_A],
                               ["длинный", LONG_Q, LONG_A]]) {
    const p = plan(q, a, image);
    const minA = L.config.typography.answer.min * p.scale;
    const minQ = L.config.typography.question.min * p.scale;
    const ok = p.answers.font >= minA - 0.6 && p.question.font >= minQ - 0.6;
    check(`${name}/${qname}: кегли не ниже минимума`, ok,
      `Q ${p.question.font.toFixed(1)} A ${p.answers.font.toFixed(1)}`);
    const inside = p.totalHeight <= p.area.height + 1.5;
    check(`${name}/${qname}: содержимое внутри области`, inside,
      `${Math.round(p.totalHeight)} > ${Math.round(p.area.height)}`);
  }
}

console.log("\nОтвет не крупнее 0.9 вопроса:");
for (const [name, image] of Object.entries(IMAGES)) {
  const p = plan(LONG_Q, LONG_A, image);
  check(`${name}`, p.answers.font <= p.question.font * 0.9 + 0.6,
    `A ${p.answers.font.toFixed(1)} Q ${p.question.font.toFixed(1)}`);
}

console.log("\nКартинка не увеличивается больше 1.5× и не обрезается:");
for (const [name, image] of Object.entries(IMAGES)) {
  if (!image) continue;
  const p = plan(SHORT_Q, SHORT_A, image);
  check(`${name}: upscale ≤ 1.5`, p.image.upscale <= 1.5 + 0.01,
    `${p.image.upscale}×`);
  const r = p.image.width / p.image.height;
  check(`${name}: пропорции сохранены`,
    Math.abs(r - image.width / image.height) < 0.02,
    `${r.toFixed(3)} vs ${(image.width / image.height).toFixed(3)}`);
}

console.log("\nСхема C: правая колонка не схлопнулась:");
for (const name of ["square", "threeFour", "tall"]) {
  const p = plan(LONG_Q, LONG_A, IMAGES[name]);
  check(`${name}: колонка шире 30% контента`,
    p.textColumnWidth > p.area.width * 0.3,
    Math.round(p.textColumnWidth));
}

console.log("\nОбрезка на минимумах вместо вылета за кадр:");
const hugeQ = "Слово ".repeat(160).trim();
const hugeA = ["Очень длинный ответ ".repeat(20).trim(),
               "Второй очень длинный ответ ".repeat(18).trim()];
const p = plan(hugeQ, hugeA, null);
check("вопрос обрезан с многоточием", /…$/.test(p.questionText),
  p.questionText.slice(-12));
check("обрезка записана в предупреждения",
  p.warnings.some((w) => w.includes("обрезан")), p.warnings.join("; "));

console.log("\nПроверка плана (validate):");
const good = plan(SHORT_Q, SHORT_A, null);
const v = L.validate(good);
check("хороший кадр проходит", v.ok, v.problems.join("; "));

console.log("");
if (failures) {
  console.log(`Провалено: ${failures}`);
  process.exit(1);
}
console.log("Движок в порядке.");
