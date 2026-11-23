"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (x) => x == null ? "-" : "₹" + Number(x).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const cls = (outcome) => (outcome || "RUNNING").replace(" ", "_");
const STATUS_WORD = { pass: "Pass", info: "Info", warn: "Note", hold: "Hold", reject: "Reject" };

const fmtTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false }); };
const REASON_NAME = (c) => ({ DUP_NUMBER: "Duplicate invoice", DUP_FILE: "Duplicate invoice", DUP_SUSPECT: "Possible duplicate",
  DUP_IN_FLIGHT: "Already in exception queue", BANK_MISMATCH: "Bank details changed", VENDOR_BLOCKED: "Blocked vendor",
  VENDOR_UNKNOWN: "Unknown vendor", QTY_EXCEEDS_PO: "Billed beyond PO quantity", PO_BALANCE_EXCEEDED: "Over PO value",
  PRICE_VARIANCE: "Price above PO", MISSING_FIELD: "Missing invoice data", ARITHMETIC: "Arithmetic error",
  PO_NOT_FOUND: "PO not found", PO_UNRESOLVED: "PO not identified" }[c.code] || c.label);
let CONFIG = null, SAMPLES = [], current = null, source = null, timerInt = null, filter = "ALL", RUNS = [];

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ---------------- tabs ---------------- */
function showTab(name) {
  $$("nav button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "dashboard") loadDashboard();
  if (name === "reference") loadReference();
  history.replaceState(null, "", "#" + name);
}
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showPane(name) {
  $$(".vtabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.v === name));
  $$(".vpane").forEach((p) => p.classList.toggle("active", p.id === "v-" + name));
}
$$(".vtabs button").forEach((b) => b.addEventListener("click", () => showPane(b.dataset.v)));

/* ---------------- init ---------------- */
async function init() {
  CONFIG = await api("/api/config");
  $("#company").textContent = "Accounts payable · " + CONFIG.company.name;
  const ex = CONFIG.extractor;
  $("#engine").textContent = ex === "rules" ? "Extractor: OCR + rules (offline)" : "Extractor: " + ex.replace(":", " · ") + " + OCR cross-check";
  SAMPLES = await api("/api/samples");
  $("#samples").innerHTML = SAMPLES.map((s, i) => `
    <li><button data-sample="${esc(s.file)}" title="${esc(s.scenario)}">
      <span class="s-label">${esc(s.label)}</span>
      <span class="s-meta"><span>${esc(s.vendor)}</span><span>${inr(s.total)}</span></span>
    </button></li>`).join("");
  $$("#samples button").forEach((b) => b.addEventListener("click", () => startRun({ sample: b.dataset.sample })));
  renderStages();
  const tab = location.hash.slice(1);
  if (["dashboard", "reference"].includes(tab)) showTab(tab);
}

/* ---------------- upload ---------------- */
const drop = $("#drop"), fileInput = $("#file");
fileInput.addEventListener("change", () => fileInput.files[0] && startRun({ file: fileInput.files[0] }));
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && startRun({ file: e.dataTransfer.files[0] }));

/* ---------------- run ---------------- */
async function startRun({ sample, file }) {
  const fd = new FormData();
  if (sample) fd.append("sample", sample); else fd.append("file", file);
  $$("#samples button").forEach((b) => b.setAttribute("aria-current", b.dataset.sample === sample));
  const meta = SAMPLES.find((s) => s.file === sample);
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: fd });
    watch(run_id, sample || file.name, meta, true);
  } catch (e) {
    $("#run-title").textContent = "Could not start the run";
    $("#run-sub").textContent = e.message;
  } finally { fileInput.value = ""; }
}

