/* XEMM live desk — client. Polls /api/state every few seconds and paints the dashboard.
   Trade-trigger toasts (win/loss + gif + optional sound) fire on newly-completed rounds. */
"use strict";

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

const ui = {
  lastEventId: null,     // highest event id we've toasted (null = not yet seeded)
  seenFeedKeys: new Set(),
  prev: {},              // for value-change flashes
  soundOn: false,
  audio: null,
  consecErrors: 0,
  lastState: null,       // last good /api/state, for chart redraws on hover/zoom/resize
  chartWindow: 15,       // zoom: minutes shown
  chartLayout: null,
  rotateIdx: 0,          // auto-cycle position through [short, 1h, 4h]
  autoRotate: true,
  lastFillTime: null,    // ms of the most recent fill (drives the 5m fill-focus)
  lastTradeTime: null,   // epoch ms of the most recent completed round (drives "time since last trade")
  winGifIdx: 0,          // alternates the big win overlay between the win gifs
  triggerTimer: null,    // dismiss timer for the centered win/loss gif overlay
};

/* escape exchange/remote-controlled strings before innerHTML (coin/symbol/reason/note) */
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ----------------------------------------------------------------- formatting ---------- */
const f = {
  money(x, dp = 2) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    const s = x < 0 ? "-" : "+";
    return `${s}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
  },
  usd(x, dp = 2) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
  },
  num(x, dp = 4) { return (x === null || x === undefined || isNaN(x)) ? "—" : Number(x).toFixed(dp); },
  bps(x, dp = 1) { return (x === null || x === undefined || isNaN(x)) ? "—" : `${x >= 0 ? "+" : ""}${Number(x).toFixed(dp)} bps`; },
  pct(x, dp = 1) { return (x === null || x === undefined || isNaN(x)) ? "—" : `${(x * 100).toFixed(dp)}%`; },
  sign(x) { return x > 0 ? "pos" : x < 0 ? "neg" : "flat"; },
  dur(s) {
    if (s === null || s === undefined || isNaN(s)) return "—";
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s % 60}s`;
    return `${s}s`;
  },
  ago(ms) {
    if (!ms) return "—";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    return s < 60 ? `${Math.floor(s)}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
  },
  agoSec(s) {
    if (s === null || s === undefined) return "—";
    return s < 1 ? "just now" : s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
  },
  short(addr) { return addr ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : "—"; },
};

/* set text + flash on numeric change */
function setVal(id, text, numForFlash) {
  const el = $(id); if (!el) return;
  el.textContent = text;
  if (numForFlash !== undefined && numForFlash !== null && !isNaN(numForFlash)) {
    const p = ui.prev[id];
    if (p !== undefined && numForFlash !== p) {
      el.classList.remove("flash-up", "flash-dn");
      void el.offsetWidth;
      el.classList.add(numForFlash > p ? "flash-up" : "flash-dn");
    }
    ui.prev[id] = numForFlash;
  }
}
function cls(el, ...c) { if (el) { el.className = c.filter(Boolean).join(" "); } }
function fmtElapsed(ms) {   // nicely format a growing span: seconds -> minutes -> hours -> days -> months
  if (ms == null || ms < 0) return "";
  const s = ms / 1000, d = s / 86400;
  if (d >= 30) { const mo = Math.floor(d / 30), rd = Math.floor(d % 30); return `${mo}mo${rd ? " " + rd + "d" : ""}`; }
  if (d >= 1) { const dd = Math.floor(d), h = Math.floor((s % 86400) / 3600); return `${dd}d${h ? " " + h + "h" : ""}`; }
  const h = Math.floor(s / 3600);
  if (h >= 1) { const m = Math.floor((s % 3600) / 60); return `${h}h${m ? " " + m + "m" : ""}`; }
  const m = Math.floor(s / 60); return m >= 1 ? `${m}m` : `${Math.floor(s)}s`;
}
function utcHMS(d) { return (d instanceof Date ? d : new Date(d)).toISOString().slice(11, 19); }  // HH:MM:SS UTC
function utcHM(d) { return (d instanceof Date ? d : new Date(d)).toISOString().slice(11, 16); }   // HH:MM UTC
function sinceMs(s) {
  if (!s) return null;
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
}
function fmtCagr(g) {
  const p = g * 100;
  if (!isFinite(p)) return "∞";
  const a = Math.abs(p);
  if (a >= 1e7) return (p < 0 ? "−" : "+") + "10M%+";          // astronomically large → cap
  if (a >= 1000) return (p >= 0 ? "+" : "−") + Math.round(a).toLocaleString() + "%";
  return (p >= 0 ? "+" : "−") + a.toFixed(1) + "%";
}
function median(a) { if (!a || !a.length) return null; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
function pctile(a, q) { if (!a || !a.length) return null; const s = [...a].sort((x, y) => x - y); const pos = q * (s.length - 1); const lo = Math.floor(pos), hi = Math.ceil(pos); return lo === hi ? s[lo] : s[lo] + (s[hi] - s[lo]) * (pos - lo); }

/* ----------------------------------------------------------------- poll loop ----------- */
async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const st = await r.json();
    ui.consecErrors = 0;
    render(st);
    const dot = $("updatedDot");
    cls(dot, "live-dot", "flash");
    $("updatedText").textContent = "live · " + utcHMS(new Date()) + " UTC";
  } catch (e) {
    ui.consecErrors++;
    const dot = $("updatedDot");
    cls(dot, "live-dot", ui.consecErrors > 3 ? "dead" : "stale");
    $("updatedText").textContent = "reconnecting…";
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

/* ----------------------------------------------------------------- render -------------- */
function render(st) {
  // /api/state no longer carries the price series (the 1s /api/prices poll owns it). Carry it over
  // so the 5s state render doesn't blank the chart (that caused a periodic flicker).
  if (!st.prices && ui.lastState && ui.lastState.prices) st.prices = ui.lastState.prices;
  ui.lastState = st;
  const live = st.live || {}, health = st.health || {}, stats = st.stats || {}, cfg = st.live_config || {};
  const derived = stats.derived || {};
  // Isolate each card: a render bug in one must NOT flip the connection indicator (poll's catch is
  // strictly for fetch/JSON failures) nor freeze the other cards or the toasts.
  const guard = (name, fn) => { try { fn(); } catch (e) { console.error("render " + name, e); } };
  guard("topbar", () => renderTopbar(st, live, health, stats));
  guard("apilat", () => renderApiLatency(st));
  guard("hero", () => renderHero(st, live, health, stats, derived));
  guard("chart", () => renderChart(st));
  guard("health", () => renderHealth(health));
  guard("breaker", () => renderBreaker(live, health, cfg, derived));
  guard("positions", () => renderPositions(live));
  guard("quoting", () => renderQuoteNotice(live, cfg));
  guard("hedge", () => renderHedge(stats));
  guard("econ", () => renderEcon(stats, derived));
  guard("spread", () => renderSpread(st));
  guard("today", () => renderToday(stats, derived));
  guard("feed", () => renderFeed(derived));
  guard("banner", () => renderBanner(stats, health));
  guard("footer", () => renderFooter(live));
  guard("events", () => handleEvents(st.events || []));
}

function renderTopbar(st, live, health, stats) {
  // since
  const since = st.since || {};
  $("sinceVal").textContent = since.value || "—";
  $("sinceSrc").textContent = since.source === "container" ? "container" : since.source === "pnl_fallback" ? "pnl.py" : "";
  $("sinceSrc").title = since.detail || "";
  const t0 = sinceMs(since.value);
  $("sinceElapsed").textContent = (t0 && st.server_time) ? "· " + fmtElapsed(st.server_time - t0) : "";

  // bot status pill
  const pill = $("botPill"), txt = $("botPillText");
  const proc = health.process || {}, hb = health.heartbeat || {};
  const market = parseMarket(proc.cmd);
  let pc = "pill pill-unknown", t = "connecting…";
  if (health.breaker && health.breaker.tripped) { pc = "pill pill-down"; t = "⛔ BREAKER TRIPPED"; }
  else if (!health.reachable) { pc = "pill pill-warn"; t = "VPS unreachable"; }
  else if (proc.running && hb && hb.age_s != null && hb.age_s > 180) { pc = "pill pill-warn"; t = "STALE · no writes " + f.agoSec(hb.age_s); }
  else if (proc.running) { pc = "pill pill-up"; t = `RUNNING${market ? " · " + market : ""}`; }
  else { pc = "pill pill-down"; t = "BOT DOWN"; }
  pill.className = pc; txt.textContent = t;
}

function renderApiLatency(st) {
  const lat = st.api_latency || {};
  $("alAster").textContent = (lat.ok && lat.aster_ms != null) ? lat.aster_ms + "ms" : "—";
  $("alHl").textContent = (lat.ok && lat.hl_ms != null) ? lat.hl_ms + "ms" : "—";
}

function parseMarket(cmd) {
  if (!cmd) return null;
  const m = cmd.match(/--markets\s+([A-Za-z0-9,]+)/);
  return m ? m[1] : null;
}

function renderHero(st, live, health, stats, derived) {
  // net P&L
  const net = derived.net_pnl ?? (stats.pnl && stats.pnl.mark_to_market);
  const pnlEl = $("pnlVal");
  setVal("pnlVal", net == null ? "—" : f.money(net, 4), net);   // 4dp, matches pnl.py
  cls(pnlEl, "kpi-val", "num", f.sign(net));
  $("pnlBasis").textContent = (derived.net_pnl_basis || (stats.empty ? "no trades in window yet" : "—"))
    + (stats.stale ? "  ·  ⚠ stats stale" : "");

  // win rate
  const wr = derived.win_rate ?? (stats.rounds_econ && stats.rounds_econ.win_rate);
  setVal("wrVal", wr == null ? "—" : f.pct(wr, 0), wr);
  $("wrFill").style.width = wr == null ? "0%" : (wr * 100).toFixed(0) + "%";
  $("wrCount").textContent = derived.priced ? `${derived.wins}/${derived.priced} rounds` : "";

  // delta
  const dEl = $("deltaVal");
  if (live.per_coin && live.per_coin.length) {
    if (live.neutral) { dEl.textContent = "● NEUTRAL"; cls(dEl, "kpi-val", "num", "pos"); $("deltaFoot").textContent = `delta-neutral (<0.001) · worst ${f.usd(live.worst_net_usd ?? 0)}`; }
    else { dEl.textContent = "⚠ " + f.usd(live.worst_net_usd ?? 0); cls(dEl, "kpi-val", "num", "neg"); $("deltaFoot").textContent = "NOT delta-neutral"; }
  } else { dEl.textContent = "—"; $("deltaFoot").textContent = "—"; }

  // equity — use the SAME trade_stats capital that pnl.py prints. On a delta-neutral book the
  // marked equity wobbles with the cross-venue basis and the vs-baseline delta is misleading, so
  // (exactly like pnl.py) we show the equity but defer to the per-round P&L and hide vs-baseline.
  const eq = (stats.return && stats.return.capital) ?? (live.equity && live.equity.total);
  setVal("eqVal", eq == null ? "—" : f.usd(eq), eq);
  const base = health.breaker && health.breaker.baseline && health.breaker.baseline.equity_usd;
  const isNeutral = derived.neutral ?? live.neutral;
  if (eq == null) { $("eqFoot").textContent = "—"; }
  else if (isNeutral) { $("eqFoot").textContent = "delta neutral — per-round P&L authoritative"; }
  else if (base) {
    const chg = eq - base, pct = (chg / base) * 100;
    $("eqFoot").innerHTML = `vs baseline ${f.usd(base)} · <span class="${f.sign(chg)}">${f.money(chg)} (${pct >= 0 ? "+" : ""}${pct.toFixed(3)}%)</span>`;
  } else { $("eqFoot").textContent = "live cross-venue equity"; }

  // latency p50
  const lat = stats.latency_ms_primary || [];
  const p50 = median(lat);
  setVal("latVal", p50 == null ? "—" : Math.round(p50) + " ms", p50);
  $("latFoot").textContent = lat.length ? `${Math.min(...lat)}–${Math.max(...lat)} ms · n=${lat.length}` : "no matched pairs";

  // return % over the window + projected CAGR (annualize "if it keeps this rate for a year")
  const cap = (stats.return && stats.return.capital);
  const t0 = sinceMs((st.since || {}).value);
  const hours = t0 ? Math.max((st.server_time - t0) / 3.6e6, 0.05) : null;
  const rfrac = (net != null && cap) ? net / cap : null;
  setVal("retVal", rfrac == null ? "—" : (rfrac >= 0 ? "+" : "−") + Math.abs(rfrac * 100).toFixed(4) + "%", rfrac);
  cls($("retVal"), "kpi-val", "num", rfrac == null ? "" : f.sign(rfrac));
  $("retFoot").textContent = (t0 && net != null && hours) ? `${f.money(net, 4)} over ${hours.toFixed(1)}h` : (net != null ? f.money(net, 4) : "—");
  let cagr = null;
  if (rfrac != null && hours) { const g = Math.pow(1 + rfrac, 8760 / hours) - 1; if (isFinite(g)) cagr = g; }
  const cEl = $("cagrVal");
  cEl.textContent = cagr == null ? "—" : fmtCagr(cagr);
  cls(cEl, "kpi-val", "num", cagr == null ? "" : f.sign(cagr));
  $("cagrFoot").textContent = cagr == null ? "—" : "compounded · illustrative";
}

function renderHealth(health) {
  const tag = $("healthTag");
  if (!health.reachable) { tag.textContent = "unreachable"; cls(tag, "tag", "tag-bad"); }
  else { tag.textContent = "ssh ok"; cls(tag, "tag", "tag-ok"); }

  const proc = health.process || {}, host = health.host || {}, hb = health.heartbeat || {};
  const psEl = $("procState");
  if (proc.running) { psEl.innerHTML = "● live"; psEl.style.color = "var(--green)"; }
  else { psEl.innerHTML = "■ down"; psEl.style.color = "var(--red)"; }

  $("procPid").textContent = proc.pid ?? "—";
  $("procUptime").textContent = f.dur(proc.uptime_s);
  const hbEl = $("hbAge");
  hbEl.textContent = hb ? f.agoSec(hb.age_s) : "—";
  hbEl.style.color = (hb && hb.age_s != null && hb.age_s > 180) ? "var(--amber)" : "var(--txt)";

  // meters
  const ncpu = host.ncpu || 1;
  meter("cpu", proc.cpu_pct, proc.cpu_pct != null ? proc.cpu_pct.toFixed(1) + "%" : "—", Math.min((proc.cpu_pct || 0), 100));
  const rssMb = proc.rss_kb ? proc.rss_kb / 1024 : null;
  meter("rss", rssMb, rssMb != null ? rssMb.toFixed(0) + " MB" : "—", host.mem_total_mb ? (rssMb / host.mem_total_mb * 100) : 5);
  const load1 = host.loadavg ? host.loadavg[0] : null;
  const loadPct = load1 != null ? (load1 / ncpu * 100) : null;   // 1m load as % of vCPU capacity
  meter("load", loadPct, loadPct != null ? Math.round(loadPct) + "%" : "—", loadPct != null ? loadPct : 0);
  const lv = $("loadVal"); if (lv) lv.title = load1 != null ? `${load1.toFixed(2)} load avg over ${ncpu} vCPU` : "";

  $("procCmd").textContent = proc.cmd || (health.reachable ? "(no xemm_eval process found)" : "—");
  const parts = [];
  if (host.uptime_s) parts.push(`host up ${f.dur(host.uptime_s)}`);
  if (host.ncpu) parts.push(`${host.ncpu} vCPU`);
  if (host.mem_total_mb) parts.push(`${(host.mem_total_mb / 1024).toFixed(1)} GB RAM`);
  if (host.disk_avail_gb != null) parts.push(`${host.disk_avail_gb} GB disk free`);
  if (health.checked_at) parts.push(`probed ${f.ago(Date.parse(health.checked_at))}`);
  if (hb && hb.file) parts.push(`heartbeat: ${hb.file}`);
  $("hostLine").textContent = parts.join("  ·  ") || (health.error || "—");
}

function meter(name, value, label, pctWidth) {
  $(name + "Val").textContent = label;
  const bar = $(name + "Bar");
  const w = Math.max(0, Math.min(100, pctWidth || 0));
  bar.style.width = w.toFixed(0) + "%";
  bar.classList.toggle("hot", w > 80);
}

// discreet breaker chip. Shows the SAME authoritative P&L as the headline (per-round matched,
// +$0.0365) vs the loss limit — consistent with the dashboard rather than the noisy equity delta.
function renderBreaker(live, health, cfg, derived) {
  const bk = (health.breaker) || {};
  const chip = $("breakerChip"), txt = $("bkChipText");
  if (!chip) return;
  const limit = cfg.breaker_max_loss_usd ?? (bk.record && Number(bk.record.limit_usd));
  const pnl = derived && derived.net_pnl;
  if (bk.tripped) {
    chip.className = "bk-chip tripped";
    txt.innerHTML = "circuit breaker <b>TRIPPED</b>";
  } else if (bk.baseline || limit) {
    chip.className = "bk-chip armed";
    const tail = (pnl != null && limit) ? ` <b class="${f.sign(pnl)}">${f.money(pnl, 2)}</b> / -${f.usd(limit, 0)}`
      : (limit ? ` limit -${f.usd(limit, 0)}` : "");
    txt.innerHTML = `circuit breaker armed${tail}`;
  } else {
    chip.className = "bk-chip";
    txt.textContent = "circuit breaker —";
  }
}

function renderPositions(live) {
  const body = $("posBody"), tag = $("posTag");
  const pc = live.per_coin || [];
  if (!pc.length) { body.innerHTML = `<tr><td colspan="7" class="empty">${live.ok === false ? "exchange error" : "no open positions"}</td></tr>`; tag.textContent = "flat"; cls(tag, "tag"); return; }
  tag.textContent = live.neutral ? "neutral" : "NOT NEUTRAL";
  cls(tag, "tag", live.neutral ? "tag-ok" : "tag-bad");
  body.innerHTML = pc.map(p => {
    const upnl = (p.aster_upnl || 0) + (p.hl_upnl || 0);
    return `<tr>
      <td class="coin-tag">${esc(p.coin)}</td>
      <td class="r">${p.mid ? f.usd(p.mid, p.mid < 10 ? 4 : 2) : "—"}</td>
      <td class="r ${p.aster_qty < 0 ? 'side-sell' : 'side-buy'}">${p.aster_qty >= 0 ? "+" : ""}${f.num(p.aster_qty, 4)}</td>
      <td class="r ${p.hl_qty < 0 ? 'side-sell' : 'side-buy'}">${p.hl_qty >= 0 ? "+" : ""}${f.num(p.hl_qty, 4)}</td>
      <td class="r ${f.sign(p.net)}">${p.net >= 0 ? "+" : ""}${f.num(p.net, 4)}</td>
      <td class="r">${p.net_usd == null ? "—" : f.usd(p.net_usd)}</td>
      <td class="r ${f.sign(upnl)}">${f.money(upnl, 3)}</td>
    </tr>`;
  }).join("");
}

// One-sided quoting notice: the bot is quoting ONLY the reducing side because it can no longer ADD
// to inventory — capital is effectively fully deployed. This mirrors the live bot's real binding
// constraint: the increasing side is suppressed (margin-reject suppression, or the position cap)
// when, on the BINDING leg, headroom against the EFFECTIVE cap = min(config soft cap, available
// margin ≈ leg equity) drops below one clip. The $200 config cap never binds at ~$124 equity, so
// margin is the true ceiling. Below capacity, one-sidedness is edge/basis-driven — we assert nothing.
function detectOneSided(live, cfg) {
  const oo = live.open_orders || [], pc = live.per_coin || [];
  const eq = live.equity || {}, capCfg = cfg.position_cap || {};
  const buffer = cfg.desired_notional || 12;            // next clip the bot tries; margin rejects below this
  // effective ceiling per leg = min(config soft cap, available margin ≈ equity); fall back to whichever
  // is present (the bot is margin-bound well before the $200 soft cap).
  const effCap = (cfgCap, equity) =>
    (cfgCap == null && equity == null) ? null
      : cfgCap == null ? equity : equity == null ? cfgCap : Math.min(cfgCap, equity);
  const asterCap = effCap(capCfg.aster_notional, eq.aster);
  const hlCap = effCap(capCfg.hl_notional, eq.hl);
  for (const p of pc) {
    const aOrders = oo.filter(o => o.venue === "aster" && o.coin === p.coin);
    if (!aOrders.length || !p.mid) continue;
    const qty = p.aster_qty || 0;
    if (Math.abs(qty) < 1e-9) continue;                 // no inventory -> not a capacity case
    const sides = [...new Set(aOrders.map(o => o.side))];
    const reducing = qty > 0 ? "SELL" : "BUY";          // long -> only the ask reduces; short -> only the bid
    if (sides.length !== 1 || sides[0] !== reducing) continue;
    // headroom left to ADD on each leg; the bot can't add another clip once it falls below `buffer`.
    const asterPos = Math.abs(qty * p.mid), hlPos = Math.abs((p.hl_qty || 0) * p.mid);
    const asterHead = asterCap != null ? asterCap - asterPos : Infinity;
    const hlHead = hlCap != null ? hlCap - hlPos : Infinity;
    if (Math.min(asterHead, hlHead) > buffer) continue; // not at capacity -> edge/basis driven, stay silent
    const onHl = hlHead < asterHead;                    // bot's binding leg = min(aster, hl) headroom
    return { coin: p.coin, side: reducing, dir: qty > 0 ? "long" : "short",
             notional: onHl ? hlPos : asterPos, cap: onHl ? hlCap : asterCap, leg: onHl ? "HL" : "Aster" };
  }
  return null;
}

function renderQuoteNotice(live, cfg) {
  const el = $("quoteNotice"); if (!el) return;
  const d = detectOneSided(live, cfg || {});
  if (!d) { el.hidden = true; el.innerHTML = ""; return; }
  const sideWord = d.side === "SELL" ? "ask" : "bid";
  const arrow = d.side === "SELL" ? "▼" : "▲";
  const pctFull = (d.cap && d.notional != null) ? Math.min(100, d.notional / d.cap * 100) : null;
  const fill = (d.cap && d.notional != null)
    ? `${f.usd(d.notional, 0)} / ${f.usd(d.cap, 0)} deployed${pctFull != null ? ` · ${pctFull.toFixed(0)}%` : ""}`
    : (d.notional != null ? f.usd(d.notional, 0) : "");
  const legNote = d.leg === "HL" ? " on the HL leg" : "";
  el.hidden = false;
  el.innerHTML = `
    <span class="qn-arrow ${d.side === "SELL" ? "sell" : "buy"}">${arrow}</span>
    <div class="qn-body">
      <div class="qn-title">capital fully deployed · one-sided</div>
      <div class="qn-sub">${esc(d.coin)} ${d.dir} at capacity${legNote} (${fill}) — quoting <b>${sideWord}</b> only, to reduce exposure.</div>
    </div>`;
}

function renderHedge(stats) {
  const tag = $("hedgeTag");
  if (stats.empty || !stats.activity) { $("hedgePrimary").textContent = "—"; $("hedgeBreakdown").innerHTML = ""; $("latCaption").textContent = ""; $("latScale").innerHTML = ""; tag.textContent = "—"; cls(tag, "tag"); return; }
  const hq = stats.hedge_quality || {}, act = stats.activity || {};
  const nonprimary = (hq.fallback || 0) + (hq.unhedged || 0) + (hq.hl_other_cloid || 0) + (hq.hl_nocloid || 0);
  tag.textContent = nonprimary === 0 ? "all clean" : `${nonprimary} non-primary`;
  cls(tag, "tag", nonprimary === 0 ? "tag-ok" : "tag-warn");
  $("hedgePrimary").textContent = `${hq.primary || 0}/${act.rounds || 0}`;
  const chips = [
    chip("fallback", hq.fallback, hq.fallback > 0),
    chip("recovery", hq.hl_other_cloid, hq.hl_other_cloid > 0),
    chip("flatten/manual", hq.hl_nocloid, hq.hl_nocloid > 0),
    chip("unhedged", hq.unhedged, hq.unhedged > 0),
  ];
  $("hedgeBreakdown").innerHTML = chips.join("");

  const lat = stats.latency_ms_primary || [];
  const scale = $("latScale");
  if (lat.length) {
    const lo = Math.min(...lat), hi = Math.max(...lat), span = Math.max(hi - lo, 1);
    const p50 = pctile(lat, 0.5);
    const dots = lat.map(v => `<span class="dot" style="left:${((v - lo) / span * 92 + 4).toFixed(1)}%"></span>`).join("");
    const p50pos = ((p50 - lo) / span * 92 + 4).toFixed(1);
    scale.innerHTML = dots + `<span class="p50" style="left:${p50pos}%"></span>`;
    $("latCaption").textContent = `${Math.round(lo)}–${Math.round(hi)}ms · p50 ${Math.round(p50)} · p90 ${Math.round(pctile(lat, 0.9))} · n${lat.length}`;
  } else { scale.innerHTML = ""; $("latCaption").textContent = "no matched pairs yet"; }
}
function chip(label, n, bad) { return `<span class="hb-chip ${bad ? 'bad' : ''}">${label} <b>${n || 0}</b></span>`; }
function ls(label, v) { return `<div><b>${v}</b><span>${label}</span></div>`; }

function renderOrders(live) {
  const body = $("ordersBody"), tag = $("ordersTag"), viz = $("spreadViz");
  const oo = live.open_orders || [];
  tag.textContent = `${oo.length} resting`;
  cls(tag, "tag", oo.length ? "tag-ok" : "tag");
  if (!oo.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">no resting quotes</td></tr>`;
    viz.innerHTML = `<span class="empty-q">no resting quotes — bot may be frozen or between requotes</span>`;
    return;
  }
  body.innerHTML = oo.map(o => `<tr>
    <td>${o.venue === "aster" ? "Aster" : "HL"}</td>
    <td class="coin-tag">${esc(o.coin)}</td>
    <td class="${o.side === 'BUY' ? 'side-buy' : 'side-sell'}">${esc(o.side)}</td>
    <td class="r">${o.price ? f.usd(o.price, o.price < 10 ? 4 : 3) : "—"}</td>
    <td class="r">${o.dist_bps == null ? "—" : f.bps(o.dist_bps)}</td>
    <td class="r">${f.num(o.qty, 4)}</td>
    <td class="r muted">${o.age_s == null ? "—" : o.age_s + "s"}</td>
  </tr>`).join("");

  // spread viz around mid (use the coin with the most orders)
  const byCoin = {};
  oo.forEach(o => { (byCoin[o.coin] = byCoin[o.coin] || []).push(o); });
  const coin = Object.keys(byCoin).sort((a, b) => byCoin[b].length - byCoin[a].length)[0];
  const orders = byCoin[coin].filter(o => o.dist_bps != null);
  if (!orders.length) { viz.innerHTML = `<div class="mid"></div>`; return; }
  const maxAbs = Math.max(10, ...orders.map(o => Math.abs(o.dist_bps)));
  let html = `<div class="mid"></div><div class="mid-lbl">${coin} mid</div>`;
  orders.forEach(o => {
    const x = 50 + (o.dist_bps / maxAbs) * 44;   // -maxAbs..+maxAbs -> 6..94%
    html += `<div class="q ${o.side === 'BUY' ? 'buy' : 'sell'}" style="left:${x.toFixed(1)}%">${o.side === 'BUY' ? '▲' : '▼'} ${f.bps(o.dist_bps, 0)}</div>`;
  });
  viz.innerHTML = html;
}

