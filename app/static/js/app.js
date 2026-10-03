/* OpenAPI Contract Evolution Workbench — vanilla JS frontend */
"use strict";

const state = {
  report: null,
  analysisId: null,
  files: { baseline: null, candidate: null },
  view: "endpoints",
  severities: new Set(["breaking", "undetermined"]),
  text: "",
  findingsById: new Map(),
  coverage: null,
  coverageSide: "baseline",
  coverageFilter: new Set(["covered", "invalid", "undetermined", "unmatched"]),
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const sevLabel = { breaking: "破坏性", undetermined: "无法判定", compatible: "兼容" };
const sevClass = {
  breaking: "sev-breaking", undetermined: "sev-undetermined",
  compatible: "sev-compatible",
};
const dotClass = {
  breaking: "dot-breaking", undetermined: "dot-undetermined",
  compatible: "dot-compatible",
};

// ------------------------------------------------------------- init
window.addEventListener("DOMContentLoaded", () => {
  bindUpload("baseline");
  bindUpload("candidate");
  $("#btn-analyze").addEventListener("click", runAnalysis);
  $$('input[type=checkbox][value]').forEach((cb) => {
    cb.addEventListener("change", () => {
      state.severities = new Set(
        $$('input[type=checkbox][value]').filter((x) => x.checked)
          .map((x) => x.value));
      render();
    });
  });
  $("#filter-text").addEventListener("input", (e) => {
    state.text = e.target.value.trim().toLowerCase();
    render();
  });
  $$(".tab").forEach((b) => b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.view = b.dataset.view;
    render();
  }));
  $$("[data-export]").forEach((b) =>
    b.addEventListener("click", () => exportReport(b.dataset.export)));
  $("#btn-history").addEventListener("click", openHistory);
  $("#history-close").addEventListener("click", closeHistory);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  initCoverage();
  refreshHealth();
});

// ------------------------------------------------------------- upload
function bindUpload(side) {
  const input = $(`#file-${side}`);
  const card = $(`#drop-${side}`);
  input.addEventListener("change", () => {
    if (input.files[0]) setFile(side, input.files[0]);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    card.addEventListener(ev, (e) => {
      e.preventDefault();
      card.classList.add("dragover");
    }));
  ["dragleave", "drop"].forEach((ev) =>
    card.addEventListener(ev, (e) => {
      e.preventDefault();
      card.classList.remove("dragover");
    }));
  card.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) {
      input.files = e.dataTransfer.files;
      setFile(side, f);
    }
  });
}

function setFile(side, file) {
  state.files[side] = file;
  const meta = $(`#meta-${side}`);
  meta.hidden = false;
  const sizeKb = (file.size / 1024).toFixed(1);
  meta.innerHTML = `已选择 <b>${escapeHtml(file.name)}</b> · ${sizeKb} KB`;
  $("#btn-analyze").disabled = !(state.files.baseline && state.files.candidate);
  $("#upload-error").textContent = "";
}

