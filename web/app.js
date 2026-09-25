/* AWS Cost & Ops Insights - chat frontend
 *
 * Streams SSE events from POST /api/run and animates each pipeline step with
 * a rotating playful verb until the server emits "step_complete". Charts and
 * formatting come from the shared toolkit in shared.js (no Chart.js/CDN).
 */
"use strict";
const {esc: escapeHtml, formatNumber, formatKpiValue, ICON, renderChartSpec, renderTable, renderLegend, fmtStepElapsed} = Shared;

const STEP_DEFS = [
  { key: "inputs",         name: "Loading input sources" },
  { key: "step1",          name: "Simplify & Normalize (LLM)" },
  { key: "step2",          name: "Context Load" },
  { key: "forecast",       name: "Cost Forecast" },
  { key: "tag_governance", name: "Tag Governance" },
  { key: "root_cause",     name: "Anomaly Root Cause" },
  { key: "step3_1",        name: "Generate Charts (LLM)" },
  { key: "step3_2",        name: "Analysis & Metrics (LLM)" },
  { key: "step3_3",        name: "Executive Summary (LLM)" },
  { key: "step4",          name: "Combine All Responses" },
  { key: "step5",          name: "Final Response" },
];

const VERBS = [
  "manifesting", "loading", "wobbling", "percolating", "brewing", "conjuring",
  "untangling", "marinating", "shimmering", "synthesizing", "polishing",
  "noodling", "vibing", "computing", "humming", "thinking", "crunching",
  "stitching", "harmonizing", "unfurling",
];

const $ = (sel, root = document) => root.querySelector(sel);

const chatEl = $("#chat");
const formEl = $("#composer");
const queryEl = $("#query");
const sendEl = $("#send");

let running = false;

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  if (running) return;
  const q = queryEl.value.trim();
  if (!q) return;
  startRun(q);
});

queryEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

function startRun(query) {
  running = true;
  sendEl.disabled = true;
  sendEl.querySelector(".send-label").textContent = "Running…";

  appendUserBubble(query);
  const { steps, refreshEdges, finalSlot } = appendAssistantBubble();

  const sourceSel = $("#source");
  const pick = sourceSel ? sourceSel.value : "local_csv";
  const useRealAws = pick === "real_aws";
  const sources = useRealAws ? ["cost_explorer", "cloudwatch"] : ["local_csv"];
  const backend = useRealAws ? "aws" : "csv";

  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, sources, backend }),
  })
    .then((res) => {
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      return streamSSE(res.body, (event, data) => {
        handleEvent(event, data, steps, refreshEdges, finalSlot);
      });
    })
    .catch((err) => {
      console.error(err);
      renderError(finalSlot, err.message || String(err));
    })
    .finally(() => {
      running = false;
      sendEl.disabled = false;
      sendEl.querySelector(".send-label").textContent = "Run pipeline";
    });
}

/* ---------- DOM builders ---------- */

function appendUserBubble(text) {
  const wrap = document.createElement("div");
  wrap.className = "bubble user";
  wrap.innerHTML = `
    <div class="bubble-label">You</div>
    <div class="content"></div>
  `;
  wrap.querySelector(".content").textContent = text;
  chatEl.appendChild(wrap);
  scrollChat();
}

function appendAssistantBubble() {
  const wrap = document.createElement("div");
  wrap.className = "bubble assistant";
  wrap.innerHTML = `
    <div class="bubble-label">Pipeline</div>
    <div class="steps"></div>
    <div class="final" hidden></div>
  `;
  chatEl.appendChild(wrap);
  const stepsEl = wrap.querySelector(".steps");
  const finalSlot = wrap.querySelector(".final");

  const { steps: rawSteps, refreshEdges } = Shared.buildFlowchart(stepsEl, STEP_DEFS);
  const steps = {};
  for (const key in rawSteps) steps[key] = { el: rawSteps[key].el, verbTimer: null };

  scrollChat();
  return { bubble: wrap, steps, refreshEdges, finalSlot };
}

/* ---------- Event handling ---------- */

