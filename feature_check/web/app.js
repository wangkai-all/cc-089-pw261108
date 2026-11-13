/* 特征一致性检查台前端逻辑（原生 JS，无外部依赖，无外联请求） */
"use strict";

const state = {
  sources: { offline: null, online: null },
  features: [],                 // FeatureSpec 列表
  currentRun: null,             // 最近一次加载的运行详情
  selectedParent: null,         // 重算父运行 ID
  resultFilter: { category: "", feature: "", entity_id: "" },
};

const DIFF_LABELS = {
  match: "一致",
  missing_row: "整行缺失",
  null_mismatch: "空值差异",
  type_mismatch: "类型差异",
  value_drift: "值漂移",
  time_skew: "时间错位",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------------------------------------------------------------------------
// 通用请求
// ---------------------------------------------------------------------------
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: options.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || (data.warnings || []).join("；") || `HTTP ${resp.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}

function setStatus(el, text, kind = "info") {
  el.textContent = text;
  el.style.color = kind === "error" ? "var(--danger)"
    : kind === "ok" ? "var(--ok)" : "var(--muted)";
}

// ---------------------------------------------------------------------------
// 选项卡
// ---------------------------------------------------------------------------
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "history") loadHistory();
  });
});

// ---------------------------------------------------------------------------
// 健康检查 & 数据源上传
// ---------------------------------------------------------------------------
async function checkHealth() {
  try {
    const h = await api("/api/health");
    $("#health").textContent = h.local_only ? "服务正常 · 纯本地" : "服务正常";
    $("#health").className = "badge badge-local";
    const src = await api("/api/sources");
    state.sources = { ...state.sources, ...src.sources };
    if (state.sources.offline) $("#path-offline").textContent = state.sources.offline;
    if (state.sources.online) $("#path-online").textContent = state.sources.online;
  } catch {
    $("#health").textContent = "本地服务未启动";
    $("#health").className = "badge badge-muted";
  }
}

async function uploadFile(inputId, role) {
  const input = $(`#file-${role}`);
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append(role, file);
    try {
      const data = await api("/api/upload", { method: "POST", body: fd });
      state.sources[role] = data.saved[role];
      $(`#path-${role}`).textContent = data.saved[role];
    } catch (err) {
      alert(`上传失败：${err.message}`);
    }
  });
}
uploadFile("file-offline", "offline");
uploadFile("file-online", "online");