function renderEcon(stats, derived) {
  const tag = $("econTag");
  if (stats.empty || !stats.activity) { ["ecSum", "ecAvg", "ecBest", "ecWorst", "ecBasis", "ecVol", "ecSpan", "ecFees"].forEach(i => $(i).textContent = "—"); $("ecWinLoss").innerHTML = ""; tag.textContent = "—"; cls(tag, "tag"); return; }
  const feed = derived.feed || [];
  const nets = feed.map(r => r.net).filter(x => x != null);
  const basis = feed.map(r => r.basis_bps).filter(x => x != null);
  tag.textContent = `${stats.activity.rounds} rounds`;
  cls(tag, "tag");
  setVal("ecSum", derived.round_net_sum == null ? "—" : f.money(derived.round_net_sum, 4), derived.round_net_sum);
  $("ecAvg").textContent = (derived.round_net_sum != null && derived.priced) ? f.money(derived.round_net_sum / derived.priced, 5) : "—";
  $("ecBest").textContent = nets.length ? f.money(Math.max(...nets), 5) : "—";
  $("ecWorst").textContent = nets.length ? f.money(Math.min(...nets), 5) : "—";
  $("ecBasis").textContent = basis.length ? f.bps(median(basis), 2) : "—";
  setVal("ecVol", f.usd(stats.activity.volume), stats.activity.volume);
  $("ecSpan").textContent = stats.activity.span_hours != null ? stats.activity.span_hours.toFixed(2) + " h" : "—";
  const fees = stats.pnl && stats.pnl.fees;
  $("ecFees").textContent = fees != null ? f.money(-Math.abs(fees), 4) : "—";

  // overall payoff ratio: average win vs average loss MAGNITUDE across the window (not daily).
  // Distinct from profit factor (gross win ÷ gross loss); avg-win ÷ avg-loss is the payoff ratio.
  const wins = nets.filter(x => x > 0);
  const losses = nets.filter(x => x < 0).map(Math.abs);
  const avgWin = wins.length ? wins.reduce((a, b) => a + b, 0) / wins.length : null;
  const avgLoss = losses.length ? losses.reduce((a, b) => a + b, 0) / losses.length : null;
  const payoff = (avgWin != null && avgLoss) ? avgWin / avgLoss : null;
  const wl = $("ecWinLoss");
  if (wl) {
    wl.title = "payoff ratio = average win ÷ average loss magnitude over the whole window — "
      + "related to but not the same as profit factor (gross win ÷ gross loss)";
    wl.innerHTML = (avgWin == null && avgLoss == null) ? "" :
      `<span>avg win <b class="pos num">${avgWin != null ? f.money(avgWin, 5) : "—"}</b></span>`
      + `<span>avg loss <b class="neg num">${avgLoss != null ? f.money(-avgLoss, 5) : "—"}</b></span>`
      + `<span>win/loss <b class="num">${payoff != null ? payoff.toFixed(2) + "×" : "—"}</b></span>`;
  }
}

