"use strict";

// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, sign = false) => {
  if (v == null || isNaN(v)) return "—";
  const s = Math.abs(v).toLocaleString(undefined, { style: "currency", currency: "USD" });
  if (v < 0) return "−" + s;
  return (sign && v > 0 ? "+" : "") + s;
};
const pnlCls = (v) => (v > 0.004 ? "pos" : v < -0.004 ? "neg" : "");
const pnl = (v) => `<span class="${pnlCls(v)}">${money(v, true)}</span>`;
const pct = (v, sign = true) => (v == null ? "—" : `${sign && v > 0 ? "+" : ""}${v.toFixed(2)}%`);
const cents = (p) => {
  if (p == null) return "—";
  const c = p * 100;
  return (Number.isInteger(Math.round(c * 10) / 10) ? c.toFixed(0) : c.toFixed(1)) + "¢";
};
const prob = (p) => (p == null ? "—" : `${(p * 100).toFixed(p < 0.01 || p > 0.99 ? 1 : 0)}%`);
const ago = (iso) => {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—");
const partyChip = (p) => (p ? `<span class="chip ${esc(p)}">${{ D: "DEM", R: "GOP", I: "IND" }[p] || esc(p)}</span>` : "");
const raceLabel = (r) => {
  if (r.chamber === "Control") return `${r.district} control`;
  if (r.chamber === "House") return `${r.state}-${r.district}`;
  return `${r.state ?? ""} ${r.chamber ?? ""}${r.district ? ` (${r.district})` : ""}`.trim();
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

// ---------- state ----------
const S = {
  portfolios: [],
  pid: null,
  summary: null,
  open: [],
  closed: [],
  races: [],
  allEvents: [],
  allCursor: null,
  source: "midterms",
  compare: null,
  sort: {},
  bet: null,
};
const savedPid = () => { try { return Number(localStorage.getItem("gambles.pid")) || null; } catch { return null; } };
const savePid = (id) => { try { localStorage.setItem("gambles.pid", id); } catch {} };

// ---------- sortable tables ----------
function table(id, cols, rows, { foot = null, empty = "Nothing here yet." } = {}) {
  const el = typeof id === "string" ? $(id) : id;
  if (!rows.length) { el.innerHTML = `<div class="empty">${empty}</div>`; return; }
  const key = el.id || "t";
  const st = S.sort[key];
  if (st) {
    const col = cols.find((c) => c.key === st.key);
    const val = col?.sortVal || ((r) => r[st.key]);
    rows = [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null) return 1; if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * st.dir;
    });
  }
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr>${cols.map((c) => `<th data-key="${c.key}" class="${c.num ? "num" : ""} ${c.nosort ? "nosort" : ""}">${c.label}${st?.key === c.key ? `<span class="arrow">${st.dir > 0 ? "▲" : "▼"}</span>` : ""}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.num ? "num" : ""} ${c.cls || ""}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join("")}</tr>`).join("")}</tbody>
    ${foot ? `<tfoot><tr>${foot.map((f, i) => `<td class="${cols[i]?.num ? "num" : ""}">${f ?? ""}</td>`).join("")}</tr></tfoot>` : ""}
  </table></div>`;
  $$("th", el).forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.key;
    if (cols.find((c) => c.key === k)?.nosort) return;
    S.sort[key] = { key: k, dir: S.sort[key]?.key === k ? -S.sort[key].dir : -1 };
    renderAll();
  }));
}

// ---------- equity chart (hand-rolled SVG) ----------
function lineChart(el, series, { height = 200 } = {}) {
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) { el.innerHTML = `<div class="empty">The chart fills in as prices refresh. A point is recorded every few minutes.</div>`; return; }
  const w = el.clientWidth || 800, h = height, padL = 70, padR = 12, padT = 10, padB = 22;
  const xs = all.map((p) => p.t), ys = all.map((p) => p.v);
  let x0 = Math.min(...xs), x1 = Math.max(...xs); if (x1 === x0) x1 = x0 + 1;
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const padY = (y1 - y0) * 0.1 || Math.max(1, Math.abs(y1) * 0.002); y0 -= padY; y1 += padY;
  const X = (t) => padL + ((t - x0) / (x1 - x0)) * (w - padL - padR);
  const Y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * (h - padT - padB);
  const ticks = 4, grid = [];
  for (let i = 0; i <= ticks; i++) {
    const v = y0 + ((y1 - y0) * i) / ticks;
    grid.push(`<line x1="${padL}" x2="${w - padR}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${padL - 8}" y="${Y(v) + 4}" text-anchor="end">${money(v).replace(/\.\d\d$/, "")}</text>`);
  }
  const xl = [x0, (x0 + x1) / 2, x1].map((t, i) => `<text x="${X(t)}" y="${h - 4}" text-anchor="${["start", "middle", "end"][i]}">${fmtDate(new Date(t).toISOString())}</text>`);
  const paths = series.map((s) => {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`;
  });
  const base = series.find((s) => s.baseline != null)?.baseline;
  const baseLine = base != null && base >= y0 && base <= y1
    ? `<line x1="${padL}" x2="${w - padR}" y1="${Y(base)}" y2="${Y(base)}" stroke="var(--muted)" stroke-dasharray="4 4" opacity=".6"/>` : "";
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <g class="grid axis">${grid.join("")}</g><g class="axis">${xl.join("")}</g>${baseLine}${paths.join("")}
    <line class="hover" y1="${padT}" y2="${h - padB}" stroke="var(--muted)" opacity="0"/>
    <rect x="${padL}" y="0" width="${w - padL - padR}" height="${h}" fill="transparent"/></svg><div class="tip" hidden></div>`;
  const svg = $("svg", el), tip = $(".tip", el), hl = $(".hover", el);
  svg.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const t = x0 + ((e.clientX - r.left) * (w / r.width) - padL) / (w - padL - padR) * (x1 - x0);
    const lines = series.map((s) => {
      const p = s.points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
      return { s, p };
    });
    const px = X(lines[0].p.t);
    hl.setAttribute("x1", px); hl.setAttribute("x2", px); hl.setAttribute("opacity", ".5");
    tip.hidden = false;
    tip.innerHTML = `<div class="muted">${fmtDate(new Date(lines[0].p.t).toISOString())}</div>` +
      lines.map(({ s, p }) => `<div><span style="color:${s.color}">●</span> ${esc(s.name)}: <b>${money(p.v)}</b></div>`).join("");
    const left = (px / w) * r.width;
    tip.style.left = Math.min(left + 12, r.width - tip.offsetWidth - 4) + "px";
    tip.style.top = "8px";
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; hl.setAttribute("opacity", "0"); });
}

// ---------- data loading ----------
async function loadPortfolios() {
  S.portfolios = await api("/api/portfolios");
  const sel = $("#portfolioSelect");
  if (!S.portfolios.length) { sel.innerHTML = `<option>(none yet)</option>`; S.pid = null; return; }
  if (!S.pid || !S.portfolios.some((p) => p.id === S.pid)) {
    const saved = savedPid();
    S.pid = S.portfolios.some((p) => p.id === saved) ? saved : S.portfolios[0].id;
  }
  sel.innerHTML = S.portfolios.map((p) => `<option value="${p.id}" ${p.id === S.pid ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  $("#betPortfolio").innerHTML = S.portfolios.map((p) => `<option value="${p.id}">${esc(p.name)} (${money(p.cash)} cash)</option>`).join("");
}

