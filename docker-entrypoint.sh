#!/bin/sh
# Stage the read-only-mounted SSH key into a private, strict-perm location before launching.
# OpenSSH refuses a key that is group/world-readable; a bind-mounted file keeps the host's perms
# (often 0644), so we copy it to a fresh 0600 file. The dashboard runs fine WITHOUT the key —
# vps_health just reports the VPS unreachable — so a missing key is a warning, never fatal.
set -e

KEY_SRC="${XEMM_SSH_KEY_PATH:-/keys/lighter.pem}"
KEY_DST="/root/.ssh/lighter.pem"

if [ -f "$KEY_SRC" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    cp "$KEY_SRC" "$KEY_DST"
    chmod 600 "$KEY_DST"
    # pre-seed the host key so the first probe doesn't pay an accept-new round-trip (best-effort)
    if [ -n "$XEMM_SSH_HOST" ]; then
        ssh-keyscan -T 5 "$XEMM_SSH_HOST" >> /root/.ssh/known_hosts 2>/dev/null || true
    fi
else
    echo "[entrypoint] no SSH key at $KEY_SRC — VPS health/latency probes will report unreachable" >&2
fi

exec "$@"
