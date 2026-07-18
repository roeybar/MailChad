#!/usr/bin/env bash
# bin/_probe/inject_unsigned_event.sh - attempt to push a sync event with no bearer.
# Expected: 401 Unauthorized. Anything else = auth bypass.
set -euo pipefail
CLOUD="${CLOUD_URL:-http://localhost:8443}"

resp=$(curl -sS -o /tmp/_probe_out -w '%{http_code}' \
  -X POST "$CLOUD/sync/push" \
  -H "Content-Type: application/json" \
  -d '[{"table":"templates","row_id":"injected","revision":99,"actor":"operator",
        "modified_at":"2026-05-19T00:00:00Z","key_id":"K_op+K_cl",
        "encrypted_payload":"AAAA","deleted":false}]')

if [ "$resp" = "401" ]; then
  echo "  ✓ unsigned event rejected (401 as expected)"
  exit 0
fi
echo "  ✗ AUTH BYPASS: expected 401, got $resp"
cat /tmp/_probe_out | head -3
rm -f /tmp/_probe_out
exit 1
