#!/usr/bin/env python3
"""Sync the local trading-repo mirror DOWN from the VPS (the VPS is the source of truth).

The dashboard reads the live edge/quote config (`config-live-hype.toml`) and the `scripts/` it
executes from the LOCAL trading repo — a downstream mirror of the VPS. When the operator edits
something on the VPS (e.g. `min_net_profit_bps` 12 -> 14), the dashboard keeps showing the stale
LOCAL value until the mirror is refreshed. This script does that refresh: it pulls the VPS
`deploy_dir` down over ssh (tar-over-ssh) and overwrites the matching local files.

  * READ-ONLY on the VPS — it only `tar`s the deploy_dir to stdout. No remote mutation.
  * VPS is master — local files are OVERWRITTEN with the VPS version.
  * Never deletes local-only files, and never touches the dashboard (`XEMM_DASHBOARD/`) or `.git/`.
  * Skips build artifacts + run data (`target/`, `target-*`, `runs/`, `*.sqlite*`, `*.jsonl.zst`,
    `__pycache__`, `.claude/`, ...) — both to stay fast and to avoid clobbering local-only state.
  * Never pulls credentials/keys down (`*.env`, `*.pem`, `*.key`) — the working local copy is kept,
    matching the established mirror practice.
  * The trading repo is a git repo, so the result is reviewable: `git diff` shows exactly what the
    VPS changed; `git checkout -- <file>` reverts any overwrite you didn't want.

Connection details come from `config.json` (the SAME file the dashboard uses:
`vps.ssh_target` / `vps.ssh_key` / `vps.deploy_dir` and `trading_root`) — nothing is hard-coded
and no host/IP is committed (`config.json` is gitignored).

By default, if config-live-hype.toml changes and the dashboard container is running, it is
auto-restarted so the new parameters take effect (the dashboard parses that file once at startup).
Pass --no-restart to suppress that. A stopped container is left stopped.

Usage:
  python sync_from_vps.py --dry-run                     # list what WOULD change (no writes) — do this first
  python sync_from_vps.py                               # pull + overwrite local from the VPS (asks to confirm; auto-restarts on a config change)
  python sync_from_vps.py --only config-live-hype.toml  # just the dashboard's edge config
  python sync_from_vps.py --only scripts/               # just the scripts the dashboard executes
  python sync_from_vps.py --no-restart                  # sync but don't auto-restart the dashboard
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Local-only / regenerable paths the VPS must never overwrite (matched against any path component).
EXCLUDE_NAMES = {".git", ".claude", "target", "runs", "__pycache__", "node_modules",
                 "XEMM_DASHBOARD", ".venv", "venv", ".idea", ".vscode"}
EXCLUDE_GLOBS = {"target-*", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.sqlite-journal",
                 "*.jsonl.zst", "*.tape", "*.pyc", "*.log",
                 "*.env", "*.pem", "*.key"}   # never pull creds/keys down — keep the working local copy


# ----------------------------------------------------------------- config -----------------------
def load_cfg(path: str | None):
    path = path or os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        path = os.path.join(HERE, "config.example.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    root = cfg.get("trading_root", "..")
    cfg["_root_abs"] = os.path.abspath(root if os.path.isabs(root) else os.path.join(HERE, root))
    return cfg, path


def _sh(s) -> str:
    """POSIX single-quote so the remote shell treats the value literally."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def ssh_base(cfg: dict) -> list[str]:
    v = cfg["vps"]
    return [
        "ssh", "-i", os.path.expanduser(v["ssh_key"]),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",                       # key-only; never hang on a password prompt
        "-o", f"ConnectTimeout={v.get('connect_timeout_s', 12)}",
        v["ssh_target"],
    ]


def remote_tar_cmd(cfg: dict) -> str:
    deploy = cfg["vps"]["deploy_dir"]
    excludes = [f"--exclude={_sh(n)}" for n in sorted(EXCLUDE_NAMES)]
    excludes += [f"--exclude={_sh(g)}" for g in sorted(EXCLUDE_GLOBS)]
    # gzip-tar the deploy dir to stdout. The remote excludes are a best-effort bandwidth saver;
    # the Python extractor below re-checks every member, so correctness never depends on tar's
    # exclude-matching semantics.
    return f"tar -cz --ignore-failed-read -C {_sh(deploy)} {' '.join(excludes)} ."