function handleEvent(event, data, steps, refreshEdges, finalSlot) {
  if (event === "run_start") return;
  if (event === "step_start") { setRunning(steps[data.step], data.label); return; }
  if (event === "step_complete") { setDone(steps[data.step], data.label, data.elapsed_s, refreshEdges); return; }
  if (event === "final") { renderFinal(finalSlot, data.result); return; }
  if (event === "error") { renderError(finalSlot, data.message || "Pipeline error"); return; }
  if (event === "done") {
    for (const k of Object.keys(steps)) {
      const s = steps[k];
      if (s.el.dataset.state === "running") {
        setDone(s, s.el.querySelector(".rs-name").textContent || "Done", null, refreshEdges);
      }
    }
    running = false;
    sendEl.disabled = false;
    sendEl.querySelector(".send-label").textContent = "Run pipeline";
    return;
  }
}

function setRunning(state, label) {
  if (!state) return;
  state.el.dataset.state = "running";
  if (label) state.el.querySelector(".rs-name").textContent = label;
  const verbEl = state.el.querySelector(".rs-verb");
  const cycle = () => { verbEl.textContent = VERBS[Math.floor(Math.random() * VERBS.length)] + "…"; };
  cycle();
  state.verbTimer = window.setInterval(cycle, 1100);
  // The flowchart scrolls horizontally past its min-width on narrow viewports
  // (see .flow-scroll in shared.css) — without this, the step the pipeline is
  // actually on can sit off-screen with no indication it's still running.
  state.el.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
}

function setDone(state, label, elapsed, refreshEdges) {
  if (!state) return;
  if (state.verbTimer) { window.clearInterval(state.verbTimer); state.verbTimer = null; }
  state.el.dataset.state = "done";
  if (label) state.el.querySelector(".rs-name").textContent = label;
  state.el.querySelector(".rs-verb").textContent = "";
  if (typeof elapsed === "number") {
    state.el.querySelector(".rs-time").textContent = fmtStepElapsed(elapsed);
  }
  if (refreshEdges) refreshEdges();
}

/* ---------- Final render ---------- */