function renderConfig(cfg, health) {
  $("cfgEdge").textContent = cfg.required_edge_bps != null ? cfg.required_edge_bps + " bps" : "—";
  $("cfgFee").textContent = cfg.roundtrip_fee_bps != null ? cfg.roundtrip_fee_bps + " bps" : "—";
  $("cfgNotional").textContent = cfg.desired_notional != null ? f.usd(cfg.desired_notional, 0) + "/side" : "—";
  $("cfgBreaker").textContent = cfg.breaker_max_loss_usd != null ? "-" + f.usd(cfg.breaker_max_loss_usd, 0) : "—";
  const markets = cfg.markets || [];
  const liveMarket = parseMarket((health.process || {}).cmd);
  const liveCoins = liveMarket ? liveMarket.split(",") : [];
  $("cfgMarkets").innerHTML = markets.length ? markets.map(m => `<span class="chip">${m}</span>`).join("") : "—";
  $("cfgLive").innerHTML = liveCoins.length ? liveCoins.map(c => `<span class="chip live">${c}</span>`).join("") : `<span class="muted small">—</span>`;
}

// "fairly big" decomposition of how the maker quote is built off the HL hedge price.
function renderSpread(st) {
  const wrap = $("spreadViz2"); if (!wrap) return;
  const edge = (st.live_config || {}).edge || {};
  const tag = $("spreadTag");
  const comps = [
    { label: "HL taker fee", bps: edge.hyperliquid_taker_fee_bps, cls: "sd-fee" },
    { label: "Aster fee", bps: edge.aster_maker_fee_bps, cls: "sd-fee" },
    { label: "slippage", bps: edge.slippage_buffer_bps, cls: "sd-buf" },
    { label: "latency", bps: edge.latency_buffer_bps, cls: "sd-buf" },
    { label: "basis", bps: edge.basis_buffer_bps, cls: "sd-buf" },
    { label: "funding", bps: edge.funding_buffer_bps, cls: "sd-buf" },
    { label: "Expected profit margin", bps: edge.min_net_profit_bps, cls: "sd-profit" },
  ].filter(c => c.bps != null && c.bps > 0);
  const total = comps.reduce((a, c) => a + c.bps, 0);
  if (!total) { wrap.innerHTML = `<div class="empty">config unavailable</div>`; if (tag) tag.textContent = "—"; return; }
  if (tag) { tag.textContent = `${total.toFixed(1)} bps/side`; }

  const pc = (st.live && st.live.per_coin && st.live.per_coin[0]) || {};
  const hlMid = pc.mid || null;
  const askPx = hlMid ? hlMid * (1 + total / 1e4) : null;
  const bidPx = hlMid ? hlMid * (1 - total / 1e4) : null;
  const oo = (st.live && st.live.open_orders) || [];
  const aBid = oo.find(o => o.venue === "aster" && o.side === "BUY");
  const aAsk = oo.find(o => o.venue === "aster" && o.side === "SELL");

  // The bar shows each component's magnitude (bps); narrow blocks can't fit a name, so EVERY
  // component is named in the legend below (same left-to-right order) — no more unlabelled "1.0 bps".
  const segs = comps.map(c =>
    `<div class="sd-seg ${c.cls}" style="flex:${c.bps}" title="${esc(c.label)}: ${c.bps} bps">
      <span class="sd-bps">${c.bps.toFixed(1)}<span class="sd-unit">bps</span></span></div>`).join("");
  const legend = comps.map(c =>
    `<span class="sd-key ${c.cls}"><i></i>${esc(c.label)} <b>${c.bps.toFixed(1)} bps</b></span>`).join("");

  wrap.innerHTML = `
    <div class="sd-ends">
      <span class="hl-end">◀ HL hedge ${hlMid ? "$" + hlMid.toFixed(3) : ""}</span>
      <span class="ask-end">Aster ask ${askPx ? "$" + askPx.toFixed(3) : ""} · +${total.toFixed(1)} bps ▶</span>
    </div>
    <div class="sd-bar">${segs}</div>
    <div class="sd-legend">${legend}</div>
    <div class="sd-foot">
      <span>full spread <b>${(total * 2).toFixed(1)} bps</b> · quotes bid <b>${bidPx ? "$" + bidPx.toFixed(3) : "—"}</b> / ask <b>${askPx ? "$" + askPx.toFixed(3) : "—"}</b></span>
      <span>live dist: bid <b>${aBid && aBid.dist_bps != null ? f.bps(aBid.dist_bps, 1) : "—"}</b> · ask <b>${aAsk && aAsk.dist_bps != null ? f.bps(aAsk.dist_bps, 1) : "—"}</b></span>
    </div>`;
}

