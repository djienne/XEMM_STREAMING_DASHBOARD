#!/usr/bin/env python3
"""XEMM live dashboard server.

A tiny stdlib HTTP server (no third-party web framework) that:

  * runs three independent background refreshers — fast exchange snapshot (~5s), VPS health
    (~12s), and the heavy trade_stats run (~45s, or immediately when a new fill is detected) —
    and serves the latest cached results so the browser's 5s poll is always instant;
  * detects newly-completed XEMM rounds and turns them into win/loss EVENTS for the UI's
    trade-trigger toasts; and
  * serves the static dashboard (index.html / css / js) and the gifs/ folder.

Security: binds to 127.0.0.1 only and never exposes any secret — signing happens inside
trade_stats.py; only derived public data (positions, PnL, public addresses) leaves the process.

Run:  python server.py            (then open http://127.0.0.1:8787)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import collectors
import vps_health
import ws_prices

HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------- config ----------------------
def load_config() -> dict:
    # config.json holds your real host/IP/key path and is gitignored; a fresh clone falls back to
    # the committed config.example.json (loopback placeholders) so the dashboard still starts.
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        path = os.path.join(HERE, "config.example.json")
    with open(path) as f:
        cfg = json.load(f)
    # Env overrides let the Docker container retarget bind/root without touching config.json,
    # so config.json stays the single source of truth for a native run (no env -> identical).
    if os.environ.get("XEMM_BIND_HOST"):
        cfg["bind_host"] = os.environ["XEMM_BIND_HOST"]
    if os.environ.get("XEMM_BIND_PORT"):
        cfg["bind_port"] = int(os.environ["XEMM_BIND_PORT"])
    if os.environ.get("XEMM_ALLOW_REMOTE_BIND"):
        cfg["allow_remote_bind"] = os.environ["XEMM_ALLOW_REMOTE_BIND"] not in ("", "0", "false", "False")
    root = os.environ.get("XEMM_TRADING_ROOT") or cfg.get("trading_root", "..")
    cfg["_root_abs"] = os.path.abspath(root if os.path.isabs(root) else os.path.join(HERE, root))
    return cfg


def local_pnl_fallback(root: str) -> str | None:
    """Parse the LOCAL scripts/pnl.py `container_start_time() or "..."` constant — the only sane
    ultimate fallback when the VPS is unreachable (never invent a third distinct cutoff)."""
    try:
        text = open(os.path.join(root, "scripts", "pnl.py"), encoding="utf-8").read()
        m = re.search(r'container_start_time\(\)\s*or\s*"([^"]+)"', text)
        return m.group(1) if m else None
    except OSError:
        return None


def parse_live_config(root: str) -> dict:
    """Best-effort read of the live edge/quote/breaker settings from config-live-hype.toml (for display)."""
    path = os.path.join(root, "config-live-hype.toml")
    out: dict = {"path": "config-live-hype.toml"}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return out

    def num(key):
        m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"?([\d.]+)"?', text, re.M)
        return float(m.group(1)) if m else None

    edge = {k: num(k) for k in ("min_net_profit_bps", "slippage_buffer_bps", "latency_buffer_bps",
                                "basis_buffer_bps", "funding_buffer_bps",
                                "aster_maker_fee_bps", "hyperliquid_taker_fee_bps")}
    required = sum(v for k, v in edge.items()
                   if v is not None and k.endswith(("profit_bps", "buffer_bps")))
    markets = re.findall(r'aster_symbol\s*=\s*"([^"]+)"', text)
    hl_coins = re.findall(r'hl_coin\s*=\s*"([^"]+)"', text)
    # Position cap = per-leg capital × leverage (quote_engine PositionContext). When |inventory|
    # reaches it the adding side has zero headroom, so the bot quotes only the reducing side —
    # the "capital fully deployed, reduce-only" state surfaced on the dashboard.
    lev = num("leverage") or 1.0
    a_cap, h_cap = num("aster_capital_usd"), num("hyperliquid_capital_usd")
    out.update({
        "edge": edge,
        "required_edge_bps": round(required, 2) if required else None,
        "roundtrip_fee_bps": round((edge.get("aster_maker_fee_bps") or 0)
                                   + (edge.get("hyperliquid_taker_fee_bps") or 0), 2),
        "desired_notional": num("desired_notional"),
        "breaker_max_loss_usd": num("max_cumulative_loss_usdc"),
        "position_cap": {
            "aster_notional": round(a_cap * lev, 2) if a_cap else None,
            "hl_notional": round(h_cap * lev, 2) if h_cap else None,
            "enforced": bool(re.search(r'^\s*enforce_position_cap\s*=\s*true', text, re.M)),
        },
        "markets": markets,
        "hl_coins": hl_coins,
    })
    return out


# ----------------------------------------------------------------- shared state ----------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.live = {"ok": False, "note": "starting…"}
        self.health = {"reachable": False, "note": "starting…"}
        self.stats = {"ok": False, "note": "starting…"}
        self.since = {"value": None, "source": "unknown", "detail": "resolving…"}
        self.live_config = {}
        self.api_latency = {"ok": False, "note": "measuring…"}   # VPS -> exchange API latency (~20min)
        self.price_hist = []                   # 1-minute closes, both venues (re-seeded periodically)
        # fine 1s ticks kept in memory for serving: ~6h (max chart window is 4h, so older is
        # discarded from the live working set). The durable on-disk archive keeps everything.
        self.price_live = deque(maxlen=21600)
        self.chart_meta = {}
        # (max_minutes, bucket_seconds): chart resolution per zoom window (overridden from config)
        self.chart_res = [(15, 1), (60, 10), (240, 30)]
        self.events = deque(maxlen=80)
        self.event_seq = 0
        self.seen_round_keys: set[str] = set()
        self.seen_fill_ids: set[str] = set()
        self.first_stats = True
        self.first_fills = True
        self.heavy_signal = threading.Event()
        self.started_at = int(time.time() * 1000)

    def snapshot(self) -> dict:
        with self.lock:
            stats = dict(self.stats)
            stats.pop("rounds", None)  # large; the derived.feed has what the UI needs
            return {
                "server_time": int(time.time() * 1000),
                "started_at": self.started_at,
                "since": self.since,
                "live": self.live,
                "health": self.health,
                "stats": stats,
                "live_config": self.live_config,
                "api_latency": self.api_latency,
                "events": list(self.events),
            }

    def prices_snapshot(self, minutes=None, max_points=2000) -> dict:
        """Chart series for the requested window at an ADAPTIVE resolution: full 1s for short
        windows, coarser (e.g. 10s @ 1h, 30s @ 4h) for wide ones. Saved 1s ticks resampled to the
        window's bucket; 1-minute candles fall back for any older part. `max_points` is a safety cap."""
        with self.lock:
            hist = self.price_hist
            ticks = list(self.price_live)
            meta = self.chart_meta
            res = self.chart_res
        bucket_s = 0
        if minutes:
            cutoff = int(time.time() * 1000) - int(minutes) * 60_000
            ticks = [t for t in ticks if t["t"] >= cutoff]
            hist = [p for p in hist if p["t"] >= cutoff]     # filter the candle fallback too
            bucket_s = next((bs for mx, bs in res if minutes <= mx), (res[-1][1] if res else 60))
        ticks = _resample(ticks, bucket_s * 1000)            # adaptive thinning by time bucket
        ticks = _downsample(ticks, max_points)               # safety cap only
        return {"server_time": int(time.time() * 1000), "meta": {**meta, "bucket_s": bucket_s},
                "hist": hist, "live": ticks}

    def add_event(self, ev: dict):
        with self.lock:
            self.event_seq += 1
            ev["id"] = self.event_seq
            ev["created_at"] = int(time.time() * 1000)
            self.events.append(ev)