async function runAnalysis() {
  const btn = $("#btn-analyze");
  btn.disabled = true;
  btn.textContent = "比对中…";
  $("#upload-error").textContent = "";
  try {
    const fd = new FormData();
    fd.append("baseline", state.files.baseline);
    fd.append("candidate", state.files.candidate);
    const resp = await fetch("/api/analyses", { method: "POST", body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const doc = await resp.json();
    loadReport(doc);
  } catch (exc) {
    $("#upload-error").textContent = "导入/比对失败：" + exc.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "开始比对";
  }
}

function loadReport(doc) {
  state.report = doc;
  state.analysisId = doc.analysis_id;
  state.findingsById = new Map(doc.findings.map((f) => [f.id, f]));
  $("#panel-result").hidden = false;
  $("#analysis-id").textContent = "#" + doc.analysis_id;
  $("#fingerprint").textContent = doc.fingerprint || "";
  render();
  $("#panel-result").scrollIntoView({ behavior: "smooth", block: "start" });
  loadCoverageForAnalysis();
}

// ------------------------------------------------------------- render
function render() {
  const doc = state.report;
  if (!doc) return;
  const s = doc.summary;
  $("#stat-breaking").querySelector(".stat-num").textContent = s.breaking;
  $("#stat-undetermined").querySelector(".stat-num").textContent =
    s.undetermined;
  $("#stat-compatible").querySelector(".stat-num").textContent =
    s.compatible;
  $("#stat-engine").querySelector(".stat-num").textContent =
    s.engine_stats.cycles_terminated || 0;

  const findings = filteredFindings();
  if (state.view === "endpoints") renderEndpoints(findings);
  else renderFlat(findings);
}

function filteredFindings() {
  return state.report.findings.filter((f) => {
    if (!state.severities.has(f.severity)) return false;
    if (!state.text) return true;
    const hay = [f.rule, f.message, f.endpoint.path, f.endpoint.method,
      f.media_type || "", f.status_code || "",
      f.pointer.baseline || "", f.pointer.candidate || ""]
      .join(" ").toLowerCase();
    return hay.includes(state.text);
  });
}

function renderEndpoints(findings) {
  const box = $("#list-endpoints");
  $("#list-findings").hidden = true;
  box.hidden = false;
  const byEp = new Map();
  for (const f of findings) {
    const key = `${f.endpoint.method} ${f.endpoint.path}`;
    if (!byEp.has(key)) byEp.set(key, []);
    byEp.get(key).push(f);
  }
  // severity ordering
  const rank = { breaking: 0, undetermined: 1, compatible: 2 };
  const epMeta = new Map(state.report.endpoints.map(
    (e) => [`${e.method} ${e.path}`, e]));
  const keys = Array.from(byEp.keys()).sort((a, b) => {
    const fa = byEp.get(a)[0], fb = byEp.get(b)[0];
    const sa = fa.severity, sb = fb.severity;
    if (rank[sa] !== rank[sb]) return rank[sa] - rank[sb];
    return a.localeCompare(b);
  });
  box.innerHTML = "";
  if (!keys.length) {
    box.innerHTML = `<p class="hint">当前筛选条件下没有结果。</p>`;
    return;
  }
  for (const key of keys) {
    const [method, ...rest] = key.split(" ");
    const path = rest.join(" ");
    const fs = byEp.get(key);
    const meta = epMeta.get(key) || {};
    const worst = fs.some((f) => f.severity === "breaking") ? "breaking"
      : fs.some((f) => f.severity === "undetermined") ? "undetermined"
        : "compatible";
    const row = document.createElement("div");
    row.className = "ep-row";
    row.innerHTML = `
      <div class="ep-main">
        <span class="ep-chevron">▶</span>
        <span class="method ${methodClass(method)}">${method}</span>
        <span class="ep-path">${escapeHtml(path)}</span>
        ${meta.operation_id ? `<span class="ep-opid">${escapeHtml(meta.operation_id)}</span>` : ""}
        <span class="count-pill ${sevClass[worst]}">${fs.length}</span>
      </div>
      <div class="ep-findings"></div>`;
    const body = row.querySelector(".ep-findings");
    for (const f of fs) body.appendChild(findingRow(f));
    row.querySelector(".ep-main").addEventListener("click", () =>
      row.classList.toggle("open"));
    box.appendChild(row);
  }
}

function renderFlat(findings) {
  const box = $("#list-findings");
  $("#list-endpoints").hidden = true;
  box.hidden = false;
  box.innerHTML = "";
  if (!findings.length) {
    box.innerHTML = `<p class="hint">当前筛选条件下没有结果。</p>`;
    return;
  }
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = "finding-row";
    const loc = f.pointer.candidate || f.pointer.baseline || "—";
    row.innerHTML = `
      <span><span class="sev-dot ${dotClass[f.severity]}"></span>${sevLabel[f.severity]}</span>
      <span class="flat-endpoint"><span class="method ${methodClass(f.endpoint.method)}">${f.endpoint.method}</span> ${escapeHtml(f.endpoint.path)}</span>
      <span class="finding-msg">${escapeHtml(f.message)}</span>
      <span class="flat-loc" title="${escapeHtml(loc)}">${escapeHtml(loc)}</span>`;
    row.addEventListener("click", () => openDrawer(f.id));
    box.appendChild(row);
  }
}

function findingRow(f) {
  const row = document.createElement("div");
  row.className = "finding-row";
  const loc = f.media_type
    ? `${f.media_type}${f.status_code ? " · " + f.status_code : ""}`
    : (f.location || "");
  row.innerHTML = `
    <span><span class="sev-dot ${dotClass[f.severity]}"></span>${sevLabel[f.severity]}</span>
    <span class="finding-msg" title="${escapeHtml(f.rule)}">${escapeHtml(f.message)}</span>
    <span class="finding-loc" title="${escapeHtml(f.pointer.candidate || f.pointer.baseline || "")}">${escapeHtml(loc)}</span>`;
  row.addEventListener("click", () => openDrawer(f.id));
  return row;
}

function methodClass(m) {
  return ({ GET: "m-get", POST: "m-post", PUT: "m-put",
    PATCH: "m-patch", DELETE: "m-delete" })[m] || "m-other";
}

// ------------------------------------------------------------- drawer
async function openDrawer(id) {
  const f = state.findingsById.get(id);
  if (!f) return;
  const w = f.witness || {};
  const isEndpoint = w.kind === "endpoint";
  const isStructural = ["parameter", "body", "response", "media"]
    .includes(w.kind);
  $("#drawer-title").textContent = f.rule_title + " (" + f.rule + ")";
  const body = $("#drawer-body");
  const witnessHeading = isEndpoint
    ? "最小 JSON 请求反例（端点级，可编辑后重放）"
    : isStructural
      ? "结构化反例（可编辑后按 finding 语义重放）"
      : "最小 JSON 反例（仅单侧校验通过）";
  body.innerHTML = `
    <div class="detail-section">
      <span class="count-pill ${sevClass[f.severity]}">${sevLabel[f.severity]}</span>
      ${isEndpoint && w.verifiable === false
        ? `<span class="count-pill sev-undetermined" style="margin-left:8px">反例不可验证</span>`
        : ""}
    </div>
    <div class="detail-section">
      <h4>命中位置</h4>
      <dl class="kv">
        <dt>端点</dt><dd><span class="method ${methodClass(f.endpoint.method)}">${f.endpoint.method}</span> ${escapeHtml(f.endpoint.path)}</dd>
        <dt>位置</dt><dd>${escapeHtml(locationText(f))}</dd>
        <dt>JSON Pointer（基线）</dt><dd class="pointer-link">${escapeHtml(formatPtr(f, "baseline"))}</dd>
        <dt>JSON Pointer（候选）</dt><dd class="pointer-link">${escapeHtml(formatPtr(f, "candidate"))}</dd>
        <dt>关键字</dt><dd><code>${escapeHtml(f.facet || "—")}</code></dd>
      </dl>
    </div>
    <div class="detail-section">
      <h4>规则解释</h4>
      <div class="rule-explain"><b>${escapeHtml(f.message)}</b><br><br>${escapeHtml(f.rule_detail)}</div>
    </div>
    <div class="detail-section">
      <h4>${witnessHeading}</h4>
      ${isEndpoint
        ? `<p class="hint">字段：<code>method</code> / <code>path</code>（已代入具体路径参数值）/
             <code>query</code> / <code>header</code> / <code>cookie</code>（必填参数）/
             <code>body</code>（存在必填请求体时给出最小 JSON）。编辑后点重放，将按基线契约重新核对。</p>`
        : isStructural
          ? `<p class="hint">${structuralHint(w)}</p>`
          : ""}
      <textarea id="witness-instance" class="replay-box" style="width:100%;min-height:170px"></textarea>
      <div id="replay-output"></div>
      <button class="btn btn-primary btn-sm btn-replay" id="btn-replay">
        ▶ ${isEndpoint || isStructural ? "按 finding 语义重放（双侧）" : "重放校验（双侧）"}</button>
    </div>`;
  const ta = $("#witness-instance");
  ta.value = JSON.stringify(w.instance ?? null, null, 2);
  if (isEndpoint && w.verifiable === false && w.unverifiable_reason) {
    const warn = document.createElement("p");
    warn.className = "replay-warn";
    warn.textContent = "⚠ 反例不可验证：" + w.unverifiable_reason
      + "。破坏性结论保留，但不会宣称单侧校验通过；可手动构造请求后重放。";
    ta.parentElement.insertBefore(warn, $("#replay-output"));
  }
  if (w.note) {
    const note = document.createElement("p");
    note.className = "hint";
    note.style.marginTop = "6px";
    note.textContent = w.note;
    ta.parentElement.insertBefore(note, $("#replay-output"));
  }
  $("#btn-replay").addEventListener("click", () => replayFinding(f.id));
  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  // auto replay once
  replayFinding(f.id);
}

function structuralHint(w) {
  if (w.kind === "parameter") {
    return `字段：<code>method</code> / <code>path</code> / <code>parameter</code> /
      <code>in</code>（query/header/cookie/path）/ <code>value</code>。
      重放会分别核对基线是否声明（或作为可选项接受）该参数、候选是否缺失或把它收紧为必填。`;
  }
  if (w.kind === "body") {
    return `字段：<code>method</code> / <code>path</code> / <code>media_type</code> /
      <code>body</code>（最小 JSON 请求体）。重放会核对基线 requestBody 的声明/必填与
      载荷 schema，以及候选是否删除 requestBody 或将其收紧为必填。`;
  }
  if (w.kind === "response") {
    return `字段：<code>method</code> / <code>path</code> / <code>status_code</code> /
      <code>media_type</code> / <code>body</code>（该状态码下的最小响应体，可空）。
      重放会核对基线是否声明该状态码（并在有 schema 时校验响应体）、候选是否已删除该状态码。`;
  }
  if (w.kind === "media") {
    return `字段：<code>method</code> / <code>path</code> / <code>location</code> /
      <code>status_code</code>（响应媒体类型时）/ <code>media_type</code> /
      <code>body</code>。重放会核对基线是否声明并接受/返回该媒体类型、候选是否已删除。`;
  }
  return "结构化反例，可编辑后重放。";
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
}

async function replayFinding(id) {
  const out = $("#replay-output");
  out.innerHTML = `<p class="hint">重放中…</p>`;
  let instance = null;
  try {
    instance = JSON.parse($("#witness-instance").value);
  } catch (exc) {
    out.innerHTML = `<div class="replay-result replay-bad">JSON 解析失败：${escapeHtml(exc.message)}</div>`;
    return;
  }
  try {
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/findings/${id}/replay`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance }) });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    const r = await resp.json();
    renderReplay(r, out);
  } catch (exc) {
    out.innerHTML = `<div class="replay-result replay-bad">重放失败：${escapeHtml(exc.message)}</div>`;
  }
}

function renderReplay(r, out) {
  if (r.ok === false) {
    out.innerHTML = `<div class="replay-result replay-bad">重放失败：${escapeHtml(r.error || "未知错误")}</div>`;
    return;
  }
  if (r.kind === "endpoint") {
    renderEndpointReplay(r, out);
    return;
  }
  if (r.kind === "structural") {
    renderStructuralReplay(r, out);
    return;
  }
  const rb = r.root_validation.baseline;
  const rc = r.root_validation.candidate;
  const exact = r.exactly_one_side;
  const card = (name, side) => {
    const v = side.valid;
    return `<div class="replay-result ${v ? "replay-ok" : "replay-bad"}">
      ${v ? "✓" : "✗"} ${name}校验：<b>${v ? "通过" : "失败"}</b>
      ${!v && side.errors.length
        ? `<ul class="replay-errors">${side.errors.slice(0, 5)
            .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
    </div>`;
  };
  let facet = "";
  if (r.facet_validation) {
    const fv = r.facet_validation;
    facet = `<h4 style="margin:14px 0 6px;font-size:12px;color:var(--muted)">
      关键字路径 <code>${escapeHtml(fv.path)}</code> 处局部校验</h4>`;
    facet += ["baseline", "candidate"].map((k) => fv[k]
      ? card(k === "baseline" ? "基线" : "候选", fv[k])
      : "").join("");
  }
  out.innerHTML = `
    <div class="replay-result ${exact ? "replay-ok" : "replay-bad"}">
      ${exact ? "✓ 该反例恰好只被一侧接受（可用于演示破坏性）"
               : "⚠ 该反例并非恰好单侧通过（结构复杂时为近似示例，可手动编辑后重放）"}
    </div>
    ${card("基线", rb)}
    ${card("候选", rc)}
    ${facet}`;
}

function renderEndpointReplay(r, out) {
  const ec = r.endpoint_check || {};
  const bc = r.baseline_check || {};
  const existenceCard = (name, exists, detail) => `
    <div class="replay-result ${exists ? "replay-ok" : "replay-bad"}">
      ${exists ? "✓" : "✗"} ${name}端点：<b>${exists ? "存在" : "不存在"}</b>
      <div class="replay-sub">${escapeHtml(detail || "")}</div>
    </div>`;
  const errorsList = (errors) => (errors && errors.length
    ? `<ul class="replay-errors">${errors.slice(0, 8)
        .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : "");
  const caveats = (bc.caveats && bc.caveats.length)
    ? `<ul class="replay-errors replay-caveats">${bc.caveats
        .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : "";
  const verdict = r.verifiable
    ? `<div class="replay-result replay-ok">
         ✓ 反例可验证：<b>基线接受该请求，候选无此端点</b>（端点删除的破坏性成立）
       </div>`
    : `<div class="replay-result replay-bad">
         ⚠ 反例不可验证：<b>${escapeHtml(r.unverifiable_reason
           || "未能证明基线接受 / 候选缺失，不宣称单侧通过")}</b><br>
         端点删除的破坏性结论仍然保留；可在编辑器中修正请求后再次重放。
       </div>`;
  out.innerHTML = `
    ${verdict}
    ${existenceCard("基线", ec.baseline, ec.baseline_detail)}
    ${existenceCard("候选", ec.candidate, ec.candidate_detail)}
    <div class="replay-result ${bc.valid ? "replay-ok" : "replay-bad"}">
      ${bc.valid ? "✓" : "✗"} 基线请求约束核对：<b>${bc.valid ? "请求满足基线契约" : "请求违反基线契约"}</b>
      ${errorsList(bc.errors)}
      ${caveats}
    </div>`;
  // if the server rebuilt a missing request from the baseline, reflect it
  // back into the editor so the user can inspect/edit the concrete payload
  const ta = $("#witness-instance");
  if (ta && r.instance && JSON.stringify(r.instance) !== ta.value.trim()
      && (ta.value.trim() === "null" || ta.value.trim() === "")) {
    ta.value = JSON.stringify(r.instance, null, 2);
  }
}

function structuralVerdictText(r) {
  switch (r.rule) {
    case "PARAMETER_MADE_REQUIRED":
      return "基线允许省略该参数 / requestBody，候选将其收紧为必填";
    case "PARAMETER_ADDED_REQUIRED":
      return "基线不要求该参数 / requestBody，候选新增了必填要求";
    case "PARAMETER_REMOVED":
      return r.location === "requestBody"
        ? "基线声明并接受该请求体，候选已删除 requestBody"
        : "基线声明并接受该参数，候选已删除该参数";
    case "STATUS_REMOVED":
      return "基线声明了该响应状态码，候选已删除该状态码";
    case "MEDIA_REMOVED":
      return r.direction === "response"
        ? "基线声明并返回该媒体类型，候选已删除该媒体类型"
        : "基线声明并接受该媒体类型，候选已删除该媒体类型";
    default:
      return "按 finding 语义双侧核对完成";
  }
}

function renderStructuralReplay(r, out) {
  const sc = r.structural_check || {};
  const checkCard = (name, chk) => {
    if (!chk) return "";
    const rows = [];
    if (typeof chk.declared !== "undefined") {
      rows.push(["声明情况", chk.declared ? "已声明" : "未声明"]);
    }
    if (typeof chk.required !== "undefined") {
      rows.push(["required", chk.required ? "必填（true）" : "可选（false）"]);
    }
    if (chk.media_type) rows.push(["媒体类型", chk.media_type]);
    const errs = chk.errors || [];
    return `<div class="replay-result ${chk.ok ? "replay-ok" : "replay-bad"}">
      ${chk.ok ? "✓" : "✗"} ${name}：<b>${escapeHtml(chk.detail || "")}</b>
      ${rows.length ? `<dl class="kv replay-kv">${rows.map(([k, v]) =>
        `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>` : ""}
      ${errs.length
        ? `<ul class="replay-errors">${errs.slice(0, 8)
            .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
    </div>`;
  };
  const cv = r.carried_value;
  let carried = "";
  if (cv) {
    const sideCard = (title, s) => {
      if (!s) return "";
      if (s.applicable === false) {
        return `<div class="replay-result replay-na">— ${title}：${escapeHtml(s.detail || "该侧无对应契约")}</div>`;
      }
      return `<div class="replay-result ${s.valid ? "replay-ok" : "replay-bad"}">
        ${s.valid ? "✓" : "✗"} ${title}载荷校验：<b>${s.valid ? "通过" : "失败"}</b>
        ${s.media_type ? `（<code>${escapeHtml(s.media_type)}</code>）` : ""}
        ${(s.errors || []).length
          ? `<ul class="replay-errors">${s.errors.slice(0, 8)
              .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      </div>`;
    };
    carried = `<h4 style="margin:14px 0 6px;font-size:12px;color:var(--muted)">
      ${escapeHtml(cv.label || "携带值 / 载荷核对")}</h4>
      ${sideCard("基线", cv.baseline)}${sideCard("候选", cv.candidate)}`;
  }
  const verdict = r.verifiable
    ? `<div class="replay-result replay-ok">
         ✓ 反例可验证：<b>${escapeHtml(structuralVerdictText(r))}</b>
       </div>`
    : `<div class="replay-result replay-bad">
         ⚠ 反例不可验证：<b>${escapeHtml(r.unverifiable_reason
           || "未能按 finding 语义证明两侧的声明/收紧差异")}</b><br>
         破坏性结论仍然保留；可在编辑器中修正结构化反例后再次重放。
       </div>`;
  out.innerHTML = `
    ${verdict}
    ${checkCard("基线核对", sc.baseline)}
    ${checkCard("候选核对", sc.candidate)}
    ${carried}`;
}

function locationText(f) {
  const parts = [];
  if (f.location) parts.push({
    parameter: "参数", requestBody: "请求体", response: "响应",
  }[f.location] || f.location);
  if (f.status_code) parts.push("HTTP " + f.status_code);
  if (f.media_type) parts.push(f.media_type);
  return parts.join(" · ") || "端点";
}

function formatPtr(f, side) {
  const file = f.file[side];
  const pointer = f.pointer[side];
  if (!pointer) return "—";
  const sameFile = !file || file === state.report.packages[side]?.entry;
  return sameFile ? "#" + pointer : `${file}#${pointer}`;
}

// ------------------------------------------------------------- export
function exportReport(kind) {
  const map = {
    all: "",
    breaking: "severity=breaking",
    undetermined: "severity=undetermined",
    compatible: "severity=compatible",
  };
  const qs = map[kind];
  const url = `/api/analyses/${state.analysisId}/export${qs ? "?" + qs : ""}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ------------------------------------------------------------- history
async function openHistory() {
  $("#modal-history").hidden = false;
  const list = $("#history-list");
  list.innerHTML = `<p class="hint" style="padding:8px 18px">加载中…</p>`;
  try {
    const resp = await fetch("/api/analyses");
    const data = await resp.json();
    if (!data.items.length) {
      list.innerHTML = `<p class="hint" style="padding:8px 18px">暂无历史记录。</p>`;
      return;
    }
    list.innerHTML = "";
    for (const a of data.items) {
      const item = document.createElement("div");
      item.className = "history-item";
      const st = a.stats || {};
      item.innerHTML = `
        <div class="history-names">
          <b>${escapeHtml(a.baseline_name)}</b> → ${escapeHtml(a.candidate_name)}
          <small>${a.created_at ? new Date(a.created_at).toLocaleString() : ""}
            · <span style="color:var(--breaking)">${st.finding_breaking || 0} 破坏</span>
            · <span style="color:var(--undetermined)">${st.finding_undetermined || 0} 待定</span>
            · <code>${a.id}</code></small>
        </div>
        <div>
          <button class="btn btn-sm">打开</button>
        </div>`;
      item.querySelector("button").addEventListener("click", () =>
        loadHistory(a.id));
      list.appendChild(item);
    }
  } catch (exc) {
    list.innerHTML = `<p class="error-text" style="padding:8px 18px">加载失败：${escapeHtml(exc.message)}</p>`;
  }
}

async function loadHistory(id) {
  const resp = await fetch(`/api/analyses/${id}`);
  if (!resp.ok) return;
  const doc = await resp.json();
  loadReport(doc);
  closeHistory();
}

function closeHistory() {
  $("#modal-history").hidden = true;
}

// ------------------------------------------------------------- health
async function refreshHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    const el = $("#storage-badge");
    if (h.storage.available) {
      el.className = "badge badge-ok";
      el.textContent = "存储：PostgreSQL";
    } else {
      el.className = "badge badge-warn";
      el.textContent = "存储：内存降级模式";
      el.title = h.storage.error || "";
    }
  } catch {
    const el = $("#storage-badge");
    el.className = "badge badge-warn";
    el.textContent = "存储：不可用";
  }
}

// ------------------------------------------------------------- utils
function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// ============================================================ call coverage
const covOutcomeLabel = {
  covered: "已覆盖", invalid: "校验失败", undetermined: "无法判定",
  unmatched: "未匹配/排除",
};
const covReasonHints = {
  malformed_json: "该行 JSON 解析失败",
  missing_method: "缺少 method",
  invalid_method: "method 不是合法 HTTP 方法",
  missing_path: "缺少以 / 开头的 path",
  malformed_field: "字段结构不合法",
  missing_response: "缺少 response",
  missing_status: "缺少 response.status",
  invalid_status: "status 不在 100-599",
  path_not_matched: "路径不匹配任何 operation",
  ambiguous_template: "路径同时命中多个模板（未归属）",
  status_not_declared: "状态码无对应精确/范围/default 声明",
  status_media_not_json: "该状态码仅声明非 JSON 媒体（排除）",
  response_media_not_json: "响应媒体非 JSON（排除）",
  response_media_not_declared: "响应媒体未在该状态码声明",
  response_media_missing: "有响应实体但缺少 media_type",
  response_entity_expected: "声明 JSON 实体但调用无实体",
  response_entity_mismatch: "声明无实体但调用返回实体",
  missing_required_parameter: "缺少必填参数",
  parameter_invalid: "参数不满足 schema",
  parameter_schema_undecidable: "参数 schema 无法判定",
  request_body_undeclared: "未声明 requestBody 却携带实体",
  request_media_not_json: "请求媒体非 JSON，无法判定请求体",
  request_media_not_declared: "请求媒体未声明",
  request_body_invalid: "请求体不满足 schema",
  request_body_schema_undecidable: "请求体 schema 无法判定",
  missing_required_request_body: "缺少必填请求体",
  response_body_invalid: "响应体不满足 schema",
  response_body_schema_undecidable: "响应体 schema 无法判定",
};

function initCoverage() {
  $$(".cside").forEach((b) => b.addEventListener("click", () => {
    $$(".cside").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.coverageSide = b.dataset.side;
    loadCoverageDoc();
  }));
  $("#btn-upload-calls").addEventListener("click", uploadCoverage);
  $("#btn-delete-calls").addEventListener("click", deleteCoverage);
  $("#btn-export-coverage").addEventListener("click", () => {
    if (!state.coverage) return;
    const a = document.createElement("a");
    a.href = `/api/analyses/${state.analysisId}/coverage/`
      + `${state.coverageSide}/export`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
  $("#cov-hide-covered").addEventListener("change", renderCoverage);
  $("#cov-filter-text").addEventListener("input", () => {
    renderCoverageOperations();
    renderCoverageRows();
  });
  $$(".cov-rowf").forEach((cb) => {
    cb.checked = state.coverageFilter.has(cb.value);
    cb.addEventListener("change", () => {
      state.coverageFilter = new Set(
        $$(".cov-rowf").filter((x) => x.checked).map((x) => x.value));
      renderCoverageRows();
    });
  });
  $("#panel-coverage").hidden = true;
}

async function loadCoverageForAnalysis() {
  $("#panel-coverage").hidden = false;
  $("#coverage-sub").textContent = "#" + state.analysisId;
  // discover which sides already have stored coverage, keep current selection
  try {
    const data = await (await fetch(
      `/api/analyses/${state.analysisId}/coverage`)).json();
    const sides = (data.items || []).map((x) => x.side);
    if (!sides.includes(state.coverageSide) && sides.length) {
      state.coverageSide = sides[0];
    }
  } catch { /* listing is best-effort */ }
  $$(".cside").forEach((b) =>
    b.classList.toggle("active", b.dataset.side === state.coverageSide));
  await loadCoverageDoc();
}

async function loadCoverageDoc() {
  const errEl = $("#coverage-upload-error");
  errEl.textContent = "";
  state.coverage = null;
  try {
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/coverage/${state.coverageSide}`);
    if (resp.status === 404) {
      renderCoverage();
      return;
    }
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    state.coverage = await resp.json();
  } catch (exc) {
    errEl.textContent = "加载覆盖结果失败：" + exc.message;
  }
  renderCoverage();
}

async function uploadCoverage() {
  const input = $("#file-calls");
  const errEl = $("#coverage-upload-error");
  errEl.textContent = "";
  if (!input.files[0]) {
    errEl.textContent = "请先选择 NDJSON 调用记录文件";
    return;
  }
  const btn = $("#btn-upload-calls");
  btn.disabled = true;
  btn.textContent = "计算中…";
  try {
    const fd = new FormData();
    fd.append("calls", input.files[0]);
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/coverage/${state.coverageSide}`,
      { method: "POST", body: fd });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    state.coverage = await resp.json();
    renderCoverage();
  } catch (exc) {
    errEl.textContent = "上传/计算失败：" + exc.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "上传并计算覆盖";
  }
}

async function deleteCoverage() {
  const errEl = $("#coverage-upload-error");
  try {
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/coverage/${state.coverageSide}`,
      { method: "DELETE" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    state.coverage = null;
    renderCoverage();
  } catch (exc) {
    errEl.textContent = "删除失败：" + exc.message;
  }
}

// ------------------------------------------------------------- rendering
function renderCoverage() {
  const doc = state.coverage;
  $("#coverage-empty").hidden = !!doc;
  $("#coverage-body").hidden = !doc;
  $("#btn-export-coverage").disabled = !doc;
  $("#btn-delete-calls").hidden = !doc;
  const meta = $("#coverage-meta");
  if (!doc) {
    meta.textContent = "";
    return;
  }
  const s = doc.summary;
  $("#cov-cells").textContent = `${s.cells_covered}/${s.cells_total}`;
  $("#cov-gaps").textContent = s.gaps_total;
  $("#cov-covered-calls").textContent = s.covered;
  $("#cov-undetermined-calls").textContent = s.undetermined;
  $("#cov-invalid-calls").textContent = s.invalid;
  $("#cov-unmatched-calls").textContent = s.unmatched;
  const src = doc.source || {};
  meta.innerHTML = `<code>${escapeHtml(src.filename || "")}</code> · 侧：${
    doc.side === "baseline" ? "基线" : "候选"} · SHA-256 <code>${
    escapeHtml((src.sha256 || "").slice(0, 16))}…</code> · ${
    src.non_empty_lines ?? s.calls_total} 行`;
  renderCoverageOperations();
  renderCoverageRows();
}

function renderCoverageOperations() {
  const box = $("#cov-operations");
  const doc = state.coverage;
  const hideCovered = $("#cov-hide-covered").checked;
  const text = $("#cov-filter-text").value.trim().toLowerCase();
  box.innerHTML = "";
  const ops = doc.operations.filter((op) => {
    if (hideCovered && op.cells_total > 0
        && op.cells_covered === op.cells_total) return false;
    if (!text) return true;
    const hay = [op.method, op.path, op.operation_id || "",
      ...op.cells.map((c) => c.status_key + " " + (c.media_type || "")),
      ...(op.excluded_media || []).flatMap((e) => e.media_types)]
      .join(" ").toLowerCase();
    return hay.includes(text);
  });
  if (!ops.length) {
    box.innerHTML = `<p class="hint">当前筛选下没有 operation。</p>`;
    return;
  }
  for (const op of ops) {
    const div = document.createElement("div");
    div.className = "cov-op";
    if (!op.applicable) {
      div.innerHTML = `
        <div class="cov-op-head">
          <span class="cov-chevron">▶</span>
          <span class="method ${methodClass(op.method)}">${op.method}</span>
          <span class="cov-op-title">${escapeHtml(op.path)}</span>
          <span class="count-pill sev-undetermined">无适用格</span>
        </div>
        <div class="cov-op-body">
          <div class="cov-na">该 operation 没有可覆盖格：所有响应都未声明 JSON 内容，
            或仅声明非 JSON 媒体类型（已排除）。分母为零，不计入覆盖率。</div>
          ${(op.excluded_media || []).length ? excludedHtml(op) : ""}
        </div>`;
    } else {
      const allCovered = op.cells_covered === op.cells_total;
      const pill = allCovered
        ? `<span class="count-pill sev-compatible">${op.cells_covered}/${op.cells_total}</span>`
        : `<span class="count-pill sev-breaking">${op.cells_covered}/${op.cells_total} · 缺口 ${op.gaps.length}</span>`;
      div.innerHTML = `
        <div class="cov-op-head">
          <span class="cov-chevron">▶</span>
          <span class="method ${methodClass(op.method)}">${op.method}</span>
          <span class="cov-op-title">${escapeHtml(op.path)}
            ${op.operation_id ? `<small class="cov-op-sub">${escapeHtml(op.operation_id)}</small>` : ""}
          </span>
          ${pill}
        </div>
        <div class="cov-op-body">
          <div class="cov-grid">${gridHtml(op)}</div>
          ${op.gaps.length
            ? `<div class="cov-excluded">缺口格：${op.gaps.map(escapeHtml)
                .join("，")}</div>`
            : `<div class="cov-cell-yes">✓ 该 operation 的所有格均已覆盖</div>`}
          ${(op.excluded_media || []).length ? excludedHtml(op) : ""}
        </div>`;
    }
    div.querySelector(".cov-op-head").addEventListener("click", () =>
      div.classList.toggle("open"));
    box.appendChild(div);
  }
}

function excludedHtml(op) {
  const items = (op.excluded_media || []).map((e) =>
    `${e.status_key}: ${e.media_types.join(", ")}`).join("；");
  return `<div class="cov-excluded">非 JSON 媒体（不建格）：${escapeHtml(items)}</div>`;
}

function gridHtml(op) {
  const head = `<tr><th>状态键</th><th>匹配</th><th>JSON 媒体类型</th>
    <th>实体</th><th>状态</th><th>命中行</th></tr>`;
  const rows = op.cells.map((c) => {
    const lines = (c.covered_by || []).map((b) => `L${b.line}`).join(", ");
    return `<tr>
      <td><code>${escapeHtml(c.status_key)}</code></td>
      <td class="cov-cell-na">${statusKindText(c.status_kind)}</td>
      <td>${c.media_type ? `<code>${escapeHtml(c.media_type)}</code>`
        : '<span class="cov-cell-na">— 无 content —</span>'}</td>
      <td class="cov-cell-na">${c.has_content ? "有实体" : "无实体"}</td>
      <td>${c.covered
        ? '<span class="cov-cell-yes">✓ 已覆盖</span>'
        : '<span class="cov-cell-no">✗ 缺口</span>'}</td>
      <td class="cov-lines">${escapeHtml(lines)}</td>
    </tr>`;
  }).join("");
  return `<table class="cov-table"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function statusKindText(kind) {
  return { exact: "精确值", range: "范围", default: "default" }[kind] || kind;
}

function renderCoverageRows() {
  const box = $("#cov-rows");
  if (!state.coverage) { box.innerHTML = ""; return; }
  const text = ($("#cov-filter-text").value || "").trim().toLowerCase();
  const rows = state.coverage.rows.filter((r) => {
    if (!state.coverageFilter.has(r.outcome)) return false;
    if (!text) return true;
    const hay = [r.line, r.method, r.path, r.cell, r.status_key,
      ...r.reasons.map((x) => x.code + " " + x.message)]
      .join(" ").toLowerCase();
    return hay.includes(text);
  });
  if (!rows.length) {
    box.innerHTML = `<p class="hint">当前筛选下没有调用行。</p>`;
    return;
  }
  box.innerHTML = "";
  for (const r of rows) {
    const div = document.createElement("div");
    div.className = "cov-row";
    const opTxt = r.operation
      ? `${r.operation.method} ${r.operation.path}` : "—";
    div.innerHTML = `
      <div class="cov-row-head">
        <span class="cov-line-no">L${r.line}</span>
        <span class="cov-out-${r.outcome}">${covOutcomeLabel[r.outcome]}</span>
        <span class="method ${methodClass(r.method)}">${escapeHtml(r.method || "?")}</span>
        <span class="cov-row-main">${escapeHtml(r.path || "（无 path）")}
          ${r.status ? `→ ${r.status}` : ""}
          ${r.cell ? `· <code>${escapeHtml(r.cell)}</code>` : ""}</span>
        <span class="cov-cell-tag">${escapeHtml(opTxt)}</span>
      </div>
      <div class="cov-row-detail">
        <ul class="cov-reasons">
          ${r.reasons.length
            ? r.reasons.map((x) => `<li><span class="cov-reason-code">${
                escapeHtml(x.code)}</span> — ${escapeHtml(x.message)}
                ${hintFor(x.code)}</li>`).join("")
            : '<li class="cov-cell-yes">所有可判定约束通过，覆盖对应格</li>'}
        </ul>
      </div>`;
    div.querySelector(".cov-row-head").addEventListener("click", () =>
      div.classList.toggle("open"));
    box.appendChild(div);
  }
}

function hintFor(code) {
  const h = covReasonHints[code];
  return h ? `<small class="cov-op-sub">（${escapeHtml(h)}）</small>` : "";
}