// ---------------------------------------------------------------------------
// 规则配置
// ---------------------------------------------------------------------------
function renderFeatureRows() {
  const tbody = $("#feature-rows");
  tbody.innerHTML = "";
  state.features.forEach((f, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${escapeHtml(f.name)}</td>
      <td>
        <select data-k="dtype">
          ${["auto", "number", "string", "bool"].map((t) =>
            `<option value="${t}" ${f.dtype === t ? "selected" : ""}>${t}</option>`).join("")}
        </select>
      </td>
      <td><input type="number" step="any" data-k="abs_tolerance"
            value="${f.abs_tolerance ?? ""}" placeholder="继承全局"></td>
      <td><input type="number" step="any" data-k="rel_tolerance"
            value="${f.rel_tolerance ?? ""}" placeholder="继承全局"></td>
      <td><input type="number" data-k="decimal_precision"
            value="${f.decimal_precision ?? ""}" placeholder="继承全局"></td>
      <td>
        <select data-k="null_policy">
          <option value="">继承全局</option>
          ${["strict", "lenient", "as_zero"].map((t) =>
            `<option value="${t}" ${f.null_policy === t ? "selected" : ""}>${t}</option>`).join("")}
        </select>
      </td>
      <td>
        <select data-k="duplicate_strategy">
          <option value="">继承全局</option>
          ${["fail", "first", "last", "min", "max", "mean", "median", "majority"].map((t) =>
            `<option value="${t}" ${f.duplicate_strategy === t ? "selected" : ""}>${t}</option>`).join("")}
        </select>
      </td>
      <td><button class="row-del" title="删除">✕</button></td>`;
    tr.querySelectorAll("[data-k]").forEach((el) => {
      el.addEventListener("change", () => {
        let v = el.value;
        if (el.type === "number") v = v === "" ? null : Number(v);
        if (el.tagName === "SELECT" && v === "") v = null;
        state.features[idx][el.dataset.k] = v;
      });
    });
    tr.querySelector(".row-del").addEventListener("click", () => {
      state.features.splice(idx, 1);
      renderFeatureRows();
    });
    tbody.appendChild(tr);
  });
}

$("#btn-add-feature").addEventListener("click", () => {
  const name = $("#new-feature").value.trim();
  const dtype = $("#new-dtype").value;
  if (!name) return alert("请输入特征列名");
  if (state.features.some((f) => f.name === name)) return alert("特征已存在");
  state.features.push({
    name, dtype,
    abs_tolerance: null, rel_tolerance: null, decimal_precision: null,
    null_policy: null, duplicate_strategy: null, nullable: true,
  });
  $("#new-feature").value = "";
  renderFeatureRows();
});

$("#btn-save-rules").addEventListener("click", () => {
  setStatus($("#rules-status"), `已保存 ${state.features.length} 个特征的规则，可在“数据源”页开始比对。`, "ok");
});

function collectConfig() {
  const precision = $("#precision").value;
  return {
    name: $("#check-name").value.trim() || "未命名检查",
    entity_column: $("#entity-column").value.trim(),
    event_time_column: $("#time-column").value.trim(),
    timezone: $("#timezone").value.trim() || null,
    time_tolerance_seconds: Number($("#time-tol").value || 0),
    default_abs_tolerance: Number($("#abs-tol").value || 0),
    default_rel_tolerance: Number($("#rel-tol").value || 0),
    default_decimal_precision: precision === "" ? null : Number(precision),
    null_policy: $("#null-policy").value,
    duplicate_strategy: $("#dup-strategy").value,
    time_range_start: $("#range-start").value.trim() || null,
    time_range_end: $("#range-end").value.trim() || null,
    accept_timezone_warning: $("#accept-tz").checked,
    features: state.features.map((f) => ({ ...f })),
  };
}

// ---------------------------------------------------------------------------
// 执行比对
// ---------------------------------------------------------------------------
$("#btn-run").addEventListener("click", async () => {
  if (!state.sources.offline || !state.sources.online) {
    return alert("请先上传离线与线上 CSV 文件");
  }
  if (state.features.length === 0) {
    return alert("请先在“比对规则”页添加至少一个特征");
  }
  const statusEl = $("#run-status");
  setStatus(statusEl, "比对中…");
  try {
    const data = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({ config: collectConfig() }),
    });
    state.currentRun = data;
    setStatus(statusEl, `完成：运行 ${data.run.run_id}，共 ${data.summary.total_cells} 个单元格`, "ok");
    renderResults(data);
    switchTab("results");
  } catch (err) {
    if (err.payload && err.payload.blocked) {
      showTzBanner(err.payload.warnings);
      setStatus(statusEl, "时区不一致，运行已阻止，请处理提示后勾选确认再试", "error");
      switchTab("datasource");
    } else {
      setStatus(statusEl, `运行失败：${err.message}`, "error");
    }
  }
});

function showTzBanner(warnings) {
  const banner = $("#tz-banner");
  banner.classList.remove("hidden", "banner-warn");
  banner.className = "banner banner-error";
  banner.innerHTML = "<strong>时区/阻断提示</strong><ul style='margin:6px 0 0 18px'>"
    + warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("") + "</ul>";
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}

// ---------------------------------------------------------------------------
// 结果渲染
// ---------------------------------------------------------------------------
function renderResults(data) {
  renderSummary(data.summary);
  // 分类与特征下拉
  const catSel = $("#filter-category");
  const catVal = catSel.value;
  catSel.innerHTML = `<option value="">全部分类</option>`
    + Object.entries(DIFF_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  catSel.value = catVal;

  const featSel = $("#filter-feature");
  const featVal = featSel.value;
  featSel.innerHTML = `<option value="">全部特征</option>`
    + (data.run.config.features || []).map((f) => `<option value="${f.name}">${f.name}</option>`).join("");
  featSel.value = featVal;

  renderResultRows(data.results);
}

function renderSummary(s) {
  const cards = [
    { lbl: "实体数", num: s.entities },
    { lbl: "单元格总数", num: s.total_cells },
    { lbl: "一致", num: s.match, cls: "" },
    { lbl: "整行缺失", num: s.missing_row, cls: "diff" },
    { lbl: "空值差异", num: s.null_mismatch, cls: s.null_mismatch ? "diff" : "" },
    { lbl: "类型差异", num: s.type_mismatch, cls: s.type_mismatch ? "diff" : "" },
    { lbl: "值漂移", num: s.value_drift, cls: s.value_drift ? "diff" : "" },
    { lbl: "时间错位", num: s.time_skew, cls: s.time_skew ? "diff" : "" },
  ];
  $("#summary-cards").innerHTML = cards.map((c) =>
    `<div class="card ${c.cls}"><div class="num">${c.num}</div><div class="lbl">${c.lbl}</div></div>`
  ).join("");
}

function renderResultRows(results) {
  const tbody = $("#result-rows");
  $("#result-empty").classList.toggle("hidden", results.length > 0);
  tbody.innerHTML = "";
  results.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${escapeHtml(c.entity_id)}</td>
      <td class="mono">${escapeHtml(c.event_time)}</td>
      <td>${escapeHtml(c.feature)}</td>
      <td><span class="chip chip-${c.category}">${DIFF_LABELS[c.category] || c.category}</span>
        ${(c.secondary_flags || []).map((f) =>
          `<span class="chip chip-${f}" style="margin-left:4px">${DIFF_LABELS[f] || f}</span>`).join("")}
      </td>
      <td class="mono">${c.offline_raw === null || c.offline_raw === undefined ? "<em>空</em>" : escapeHtml(String(c.offline_raw))}</td>
      <td class="mono">${c.online_raw === null || c.online_raw === undefined ? "<em>空</em>" : escapeHtml(String(c.online_raw))}</td>
      <td>${escapeHtml(c.detail || "")}</td>
      <td><button class="cell-link">查看原值与规则</button></td>`;
    tr.querySelector(".cell-link").addEventListener("click", () => openDetail(c));
    tbody.appendChild(tr);
  });
}