# ----------------------------------------------------------------- refresh loops ---------------
def _supervise(fn, cfg, state: State):
    """Run a refresher loop forever; if it ever throws out (e.g. a bad config key, or a
    SystemExit raised by trade_stats._req on an API failure), log and restart with backoff
    instead of letting the thread die silently and freeze a card."""
    while True:
        try:
            fn(cfg, state)
            return  # a loop returning normally is intentional (it shouldn't, but don't spin)
        except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
            print(f"[{fn.__name__}] crashed: {e!r} — restarting in 5s", file=sys.stderr)
            time.sleep(5)


def live_loop(cfg, state: State):
    root = cfg["_root_abs"]
    period = cfg["refresh"]["live_s"]
    while True:
        try:
            snap = collectors.fast_snapshot(root)
            with state.lock:
                state.live = snap
            # detect genuinely new maker fills -> kick the heavy refresh for prompt win/loss
            ids = {f["id"] for f in snap.get("recent_fills", [])}
            with state.lock:
                if state.first_fills:
                    state.seen_fill_ids |= ids
                    state.first_fills = False
                    fresh = set()
                else:
                    fresh = ids - state.seen_fill_ids
                    state.seen_fill_ids |= ids
            if fresh:
                state.heavy_signal.set()  # a fill just happened — refresh stats now
        except Exception as e:  # noqa: BLE001
            with state.lock:
                state.live = {"ok": False, "error": repr(e), "ts": int(time.time() * 1000)}
        time.sleep(period)