async function loadPortfolio() {
  if (!S.pid) { S.summary = null; S.open = []; S.closed = []; return; }
  const [summary, open, closed, snaps] = await Promise.all([
    api(`/api/portfolios/${S.pid}/summary`),
    api(`/api/portfolios/${S.pid}/bets?status=open`),
    api(`/api/portfolios/${S.pid}/bets?status=closed`),
    api(`/api/portfolios/${S.pid}/snapshots`),
  ]);
  Object.assign(S, { summary, open, closed, snaps });
}

async function loadMidterms() {
  const d = await api("/api/midterms");
  S.races = d.races;
  S.catalog = d.catalog;
}

async function loadCompare() { S.compare = await api("/api/compare"); }

async function loadStatus() {
  const st = await api("/api/status");
  const pill = $("#statusPill");
  const parts = [];
  if (st.catalog_refreshing) parts.push("scanning Kalshi…");
  parts.push(`prices ${ago(st.last_price_refresh)}`);
  if (st.authenticated) parts.push("API key ✓");
  pill.textContent = parts.join(" · ");
  pill.className = "pill " + (st.last_error ? "warn" : "live");
  pill.title = st.last_error || `Live Kalshi data. Auto-refresh every ${st.price_refresh_seconds}s. Fees ${st.simulate_fees ? "simulated" : "off"}.`;
  return st;
}

