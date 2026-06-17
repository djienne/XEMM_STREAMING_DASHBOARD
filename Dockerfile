# XEMM live dashboard — containerized.
# Tiny stdlib HTTP server + websocket price feed + read-only SSH probes to the VPS.
# The durable 1s/trade-history archive lives in a mounted volume (./data), NOT in the image,
# so rebuilding the image never touches a single tick.
FROM python:3.12-slim

# - openssh-client : vps_health.py shells out to `ssh` for the read-only VPS health + latency probes
# - ca-certificates: TLS to the Aster / Hyperliquid REST + WebSocket endpoints
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Etc/UTC

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code only. Data (data/), creds and the trading repo are MOUNTED at runtime, never baked in.
# config*.json copies config.example.json always, plus your local (gitignored) config.json if present.
COPY server.py collectors.py vps_health.py ws_prices.py ./
COPY config*.json ./
COPY static/ ./static/
COPY gifs/ ./gifs/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# strip any CR (the repo may be checked out with CRLF on Windows) so the shebang works, then +x
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8787
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "server.py"]