def health_loop(cfg, state: State):
    period = cfg["refresh"]["health_s"]
    while True:
        try:
            h = vps_health.probe(cfg)
            with state.lock:
                state.health = h
                if h.get("reachable") and h.get("since", {}).get("value"):
                    state.since = h["since"]
        except Exception as e:  # noqa: BLE001
            with state.lock:
                state.health = {"reachable": False, "error": repr(e),
                                "checked_at": vps_health._now_iso()}
        time.sleep(period)


def latency_loop(cfg, state: State):
    """Measure VPS -> exchange API latency on the VPS, at most ~once every 20 minutes."""
    period = cfg["refresh"].get("latency_s", 1200)
    while True:
        try:
            lat = vps_health.measure_api_latency(cfg)
            with state.lock:
                state.api_latency = lat
        except Exception as e:  # noqa: BLE001
            with state.lock:
                state.api_latency = {"ok": False, "error": repr(e)[:200],
                                     "checked_at": vps_health._now_iso()}
        time.sleep(period)


def price_loop(cfg, state: State):
    """Maintain the chart series: a 4h 1-minute history (re-seeded ~every 90s so all zoom levels
    always have data) plus a fine 5s live tail for sub-minute detail near 'now'."""
    root = cfg["_root_abs"]
    period = cfg["refresh"].get("price_s", 1)   # fast price feed (fills/stats stay on their own cadences)
    ch = cfg.get("chart", {})
    coin = ch.get("coin", "HYPE")
    symbol = ch.get("symbol", "HYPEUSDT")
    hist_minutes = int(ch.get("history_minutes", 240))

    # Durable second-resolution tick archive in SQLite (ts PRIMARY KEY, indexed for range queries):
    # exchanges only serve 1-minute candles historically, so we persist every 1s mid here to rebuild
    # a high-res chart later. Only the price_loop thread touches this connection. Recover the recent
    # fine tail across restarts.
    db_path = ch.get("tick_db", "data/prices.sqlite")
    db_path = db_path if os.path.isabs(db_path) else os.path.join(HERE, db_path)
    db = _open_tick_db(db_path)
    recovered = _recover_ticks(db, state.price_live.maxlen)
    if recovered:
        with state.lock:
            for r in recovered:
                state.price_live.append(r)

    # Prices come primarily from WebSockets; REST is fallback + a periodic sanity check.
    feed = ws_prices.WsPriceFeed(coin, symbol)
    feed.start()
    last_seed = 0.0
    last_sanity = 0.0
    while True:
        now = time.time()
        try:
            if now - last_seed > 90:
                hist = collectors.seed_prices(root, coin, symbol, hist_minutes)
                if hist:
                    with state.lock:
                        state.price_hist = hist
                        state.chart_meta = {"coin": coin, "symbol": symbol,
                                            "history_minutes": hist_minutes}
                    last_seed = now

            a, h, ages = feed.mids()
            src = "ws"
            do_sanity = now - last_sanity > 120
            if a is None or h is None or do_sanity:
                rest = collectors.price_tick(root, coin, symbol)
                if do_sanity:                      # REST sanity check: if WS drifts from REST, trust REST
                    last_sanity = now
                    if a and rest.get("aster") and abs(a - rest["aster"]) / rest["aster"] > 0.002:
                        print(f"[price] WS/REST aster divergence ws={a} rest={rest['aster']} — using REST", file=sys.stderr)
                        a, src = rest["aster"], "rest(diverged)"
                    if h and rest.get("hl") and abs(h - rest["hl"]) / rest["hl"] > 0.002:
                        print(f"[price] WS/REST hl divergence ws={h} rest={rest['hl']} — using REST", file=sys.stderr)
                        h, src = rest["hl"], "rest(diverged)"
                if a is None:
                    a, src = rest.get("aster"), "rest"
                if h is None:
                    h, src = rest.get("hl"), "rest"

            tick = {"t": int(now * 1000), "aster": a, "hl": h}
            if tick["aster"] or tick["hl"]:
                with state.lock:
                    state.price_live.append(tick)
                    state.chart_meta = {**state.chart_meta, "feed": src,
                                        "ws_age_s": ages.get("aster_age"), "ws_hl_age_s": ages.get("hl_age")}
                _save_tick(db, tick)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(period)