// "Today" summary (UTC day): trades / win rate / net P&L ($ and % of capital), from the per-round
// feed filtered to >= 00:00 UTC. Rolls over automatically since dayStart is recomputed each poll.
function renderToday(stats, derived) {
  if (!$("tdTrades")) return;
  const feed = (derived && derived.feed) || [];
  const now = new Date();
  const dayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const today = feed.filter(r => r.time != null && r.time >= dayStart);
  const ok = !!(stats && stats.ok);
  const n = today.length;
  const wins = today.filter(r => (r.net || 0) > 0).length;
  const pnl = today.reduce((a, r) => a + (r.net || 0), 0);
  const cap = stats && stats.return && stats.return.capital;
  const pct = (cap && n) ? pnl / cap : null;

  $("tdTrades").textContent = n ? String(n) : (ok ? "0" : "—");
  const wr = n ? wins / n : null;
  $("tdWr").textContent = wr == null ? "—" : `${(wr * 100).toFixed(0)}%`;
  $("tdWr").title = n ? `${wins}/${n} rounds won` : "";
  setVal("tdPnl", n ? f.money(pnl, 4) : (ok ? f.money(0, 4) : "—"), n ? pnl : undefined);
  cls($("tdPnl"), "td-v", n ? f.sign(pnl) : "");
  $("tdPnlPct").textContent = pct == null ? (ok && !n ? f.pct(0, 4) : "—")
    : `${pct >= 0 ? "+" : "−"}${Math.abs(pct * 100).toFixed(4)}%`;
  cls($("tdPnlPct"), "td-v", pct == null ? "" : f.sign(pct));
}

