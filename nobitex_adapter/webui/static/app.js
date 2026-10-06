/* Nobitex Backtest Manager — frontend (no external dependencies) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  markets: [],
  selected: new Set(),
  job: null,
  pollTimer: null,
  logOffset: 0,
  stepState: { 1: "idle", 2: "idle", 3: "idle", 4: "idle", 5: "idle" },
};

// ------------------------------------------------------------------ utils
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(String(msg));
  }
  return r.json();
}
const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

const fmt = (n, d = 2) => (n === null || n === undefined || isNaN(n)) ? "—" :
  Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: 0 });
const fmtPct = (n, d = 2) => (n === null || n === undefined || isNaN(n)) ? "—" :
  Number(n).toLocaleString("en-US", { maximumFractionDigits: d }) + "%";
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------------------ init
async function init() {
  try {
    const st = await api("/api/status");
    $("#statusBadge").textContent = `${st.app} ${st.version} · Freqtrade ${st.freqtrade}`;
  } catch (e) {
    $("#statusBadge").textContent = "API unreachable";
  }
  loadStrategies();
  loadExchanges();
  loadPresets();
  loadHist();
  loadResultsList();
  loadRuntime();
  bind();
}

// ------------------------------------------------------------------ runtime
async function loadRuntime() {
  try {
    const r = await api("/api/runtime");
    const sel = r.selected || {};
    $("#repoPath").value = sel.repo && sel.repo !== "None" ? sel.repo : "";
    const chip = $("#runtimeChip");
    const meta = $("#runtimeMeta");
    if (sel.ok) {
      chip.textContent = `backtest runtime: Freqtrade ${sel.freqtrade} · ${sel.python_version || ""}`;
      chip.className = "chip ok";
      meta.textContent = `python ${sel.python}  ·  freqtrade ${sel.freqtrade}  ·  ccxt ${sel.ccxt || "?"}  ·  ${sel.freqtrade_module || ""}`;
    } else if (sel.error) {
      chip.textContent = "backtest runtime: ⚠ unavailable";
      chip.className = "chip warn";
      meta.textContent = sel.error;
    } else {
      chip.textContent = "backtest runtime: (repo without .venv — using current interpreter)";
      chip.className = "chip";
      meta.textContent = "No Freqtrade repository with a .venv is selected; backtests run in the GUI host interpreter.";
    }
  } catch (e) {
    $("#runtimeChip").textContent = "runtime: ?";
  }
}
async function applyRepo() {
  const repo = $("#repoPath").value.trim() || null;
  $("#btnApplyRepo").disabled = true;
  try {
    const r = await api("/api/repo", { method: "POST", body: JSON.stringify({ repo }) });
    loadRuntime();
    loadStrategies();   // strategy list follows the selected repo
    loadHist();
  } catch (e) {
    $("#runtimeMeta").textContent = String(e.message || e);
  } finally {
    $("#btnApplyRepo").disabled = false;
  }
}

async function loadStrategies() {
  const d = await api("/api/strategies");
  const sel = $("#strategy");
  sel.innerHTML = "";
  for (const s of d.strategies) {
    const o = document.createElement("option");
    o.value = s.name;
    o.textContent = s.name;
    sel.appendChild(o);
  }
  const x8 = d.strategies.find((s) => s.name === "NostalgiaForInfinityX8");
  if (x8) sel.value = x8.name;
  updateStrategyMeta();
}
function updateStrategyMeta() {
  const d = $("#strategy");
  const opt = d.options[d.selectedIndex];
  $("#strategyMeta").textContent = opt ? opt.dataset.meta || "" : "";
}
async function loadExchanges() {
  const d = await api("/api/exchanges");
  const sel = $("#exchange");
  sel.innerHTML = "";
  for (const e of d.exchanges) {
    const o = document.createElement("option");
    o.value = e.id;
    o.textContent = `${e.name}${e.status === "planned" ? " (planned)" : ""}`;
    o.disabled = e.status !== "ready";
    sel.appendChild(o);
  }
}
async function loadPresets() {
  try {
    const d = await api("/api/presets");
    window.__presets = d.presets;
  } catch (e) {}
}

// ------------------------------------------------------------------ markets
async function discover(showLog = true) {
  const job = await startJob("discover", { quote: "USDT" }, showLog);
  if (job) {
    state.stepState[1] = "run"; renderSteps();
    const r = await waitJob(job.id);
    if (r && r.status === "done" && r.result) {
      state.markets = r.result.markets || [];
      renderPairs();
      $("#discoverMeta").textContent = `${state.markets.length} markets · ${new Date(r.result.fetched_at).toISOString().slice(0, 16)} UTC`;
      setStep(1, "ok", `${state.markets.length} markets`);
    } else {
      setStep(1, "err", r && r.error ? r.error.slice(0, 60) : "failed");
    }
  }
}
function renderPairs() {
  const box = $("#pairList");
  box.innerHTML = "";
  const q = ($("#pairSearch").value || "").trim().toUpperCase();
  const list = state.markets.filter((m) =>
    !q || m.ft_symbol.toUpperCase().includes(q) || m.symbol.toUpperCase().includes(q));
  if (!list.length) box.innerHTML = '<div class="muted">موردی یافت نشد</div>';
  for (const m of list) {
    const div = document.createElement("div");
    div.className = "item" + (m.active ? "" : " closed");
    const checked = state.selected.has(m.ft_symbol);
    div.innerHTML = `<input type="checkbox" ${checked ? "checked" : ""} ${m.active ? "" : "disabled"}>
      <span class="sym">${esc(m.ft_symbol)}</span>
      <span class="vol">P ${fmt(m.price, m.price < 1 ? 6 : 2)} · Vol ${fmt(m.volume_quote / 1e6, 1)}M${m.active ? "" : " · CLOSED"}</span>`;
    div.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) state.selected.add(m.ft_symbol);
      else state.selected.delete(m.ft_symbol);
      syncPairSelection();
    });
    div.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      const cb = div.querySelector("input");
      if (cb.disabled) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change"));
    });
    box.appendChild(div);
  }
  syncPairSelection();
}
function syncPairSelection() {
  $("#pairCount").textContent = state.selected.size;
}

// ------------------------------------------------------------------ jobs
function bind() {
  $("#btnApplyRepo").onclick = applyRepo;
  $("#repoPath").addEventListener("keydown", (e) => { if (e.key === "Enter") applyRepo(); });
  $("#btnRefreshStrat").onclick = () => {
    api("/api/strategies/refresh", { method: "POST" }).then((d) => {
      const sel = $("#strategy"); const v = sel.value;
      sel.innerHTML = "";
      for (const s of d.strategies) {
        const o = document.createElement("option");
        o.value = s.name; o.textContent = s.name;
        o.dataset.meta = `tf ${s.timeframe || "?"}${s.info_timeframes?.length ? " + " + s.info_timeframes.join(",") : ""}`;
        sel.appendChild(o);
      }
      if (d.strategies.some((s) => s.name === v)) sel.value = v;
      else if (d.strategies.some((s) => s.name === "NostalgiaForInfinityX8")) sel.value = "NostalgiaForInfinityX8";
    });
  };
  // set meta on load
  api("/api/strategies").then((d) => {
    const sel = $("#strategy");
    d.strategies.forEach((s) => {
      const o = Array.from(sel.options).find((x) => x.value === s.name);
      if (o) o.dataset.meta = `tf ${s.timeframe || "?"}${s.info_timeframes?.length ? " + " + s.info_timeframes.join(",") : ""}`;
    });
    sel.onchange = updateStrategyMeta;
  });

  $("#btnDiscover").onclick = () => discover();
  $("#pairSearch").oninput = () => renderPairs();
  $("#btnSelAll").onclick = () => {
    state.markets.filter((m) => m.active).forEach((m) => state.selected.add(m.ft_symbol));
    renderPairs();
  };
  $("#btnSelRec").onclick = () => {
    // X8 guidance: liquid USDT spot pairs; exclude closed + wrapped (WBTC)
    state.selected = new Set(
      state.markets
        .filter((m) => m.active && !/^WBTC/.test(m.ft_symbol) && (m.volume_quote || 0) > 0)
        .sort((a, b) => (b.volume_quote || 0) - (a.volume_quote || 0))
        .slice(0, 50)
        .map((m) => m.ft_symbol)
    );
    renderPairs();
  };
  $("#btnSelClear").onclick = () => { state.selected = new Set(); renderPairs(); };

  $$("[data-preset]").forEach((b) => {
    b.onclick = async () => {
      if (!window.__presets) await loadPresets();
      const p = window.__presets && window.__presets[b.dataset.preset];
      if (p) { $("#start").value = p.start; $("#end").value = p.end; }
    };
  });

  $("#act1").onclick = () => discover();
  $("#act2").onclick = () => startDownload();
  $("#act3").onclick = () => startValidate();
  $("#act4").onclick = () => startBacktest();
  $("#act5").onclick = () => openResults();
  $("#btnCancel").onclick = async () => {
    if (state.job) await post(`/api/jobs/${state.job.id}/cancel`);
  };

  $("#btnResultsRefresh").onclick = () => openResults();
  $("#btnHistRefresh").onclick = loadHist;
  $("#resultsSel").onchange = async () => {
    const name = $("#resultsSel").value;
    if (!name) return;
    try {
      const d = await api(`/api/results/${encodeURIComponent(name)}`);
      renderResults(d);
    } catch (e) { alert(String(e.message)); }
  };
}

function paramsBase() {
  return {
    strategy: $("#strategy").value,
    pairs: Array.from(state.selected).join(","),
    timeframe: $("#timeframe").value,
    timeframes: requiredTfs($("#timeframe").value),
    start: $("#start").value,
    end: $("#end").value,
    capital: parseFloat($("#capital").value) || 10000,
    max_open_trades: parseInt($("#maxOpen").value) || 8,
    stake: $("#stakeAmount").value,
    stake_currency: $("#stakeCur").value,
    fee: $("#fee").value,
  };
}
// X8 informative timeframes (auto-detected server-side for backtests;
// downloads need the explicit list)
function requiredTfs(base) {
  const map = { "5m": ["5m", "15m", "1h", "4h", "1d"], "15m": ["15m", "1h", "4h", "1d"],
                "1h": ["1h", "4h", "1d"], "4h": ["4h", "1d"], "1d": ["1d"] };
  return map[base] || [base];
}

async function startJob(kind, params, showLog = true) {
  if (state.job && state.job.status === "running") {
    alert("یک شغل در حال اجراست / a job is already running");
    return null;
  }
  const d = await post("/api/jobs", { kind, params });
  state.job = { id: d.job_id, kind, status: "running" };
  state.logOffset = 0;
  $("#logBox").classList.remove("hidden");
  $("#logBox").innerHTML = "";
  $("#progressWrap").classList.remove("hidden");
  setProgressBar(0, `${kind} started…`);
  setStep(kind === "discover" ? 1 : kind === "download" ? 2 : kind === "validate" ? 3 : 4, "run", "در حال اجرا…");
  startPolling();
  return state.job;
}

function startDownload() {
  const p = paramsBase();
  if (!p.pairs) return alert("اول جفت‌ارز انتخاب کنید / select at least one pair first");
  if (!p.start || !p.end) return alert("محدوده تاریخ را مشخص کنید / set the date range");
  const extra = advancedJson();
  if (extra) Object.assign(p, extra);
  return startJob("download", p);
}
function startValidate() {
  const p = paramsBase();
  if (!p.pairs) return alert("اول جفت‌ارز انتخاب کنید / select at least one pair first");
  return startJob("validate", p);
}
function startBacktest() {
  const p = paramsBase();
  if (!p.pairs) return alert("اول جفت‌ارز انتخاب کنید / select at least one pair first");
  if (!p.start || !p.end) return alert("محدوده تاریخ را مشخص کنید / set the date range");
  const extra = advancedJson();
  if (extra) Object.assign(p, extra);
  return startJob("backtest", p);
}
function advancedJson() {
  const t = ($("#advJson").value || "").trim();
  if (!t) return null;
  try { return JSON.parse(t); } catch (e) { alert("JSON نامعتبر / invalid JSON: " + e.message); return null; }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(pollJob, 1500);
  pollJob();
}
function stopPolling() { if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; } }

async function pollJob() {
  if (!state.job) return stopPolling();
  const d = await api(`/api/jobs/${state.job.id}`);
  state.job = d;
  // log tail
  const lg = await api(`/api/jobs/${state.job.id}/log?after=${state.logOffset}`);
  if (lg.lines.length) {
    const box = $("#logBox");
    for (const line of lg.lines) {
      const div = document.createElement("div");
      if (/\[error\]/.test(line)) div.className = "err";
      else if (/\[ok\]|\[done\]/.test(line)) div.className = "ok";
      div.textContent = line;
      box.appendChild(div);
    }
    box.scrollTop = box.scrollHeight;
    state.logOffset = lg.total;
  }
  // progress
  const pr = d.progress || {};
  if (pr.pair && pr.chunks_total) {
    const pct = Math.round(((pr.chunk || 0) / pr.chunks_total) * 100);
    setProgressBar(pct, `${pr.pair} ${pr.timeframe} — chunk ${pr.chunk}/${pr.chunks_total} — ${fmt(pr.candles || 0)} candles`);
    $("#progressMeta").textContent = `job ${d.id} · elapsed ${d.elapsed_s}s`;
  } else if (pr.stage) {
    setProgressBar(50, pr.stage);
  }
  if (d.status !== "running") finishJob(d);
}

async function waitJob(id, timeoutMs = 120000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const d = await api(`/api/jobs/${id}`);
    if (d.status !== "running") return d;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return await api(`/api/jobs/${id}`);
}

function finishJob(d) {
  stopPolling();
  setProgressBar(d.status === "done" ? 100 : 0, d.status);
  $("#progressMeta").textContent = `job ${d.id} · ${d.status} · elapsed ${d.elapsed_s}s`;
  const kind = d.kind;
  if (d.status === "done") {
    if (kind === "download") {
      setStep(2, "ok", `${d.result.total_rows.toLocaleString()} rows`);
    } else if (kind === "validate") {
      setStep(3, d.result.all_ok ? "ok" : "err", d.result.all_ok ? "PASS" : "problems found");
    } else if (kind === "backtest") {
      setStep(4, "ok", `${d.result.elapsed}s`);
      setStep(5, "idle", "آماده / ready");
      openResults();
    }
  } else {
    const step = kind === "discover" ? 1 : kind === "download" ? 2 : kind === "validate" ? 3 : 4;
    setStep(step, "err", (d.error || d.status).slice(0, 70));
  }
  state.job = d;
}

function setStep(n, st, text) {
  state.stepState[n] = st;
  renderSteps();
  const el = document.querySelector(`.step[data-step="${n}"] .st`);
  if (el && text) el.textContent = text;
}
function renderSteps() {
  $$(".step").forEach((el) => {
    const n = el.dataset.step;
    el.classList.toggle("active", state.stepState[n] === "run");
    const st = el.querySelector(".st");
    if (st) st.className = "st " + (state.stepState[n] || "idle");
  });
}
function setProgressBar(pct, label) {
  $("#progressBar").style.width = Math.max(0, Math.min(100, pct)) + "%";
  $("#progressLabel").textContent = label || "";
}

// ------------------------------------------------------------------ history
async function loadHist() {
  const d = await api("/api/data-history");
  const tbl = $("#histTbl");
  if (!d.items.length) { tbl.innerHTML = '<tr><td class="muted">هنوز داده‌ای دانلود نشده / no data yet</td></tr>'; return; }
  let html = `<tr><th>Date</th><th>Exchange</th><th>Pair</th><th>TF</th><th>Range</th><th>Chunks</th><th>Status</th></tr>`;
  for (const it of d.items) {
    html += `<tr>
      <td>${esc(it.updated)}</td><td>nobitex</td><td>${esc(it.pair)}</td><td>${esc(it.timeframe)}</td>
      <td>${esc(it.range_start)} → ${esc(it.range_end)}</td>
      <td>${it.chunks_complete}+${it.chunks_empty}∅</td>
      <td class="pos">${it.chunks_complete ? "READY" : "PARTIAL"}</td></tr>`;
  }
  tbl.innerHTML = html;
}

// ------------------------------------------------------------------ results
async function loadResultsList() {
  const d = await api("/api/results/list");
  const sel = $("#resultsSel");
  sel.innerHTML = '<option value="">— انتخاب نتایج / pick a result —</option>';
  for (const r of d.results) {
    const o = document.createElement("option");
    o.value = r.file;
    o.textContent = r.timestamp.replace("T", " ").replace(/_/g, " ");
    sel.appendChild(o);
  }
}
async function openResults() {
  try {
    const d = await api("/api/results/latest");
    $("#resultsPanel").classList.remove("hidden");
    $("#resultsMeta").textContent = `${d.strategy} · ${d.backtest_start} → ${d.backtest_end} · ${d.pairlist.length} pairs`;
    renderResults(d);
    setStep(5, "ok", "آماده / ready");
    loadResultsList();
  } catch (e) {
    alert("نتیجه‌ای وجود ندارد / no results yet: " + e.message);
  }
}

function renderResults(d) {
  const c = d.cards;
  const cards = [
    ["Total Profit", `${fmt(c.total_profit_abs)} USDT`, cls(c.total_profit_abs)],
    ["Return", fmtPct(c.return_pct), cls(c.return_pct)],
    ["Max Drawdown", fmtPct(c.max_drawdown_pct), "neg"],
    ["Trades", fmt(c.trades, 0), ""],
    ["Win Rate", fmtPct(c.win_rate_pct), ""],
    ["Profit Factor", fmt(c.profit_factor), cls(c.profit_factor - 1)],
    ["Sharpe", fmt(c.sharpe), ""],
    ["Sortino", fmt(c.sortino), ""],
    ["CAGR", fmtPct(c.cagr_pct), cls(c.cagr_pct)],
  ];
  $("#cards").innerHTML = cards.map(([k, v, cl]) =>
    `<div class="card"><div class="k">${k}</div><div class="v ${cl}">${v}</div></div>`).join("");

  drawLineChart($("#equityChart"), d.equity_curve, { color: "#3ba7ff", label: "Equity (USDT)" });
  drawLineChart($("#ddChart"), d.drawdown, { color: "#e35d6a", fill: true, label: "Drawdown %" });

  // per pair
  renderTable($("#pairTbl"),
    [["Pair", "pair"], ["Trades", "trades"], ["Profit %", "profit_pct"], ["Profit $", "profit_abs"],
     ["Winrate", "win_rate_pct"], ["Avg Trade %", "avg_trade_pct"], ["Max DD", "max_drawdown_pct"]],
    d.per_pair, { numeric: ["trades", "profit_pct", "profit_abs", "win_rate_pct", "avg_trade_pct", "max_drawdown_pct"] });

  // exit reasons
  renderTable($("#exitTbl"),
    [["Exit reason", "reason"], ["Trades", "trades"], ["Profit $", "profit_abs"], ["Profit %", "profit_pct"]],
    d.exit_reasons.filter((r) => r.reason !== "TOTAL"),
    { numeric: ["trades", "profit_abs", "profit_pct"] });

  // monthly
  renderTable($("#monthTbl"),
    [["Month", "period"], ["Trades", "trades"], ["Profit $", "profit_abs"], ["Profit Factor", "profit_factor"]],
    d.monthly, { numeric: ["trades", "profit_abs", "profit_factor"] });

  // buy & hold
  const bh = d.buy_hold || {};
  $("#bhCards").innerHTML = `
    <div class="grid cols-3">
      <div class="card"><div class="k">Strategy</div><div class="v ${cls(bh.strategy_return_pct)}">${fmtPct(bh.strategy_return_pct)}</div></div>
      <div class="card"><div class="k">Market (price move)</div><div class="v ${cls(bh.market_change_pct)}">${fmtPct(bh.market_change_pct)}</div></div>
      <div class="card"><div class="k">Buy &amp; Hold (equal weight)</div><div class="v ${cls(bh.equal_weight_buy_hold_pct)}">${fmtPct(bh.equal_weight_buy_hold_pct)}</div></div>
    </div>
    <div class="muted small ltr" style="margin-top:8px">${Object.entries(bh.per_pair || {})
      .map(([p, v]) => `${p}: <span class="${cls(v)}">${fmtPct(v, 1)}</span>`).join(" · ")}</div>`;

  // trades
  renderTable($("#tradeTbl"),
    [["Pair", "pair"], ["Open", "open_date"], ["Close", "close_date"], ["Profit %", "profit_pct"],
     ["Profit $", "profit_abs"], ["Exit", "exit_reason"]],
    d.trades_sample || [], { numeric: ["profit_pct", "profit_abs"] });
}

function renderTable(el, cols, rows, opt = {}) {
  let sortKey = null, sortDir = -1;
  function draw() {
    const data = rows.slice();
    if (sortKey) data.sort((a, b) => {
      const va = a[sortKey], vb = b[sortKey];
      if (va === vb) return 0;
      const r = (typeof va === "number" || typeof vb === "number")
        ? (va ?? -1e18) - (vb ?? -1e18) : String(va).localeCompare(String(vb));
      return r * sortDir;
    });
    let html = "<tr>" + cols.map(([label, key]) =>
      `<th data-k="${key}">${label}${sortKey === key ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("") + "</tr>";
    for (const row of data) {
      html += "<tr>" + cols.map(([label, key]) => {
        let v = row[key];
        if (opt.numeric && opt.numeric.includes(key) && typeof v === "number") {
          const isPct = key.includes("pct");
          return `<td class="${cls(v)}">${isPct ? fmtPct(v) : fmt(v, key.includes("abs") ? 2 : 2)}</td>`;
        }
        if (typeof v === "string" && v.length > 28 && key !== "reason") v = v.slice(0, 28) + "…";
        return `<td>${esc(v ?? "")}</td>`;
      }).join("") + "</tr>";
    }
    el.innerHTML = html;
    el.querySelectorAll("th[data-k]").forEach((th) => {
      th.onclick = () => {
        const k = th.dataset.k;
        if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = -1; }
        draw();
      };
    });
  }
  draw();
}

// ------------------------------------------------------------------ charts
function drawLineChart(container, points, { color = "#3ba7ff", fill = false, label = "" }) {
  const tip = container.querySelector(".tooltip");
  // clear previous svg
  const old = container.querySelector("svg");
  if (old) old.remove();
  if (!points || !points.length) {
    container.insertAdjacentHTML("beforeend", '<div class="muted">no data</div>');
    return;
  }
  const W = container.clientWidth || 800, H = 260;
  const P = { l: 58, r: 14, t: 12, b: 26 };
  const xs = points.map((p) => p.t);
  const ys = points.map((p) => p.v);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  if (ymax === ymin) { ymax += 1; ymin -= 1; }
  const pad = (ymax - ymin) * 0.06;
  ymin -= pad; ymax += pad;
  const X = (t) => P.l + ((t - xmin) / (xmax - xmin || 1)) * (W - P.l - P.r);
  const Y = (v) => H - P.b - ((v - ymin) / (ymax - ymin)) * (H - P.t - P.b);

  let path = "";
  points.forEach((p, i) => { path += (i ? "L" : "M") + X(p.t).toFixed(1) + "," + Y(p.v).toFixed(1); });
  let area = "";
  if (fill) {
    area = `M${X(xmin).toFixed(1)},${Y(0).toFixed(1)}` +
      points.map((p) => `L${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join("") +
      `L${X(xmax).toFixed(1)},${Y(0).toFixed(1)}Z`;
  }
  // grid: 4 y lines + 4 x labels
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = ymin + ((ymax - ymin) * i) / 4;
    const y = Y(v);
    grid += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="#263245" stroke-width="1"/>
             <text x="${P.l - 6}" y="${y + 4}" fill="#8294ac" font-size="10" text-anchor="end">${fmt(v, v >= 1000 ? 0 : 2)}</text>`;
  }
  for (let i = 0; i <= 4; i++) {
    const t = xmin + ((xmax - xmin) * i) / 4;
    const x = X(t);
    const d = new Date(t);
    const lbl = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
    grid += `<text x="${x}" y="${H - 8}" fill="#8294ac" font-size="10" text-anchor="middle">${lbl}</text>`;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `
    ${grid}
    ${fill ? `<path d="${area}" fill="${color}" opacity="0.15"/>` : ""}
    <path d="${path}" fill="none" stroke="${color}" stroke-width="1.8"/>
    <line id="hline" x1="0" y1="0" x2="0" y2="0" stroke="#8294ac" stroke-width="1" stroke-dasharray="3,3" visibility="hidden"/>
    <circle id="hdot" r="3.5" fill="${color}" visibility="hidden"/>
    <rect x="${P.l}" y="${P.t}" width="${W - P.l - P.r}" height="${H - P.t - P.b}" fill="transparent" id="hit"/>`;
  container.insertBefore(svg, tip);

  const hit = svg.querySelector("#hit");
  const hline = svg.querySelector("#hline");
  const hdot = svg.querySelector("#hdot");
  function move(ev) {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const t = xmin + ((px - P.l) / (W - P.l - P.r)) * (xmax - xmin);
    // binary search nearest
    let lo = 0, hi = points.length - 1;
    while (hi - lo > 1) { const mid = (hi + lo) >> 1; if (points[mid].t < t) lo = mid; else hi = mid; }
    const p = Math.abs(points[lo].t - t) < Math.abs(points[hi].t - t) ? points[lo] : points[hi];
    const cx = X(p.t), cy = Y(p.v);
    hline.setAttribute("x1", cx); hline.setAttribute("x2", cx);
    hline.setAttribute("y1", P.t); hline.setAttribute("y2", H - P.b);
    hline.setAttribute("visibility", "visible");
    hdot.setAttribute("cx", cx); hdot.setAttribute("cy", cy); hdot.setAttribute("visibility", "visible");
    const d = new Date(p.t);
    tip.innerHTML = `${label}<br>${d.toISOString().slice(0, 16)} UTC<br><b>${fmt(p.v)}</b>`;
    tip.style.display = "block";
    const left = Math.min(container.clientWidth - 150, Math.max(0, (cx / W) * container.clientWidth + 12));
    tip.style.left = left + "px";
    tip.style.top = "8px";
  }
  hit.addEventListener("mousemove", move);
  hit.addEventListener("mouseleave", () => {
    tip.style.display = "none";
    hline.setAttribute("visibility", "hidden");
    hdot.setAttribute("visibility", "hidden");
  });
}

init();
