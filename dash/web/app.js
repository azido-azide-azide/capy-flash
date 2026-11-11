"use strict";
/* Capy Flash dashboard — trimmed from basalt serve/web/app.js @ main cc0a763.
   Dropped vs upstream (single-stream engine, dense model, one model per serve):
   - Experts sidebar tiers (merged app.js 141-156): no expert tiers to split
   - model picker + POST v1/load (merged 329-391): ninfer serves one model
   - slot grid + decode-line chips (merged 161-181, 221-225, 234-257, 321-323): total slots = 1
   - loading/waking state, load % + pbar (merged 399, 403-405): ninfer never hot-loads
   Everything else (charts, single-slot Context pane, Requests, Queue, log level
   tabs with server-side level windows) is byte-faithful to the merged upstream. */
const $ = s => document.querySelector(s);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const num = v => typeof v === "number" && Number.isFinite(v);
const fmt = (v, d = 0) => num(v) ? v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "-";
const p2 = n => String(n).padStart(2, "0");
const clock = ms => { const d = new Date(ms); return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`; };
const gib = n => num(n) ? (n / 1024 ** 3).toFixed(1) : "-";
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
const drawn = new WeakMap();
const html = (el, h) => { if (drawn.get(el) !== h) { el.innerHTML = h; drawn.set(el, h); } };   // skip unchanged redraws
const text = (el, v) => { if (el.textContent !== v) el.textContent = v; };
const HIST = 60;
const kt = n => n >= 100000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);   // 41.2k, 262k
const tick = n => n >= 1000 ? `${+(n / 1000).toFixed(1)}k` : String(Math.round(n));
const API = { openai: "OpenAI", anthropic: "Anthropic" };
const shortGpu = s => String(s || "").replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "");
const shortCpu = s => String(s || "").replace(/\((R|TM)\)/gi, "").replace(/\s+\d+-Core Processor$/i, "")
  .replace(/\s+CPU\s+@.*$/i, "").replace(/\s+Processor$/i, "").replace(/\s+/g, " ").trim();

let apiKey = sessionStorage.getItem("capy.key") || "";
let M = null;          // the last /metrics reading, with .slots from /slots

async function get(path) {
  const r = await fetch(path, { cache: "no-store", headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {} });
  if (r.status === 401) throw Object.assign(new Error("locked"), { locked: true });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/* ---- series and charts ---- */
// the last HIST samples, padded at the start with null (no line) so the newest sample sits at the right edge
const tail = a => { const v = (a || []).slice(-HIST).map(x => num(x) ? x : null); while (v.length < HIST) v.unshift(null); return v; };
const stats = a => {
  const v = a.filter(num);
  return v.length ? { min: Math.min(...v), max: Math.max(...v), avg: v.reduce((t, x) => t + x, 0) / v.length, last: v[v.length - 1] } : null;
};
const STEPS = [10, 20, 50, 100, 120, 150, 160, 200, 250, 300, 400, 500, 600, 800, 1000, 1200, 1500, 2000, 3000, 4000, 5000,
  8000, 10000, 16000, 20000, 32000, 50000];
const nice = v => STEPS.find(s => s >= v * 1.15) || Math.ceil(v * 1.15 / 10000) * 10000;
// polyline points in a 0..W x 0..100 box; a null sample breaks the line
function lines(a, y, W = 100) {
  const out = []; let cur = [];
  a.forEach((v, i) => {
    if (v == null) { if (cur.length) out.push(cur); cur = []; return; }
    cur.push(`${(i / (a.length - 1) * W).toFixed(2)},${y(v).toFixed(2)}`);
  });
  if (cur.length) out.push(cur);
  return out.map(c => c.length === 1 ? [c[0], c[0].replace(/^[\d.]+/, x => String(+x + .01))] : c);
}
const poly = (pts, attrs) => `<polyline points="${pts.join(" ")}" fill="none" vector-effect="non-scaling-stroke" stroke-linejoin="round" ${attrs}/>`;
// trailing gap (stream ended, no fresh data): plot the remaining samples on
// the y=0 baseline so the line reaches y=0 and runs to the right edge
// instead of ending mid-chart
const tailZero = a => {
  let last = a.length - 1;
  while (last >= 0 && a[last] == null) last--;
  if (last < 0 || last === a.length - 1) return a;
  return a.map((v, i) => i > last ? 0 : v);
};
const area = (pts, fill) => `<polygon points="${pts[0].split(",")[0]},100 ${pts.join(" ")} ${pts[pts.length - 1].split(",")[0]},100" fill="${fill}"/>`;
const baseLine = `<line x1="0" y1="100" x2="100" y2="100" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
let gid = 0;
function sbChart(a, lo, hi, accent, limit) {
  const id = `sbg${++gid}`, tint = accent ? "255,122,61" : "154,164,173";
  const segs = lines(a, v => clamp(100 - (v - lo) / (hi - lo) * 100, 1, 99));
  return `<div class="sb-ch"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0" stop-color="rgba(${tint},.13)"/><stop offset="1" stop-color="rgba(${tint},.02)"/></linearGradient></defs>${baseLine}`
    + (limit ? `<line x1="0" y1="1" x2="100" y2="1" stroke="var(--basalt-500)" stroke-width="1" stroke-dasharray="2 3" vector-effect="non-scaling-stroke"/>` : "")
    + segs.map(p => area(p, `url(#${id})`) + poly(p, `stroke="${accent ? "var(--accent)" : "var(--fg-2)"}" stroke-width="1.5"`)).join("")
    + `</svg></div>`;
}
const gy = t => t ? 100 - t * 92 - 4 : 4;             // gridline height for a fraction of the axis
const grid = fr => fr.map(t => `<line x1="0" x2="100" y1="${gy(t)}" y2="${gy(t)}" stroke="var(--line-soft)" vector-effect="non-scaling-stroke"/>`).join("");
// the y=0 axis: the Speed charts map 0 → 96 (4 top pad, 92 span), so the base
// line sits one grid step below the .75 line — same --line style as the sparklines' baseLine
const speedBase = `<line x1="0" x2="100" y1="96" y2="96" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
// small tick labels at the right edge, at the top and middle gridlines, skipped where an end label sits
const ticks = (max, labels) => [[4, max], [50, max / 2]].filter(t => !labels.some(l => Math.abs(l - t[0]) < 12))
  .map(t => `<span class="tk" style="top:${t[0]}%">${tick(t[1])}</span>`).join("");

/* ---- top bar ---- */
function setState(s, label) { $("#state").dataset.s = s; text($("#state"), label); }

/* ---- sidebar ---- */
const sbRow = (k, v, u, st) => `<div class="sb-row"><span class="k">${k}</span>${st ? `<span class="st">${st}</span>` : ""}<span class="num">${v}${u ? `<span class="u">${u}</span>` : ""}</span></div>`;
const bar = f => `<div class="bar4"><i style="width:${(clamp(f || 0, 0, 1) * 100).toFixed(1)}%"></i></div>`;
const r0 = v => num(v) ? String(Math.round(v)) : "-";
function sbLoad(h, now, label = "Load") {
  const s = stats(h);
  return `<div class="sb-met grow">${sbRow(label, num(now) ? `${Math.round(now)}%` : "-", "", s ? `Min ${Math.round(s.min)} · Avg ${Math.round(s.avg)}%` : "")}${sbChart(h, 0, 100, true)}</div>`;
}
function sbPower(h, now, lim) {
  const s = stats(h), top = num(lim) && lim > 0 ? lim : Math.max(1, s ? s.max : 1);
  return `<div class="sb-met grow">${sbRow("Power", `${r0(now)}${num(lim) ? ` / ${Math.round(lim)}` : ""}`, "W", s ? `Avg ${Math.round(s.avg)} W` : "")}${sbChart(h, 0, top, false, num(lim))}</div>`;
}
function sbTemp(h, now) {
  const s = stats(h), lo = Math.min(40, s ? Math.floor(s.min) - 5 : 40), hi = Math.max(85, s ? Math.ceil(s.max) + 5 : 85);
  return `<div class="sb-met grow">${sbRow("Temp", r0(now), "°C", s ? `Min ${Math.round(s.min)} · Max ${Math.round(s.max)} °C` : "")}${sbChart(h, lo, hi, false)}</div>`;
}
const link = g => num(g.pcie_gen_max ?? g.pcie_gen) ? `Gen${g.pcie_gen_max ?? g.pcie_gen}${num(g.pcie_width) ? ` x${g.pcie_width}` : ""}` : "";
const vramRow = g => `<div class="sb-met">${sbRow("VRAM", num(g.mem_used) ? `${gib(g.mem_used)} / ${gib(g.mem_total)}` : "-", "GiB")}${bar(num(g.mem_total) ? g.mem_used / g.mem_total : 0)}</div>`;
function sbGpu(g, h, name, alone) {
  const right = alone ? `<span class="num">${esc(link(g))}</span>`
    : `<span class="num">${num(g.temp) ? `<b>${Math.round(g.temp)}</b><span class="u">°C</span>` : ""}${num(g.temp) && link(g) ? " · " : ""}${esc(link(g))}</span>`;
  return `<div class="sb-gpu"><div class="sb-gt"><span>${esc(name)}</span>${right}</div>${vramRow(g)}`
    + sbLoad(tail(h.util), g.util) + sbPower(tail(h.power), g.power, g.power_limit) + (alone ? sbTemp(tail(h.temp), g.temp) : "") + `</div>`;
}
function sbTable(G, H, names) {
  let h = `<div class="sb-tbl"><div class="h l">Load</div><div class="h">VRAM GiB</div><div class="h">Power W</div><div class="h">°C</div>`;
  G.forEach(g => {
    h += `<div class="t"><span>${esc(names(g))}</span><span class="num">${esc(link(g))}</span></div>`
      + `<div class="c l">${num(g.util) ? `${Math.round(g.util)}%` : "-"}</div>`
      + `<div class="c"><span>${gib(g.mem_used)}<span class="u">/${gib(g.mem_total)}</span></span></div>`
      + `<div class="c"><span>${r0(g.power)}<span class="u">/${r0(g.power_limit)}</span></span></div>`
      + `<div class="c">${r0(g.temp)}</div>`;
  });
  // the mean load and total power of all cards, sample by sample
  const series = k => tail(H[0] ? H[0][k] : []).map((_, i) => {
    const v = H.map(x => tail(x[k])[i]).filter(num);
    return v.length ? v.reduce((t, x) => t + x, 0) / (k === "util" ? v.length : 1) : null;
  });
  const sum = k => G.reduce((t, g) => t + (num(g[k]) ? g[k] : 0), 0);
  const load = G.filter(g => num(g.util)), mean = load.length ? load.reduce((t, g) => t + g.util, 0) / load.length : null;
  return h + `</div><div class="sb-agg"><div class="sb-gt"><span>All ${G.length} GPUs</span></div>`
    + sbLoad(series("util"), mean, "Mean load") + sbPower(series("power"), sum("power"), sum("power_limit") || null) + `</div>`;
}
function drawSide(hw, st, hist) {
  gid = 0;
  const G = hw.gpus || [], H = Object.fromEntries((hist.gpus || []).map(x => [x.index, x]));
  const named = Object.fromEntries((st.gpus || []).map(g => [g.index, shortGpu(g.name)]));
  const title = g => `GPU ${g.index} · ${named[g.index] || "GPU"}`;
  html($("#gpus"), !G.length ? `<div class="sb-none">No GPU readings.</div>`
    : G.length >= 3 ? sbTable(G, G.map(g => H[g.index] || {}), title)
    : G.map(g => sbGpu(g, H[g.index] || {}, G.length === 1 ? named[g.index] || title(g) : title(g), G.length === 1)).join(""));

  const cpu = shortCpu(st.cpu_name);
  text($("#cpu-name"), cpu); $("#cpu-name").title = st.cpu_name || "";
  html($("#sys"), sbRow("CPU", num(hw.cpu) ? `${Math.round(hw.cpu)}%` : "-")
    + `<div class="sb-met">${sbRow("RAM", num(hw.ram_total) ? `${gib(hw.ram_used)} / ${gib(hw.ram_total)}` : "-", "GiB")}${bar(num(hw.ram_total) ? hw.ram_used / hw.ram_total : 0)}</div>`
    + sbRow("Disk read", num(hw.disk_read_mb) ? hw.disk_read_mb.toFixed(1) : "-", "MB/s")
    + (st.driver ? `<div class="sb-foot">Driver ${esc(st.driver)}${st.cuda ? ` · CUDA ${esc(st.cuda)}` : ""}</div>` : ""));
  // upstream's expert-tier stack (merged 141-156) is cut: the 27B dense model has no experts panel to feed
}

/* ---- Speed ---- */
const SC0 = "#FF7A3D";
function drawHero(live, hist, reqs, best, slots) {
  const generating = live.state === "generating", reading = live.state === "reading";
  // the live rate while generating, else the newest finished request's that has one
  const r = reqs[0], rd = reqs.find(x => num(x.decode_tok_s)), last = rd ? rd.decode_tok_s : null;
  const liveDec = generating && num(live.tok_s), dec = liveDec ? live.tok_s : last;
  text($("#dec-k"), liveDec ? "Decode" : last != null ? "Decode · Last request" : "Decode");
  html($("#dec-v"), num(dec) ? `${dec.toFixed(1)}<small>tok/s</small>` : "-");
  $("#dec-v").classList.toggle("idle", !liveDec);

  // single stream: one aggregate series, no per-slot chips (upstream 161-181, 221-225)
  const agg = tailZero(tail(hist.tok_s));
  const peak = a => Math.max(0, ...a.filter(num));
  const max = nice(Math.max(peak(agg), 40));
  const y = v => 100 - 4 - Math.min(v, max) / max * 92;
  let svg = grid([0, .25, .5, .75]) + speedBase;
  lines(agg, y).forEach(p => { svg += poly(p, `stroke="${SC0}" stroke-width="1.7"`); });
  html($("#dec-chart"), `<svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Decode tokens per second, last 60 seconds">${svg}</svg>${ticks(max, [])}`);

  // the live read rate while reading, else the newest finished request's that read part of its prompt
  const readOf = x => (x.prompt_tokens || 0) - (x.reused || 0);
  const rp = reqs.find(x => x.prompt_ms > 0 && readOf(x) > 0), lastPre = rp ? readOf(rp) / (rp.prompt_ms / 1000) : null;
  const livePre = reading && num(live.prefill_tok_s_mean) && live.prefill_tok_s_mean > 0;
  const pre = livePre ? live.prefill_tok_s_mean : lastPre;
  text($("#pre-k"), livePre ? "Prefill" : lastPre != null ? "Prefill · Last request" : "Prefill");
  html($("#pre-v"), num(pre) ? `${fmt(pre)}<small>tok/s</small>` : "-");
  const pa = tailZero(tail(hist.prefill_tok_s_mean).map(v => v > 0 ? v : v === 0 ? 0 : null));
  const pmax = nice(Math.max(1000, ...pa.filter(num)));
  html($("#pre-chart"), `<svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Prefill tokens per second, last 60 seconds">${grid([0, .5])}${speedBase}`
    + lines(pa, v => 100 - 4 - Math.min(v, pmax) / pmax * 92).map(p => poly(p, `stroke="var(--fg)" stroke-width="1.6"`)).join("") + `</svg>${ticks(pmax, [])}`);
  html($("#pre-last"), r && num(r.prompt_tokens) ? `<b>${fmt(r.prompt_tokens)}</b> tok${num(r.prompt_ms) ? ` in <b>${(r.prompt_ms / 1000).toFixed(2)}</b> s` : ""}` : "-");
  text($("#pre-hit"), r && r.prompt_tokens > 0 && num(r.reused) ? `${Math.round(r.reused / r.prompt_tokens * 100)}%` : "-");

  const bd = best.decode, bp = best.prefill;
  const p85 = M.p85 || {}, pDec = p85.decode, pPre = p85.prefill;
  const pNote = (m, unit) => m ? ` · The 85th percentile of ${fmt(m.n)} kept requests with at least ${fmt(m.min_tokens)} ${unit}` : "";
  html($("#foot-dec"), `Single stream · Best <b>${bd ? fmt(bd.tok_s, 1) : "-"}</b> · P85 <b>${pDec ? fmt(pDec.tok_s, 1) : "-"}</b> tok/s`);
  html($("#foot-pre"), `Best <b>${bp ? fmt(bp.tok_s) : "-"}</b> · P85 <b>${pPre ? fmt(pPre.tok_s, 1) : "-"}</b> tok/s`);
  $("#foot-dec").title = (bd ? `Fastest whole request since the server started: ${bd.id}, ${fmt(bd.output_tokens)} tokens, ${clock(bd.time * 1000)}` : "") + pNote(pDec, "output tokens");
  $("#foot-pre").title = (bp ? `Fastest prompt read since the server started: ${bp.id}, ${fmt(bp.read_tokens)} tokens read, ${clock(bp.time * 1000)}` : "") + pNote(pPre, "newly read tokens");
}