function renderFeed(derived) {
  const list = $("feedList"), tag = $("feedTag");
  const feed = derived.feed || [];
  if (!feed.length) { list.innerHTML = `<div class="empty">no completed rounds in the window yet</div>`; tag.textContent = "0"; cls(tag, "tag"); return; }
  tag.textContent = `${feed.length}`;
  cls(tag, "tag");
  const maxT = Math.max(0, ...feed.map(r => r.time || 0));
  if (maxT) { ui.lastFillTime = Math.max(ui.lastFillTime || 0, maxT); ui.lastTradeTime = maxT; }
  tickLastTrade();
  list.innerHTML = feed.map(r => {
    const isNew = !ui.seenFeedKeys.has(r.key);
    const t = r.time ? new Date(r.time) : null;
    return `<div class="feed-row ${r.win ? 'win' : 'loss'} ${isNew ? 'flash-in' : ''}">
      <div class="fr-top">
        <span class="fr-res">${r.win ? "WIN" : "LOSS"}</span>
        <span class="fr-pair"><span class="${r.side === 'BUY' ? 'side-buy' : 'side-sell'}">${esc(r.side)}</span> ${esc(r.coin)}</span>
        <span class="fr-pnl ${f.sign(r.net)}"><span class="fr-net">net</span> ${f.money(r.net, 4)}</span>
      </div>
      <div class="fr-bot">
        <span>${f.num(r.qty, 4)} @ ${f.usd(r.price, r.price < 10 ? 4 : 3)}${r.basis_bps != null ? " · " + f.bps(r.basis_bps, 1) : ""}</span>
        <span>${r.latency_ms != null ? r.latency_ms + "ms" : ""}${t ? " · " + utcHMS(t) : ""}</span>
      </div>
    </div>`;
  }).join("");
  feed.forEach(r => ui.seenFeedKeys.add(r.key));
}