function watch(runId, name, meta, live) {
  showTab("process");
  if (source) source.close();
  clearInterval(timerInt);
  current = { id: runId, result: null };
  $("#run-title").textContent = `Run #${runId} · ${name}`;
  $("#run-sub").textContent = meta ? meta.scenario : (live ? "Uploaded invoice" : "Replay of a stored run");
  renderStages();
  $("#v-decision").innerHTML = `<div class="empty">Working through the checks…</div>`;
  $("#v-fields").innerHTML = `<div class="empty">Waiting for extraction…</div>`;
  $("#v-doc").innerHTML = `<iframe class="doc-frame" title="Original invoice" src="/api/runs/${runId}/file"></iframe>`;
  showPane("decision");
  const t0 = performance.now();
  if (live) timerInt = setInterval(() => ($("#run-timer").textContent = ((performance.now() - t0) / 1000).toFixed(1) + " s"), 100);
  else $("#run-timer").textContent = "";

  source = new EventSource(`/api/runs/${runId}/events`);
  source.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "stage_start") setStage(ev.stage, "running");
    if (ev.type === "stage_done") {
      setStage(ev.stage, ev.payload.status, ev.payload);
      if (ev.stage === "extract" && ev.payload.data.fields) renderFields(ev.payload.data);
    }
    if (ev.type === "done") {
      source.close(); clearInterval(timerInt);
      current.result = ev.result;
      $("#run-timer").textContent = (ev.result.duration_ms / 1000).toFixed(1) + " s end to end";
      renderDecision(ev.result, live);
    }
    if (ev.type === "error") {
      source.close(); clearInterval(timerInt);
      $("#v-decision").innerHTML = `<div class="empty">The run stopped: ${esc(ev.message)}</div>`;
    }
  };
  source.onerror = () => { if (!current.result) { source.close(); clearInterval(timerInt); } };
}

function renderStages() {
  $("#stages").innerHTML = CONFIG.stages.map((s) => `
    <li class="stage pending" id="st-${s.key}">
      <div class="st-head"><span class="st-name">${esc(s.name)}</span><span class="st-ms"></span></div>
      <div class="st-desc">${esc(s.desc)}</div>
      <div class="st-body"></div>
    </li>`).join("");
}

function setStage(key, status, payload) {
  const el = $("#st-" + key);
  if (!el) return;
  el.className = "stage " + status;
  if (!payload) return;
  $(".st-ms", el).textContent = payload.ms + " ms";
  const checks = payload.checks.filter((c) => c.code !== "DECISION").map((c) => `
    <li class="check"><span class="pill ${c.status}">${STATUS_WORD[c.status]}</span>
      <span><b>${esc(c.label)}</b>${c.detail ? ` <span class="detail">${esc(c.detail)}</span>` : ""}</span></li>`).join("");
  const logs = payload.logs.length ? `<div class="logs">${esc(payload.logs.join("\n"))}</div>` : "";
  $(".st-body", el).innerHTML = (checks ? `<ul class="checks">${checks}</ul>` : "") + logs;
}

