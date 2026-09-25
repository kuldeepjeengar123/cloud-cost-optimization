/* =============================================================================
   Shared runtime — AWS Cost & Ops Insights
   Formatters, toast/tooltip system, and a plain-SVG chart toolkit (no chart
   library, no build step). Loaded by every page via a plain <script> tag;
   everything is attached to the global `Shared` object so page scripts stay
   free of module wiring.
   ========================================================================= */
(function(){
"use strict";

/* =========================================================================
   Formatting
   ========================================================================= */
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* =========================================================================
   Markdown rendering — small self-contained subset (headers, bold/italic,
   inline code, fenced code blocks, links, ordered/unordered lists,
   blockquotes, hr, GFM pipe tables). Used to render LLM chat answers, which
   are untrusted text, so everything is run through esc() before any markup
   is added — the source text is escaped first and markdown syntax is layered
   on top of the escaped string, so no HTML from the model (or from a page a
   citation points at) can reach the DOM as live tags/attributes.
   ========================================================================= */
function isTableSeparatorRow(line){
  const stripped = line.replace(/[\s|:-]/g, "");
  return stripped === "" && line.includes("-") && line.includes("|");
}
function splitTableRow(line){
  let s = line.trim();
  if(s.startsWith("|")) s = s.slice(1);
  if(s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map(c => c.trim());
}
function mdInline(s){
  return s
    .replace(/`([^`]+?)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+?)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+?)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
}
function renderMarkdown(raw){
  if(!raw) return "";
  const codeBlocks = [];
  let text = String(raw).replace(/```[^\S\n]*\w*\n([\s\S]*?)```/g, (_, code) => {
    codeBlocks.push(code.replace(/\n$/, ""));
    return ` CODEBLOCK${codeBlocks.length - 1} `;
  });
  text = esc(text);

  const lines = text.split("\n");
  const out = [];
  let para = [];
  const flush = () => { if(para.length){ out.push(`<p>${mdInline(para.join(" "))}</p>`); para = []; } };

  let i = 0;
  while(i < lines.length){
    const line = lines[i];

    const cb = line.match(/^\s*CODEBLOCK(\d+)\s*$/);
    if(cb){ flush(); out.push(`<pre><code>${esc(codeBlocks[Number(cb[1])])}</code></pre>`); i++; continue; }

    if(!line.trim()){ flush(); i++; continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flush(); const lvl = h[1].length; out.push(`<h${lvl}>${mdInline(h[2].trim())}</h${lvl}>`); i++; continue; }

    if(line.includes("|") && lines[i+1] !== undefined && isTableSeparatorRow(lines[i+1])){
      flush();
      const head = splitTableRow(line);
      i += 2;
      const rows = [];
      while(i < lines.length && lines[i].trim() && lines[i].includes("|")){ rows.push(splitTableRow(lines[i])); i++; }
      out.push("<table><thead><tr>" + head.map(c => `<th>${mdInline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        rows.map(r => "<tr>" + r.map(c => `<td>${mdInline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }

    if(/^\s*[-*+]\s+/.test(line)){
      flush();
      const items = [];
      while(i < lines.length && /^\s*[-*+]\s+/.test(lines[i])){ items.push(lines[i].replace(/^\s*[-*+]\s+/, "")); i++; }
      out.push(`<ul>${items.map(it => `<li>${mdInline(it)}</li>`).join("")}</ul>`);
      continue;
    }

    if(/^\s*\d+[.)]\s+/.test(line)){
      flush();
      const items = [];
      while(i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])){ items.push(lines[i].replace(/^\s*\d+[.)]\s+/, "")); i++; }
      out.push(`<ol>${items.map(it => `<li>${mdInline(it)}</li>`).join("")}</ol>`);
      continue;
    }

    if(/^\s*>\s?/.test(line)){
      flush();
      const items = [];
      while(i < lines.length && /^\s*>\s?/.test(lines[i])){ items.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      out.push(`<blockquote>${mdInline(items.join(" "))}</blockquote>`);
      continue;
    }

    if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)){ flush(); out.push("<hr>"); i++; continue; }

    para.push(line.trim());
    i++;
  }
  flush();
  return out.join("\n");
}

