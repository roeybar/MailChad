#!/bin/sh
# cloud entrypoint - sets up WireGuard tunnel then starts uvicorn.
set -e
cd /app

# WireGuard setup (dev: keys from env; prod: keys from secrets manager)
if [ -n "$CLOUD_WG_PRIVATE_KEY" ] && [ -n "$TERMINAL_WG_PUBLIC_KEY" ]; then
  ip link add wg0 type wireguard 2>/dev/null || true
  echo "$CLOUD_WG_PRIVATE_KEY" | wg set wg0 private-key /dev/stdin
  wg set wg0 listen-port 51820 \
    peer "$TERMINAL_WG_PUBLIC_KEY" \
      allowed-ips 10.90.0.2/32
  ip addr add 10.90.0.1/24 dev wg0 2>/dev/null || true
  ip link set wg0 up
  echo "WireGuard wg0 up: cloud=10.90.0.1, peer terminal=10.90.0.2"
else
  echo "WireGuard skipped: CLOUD_WG_PRIVATE_KEY or TERMINAL_WG_PUBLIC_KEY not set"
fi

exec uvicorn mailchad.cloud.main:app --host 0.0.0.0 --port 8443