function renderFields(d) {
  const f = d.fields, conf = f.field_confidence || {};
  const row = (label, key, val) => {
    const c = conf[key];
    const low = c != null && c < (CONFIG.rules.min_extraction_confidence + 0.15);
    return `<tr><td>${esc(label)}</td><td class="${val == null || val === "" ? "lowconf" : ""}">${val == null || val === "" ? "Not found" : esc(val)}${c != null && val != null ? ` <span class="st-ms ${low ? "lowconf" : ""}">${Math.round(c * 100)}%</span>` : ""}</td></tr>`;
  };
  const lines = (f.line_items || []).map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${esc(l.quantity)}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("");
  const taxes = (f.tax_lines || []).map((t) => `${t.type} ${t.rate ?? "?"}% ${inr(t.amount)}`).join(", ");
  $("#v-fields").innerHTML = `
    <p class="muted" style="margin:0 0 12px">Read by <b>${esc(d.engine)}</b> · key-field confidence ${Math.round((d.confidence || 0) * 100)}%</p>
    <table class="kv">
      ${row("Vendor", "vendor_name", f.vendor_name)}${row("Vendor GSTIN", "vendor_gstin", f.vendor_gstin)}
      ${row("Billed to GSTIN", "buyer_gstin", f.buyer_gstin)}${row("Invoice number", "invoice_number", f.invoice_number)}
      ${row("Invoice date", "invoice_date", f.invoice_date)}${row("PO reference", "po_number", f.po_number)}
      ${row("Subtotal", "subtotal", f.subtotal == null ? null : inr(f.subtotal))}<tr><td>Tax</td><td>${esc(taxes) || "Not found"}</td></tr>
      ${row("Total", "total", f.total == null ? null : inr(f.total))}
      ${row("Remit-to account", "bank_account", f.bank_account ? `${f.bank_name || ""} ${f.bank_account} · ${f.ifsc || ""}` : null)}
      ${(f.notes || []).length ? `<tr><td>Remarks on invoice</td><td>${esc(f.notes.join(" "))}</td></tr>` : ""}
    </table>
    <h2 class="mt">Line items</h2>
    <table><thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead><tbody>${lines || `<tr><td colspan="4" class="lowconf">No line items found</td></tr>`}</tbody></table>`;
}

function renderDecision(res, animate) {
  const d = res.decision;
  if (!d) { $("#v-decision").innerHTML = `<div class="empty">No decision was produced.</div>`; return; }
  const reasons = d.reasons.map((c) => `<li class="${c.status}"><b>${esc(c.label)}</b><br>${esc(c.detail)}</li>`).join("");
  const warns = d.warnings.map((c) => `<li class="warn"><b>${esc(c.label)}</b><br>${esc(c.detail)}</li>`).join("");
  const msg = d.vendor_message;
  const amount = d.outcome === "APPROVED"
    ? `<div class="amount">${inr(d.payable)}<small>approved to pay</small></div>`
    : `<div class="amount">${inr(res.extracted && res.extracted.total)}<small>${d.outcome === "REJECTED" ? "not payable" : "held, not paid"}</small></div>`;
  $("#v-decision").innerHTML = `
    <div class="stamp-row"><span class="stamp ${cls(d.outcome)} ${animate ? "thunk" : ""}">${esc(d.outcome)}</span>${amount}</div>
    <p class="headline">${esc(d.headline)}</p>
    <p class="summary">${esc(d.summary)}</p>
    <dl class="facts">
      <dt>Routed to</dt><dd>${esc(d.routing)}</dd>
      <dt>Next action</dt><dd>${esc(d.next_action)}</dd>
      ${res.po ? `<dt>Purchase order</dt><dd>${esc(res.po.po_number)} · ${esc(res.po.buyer)}</dd>` : ""}
      <dt>Extracted by</dt><dd>${esc(res.engine)}</dd>
    </dl>
    ${reasons ? `<h2>Why it was stopped</h2><ul class="reasons">${reasons}</ul>` : ""}
    ${warns ? `<h2>Noted, not blocking</h2><ul class="reasons">${warns}</ul>` : ""}
    ${msg ? `<h2>Vendor communication</h2><div class="message"><header><span>To: ${esc(msg.to)}${msg.subject ? `<br>Subject: ${esc(msg.subject)}` : ""}</span>${msg.subject ? `<button class="copy" id="copy-msg">Copy email</button>` : ""}</header><pre>${esc(msg.body)}</pre></div>` : ""}`;
  const cb = $("#copy-msg");
  if (cb) cb.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(`Subject: ${msg.subject}\n\n${msg.body}`); cb.textContent = "Copied"; }
    catch { cb.textContent = "Copy failed - select the text"; }
  });
}