// ---------- renderers ----------
function renderSummary() {
  const s = S.summary, el = $("#summary");
  if (!s) { el.innerHTML = `<div class="empty">Loading portfolios… On first run the app scans Kalshi for midterm markets, which takes about a minute.</div>`; return; }
  const c = s.counts;
  const cards = [
    ["Equity", money(s.equity), `started at ${money(s.starting_balance)}`],
    ["Total P/L", pnl(s.total_pnl), `<span class="${pnlCls(s.total_pnl)}">${pct(s.total_return_pct)}</span> return`],
    ["Cash", money(s.cash), "available to bet"],
    ["Open positions", money(s.positions_value), `${s.open_count} bets · cost ${money(s.open_cost_basis)}`],
    ["Unrealized P/L", pnl(s.unrealized_pnl), `pays ${money(s.max_payout_if_all_win)} if all win`],
    ["Realized P/L", pnl(s.realized_pnl), `${c.won}W · ${c.lost}L · ${c.sold} sold`],
    ["Win rate", s.win_rate_pct == null ? "—" : `${s.win_rate_pct}%`, `of ${c.won + c.lost} settled bets`],
    ["Fees paid", money(s.fees_paid), "simulated Kalshi fees"],
  ];
  el.innerHTML = cards.map(([l, v, sub]) => `<div class="card"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`).join("");
  $("#activeCount").textContent = s.open_count || "";
}

function renderChart() {
  const pts = (S.snaps || []).map((p) => ({ t: new Date(p.ts).getTime(), v: p.equity }));
  $("#chartNote").textContent = S.summary ? `${S.summary.portfolio.name} · dashed line = starting balance` : "";
  lineChart($("#equityChart"), [{ name: "Equity", color: "var(--accent)", points: pts, baseline: S.summary?.starting_balance }]);
}

function ownedMap() {
  const m = {};
  for (const b of S.open) (m[b.market_ticker] ||= []).push(b);
  return m;
}

function marketRow(m, owned) {
  const yesAsk = m.yes_ask > 0 && m.yes_ask < 1 ? m.yes_ask : null;
  const noAsk = m.no_ask > 0 && m.no_ask < 1 ? m.no_ask : null;
  const p = m.last_price ?? yesAsk;
  const mine = owned[m.ticker];
  return `<div class="mkt">
    <div class="who">${partyChip(m.party)}<span class="name" title="${esc(m.yes_sub_title || m.title)}">${esc(m.yes_sub_title || m.title || m.ticker)}</span>
      ${mine ? `<span class="owned">● ${mine.reduce((a, b) => a + b.contracts, 0)} ${esc(mine[0].side.toUpperCase())}</span>` : ""}</div>
    <div class="actions"><span class="prob">${prob(p)}</span>
      <button class="btn sm yes" data-bet="${esc(m.ticker)}" data-side="yes" ${yesAsk ? "" : "disabled"}>Yes ${cents(yesAsk)}</button>
      <button class="btn sm no" data-bet="${esc(m.ticker)}" data-side="no" ${noAsk ? "" : "disabled"}>No ${cents(noAsk)}</button></div>
    <div class="bar ${esc(m.party || "")}"><i style="width:${Math.max(0, Math.min(100, (p || 0) * 100))}%"></i></div>
    <div class="quote-line">Yes ${cents(m.yes_bid)} bid / ${cents(m.yes_ask)} ask · vol ${m.volume != null ? Math.round(m.volume).toLocaleString() : "—"} · <span class="mono">${esc(m.ticker)}</span></div>
  </div>`;
}