// "time since last trade" — ticks every second off the most recent completed round.
const TRADE_QUIET_MS = 20 * 60000;   // highlight (amber) once the desk has been quiet this long
function tickLastTrade() {
  const el = $("lastTradeAgo"); if (!el) return;
  if (!ui.lastTradeTime) { el.textContent = "last trade —"; el.classList.remove("stale"); return; }
  const dt = Math.max(0, Date.now() - ui.lastTradeTime);
  el.textContent = "last trade " + fmtElapsed(dt) + " ago";
  el.classList.toggle("stale", dt >= TRADE_QUIET_MS);
}

function renderBanner(stats, health) {
  const b = $("banner");
  if (health.breaker && health.breaker.tripped) {
    b.hidden = false; b.className = "banner danger";
    const rec = health.breaker.record || {};
    b.innerHTML = `⛔ <b>Circuit breaker TRIPPED</b> — ${esc(rec.reason || "cumulative loss limit hit")}. Bot halted; positions left open. Clear with <code>scripts/reset_breaker.py</code>.`;
    return;
  }
  // Boundary caveats intentionally not shown as a banner (per request). The circuit-breaker
  // alert above is the only banner.
  b.hidden = true;
}

function renderFooter(live) {
  const a = live.accounts;
  if (a) $("footAccounts").textContent = `Aster ${f.short(a.aster_user)} · HL ${f.short(a.hl_user)}`;
}

/* ----------------------------------------------------------------- events / toasts ----- */
function handleEvents(events) {
  if (!events.length) return;
  const maxId = Math.max(...events.map(e => e.id));
  if (ui.lastEventId === null) { ui.lastEventId = maxId; return; }   // seed: don't toast backlog
  const fresh = events.filter(e => e.id > ui.lastEventId).sort((a, b) => a.id - b.id);
  fresh.forEach(showToast);
  if (fresh.length) {
    ui.lastEventId = maxId;
    focusFill();                       // a fresh fill -> snap to the 5m view
    showTrigger(fresh[fresh.length - 1]);   // big centered gif for the most recent round
  }
}

/* big celebratory / commiserating gif, centered, with a green/red glow.
   win  -> alternates dicaprio.gif <-> macmahon.gif ;  loss -> gosling-dive.gif */
const WIN_GIFS = ["dicaprio.gif", "macmahon.gif"];
const LOSS_GIF = "gosling-dive.gif";

// Warm the browser cache once so the overlay pops instantly and the gif plays from frame 0.
// All local (served over loopback from /gifs), so this costs no external bandwidth.
function preloadTriggerGifs() {
  [LOSS_GIF, ...WIN_GIFS].forEach((name) => { const i = new Image(); i.src = `/gifs/${name}`; });
}

function showTrigger(ev) {
  const wrap = $("gifTrigger");
  if (!wrap) return;
  const win = ev.kind === "win";
  const file = win ? WIN_GIFS[ui.winGifIdx++ % WIN_GIFS.length] : LOSS_GIF;
  const detail = `${esc(ev.side)} ${esc(ev.coin)} ${f.num(ev.qty, 4)} @ ${f.usd(ev.price, ev.price < 10 ? 4 : 3)}`
    + (ev.latency_ms != null ? ` · ${ev.latency_ms}ms` : "");
  // Recreate the <img> each time so the cached gif restarts its animation from the first frame.
  wrap.innerHTML = `
    <div class="gt-card">
      <img class="gt-gif" src="/gifs/${file}" alt="">
      <div class="gt-cap">
        <span class="gt-title">${win ? "WINNING TRADE" : "LOSING TRADE"}</span>
        <span class="gt-pnl">${f.money(ev.net, 4)}</span>
        <span class="gt-sub">${detail}</span>
      </div>
    </div>`;
  wrap.className = `gif-trigger ${win ? "win" : "loss"}`;
  wrap.hidden = false;
  void wrap.offsetWidth;               // force reflow so the pop-in animation restarts
  wrap.classList.remove("out"); wrap.classList.add("in");
  clearTimeout(ui.triggerTimer);
  ui.triggerTimer = setTimeout(() => {
    wrap.classList.remove("in"); wrap.classList.add("out");
    setTimeout(() => { if (wrap.classList.contains("out")) { wrap.hidden = true; wrap.innerHTML = ""; } }, 500);
  }, 6000);
}

function showToast(ev) {
  const win = ev.kind === "win";
  const el = document.createElement("div");
  el.className = `toast ${win ? "win" : "loss"}`;
  const gif = win ? "win" : "loss";
  el.innerHTML = `
    <img src="/gifs/${gif}.gif" onerror="this.onerror=null;this.src='/gifs/${gif}.svg'" alt="">
    <div class="t-body">
      <div class="t-title">${win ? "✓ WINNING TRADE" : "✗ LOSING TRADE"}</div>
      <div class="t-sub">${esc(ev.side)} ${esc(ev.coin)} ${f.num(ev.qty, 4)} @ ${f.usd(ev.price, ev.price < 10 ? 4 : 3)}${ev.latency_ms != null ? " · " + ev.latency_ms + "ms" : ""}</div>
    </div>
    <div class="t-pnl">${f.money(ev.net, 4)}</div>`;
  $("toasts").prepend(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 400); }, 9000);
  // cap stack
  const stack = $("toasts");
  while (stack.children.length > 5) stack.lastChild.remove();
}

/* ----------------------------------------------------------------- controls ------------ */
$("refreshBtn").addEventListener("click", async () => {
  const b = $("refreshBtn"); b.style.transform = "rotate(360deg)"; b.style.transition = "transform .5s";
  try { await fetch("/api/refresh", { method: "POST" }); } catch (e) {}
  setTimeout(() => { b.style.transform = ""; }, 500);
});

/* ----------------------------------------------------------------- PRICE CHART (main) ---- */
let chartHover = null, rafPending = false;

