(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const state = { report: null, selected: null };
  const caseDescriptions = {
    text: "最小 ACP → Kernel 回路",
    tool_permission: "工具权限先于 executor",
    context_memory: "Context / Memory 插件",
    workflow_continuation: "WorkflowPolicy 延续",
    resume: "SQLite 任意边界恢复",
  };

  function setEmpty(visible) {
    byId("empty-state").hidden = !visible;
    byId("case-section").hidden = visible;
    byId("timeline-section").hidden = visible;
  }

  function renderSummary(cases) {
    byId("case-count").textContent = cases.length;
    byId("pass-count").textContent = cases.filter((item) => item.status === "passed").length;
    byId("event-count").textContent = cases.reduce((total, item) => total + (item.events || []).length, 0);
    byId("report-state").textContent = "报告已载入";
    byId("report-state").parentElement.classList.add("is-live");
  }

  function renderFilter(cases) {
    const filter = byId("case-filter");
    filter.innerHTML = '<option value="all">全部 case</option>';
    cases.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.id;
      filter.appendChild(option);
    });
  }

  function renderCases(cases) {
    const list = byId("case-list");
    list.innerHTML = "";
    cases.forEach((item) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = `case-card ${item.status === "failed" ? "is-failed" : ""} ${state.selected === item.id ? "is-selected" : ""}`;
      card.setAttribute("aria-label", `查看 ${item.id} 事件`);
      card.innerHTML = `<span class="status ${item.status === "failed" ? "failed" : ""}">${item.status === "failed" ? "FAILED" : "PASSED"}</span><h3>${item.id}</h3><p>${caseDescriptions[item.id] || "插件能力场景"}</p><p>${(item.events || []).length} events · ${item.duration_ms ?? "—"} ms</p>`;
      card.addEventListener("click", () => { state.selected = item.id; renderCases(cases); renderTimeline(item); });
      list.appendChild(card);
    });
  }

  function renderTimeline(item) {
    byId("timeline-title").textContent = `${item.id} / event timeline`;
    const timeline = byId("timeline");
    timeline.innerHTML = "";
    (item.events || []).forEach((event) => {
      const row = document.createElement("article");
      row.className = "timeline-event";
      const payload = JSON.stringify(event.payload || {}, null, 2);
      row.innerHTML = `<div class="seq">#${event.sequence}</div><div class="type">${event.type}</div><details><summary>展开 payload</summary><pre>${escapeHtml(payload)}</pre></details>`;
      timeline.appendChild(row);
    });
    if (item.assertions && item.assertions.some((assertion) => !assertion.passed)) {
      const failed = item.assertions.filter((assertion) => !assertion.passed).map((assertion) => assertion.name).join(", ");
      const note = document.createElement("p");
      note.className = "lede";
      note.textContent = `失败断言：${failed}`;
      timeline.prepend(note);
    }
  }

  function escapeHtml(value) {
    return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[character]));
  }

  function render(report, source) {
    if (!report || !Array.isArray(report.cases) || !report.cases.length) { setEmpty(true); return; }
    state.report = report;
    state.selected = state.selected || report.cases[0].id;
    setEmpty(false);
    renderSummary(report.cases);
    renderFilter(report.cases);
    renderCases(report.cases);
    renderTimeline(report.cases.find((item) => item.id === state.selected) || report.cases[0]);
    byId("source-note").textContent = source || "report.json";
  }

  async function loadDefault() {
    try {
      const response = await fetch("report.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json(), "report.json");
    } catch (error) {
      setEmpty(true);
      byId("source-note").textContent = "直接打开 file:// 时请选择 report.json；HTTP server 会自动加载它";
    }
  }

  byId("case-filter").addEventListener("change", (event) => {
    if (!state.report) return;
    const item = state.report.cases.find((candidate) => candidate.id === event.target.value) || state.report.cases[0];
    state.selected = item.id;
    renderCases(state.report.cases);
    renderTimeline(item);
  });
  byId("report-file").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { try { render(JSON.parse(reader.result), file.name); } catch (_) { setEmpty(true); } };
    reader.readAsText(file, "utf-8");
  });
  loadDefault();
})();