/* ---- main card: Context | Requests | Queue ---- */
let tab = sessionStorage.getItem("capy.tab") || "req";
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-t]"); if (!b) return;
  tab = b.dataset.t; try { sessionStorage.setItem("capy.tab", tab); } catch (err) { /* private mode */ }
  draw();
});
function requestsPane(live, reqs) {
  const rows = [];
  if (live.state === "reading" || live.state === "generating")
    rows.push({ t: Date.now() / 1000 - (live.elapsed_s || 0), id: "Running", path: "", out: live.generated, tps: live.tok_s, dur: live.elapsed_s, live: true });
  reqs.forEach(r => rows.push({ t: r.time, id: r.id, path: r.path, out: r.output_tokens, tps: r.decode_tok_s, dur: r.duration_s, finish: r.finish }));
  if (!rows.length) return `<div class="empty">No requests yet.</div>`;
  const ep = p => String(p || "-").replace("/v1/", "").replace("chat/completions", "chat");
  return `<table><thead><tr><th>Time</th><th>ID</th><th>Endpoint</th><th class="r">Output</th><th class="r">Rate</th><th class="r">Duration</th></tr></thead><tbody>`
    + rows.map(r => {
      // a request that did not simply stop is marked on its id, with the reason
      const odd = r.finish && r.finish !== "stop" ? (r.finish === "error" ? "err" : "warn") : "";
      return `<tr${r.live ? ` class="live"` : ""}><td>${clock(r.t * 1000)}</td><td class="${odd}">${esc(r.id)}${odd ? ` · ${esc(r.finish)}` : ""}</td>`
        + `<td>${r.live ? "-" : esc(ep(r.path))}</td><td class="r">${num(r.out) ? fmt(r.out) : "-"}</td><td class="r">${num(r.tps) ? r.tps.toFixed(1) : "-"}</td>`
        + `<td class="r">${num(r.dur) ? `${r.dur.toFixed(1)} s` : "-"}</td></tr>`;
    }).join("") + `</tbody></table>`;
}
const mss = s => `${Math.floor(s / 60)}:${p2(Math.floor(s % 60))}`;
function queuePane(live) {
  const q = live.queue || [], n = live.queued || 0;
  if (!n && !q.length) return `<div class="empty">No request is waiting.</div>`;
  const toks = q.reduce((t, r) => t + (num(r.prompt_tokens) ? r.prompt_tokens : 0), 0);
  return `<div class="qsum"><span><b>${fmt(Math.max(n, q.length))}</b> prompt${Math.max(n, q.length) === 1 ? "" : "s"} waiting</span><span><b>${fmt(toks)}</b> tok to read</span></div>`
    + `<table><thead><tr><th>API</th><th class="r">Prompt tok</th><th class="r">Waiting</th></tr></thead><tbody>`
    + q.map(r => `<tr><td>${esc(API[r.api] || r.api || "-")}</td><td class="r">${num(r.prompt_tokens) ? fmt(r.prompt_tokens) : "-"}</td><td class="r">${mss(r.waited_s || 0)}</td></tr>`).join("")
    + `</tbody></table>`;
}
/* ---- Context: the one slot's window, used against free, and the tokens of each recent request ---- */
const kc = n => n >= 1048576 ? `${n / 1048576 >= 10 ? Math.round(n / 1048576) : +(n / 1048576).toFixed(1)}M` : kt(n).replace("k", "K");
const pctOf = (a, b) => b ? Math.round(a / b * 100) : 0;
// stacked columns, one per recent request, oldest first: prompt read (the uncached part) / generated, scaled to the largest
function ctxHistory(reqs) {
  const h = reqs.slice(0, 24).reverse().map(r => { return { p: (r.prompt_tokens || 0) - clamp(r.reused || 0, 0, r.prompt_tokens || 0), g: r.output_tokens || 0 }; });
  if (!h.length) return "";
  const W = 480, H = 100, step = W / h.length, bw = step * .68;
  const top = nice(Math.max(1, ...h.map(r => r.p + r.g)) / 1.15);
  let g = "";
  h.forEach((r, i) => {
    let y = H; const x = i * step + (step - bw) / 2;
    [["p", "rgba(154, 164, 173, .5)"], ["g", "var(--accent)"]].forEach(([k, col]) => {
      const hh = r[k] / top * H; y -= hh;
      g += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" fill="${col}"/>`;
    });
  });
  return `<div class="cx-hist"><div class="hh"><b>Tokens per request</b><span>${h.length === 1 ? "Last request" : `Last ${h.length} requests`}</span></div><div class="cx-chart">`
    + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Tokens per request">`
    + `<line x1="0" x2="${W}" y1="${H}" y2="${H}" stroke="var(--line)"/><line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--line-soft)" stroke-dasharray="2 4"/>${g}</svg>`
    + `<span class="tk" style="top:0">${kc(top)}</span><span class="tk" style="top:50%">${kc(top / 2)}</span><span class="tk" style="top:100%">0</span>`
    + `<div class="xl"><span>Oldest</span><span>Latest</span></div></div></div>`;
}
function contextPane(s, reqs, eng) {
  const n = s.n_ctx || eng.context || 0, used = s.ctx_used || 0;
  return `<div class="cx1"><div class="cx-head"><div class="v">${kc(used)}<small>/ ${kc(n)} · ${pctOf(used, n)}%</small></div></div>`
    + `<div class="cx-bar"><i style="width:${n ? clamp(used / n * 100, 0, 100).toFixed(2) : 0}%"></i></div>`
    + `<div class="cx-scale"><span>0</span><span>${kc(n / 2)}</span><span>${kc(n)}</span></div>`
    + ctxHistory(reqs) + `</div>`;
}
function drawMain(live, hist, reqs, slots) {
  const n = live.queued || 0;
  const T = (reqs.length || (slots[0] && slots[0].ctx_used > 0) ? [["ctx", "Context"]] : []).concat([["req", "Requests"], ["queue", `Queue (${n})`]]);
  const cur = T.some(t => t[0] === tab) ? tab : T[0][0];
  html($("#tabs"), T.map(t => `<button type="button" role="tab" data-t="${t[0]}" aria-selected="${t[0] === cur}">${t[1]}</button>`).join(""));
  html($("#pane"), cur === "ctx" ? contextPane(slots[0] || {}, reqs, M.engine || {})
    : cur === "req" ? requestsPane(live, reqs) : queuePane(live));
}

