#!/usr/bin/env python3
"""Read-only VPS health probe for the XEMM live dashboard.

Opens ONE ssh session and runs a single remote bash script that gathers everything the
dashboard needs about the host running the *native* `xemm_eval` binary:

  * the live process (pid / uptime / %cpu / %mem / rss / full launch command),
  * host metrics (uptime, load average, RAM, disk),
  * a write-heartbeat (freshest mtime among the run's data files -> "is it still working?"),
  * the circuit-breaker trip latch (runs/*.trip.json) + the armed baseline equity, and
  * the inputs needed to resolve the SAME "since" cutoff the remote scripts/pnl.py uses.

Everything here is READ ONLY: `ps`, `cat`, `grep`, `df`, `docker inspect`. No remote mutation.

`resolve_since()` mirrors remote scripts/pnl.py EXACTLY: it uses the `xemm-hype-soak` container
start time when that container exists, otherwise the hard-coded fallback string parsed live from
the remote pnl.py file (so it never drifts when the operator edits that constant on restart).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

# ---- the remote probe: a single bash script piped to `bash -s` over one ssh session ----------
# It prints TAB-separated `KEY<TAB>VALUE` lines (values are single-line; JSON blobs are compacted).
_REMOTE_SCRIPT = r"""
set +e
cd "$DEPLOY_DIR" 2>/dev/null || cd /
now=$(date +%s)
printf 'NOW_EPOCH\t%s\n' "$now"

# --- the live xemm_eval process(es): one PROC line each (Python picks the --mode live one) ---
pids=$(pgrep -f "$PROC_NAME" 2>/dev/null)
if [ -n "$pids" ]; then
  ps -p $(echo $pids | tr ' ' ',') -o pid=,etimes=,pcpu=,pmem=,rss= -o args= 2>/dev/null | \
  while read -r pid etimes pcpu pmem rss args; do
    printf 'PROC\t%s\t%s\t%s\t%s\t%s\t%s\n' "$pid" "$etimes" "$pcpu" "$pmem" "$rss" "$args"
  done
fi

# --- host metrics ---
printf 'HOST_UPTIME_S\t%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null)"
printf 'HOST_LOADAVG\t%s\n' "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
printf 'HOST_NCPU\t%s\n' "$(nproc 2>/dev/null)"
mt=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)
ma=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)
printf 'MEM_TOTAL_KB\t%s\n' "$mt"
printf 'MEM_AVAIL_KB\t%s\n' "$ma"
df -kP / 2>/dev/null | tail -1 | awk '{printf "DISK_TOTAL_KB\t%s\nDISK_USED_KB\t%s\nDISK_AVAIL_KB\t%s\nDISK_USED_PCT\t%s\n",$2,$3,$4,$5}'

# --- write-heartbeat: freshest data file the bot keeps touching ---
hb=$(find runs -maxdepth 1 -type f \
       \( -name '*.sqlite-wal' -o -name '*.jsonl.zst' -o -name '*-journal.jsonl' -o -name '*.sqlite' \) \
       -printf '%T@\t%f\n' 2>/dev/null | sort -nr | head -1)
if [ -n "$hb" ]; then
  hbts=$(echo "$hb" | cut -f1 | cut -d. -f1)
  hbfile=$(echo "$hb" | cut -f2)
  printf 'HEARTBEAT_EPOCH\t%s\n' "$hbts"
  printf 'HEARTBEAT_FILE\t%s\n' "$hbfile"
fi