# ----------------------------------------------------------------- path helpers -----------------
def norm(name: str) -> str:
    n = name.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n


def excluded(rel: str) -> bool:
    for p in (x for x in rel.split("/") if x not in ("", ".")):
        if p in EXCLUDE_NAMES or any(fnmatch.fnmatch(p, g) for g in EXCLUDE_GLOBS):
            return True
    return False


def unsafe(rel: str) -> bool:
    # reject absolute paths and any traversal that would escape the trading root
    return rel.startswith("/") or rel == ".." or rel.startswith("../") or "/../" in rel


# ----------------------------------------------------------------- config bps diff --------------
_BPS_KEYS = ("min_net_profit_bps", "slippage_buffer_bps", "latency_buffer_bps", "basis_buffer_bps",
             "funding_buffer_bps", "aster_maker_fee_bps", "hyperliquid_taker_fee_bps")


def parse_bps(text: str | None) -> dict | None:
    if not text:
        return None

    def num(key):
        m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"?([\d.]+)"?', text, re.M)
        return float(m.group(1)) if m else None

    vals = {k: num(k) for k in _BPS_KEYS}
    required = sum(v for k, v in vals.items()
                   if v is not None and k.endswith(("profit_bps", "buffer_bps")))
    return {"min_net_profit_bps": vals["min_net_profit_bps"],
            "required_edge_bps": round(required, 2) if required else None}


def read_local_config_text(root: str) -> str | None:
    try:
        return open(os.path.join(root, "config-live-hype.toml"), encoding="utf-8").read()
    except OSError:
        return None


def fmt_bps(b: dict | None) -> str:
    if not b:
        return "-"
    return (f"min_net_profit={b['min_net_profit_bps']} | "
            f"required_edge={b['required_edge_bps']} bps")