/* ---- the whole page ---- */
function draw() {
  if (!M) return;
  const live = M.live || {}, eng = M.engine || {}, hist = M.history || {}, reqs = M.requests || [];
  const slots = M.slots || [];
  const name = (live.state || "idle")[0].toUpperCase() + (live.state || "idle").slice(1);
  setState(live.state || "idle", live.queued > 0 ? `${name} · ${live.queued} queued` : name);
  text($("#model"), eng.model || "");
  drawSide(M.hardware || {}, M.hardware_static || {}, hist);
  drawHero(live, hist, reqs, M.best || {}, slots);
  drawMain(live, hist, reqs, slots);
}

/* ---- logs: level tabs filter the kept lines, their counts are the server's lifetime ones ---- */
const logEl = $("#log"), MAXLOG = 400;
let logCount = 0, follow = true;
let logBuf = [], logLevels = null, logTab = sessionStorage.getItem("capy.logtab") || "all";
let logWin = null, logWinMax = 0;      // the open level's newest lines, fetched from the server's whole ring
const LOG_TABS = [["all", "All"], ["warn", "Warnings"], ["err", "Errors"]];
const logShown = e => logTab === "all" || e.lvl === logTab;
function lineEl(e) {
  const d = document.createElement("div");
  if (e.lvl !== "info") d.className = e.lvl;
  const t = document.createElement("i"); t.textContent = clock(e.t * 1000);
  d.append(t, `  ${e.src === "srv" ? "[server] " : ""}${e.msg}`);
  return d;
}
function drawLogTabs() {
  const at = l => (logLevels && logLevels[l]) || 0;
  const total = logLevels ? at("info") + at("warn") + at("err") : 0;
  html($("#logtabs"), LOG_TABS.map(([k, word]) =>
    `<button type="button" role="tab" data-t="${k}" aria-selected="${k === logTab}" title="${k === "all" ? "lines" : word.toLowerCase()} since the server started">${word} (${fmt(k === "all" ? total : at(k))})</button>`).join(""));
}
function emptyNote(text) {
  const d = document.createElement("div"); d.className = "empty"; d.textContent = text; logEl.replaceChildren(d);
}
function domAppend(lines) {
  if (!lines.length) return;
  logEl.querySelector(".empty")?.remove();
  logEl.append(...lines.map(lineEl));
  while (logEl.childElementCount > MAXLOG) logEl.firstChild.remove();
  if (follow) logEl.scrollTop = logEl.scrollHeight;
}
function renderLogs() {                // the All tab shows the client's own newest lines
  const lines = logBuf.filter(logShown);
  if (lines.length) { logEl.replaceChildren(...lines.map(lineEl)); if (follow) logEl.scrollTop = logEl.scrollHeight; }
  else emptyNote("No log lines yet.");
}
async function showLogs() {            // the level tabs ask the server for the newest lines at their level,
  drawLogTabs();                       // so boot-time lines show up although no client kept them
  if (logTab === "all") { logWin = null; renderLogs(); return; }
  const tab = logTab;
  logWin = null;
  try {
    const r = await get(`api/logs?level=${tab}&limit=${MAXLOG}&after=0`);
    if (logTab !== tab) return;        // another tab was chosen while this was in flight
    logWin = r.lines; logWinMax = logWin.length ? logWin[logWin.length - 1].id : 0;
    if (logWin.length) { logEl.replaceChildren(...logWin.map(lineEl)); if (follow) logEl.scrollTop = logEl.scrollHeight; }
    else emptyNote(logLevels && logLevels[tab] ? `The ${tab === "warn" ? "warning" : "error"} lines are older than what the server keeps.` : `No ${tab === "warn" ? "warnings" : "errors"}.`);
  } catch (e) { /* the metrics poll reports the connection */ }
}
$("#logtabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-t]"); if (!b) return;
  logTab = b.dataset.t; try { sessionStorage.setItem("capy.logtab", logTab); } catch (err) { /* private mode */ }
  showLogs();
});
function append(lines) {
  logBuf.push(...lines);
  if (logBuf.length > MAXLOG) logBuf.splice(0, logBuf.length - MAXLOG);
  if (logTab === "all") domAppend(lines);
  else if (logWin) {                   // a live line at the open level joins the window's lines
    const fresh = lines.filter(e => e.lvl === logTab && e.id > logWinMax);
    if (fresh.length) { logWinMax = fresh[fresh.length - 1].id; domAppend(fresh); }
  }
  logCount += lines.length; text($("#log-count"), `${fmt(logCount)} lines`);
}
logEl.addEventListener("scroll", () => { follow = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 12; });
function clearLog(note) {
  logCount = 0; logBuf = []; logWin = null; logWinMax = 0; text($("#log-count"), "");
  const d = document.createElement("div"); d.className = "empty"; d.textContent = note; logEl.replaceChildren(d);
}