function renderMarkets() {
  const q = $("#marketSearch").value.trim().toLowerCase();
  const owned = ownedMap();
  const list = $("#marketList");
  $("#chamberFilter").hidden = S.source !== "midterms";
  $("#categoryFilter").hidden = S.source !== "all";
  $("#loadMore").hidden = S.source !== "all" || !S.allCursor;

  if (S.source === "midterms") {
    const ch = $("#chamberFilter").value;
    const cat = S.catalog;
    $("#catalogNote").textContent = cat
      ? `${S.races.length} races · ${cat.markets} markets · scanned ${ago(cat.refreshed_at)}. Probabilities are the last traded price.`
      : "Scanning Kalshi for 2026 midterm markets… this takes about a minute on first run.";
    const order = { Control: 0, Senate: 1, Governor: 2, House: 3 };
    const races = S.races
      .filter((r) => !ch || r.chamber === ch)
      .filter((r) => !q || [r.title, r.state, raceLabel(r), ...r.markets.map((m) => m.yes_sub_title + " " + m.ticker)].join(" ").toLowerCase().includes(q))
      .sort((a, b) => order[a.chamber] - order[b.chamber] || (a.state || "").localeCompare(b.state || "") || (parseInt(a.district) || 0) - (parseInt(b.district) || 0));
    if (!races.length) { list.innerHTML = `<div class="empty">${S.races.length ? "No races match." : "No midterm markets cached yet."}</div>`; return; }
    list.innerHTML = `<div class="race-grid">${races.map((r) => {
      const ms = [...r.markets].sort((a, b) => (b.last_price ?? 0) - (a.last_price ?? 0));
      const settled = ms.find((m) => m.result === "yes");
      return `<div class="race">
        <div class="race-head"><div><div class="t">${esc(raceLabel(r))}</div><div class="meta">${esc(r.title)}</div></div>
          <span class="chip">${esc(r.chamber)}</span></div>
        ${settled ? `<div class="chip won">Settled: ${esc(settled.yes_sub_title)}</div>` : ""}
        ${ms.map((m) => marketRow(m, owned)).join("")}
      </div>`;
    }).join("")}</div>`;
  } else {
    $("#catalogNote").textContent = "Live list of every open Kalshi event, newest first. Load more to page through.";
    const catSel = $("#categoryFilter"), cur = catSel.value;
    const cats = [...new Set(S.allEvents.map((e) => e.category).filter(Boolean))].sort();
    catSel.innerHTML = `<option value="">All categories</option>` + cats.map((c) => `<option ${c === cur ? "selected" : ""}>${esc(c)}</option>`).join("");
    const evs = S.allEvents
      .filter((e) => !catSel.value || e.category === catSel.value)
      .filter((e) => !q || [e.title, e.sub_title, e.event_ticker, ...e.markets.map((m) => m.yes_sub_title)].join(" ").toLowerCase().includes(q));
    if (!evs.length) { list.innerHTML = `<div class="empty">${S.allEvents.length ? "No events match. Try loading more." : "Loading…"}</div>`; return; }
    list.innerHTML = `<div class="race-grid">${evs.map((e) => `<div class="race">
        <div class="race-head"><div><div class="t">${esc(e.title)}</div><div class="meta">${esc(e.sub_title || "")}</div></div>
          <span class="chip">${esc(e.category || "")}</span></div>
        ${e.markets.slice(0, 8).map((m) => marketRow(m, owned)).join("")}
        ${e.markets.length > 8 ? `<div class="muted small">+${e.markets.length - 8} more markets. Use ticker lookup.</div>` : ""}
      </div>`).join("")}</div>`;
  }
}

const betTitle = (b) => `<div>${partyChip(b.party)} <b>${esc(b.yes_sub_title || b.market_title || b.market_ticker)}</b></div>
  <div class="ev">${esc(b.chamber ? raceLabel(b) + " · " : "")}${esc(b.event_title || "")}</div>`;

function renderActive() {
  const q = $("#activeSearch").value.trim().toLowerCase();
  const rows = S.open.filter((b) => !q || [b.yes_sub_title, b.event_title, b.market_ticker, b.state].join(" ").toLowerCase().includes(q));
  const sum = (k) => rows.reduce((a, b) => a + (b[k] || 0), 0);
  table("#activeTable", [
    { key: "yes_sub_title", label: "Bet", cls: "title-cell", render: betTitle },
    { key: "side", label: "Side", render: (b) => `<span class="chip ${b.side}">${b.side.toUpperCase()}</span>` },
    { key: "contracts", label: "Contracts", num: true, render: (b) => b.contracts.toLocaleString() },
    { key: "entry_price", label: "Entry", num: true, render: (b) => cents(b.entry_price) },
    { key: "mark_price", label: "Now", num: true, render: (b) => cents(b.mark_price) },
    { key: "cost", label: "Cost + fee", num: true, sortVal: (b) => b.cost + b.entry_fee, render: (b) => money(b.cost + b.entry_fee) },
    { key: "market_value", label: "Value", num: true, render: (b) => money(b.market_value) },
    { key: "unrealized_pnl", label: "Unrealized", num: true, render: (b) => pnl(b.unrealized_pnl) },
    { key: "max_profit", label: "If it wins", num: true, render: (b) => pnl(b.max_profit) },
    { key: "placed_at", label: "Placed", render: (b) => `<span class="small muted">${fmtDate(b.placed_at)}</span>` },
    { key: "_sell", label: "", nosort: true, render: (b) => `<button class="btn sm ghost" data-sell="${b.id}" ${b.market_status === "active" ? "" : "disabled"}>Sell</button>` },
  ], rows, {
    empty: "No active bets. Pick something from Available Bets.",
    foot: rows.length ? [`${rows.length} positions`, "", "", "", "", money(sum("cost") + sum("entry_fee")), money(sum("market_value")), pnl(sum("unrealized_pnl")), pnl(sum("max_profit")), "", ""] : null,
  });
}

