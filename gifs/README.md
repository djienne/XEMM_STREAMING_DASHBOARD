# Trade-trigger GIFs

When a XEMM round completes, the dashboard reacts in two places:

1. a small **toast** in the top-right corner with the trade detail + P&L, and
2. a **big centered overlay** that plays a celebratory (win) or commiserating (loss) GIF,
   ringed with a **green** (win) or **red** (loss) glow, then fades after a few seconds.

## The centered overlay gifs

| event | gif played |
|-------|------------|
| net-positive round | alternates **`dicaprio.gif`** ⇄ **`macmahon.gif`** on each win |
| net-negative round | **`gosling-dive.gif`** |

These files live right here in `gifs/` and are served at `/gifs/<name>`. They're warmed into the
browser cache on page load so the overlay pops instantly and the gif plays from its first frame.
Swap in your own by replacing the files with the same names (no server restart needed for asset
swaps — but per `CLAUDE.md` still `docker compose up -d --build` so the running container never
serves a stale image).

## The corner-toast thumbnail

The small toast shows a self-contained animated **`win.svg`** / **`loss.svg`** (or `win.gif` /
`loss.gif` if you drop those in — the toast loads the `.gif` first and falls back to the `.svg` on a
404). These are tiny (rendered ~54×54) and work out of the box with no downloads.