function renderFinal(slot, result) {
  const payload = result?.payload || {};
  const analysis = payload.analysis || {};
  const summary = payload.summary || {};
  const charts = payload.charts || [];
  const bm = payload.business_metadata || {};

  slot.hidden = false;

  const kpis = analysis.kpis || {};
  const kpiCards = Object.entries(kpis)
    .map(([k, v]) => `
      <div class="rr-kpi">
        <div class="rk-name">${escapeHtml(k.replaceAll("_", " "))}</div>
        <div class="rk-val">${escapeHtml(formatKpiValue(v))}</div>
      </div>
    `).join("");

  const totalCost = bm.total_cost_observed;
  const totalCard = totalCost != null
    ? `<div class="rr-kpi"><div class="rk-name">Total observed cost</div><div class="rk-val">$${Number(totalCost).toFixed(6)}</div></div>`
    : "";

  const anomaliesHtml = renderList("Anomalies", analysis.anomalies || [],
    (a) => `${severityBadge(a.severity)}${a.source === "detector" ? '<span class="chip" title="Found by the statistical anomaly detector, not the LLM">detected</span> ' : ""}${escapeHtml(a.finding || "")} <span style="color:var(--text-muted)">— ${escapeHtml(a.evidence || "")}</span>`);
  const trendsHtml = renderList("Trends", analysis.trends || [],
    (t) => `${escapeHtml(t.observation || "")} <span style="color:var(--text-muted)">(${escapeHtml(t.metric || "")})</span>`);
  const findingsHtml = renderList("Key findings", summary.key_findings || [], (s) => escapeHtml(s));
  const recsHtml = renderList("Recommendations", summary.recommendations || [],
    (r) => `${severityBadge(r.impact)}${escapeHtml(r.action || "")}`);
  const nextStepsHtml = renderList("Next steps", summary.next_steps || [], (s) => escapeHtml(s));

  const actionsHtml = renderActions(result?.actions || [], result?.action_backend || "csv");

  const isAws = (result?.action_backend || "") === "aws";
  const fleetHtml = isAws
    ? `<div class="rr-section" id="fleetSection">
         <h4>Live AWS fleet — updates as you apply</h4>
         <div class="fleet-wrap">Loading…</div>
       </div>`
    : "";

  const chartsHtml = charts.length
    ? `<div class="rr-section"><h4>Suggested charts</h4>
        <div class="chart-cards">
          ${charts.map((c, i) => `
            <div class="chart-card" data-chart-idx="${i}">
              <div class="chart-head"><div>
                <h3>${escapeHtml(c.title || c.id || "Chart")}</h3>
                <p class="csub">${escapeHtml(c.type || "?")} · ${escapeHtml(c.x || c.x_field || "")} → ${escapeHtml(c.y || c.y_field || "")} · <span class="mono">${escapeHtml(c.data_table || "")}</span></p>
              </div></div>
              <div class="plot"></div>
              <div class="legend"></div>
            </div>
          `).join("")}
        </div>
      </div>`
    : "";

  const files = result?.json_path || result?.markdown_path
    ? `<p class="chart-foot">Saved → <span class="mono">${escapeHtml(result.markdown_path || "")}</span> &nbsp;·&nbsp; <span class="mono">${escapeHtml(result.json_path || "")}</span></p>`
    : "";

  const followUpHtml = `
    <div class="rr-section">
      <h4>Ask a follow-up</h4>
      <form class="followup-form">
        <textarea class="followup-input" rows="2" placeholder="e.g. which region cost the most, and why?"></textarea>
        <button class="btn btn-quiet" type="submit">Ask</button>
      </form>
      <div class="followup-answer" hidden></div>
    </div>`;

  slot.innerHTML = `
    <div class="rr-kpi-grid">${totalCard}${kpiCards}</div>
    ${findingsHtml}${anomaliesHtml}${trendsHtml}${recsHtml}${nextStepsHtml}
    ${fleetHtml}${actionsHtml}${chartsHtml}${files}${followUpHtml}
  `;

  requestAnimationFrame(() => {
    slot.querySelectorAll(".chart-card[data-chart-idx]").forEach((card) => {
      const idx = Number(card.dataset.chartIdx);
      const spec = charts[idx];
      const plot = card.querySelector(".plot");
      const legend = card.querySelector(".legend");
      if (!spec || !renderChartSpec(plot, legend, null, spec)) {
        plot.innerHTML = `<div class="empty">No data resolved for this spec.</div>`;
      }
    });
    wireActions(slot);
    wireFollowUp(slot);
    if (isAws) refreshFleet(slot);
  });

  scrollChat();
}

/* ---------- Follow-up Q&A over the run's results (no pipeline re-run) ---------- */

function wireFollowUp(slot) {
  const form = slot.querySelector(".followup-form");
  const input = slot.querySelector(".followup-input");
  const answerEl = slot.querySelector(".followup-answer");
  if (!form || !input || !answerEl) return;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    const btn = form.querySelector("button");
    btn.disabled = true;
    answerEl.hidden = false;
    answerEl.className = "followup-answer outcome wait";
    answerEl.innerHTML = `${ICON.clock}<div><span class="oh">Thinking…</span></div>`;
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      const data = await res.json();
      answerEl.className = "followup-answer outcome ok";
      const citations = (data.citations || []).length
        ? `<p class="ob">Based on: ${escapeHtml(data.citations.join(", "))}</p>` : "";
      answerEl.innerHTML = `${ICON.check}<div><div class="oh md">${Shared.renderMarkdown(data.answer || "No answer.")}</div>${citations}</div>`;
    } catch (err) {
      answerEl.className = "followup-answer outcome no";
      answerEl.innerHTML = `${ICON.x}<div><span class="oh">Request failed: ${escapeHtml(err.message || String(err))}</span></div>`;
    } finally {
      btn.disabled = false;
      input.value = "";
    }
  });
}

/* ---------- Live AWS fleet view ---------- */

