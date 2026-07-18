#!/usr/bin/env bash
# bin/_probe/sync_flood.sh - push 1K events rapidly. Cloud should handle
# without errors. Tests for rate-limit or DB lock issues at moderate scale.
set -euo pipefail
CLOUD="${CLOUD_URL:-http://localhost:8443}"
BEARER="${BEARER:-}"
N="${N:-1000}"
[ -n "$BEARER" ] || { echo "  - BEARER env unset; skip"; exit 0; }

start=$(date +%s)
batch=$(python3 -c "
import json
events = [{
  'table': 'probe_flood', 'row_id': f'r{i}', 'revision': 1, 'actor': 'operator',
  'modified_at': '2026-05-19T00:00:00Z', 'key_id': 'K_op+K_cl',
  'encrypted_payload': '', 'deleted': False,
} for i in range($N)]
print(json.dumps(events))
")

resp=$(curl -sS -X POST "$CLOUD/sync/push" \
  -H "Authorization: Bearer $BEARER" -H "Content-Type: application/json" \
  -d "$batch" -o /tmp/_probe_flood_out -w '%{http_code}')

elapsed=$(($(date +%s) - start))

if [ "$resp" = "200" ]; then
  accepted=$(grep -oE '"accepted":[0-9]+' /tmp/_probe_flood_out | cut -d: -f2)
  echo "  ✓ flood $N events accepted in ${elapsed}s (accepted=$accepted)"
  rm -f /tmp/_probe_flood_out
  exit 0
fi
echo "  ✗ flood failed: $resp"
cat /tmp/_probe_flood_out | head -3
rm -f /tmp/_probe_flood_out
exit 1