# ----------------------------------------------------------------- main -------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sync the local trading-repo mirror DOWN from the VPS (VPS is master).")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="list what would change; write nothing (do this first)")
    ap.add_argument("--config", help="path to config.json (default: alongside this script)")
    ap.add_argument("--only", nargs="+", metavar="PREFIX",
                    help="only sync paths equal to / under one of these "
                         "(e.g. 'config-live-hype.toml' or 'scripts/')")
    ap.add_argument("--exclude", nargs="+", default=[], metavar="GLOB",
                    help="extra path-component globs to skip")
    ap.add_argument("--no-restart", action="store_true",
                    help="do NOT auto-restart the dashboard container after a config change "
                         "(by default it restarts if config-live-hype.toml changed and the container is running)")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the overwrite confirmation prompt")
    ap.add_argument("--timeout", type=float, default=180.0, help="ssh/transfer timeout seconds (default 180)")
    args = ap.parse_args()
    EXCLUDE_GLOBS.update(args.exclude)

    cfg, cfg_path = load_cfg(args.config)
    v = cfg.get("vps", {})
    if "YOUR_VPS_HOST" in v.get("ssh_target", "") or not v.get("ssh_target"):
        return _die(f"{os.path.basename(cfg_path)} still has placeholder VPS details - "
                    f"fill in your real config.json (copied from config.example.json) first.")

    root = cfg["_root_abs"]
    only = [norm(o) for o in (args.only or [])]
    print(f"[sync] VPS {v['ssh_target']}:{v['deploy_dir']}")
    print(f"[sync]  ->  local {root}")
    print(f"[sync] mode: {'DRY-RUN (no writes)' if args.dry_run else 'WRITE - VPS overwrites local'}"
          + (f"   filter: {', '.join(only)}" if only else ""))

    before = parse_bps(read_local_config_text(root))

    # confirm before a real (writing) run — this overwrites your working tree with the VPS version
    if not args.dry_run and not args.yes:
        try:
            ans = input("[sync] overwrite local files with the VPS versions? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("[sync] aborted (no changes written). Re-run with --dry-run to preview, or -y to confirm.")
            return 1

    def wanted(rel: str) -> bool:
        return (not only) or any(rel == o or rel.startswith(o.rstrip("/") + "/") for o in only)

    cmd = ssh_base(cfg) + [remote_tar_cmd(cfg)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return _die("ssh client not found on PATH (install the OpenSSH client).")

    new: list[str] = []
    changed: list[str] = []
    written = skipped = same = 0
    cfg_new_text: str | None = None

    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|gz", errorlevel=1) as tf:
            for m in tf:
                rel = norm(m.name)
                if not rel or rel == ".":
                    continue
                if unsafe(rel):
                    print(f"  ! skip unsafe path: {m.name}")
                    skipped += 1
                    continue
                if excluded(rel) or not wanted(rel):
                    continue
                if m.isdir():
                    if not args.dry_run:
                        os.makedirs(os.path.join(root, rel), exist_ok=True)
                    continue
                if not m.isfile():
                    skipped += 1            # symlinks / devices / etc. — never written
                    continue

                data = tf.extractfile(m).read()
                if rel == "config-live-hype.toml":
                    cfg_new_text = data.decode("utf-8", "replace")

                dest = os.path.join(root, rel)
                old = None
                if os.path.exists(dest):
                    try:
                        old = open(dest, "rb").read()
                    except OSError:
                        old = None
                if old == data:
                    same += 1
                    continue
                (new if old is None else changed).append(rel)
                if not args.dry_run:
                    os.makedirs(os.path.dirname(dest) or root, exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(data)
                    written += 1
    except tarfile.ReadError:
        err = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "").strip()
        proc.wait()
        return _die("could not read the VPS tar stream - ssh likely failed.\n"
                    + ("        " + err if err else "        (no stderr; check ssh_target / ssh_key / network)"))
    except tarfile.TarError as e:
        proc.wait()
        return _die(f"tar stream error: {e}")
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass

    err = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "").strip()
    proc.wait()

    # ---- report -------------------------------------------------------------------------------
    verb = "would update" if args.dry_run else "updated"
    print(f"\n[sync] {verb} {len(new) + len(changed)} file(s)  "
          f"({len(new)} new, {len(changed)} changed; {same} already current, {skipped} skipped)")
    for tag, items in (("new", new), ("changed", changed)):
        for rel in sorted(items):
            print(f"    {tag:>7}: {rel}")

    after = parse_bps(cfg_new_text) or before
    if before or after:
        if before != after:
            print(f"\n[sync] config-live-hype.toml edge:  {fmt_bps(before)}")
            print(f"[sync]                          -> {fmt_bps(after)}"
                  + ("  (preview - not written yet)" if args.dry_run else ""))
        else:
            print(f"\n[sync] config-live-hype.toml edge unchanged: {fmt_bps(after)}")

    if err:
        print(f"\n[sync] note: remote tar stderr (usually harmless):\n        {err.splitlines()[0]}")

    if args.dry_run:
        print("\n[sync] dry run - nothing written. Re-run without --dry-run to apply.")
        return 0

    # The dashboard parses config-live-hype.toml once at startup, so a parameter change only takes
    # effect after a restart. Auto-restart by default when that file actually changed (and the
    # container is up); scripts/ etc. are read live and need no restart.
    if "config-live-hype.toml" in new or "config-live-hype.toml" in changed:
        maybe_restart_dashboard(args.no_restart)
    print("\n[sync] review the result:  git -C .. diff   |   revert a file:  git -C .. checkout -- <path>")
    return 0


DASHBOARD_CONTAINER = "xemm-dashboard"   # set by docker-compose.yml (container_name:)


def container_running(name: str = DASHBOARD_CONTAINER):
    """True/False if the dashboard container is up/down, or None if docker can't be queried."""
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                           capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return False                      # no such container
    return r.stdout.strip() == "true"


def maybe_restart_dashboard(no_restart: bool) -> None:
    """Restart the dashboard so it re-parses the config — but only if it's currently running."""
    if no_restart:
        print("\n[sync] config changed - skipping auto-restart (--no-restart). To apply it:")
        print("         docker compose restart        # run in XEMM_DASHBOARD/")
        return
    running = container_running()
    if running is None:
        print("\n[sync] config changed, but docker isn't reachable - couldn't auto-restart.")
        print("         start it / re-run with the engine up, or: docker compose restart")
        return
    if not running:
        print(f"\n[sync] config changed; dashboard container '{DASHBOARD_CONTAINER}' is not running "
              "- nothing to restart (it'll read the new config when next started).")
        return
    print("\n[sync] config changed - auto-restarting the dashboard so it re-parses it...")
    r = subprocess.run(["docker", "compose", "restart"], cwd=HERE)
    print(f"[sync] docker compose restart exit={r.returncode}")


def _die(msg: str) -> int:
    print(f"[sync] ERROR: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