def _open_tick_db(path):
    """Open (creating if needed) the SQLite tick archive. None on failure."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path)               # single-threaded: only price_loop uses it
        conn.execute("PRAGMA journal_mode=WAL")    # lets analysis tools read while we append
        conn.execute("PRAGMA synchronous=FULL")    # fsync every commit (1/s) -> no tick lost on power loss
        conn.execute("CREATE TABLE IF NOT EXISTS ticks (ts INTEGER PRIMARY KEY, aster REAL, hl REAL)")
        conn.commit()
        return conn
    except sqlite3.Error as e:
        print(f"[archive] tick DB open failed ({path}): {e!r} — ticks NOT being archived", file=sys.stderr)
        return None


def _save_tick(conn, tick):
    if conn is None:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO ticks(ts, aster, hl) VALUES (?, ?, ?)",
                     (tick["t"], tick.get("aster"), tick.get("hl")))
        conn.commit()
    except sqlite3.Error:
        pass


def _recover_ticks(conn, max_rows):
    """Load the last `max_rows` ticks (chronological) from the archive for restart recovery."""
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT ts, aster, hl FROM ticks ORDER BY ts DESC LIMIT ?",
                            (max_rows,)).fetchall()
    except sqlite3.Error:
        return []
    return [{"t": r[0], "aster": r[1], "hl": r[2]} for r in reversed(rows)]


# ---- durable trade-history archive (the API/WS may limit history long-term; keep our own) ----
_ROUND_COLS = ("order_id", "trade_id", "coin", "side", "qty", "aster_px", "hedge_px", "basis",
               "round_net", "latency_ms", "hedge_method", "aster_time", "hedge_time",
               "aster_comm", "hedge_fee", "hedge_pnl")
# Upsert that NEVER overwrites a stored non-null with a null: a later run that transiently misses
# the HL hedge keeps the previously-recorded hedge fields (COALESCE(new, existing)).
_ROUND_SET = ", ".join(f"{c}=COALESCE(excluded.{c}, rounds.{c})" for c in _ROUND_COLS[2:])
_ROUND_UPSERT = (f"INSERT INTO rounds ({','.join(_ROUND_COLS)}) "
                 f"VALUES ({','.join('?' * len(_ROUND_COLS))}) "
                 f"ON CONFLICT(order_id, trade_id) DO UPDATE SET {_ROUND_SET}")


def _open_rounds_db(path):
    """Open the trade-history table (own connection; WAL lets it coexist with the tick writer)."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS rounds (
            order_id TEXT, trade_id TEXT, coin TEXT, side TEXT, qty REAL, aster_px REAL,
            hedge_px REAL, basis REAL, round_net REAL, latency_ms INTEGER, hedge_method TEXT,
            aster_time INTEGER, hedge_time INTEGER, aster_comm REAL, hedge_fee REAL, hedge_pnl REAL,
            PRIMARY KEY (order_id, trade_id))""")
        conn.commit()
        return conn
    except sqlite3.Error as e:
        print(f"[archive] rounds DB open failed ({path}): {e!r} — trade history NOT being archived", file=sys.stderr)
        return None


def _persist_rounds(conn, rounds):
    """Upsert matched rounds, deduped by (order_id, trade_id) and validated. Idempotent: re-running
    the same window just refreshes each row (a later run may improve a hedge match). Returns count."""
    if conn is None or not rounds:
        return 0
    n = 0
    for r in rounds:
        oid, tid = r.get("order_id"), r.get("trade_id")
        # validation: a real round needs an id pair + a sane qty/price (skip garbage)
        if oid is None or tid is None or not r.get("qty") or not r.get("aster_px"):
            continue
        vals = (str(oid), str(tid), r.get("coin"), r.get("aster_side"), r.get("qty"),
                r.get("aster_px"), r.get("hedge_px"), r.get("basis"), r.get("round_net"),
                r.get("latency_ms"), r.get("hedge_method"), r.get("aster_time"), r.get("hedge_time"),
                r.get("aster_comm"), r.get("hedge_fee"), r.get("hedge_pnl"))
        try:
            conn.execute(_ROUND_UPSERT, vals)
            n += 1
        except sqlite3.Error:
            continue
    try:
        conn.commit()
    except sqlite3.Error as e:
        print(f"[archive] rounds commit failed: {e!r}", file=sys.stderr)
    return n


def _resample(ticks, bucket_ms):
    """Keep the last tick in each `bucket_ms` time bucket — adaptive resolution by zoom window.
    bucket_ms <= 0 means full 1-second resolution (no thinning)."""
    if bucket_ms <= 0 or len(ticks) <= 2:
        return ticks
    out, cur = [], None
    for t in ticks:                 # ticks are chronological
        b = t["t"] // bucket_ms
        if b != cur:
            out.append(t)
            cur = b
        else:
            out[-1] = t             # latest tick in this bucket represents it
    return out


def _downsample(ticks, max_points):
    """Evenly thin `ticks` to <= max_points, always keeping the last point. Keeps payloads light
    for wide windows while preserving far more detail than 1-minute candles."""
    n = len(ticks)
    if n <= max_points or max_points <= 1:
        return ticks
    stride = n / (max_points - 1)            # reserve one slot for the final tick -> never exceed max
    out, i = [], 0.0
    while int(i) < n and len(out) < max_points - 1:
        out.append(ticks[int(i)])
        i += stride
    if out and out[-1] is not ticks[-1]:
        out.append(ticks[-1])
    return out


def stats_loop(cfg, state: State):
    root = cfg["_root_abs"]
    period = cfg["refresh"]["stats_s"]
    fallback = local_pnl_fallback(root)        # mirrors local pnl.py; no invented third cutoff
    ch = cfg.get("chart", {})
    db_path = ch.get("tick_db", "data/prices.sqlite")
    db_path = db_path if os.path.isabs(db_path) else os.path.join(HERE, db_path)
    rounds_db = _open_rounds_db(db_path)        # durable trade-history archive (own connection)

    def since_value():
        with state.lock:
            return state.since.get("value")

    # wait briefly for the first since-resolution from the health loop
    for _ in range(20):
        if since_value():
            break
        time.sleep(0.5)
    while True:
        # clear BEFORE the work so a fill detected mid-run survives to trigger the next pass
        state.heavy_signal.clear()
        since = since_value() or fallback
        if since:
            try:
                d = collectors.heavy_stats(root, since)
                if d.get("ok"):
                    with state.lock:
                        state.stats = d
                    _reconcile_events(state, d)
                    _persist_rounds(rounds_db, d.get("rounds") or [])   # archive trade history
                else:
                    _mark_stats_stale(state, d.get("error"), since)
            except Exception as e:  # noqa: BLE001
                _mark_stats_stale(state, repr(e), since)
        else:
            _mark_stats_stale(state, "since unresolved (VPS unreachable, no local pnl.py fallback)", None)
        # sleep until period elapses OR a new fill signals an early refresh
        state.heavy_signal.wait(timeout=period)


def _mark_stats_stale(state: State, error, since):
    """Keep serving the last GOOD stats with a stale flag rather than blanking every P&L KPI."""
    with state.lock:
        if state.stats.get("ok"):
            state.stats = {**state.stats, "stale": True, "error": error,
                           "stale_at": int(time.time() * 1000)}
        else:
            state.stats = {"ok": False, "error": error, "since": since}


def _reconcile_events(state: State, d: dict):
    feed = (d.get("derived") or {}).get("feed") or []
    with state.lock:
        first = state.first_stats
        keys_now = {f["key"] for f in feed}
        new = [f for f in feed if f["key"] not in state.seen_round_keys]
        state.seen_round_keys |= keys_now
        state.first_stats = False
    if first:
        return  # seed silently on first load; history shows in the trade log, no toast storm
    for f in sorted(new, key=lambda x: x.get("time") or 0):  # oldest -> newest
        state.add_event({
            "kind": "win" if f["win"] else "loss",
            "coin": f["coin"], "side": f["side"], "qty": f["qty"], "price": f["price"],
            "net": f["net"], "time": f["time"], "latency_ms": f.get("latency_ms"),
            "basis_bps": f.get("basis_bps"), "hedge_method": f.get("hedge_method"),
            "key": f["key"],
        })


# ----------------------------------------------------------------- http handler ----------------
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".gif": "image/gif", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".ico": "image/x-icon", ".mp3": "audio/mpeg", ".wav": "audio/wav",
}


def make_handler(cfg, state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "XEMMDash/1.0"

        def log_message(self, *args):  # quiet; we have our own startup banner
            pass

        def _send(self, code, body: bytes, ctype: str, cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not cache:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json")

        def _origin_ok(self):
            """Reject cross-origin mutating requests (CSRF). Same-origin or no Origin/Referer ok.
            Parses the URL and compares hostname + port EXACTLY (a prefix test would let
            http://127.0.0.1:8787.evil.com through)."""
            o = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if not o:
                return True
            try:
                u = urlparse(o)
            except ValueError:
                return False
            port = int(cfg.get("bind_port", 8787))
            ok_hosts = {"127.0.0.1", "localhost", "::1", cfg.get("bind_host")}
            return (u.hostname in ok_hosts
                    and (u.port or (443 if u.scheme == "https" else 80)) == port)

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            path = urlparse(self.path).path
            try:
                # mutating route: POST only + same-origin (no GET-via-<img> CSRF/DoS)
                if path in ("/api/refresh", "/api/refresh/"):
                    if method != "POST":
                        return self._json({"error": "use POST"}, 405)
                    if not self._origin_ok():
                        return self._json({"error": "cross-origin refused"}, 403)
                    state.heavy_signal.set()
                    return self._json({"ok": True})
                if method == "POST":
                    return self._json({"error": "method not allowed"}, 405)
                # read-only routes (GET/HEAD)
                if path in ("/api/state", "/api/state/"):
                    return self._json(state.snapshot())
                if path in ("/api/prices", "/api/prices/"):
                    minutes = None
                    try:
                        minutes = int(parse_qs(urlparse(self.path).query).get("minutes", [""])[0])
                    except (TypeError, ValueError):
                        minutes = None
                    return self._json(state.prices_snapshot(minutes))
                if path == "/" or path == "/index.html":
                    return self._serve_file(os.path.join(HERE, "static", "index.html"))
                if path.startswith("/static/"):
                    return self._serve_under(os.path.join(HERE, "static"), path[len("/static/"):])
                if path.startswith("/gifs/"):
                    return self._serve_under(os.path.join(HERE, "gifs"), path[len("/gifs/"):])
                if path == "/favicon.ico":
                    return self._serve_file(os.path.join(HERE, "static", "favicon.svg"))
                return self._json({"error": "not found", "path": path}, 404)
            except BrokenPipeError:
                pass
            except Exception as e:  # noqa: BLE001
                try:
                    return self._json({"error": repr(e)}, 500)
                except Exception:  # noqa: BLE001
                    pass

        def _serve_under(self, base, rel):
            # prevent path traversal: resolved path must stay inside `base`
            target = os.path.normpath(os.path.join(base, rel))
            if not target.startswith(os.path.normpath(base) + os.sep) and target != os.path.normpath(base):
                return self._json({"error": "forbidden"}, 403)
            return self._serve_file(target)

        def _serve_file(self, target):
            if not os.path.isfile(target):
                return self._json({"error": "not found"}, 404)
            ext = os.path.splitext(target)[1].lower()
            ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
            with open(target, "rb") as f:
                body = f.read()
            cache = ext in (".svg", ".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav", ".ico")
            self._send(200, body, ctype, cache=cache)

    return Handler


# ----------------------------------------------------------------- main ------------------------
def main():
    cfg = load_config()
    state = State()
    try:
        state.live_config = parse_live_config(cfg["_root_abs"])
    except Exception:  # noqa: BLE001
        state.live_config = {}
    rb = cfg.get("chart", {}).get("resolution_buckets")
    if rb:
        try:
            state.chart_res = sorted((int(b["max_minutes"]), int(b["bucket_s"])) for b in rb)
        except (KeyError, ValueError, TypeError):
            pass

    for fn in (health_loop, live_loop, stats_loop, price_loop, latency_loop):
        threading.Thread(target=_supervise, args=(fn, cfg, state), daemon=True).start()

    host, port = cfg.get("bind_host", "127.0.0.1"), int(cfg.get("bind_port", 8787))
    # The dashboard exposes live account state + a refresh control — refuse to bind a non-loopback
    # interface (e.g. a stray 0.0.0.0) unless explicitly allowed.
    if host not in ("127.0.0.1", "::1", "localhost") and not cfg.get("allow_remote_bind"):
        print(f"[server] refusing non-loopback bind {host!r}; using 127.0.0.1 "
              f"(set allow_remote_bind:true in config.json to override)", file=sys.stderr)
        host = "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, state))
    url = f"http://{host}:{port}"
    print("=" * 60)
    print("  XEMM live dashboard")
    print(f"  serving      {url}")
    print(f"  trading root {cfg['_root_abs']}")
    print(f"  VPS          {cfg['vps']['ssh_target']}  ({cfg['vps']['process_name']})")
    print(f"  refresh      live {cfg['refresh']['live_s']}s · "
          f"health {cfg['refresh']['health_s']}s · stats {cfg['refresh']['stats_s']}s")
    print("=" * 60)
    print(f"  open {url} in your browser.  Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        httpd.shutdown()


if __name__ == "__main__":
    main()
