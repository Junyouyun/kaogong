const state = {
  auth: null,
  authMode: "login",
  user: null,
  subject: "xingce",
  view: "random",
  question: null,
  questionMode: "random",
  session: null,
  timer: null,
};

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function sanitizeRichHtml(raw) {
  const allowed = new Set([
    "P", "BR", "B", "STRONG", "I", "EM", "U", "UL", "OL", "LI",
    "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "SPAN", "SUB", "SUP",
    "IMG",
  ]);
  const decoded = String(raw || "")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
  const doc = new DOMParser().parseFromString(decoded, "text/html");
  for (const node of [...doc.body.querySelectorAll("*")]) {
    if (!allowed.has(node.tagName)) {
      node.replaceWith(...node.childNodes);
      continue;
    }
    for (const attr of [...node.attributes]) {
      if (node.tagName === "IMG" && ["src", "alt", "width", "height"].includes(attr.name)) {
        continue;
      }
      node.removeAttribute(attr.name);
    }
    if (node.tagName === "IMG") {
      const src = node.getAttribute("src") || "";
      if (src.startsWith("/90-图片/")) {
        node.setAttribute("src", `/api/images/gongkao-naoku/${src.slice(1)}`);
      } else {
        node.remove();
        continue;
      }
      node.setAttribute("loading", "lazy");
      node.setAttribute("alt", node.getAttribute("alt") || "题目图片");
    }
  }
  return doc.body.innerHTML;
}

function richOrText(value) {
  if (!value) return "";
  return /<\/?[a-z][\s\S]*>/i.test(value)
    ? sanitizeRichHtml(value)
    : escapeHtml(value).replaceAll("\n", "<br>");
}

function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `请求失败：${response.status}`;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch {}
    if (response.status === 401) showAuth("登录已失效，请重新登录。");
    throw new Error(message);
  }
  return response.json();
}

function formatTimer(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

async function loadAuthStatus() {
  const response = await fetch("/api/auth/status");
  state.auth = await response.json();
  if (state.auth.authenticated) {
    state.user = state.auth.user;
    await enterApp();
  } else {
    showAuth();
  }
}

function showAuth(copy = "") {
  el("app-shell").classList.add("hidden");
  el("auth-screen").classList.remove("hidden");
  state.authMode = state.auth?.initialized ? "login" : "register";
  el("auth-copy").textContent =
    copy ||
    (state.authMode === "register"
      ? "首次使用，请创建管理员账号。"
      : "请输入账号和密码。");
  el("auth-submit").textContent =
    state.authMode === "register" ? "创建管理员账号" : "登录";
  el("invite-row").classList.toggle(
    "hidden",
    state.authMode !== "register" ||
      (!state.auth?.initialized && !state.auth?.setup_required),
  );
  el("invite-row").firstChild.textContent =
    state.auth?.setup_required && !state.auth?.initialized
      ? "首次部署码"
      : "邀请码";
  el("auth-switch-mode").classList.toggle(
    "hidden",
    !state.auth?.initialized || !state.auth?.registration_open,
  );
  el("auth-switch-mode").textContent =
    state.authMode === "login" ? "使用邀请码创建账号" : "返回登录";
}

async function submitAuth(event) {
  event.preventDefault();
  el("auth-error").textContent = "";
  el("auth-submit").disabled = true;
  try {
    const endpoint =
      state.authMode === "register" ? "/api/auth/register" : "/api/auth/login";
    const payload = {
      username: el("auth-username").value.trim(),
      password: el("auth-password").value,
    };
    if (state.authMode === "register") {
      payload.invite_code = el("auth-invite").value.trim();
    }
    const result = await api(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.user = result.user;
    el("auth-password").value = "";
    await loadAuthStatus();
  } catch (error) {
    el("auth-error").textContent = error.message;
  } finally {
    el("auth-submit").disabled = false;
  }
}

async function enterApp() {
  el("auth-screen").classList.add("hidden");
  el("app-shell").classList.remove("hidden");
  await Promise.all([loadFilters(), loadStats()]);
  setView("random");
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {}
  clearTimer();
  state.session = null;
  state.question = null;
  state.auth = { ...(state.auth || {}), authenticated: false, user: null };
  showAuth("已经退出登录。");
}

function renderStats(summary) {
  const graded = summary.graded || 0;
  const accuracy = graded ? `${Math.round(summary.accuracy * 100)}%` : "暂无判分";
  el("stats").innerHTML = `
    <span>${escapeHtml(state.user?.username || "")} · 已完成 ${summary.total} 题</span>
    <strong>准确率 ${accuracy} · 错题 ${summary.wrong_book_count}</strong>
  `;
}

async function loadStats() {
  try {
    renderStats(await api("/api/attempts/summary"));
  } catch (error) {
    console.error(error);
  }
}

async function loadFilters() {
  const data = await api(`/api/filters?subject=${state.subject}`);
  const fill = (id, items, valueKey, labelKey, placeholder) => {
    const node = el(id);
    node.innerHTML = `<option value="">${placeholder}</option>` + items
      .map((item) => `<option value="${escapeHtml(item[valueKey])}">${escapeHtml(item[labelKey])}（${item.count}）</option>`)
      .join("");
  };
  fill("module-filter", data.modules, "module", "module", "全部模块");
  fill("region-filter", data.regions, "region", "region", "全部地区");
  fill("year-filter", data.years, "year", "year", "全部年份");
}

function hideWorkspace() {
  el("empty-state").classList.add("hidden");
  el("paper-list").classList.add("hidden");
  el("wrongbook-list").classList.add("hidden");
  el("question-card").classList.add("hidden");
}

function setView(view) {
  if (state.session) {
    toast("请先退出当前试卷");
    return;
  }
  state.view = view;
  document.querySelectorAll("[data-view]").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === view);
  });
  hideWorkspace();
  el("leave-session").classList.add("hidden");
  if (view === "random") {
    el("main-action").textContent = "开始随机练习";
    el("empty-state").classList.remove("hidden");
  } else if (view === "papers") {
    el("main-action").textContent = "筛选试卷";
    loadPapers();
  } else {
    el("main-action").textContent = "刷新错题";
    loadWrongbook();
  }
}