function renderChart(st) {
  const cv = $("priceChart"); if (!cv) return;
  const prices = st.prices || {};
  const hist = prices.hist || [], live = prices.live || [];
  // Merge: coarse 1m history for the old part, fine 5s ticks for the recent tail (no overlap).
  let series;
  if (live.length) { const cut = live[0].t; series = hist.filter(p => p.t < cut).concat(live); }
  else series = hist.slice();
  series = series.filter(p => p.aster != null || p.hl != null).sort((a, b) => a.t - b.t);

  const empty = $("chartEmpty");
  if (series.length < 2) { empty.hidden = false; cv.style.opacity = 0.15; return; }
  empty.hidden = true; cv.style.opacity = 1;

  const W = (ui.chartWindow || 60) * 60000;
  const maxT = series[series.length - 1].t;
  const tMax = maxT, tMin = maxT - W;
  const pts = series.filter(p => p.t >= tMin);
  if (pts.length < 2) { empty.hidden = false; empty.textContent = "no data in this window"; return; }

  const feed = ((st.stats || {}).derived || {}).feed || [];
  const fills = feed.filter(r => r.time != null && r.time >= tMin && r.time <= tMax && r.price != null);
  const orders = ((st.live || {}).open_orders || []).filter(o => o.venue === "aster" && o.price);
  const reqEdge = (st.live_config || {}).required_edge_bps || null;

  drawChart(cv, pts, fills, orders, tMin, tMax, reqEdge);

  const meta = prices.meta || {};
  $("chartSub").textContent = meta.feed === "ws" ? "— live via WebSocket · every fill marked"
    : "— live · every fill marked";

  const last = pts[pts.length - 1];
  $("lgAster").textContent = last.aster != null ? f.usd(last.aster, 3) : "—";
  $("lgHl").textContent = last.hl != null ? f.usd(last.hl, 3) : "—";
  const lb = (last.aster != null && last.hl != null) ? (last.aster - last.hl) / last.hl * 1e4 : null;
  $("lgBasis").textContent = lb != null ? f.bps(lb, 1) : "—";
}

function drawChart(cv, pts, fills, orders, tMin, tMax, reqEdge) {
  const dpr = window.devicePixelRatio || 1;
  const rect = cv.getBoundingClientRect();
  const W = Math.max(320, rect.width), H = Math.max(240, rect.height);
  const tw = Math.round(W * dpr), th = Math.round(H * dpr);
  if (cv.width !== tw || cv.height !== th) { cv.width = tw; cv.height = th; }  // only on real resize
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const padR = 62, padB = 20, basisH = 50, gap = 12;
  const plotL = 10, plotR = W - padR, plotW = plotR - plotL;
  const priceTop = 10, priceBot = H - padB - basisH - gap;
  const basisTop = H - padB - basisH, basisBot = H - padB;

  // price y-range across both venues + quotes + fills
  let lo = Infinity, hi = -Infinity;
  const grow = (v) => { if (v != null && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
  pts.forEach(p => { grow(p.aster); grow(p.hl); });
  orders.forEach(o => grow(o.price));
  fills.forEach(fl => grow(fl.price));
  if (!isFinite(lo) || !isFinite(hi)) return;
  const pad = (hi - lo) * 0.12 || (hi * 0.001) || 0.1; lo -= pad; hi += pad;
  const tSpan = Math.max(tMax - tMin, 1), pSpan = Math.max(hi - lo, 1e-9);
  const xOf = (t) => plotL + (t - tMin) / tSpan * plotW;
  const yOf = (p) => priceTop + (1 - (p - lo) / pSpan) * (priceBot - priceTop);

  // grid + price labels
  ctx.font = "11px ui-monospace,monospace"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const v = lo + pSpan * i / 4, y = yOf(v);
    ctx.strokeStyle = "rgba(255,255,255,0.05)"; ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke();
    ctx.fillStyle = "#5d6678"; ctx.textAlign = "left"; ctx.fillText("$" + v.toFixed(3), plotR + 6, y);
  }
  // time labels
  ctx.textAlign = "center"; ctx.fillStyle = "#5d6678";
  for (let i = 0; i <= 4; i++) {
    const t = tMin + tSpan * i / 4;
    ctx.fillText(utcHM(t), xOf(t), priceBot + 12);   // UTC axis labels
  }

  // resting quote lines
  orders.forEach(o => {
    if (o.price < lo || o.price > hi) return;
    const y = yOf(o.price), buy = o.side === "BUY";
    ctx.setLineDash([4, 4]); ctx.strokeStyle = buy ? "rgba(52,224,161,0.45)" : "rgba(255,93,115,0.45)";
    ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = buy ? "#34e0a1" : "#ff5d73"; ctx.textAlign = "left"; ctx.font = "9px ui-monospace,monospace";
    ctx.fillText((buy ? "bid " : "ask ") + o.price.toFixed(3), plotL + 4, y - 6);
    ctx.font = "11px ui-monospace,monospace";
  });

  // price lines (HL under Aster)
  drawSeries(ctx, pts, "hl", xOf, yOf, "#4fd6e6");
  drawSeries(ctx, pts, "aster", xOf, yOf, "#ffc25c");

  // fill markers — shape=side (▲buy ▼sell), fill=outcome (green win / red loss)
  const markers = [];
  fills.forEach(fl => {
    const x = xOf(fl.time), y = yOf(fl.price);
    drawMarker(ctx, x, y, fl.side === "BUY", fl.win);
    markers.push({ x, y, fl });
  });

  // basis strip
  drawBasis(ctx, pts, xOf, basisTop, basisBot, plotL, plotR, reqEdge);

  ui.chartLayout = { markers, plotL, plotR, priceTop, priceBot: basisBot };
  if (chartHover) drawHover(ctx, markers, chartHover, plotL, plotR, priceTop, basisBot);
}

function drawSeries(ctx, pts, key, xOf, yOf, color) {
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.lineJoin = "round";
  ctx.beginPath(); let pen = false;
  pts.forEach(p => {
    const v = p[key];
    if (v == null) { pen = false; return; }
    const x = xOf(p.t), y = yOf(v);
    if (!pen) { ctx.moveTo(x, y); pen = true; } else ctx.lineTo(x, y);
  });
  ctx.stroke();
  // glow dot at the last point
  const last = [...pts].reverse().find(p => p[key] != null);
  if (last) { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(xOf(last.t), yOf(last[key]), 2.6, 0, 7); ctx.fill(); }
}

function drawMarker(ctx, x, y, buy, win) {
  const c = win ? "#34e0a1" : "#ff5d73", s = 9, off = 13;  // bigger + offset off the line for clarity
  const ty = buy ? y + off : y - off;        // buys sit below the price point, sells above
  ctx.save();
  ctx.shadowColor = c; ctx.shadowBlur = 9;
  ctx.beginPath();
  if (buy) { ctx.moveTo(x, ty - s); ctx.lineTo(x - s * 0.92, ty + s * 0.78); ctx.lineTo(x + s * 0.92, ty + s * 0.78); }
  else { ctx.moveTo(x, ty + s); ctx.lineTo(x - s * 0.92, ty - s * 0.78); ctx.lineTo(x + s * 0.92, ty - s * 0.78); }
  ctx.closePath();
  ctx.fillStyle = c; ctx.fill();
  ctx.restore();
  ctx.lineWidth = 1.6; ctx.strokeStyle = "rgba(255,255,255,0.9)"; ctx.stroke();
  // thin connector from the marker to the exact fill point on the price line
  ctx.strokeStyle = c; ctx.globalAlpha = 0.5; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, buy ? ty - s : ty + s); ctx.stroke(); ctx.globalAlpha = 1;
}