$("#btn-apply-filter").addEventListener("click", async () => {
  if (!state.currentRun) return;
  const params = new URLSearchParams({
    run_id: state.currentRun.run.run_id,
    category: $("#filter-category").value,
    feature: $("#filter-feature").value,
    entity_id: $("#filter-entity").value.trim(),
  });
  const data = await api(`/api/run?${params}`);
  renderResultRows(data.results);
});

// 导出链接
$("#link-export").addEventListener("click", (e) => {
  if (!state.currentRun) { e.preventDefault(); return alert("暂无运行结果"); }
  e.target.href = `/api/export?run_id=${state.currentRun.run.run_id}&include_matches=1`;
});
$("#link-export-diff").addEventListener("click", (e) => {
  if (!state.currentRun) { e.preventDefault(); return alert("暂无运行结果"); }
  const cats = Object.keys(DIFF_LABELS).filter((c) => c !== "match").join(",");
  e.target.href = `/api/export?run_id=${state.currentRun.run.run_id}&categories=${cats}`;
});

// ---------------------------------------------------------------------------
// 下钻弹窗：原值 + 计算规则
// ---------------------------------------------------------------------------
function openDetail(c) {
  const rows = [
    ["实体 ID", c.entity_id],
    ["事件时间", c.event_time],
    ["特征", c.feature],
    ["差异分类", `<span class="chip chip-${c.category}">${DIFF_LABELS[c.category] || c.category}</span>`
      + (c.secondary_flags || []).map((f) =>
        ` <span class="chip chip-${f}">${DIFF_LABELS[f] || f}</span>`).join("")],
    ["并存标记", (c.secondary_flags || []).length ? c.secondary_flags.join(", ") : "-"],
    ["缺失方向", c.missing_side || "-"],
    ["离线原值", fmtVal(c.offline_raw, c.offline_type)],
    ["线上原值", fmtVal(c.online_raw, c.online_type)],
    ["离线解析值", fmtVal(c.offline_value, c.offline_type)],
    ["线上解析值", fmtVal(c.online_value, c.online_type)],
    ["as-of 离线来源时间", c.offline_source_time || "-"],
    ["线上来源时间", c.online_source_time || "-"],
    ["数值绝对差", c.numeric_delta ?? "-"],
    ["是否在容差内", c.within_tolerance === null || c.within_tolerance === undefined ? "-" : String(c.within_tolerance)],
    ["判定说明", c.detail],
  ];
  $("#modal-body").innerHTML = `
    <dl class="kv">
      ${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join("")}
    </dl>
    <div class="rule-box">生效计算规则\n${escapeHtml(JSON.stringify(c.rule, null, 2))}</div>`;
  $("#modal").classList.remove("hidden");
}
function fmtVal(v, type) {
  if (v === null || v === undefined || v === "") return "<em>空（null）</em>";
  return `<span class="mono">${escapeHtml(String(v))}</span> <small style="color:var(--muted)">[${type || "?"}]</small>`;
}
$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") $("#modal").classList.add("hidden");
});