async function loadPapers() {
  if (state.session) return;
  hideWorkspace();
  el("paper-list").classList.remove("hidden");
  el("paper-items").innerHTML = '<p class="muted">正在加载试卷……</p>';
  const params = new URLSearchParams({ subject: state.subject, limit: "300" });
  const module = el("module-filter").value;
  const region = el("region-filter").value;
  const year = el("year-filter").value;
  if (module) params.set("module", module);
  if (region) params.set("region", region);
  if (year) params.set("year", year);
  try {
    const papers = await api(`/api/papers?${params}`);
    el("paper-count").textContent = `显示 ${papers.length} 套，可使用左侧条件缩小范围`;
    if (!papers.length) {
      el("paper-items").innerHTML = '<p class="muted">没有符合条件的试卷。</p>';
      return;
    }
    el("paper-items").innerHTML = papers.map((paper) => `
      <article class="paper-card">
        <div class="paper-title">${escapeHtml(paper.title)}</div>
        <div class="paper-meta">${escapeHtml(paper.year || "未知年份")} · ${escapeHtml(paper.region || "未知地区")} · ${paper.question_count} 题</div>
        <div class="paper-actions">
          <button class="primary" data-start-paper="${paper.id}" data-mode="practice">逐题练习</button>
          <button class="secondary" data-start-paper="${paper.id}" data-mode="exam">模拟考试</button>
        </div>
      </article>
    `).join("");
  } catch (error) {
    el("paper-items").innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`;
  }
}

async function startPaper(paperId, mode) {
  try {
    const created = await api("/api/practice-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_id: Number(paperId), mode }),
    });
    const detail = await api(`/api/practice-sessions/${created.session_id}`);
    state.session = {
      id: created.session_id,
      mode,
      detail,
      position: 0,
      answers: detail.answers || {},
      summary: null,
    };
    el("leave-session").classList.remove("hidden");
    el("main-action").classList.add("hidden");
    startTimer();
    await loadSessionQuestion();
  } catch (error) {
    toast(error.message);
  }
}

function clearTimer() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
}

function startTimer() {
  clearTimer();
  const update = () => {
    const session = state.session;
    if (!session) return;
    if (!session.detail.duration_seconds || session.detail.status === "submitted") {
      el("session-timer").textContent =
        session.detail.status === "submitted" ? "已交卷" : "不限时";
      return;
    }
    const elapsed = (Date.now() - new Date(session.detail.started_at).getTime()) / 1000;
    const left = session.detail.duration_seconds - elapsed;
    el("session-timer").textContent = formatTimer(left);
    if (left <= 0) {
      clearTimer();
      submitPaper(true);
    }
  };
  update();
  state.timer = setInterval(update, 1000);
}

async function loadSessionQuestion() {
  const session = state.session;
  if (!session) return;
  const question = await api(
    `/api/practice-sessions/${session.id}/questions/${session.position}`,
  );
  const saved = session.answers[question.id] || {};
  renderQuestion(question, {
    mode: "session",
    saved,
    submitted: session.detail.status === "submitted",
  });
}

function paperLine(question) {
  const paper = question.paper || {};
  return [
    paper.year ? `${paper.year} 年` : "",
    paper.region || "",
    paper.title || "未命名试卷",
  ].filter(Boolean).join(" · ");
}

function renderQuestion(question, options = {}) {
  state.question = question;
  state.questionMode = options.mode || "random";
  state.startedAt = performance.now();
  hideWorkspace();
  el("question-card").classList.remove("hidden");
  el("answer-result").classList.add("hidden");
  el("answer-result").innerHTML = "";
  el("free-answer").value = options.saved?.free_text || "";

  const inSession = state.questionMode === "session" && state.session;
  el("session-bar").classList.toggle("hidden", !inSession);
  if (inSession) {
    el("session-progress").textContent =
      `第 ${state.session.position + 1} / ${state.session.detail.question_ids.length} 题`;
  }

  el("question-meta").innerHTML = `
    <span class="badge">${question.subject === "xingce" ? "行测" : "申论"}</span>
    <span>${escapeHtml(question.module || "")}</span>
    <span>${escapeHtml(question.category || "")}</span>
    ${question.knowledge_point ? `<span>${escapeHtml(question.knowledge_point)}</span>` : ""}
    <span>${escapeHtml(paperLine(question))}</span>
    ${options.submitted ? '<span class="badge success">已交卷</span>' : ""}
  `;

  const passage = el("passage");
  passage.innerHTML = sanitizeRichHtml(question.passage_html);
  passage.classList.toggle("hidden", !passage.innerHTML.trim());
  el("stem").innerHTML = sanitizeRichHtml(question.stem_html);

  const isSubjective =
    question.subject === "shenlun" || question.question_type === "subjective";
  const answerBox = el("options");
  answerBox.innerHTML = "";
  answerBox.classList.toggle("hidden", isSubjective);
  el("free-answer").classList.toggle("hidden", !isSubjective);
  el("free-answer").disabled = Boolean(options.submitted);

  if (!isSubjective) {
    const inputType = question.question_type === "multi" ? "checkbox" : "radio";
    const chosen = new Set(options.saved?.selected_answer || []);
    for (const option of question.options) {
      const label = document.createElement("label");
      label.className = "option";
      label.innerHTML = `
        <input type="${inputType}" name="answer" value="${escapeHtml(option.key || "")}"
          ${chosen.has(option.key) ? "checked" : ""} ${options.submitted ? "disabled" : ""}>
        <span class="option-key">${escapeHtml(option.key || "")}</span>
        <span class="option-copy">${sanitizeRichHtml(option.html || option.label || "")}</span>
      `;
      answerBox.appendChild(label);
    }
  }

  const materials = el("materials");
  materials.innerHTML = "";
  materials.classList.toggle("hidden", !question.materials?.length);
  for (const material of question.materials || []) {
    const details = document.createElement("details");
    details.innerHTML = `
      <summary>${escapeHtml(material.title || `材料 ${material.index}`)}</summary>
      <p>${escapeHtml(material.text || "")}</p>
    `;
    materials.appendChild(details);
  }

  el("submit-button").classList.toggle("hidden", options.submitted || state.questionMode === "session");
  el("next-button").classList.toggle("hidden", state.questionMode === "session");
  el("prev-question").classList.toggle("hidden", !inSession);
  el("next-question").classList.toggle("hidden", !inSession);
  el("submit-paper").classList.toggle("hidden", !inSession || options.submitted);
  el("prev-question").disabled = !inSession || state.session.position === 0;
  el("next-question").disabled =
    !inSession || state.session.position >= state.session.detail.question_ids.length - 1;

  if (inSession) {
    el("session-timer").textContent =
      state.session.detail.status === "submitted"
        ? "已交卷"
        : state.session.detail.duration_seconds
          ? el("session-timer").textContent
          : "不限时";
  }

  if (options.submitted) {
    renderReveal(question);
  } else if (state.session?.summary) {
    showSessionSummary(state.session.summary);
  }
}

async function startRandom() {
  const params = new URLSearchParams({ subject: state.subject });
  const module = el("module-filter").value;
  const region = el("region-filter").value;
  const year = el("year-filter").value;
  if (module) params.set("module", module);
  if (region) params.set("region", region);
  if (year) params.set("year", year);
  if (state.question?.id) params.set("exclude_id", state.question.id);
  try {
    const question = await api(`/api/practice?${params}`);
    renderQuestion(question, { mode: "random" });
  } catch (error) {
    hideWorkspace();
    el("empty-state").classList.remove("hidden");
    el("empty-state").innerHTML = `
      <p class="eyebrow">NO MATCH</p>
      <h2>没有符合条件的题目</h2>
      <p>${escapeHtml(error.message)}</p>
    `;
  }
}

function selectedAnswers() {
  return [...document.querySelectorAll('input[name="answer"]:checked')]
    .map((node) => node.value);
}

function showAnswerResult(result) {
  const panel = el("answer-result");
  panel.classList.remove("hidden");
  if (result.is_correct === true) {
    panel.innerHTML = '<p class="result-correct">回答正确</p>';
  } else if (result.is_correct === false) {
    panel.innerHTML = `<p class="result-wrong">回答错误，正确答案：${escapeHtml(result.answer_text || (result.correct_option_keys || []).join(""))}</p>`;
  } else {
    panel.innerHTML = "<p>作答已保存。</p>";
  }
  if (result.analysis_text) {
    panel.innerHTML += `<h3>解析</h3><div>${richOrText(result.analysis_text)}</div>`;
  }
}

function renderReveal(question) {
  const panel = el("answer-result");
  panel.classList.remove("hidden");
  panel.innerHTML = `
    <p class="result-correct">参考答案：${escapeHtml(question.answer_text || (question.correct_option_keys || []).join("") || "主观题")}</p>
    ${question.analysis_text ? `<h3>解析</h3><div>${richOrText(question.analysis_text)}</div>` : ""}
  `;
}

function showSessionSummary(summary) {
  const panel = el("answer-result");
  panel.classList.remove("hidden");
  panel.innerHTML += `
    <div class="session-summary">
      <strong>交卷完成</strong>
      <span>已答 ${summary.answered}/${summary.question_count}</span>
      <span>判分 ${summary.correct}/${summary.graded}</span>
      <span>正确率 ${summary.accuracy === null ? "—" : Math.round(summary.accuracy * 100) + "%"}</span>
    </div>
  `;
}

async function submitRandom(event) {
  event.preventDefault();
  if (!state.question) return;
  const payload = {
    selected_answer: selectedAnswers(),
    free_text: el("free-answer").value,
    elapsed_ms: Math.round(performance.now() - state.startedAt),
  };
  try {
    const result = await api(`/api/questions/${state.question.id}/attempt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showAnswerResult(result);
    el("submit-button").classList.add("hidden");
    await loadStats();
  } catch (error) {
    toast(error.message);
  }
}