# --- circuit breaker trip latch (present == tripped) + armed baseline equity ---
latch=$(ls runs/*.trip.json 2>/dev/null | head -1)
if [ -n "$latch" ]; then
  printf 'TRIP_LATCH\t%s\n' "$latch"
  printf 'TRIP_JSON\t%s\n' "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))))' "$latch" 2>/dev/null | tr -d '\n')"
else
  printf 'TRIP_LATCH\t\n'
fi
if [ -f runs/baseline_equity.json ]; then
  printf 'BASELINE_JSON\t%s\n' "$(python3 -c 'import json;print(json.dumps(json.load(open("runs/baseline_equity.json"))))' 2>/dev/null | tr -d '\n')"
fi

# --- inputs for resolving the same "since" as scripts/pnl.py ---
printf 'DOCKER_STARTEDAT\t%s\n' "$(docker inspect "$CONTAINER_NAME" --format '{{.State.StartedAt}}' 2>/dev/null)"
printf 'PNL_FALLBACK\t%s\n' "$(grep -oP 'container_start_time\(\) or \"\K[^\"]+' scripts/pnl.py 2>/dev/null | head -1)"
"""


def _expand_key(path: str) -> str:
    return os.path.expanduser(path)


def _ssh_cmd(cfg: dict) -> list[str]:
    v = cfg["vps"]
    return [
        "ssh", "-i", _expand_key(v["ssh_key"]),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",                       # never hang on a password prompt
        "-o", f"ConnectTimeout={v.get('connect_timeout_s', 12)}",
        v["ssh_target"], "bash -s",
    ]


def probe(cfg: dict, timeout: float = 25.0) -> dict:
    """Run the remote probe once. Returns a structured dict; never raises (errors -> reachable=False)."""
    v = cfg["vps"]
    env = os.environ.copy()
    script = (
        f'DEPLOY_DIR={_sh(v["deploy_dir"])}; '
        f'PROC_NAME={_sh(v["process_name"])}; '
        f'CONTAINER_NAME={_sh(v["container_name"])}; '
        + _REMOTE_SCRIPT
    )
    # Normalize to LF and send as BYTES. On Windows, subprocess text-mode stdin re-translates
    # \n -> \r\n, which makes remote bash choke on `$'\r'`; bytes mode preserves clean LF.
    script = script.replace("\r\n", "\n").replace("\r", "\n")
    try:
        r = subprocess.run(_ssh_cmd(cfg), input=script.encode("utf-8"),
                           capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"reachable": False, "error": "ssh timed out", "checked_at": _now_iso()}
    except FileNotFoundError:
        return {"reachable": False, "error": "ssh client not found on PATH", "checked_at": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": repr(e), "checked_at": _now_iso()}

    stdout = r.stdout.decode("utf-8", "replace")
    stderr = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 and not stdout.strip():
        return {"reachable": False, "error": (stderr or "ssh failed").strip()[:300],
                "checked_at": _now_iso()}

    return _parse(stdout, cfg)


def _parse(out: str, cfg: dict) -> dict:
    rows: dict[str, list[str]] = {}
    procs: list[dict] = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        key, _, rest = line.partition("\t")
        if key == "PROC":
            parts = rest.split("\t", 5)
            if len(parts) == 6:
                pid, etimes, pcpu, pmem, rss, args = parts
                procs.append({
                    "pid": _int(pid), "uptime_s": _int(etimes),
                    "cpu_pct": _float(pcpu), "mem_pct": _float(pmem),
                    "rss_kb": _int(rss), "cmd": args.strip(),
                })
            continue
        rows.setdefault(key, []).append(rest)

    def g(key, default=None):
        vals = rows.get(key)
        return vals[0] if vals else default

    proc_name = cfg.get("vps", {}).get("process_name", "xemm_eval")
    bot_procs = [p for p in procs if _is_process_cmd(p["cmd"], proc_name)]

    # Pick the live process: prefer one whose command contains "--mode live".
    live = next((p for p in bot_procs if "--mode live" in p["cmd"]), None)
    if live is None and bot_procs:
        live = max(bot_procs, key=lambda p: p["uptime_s"])  # fall back to the longest-running match

    now_epoch = _int(g("NOW_EPOCH")) or None
    hb_epoch = _int(g("HEARTBEAT_EPOCH"))
    heartbeat = None
    if hb_epoch and now_epoch:
        heartbeat = {"file": g("HEARTBEAT_FILE"), "epoch": hb_epoch,
                     "age_s": max(now_epoch - hb_epoch, 0)}

    mem_total = _int(g("MEM_TOTAL_KB"))
    mem_avail = _int(g("MEM_AVAIL_KB"))
    disk_total = _int(g("DISK_TOTAL_KB"))
    disk_avail = _int(g("DISK_AVAIL_KB"))

    trip_latch = (g("TRIP_LATCH") or "").strip()
    trip_record = _json(g("TRIP_JSON")) if trip_latch else None
    baseline = _json(g("BASELINE_JSON"))

    since, since_source, since_detail = _resolve_since_from_rows(g)

    health = {
        "reachable": True,
        "checked_at": _now_iso(),
        "now_epoch": now_epoch,
        "process": {
            "running": live is not None,
            "count": len(bot_procs),
            **(live or {}),
        },
        "host": {
            "uptime_s": _int(g("HOST_UPTIME_S")),
            "loadavg": [_float(x) for x in (g("HOST_LOADAVG") or "").split()] or None,
            "ncpu": _int(g("HOST_NCPU")),
            "mem_total_mb": round(mem_total / 1024) if mem_total else None,
            "mem_avail_mb": round(mem_avail / 1024) if mem_avail else None,
            "mem_used_pct": round(100 * (1 - mem_avail / mem_total), 1) if (mem_total and mem_avail) else None,
            "disk_total_gb": round(disk_total / (1024 * 1024), 1) if disk_total else None,
            "disk_avail_gb": round(disk_avail / (1024 * 1024), 1) if disk_avail else None,
            "disk_used_pct": _int((g("DISK_USED_PCT") or "").rstrip("%")),
        },
        "heartbeat": heartbeat,
        "breaker": {
            "tripped": bool(trip_latch),
            "latch": trip_latch or None,
            "record": trip_record,
            "baseline": baseline,
        },
        "since": {"value": since, "source": since_source, "detail": since_detail},
    }
    return health


def _is_process_cmd(cmd: str, proc_name: str) -> bool:
    """True only when the command's executable is the bot binary.

    `pgrep -f xemm_eval` also matches compiler commands such as
    `rustc --crate-name xemm_eval`; those must not produce a false green bot status.
    """
    exe = (cmd or "").strip().split(" ", 1)[0]
    return os.path.basename(exe) in {proc_name, f"{proc_name}.exe"}


def _resolve_since_from_rows(g):
    """Mirror remote scripts/pnl.py: container StartedAt -> "%Y-%m-%d %H:%M", else the file fallback."""
    started = (g("DOCKER_STARTEDAT") or "").strip()
    if started:
        try:
            dt = datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M"), "container", "xemm-hype-soak StartedAt"
        except ValueError:
            pass
    fallback = (g("PNL_FALLBACK") or "").strip()
    if fallback:
        return fallback, "pnl_fallback", "scripts/pnl.py hard-coded default"
    return None, "unknown", "could not resolve from VPS"


# ---- VPS -> exchange API latency (measured ON the VPS; throttled to ~once/20min by the caller) ----
_LATENCY_SCRIPT = r"""
set +e
sample() {  # $1=method $2=url $3=data -> best (min) TTFB in ms over 3 tries
  best=99999
  for i in 1 2 3; do
    if [ "$1" = POST ]; then
      t=$(curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d "$3" -w '%{time_starttransfer}' --max-time 6 "$2" 2>/dev/null)
    else
      t=$(curl -s -o /dev/null -w '%{time_starttransfer}' --max-time 6 "$2" 2>/dev/null)
    fi
    ms=$(awk "BEGIN{printf \"%d\", ($t)*1000}" 2>/dev/null)
    [ -n "$ms" ] && [ "$ms" -gt 0 ] && [ "$ms" -lt "$best" ] && best=$ms
  done
  [ "$best" = 99999 ] && best=""
  echo "$best"
}
printf 'ASTER_MS\t%s\n' "$(sample GET https://fapi.asterdex.com/fapi/v1/time '')"
printf 'HL_MS\t%s\n' "$(sample POST https://api.hyperliquid.xyz/info '{\"type\":\"allMids\"}')"
"""


def measure_api_latency(cfg: dict, timeout: float = 45.0) -> dict:
    """SSH to the VPS and measure best-of-3 TTFB to the Aster + Hyperliquid APIs (ms)."""
    script = _LATENCY_SCRIPT.replace("\r\n", "\n").replace("\r", "\n")
    try:
        r = subprocess.run(_ssh_cmd(cfg), input=script.encode("utf-8"),
                           capture_output=True, timeout=timeout, env=os.environ.copy())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200], "checked_at": _now_iso()}
    out = r.stdout.decode("utf-8", "replace")
    vals = {}
    for line in out.splitlines():
        if "\t" in line:
            k, _, v = line.partition("\t")
            vals[k] = v.strip()
    a, h = _int(vals.get("ASTER_MS")), _int(vals.get("HL_MS"))
    if a is None and h is None:
        return {"ok": False, "error": (r.stderr.decode("utf-8", "replace") or "no result").strip()[:200],
                "checked_at": _now_iso()}
    return {"ok": True, "aster_ms": a, "hl_ms": h, "checked_at": _now_iso()}


def resolve_since(cfg: dict, health: dict | None = None) -> dict:
    """Return {'value','source','detail'}. Uses a fresh probe if `health` not supplied."""
    if health and health.get("reachable") and health.get("since", {}).get("value"):
        return health["since"]
    h = health if (health and health.get("reachable")) else probe(cfg)
    if h.get("reachable") and h.get("since", {}).get("value"):
        return h["since"]
    return {"value": None, "source": "unknown", "detail": h.get("error", "VPS unreachable")}


# ---- tiny helpers -----------------------------------------------------------------------------
def _sh(s: str) -> str:
    """Single-quote a value for safe interpolation into the remote bash script."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _int(s):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def _float(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":  # debug: python vps_health.py [path/to/config.json]
    import sys
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    print(json.dumps(probe(cfg), indent=2))