// ---------------------------------------------------------------------------
// 历史与增量重算
// ---------------------------------------------------------------------------
async function loadHistory() {
  const data = await api("/api/runs");
  const tbody = $("#history-rows");
  tbody.innerHTML = "";
  data.runs.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${r.run_id}</td>
      <td class="mono">${r.created_at}</td>
      <td>${escapeHtml(r.name)}</td>
      <td>${r.total_cells}</td>
      <td>${r.diff_cells}</td>
      <td class="mono">${r.parent_run_id || "-"}</td>
      <td>
        <button class="cell-link act-open">打开结果</button> ·
        <button class="cell-link act-recompute">以此重算</button>
      </td>`;
    tr.querySelector(".act-open").addEventListener("click", async () => {
      state.currentRun = await api(`/api/run?run_id=${r.run_id}`);
      renderResults(state.currentRun);
      switchTab("results");
    });
    tr.querySelector(".act-recompute").addEventListener("click", async () => {
      state.selectedParent = r.run_id;
      $("#recompute-parent").textContent = r.run_id;
      $("#recompute-panel").classList.remove("hidden");
      $("#diff-panel").classList.add("hidden");
      // 始终从服务器拉取父运行的完整规则回填，便于在其基础上修改
      const parentRun = await api(`/api/run?run_id=${r.run_id}`);
      loadConfigIntoForm(parentRun.run.config);
      switchTab("rules");
    });
    tbody.appendChild(tr);
  });
}

function loadConfigIntoForm(config) {
  if (!config || !config.features) return;
  $("#check-name").value = config.name || "";
  $("#entity-column").value = config.entity_column || "entity_id";
  $("#time-column").value = config.event_time_column || "event_time";
  $("#timezone").value = config.timezone || "";
  $("#time-tol").value = config.time_tolerance_seconds ?? 0;
  $("#abs-tol").value = config.default_abs_tolerance ?? 0;
  $("#rel-tol").value = config.default_rel_tolerance ?? 0;
  $("#precision").value = config.default_decimal_precision ?? "";
  $("#null-policy").value = config.null_policy || "strict";
  $("#dup-strategy").value = config.duplicate_strategy || "fail";
  $("#range-start").value = config.time_range_start || "";
  $("#range-end").value = config.time_range_end || "";
  $("#accept-tz").checked = !!config.accept_timezone_warning;
  state.features = (config.features || []).map((f) => ({ ...f }));
  renderFeatureRows();
}

$("#btn-recompute").addEventListener("click", async () => {
  if (!state.selectedParent) return;
  try {
    const data = await api("/api/recompute", {
      method: "POST",
      body: JSON.stringify({
        config: collectConfig(),
        parent_run_id: state.selectedParent,
      }),
    });
    state.currentRun = data;
    $("#recompute-note").className = "banner banner-ok";
    $("#recompute-note").textContent = data.run.recompute_note || "重算完成";
    renderResults(data);
    await loadHistory();
  } catch (err) {
    if (err.payload && err.payload.blocked) {
      showTzBanner(err.payload.warnings);
    } else {
      alert(`重算失败：${err.message}`);
    }
  }
});

$("#btn-view-diff").addEventListener("click", async () => {
  if (!state.currentRun || !state.currentRun.run.parent_run_id) {
    return alert("当前运行不是重算产生的，没有父运行可对比");
  }
  const data = await api(
    `/api/run/diff?old_run_id=${state.currentRun.run.parent_run_id}`
    + `&new_run_id=${state.currentRun.run.run_id}`
  );
  $("#diff-panel").classList.remove("hidden");
  $("#diff-rows").innerHTML = data.changes.map((ch) => {
    const o = ch.old, n = ch.new;
    return `<tr>
      <td class="mono">${escapeHtml(ch.entity_id)}</td>
      <td class="mono">${escapeHtml(ch.event_time)}</td>
      <td>${escapeHtml(ch.feature)}</td>
      <td>${changeLabel(ch.change_type)}</td>
      <td>${o ? `<span class="chip chip-${o.category}">${DIFF_LABELS[o.category]}</span>` : "-"}</td>
      <td>${n ? `<span class="chip chip-${n.category}">${DIFF_LABELS[n.category]}</span>` : "-"}</td>
      <td class="mono">${o ? `${escapeHtml(String(o.offline_raw))} / ${escapeHtml(String(o.online_raw))}` : "-"}</td>
      <td class="mono">${n ? `${escapeHtml(String(n.offline_raw))} / ${escapeHtml(String(n.online_raw))}` : "-"}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" style="color:var(--muted)">两次运行结果完全一致，无变化。</td></tr>`;
});

function changeLabel(t) {
  return { new_cell: "新增", changed: "变化", removed: "移除" }[t] || t;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

// 初始化
checkHealth();
renderFeatureRows();