async function refreshFleet(slot) {
  const wrap = slot.querySelector("#fleetSection .fleet-wrap");
  if (!wrap) return;
  try {
    const d = await (await fetch("/api/aws/fleet")).json();
    if (!d.available) {
      wrap.innerHTML = `<div class="empty">AWS not reachable.</div>`;
      return;
    }
    const rows = (d.instances || []).map((i) => {
      const proj = (i.Tags.find((t) => t.Key === "Project") || {}).Value || "";
      const isRunning = i.State.Name === "running";
      return `<tr class="${isRunning ? "" : "row-stopped"}">
        <td>${escapeHtml(i.InstanceId)}</td>
        <td>${escapeHtml(i.InstanceType)}</td>
        <td>${escapeHtml(i.Region)}</td>
        <td>${escapeHtml(proj)}</td>
        <td class="num">${i.CpuUtilization ?? ""}</td>
        <td class="${isRunning ? "st-run" : "st-stop"}">${escapeHtml(i.State.Name)}</td>
        <td class="num">$${Number(i.MonthlyCost || 0).toFixed(2)}</td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `
      <div class="fleet-summary">Running: <b>${d.running_count}</b>
        &nbsp;·&nbsp; EC2 cost / mo: <b>$${Number(d.monthly_cost).toFixed(2)}</b></div>
      <table class="fleet-table">
        <thead><tr><th>Instance</th><th>Type</th><th>Region</th><th>Project</th><th class="num">CPU%</th><th>State</th><th class="num">$/mo</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty">Could not load fleet: ${escapeHtml(e.message || String(e))}</div>`;
  }
}

/* ---------- Human-in-the-loop: review & apply recommended changes ---------- */

function renderActions(actions, backend) {
  if (!actions || !actions.length) return "";
  const backendLabel = String(backend).toUpperCase();

  const cards = actions.map((a) => {
    const resolved = ["applied", "dismissed", "acknowledged"].includes(a.status);
    const target = a.executable
      ? `Target → <span class="mono">${escapeHtml(a.table)}.csv</span> where <span class="mono">${escapeHtml(a.match_column)} = ${escapeHtml(a.match_value)}</span>`
      : `No matching data row — <b>manual</b> recommendation (acknowledge only).`;
    const applyLabel = a.executable ? "Send to RE team" : "Acknowledge";
    const confirmText = a.executable
      ? `Sends this to the RE team's approval queue. Nothing changes on the <b>${escapeHtml(backendLabel)}</b> backend until they stage and commit it.`
      : `Mark this recommendation acknowledged. Nothing in the data changes.`;

    const controls = resolved
      ? `<div class="outcome ${a.status === "dismissed" ? "no" : "ok"}">${a.status === "dismissed" ? ICON.x : ICON.check}
           <div><span class="oh">Already ${escapeHtml(a.status)}.</span>${a.result_note ? ` <span class="ob">${escapeHtml(a.result_note)}</span>` : ""}</div></div>`
      : `
        <div class="actions action-controls">
          <button class="btn btn-primary btn-apply" type="button">${applyLabel}</button>
          <button class="btn btn-danger btn-dismiss" type="button">Dismiss</button>
        </div>
        <div class="plan confirm-bar" hidden>
          <p class="rationale" style="margin:0 0 9px">${confirmText}</p>
          <div class="actions">
            <button class="btn btn-approve btn-confirm" type="button">Confirm</button>
            <button class="btn btn-quiet btn-cancel" type="button">Cancel</button>
          </div>
        </div>
        <div class="action-result" hidden></div>`;

    return `
      <article class="rec" data-id="${escapeHtml(a.id)}">
        <div class="rec-top"><div style="min-width:0;flex:1">
          <div class="chips">${severityBadge(a.impact)}</div>
          <h4>${escapeHtml(a.title)}</h4>
          <p class="rationale">${target}</p>
        </div></div>
        ${controls}
      </article>`;
  }).join("");

  return `
    <div class="section-head" style="margin-top:20px"><div>
      <h2>Recommended changes — review &amp; send</h2>
      <p>Confirming sends the change to the RE team's approval queue — it only reaches the <span class="mono">${escapeHtml(backendLabel)}</span> backend once they stage and commit it.</p>
    </div></div>
    <div class="rec-list">${cards}</div>`;
}

function wireActions(root) {
  root.querySelectorAll(".rec[data-id]").forEach((card) => {
    const applyBtn = card.querySelector(".btn-apply");
    const dismissBtn = card.querySelector(".btn-dismiss");
    const confirmBar = card.querySelector(".confirm-bar");
    const controls = card.querySelector(".action-controls");
    const confirmBtn = card.querySelector(".btn-confirm");
    const cancelBtn = card.querySelector(".btn-cancel");
    // Both executable applies and manual acknowledgements POST decision=apply;
    // the server's executor turns a non-executable action into "acknowledged".
    const decision = "apply";

    if (applyBtn && confirmBar && controls) {
      applyBtn.addEventListener("click", () => { controls.hidden = true; confirmBar.hidden = false; });
    }
    if (cancelBtn && confirmBar && controls) {
      cancelBtn.addEventListener("click", () => { confirmBar.hidden = true; controls.hidden = false; });
    }
    if (confirmBtn) confirmBtn.addEventListener("click", () => submitAction(card, decision));
    if (dismissBtn) dismissBtn.addEventListener("click", () => submitAction(card, "dismiss"));
  });
}

async function submitAction(card, decision) {
  const id = card.dataset.id;
  const controls = card.querySelector(".action-controls");
  const confirmBar = card.querySelector(".confirm-bar");
  const resultEl = card.querySelector(".action-result");

  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  resultEl.hidden = false;
  resultEl.className = "outcome wait";
  resultEl.innerHTML = `${ICON.clock}<div><span class="oh">${decision === "dismiss" ? "Dismissing…" : "Applying…"}</span></div>`;

  try {
    const res = await fetch("/api/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, decision }),
    });
    const data = await res.json();
    resultEl.className = "outcome " + (data.ok ? "ok" : "no");
    resultEl.innerHTML = `${data.ok ? ICON.check : ICON.x}<div><span class="oh">${escapeHtml(data.message || (data.ok ? "Done." : "Failed."))}</span></div>`;
    if (data.ok || data.already) {
      if (controls) controls.hidden = true;
      if (confirmBar) confirmBar.hidden = true;
      const slot = card.closest(".final");
      if (slot) refreshFleet(slot);
    } else {
      card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      if (confirmBar) confirmBar.hidden = true;
      if (controls) controls.hidden = false;
    }
  } catch (e) {
    resultEl.className = "outcome no";
    resultEl.innerHTML = `${ICON.x}<div><span class="oh">Request failed: ${escapeHtml(e.message || String(e))}</span></div>`;
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    if (confirmBar) confirmBar.hidden = true;
    if (controls) controls.hidden = false;
  }
}

function renderError(slot, message) {
  slot.hidden = false;
  slot.innerHTML = `<div class="outcome no">${ICON.x}<div><span class="oh">Error</span> <span class="ob">${escapeHtml(message)}</span></div></div>`;
  scrollChat();
}

function renderList(title, items, render) {
  if (!items || !items.length) return "";
  return `<div class="rr-section"><h4>${escapeHtml(title)}</h4><ul class="rr-list">${items
    .map((it) => `<li>${render(it)}</li>`).join("")}</ul></div>`;
}

function severityBadge(level) {
  const cls = ["high", "medium", "low"].includes(level) ? level : "low";
  return level ? `<span class="chip impact-${cls}">${escapeHtml(level)} impact</span>` : "";
}

function scrollChat() {
  requestAnimationFrame(() => {
    chatEl.scrollTop = chatEl.scrollHeight;
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  });
}

/* ---------- SSE parser (fetch-based, works in all browsers; EventSource
   can't send a POST body, which /api/run requires) ---------- */

async function streamSSE(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let finished = false;

  while (!finished) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);

      let event = "message";
      let dataLines = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      const raw = dataLines.join("\n");
      let data = {};
      try { data = raw ? JSON.parse(raw) : {}; } catch { data = { raw }; }
      onEvent(event, data);

      if (event === "done") {
        finished = true;
        try { await reader.cancel(); } catch {}
        break;
      }
    }
  }
}
