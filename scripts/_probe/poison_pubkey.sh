#!/usr/bin/env bash
# bin/_probe/poison_pubkey.sh - try to re-register a role's pubkey with the
# bootstrap token without first deleting. Expected: 409.
set -euo pipefail
CLOUD="${CLOUD_URL:-http://localhost:8443}"
BOOT="${BOOTSTRAP_TOKEN:-}"
[ -n "$BOOT" ] || { echo "  - BOOTSTRAP_TOKEN env unset; skip"; exit 0; }

# Generate a different pubkey
NEW_PUB=$(docker run --rm email-platform-v3-terminal python -c "
import nacl.public, base64
print(base64.b64encode(bytes(nacl.public.PrivateKey.generate().public_key)).decode())
")

resp=$(curl -sS -o /tmp/_probe_out -w '%{http_code}' \
  -X POST "$CLOUD/init/handshake" \
  -H "Content-Type: application/json" \
  -H "X-Bootstrap-Token: $BOOT" \
  -d "{\"role\":\"operator\",\"kem_pub\":\"$NEW_PUB\"}")

if [ "$resp" = "409" ]; then
  echo "  ✓ pubkey re-register rejected (409 - handshake is one-shot per role)"
  rm -f /tmp/_probe_out
  exit 0
fi
echo "  ✗ HANDSHAKE BYPASS: expected 409, got $resp"
cat /tmp/_probe_out | head -3
rm -f /tmp/_probe_out
exit 1