function drawBasis(ctx, pts, xOf, top, bot, plotL, plotR, reqEdge) {
  const bvals = pts.filter(p => p.aster != null && p.hl != null)
    .map(p => ({ t: p.t, b: (p.aster - p.hl) / p.hl * 1e4 }));
  ctx.fillStyle = "#5d6678"; ctx.font = "9px ui-monospace,monospace"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText("basis (bps)", plotL + 2, top + 6);
  if (bvals.length < 2) return;
  let amax = Math.max(2, ...bvals.map(v => Math.abs(v.b)));
  if (reqEdge) amax = Math.max(amax, reqEdge + 2);
  const yB = (b) => top + (1 - (b + amax) / (2 * amax)) * (bot - top);
  // required-edge band
  if (reqEdge) {
    ctx.fillStyle = "rgba(154,140,255,0.08)";
    ctx.fillRect(plotL, yB(reqEdge), plotR - plotL, yB(-reqEdge) - yB(reqEdge));
    ctx.strokeStyle = "rgba(154,140,255,0.35)"; ctx.setLineDash([3, 3]);
    [reqEdge, -reqEdge].forEach(b => { ctx.beginPath(); ctx.moveTo(plotL, yB(b)); ctx.lineTo(plotR, yB(b)); ctx.stroke(); });
    ctx.setLineDash([]);
  }
  // zero line
  ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.beginPath(); ctx.moveTo(plotL, yB(0)); ctx.lineTo(plotR, yB(0)); ctx.stroke();
  // basis line
  ctx.strokeStyle = "#9a8cff"; ctx.lineWidth = 1.3; ctx.beginPath();
  bvals.forEach((v, i) => { const x = xOf(v.t), y = yB(v.b); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
}

function drawHover(ctx, markers, hov, plotL, plotR, top, bot) {
  if (hov.x < plotL || hov.x > plotR) { hideTip(); return; }
  ctx.save(); ctx.strokeStyle = "rgba(255,255,255,0.16)"; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(hov.x, top); ctx.lineTo(hov.x, bot); ctx.stroke(); ctx.restore();
  let best = null, bd = 1e9;
  markers.forEach(m => { const d = Math.hypot(m.x - hov.x, m.y - hov.y); if (d < bd) { bd = d; best = m; } });
  if (best && bd < 26) {
    ctx.beginPath(); ctx.arc(best.x, best.y, 8, 0, 7);
    ctx.strokeStyle = best.fl.win ? "#34e0a1" : "#ff5d73"; ctx.lineWidth = 1.5; ctx.stroke();
    showTip(best, plotR);
  } else hideTip();
}

function showTip(m, plotR) {
  const tip = $("chartTip"), fl = m.fl;
  tip.innerHTML = `<div class="tip-h ${fl.win ? "win" : "loss"}">${fl.win ? "WIN" : "LOSS"} · ${esc(fl.side)} ${esc(fl.coin)}</div>
    <div>${f.num(fl.qty, 4)} @ ${f.usd(fl.price, 3)}</div>
    <div>net <b class="${f.sign(fl.net)}">${f.money(fl.net, 4)}</b>${fl.basis_bps != null ? " · " + f.bps(fl.basis_bps, 1) : ""}</div>
    <div class="muted">${fl.latency_ms != null ? fl.latency_ms + "ms" : ""}${fl.time ? " · " + utcHMS(fl.time) + " UTC" : ""}</div>`;
  tip.hidden = false;
  const tw = tip.offsetWidth || 150;
  tip.style.left = Math.max(4, Math.min(m.x + 12, plotR - tw)) + "px";
  tip.style.top = Math.max(4, m.y - 12) + "px";
}
function hideTip() { const t = $("chartTip"); if (t) t.hidden = true; }

// ---- auto-cycling zoom (15m·1h·4h every ~2min; 5m focus on a fresh fill) ----
const ROTATE_MS = 120000;       // ~2 minutes per window
const FILL_FOCUS_MIN = 5;       // a fill within this many minutes -> the short slot is 5m
let rotateTimer = null;

function recentFill() { return ui.lastFillTime && (Date.now() - ui.lastFillTime) < FILL_FOCUS_MIN * 60000; }
function rotationWindows() { return [recentFill() ? 5 : 15, 60, 240]; }   // short slot adapts to fills

function applyChartWindow(w) {
  ui.chartWindow = w;
  document.querySelectorAll("#zoom .zoom-btn[data-w]").forEach(x => x.classList.toggle("on", +x.dataset.w === w));
  if (ui.lastState) renderChart(ui.lastState);
}
function scheduleRotate() {
  clearTimeout(rotateTimer);
  if (!ui.autoRotate) return;
  rotateTimer = setTimeout(() => {
    ui.rotateIdx = (ui.rotateIdx + 1) % 3;
    applyChartWindow(rotationWindows()[ui.rotateIdx]);
    scheduleRotate();
  }, ROTATE_MS);
}
function focusFill() {           // jump straight to the tight 5m view and hold it a full interval
  ui.lastFillTime = Date.now();
  ui.rotateIdx = 0;
  applyChartWindow(5);
  scheduleRotate();
}

// hover + zoom + resize + rotation wiring
(function chartInteractions() {
  const cv = $("priceChart"); if (!cv) return;
  const redraw = () => { if (!rafPending) { rafPending = true; requestAnimationFrame(() => { rafPending = false; if (ui.lastState) renderChart(ui.lastState); }); } };
  cv.addEventListener("mousemove", (e) => { const r = cv.getBoundingClientRect(); chartHover = { x: e.clientX - r.left, y: e.clientY - r.top }; redraw(); });
  cv.addEventListener("mouseleave", () => { chartHover = null; hideTip(); redraw(); });
  window.addEventListener("resize", redraw);

  // manual zoom click: pause the rotation timer for one interval, snap rotateIdx to this window
  document.querySelectorAll("#zoom .zoom-btn[data-w]").forEach(b => {
    b.addEventListener("click", () => {
      const w = +b.dataset.w;
      const idx = rotationWindows().indexOf(w);
      ui.rotateIdx = idx >= 0 ? idx : ui.rotateIdx;
      applyChartWindow(w);
      scheduleRotate();             // hold the manual choice for a full interval before cycling on
    });
  });
  // auto toggle
  const auto = $("autoBtn");
  if (auto) auto.addEventListener("click", () => {
    ui.autoRotate = !ui.autoRotate;
    auto.classList.toggle("auto-on", ui.autoRotate);
    if (ui.autoRotate) scheduleRotate(); else clearTimeout(rotateTimer);
  });

  applyChartWindow(rotationWindows()[0]);   // start at the short slot
  scheduleRotate();
})();

// fast price feed: refresh just the chart ~every second (fills/stats stay on the 5s /api/state poll).
// Requests the series for the CURRENT zoom window so the backend serves 1s ticks (downsampled)
// wherever they're available, falling back to 1m candles only for older gaps.
async function fetchPrices() {
  if (!ui.lastState) return;
  try {
    const r = await fetch(`/api/prices?minutes=${ui.chartWindow || 60}`, { cache: "no-store" });
    if (r.ok) { ui.lastState.prices = await r.json(); renderChart(ui.lastState); }
  } catch (e) { /* ignore — next poll recovers */ }
}
async function pricePoll() {
  await fetchPrices();
  setTimeout(pricePoll, 1000);
}

poll();
pricePoll();
preloadTriggerGifs();
setInterval(tickLastTrade, 1000);   // keep "time since last trade" live between the 5s state polls