function groupTable(title, groups, labeler = (g) => esc(g)) {
  if (!groups?.length) return "";
  return `<div class="panel"><h2>${title}</h2><table class="mini"><thead><tr><th class="nosort"></th><th class="nosort num">Bets</th><th class="nosort num">W-L</th><th class="nosort num">Staked</th><th class="nosort num">Realized</th><th class="nosort num">Unrealized</th></tr></thead>
    <tbody>${groups.map((g) => `<tr><td>${labeler(g.group)}</td><td class="num">${g.bets}</td><td class="num">${g.won}-${g.lost}${g.sold ? ` (${g.sold}s)` : ""}</td>
    <td class="num">${money(g.staked)}</td><td class="num">${pnl(g.pnl)}</td><td class="num">${pnl(g.unrealized)}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderHistory() {
  const s = S.summary;
  if (!s) return;
  const c = s.counts, tot = c.won + c.lost + c.sold || 1;
  const big = (b) => b ? `${esc(b.yes_sub_title || b.market_ticker)} <span class="muted small">(${esc(b.side.toUpperCase())})</span>` : "—";
  $("#historyBreakdown").innerHTML = `<div class="breakdown">
    <div class="panel"><h2>Wins &amp; losses</h2>
      <div class="wl-bar"><div class="w" style="width:${(c.won / tot) * 100}%"></div><div class="s" style="width:${(c.sold / tot) * 100}%"></div><div class="l" style="width:${(c.lost / tot) * 100}%"></div></div>
      <div class="kv">
        <div>Won</div><div class="num pos">${c.won}</div>
        <div>Lost</div><div class="num neg">${c.lost}</div>
        <div>Sold early</div><div class="num">${c.sold} <span class="muted small">(${s.sold_profitably} at a profit)</span></div>
        <div>Win rate (settled)</div><div class="num">${s.win_rate_pct == null ? "—" : s.win_rate_pct + "%"}</div>
        <div>Gross winnings</div><div class="num">${pnl(s.gross_winnings)}</div>
        <div>Gross losses</div><div class="num">${pnl(s.gross_losses)}</div>
        <div>Net realized</div><div class="num">${pnl(s.realized_pnl)}</div>
        <div>Fees paid</div><div class="num">${money(s.fees_paid)}</div>
        <div>Biggest win</div><div class="num">${big(s.biggest_win && s.biggest_win.pnl > 0 ? s.biggest_win : null)} ${s.biggest_win?.pnl > 0 ? pnl(s.biggest_win.pnl) : ""}</div>
        <div>Biggest loss</div><div class="num">${big(s.biggest_loss && s.biggest_loss.pnl < 0 ? s.biggest_loss : null)} ${s.biggest_loss?.pnl < 0 ? pnl(s.biggest_loss.pnl) : ""}</div>
      </div></div>
    ${groupTable("By race type", s.by_chamber)}
    ${groupTable("By party", s.by_party, (g) => partyChip(g) || esc(g))}
    ${s.by_category.length > 1 ? groupTable("By category", s.by_category) : ""}
  </div>`;

  const q = $("#historySearch").value.trim().toLowerCase();
  const rows = S.closed.filter((b) => !q || [b.yes_sub_title, b.event_title, b.market_ticker, b.status].join(" ").toLowerCase().includes(q));
  table("#historyTable", [
    { key: "yes_sub_title", label: "Bet", cls: "title-cell", render: betTitle },
    { key: "side", label: "Side", render: (b) => `<span class="chip ${b.side}">${b.side.toUpperCase()}</span>` },
    { key: "status", label: "Outcome", render: (b) => `<span class="chip ${b.status}">${b.status.toUpperCase()}</span>` },
    { key: "contracts", label: "Contracts", num: true },
    { key: "entry_price", label: "Entry", num: true, render: (b) => cents(b.entry_price) },
    { key: "exit_price", label: "Exit", num: true, render: (b) => cents(b.exit_price) },
    { key: "cost", label: "Staked", num: true, sortVal: (b) => b.cost + b.entry_fee, render: (b) => money(b.cost + b.entry_fee) },
    { key: "proceeds", label: "Returned", num: true, sortVal: (b) => b.proceeds - b.exit_fee, render: (b) => money(b.proceeds - b.exit_fee) },
    { key: "pnl", label: "P/L", num: true, render: (b) => pnl(b.pnl) },
    { key: "closed_at", label: "Closed", render: (b) => `<span class="small muted">${fmtDate(b.closed_at)}</span>` },
  ], rows, {
    empty: "No closed bets yet. Bets show up here when their market settles or you sell.",
    foot: rows.length ? [`${rows.length} bets`, "", "", "", "", "", money(rows.reduce((a, b) => a + b.cost + b.entry_fee, 0)), money(rows.reduce((a, b) => a + b.proceeds - b.exit_fee, 0)), pnl(rows.reduce((a, b) => a + b.pnl, 0)), ""] : null,
  });
}

function renderCompare() {
  const d = S.compare;
  if (!d) return;
  if (!d.scenarios.length) { $("#compareCards").innerHTML = `<div class="empty">Scenarios are seeded after the first Kalshi scan finishes.</div>`; $("#compareTable").innerHTML = ""; return; }
  $("#compareCards").innerHTML = d.scenarios.map((s) => `<div class="panel compare-card ${esc(s.portfolio.slug)}">
      <h2>${esc(s.portfolio.name)}</h2>
      <div class="big ${pnlCls(s.total_pnl)}">${money(s.total_pnl, true)}</div>
      <div class="muted">${pct(s.total_return_pct)} on ${money(s.starting_balance)} bankroll</div>
      <div class="kv mt">
        <div>Bets placed</div><div class="num">${s.open_count + s.counts.won + s.counts.lost + s.counts.sold}${s.meta?.skipped?.length ? ` <span class="muted small">(${s.meta.skipped.length} races skipped)</span>` : ""}</div>
        <div>Total staked</div><div class="num">${money(s.by_chamber.reduce((a, g) => a + g.staked, 0))}</div>
        <div>Current value of open bets</div><div class="num">${money(s.positions_value)}</div>
        <div>Unrealized P/L</div><div class="num">${pnl(s.unrealized_pnl)}</div>
        <div>Realized P/L</div><div class="num">${pnl(s.realized_pnl)}</div>
        <div>Record (settled)</div><div class="num">${s.counts.won}W - ${s.counts.lost}L</div>
        <div>Payout if every bet wins</div><div class="num">${money(s.max_payout_if_all_win)}</div>
      </div>
      <p class="muted small">${esc(s.portfolio.description)}</p>
    </div>`).join("") +
    `<div class="panel" style="grid-column:1/-1"><div class="panel-head"><h2>Equity: Dem vs GOP</h2>
      <div class="legend"><span><i style="background:var(--dem)"></i>All-Dem</span><span><i style="background:var(--gop)"></i>All-GOP</span></div></div>
      <div id="compareChart" class="chart"></div></div>`;
  Promise.all(d.scenarios.map((s) => api(`/api/portfolios/${s.portfolio.id}/snapshots`))).then((snaps) => {
    lineChart($("#compareChart"), d.scenarios.map((s, i) => ({
      name: s.portfolio.slug === "all-dem" ? "All-Dem" : "All-GOP",
      color: s.portfolio.slug === "all-dem" ? "var(--dem)" : "var(--gop)",
      points: snaps[i].map((p) => ({ t: new Date(p.ts).getTime(), v: p.equity - s.starting_balance })),
      baseline: 0,
    })));
  });

  const q = $("#compareSearch").value.trim().toLowerCase(), ch = $("#compareChamber").value;
  const rows = d.races.filter((r) => (!ch || r.chamber === ch) && (!q || [r.race, raceLabel(r)].join(" ").toLowerCase().includes(q)));
  const cell = (x) => x ? `<span class="cmp-cell">${cents(x.entry_price)} → ${cents(x.mark)} ${x.status !== "open" ? `<span class="chip ${x.status}">${x.status}</span>` : ""}</span>` : `<span class="muted">—</span>`;
  table("#compareTable", [
    { key: "race", label: "Race", cls: "title-cell", sortVal: (r) => raceLabel(r), render: (r) => `<b>${esc(raceLabel(r))}</b><div class="ev">${esc(r.race)}</div>` },
    { key: "chamber", label: "Type" },
    { key: "dem", label: "Dem entry → now", nosort: true, render: (r) => cell(r["all-dem"]) },
    { key: "demp", label: "Dem P/L", num: true, sortVal: (r) => r["all-dem"]?.pnl, render: (r) => pnl(r["all-dem"]?.pnl) },
    { key: "gop", label: "GOP entry → now", nosort: true, render: (r) => cell(r["all-gop"]) },
    { key: "gopp", label: "GOP P/L", num: true, sortVal: (r) => r["all-gop"]?.pnl, render: (r) => pnl(r["all-gop"]?.pnl) },
  ], rows, {
    foot: rows.length ? [`${rows.length} races`, "", "", pnl(rows.reduce((a, r) => a + (r["all-dem"]?.pnl || 0), 0)), "", pnl(rows.reduce((a, r) => a + (r["all-gop"]?.pnl || 0), 0))] : null,
  });
}

function renderPortfolios() {
  $("#portfolioList").innerHTML = S.portfolios.length ? S.portfolios.map((p) => `<div class="pf-row">
      <div><b>${esc(p.name)}</b>${p.id === S.pid ? ' <span class="chip">viewing</span>' : ""}<div class="muted small">${esc(p.description)}</div></div>
      <div class="num">${money(p.equity)}<div class="small ${pnlCls(p.total_pnl)}">${money(p.total_pnl, true)} (${pct(p.total_return_pct)})</div></div>
      <div style="display:flex;gap:6px"><button class="btn sm ghost" data-view="${p.id}">View</button>
        ${p.slug.startsWith("all-") ? "" : `<button class="btn sm danger" data-del="${p.id}">Delete</button>`}</div>
    </div>`).join("") : `<div class="empty">No portfolios yet.</div>`;
}

function renderAll() {
  renderSummary(); renderChart(); renderMarkets(); renderActive(); renderHistory(); renderCompare(); renderPortfolios();
}

async function refreshEverything() {
  try {
    await loadPortfolios();
    await Promise.all([loadPortfolio(), loadMidterms(), loadCompare(), loadStatus()]);
    renderAll();
  } catch (e) { toast(e.message, true); }
}

// ---------- bet dialog ----------
let quoteTimer;
async function openBet(ticker, side) {
  if (!S.portfolios.length) return toast("Create a portfolio first", true);
  S.bet = { ticker, side };
  $("#betError").textContent = "";
  $("#betQuote").innerHTML = '<span class="muted">Fetching live quote…</span>';
  $("#betPortfolio").value = S.pid;
  $$("#betSide button").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
  $("#betDialog").showModal();
  updateQuote();
}

async function updateQuote() {
  const { ticker, side } = S.bet;
  const amt = Number($("#betAmount").value);
  const unit = $("#betUnit").value;
  try {
    const q = await api(`/api/quote?ticker=${encodeURIComponent(ticker)}&side=${side}&${unit}=${amt}`);
    const m = q.market;
    $("#betEvent").textContent = m.event_title || m.event_ticker;
    $("#betTitle").innerHTML = `${partyChip(m.party)} ${esc(m.yes_sub_title || m.title)}`;
    if (q.error) { $("#betQuote").innerHTML = `<span class="neg">${esc(q.error)}</span>`; $("#betSubmit").disabled = true; return; }
    const pf = S.portfolios.find((p) => p.id === Number($("#betPortfolio").value));
    const over = pf && q.total > pf.cash;
    $("#betSubmit").disabled = !q.contracts || over;
    $("#betQuote").innerHTML = `<div class="kv">
      <div>Fill price (${side.toUpperCase()} ask)</div><div class="num">${cents(q.price)} <span class="muted small">≈ ${prob(q.implied_prob)} implied</span></div>
      <div>Contracts</div><div class="num">${q.contracts.toLocaleString()}</div>
      <div>Cost</div><div class="num">${money(q.cost)}</div>
      <div>Kalshi fee</div><div class="num">${money(q.fee)}</div>
      <div><b>Total</b></div><div class="num"><b>${money(q.total)}</b></div>
      <div>Pays if ${side.toUpperCase()} wins</div><div class="num">${money(q.max_payout)} <span class="pos small">(${money(q.max_profit, true)})</span></div>
      <div>If it loses</div><div class="num neg">${money(-q.total)}</div>
    </div>${over ? `<div class="error">Not enough cash in this portfolio (${money(pf.cash)}).</div>` : ""}`;
  } catch (e) {
    $("#betQuote").innerHTML = `<span class="neg">${esc(e.message)}</span>`;
    $("#betSubmit").disabled = true;
  }
}

$("#betSide").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-side]"); if (!b) return;
  S.bet.side = b.dataset.side;
  $$("#betSide button").forEach((x) => x.classList.toggle("active", x === b));
  updateQuote();
});
["input", "change"].forEach((ev) => {
  $("#betAmount").addEventListener(ev, () => { clearTimeout(quoteTimer); quoteTimer = setTimeout(updateQuote, 250); });
});
$("#betUnit").addEventListener("change", () => {
  const u = $("#betUnit").value;
  $("#betAmount").step = u === "contracts" ? "1" : "0.01";
  $("#betAmount").value = u === "contracts" ? "100" : "100";
  updateQuote();
});
$("#betPortfolio").addEventListener("change", updateQuote);
$("#betForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "default") return;
  e.preventDefault();
  const pid = Number($("#betPortfolio").value);
  const unit = $("#betUnit").value, amt = Number($("#betAmount").value);
  try {
    $("#betSubmit").disabled = true;
    const bet = await api(`/api/portfolios/${pid}/bets`, { method: "POST", body: { ticker: S.bet.ticker, side: S.bet.side, [unit]: unit === "contracts" ? Math.floor(amt) : amt } });
    $("#betDialog").close();
    toast(`Paper bet placed: ${bet.contracts} ${bet.side.toUpperCase()} @ ${cents(bet.entry_price)}`);
    S.pid = pid; savePid(pid);
    refreshEverything();
  } catch (err) { $("#betError").textContent = err.message; $("#betSubmit").disabled = false; }
});

