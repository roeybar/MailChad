#!/bin/sh
# terminal entrypoint - sets up WireGuard tunnel then starts uvicorn.
set -e
cd /app

# WireGuard setup (dev: keys from env; prod: keys from secrets manager)
if [ -n "$TERMINAL_WG_PRIVATE_KEY" ] && [ -n "$CLOUD_WG_PUBLIC_KEY" ]; then
  ip link add wg0 type wireguard 2>/dev/null || true
  echo "$TERMINAL_WG_PRIVATE_KEY" | wg set wg0 private-key /dev/stdin
  # In dev the cloud container is reachable as 'cloud' on the Docker bridge.
  # endpoint resolves at startup; if cloud isn't up yet, WG will retry on handshake.
  CLOUD_HOST="${CLOUD_HOST:-cloud}"
  wg set wg0 \
    peer "$CLOUD_WG_PUBLIC_KEY" \
      allowed-ips 10.90.0.1/32 \
      endpoint "${CLOUD_HOST}:51820" \
      persistent-keepalive 25
  ip addr add 10.90.0.2/24 dev wg0 2>/dev/null || true
  ip link set wg0 up
  echo "WireGuard wg0 up: terminal=10.90.0.2, peer cloud=10.90.0.1"
else
  echo "WireGuard skipped: TERMINAL_WG_PRIVATE_KEY or CLOUD_WG_PUBLIC_KEY not set"
fi

exec uvicorn mailchad.terminal.main:app --host 0.0.0.0 --port 8000
