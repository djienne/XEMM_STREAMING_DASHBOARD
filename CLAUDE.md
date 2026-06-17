# XEMM live dashboard — agent notes

This dashboard runs in **Docker**, not natively (`python server.py`).

## ALWAYS rebuild + restart the container after ANY code change

After editing any dashboard code — Python, `config.json`, `Dockerfile`,
`requirements.txt`, or even the `static/` / `gifs/` assets — rebuild and restart so the
running container reflects the change and never serves a stale image:

```bash
docker compose up -d --build
```

This single command rebuilds the image (cached layers make it fast when only a few files
changed) and recreates/restarts the container. The durable archive in the mounted
`./data` volume survives every rebuild — no 1s ticks or trade history are ever lost.

> `static/` and `gifs/` are also bind-mounted read-only, so pure css/js/html tweaks show on
> a plain browser refresh too — but still run the rebuild+restart so nothing drifts.

## Ops

- Apply changes / start: `docker compose up -d --build`
- Tail logs: `docker compose logs -f`
- Stop (keep container + data): `docker compose stop`
- Remove container (data volume survives): `docker compose down`
- URL: http://127.0.0.1:8787 (published on host loopback only)

Data safety: `./data` is `[rw]` (persistent, same file inside and outside the container);
the trading repo (`..` → `/trading`), UI assets, and the SSH key are all mounted `[ro]`.
See `README.md` for the full architecture.