// ---------- events ----------
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-bet],[data-sell],[data-view],[data-del],[data-reset]");
  if (!t) return;
  if (t.dataset.bet) return openBet(t.dataset.bet, t.dataset.side);
  try {
    if (t.dataset.sell) {
      const b = S.open.find((x) => x.id === Number(t.dataset.sell));
      if (!confirm(`Sell ${b.contracts} ${b.side.toUpperCase()} contracts of "${b.yes_sub_title || b.market_ticker}" at the current bid?`)) return;
      const r = await api(`/api/bets/${t.dataset.sell}/sell`, { method: "POST" });
      toast(`Sold @ ${cents(r.exit_price)} · P/L ${money(r.pnl, true)}`);
    } else if (t.dataset.view) {
      S.pid = Number(t.dataset.view); savePid(S.pid);
    } else if (t.dataset.del) {
      if (!confirm("Delete this portfolio and all its bets?")) return;
      await api(`/api/portfolios/${t.dataset.del}`, { method: "DELETE" });
    } else if (t.dataset.reset) {
      const stake = Number($("#scenarioStake").value) || 100;
      if (!confirm(`Reset ${t.dataset.reset} at current prices with $${stake}/race? Its existing bets and history will be erased.`)) return;
      t.disabled = true;
      const r = await api(`/api/scenarios/${t.dataset.reset}/reset?stake=${stake}`, { method: "POST" });
      toast(`Reseeded: ${r.bets_placed} bets placed, ${r.skipped.length} races skipped`);
      t.disabled = false;
    }
    refreshEverything();
  } catch (err) { toast(err.message, true); t.disabled = false; }
});

