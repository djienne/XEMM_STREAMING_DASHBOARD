# Trade-trigger GIFs

When a XEMM round completes, the dashboard pops a toast with a celebratory (win) or
commiserating (loss) animation:

| event | shown |
|-------|-------|
| net-positive round | `win.gif` if present, else the built-in **`win.svg`** |
| net-negative round | `loss.gif` if present, else the built-in **`loss.svg`** |

The `*.svg` files here are **self-contained animated fallbacks** — the dashboard works out of
the box with no downloads.

## Use your own GIFs

Drop a real animated GIF next to the SVG and it takes priority automatically (the toast loads
`/gifs/win.gif` first and only falls back to `/gifs/win.svg` on a 404):

```
gifs/win.gif      # e.g. a money-rain / "let's go" clip
gifs/loss.gif     # e.g. a facepalm / sad-trombone clip
```

Keep them small (square, ≤ ~1 MB) — they render at 54×54 in the toast. No server restart needed;
just refresh the page.
