#!/usr/bin/env python3
"""Real-time WebSocket price feed for Aster + Hyperliquid (with auto-reconnect).

Prices come primarily from WebSockets (Aster `@bookTicker`, HL `bbo`); the server falls back to
REST and runs a periodic REST sanity check. Each venue runs in its own daemon thread and keeps the
latest mid + a timestamp; `mids()` returns them only if fresh.
"""
from __future__ import annotations

import json
import threading
import time

import websocket  # websocket-client (synchronous, thread-friendly)


class WsPriceFeed:
    def __init__(self, coin: str = "HYPE", symbol: str = "HYPEUSDT"):
        self.coin = coin
        self.symbol = symbol.lower()
        self._lock = threading.Lock()
        self._aster = None
        self._aster_ts = 0.0
        self._hl = None
        self._hl_ts = 0.0
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, args=("aster",), daemon=True).start()
        threading.Thread(target=self._run, args=("hl",), daemon=True).start()

    def _run(self, which: str):
        while True:
            ws = None
            try:
                if which == "aster":
                    ws = websocket.create_connection(
                        f"wss://fstream.asterdex.com/ws/{self.symbol}@bookTicker", timeout=10)
                else:
                    ws = websocket.create_connection("wss://api.hyperliquid.xyz/ws", timeout=10)
                    ws.send(json.dumps({"method": "subscribe",
                                        "subscription": {"type": "bbo", "coin": self.coin}}))
                ws.settimeout(30)
                while True:
                    msg = json.loads(ws.recv())
                    if which == "aster":
                        b, a = float(msg.get("b") or 0), float(msg.get("a") or 0)
                        if b > 0 and a > 0:
                            with self._lock:
                                self._aster = round((b + a) / 2, 5)
                                self._aster_ts = time.time()
                    elif msg.get("channel") == "bbo":
                        bbo = (msg.get("data") or {}).get("bbo") or []
                        if len(bbo) == 2 and bbo[0] and bbo[1]:
                            b, a = float(bbo[0]["px"]), float(bbo[1]["px"])
                            if b > 0 and a > 0:
                                with self._lock:
                                    self._hl = round((b + a) / 2, 5)
                                    self._hl_ts = time.time()
            except Exception:  # noqa: BLE001 — reconnect with backoff
                time.sleep(3)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass

    def mids(self, max_age: float = 8.0):
        """Latest WS mids that are fresher than max_age seconds (else None), plus ages for status."""
        now = time.time()
        with self._lock:
            a = self._aster if (now - self._aster_ts) < max_age else None
            h = self._hl if (now - self._hl_ts) < max_age else None
            return a, h, {"aster_age": round(now - self._aster_ts, 1) if self._aster_ts else None,
                          "hl_age": round(now - self._hl_ts, 1) if self._hl_ts else None}