async function saveSessionAnswer(silent = false) {
  if (!state.session || !state.question) return false;
  const selected = selectedAnswers();
  const freeText = el("free-answer").value;
  if (!selected.length && !freeText.trim()) {
    if (!silent) toast("请先作答");
    return false;
  }
  const result = await api(
    `/api/practice-sessions/${state.session.id}/attempt`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question_id: state.question.id,
        selected_answer: selected,
        free_text: freeText,
        elapsed_ms: Math.round(performance.now() - state.startedAt),
      }),
    },
  );
  state.session.answers[state.question.id] = {
    selected_answer: selected,
    free_text: freeText,
  };
  if (!silent) {
    if (state.session.mode === "practice") {
      showAnswerResult(result);
    } else {
      const panel = el("answer-result");
      panel.classList.remove("hidden");
      panel.innerHTML = "<p>答案已保存，交卷后显示结果。</p>";
    }
    await loadStats();
  }
  return true;
}

async function moveSession(offset) {
  if (!state.session) return;
  await saveSessionAnswer(true);
  const next = state.session.position + offset;
  if (next < 0 || next >= state.session.detail.question_ids.length) return;
  state.session.position = next;
  await loadSessionQuestion();
}

async function submitPaper(auto = false) {
  if (!state.session) return;
  if (!auto && !confirm("确定提交当前试卷吗？交卷后不能修改答案。")) return;
  try {
    await saveSessionAnswer(true);
    const summary = await api(
      `/api/practice-sessions/${state.session.id}/submit`,
      { method: "POST" },
    );
    state.session.summary = summary;
    state.session.detail.status = "submitted";
    state.session.detail.submitted_at = new Date().toISOString();
    clearTimer();
    await loadSessionQuestion();
    showSessionSummary(summary);
    await loadStats();
    toast("交卷完成");
  } catch (error) {
    toast(error.message);
  }
}

