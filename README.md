# XEMM Streaming Dashboard

A real-time browser dashboard for following a **cross-exchange market-making (XEMM)** bot —
maker quotes on **Aster**, delta-neutral hedge on **Hyperliquid** — plus the health of the **VPS**
running the native `xemm_eval` (Rust) binary. Designed to look good on a **1920×1080** screen for
live streaming, and to refresh every few seconds.

> All numbers come **straight from the exchanges** (Aster + Hyperliquid), never from a replay tape
> or a modelling DB — so the headline P&L matches what the bot operator's `scripts/pnl.py` prints.

## Highlights

- **Live dual-venue price chart** (the centerpiece) — Aster vs Hyperliquid mid, the cross-venue
  **basis** strip, and **every fill marked** (▲ buy / ▼ sell, green win / red loss). Auto-cycles
  5m·15m·1h·4h and snaps to a tight 5m view on a fresh fill. Fed by **WebSockets** (REST fallback +
  periodic sanity check), with an **adaptive resolution** per zoom (full 1s up close, 10s @ 1h, 30s @ 4h).
- **Hero KPIs** — net P&L, win rate, net delta (neutrality), equity, hedge-latency p50, return %,
  and a projected annualized P&L.
- **TODAY (UTC) summary** — trades / win rate / net P&L ($ and % of capital) since 00:00 UTC.
- **Economics** — round net Σ, avg/best/worst, entry-basis bps, volume, fees, and the
  **payoff ratio** (avg win ÷ avg loss).
- **Quote-spread decomposition** — how the maker quote is built off the HL hedge: each fee/buffer
  and the expected profit margin, in bps, with a labeled legend.
- **VPS & Rust bot health** — is the native `xemm_eval` process alive, its CPU/RSS, host load
  (as % of vCPU), RAM/disk, and a **write-heartbeat**; plus **AWS Tokyo → exchange API latency**.
- **Positions & net delta**, **hedge execution** (primary/fallback/recovery + latency distribution),
  and a discreet **circuit-breaker** chip (armed/tripped vs the loss limit).
- **One-sided-quoting notice** — surfaces when the bot is at its capital limit and can only post
  reduce-only orders, mirroring the bot's real binding constraint.
- **Trade history rail** — every completed round, tagged win/loss, **direct from the exchanges**.
- **Trade-trigger toasts** — a win/loss animation pops the moment a new round completes.
- **Durable archives** — every 1s mid and every matched round are persisted to SQLite (WAL,
  `synchronous=FULL`) so a high-resolution chart and full trade history survive restarts.

## How it gets the data

| Panel | Source |
|-------|--------|
| P&L, win rate, hedge quality, latency, trade history, positions, equity, open orders | **Aster + Hyperliquid APIs** via the sibling repo's `scripts/trade_stats.py` (same signing + deterministic hedge-cloid matching `pnl.py` uses) |
| Process/host health, breaker latch, baseline equity, write-heartbeat, AWS→API latency | **read-only `ssh`** to the VPS (`ps`/`cat`/`df`/`grep`/`curl`) |
| `since` cutoff | resolved **identically to remote `scripts/pnl.py`** |

This is a **companion tool**: it sits one level above the (private) trading-bot repo and reuses its
`scripts/trade_stats.py` + `aster.env` / `hyperliquid.env`. Point `trading_root` at that repo.

## Run with Docker (recommended)

```bash
cp config.example.json config.json     # then edit your VPS host / SSH key path
docker compose up -d --build           # serves http://127.0.0.1:8787
```

- Published on the **host loopback only** (`127.0.0.1:8787`).
- The durable archive lives in a mounted `./data` volume — it survives rebuilds; nothing is lost.
- The trading repo is mounted **read-only**; your SSH key is mounted read-only (see `.env.docker.example`).
- `docker compose logs -f` to tail · `docker compose stop` to pause (data persists).

## Run natively (no Docker)

Requires **Python 3.10+** with the packages `trade_stats.py` uses (`requests`, `eth_account`,
`eth_abi`, `eth_utils`) and an `ssh` client on PATH.

```bash
cp config.example.json config.json
python server.py        # or ./run.sh (macOS/Linux) · ./run.ps1 (Windows)
```

…then open <http://127.0.0.1:8787>. No web framework, no build step, no CDN — pure stdlib + your deps.

## Configure — `config.json`

`config.json` is **gitignored** (your real host/IP/key path stay local). Copy the example and edit:

```jsonc
{
  "bind_host": "127.0.0.1", "bind_port": 8787,
  "vps": {
    "ssh_target": "user@YOUR_VPS_HOST",          // host running native xemm_eval
    "ssh_key": "~/.ssh/your-vps-key.pem",
    "deploy_dir": "/home/user/your-trading-repo",
    "process_name": "xemm_eval",
    "container_name": "xemm-hype-soak"            // only used to mirror pnl.py's since
  },
  "trading_root": "..",                            // repo holding scripts/ + *.env
  "chart": { "coin": "HYPE", "symbol": "HYPEUSDT",
             "resolution_buckets": [ {"max_minutes":15,"bucket_s":1}, {"max_minutes":60,"bucket_s":10}, {"max_minutes":240,"bucket_s":30} ] },
  "refresh": { "ui_poll_s": 5, "live_s": 5, "price_s": 1, "health_s": 12, "stats_s": 45, "latency_s": 1200 }
}
```

Two cadences: the live snapshot (positions, delta, quotes, health) is cheap and refreshes every
**5 s**; the heavier `trade_stats.py` fill-history run refreshes on a slower timer **and immediately
when a new fill is detected**, so win/loss toasts feel instant.

## Security & privacy

- Binds **127.0.0.1 only** (Docker publishes to host loopback). Refuses a non-loopback bind unless
  `allow_remote_bind` is set.
- **No secrets in this repo** — signing happens inside `trade_stats.py`; only derived public data
  (positions, P&L, public addresses) reaches the browser. `config.json`, `*.env`, `*.pem`, `secrets/`,
  and the `data/` archive are gitignored.
- Mutating route (`/api/refresh`) is POST + same-origin only (CSRF-guarded).

## Endpoints

| route | purpose |
|-------|---------|
| `GET /` | the dashboard |
| `GET /api/state` | merged cached snapshot (live + health + stats + since + events + config) |
| `GET /api/prices?minutes=N` | chart series for a window, at the adaptive resolution |
| `POST /api/refresh` | force an immediate heavy `trade_stats` refresh (same-origin) |

## Trade-trigger GIFs

Built-in animated SVGs work out of the box; drop `gifs/win.gif` / `gifs/loss.gif` to use your own —
see [`gifs/README.md`](gifs/README.md).