/* ---- polling ---- */
let lastLog = 0;
async function pollMetrics() {
  try {
    const m = await get("metrics");
    m.slots = await get("slots").catch(() => []);
    M = m; $("#auth").hidden = true; draw();
  } catch (e) {
    $("#auth").hidden = !e.locked; M = null;
    setState("offline", e.locked ? "Locked" : "Offline");
  }
  setTimeout(pollMetrics, 1000);
}
async function pollLog() {
  try {
    const r = await get(`api/logs?after=${lastLog}`);
    if (r.levels) { logLevels = r.levels; drawLogTabs(); }        // counts that outlive the client and every ring
    if (r.last < lastLog) { lastLog = 0; clearLog("The server restarted."); }
    else if (r.lines.length) { lastLog = r.last; append(r.lines); }
    else if (!logCount && !logEl.firstChild) clearLog("No log lines yet.");
  } catch (e) { /* the metrics poll reports the connection */ }
  setTimeout(pollLog, 1000);
}
$("#auth").onsubmit = e => {
  e.preventDefault(); apiKey = $("#key").value.trim(); sessionStorage.setItem("capy.key", apiKey);
};

$("#host").textContent = location.host;
fetch("health").then(r => r.json()).then(h => { $("#keyed").hidden = !h.api_key; }).catch(() => {});
showLogs();
pollMetrics(); pollLog();
