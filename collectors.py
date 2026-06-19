#!/usr/bin/env python3
"""Direct-from-exchange data collectors for the XEMM live dashboard.

Order history, PnL and stats come STRAIGHT from Aster + Hyperliquid — never from the replay
tape or the modelling DB. We reuse `scripts/trade_stats.py` verbatim (same EIP-712 signing, same
deterministic hedge-cloid matching) so the numbers are byte-for-byte what `pnl.py` would print.

Two tiers:

  * `fast_snapshot()`  — a handful of cheap signed calls (positions, equity, mids, open orders,
    recent maker fills). Cheap enough to run on the 5-second live cadence.
  * `heavy_stats()`    — shells out to `scripts/trade_stats.py --json` (the full paginated
    fill history + hedge matching + latency). Slower; run on a background timer and cached.

`heavy_stats()` also derives the SAME "net P&L" headline `pnl.py` shows: when the book is
delta-neutral the venue-reported realized PnL is misleading (HL only books closedPnl on close),
so the per-round matched sum is authoritative.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from decimal import Decimal

_TS = None  # cached trade_stats module


def load_trade_stats(root: str):
    """Import the repo's scripts/trade_stats.py once and cache it. `root` = the trading repo root."""
    global _TS
    if _TS is not None:
        return _TS
    scripts = os.path.join(root, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    _TS = importlib.import_module("trade_stats")
    return _TS


def _coin_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# --- Aster equity: signed multi-collateral balance -------------------------------------------
# Aster is multi-collateral cross-margin and can settle fees/losses into a negative USDT row while
# USDC remains positive. Filtering to positive rows overstates account equity. Mirror the trading
# repo's trade_stats.aster_equity(): signed USDC + USDF + USDT, plus positionRisk uPnL.
_ASTER_EQUITY_ASSETS = ("USDC", "USDF", "USDT")


def aster_equity_full(ts, roles) -> float:
    """Aster equity: signed USDC/USDF/USDT collateral + open-position uPnL."""
    bal = Decimal(0)
    for row in ts.aster_get("/fapi/v3/balance", {}, roles):
        if str(row.get("asset", "")).upper() not in _ASTER_EQUITY_ASSETS:
            continue
        b = Decimal(str(row.get("balance", "0")))
        bal += b
    upnl = Decimal(0)
    for row in ts.aster_get("/fapi/v3/positionRisk", {}, roles):
        upnl += Decimal(str(row.get("unRealizedProfit", "0")))
    return float(bal + upnl)


# ----------------------------------------------------------------- fast tier (5s) ------------
def fast_snapshot(root: str) -> dict:
    """Cheap signed snapshot: per-coin position/delta, equity, open orders, recent maker fills.

    Never raises; on a venue error returns {'ok': False, 'error': ...} with whatever was gathered.
    """
    ts = load_trade_stats(root)
    out: dict = {"ok": True, "ts": int(time.time() * 1000), "errors": []}
    try:
        roles = ts.aster_roles()
        user, signer, _ = roles
        hl_user = ts.load_env("hyperliquid.env")["subaccount_address"]
        out["accounts"] = {"aster_user": user, "aster_signer": signer, "hl_user": hl_user}
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        return {"ok": False, "error": f"creds/env: {e!r}", "ts": int(time.time() * 1000)}

    # --- mids (one public call) ---
    mids = {}
    try:
        mids = {k: _f(v) for k, v in ts.hl_post({"type": "allMids"}).items()}
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        out["errors"].append(f"mids: {e!r}")

    # --- positions + equity ---
    a_pos, a_eq = {}, None
    h_pos, h_eq = {}, None
    try:
        a_pos = ts.aster_positions(roles)              # {symbol: (Decimal amt, Decimal upnl)}
        a_eq = aster_equity_full(ts, roles)            # signed USDC+USDF+USDT (+uPnL)
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        out["errors"].append(f"aster positions/equity: {e!r}")
    try:
        h_eq_d, h_pos = ts.hl_state(hl_user)           # (Decimal equity, {coin: (Decimal szi, upnl)})
        h_eq = float(h_eq_d)
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        out["errors"].append(f"hl state: {e!r}")

    # --- open orders (resting maker quotes) ---
    a_orders, h_orders = [], []
    try:
        for o in ts.aster_get("/fapi/v3/openOrders", {}, roles) or []:
            coin = _coin_of(o.get("symbol", ""))
            px = _f(o.get("price"))
            mid = mids.get(coin, 0.0)
            dist = ((px - mid) / mid * 1e4) if (mid and px) else None
            a_orders.append({
                "venue": "aster", "coin": coin, "symbol": o.get("symbol"),
                "side": o.get("side"), "price": px,
                "qty": _f(o.get("origQty")), "filled": _f(o.get("executedQty")),
                "type": o.get("type"), "dist_bps": (round(dist, 1) if dist is not None else None),
                "age_s": (round((time.time() * 1000 - _f(o.get("time"))) / 1000) if o.get("time") else None),
            })
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        out["errors"].append(f"aster openOrders: {e!r}")
    try:
        for o in ts.hl_post({"type": "frontendOpenOrders", "user": hl_user}) or []:
            coin = o.get("coin", "")
            px = _f(o.get("limitPx"))
            mid = mids.get(coin, 0.0)
            dist = ((px - mid) / mid * 1e4) if (mid and px) else None
            h_orders.append({
                "venue": "hl", "coin": coin, "side": ("BUY" if o.get("side") == "B" else "SELL"),
                "price": px, "qty": _f(o.get("sz")),
                "dist_bps": (round(dist, 1) if dist is not None else None),
                "age_s": (round((time.time() * 1000 - _f(o.get("timestamp"))) / 1000) if o.get("timestamp") else None),
            })
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        out["errors"].append(f"hl openOrders: {e!r}")

    # --- per-coin net delta (the neutrality crux) ---
    coins = sorted({_coin_of(s) for s in a_pos} | set(h_pos)
                   | {o["coin"] for o in a_orders} | {o["coin"] for o in h_orders})
    per_coin = []
    worst_net_usd = 0.0
    for c in coins:
        a_qty = float(a_pos.get(c + "USDT", (Decimal(0), Decimal(0)))[0])
        a_up = float(a_pos.get(c + "USDT", (Decimal(0), Decimal(0)))[1])
        h_qty = float(h_pos.get(c, (Decimal(0), Decimal(0)))[0])
        h_up = float(h_pos.get(c, (Decimal(0), Decimal(0)))[1])
        mid = mids.get(c)                       # None if the public mids call failed
        net = a_qty + h_qty
        net_usd = (abs(net) * mid) if mid else None
        # Neutrality boolean matches pnl.py's qty convention (abs(net) < 0.001 coins), NOT a $ band,
        # so the live DELTA tile agrees with the heavy per-round-vs-MTM P&L decision. net_usd is for display only.
        neutral = abs(net) < 0.001
        if net_usd is not None:
            worst_net_usd = max(worst_net_usd, net_usd)
        per_coin.append({
            "coin": c, "aster_qty": a_qty, "hl_qty": h_qty, "net": net,
            "net_usd": net_usd, "neutral": neutral, "mid": mid,
            "aster_upnl": a_up, "hl_upnl": h_up,
            "a_orders": sum(1 for o in a_orders if o["coin"] == c),
            "h_orders": sum(1 for o in h_orders if o["coin"] == c),
        })

    total_eq = (a_eq or 0.0) + (h_eq or 0.0)
    out.update({
        "per_coin": per_coin,
        "neutral": all(p["neutral"] for p in per_coin) if per_coin else True,
        "worst_net_usd": round(worst_net_usd, 2),
        "equity": {"aster": a_eq, "hl": h_eq, "total": (round(total_eq, 2) if (a_eq is not None and h_eq is not None) else None)},
        "open_orders": a_orders + h_orders,
        "open_orders_count": len(a_orders) + len(h_orders),
    })

    # --- recent maker fills, for instant trade-trigger detection (server reconciles win/loss) ---
    out["recent_fills"] = _recent_maker_fills(ts, roles, a_pos, a_orders, mids)
    return out


def _recent_maker_fills(ts, roles, a_pos, a_orders, mids, lookback_ms: int = 20 * 60 * 1000):
    """Aster MAKER fills in the last ~20 min for the actively-quoted/held symbols.

    The set of symbols to scan = those with open orders or open positions (usually just HYPEUSDT),
    so this stays to one or two extra calls on the 5s cadence.
    """
    symbols = {o["symbol"] for o in a_orders if o.get("symbol")} | set(a_pos.keys())
    if not symbols:
        symbols = {"HYPEUSDT"}  # the live market by default; cheap and avoids a blind gap
    start_ms = int(time.time() * 1000) - lookback_ms
    fills = []
    for sym in sorted(symbols)[:6]:  # hard cap: never fan out unboundedly
        try:
            rows = ts.aster_get("/fapi/v3/userTrades",
                                {"symbol": sym, "startTime": str(start_ms), "limit": "200"}, roles)
        except (Exception, SystemExit):  # SystemExit: trade_stats._req exits on request failure
            continue
        for t in rows if isinstance(rows, list) else []:
            if not t.get("maker"):
                continue
            fills.append({
                "id": str(t.get("id")), "order_id": str(t.get("orderId")),
                "symbol": sym, "coin": _coin_of(sym), "side": t.get("side"),
                "qty": _f(t.get("qty")), "price": _f(t.get("price")),
                "time": int(t.get("time", 0)),
                "commission": _f(t.get("commission")),
            })
    fills.sort(key=lambda x: x["time"])
    return fills


# ----------------------------------------------------------------- heavy tier (~45s) ---------
def heavy_stats(root: str, since: str, until: str | None = None, timeout: float = 90.0) -> dict:
    """Run scripts/trade_stats.py --json and return its dict, augmented with pnl.py-style derived
    fields (delta-neutral net P&L, per-round win/loss feed). Direct-from-exchange ground truth."""
    script = os.path.join(root, "scripts", "trade_stats.py")
    fd, jpath = tempfile.mkstemp(suffix=".json", prefix="xemm_stats_")
    os.close(fd)
    cmd = [sys.executable, script, "--since", since, "--json", jpath]
    if until:
        cmd += ["--until", until]
    # Pin the capital base to the signed live equity incl. the Aster USDT balance, so return.capital
    # (the dashboard's headline equity + the return%/CAGR/today-P&L% denominator) reflects the whole
    # account and matches the fast headline calculation for this refresh.
    try:
        ts = load_trade_stats(root)
        roles = ts.aster_roles()
        hl_user = ts.load_env("hyperliquid.env")["subaccount_address"]
        cap = aster_equity_full(ts, roles) + float(ts.hl_state(hl_user)[0])
        if cap > 0:
            cmd += ["--capital", f"{cap:.8f}"]
    except (Exception, SystemExit):
        pass  # fall back to trade_stats' default capital (USDC/USDF + HL) on any venue/creds error
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _rm(jpath)
        return {"ok": False, "error": f"trade_stats.py timed out after {timeout}s", "since": since}
    except (Exception, SystemExit) as e:  # SystemExit: trade_stats._req exits on request failure
        _rm(jpath)
        return {"ok": False, "error": repr(e), "since": since}

    if not os.path.exists(jpath) or os.path.getsize(jpath) == 0:
        # trade_stats exits early (no JSON) when the window has zero trades.
        _rm(jpath)
        msg = (r.stderr or "").strip()
        if r.returncode == 0:
            return {"ok": True, "empty": True, "since": since,
                    "note": "no trades in window yet", "raw": None}
        return {"ok": False, "error": (msg or f"trade_stats exited {r.returncode}")[:400], "since": since}

    try:
        with open(jpath) as f:
            d = json.load(f)
    finally:
        _rm(jpath)

    d["ok"] = True
    d["empty"] = False
    d["derived"] = _derive(d)
    return d


def _derive(d: dict) -> dict:
    """Compute the headline net P&L the way pnl.py does, plus the per-round win/loss feed."""
    rounds = d.get("rounds", []) or []
    pnl = d.get("pnl", {}) or {}
    pos = d.get("positions")            # trade_stats emits None for a historical (--until) window

    priced = [r for r in rounds if r.get("round_net") is not None]
    round_net_sum = sum(r["round_net"] for r in priced) if priced else None
    wins = [r for r in priced if r["round_net"] > 0]

    # neutral if every coin's aster+hl nets to ~0 (matches pnl.py threshold 0.001). When positions
    # are None (historical window: mark_to_market is also None) we CANNOT confirm neutrality, so we
    # must NOT default to True — fall back to mark-to-market exactly as pnl.py does.
    if pos is None:
        neutral = False
    else:
        neutral = True
        coins = sorted(set(c.replace("USDT", "") for c in pos.get("aster", {})) | set(pos.get("hl", {})))
        for c in coins:
            if abs(_f(pos.get("aster", {}).get(c + "USDT", 0)) + _f(pos.get("hl", {}).get(c, 0))) >= 0.001:
                neutral = False
                break

    if neutral and round_net_sum is not None:
        net_pnl = round_net_sum
        net_pnl_basis = "per-round matched (delta neutral)"
    else:
        net_pnl = pnl.get("mark_to_market")
        net_pnl_basis = "mark-to-market (realized + unrealized)"

    # per-round feed (newest first), each tagged win/loss for the trade-trigger notifications
    feed = []
    for r in sorted(priced, key=lambda x: x.get("aster_time", 0), reverse=True):
        feed.append({
            "key": f'{r.get("order_id")}-{r.get("trade_id")}',
            "order_id": str(r.get("order_id")), "trade_id": str(r.get("trade_id")),
            "coin": r.get("coin"), "side": r.get("aster_side"),
            "qty": _f(r.get("qty")), "price": _f(r.get("aster_px")),
            "net": _f(r.get("round_net")), "win": r["round_net"] > 0,
            "time": r.get("aster_time"), "latency_ms": r.get("latency_ms"),
            "hedge_method": r.get("hedge_method"),
            "basis_bps": (round(_f(r.get("basis")) / _f(r.get("aster_px")) * 1e4, 2)
                          if r.get("basis") is not None and _f(r.get("aster_px")) else None),
        })

    return {
        "net_pnl": net_pnl,
        "net_pnl_basis": net_pnl_basis,
        "round_net_sum": round_net_sum,
        "neutral": neutral,
        "wins": len(wins),
        "priced": len(priced),
        "win_rate": (len(wins) / len(priced)) if priced else None,
        "feed": feed,
    }


# ----------------------------------------------------------------- price series -------------
def seed_prices(root: str, coin: str, symbol: str, minutes: int = 90) -> list:
    """Seed the chart with the last `minutes` of 1-minute closes from BOTH venues, merged by
    minute timestamp. Public endpoints only (no signing). Returns [{t, aster, hl}] (either may be None)."""
    ts = load_trade_stats(root)
    now = int(time.time() * 1000)
    start = now - minutes * 60_000
    aster, hl = {}, {}
    try:
        kl = ts.aster_public("/fapi/v1/klines", symbol=symbol, interval="1m", limit=min(minutes + 5, 1000))
        aster = {int(k[0]): round(_f(k[4]), 5) for k in (kl or [])}
    except (Exception, SystemExit):  # SystemExit: trade_stats._req exits on request failure
        pass
    try:
        cs = ts.hl_post({"type": "candleSnapshot",
                         "req": {"coin": coin, "interval": "1m", "startTime": start, "endTime": now}})
        hl = {int(c["t"]): round(_f(c["c"]), 5) for c in (cs or [])}
    except (Exception, SystemExit):  # SystemExit: trade_stats._req exits on request failure
        pass
    keys = sorted(k for k in (set(aster) | set(hl)) if k >= start)
    return [{"t": k, "aster": aster.get(k), "hl": hl.get(k)} for k in keys]


def price_tick(root: str, coin: str, symbol: str) -> dict:
    """One live mid for each venue (Aster bookTicker mid, HL allMids). Public; cheap enough for 5s."""
    ts = load_trade_stats(root)
    a = h = None
    try:
        bt = ts.aster_public("/fapi/v1/ticker/bookTicker", symbol=symbol)
        bid, ask = _f(bt.get("bidPrice")), _f(bt.get("askPrice"))
        if bid and ask:
            a = round((bid + ask) / 2, 5)
    except (Exception, SystemExit):  # SystemExit: trade_stats._req exits on request failure
        pass
    try:
        h = round(_f(ts.hl_post({"type": "allMids"}).get(coin)), 5) or None
    except (Exception, SystemExit):  # SystemExit: trade_stats._req exits on request failure
        pass
    return {"t": int(time.time() * 1000), "aster": a, "hl": h}


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


if __name__ == "__main__":  # debug: python collectors.py [since]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    print("== fast ==")
    print(json.dumps(fast_snapshot(root), indent=2, default=str))
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-06-16 21:57:00"
    print("== heavy ==")
    print(json.dumps(heavy_stats(root, since), indent=2, default=str))