const money = (n, dp) => {
  const d = dp === undefined ? (Math.abs(n) >= 100 ? 0 : 2) : dp;
  return "$" + n.toLocaleString("en-US",{minimumFractionDigits:d, maximumFractionDigits:d});
};
const money2 = n => "$" + n.toLocaleString("en-US",{minimumFractionDigits:2, maximumFractionDigits:2});
function compactMoney(n){
  if(n >= 1000000) return "$" + (n/1000000).toFixed(1).replace(/\.0$/,"") + "M";
  if(n >= 10000)   return "$" + (n/1000).toFixed(1).replace(/\.0$/,"") + "K";
  return money(n,0);
}
function formatNumber(n){
  const v = Number(n) || 0;
  if(Math.abs(v) >= 1) return v.toFixed(2);
  if(Math.abs(v) >= 0.0001) return v.toFixed(4);
  return v === 0 ? "0.00" : v.toExponential(2);
}
function formatKpiValue(v){
  if(v == null) return "—";
  if(typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(6);
  if(typeof v === "object") return Object.entries(v)
    .map(([k,val]) => `${k}: ${typeof val === "number" ? Number(val).toFixed(4) : val}`).join(", ");
  return String(v);
}
function fmtTime(iso){
  if(!iso) return null;
  return String(iso).replace("T"," ").slice(0,16) + " UTC";
}
// A deterministic (no-LLM) pipeline step can genuinely finish in a few
// milliseconds — e.g. the forecast/tag-governance/root-cause agents on a
// normal-sized dataset. "0.0s" reads like the step never ran; "<0.1s" reads
// as what it is: real, just fast.
function fmtStepElapsed(seconds){
  if(typeof seconds !== "number") return "";
  return seconds < 0.1 ? "<0.1s" : `${seconds.toFixed(1)}s`;
}

/* =========================================================================
   Toasts
   ========================================================================= */
function ensureToastHost(){
  let host = document.getElementById("toasts");
  if(!host){
    host = document.createElement("div");
    host.id = "toasts";
    host.className = "toasts";
    host.setAttribute("aria-live","polite");
    document.body.appendChild(host);
  }
  return host;
}
function toast(kind, title, body){
  const host = ensureToastHost();
  const d = document.createElement("div");
  d.className = "toast " + kind;
  d.innerHTML = `<b>${esc(title)}</b>${esc(body || "")}`;
  host.appendChild(d);
  setTimeout(() => { d.style.opacity = "0"; d.style.transform = "translateX(12px)";
                     setTimeout(() => d.remove(), 240); }, 4600);
}

/* =========================================================================
   Tooltip (shared crosshair-style hover tooltip for every chart)
   ========================================================================= */
function ensureTipEl(){
  let tip = document.getElementById("tip");
  if(!tip){
    tip = document.createElement("div");
    tip.id = "tip";
    tip.className = "tip";
    tip.setAttribute("role","status");
    tip.setAttribute("aria-live","polite");
    document.body.appendChild(tip);
  }
  return tip;
}
const tipEl = ensureTipEl();
function showTip(evt, title, rows){
  tipEl.innerHTML = `<div class="tt">${esc(title)}</div>` +
    rows.map(r => `<div class="tr"><span>${esc(r[0])}</span><b>${esc(r[1])}</b></div>`).join("");
  tipEl.style.opacity = "1";
  moveTip(evt);
}
function moveTip(evt){
  const pad = 14, r = tipEl.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if(x + r.width  > innerWidth  - 8) x = evt.clientX - r.width  - pad;
  if(y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
  tipEl.style.left = x + "px"; tipEl.style.top = Math.max(8, y) + "px";
}
function hideTip(){ tipEl.style.opacity = "0"; }
function hoverable(node, title, rows){
  node.style.cursor = "default";
  node.addEventListener("mouseenter", e => showTip(e, title, rows));
  node.addEventListener("mousemove", moveTip);
  node.addEventListener("mouseleave", hideTip);
  node.setAttribute("tabindex","0");
  node.addEventListener("focus", e => {
    const b = node.getBoundingClientRect();
    showTip({clientX:b.left + b.width/2, clientY:b.top}, title, rows);
  });
  node.addEventListener("blur", hideTip);
}

/* =========================================================================
   Icons
   ========================================================================= */
const ICON = {
  check:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  x:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
  clock:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  alert:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3L2 20h20L12 3z"/><path d="M12 9v5M12 17.5v.01"/></svg>',
  info:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/></svg>',
  shield:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v6c0 4.5-3 7.7-7 9-4-1.3-7-4.5-7-9V6z"/></svg>',
  caret:'<svg class="caret" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
  dot:'<svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="currentColor"/></svg>'
};
const IMPACT_ICON = {high:ICON.alert, medium:ICON.info, low:ICON.dot};

/* =========================================================================
   Chart toolkit — plain SVG. Bars capped at 24px with a 4px rounded data-end
   square at the baseline, hairline grid one step off the surface, 8px+
   markers with a 2px surface ring, selective direct labels, and a table-view
   twin for every chart (see renderTable).
   ========================================================================= */
const NS = "http://www.w3.org/2000/svg";
function cssVar(name, fallback){
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
const GRID = "var(--border)", AXIS = "var(--axis)";
const INK_MUTED = "var(--text-muted)", INK_2 = "var(--text-secondary)", INK_1 = "var(--text-primary)";
const CHART_SURFACE = cssVar("--surface", "#131a2c");
const SERIES = ["var(--series-1)","var(--series-2)","var(--series-3)","var(--series-4)",
                 "var(--series-5)","var(--series-6)","var(--series-7)","var(--series-8)"];
const BAR_MAX = 22;

function el(tag, attrs, parent){
  const n = document.createElementNS(NS, tag);
  for(const k in attrs) n.setAttribute(k, attrs[k]);
  if(parent) parent.appendChild(n);
  return n;
}
function svgRoot(host, w, h){
  host.innerHTML = "";
  const s = el("svg", {viewBox:`0 0 ${w} ${h}`, width:w, height:h,
                       preserveAspectRatio:"xMidYMid meet", role:"img"}, host);
  s.style.width = "100%"; s.style.height = "auto";
  return s;
}
function txt(p, x, y, s, o){
  o = o || {};
  const t = el("text", {x, y,
    "font-size": o.size || 11,
    "font-family":"var(--font)",
    fill: o.fill || INK_MUTED,
    "text-anchor": o.anchor || "start",
    "dominant-baseline": o.baseline || "middle"}, p);
  if(o.weight) t.setAttribute("font-weight", o.weight);
  if(o.tabular) t.setAttribute("style","font-variant-numeric:tabular-nums");
  t.textContent = s;
  return t;
}
function barRight(p, x, y, w, h, fill){
  const r = Math.min(4, w);
  const d = w <= 0.6
    ? `M${x} ${y}h${Math.max(w,0.6)}v${h}h${-Math.max(w,0.6)}z`
    : `M${x} ${y}h${w-r}a${r} ${r} 0 0 1 ${r} ${r}v${h-2*r}a${r} ${r} 0 0 1 ${-r} ${r}h${-(w-r)}z`;
  return el("path", {d, fill}, p);
}
function barUp(p, x, yTop, w, h, fill){
  const r = Math.min(4, h, w/2);
  const d = h <= 0.6
    ? `M${x} ${yTop}h${w}v${Math.max(h,0.6)}h${-w}z`
    : `M${x} ${yTop+r}a${r} ${r} 0 0 1 ${r} ${-r}h${w-2*r}a${r} ${r} 0 0 1 ${r} ${r}v${h-r}h${-w}z`;
  return el("path", {d, fill}, p);
}
function niceTicks(max, count){
  if(max <= 0) return [0];
  const raw = max / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const out = []; for(let v = 0; v <= max + step*0.001; v += step) out.push(v);
  return out;
}

/* ---- table-view twin ---- */
function renderTable(host, cols, rows){
  host.innerHTML =
    "<table><thead><tr>" +
      cols.map(c => `<th${c.num?' class="num"':""}>${esc(c.label)}</th>`).join("") +
    "</tr></thead><tbody>" +
      rows.map(r => "<tr>" + r.map((v,i) =>
        `<td${cols[i].num?' class="num"':""}>${esc(v)}</td>`).join("") + "</tr>").join("") +
    "</tbody></table>";
}
function renderLegend(host, items){
  // items: [{color, label, shape}] — shape: circle|square|triangle|ring|dash
  const mark = (color, shape) => {
    if(shape === "triangle") return `<svg width="13" height="13" viewBox="0 0 13 13"><path d="M6.5 1L12 11.5H1Z" fill="${color}"/></svg>`;
    if(shape === "square") return `<svg width="12" height="12" viewBox="0 0 12 12"><rect x="0.5" y="0.5" width="11" height="11" rx="2" fill="${color}"/></svg>`;
    if(shape === "ring") return `<svg width="13" height="13" viewBox="0 0 13 13"><circle cx="6.5" cy="6.5" r="5" fill="none" stroke="${color}" stroke-width="2"/></svg>`;
    if(shape === "dash") return `<svg width="15" height="13" viewBox="0 0 15 13"><circle cx="7.5" cy="6.5" r="5.5" fill="none" stroke="${color}" stroke-width="1.5" stroke-dasharray="3 3"/></svg>`;
    return `<svg width="12" height="12" viewBox="0 0 12 12"><circle cx="6" cy="6" r="5.5" fill="${color}"/></svg>`;
  };
  host.innerHTML = items.map(it => `<span class="li">${mark(it.color, it.shape)}${esc(it.label)}</span>`).join("");
}

/* ---------------- Chart 1: horizontal bars (single series) ---------------- */
function hbarChart(host, data, opts){
  opts = opts || {};
  const rows = data.length;
  const labelW = opts.labelW || 172, valueW = 78;
  const rowH = opts.rowH || 26, padT = 6, padB = 24;
  const w = Math.max(host.clientWidth || 520, 340);
  const h = padT + rows*rowH + padB;
  const plotX = labelW, plotW = Math.max(60, w - labelW - valueW);
  const s = svgRoot(host, w, h);
  s.setAttribute("aria-label", opts.aria || "bar chart");

  const max = Math.max(...data.map(d => d[1]), 1);
  const ticks = niceTicks(max, 4);
  const X = v => plotX + (v/max) * plotW;

  ticks.forEach(t => {
    el("line", {x1:X(t), y1:padT, x2:X(t), y2:padT + rows*rowH,
                stroke: t===0 ? AXIS : GRID, "stroke-width":1}, s);
    txt(s, X(t), padT + rows*rowH + 12, opts.tickFmt ? opts.tickFmt(t) : compactMoney(t),
        {anchor: t===0 ? "start" : "middle", size:10, fill:INK_MUTED, tabular:true});
  });

  data.forEach((d,i) => {
    const [name, val] = d;
    const bh = Math.min(BAR_MAX, rowH - 8);
    const y = padT + i*rowH + (rowH - bh)/2;
    const bw = Math.max(1, (val/max) * plotW);

    txt(s, plotX - 10, y + bh/2,
        name.length > 26 ? name.slice(0,25) + "…" : name,
        {anchor:"end", size:11, fill:INK_2});

    barRight(s, plotX, y, bw, bh, opts.color || SERIES[0]);
    const hit = el("rect", {x:plotX, y:padT + i*rowH - 1, width:plotW, height:rowH,
                            fill:"transparent"}, s);
    hoverable(hit, name, [[opts.costLabel || "Cost", money2(val)],
                          ["Share of total", ((val/opts.total)*100).toFixed(2) + "%"]]);

    txt(s, plotX + bw + 8, y + bh/2, val >= 0.01 ? money2(val) : "$0.00",
        {size:10.5, fill:INK_2, tabular:true});
  });
  return s;
}

/* ---------------- Chart 2: columns (single series, time axis) ---------------- */
function columnChart(host, data, opts){
  opts = opts || {};
  const w = Math.max(host.clientWidth || 520, 340);
  const padL = 46, padR = 14, padT = 16, padB = 40;
  const h = 250;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const s = svgRoot(host, w, h);
  s.setAttribute("aria-label", opts.aria || "column chart");

  const max = Math.max(...data.map(d => d[1]), 1);
  const ticks = niceTicks(max, 4);
  const Y = v => padT + plotH - (v/max)*plotH;
  const band = plotW / data.length;
  const bw = Math.min(BAR_MAX, band - 3);

  ticks.forEach(t => {
    el("line", {x1:padL, y1:Y(t), x2:padL + plotW, y2:Y(t),
                stroke: t===0 ? AXIS : GRID, "stroke-width":1}, s);
    txt(s, padL - 9, Y(t), compactMoney(t), {anchor:"end", size:10, fill:INK_MUTED, tabular:true});
  });

  data.forEach((d,i) => {
    const [date, val] = d;
    const x = padL + i*band + (band - bw)/2;
    const bh = Math.max(0.8, (val/max)*plotH);
    barUp(s, x, Y(val), bw, bh, opts.color || SERIES[0]);
    const hit = el("rect", {x:padL + i*band, y:padT, width:band, height:plotH, fill:"transparent"}, s);
    hoverable(hit, date, [["Spend", money2(val)]]);
  });

  const stepLabels = Math.max(1, Math.round(data.length / 7));
  data.forEach((d,i) => {
    if(i % stepLabels !== 0 && i !== data.length-1) return;
    const x = padL + i*band + band/2;
    txt(s, x, padT + plotH + 14, String(d[0]).slice(5), {anchor:"middle", size:10, fill:INK_MUTED});
  });
  return s;
}

/* ---------------- Chart 3: pie / donut (categorical, ≥2 series) ---------------- */
function pieChart(host, data, opts){
  // data: [[label, value], ...]. opts.donut: boolean.
  opts = opts || {};
  const w = Math.max(host.clientWidth || 420, 300);
  const h = Math.max(240, Math.min(320, w * 0.62));
  const s = svgRoot(host, w, h);
  s.setAttribute("aria-label", opts.aria || "pie chart");

  const cx = h/2 + 10, cy = h/2, r = Math.min(cx, h/2) - 12;
  const inner = opts.donut ? r * 0.58 : 0;
  const total = data.reduce((sum,d) => sum + Math.max(0,d[1]), 0) || 1;

  let angle = -Math.PI/2;
  const legendItems = [];
  data.forEach((d,i) => {
    const [label, val] = d;
    const frac = Math.max(0,val) / total;
    const a0 = angle, a1 = angle + frac * Math.PI*2;
    angle = a1;
    const color = SERIES[i % SERIES.length];
    legendItems.push({color, label:`${label} — ${(frac*100).toFixed(1)}%`, shape:"circle"});
    if(frac <= 0) return;

    const large = (a1 - a0) > Math.PI ? 1 : 0;
    const x0 = cx + r*Math.cos(a0), y0 = cy + r*Math.sin(a0);
    const x1 = cx + r*Math.cos(a1), y1 = cy + r*Math.sin(a1);
    let d_attr;
    if(inner > 0){
      const ix0 = cx + inner*Math.cos(a0), iy0 = cy + inner*Math.sin(a0);
      const ix1 = cx + inner*Math.cos(a1), iy1 = cy + inner*Math.sin(a1);
      d_attr = `M${ix0} ${iy0}L${x0} ${y0}A${r} ${r} 0 ${large} 1 ${x1} ${y1}L${ix1} ${iy1}A${inner} ${inner} 0 ${large} 0 ${ix0} ${iy0}Z`;
    } else {
      d_attr = `M${cx} ${cy}L${x0} ${y0}A${r} ${r} 0 ${large} 1 ${x1} ${y1}Z`;
    }
    const path = el("path", {d:d_attr, fill:color, stroke:CHART_SURFACE, "stroke-width":2}, s);
    hoverable(path, label, [["Value", money2(val)], ["Share", (frac*100).toFixed(2) + "%"]]);
  });

  if(opts.donut){
    txt(s, cx, cy - 6, compactMoney(total), {anchor:"middle", size:15, fill:INK_1, weight:650, tabular:true});
    txt(s, cx, cy + 12, "total", {anchor:"middle", size:10, fill:INK_MUTED});
  }

  if(opts.legendHost) renderLegend(opts.legendHost, legendItems);
  return s;
}

/* ---------------- Chart 4: line (single series, time axis) ---------------- */
function lineChart(host, data, opts){
  opts = opts || {};
  const w = Math.max(host.clientWidth || 520, 340);
  const padL = 46, padR = 14, padT = 16, padB = 32;
  const h = 250;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const s = svgRoot(host, w, h);
  s.setAttribute("aria-label", opts.aria || "line chart");

  const values = data.map(d => d[1]);
  const max = Math.max(...values, 1);
  const min = Math.min(0, ...values);
  const ticks = niceTicks(max, 4);
  const Y = v => padT + plotH - ((v-min)/(max-min || 1))*plotH;
  const X = i => padL + (data.length <= 1 ? plotW/2 : (i/(data.length-1))*plotW);

  ticks.forEach(t => {
    el("line", {x1:padL, y1:Y(t), x2:padL + plotW, y2:Y(t),
                stroke: t===0 ? AXIS : GRID, "stroke-width":1}, s);
    txt(s, padL - 9, Y(t), compactMoney(t), {anchor:"end", size:10, fill:INK_MUTED, tabular:true});
  });

  const pts = data.map((d,i) => [X(i), Y(d[1])]);
  const path = pts.map((p,i) => (i===0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
  el("path", {d:path, fill:"none", stroke:opts.color || SERIES[0], "stroke-width":2.4,
              "stroke-linejoin":"round", "stroke-linecap":"round"}, s);

  pts.forEach(([x,y], i) => {
    el("circle", {cx:x, cy:y, r:3.5, fill:opts.color || SERIES[0], stroke:CHART_SURFACE, "stroke-width":1.5}, s);
    const hit = el("circle", {cx:x, cy:y, r:14, fill:"transparent"}, s);
    hoverable(hit, String(data[i][0]), [["Value", money2(data[i][1])]]);
  });

  const stepLabels = Math.max(1, Math.round(data.length / 7));
  data.forEach((d,i) => {
    if(i % stepLabels !== 0 && i !== data.length-1) return;
    txt(s, X(i), padT + plotH + 14, String(d[0]).slice(5), {anchor:"middle", size:10, fill:INK_MUTED});
  });
  return s;
}

/* ---------------- Chart 5: status-encoded scatter (EC2 fleet) -------------- */
const BAND = {
  idle:  {label:"Idle (10% CPU or less)",  color:"var(--critical)", shape:"triangle"},
  under: {label:"Underutilized (10–45%)",  color:"var(--warning)",  shape:"square"},
  right: {label:"Right-sized (above 45%)", color:"var(--good)",     shape:"circle"},
  off:   {label:"Stopped or scheduled",    color:"var(--text-muted)", shape:"ring"}
};
function fleetScatter(host, list, opts, deps){
  // deps: {utilBand, instCost, monthlyOf}
  opts = opts || {};
  const w = Math.max(host.clientWidth || 520, 340);
  const padL = 56, padR = 92, padT = 18, padB = 44;
  const h = opts.height || 280;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const s = svgRoot(host, w, h);
  s.setAttribute("aria-label","Scatter of EC2 instances: CPU utilization against monthly cost");

  const maxCost = Math.max(200, ...list.map(i => deps.monthlyOf(i.type)));
  const X = c => padL + (c/100)*plotW;
  const Y = v => padT + plotH - (v/maxCost)*plotH;

  niceTicks(maxCost, 4).forEach(t => {
    el("line", {x1:padL, y1:Y(t), x2:padL + plotW, y2:Y(t),
                stroke: t===0 ? AXIS : GRID, "stroke-width":1}, s);
    txt(s, padL - 9, Y(t), compactMoney(t), {anchor:"end", size:10, fill:INK_MUTED, tabular:true});
  });
  [0,25,50,75,100].forEach(t => {
    txt(s, X(t), padT + plotH + 14, t + "%", {anchor:"middle", size:10, fill:INK_MUTED, tabular:true});
  });
  el("line", {x1:padL, y1:padT + plotH, x2:padL + plotW, y2:padT + plotH,
              stroke:AXIS, "stroke-width":1}, s);

  [[10,"idle"],[45,"right-sized"]].forEach(([v,lbl]) => {
    el("line", {x1:X(v), y1:padT, x2:X(v), y2:padT + plotH, stroke:GRID, "stroke-width":1}, s);
    txt(s, X(v) + 4, padT + 4, lbl, {size:9.5, fill:INK_MUTED});
  });

  txt(s, padL + plotW/2, h - 8, "Average CPU utilization (24h)",
      {anchor:"middle", size:10.5, fill:INK_MUTED});
  txt(s, 12, padT + plotH/2, "USD / month",
      {anchor:"middle", size:10.5, fill:INK_MUTED}).setAttribute(
      "transform", `rotate(-90 12 ${padT + plotH/2})`);

  const flagged = opts.ring || new Set();
  const R = 6;

  const placed = list.map(inst => {
    const cfg  = BAND[deps.utilBand(inst)];
    const cost = deps.instCost(inst);
    const x = X(inst.cpu), y = Y(cost);
    const g = el("g", {}, s);

    if(flagged.has(inst.id)){
      el("circle", {cx:x, cy:y, r:R + 6, fill:"none",
                    stroke:"var(--warning)", "stroke-width":1.5,
                    "stroke-dasharray":"3 3", opacity:.9}, g);
    }
    if(cfg.shape === "triangle"){
      el("path", {d:`M${x} ${y-R-1}L${x+R+1} ${y+R}L${x-R-1} ${y+R}Z`,
                  fill:cfg.color, stroke:CHART_SURFACE, "stroke-width":2,
                  "stroke-linejoin":"round"}, g);
    } else if(cfg.shape === "square"){
      el("rect", {x:x-R+.5, y:y-R+.5, width:2*R-1, height:2*R-1, rx:1.5,
                  fill:cfg.color, stroke:CHART_SURFACE, "stroke-width":2}, g);
    } else if(cfg.shape === "ring"){
      el("circle", {cx:x, cy:y, r:R-1, fill:"none",
                    stroke:cfg.color, "stroke-width":2}, g);
    } else {
      el("circle", {cx:x, cy:y, r:R, fill:cfg.color, stroke:CHART_SURFACE, "stroke-width":2}, g);
    }

    const hit = el("circle", {cx:x, cy:y, r:15, fill:"transparent"}, g);
    hoverable(hit, inst.name + "  ·  " + inst.id, [
      ["Type", inst.type + (inst.type !== inst.origType ? "  (was " + inst.origType + ")" : "")],
      ["State", inst.state],
      ["Avg CPU", inst.cpu + "%"],
      ["Cost", money2(cost) + " / mo"],
      ["Region", inst.region],
      ["Project / env", inst.project + " · " + inst.env],
      ["Classification", cfg.label]
    ]);
    return {inst, x, y};
  });

  const boxes = [];
  const CH = 5.55, LH = 12;
  const hits = b => boxes.some(o =>
    b.x1 < o.x2 && b.x2 > o.x1 && b.y1 < o.y2 && b.y2 > o.y1);

  placed.forEach(({inst, x, y}) => {
    const tw = inst.name.length * CH;
    const cands = [
      {x:x + R + 7, y,          anchor:"start",  x1:x + R + 7,      x2:x + R + 7 + tw},
      {x,           y:y + LH+5, anchor:"middle", x1:x - tw/2,       x2:x + tw/2},
      {x,           y:y - LH-5, anchor:"middle", x1:x - tw/2,       x2:x + tw/2},
      {x:x - R - 7, y,          anchor:"end",    x1:x - R - 7 - tw, x2:x - R - 7}
    ];
    const fit = cands.find(c => {
      const b = {x1:c.x1 - 2, x2:c.x2 + 2, y1:c.y - LH/2, y2:c.y + LH/2};
      return c.x1 >= 4 && c.x2 <= w - 4 && c.y >= padT + 4 &&
             c.y <= padT + plotH - 2 && !hits(b);
    });
    if(!fit) return;
    boxes.push({x1:fit.x1 - 2, x2:fit.x2 + 2, y1:fit.y - LH/2, y2:fit.y + LH/2});
    txt(s, fit.x, fit.y, inst.name, {size:10.5, fill:INK_2, anchor:fit.anchor});
  });
  return s;
}
function fleetLegendItems(extra){
  const items = ["idle","under","right","off"].map(k => ({color:BAND[k].color, shape:BAND[k].shape, label:BAND[k].label}));
  if(extra) items.push({color:"var(--warning)", shape:"dash", label:extra});
  return items;
}

/* ---------------- chart-spec normalizer (LLM-suggested charts) ------------- */
function mapChartType(t){
  t = String(t || "").toLowerCase();
  if(t === "pie") return "pie";
  if(t === "doughnut" || t === "donut") return "donut";
  if(t === "line" || t === "area") return "line";
  return "bar"; // bar, and anything unsupported (e.g. heatmap), degrades to bar
}
/* Renders an LLM chart spec {type, x_field/x, y_field/y, data:[{x,y}|{...}]}
   into `host`, with a `legendHost` for multi-category forms and a `tableHost`
   for the always-present table-view twin. Returns false if there's no usable
   data (caller should show an empty state instead). */
function renderChartSpec(host, legendHost, tableHost, spec){
  const xKey = spec.x_field || spec.x || "x";
  const yKey = spec.y_field || spec.y || "y";
  const rows = (spec.data || []).map(d => [
    String(d?.[xKey] ?? d?.x ?? d?.name ?? ""),
    Number(d?.[yKey] ?? d?.y ?? d?.value ?? 0)
  ]).filter(([,v]) => Number.isFinite(v));
  if(!rows.length) return false;

  const kind = mapChartType(spec.type);
  if(legendHost) legendHost.innerHTML = "";
  if(kind === "pie" || kind === "donut"){
    pieChart(host, rows, {donut: kind === "donut", legendHost, aria: spec.title || "chart"});
  } else if(kind === "line"){
    lineChart(host, rows, {aria: spec.title || "chart"});
  } else {
    const total = rows.reduce((s,d) => s + d[1], 0);
    columnChart(host, rows, {aria: spec.title || "chart", total});
  }
  if(tableHost){
    renderTable(tableHost, [{label:xKey}, {label:yKey, num:true}],
      rows.map(r => [r[0], money2(r[1])]));
  }
  return true;
}

/* =========================================================================
   Pipeline flowchart — renders the run-progress steps as a small flowchart
   instead of a plain list, mirroring the pipeline's real shape (see
   src/orchestrator.py's own diagram comment):
     Inputs -> Step1 -> Step2 -> (Step3.1 || Step3.2 -> Step3.3) -> Step4 -> Step5
   Node positions and edges live on a 0–100 logical grid; the SVG edge layer
   uses viewBox="0 0 100 100" with preserveAspectRatio="none" so it stretches
   to exactly match the percentage-positioned node overlay at any width.
   ========================================================================= */
const FLOW_LAYOUT = {
  // 10 evenly-spaced columns (7% margin each side) and 3 rows (18/50/82) —
  // spacing is chosen so that, combined with .flowchart's min-width and
  // .flow-node's fixed pixel width in shared.css, adjacent same-row nodes
  // never overlap and top/bottom-row nodes never clip the container edge.
  positions: {
    inputs:         {x:7,     y:50},
    step1:          {x:16.56, y:50},
    step2:          {x:26.11, y:50},
    forecast:       {x:35.67, y:50},
    tag_governance: {x:45.22, y:50},
    root_cause:     {x:54.78, y:50},
    step3_1:        {x:64.33, y:18},
    step3_2:        {x:64.33, y:82},
    step3_3:        {x:73.89, y:82},
    step4:          {x:83.44, y:50},
    step5:          {x:93,    y:50},
  },
  edges: [
    ["inputs","step1"], ["step1","step2"],
    ["step2","forecast"], ["forecast","tag_governance"], ["tag_governance","root_cause"],
    ["root_cause","step3_1"], ["root_cause","step3_2"],
    ["step3_2","step3_3"],
    ["step3_1","step4"], ["step3_3","step4"],
    ["step4","step5"],
  ],
};
let flowInstanceCounter = 0;

function flowEdgePath(a, b){
  if(Math.abs(a.y - b.y) < 0.5) return `M${a.x} ${a.y} L${b.x} ${b.y}`;
  const mx = (a.x + b.x) / 2; // horizontal-anchored S-curve: a clean branch/merge look
  return `M${a.x} ${a.y} C${mx} ${a.y} ${mx} ${b.y} ${b.x} ${b.y}`;
}

/* stepDefs: [{key, name}] — key must match FLOW_LAYOUT.positions (the 8
   pipeline step keys the backend emits). Returns {steps, refreshEdges}:
   `steps` has the same {key: {el, timer}} shape the old plain-list builder
   returned (el is a node div containing .rs-name/.rs-verb/.rs-time, same as
   before) so callers only need to swap the builder, not their event logic.
   Call refreshEdges() after any node's data-state changes to "done" so the
   edge leaving it lights up. */
const FLOW_BASE_W = 1260, FLOW_BASE_H = 280;
const FLOW_ZOOM_MIN = 0.5, FLOW_ZOOM_MAX = 1.5, FLOW_ZOOM_STEP = 0.1;

function buildFlowchart(host, stepDefs){
  const markerId = `flowArrow${flowInstanceCounter++}`;
  const pos = FLOW_LAYOUT.positions;
  const edgesHtml = FLOW_LAYOUT.edges.map(([f,t]) => {
    const a = pos[f], b = pos[t];
    if(!a || !b) return "";
    return `<path class="flow-edge" data-from="${f}" data-to="${t}" d="${flowEdgePath(a,b)}" marker-end="url(#${markerId})"/>`;
  }).join("");
  const nodesHtml = stepDefs.map(d => {
    const p = pos[d.key] || {x:50,y:50};
    return `<div class="flow-node" data-key="${d.key}" data-state="pending" style="left:${p.x}%;top:${p.y}%">
      <div class="fn-top"><span class="rs-dot"></span><span class="rs-name">${esc(d.name)}</span></div>
      <span class="rs-verb"></span><span class="rs-time"></span>
    </div>`;
  }).join("");

  host.innerHTML = `
    <div class="flow-toolbar">
      <button type="button" class="flow-zoom-btn" data-zoom="out" aria-label="Zoom out">−</button>
      <span class="flow-zoom-label">100%</span>
      <button type="button" class="flow-zoom-btn" data-zoom="in" aria-label="Zoom in">+</button>
    </div>
    <div class="flow-scroll"><div class="flow-zoom-sizer" style="width:${FLOW_BASE_W}px;height:${FLOW_BASE_H}px">
    <div class="flowchart">
    <svg class="flow-edges" viewBox="0 0 100 100" preserveAspectRatio="none">
      <defs><marker id="${markerId}" viewBox="0 0 10 10" refX="8" refY="5"
        markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M0 0L10 5L0 10z" fill="context-stroke"/>
      </marker></defs>
      ${edgesHtml}
    </svg>
    ${nodesHtml}
  </div></div></div>`;

  // Zoom: scales the .flowchart node visually via CSS transform while the
  // .flow-zoom-sizer wrapper's own (unscaled-by-transform) width/height is set
  // to match, so .flow-scroll's overflow-x scrollbar always reflects the true
  // scrollable size at the current zoom level instead of the original 100%.
  let zoom = 1;
  const flowchartEl = host.querySelector(".flowchart");
  const sizerEl = host.querySelector(".flow-zoom-sizer");
  const zoomLabel = host.querySelector(".flow-zoom-label");
  function applyZoom(){
    flowchartEl.style.transform = `scale(${zoom})`;
    sizerEl.style.width = `${FLOW_BASE_W * zoom}px`;
    sizerEl.style.height = `${FLOW_BASE_H * zoom}px`;
    zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  }
  host.querySelectorAll(".flow-zoom-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      zoom = btn.dataset.zoom === "in"
        ? Math.min(FLOW_ZOOM_MAX, +(zoom + FLOW_ZOOM_STEP).toFixed(2))
        : Math.max(FLOW_ZOOM_MIN, +(zoom - FLOW_ZOOM_STEP).toFixed(2));
      applyZoom();
    });
  });

  const steps = {};
  stepDefs.forEach(d => {
    steps[d.key] = {el: host.querySelector(`.flow-node[data-key="${d.key}"]`), timer:null};
  });
  const edgeEls = Array.from(host.querySelectorAll(".flow-edge"));
  function refreshEdges(){
    edgeEls.forEach(p => {
      const src = steps[p.dataset.from];
      p.classList.toggle("done", !!src && src.el.dataset.state === "done");
    });
  }
  return {steps, refreshEdges};
}

/* =========================================================================
   Export
   ========================================================================= */
window.Shared = {
  esc, renderMarkdown, money, money2, compactMoney, formatNumber, formatKpiValue, fmtTime, fmtStepElapsed,
  toast, hoverable, showTip, hideTip,
  ICON, IMPACT_ICON, BAND,
  el, svgRoot, txt, barRight, barUp, niceTicks,
  renderTable, renderLegend,
  hbarChart, columnChart, pieChart, lineChart, fleetScatter, fleetLegendItems,
  mapChartType, renderChartSpec, buildFlowchart
};
})();