function leaveSession() {
  clearTimer();
  state.session = null;
  state.question = null;
  el("main-action").classList.remove("hidden");
  el("leave-session").classList.add("hidden");
  setView(state.view);
}

async function loadWrongbook() {
  if (state.session) return;
  hideWorkspace();
  el("wrongbook-list").classList.remove("hidden");
  el("wrongbook-items").innerHTML = '<p class="muted">正在加载错题……</p>';
  try {
    const data = await api(`/api/wrongbook?subject=${state.subject}`);
    el("wrongbook-count").textContent = `共 ${data.items.length} 道待复盘`;
    if (!data.items.length) {
      el("wrongbook-items").innerHTML = '<p class="muted">当前没有待复盘的错题。</p>';
      return;
    }
    el("wrongbook-items").innerHTML = data.items.map((item) => `
      <article class="paper-card wrong-card">
        <div class="paper-title">${escapeHtml(item.module || "")} · 错 ${item.wrong_count} 次</div>
        <div class="wrong-preview">${escapeHtml(item.preview)}</div>
        <div class="paper-meta">${escapeHtml(item.paper_title || "")}</div>
        <div class="paper-actions">
          <button class="primary" data-retry-wrong="${item.id}">重做</button>
          <button class="secondary" data-resolve-wrong="${item.id}">移出错题本</button>
        </div>
      </article>
    `).join("");
  } catch (error) {
    el("wrongbook-items").innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`;
  }
}

async function retryWrong(questionId) {
  const question = await api(`/api/questions/${questionId}`);
  renderQuestion(question, { mode: "wrongbook" });
}

async function resolveWrong(questionId) {
  try {
    await api(`/api/wrongbook/${questionId}/resolve`, { method: "POST" });
    await loadWrongbook();
    await loadStats();
  } catch (error) {
    toast(error.message);
  }
}

document.querySelectorAll("[data-subject]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (state.session) {
      toast("请先退出当前试卷");
      return;
    }
    state.subject = button.dataset.subject;
    document.querySelectorAll("[data-subject]").forEach((node) => {
      node.classList.toggle("active", node === button);
    });
    state.question = null;
    await loadFilters();
    setView(state.view);
  });
});

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

el("auth-form").addEventListener("submit", submitAuth);
el("auth-switch-mode").addEventListener("click", () => {
  state.authMode = state.authMode === "login" ? "register" : "login";
  showAuth();
});
el("logout-button").addEventListener("click", logout);
el("main-action").addEventListener("click", () => {
  if (state.view === "random") startRandom();
  else if (state.view === "papers") loadPapers();
  else loadWrongbook();
});
el("answer-form").addEventListener("submit", (event) => {
  if (state.questionMode === "session") {
    event.preventDefault();
    saveSessionAnswer(false).catch((error) => toast(error.message));
  } else {
    submitRandom(event);
  }
});
el("next-button").addEventListener("click", startRandom);
el("prev-question").addEventListener("click", () => moveSession(-1));
el("next-question").addEventListener("click", () => moveSession(1));
el("submit-paper").addEventListener("click", () => submitPaper(false));
el("leave-session").addEventListener("click", leaveSession);

el("paper-items").addEventListener("click", (event) => {
  const button = event.target.closest("[data-start-paper]");
  if (button) startPaper(button.dataset.startPaper, button.dataset.mode);
});
el("wrongbook-items").addEventListener("click", (event) => {
  const retry = event.target.closest("[data-retry-wrong]");
  if (retry) {
    retryWrong(retry.dataset.retryWrong).catch((error) => toast(error.message));
    return;
  }
  const resolve = event.target.closest("[data-resolve-wrong]");
  if (resolve) resolveWrong(resolve.dataset.resolveWrong);
});

loadAuthStatus().catch((error) => {
  console.error(error);
  el("auth-copy").textContent = error.message;
});