/* ---------------- dashboard ---------------- */
async function loadDashboard() {
  RUNS = await api("/api/runs");
  const done = RUNS.filter((r) => r.status === "done");
  const by = (o) => done.filter((r) => r.decision === o);
  const appr = by("APPROVED"), hold = by("ON HOLD"), rej = by("REJECTED");
  const avg = done.length ? done.reduce((a, r) => a + (r.duration_ms || 0), 0) / done.length / 1000 : 0;
  const k = (v, l, c = "") => `<div class="kpi ${c}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  $("#kpis").innerHTML =
    k(done.length, "Invoices processed") +
    k(appr.length, `Approved · ${inr(appr.reduce((a, r) => a + (r.total || 0), 0))}`, "ok") +
    k(hold.length, `On hold · ${inr(hold.reduce((a, r) => a + (r.total || 0), 0))}`, "hold") +
    k(rej.length, `Rejected · ${inr(rej.reduce((a, r) => a + (r.total || 0), 0))} stopped`, "bad") +
    k(done.length ? Math.round((appr.length / done.length) * 100) + "%" : "-", "Straight-through rate") +
    k(done.length ? avg.toFixed(1) + " s" : "-", "Average time per invoice");
  renderRunsTable();

  const details = await Promise.all([...hold, ...rej].slice(0, 50).map((r) => api(`/api/runs/${r.id}`).catch(() => null)));
  const tally = (f) => details.filter(Boolean).reduce((m, r) => { const key = f(r); if (key) m[key] = (m[key] || 0) + 1; return m; }, {});
  const list = (m) => {
    const entries = Object.entries(m).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...entries.map((e) => e[1]));
    return entries.length ? entries.map(([n, c]) => `<li><div style="flex:1">${esc(n)}<div class="bar" style="width:${(c / max) * 100}%"></div></div><b>${c}</b></li>`).join("")
      : `<li class="muted">No exceptions yet.</li>`;
  };
  $("#queues").innerHTML = list(tally((r) => r.result && r.result.decision && r.result.decision.routing));
  $("#reasons").innerHTML = list(tally((r) => { const c = r.result && r.result.decision && r.result.decision.reasons[0]; return c && REASON_NAME(c); }));
}

function renderRunsTable() {
  const rows = RUNS.filter((r) => filter === "ALL" || r.decision === filter);
  $("#runs").innerHTML = rows.length ? rows.map((r) => `
    <tr data-id="${r.id}" tabindex="0">
      <td>#${r.id}</td><td>${esc(fmtTime(r.created_at))}</td>
      <td>${esc(r.vendor_name || r.filename)}</td><td>${esc(r.invoice_number || "-")}</td>
      <td class="num">${inr(r.total)}</td>
      <td><span class="badge ${cls(r.status === "running" ? null : r.decision)}">${esc(r.status === "running" ? "Running" : r.decision)}</span></td>
      <td class="why">${esc(r.primary_reason || "")}</td>
      <td class="num">${r.duration_ms != null ? (r.duration_ms / 1000).toFixed(1) + " s" : ""}</td>
    </tr>`).join("") : `<tr><td colspan="8" class="muted">No runs yet. Process a test invoice to see it here.</td></tr>`;
  $$("#runs tr[data-id]").forEach((tr) => {
    const open = () => watch(Number(tr.dataset.id), RUNS.find((r) => r.id == tr.dataset.id).filename, null, false);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => e.key === "Enter" && open());
  });
}
$$("#filters button").forEach((b) => b.addEventListener("click", () => {
  filter = b.dataset.f;
  $$("#filters button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  renderRunsTable();
}));

/* ---------------- reference ---------------- */
async function loadReference() {
  const r = await api("/api/reference");
  $("#po-table").innerHTML = `<thead><tr><th>PO</th><th>Vendor</th><th>Items</th><th>Owner</th><th>GST</th><th class="num">Value</th><th class="num">Billed</th><th class="num">Remaining</th></tr></thead><tbody>` +
    r.pos.map((p) => `<tr><td>${esc(p.po_number)}</td><td>${esc(p.vendor_name)}</td>
      <td>${p.lines.map((l) => `${esc(l.description)} · ${l.billed_qty}/${l.quantity} billed @ ${inr(l.unit_price)}`).join("<br>")}</td>
      <td>${esc(p.buyer)}</td><td>${p.gst_rate}%</td><td class="num">${inr(p.value)}</td><td class="num">${inr(p.billed)}</td>
      <td class="num">${inr(p.remaining)}<div class="meter"><i style="width:${Math.min(100, (p.billed / p.value) * 100)}%"></i></div></td></tr>`).join("") + "</tbody>";
  $("#vendor-table").innerHTML = `<thead><tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>Bank on file</th><th>Status</th></tr></thead><tbody>` +
    r.vendors.map((v) => `<tr><td>${esc(v.vendor_id)}</td><td>${esc(v.name)}</td><td>${esc(v.gstin)}</td>
      <td>${esc(v.bank.bank_name)} · ${esc(v.bank.account_number)} · ${esc(v.bank.ifsc)}</td>
      <td class="${v.status === "blocked" ? "status-blocked" : ""}">${esc(v.status)}${v.status_reason ? `<br><span class="muted">${esc(v.status_reason)}</span>` : ""}</td></tr>`).join("") + "</tbody>";
  const R = r.rules;
  const rules = [
    ["Unit price above PO", `up to ${R.price_tolerance_pct}% accepted`],
    ["Amount above PO balance", `up to ${R.amount_tolerance_pct}% or ${inr(R.amount_tolerance_abs)}, whichever is lower`],
    ["Rounding", `${inr(R.rounding_tolerance_abs)}`],
    ["Invoice age", `${R.max_invoice_age_days} days max`],
    ["Same-amount duplicate window", `${R.suspected_duplicate_window_days} days`],
    ["PO inference", `score ≥ ${R.po_inference_min_score} and 0.2 clear of next PO`],
    ["Min. extraction confidence", `${Math.round(R.min_extraction_confidence * 100)}%`],
    ["Our GSTIN", `${r.company.gstin} (${r.company.name})`],
  ];
  $("#rules-table").innerHTML = "<tbody>" + rules.map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join("") + "</tbody>";
  $("#ledger-table").innerHTML = `<thead><tr><th>Ledger</th><th>Vendor</th><th>Invoice</th><th>Date</th><th>PO</th><th class="num">Total</th><th>Status</th><th>Source</th></tr></thead><tbody>` +
    r.ledger.slice().reverse().map((e) => `<tr><td>${esc(e.ledger_id)}</td><td>${esc(r.vendors.find((v) => v.vendor_id === e.vendor_id)?.name)}</td>
      <td>${esc(e.invoice_number)}</td><td>${esc(e.invoice_date)}</td><td>${esc(e.po_number)}</td><td class="num">${inr(e.total)}</td>
      <td>${esc(e.status)}${e.paid_on ? " " + esc(e.paid_on) : ""}</td><td>${esc(e.source === "seed" ? "history" : e.source)}</td></tr>`).join("") + "</tbody>";
}

/* ---------------- reset ---------------- */
$("#reset").addEventListener("click", async () => {
  if (!confirm("Clear all runs and restore the original ledger? Use this before a demo.")) return;
  try {
    await api("/api/reset", { method: "POST" });
    if (source) source.close();
    $("#run-title").textContent = "Demo data reset";
    $("#run-sub").textContent = "Run history cleared and the ledger restored to its starting state.";
    $("#run-timer").textContent = "";
    renderStages();
    ["decision", "fields", "doc"].forEach((p) => ($("#v-" + p).innerHTML = `<div class="empty">Nothing here yet.</div>`));
    $$("#samples button").forEach((b) => b.removeAttribute("aria-current"));
    const active = $(".tab.active").id.replace("tab-", "");
    if (active !== "process") showTab(active);
  } catch (e) { alert("Reset failed: " + e.message); }
});

init();