$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
  if (tab.dataset.tab === "compare") renderCompare();
}));

$("#portfolioSelect").addEventListener("change", async (e) => {
  S.pid = Number(e.target.value); savePid(S.pid);
  await loadPortfolio(); renderAll();
});

$("#marketSource").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  S.source = b.dataset.src;
  $$("#marketSource button").forEach((x) => x.classList.toggle("active", x === b));
  if (S.source === "all" && !S.allEvents.length) { renderMarkets(); await loadMoreEvents(); }
  renderMarkets();
});
async function loadMoreEvents() {
  try {
    const btn = $("#loadMore"); btn.disabled = true; btn.textContent = "Loading…";
    const d = await api(`/api/kalshi/events?limit=100${S.allCursor ? `&cursor=${encodeURIComponent(S.allCursor)}` : ""}`);
    S.allEvents.push(...d.events); S.allCursor = d.cursor;
    btn.disabled = false; btn.textContent = "Load more events";
  } catch (e) { toast(e.message, true); }
  renderMarkets();
}
$("#loadMore").addEventListener("click", loadMoreEvents);

["marketSearch", "chamberFilter", "categoryFilter"].forEach((id) => $("#" + id).addEventListener("input", renderMarkets));
$("#activeSearch").addEventListener("input", renderActive);
$("#historySearch").addEventListener("input", renderHistory);
["compareSearch", "compareChamber"].forEach((id) => $("#" + id).addEventListener("input", renderCompare));

