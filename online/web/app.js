// Testova online test — student page. The server decides everything (time,
// violations, score); this file only shows what it is told and reports
// focus/visibility changes.
"use strict";

(function () {
  const code = (location.pathname.split("/").filter(Boolean)[1] || "").toUpperCase();
  const API = "/api/t/" + encodeURIComponent(code);

  const TEXT = {
    uz: {
      loading: "Yuklanmoqda...", last_name: "Familiya", first_name: "Ism", class_name: "Sinf",
      start: "Testni boshlash", prev: "← Oldingi", next: "Keyingi →", finish: "Testni yakunlash",
      ok: "Tushunarli", cancel: "Bekor qilish",
      rule_once: "Testni faqat bir marta topshirasiz.",
      rule_leave: "Sahifadan chiqmang: boshqa ilova yoki oynaga o'tish qoidabuzarlik hisoblanadi. 3-marta test yopiladi.",
      rule_saved: "Har bir javob darhol saqlanadi.",
      meta: "{n} ta savol", meta_time: ", {m} daqiqa", meta_q: ", har savolga {s} soniya",
      question: "Savol {i} / {n}", multi_hint: "Bir nechta to'g'ri javob bo'lishi mumkin.",
      saved: "Saqlandi ✓", save_failed: "Saqlanmadi, internetni tekshiring.",
      confirm_finish: "Testni yakunlaysizmi? Javobsiz savollar: {k}.",
      warn: "⚠️ Siz test sahifasidan chiqdingiz. Bu qoidabuzarlik hisoblandi. Yana {r} ta imkoniyat qoldi.",
      warn_replaced: " Javob berilmagan {k} ta savol almashtirildi.",
      done: "Test yakunlandi", done_text: "Natija: {s} / {n}",
      expired: "Vaqt tugadi", caught: "Test yopildi",
      caught_text: "Test sahifasidan 3 marta chiqildi, shuning uchun test yopildi va natija 0. O'qituvchingizga murojaat qiling.",
      closed: "Bu test yopilgan.", no_such_test: "Bunday test topilmadi. Havolani tekshiring.",
      disabled: "Onlayn test hozir o'chirilgan.", too_many: "Juda ko'p urinish. Birozdan keyin qayta urinib ko'ring.",
      already_started: "Bu ism bilan test allaqachon boshlangan. O'qituvchingizga murojaat qiling.",
      name_required: "Familiya, ism va sinfni to'ldiring.",
      no_going_back: "Bu testda oldingi savolga qaytib bo'lmaydi.",
      question_time_over: "Bu savolning vaqti tugagan.", error: "Xatolik yuz berdi. Sahifani yangilang.",
      seconds: "{s} s",
    },
    ru: {
      loading: "Загрузка...", last_name: "Фамилия", first_name: "Имя", class_name: "Класс",
      start: "Начать тест", prev: "← Назад", next: "Далее →", finish: "Завершить тест",
      ok: "Понятно", cancel: "Отмена",
      rule_once: "Тест можно пройти только один раз.",
      rule_leave: "Не уходите со страницы: переход в другое приложение или окно — нарушение. На 3-й раз тест закроется.",
      rule_saved: "Каждый ответ сохраняется сразу.",
      meta: "{n} вопросов", meta_time: ", {m} мин", meta_q: ", на вопрос {s} с",
      question: "Вопрос {i} / {n}", multi_hint: "Может быть несколько правильных ответов.",
      saved: "Сохранено ✓", save_failed: "Не сохранилось, проверьте интернет.",
      confirm_finish: "Завершить тест? Без ответа: {k}.",
      warn: "⚠️ Вы ушли со страницы теста. Это засчитано как нарушение. Осталось попыток: {r}.",
      warn_replaced: " Заменены вопросы без ответа: {k}.",
      done: "Тест завершён", done_text: "Результат: {s} / {n}",
      expired: "Время вышло", caught: "Тест закрыт",
      caught_text: "Вы 3 раза уходили со страницы теста, поэтому тест закрыт, результат 0. Обратитесь к учителю.",
      closed: "Этот тест закрыт.", no_such_test: "Такой тест не найден. Проверьте ссылку.",
      disabled: "Онлайн-тесты сейчас отключены.", too_many: "Слишком много попыток. Попробуйте позже.",
      already_started: "Тест с этим именем уже начат. Обратитесь к учителю.",
      name_required: "Заполните фамилию, имя и класс.",
      no_going_back: "В этом тесте нельзя вернуться к предыдущему вопросу.",
      question_time_over: "Время на этот вопрос истекло.", error: "Произошла ошибка. Обновите страницу.",
      seconds: "{s} с",
    },
    en: {
      loading: "Loading...", last_name: "Last name", first_name: "First name", class_name: "Class",
      start: "Start the test", prev: "← Previous", next: "Next →", finish: "Finish the test",
      ok: "OK", cancel: "Cancel",
      rule_once: "You can take the test only once.",
      rule_leave: "Do not leave the page: switching to another app or window is a violation. The third time closes the test.",
      rule_saved: "Every answer is saved at once.",
      meta: "{n} questions", meta_time: ", {m} min", meta_q: ", {s} s per question",
      question: "Question {i} / {n}", multi_hint: "There may be more than one correct answer.",
      saved: "Saved ✓", save_failed: "Not saved, check your connection.",
      confirm_finish: "Finish the test? Unanswered: {k}.",
      warn: "⚠️ You left the test page. This counts as a violation. Chances left: {r}.",
      warn_replaced: " {k} unanswered questions were replaced.",
      done: "Test finished", done_text: "Score: {s} / {n}",
      expired: "Time is up", caught: "Test closed",
      caught_text: "You left the test page 3 times, so the test was closed with a score of 0. Please talk to your teacher.",
      closed: "This test is closed.", no_such_test: "No such test. Check the link.",
      disabled: "Online tests are switched off right now.", too_many: "Too many attempts. Please try again later.",
      already_started: "A test with this name has already been started. Please talk to your teacher.",
      name_required: "Fill in last name, first name and class.",
      no_going_back: "You cannot go back to a previous question in this test.",
      question_time_over: "Time for this question is over.", error: "Something went wrong. Reload the page.",
      seconds: "{s} s",
    },
  };

  const $ = (id) => document.getElementById(id);
  let lang = "uz";
  let info = null;
  let state = null;
  let q = null;
  let deadline = null;   // client-side estimate, re-synced from the server
  let qDeadline = null;
  let busy = false;
  let away = false;
  let seenViolations = null;

  function t(key, vars) {
    let s = (TEXT[lang] && TEXT[lang][key]) || TEXT.uz[key] || key;
    for (const k in vars || {}) s = s.split("{" + k + "}").join(String(vars[k]));
    return s;
  }

  function applyLang() {
    document.documentElement.lang = lang;
    $("lang").value = lang;
    document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
    if (info) renderMeta();
    if (state && state.status === "active") renderTop();
    if (state && !$("done").hidden) showDone();
  }

  function show(id) {
    ["loading", "start", "exam", "done", "error"].forEach((s) => { $(s).hidden = s !== id; });
  }

  async function api(path, method, body) {
    const opts = { method: method || "GET", credentials: "same-origin", headers: {} };
    if (opts.method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Requested-With"] = "testova";
      opts.body = JSON.stringify(body || {});
    }
    const r = await fetch(API + path, opts);
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    if (!r.ok) {
      const err = new Error(data.error || "error");
      err.code = data.error || "error";
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function fail(err) {
    const code = (err && err.code) || "error";
    if (code === "attempt_over") return refreshState();
    $("error-text").textContent = t(code in TEXT.uz ? code : "error");
    show("error");
  }

  function modal(text, withCancel) {
    return new Promise((resolve) => {
      $("modal-text").textContent = text;
      $("modal-cancel").hidden = !withCancel;
      $("modal").hidden = false;
      const done = (v) => () => { $("modal").hidden = true; resolve(v); };
      $("modal-ok").onclick = done(true);
      $("modal-cancel").onclick = done(false);
    });
  }

  // ── start ────────────────────────────────────────────────────────────────
  function renderMeta() {
    $("title").textContent = info.title;
    $("start-title").textContent = info.title;
    let meta = t("meta", { n: info.question_count });
    if (info.total_seconds) meta += t("meta_time", { m: Math.round(info.total_seconds / 60) });
    if (info.per_question_seconds) meta += t("meta_q", { s: info.per_question_seconds });
    $("start-meta").textContent = meta;
  }

  async function init() {
    try { lang = localStorage.getItem("testova_lang") || ""; } catch (e) { lang = ""; }
    try {
      info = await api("/info");
    } catch (err) {
      lang = lang || "uz"; applyLang(); return fail(err);
    }
    lang = lang || info.lang || "uz";
    applyLang();
    if (info.has_attempt) return refreshState();
    if (!info.open) { $("error-text").textContent = t("closed"); return show("error"); }
    show("start");
  }

  $("start-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const body = {
      first_name: f.first_name.value.trim(), last_name: f.last_name.value.trim(),
      class_name: f.class_name.value.trim(),
    };
    if (!body.first_name || !body.last_name || !body.class_name) {
      return modal(t("name_required"));
    }
    f.querySelector("button").disabled = true;
    try {
      state = await api("/start", "POST", body);
      seenViolations = state.violations;
      enterExam();
    } catch (err) {
      f.querySelector("button").disabled = false;
      if (err.code === "already_started" || err.code === "name_required") return modal(t(err.code));
      fail(err);
    }
  });

  // ── exam ─────────────────────────────────────────────────────────────────
  async function refreshState() {
    try {
      state = await api("/state");
    } catch (err) {
      if (err.status === 401) { show("start"); return; }
      return fail(err);
    }
    if (state.status === "annulled") { show("start"); return; }
    if (state.status !== "active") return showDone();
    // A violation decided without a "visible" report (the page was closed or
    // reloaded while away): the server counted it; tell the student.
    // After a reload the previous count is unknown: remind once if any.
    if (state.violations > (seenViolations == null ? 0 : seenViolations)) {
      modal(t("warn", { r: state.remaining_chances }));
    }
    seenViolations = state.violations;
    if ($("exam").hidden) return enterExam();
    syncTimers();
    renderPanel();
  }

  function enterExam() {
    if (state.status !== "active") return showDone();
    show("exam");
    renderWatermark();
    syncTimers();
    renderPanel();
    loadQuestion(state.current, true);
  }

  function syncTimers() {
    deadline = state.seconds_left == null ? null : Date.now() + state.seconds_left * 1000;
  }

  function renderTop() {
    $("pos").textContent = t("question", { i: state.current, n: state.total });
  }

  function renderPanel() {
    const panel = $("panel");
    panel.textContent = "";
    const answered = new Set(state.answered);
    for (let i = 1; i <= state.total; i++) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = String(i);
      if (answered.has(i)) b.classList.add("answered");
      if (i === state.current) { b.classList.add("current"); b.setAttribute("aria-current", "true"); }
      if (!state.allow_back && i < state.current) b.disabled = true;
      b.addEventListener("click", () => loadQuestion(i));
      panel.appendChild(b);
    }
    renderTop();
    $("prev").disabled = !state.allow_back || state.current <= 1;
  }

  function renderWatermark() {
    const wm = $("watermark");
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    const label = state.student + " · " + state.class_name + " · " + hh + ":" + mm;
    wm.textContent = "";
    for (let i = 0; i < 40; i++) {
      const s = document.createElement("span");
      s.textContent = label;
      wm.appendChild(s);
    }
  }

  async function loadQuestion(pos, initial) {
    if (busy) return;
    busy = true;
    try {
      q = (pos === state.current && initial)
        ? await api("/q/" + pos)
        : await api("/goto", "POST", { position: pos });
      state.current = q.position;
      renderQuestion();
      renderPanel();
    } catch (err) {
      if (err.code === "no_going_back" || err.code === "question_time_over") modal(t(err.code));
      else fail(err);
    } finally {
      busy = false;
    }
  }

  function renderQuestion() {
    $("qtext").textContent = q.text;
    $("context").hidden = !q.context;
    $("context").textContent = q.context || "";
    const img = $("qimg");
    if (q.has_image) { img.src = API + "/img/" + q.position + "?v=" + Date.now(); img.hidden = false; }
    else { img.hidden = true; img.removeAttribute("src"); }
    $("multi-hint").hidden = !q.multi;
    $("saved").textContent = "";
    const box = $("options");
    box.textContent = "";
    box.setAttribute("role", q.multi ? "group" : "radiogroup");
    q.options.forEach((text, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "option" + (q.multi ? " multi" : "");
      b.setAttribute("role", q.multi ? "checkbox" : "radio");
      b.setAttribute("aria-checked", q.chosen.includes(i) ? "true" : "false");
      const mark = document.createElement("span");
      mark.className = "mark";
      mark.textContent = String.fromCharCode(65 + i);
      const label = document.createElement("span");
      label.textContent = text;
      b.append(mark, label);
      b.addEventListener("click", () => choose(i));
      box.appendChild(b);
    });
    qDeadline = q.seconds_left == null ? null : Date.now() + q.seconds_left * 1000;
    $("qtimer").hidden = qDeadline == null;
    $("next").textContent = state.current >= state.total ? t("finish") : t("next");
  }

  async function choose(i) {
    let chosen = q.multi
      ? (q.chosen.includes(i) ? q.chosen.filter((x) => x !== i) : q.chosen.concat([i]))
      : [i];
    const before = q.chosen;
    q.chosen = chosen;
    renderChoices();
    try {
      await api("/answer", "POST", { position: q.position, choices: chosen });
      $("saved").textContent = t("saved");
      const set = new Set(state.answered);
      if (chosen.length) set.add(q.position); else set.delete(q.position);
      state.answered = Array.from(set);
      renderPanel();
    } catch (err) {
      q.chosen = before;
      renderChoices();
      if (err.code === "question_time_over" || err.code === "no_going_back") return modal(t(err.code));
      if (err.status) return fail(err);
      $("saved").textContent = t("save_failed");
    }
  }

  function renderChoices() {
    document.querySelectorAll("#options .option").forEach((b, i) => {
      b.setAttribute("aria-checked", q.chosen.includes(i) ? "true" : "false");
    });
  }

  $("prev").addEventListener("click", () => loadQuestion(state.current - 1));
  $("next").addEventListener("click", () => {
    if (state.current >= state.total) return askFinish();
    loadQuestion(state.current + 1);
  });
  $("finish").addEventListener("click", askFinish);

  async function askFinish() {
    const left = state.total - state.answered.length;
    if (!(await modal(t("confirm_finish", { k: left }), true))) return;
    try {
      state = await api("/finish", "POST", {});
      showDone();
    } catch (err) { fail(err); }
  }

  function showDone() {
    stopTimers();
    if (state.status === "caught") {
      $("done-title").textContent = t("caught");
      $("done-text").textContent = t("caught_text");
    } else {
      $("done-title").textContent = state.status === "expired" ? t("expired") : t("done");
      $("done-text").textContent = t("done_text", { s: state.score == null ? 0 : state.score, n: state.total });
    }
    show("done");
  }

  // ── timers ───────────────────────────────────────────────────────────────
  let tick = null;
  let sync = null;
  let wmTick = null;

  function fmt(sec) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return (m >= 60 ? Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }

  function onTick() {
    if (!state || state.status !== "active" || $("exam").hidden) return;
    if (deadline != null) {
      const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
      $("timer").textContent = "⏱ " + fmt(left);
      $("timer").classList.toggle("low", left <= 60);
      if (left === 0) { deadline = null; refreshState(); }
    } else {
      $("timer").textContent = "";
    }
    if (qDeadline != null) {
      const left = Math.max(0, Math.round((qDeadline - Date.now()) / 1000));
      $("qtimer").textContent = t("seconds", { s: left });
      $("qtimer").classList.toggle("low", left <= 10);
      if (left === 0) {
        qDeadline = null;
        if (state.current < state.total) loadQuestion(state.current + 1);
        else askFinish();
      }
    }
  }

  function startTimers() {
    tick = setInterval(onTick, 500);
    sync = setInterval(() => { if (!away) refreshState(); }, 15000);
    wmTick = setInterval(() => { if (state && state.status === "active") renderWatermark(); }, 60000);
  }

  function stopTimers() {
    clearInterval(tick); clearInterval(sync); clearInterval(wmTick);
  }

  // ── leaving the page: report, the server decides ─────────────────────────
  async function report(kind) {
    if (!state || state.status !== "active") return;
    if ((kind === "hidden") === away) return;   // one report per change
    away = kind === "hidden";
    try {
      const r = await fetch(API + "/event", {
        method: "POST", credentials: "same-origin", keepalive: true,
        headers: { "Content-Type": "application/json", "X-Requested-With": "testova" },
        body: JSON.stringify({ kind: kind }),
      });
      if (kind !== "visible" || !r.ok) return;
      const out = await r.json();
      if (out.caught) { await refreshState(); return; }
      if (out.counted) {
        let msg = t("warn", { r: out.remaining_chances });
        if (out.replaced && out.replaced.length) msg += t("warn_replaced", { k: out.replaced.length });
        seenViolations = out.violations;
        await refreshState();
        modal(msg);
      }
    } catch (e) { /* offline: the server judges the gap at the next contact */ }
  }

  document.addEventListener("visibilitychange", () => report(document.hidden ? "hidden" : "visible"));
  window.addEventListener("blur", () => report("hidden"));
  window.addEventListener("focus", () => report("visible"));
  window.addEventListener("pagehide", () => report("hidden"));

  // ── no copying ───────────────────────────────────────────────────────────
  ["copy", "cut", "contextmenu", "selectstart", "dragstart"].forEach((ev) => {
    document.addEventListener(ev, (e) => {
      if (e.target && e.target.tagName === "INPUT") return;
      e.preventDefault();
    });
  });
  document.addEventListener("keydown", (e) => {
    const k = (e.key || "").toLowerCase();
    if ((e.ctrlKey || e.metaKey) && ["c", "x", "a", "p", "s", "u"].includes(k) &&
        !(e.target && e.target.tagName === "INPUT")) e.preventDefault();
  });

  $("lang").addEventListener("change", (e) => {
    lang = e.target.value;
    try { localStorage.setItem("testova_lang", lang); } catch (err) { /* private mode */ }
    applyLang();
    if (q && state && state.status === "active") renderQuestion();
  });

  startTimers();
  init();
})();