$("#tickerLookup").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tk = e.target.ticker.value.trim().toUpperCase();
  if (!tk) return;
  try { await api(`/api/kalshi/markets/${encodeURIComponent(tk)}`); openBet(tk, "yes"); }
  catch (err) { toast(err.message, true); }
});

$("#refreshBtn").addEventListener("click", async () => {
  const b = $("#refreshBtn"); b.disabled = true;
  try {
    const r = await api("/api/refresh/prices", { method: "POST" });
    toast(`Updated ${r.quotes} quotes${r.settled ? `, settled ${r.settled} bets` : ""}`);
  } catch (e) { toast(e.message, true); }
  b.disabled = false;
  refreshEverything();
});

$("#newPortfolio").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const p = await api("/api/portfolios", { method: "POST", body: { name: f.get("name"), description: f.get("description"), starting_balance: Number(f.get("starting_balance")) } });
    S.pid = p.id; savePid(p.id); e.target.reset();
    toast(`Created "${p.name}"`);
    refreshEverything();
  } catch (err) { toast(err.message, true); }
});

$("#rediscover").addEventListener("click", async () => {
  const r = await api("/api/refresh/catalog", { method: "POST" });
  toast(r.started ? "Scanning Kalshi… new races will appear in about a minute" : "A scan is already running");
});

window.addEventListener("resize", () => { renderChart(); });

// ---------- boot ----------
refreshEverything();
setInterval(async () => {
  try {
    const st = await loadStatus();
    // While the first scan runs, poll faster so the dashboard fills in.
    if (st.catalog_refreshing || !S.portfolios.length) return refreshEverything();
  } catch {}
}, 5000);
setInterval(refreshEverything, 30000);
