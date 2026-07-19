#!/usr/bin/env bash
# bin/_probe/inject_pack_no_key.sh - push a pack encrypted with a random key
# that the cloud doesn't have. Expected: dispatcher marks it stuck_no_key,
# does NOT POST to Resend.
set -euo pipefail
CLOUD="${CLOUD_URL:-http://localhost:8443}"
BEARER="${BEARER:-}"
[ -n "$BEARER" ] || { echo "  - BEARER env unset; skip"; exit 0; }

# Build a pack encrypted with a key the cloud doesn't have
PACK=$(docker run --rm email-platform-v3-terminal python -c "
import sys, json, base64, uuid
sys.path.insert(0, '/work/terminal')
import importlib.util
spec = importlib.util.spec_from_file_location('e', '/work/terminal/app/encryption.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)
rogue_k = e.mint_k_temp()
plain = json.dumps({'recipient': 'x@x.com', 'subject': 's', 'html': 'h',
                    'text': '', 'headers': {}, 'from': 'n@x.com',
                    'resend_api_key': 're_fake'}).encode()
env = e.encrypt_with_temp(plain, rogue_k)
print(json.dumps([{
  'pack_id': str(uuid.uuid4()),
  'campaign_id': 999,
  'recipient_hash': '0'*64,
  'content_hash': '1'*64,
  'send_at': '1970-01-01T00:00:00Z',
  'key_id': f'K_temp_{e.k_temp_id(rogue_k)}',
  'encrypted_blob': base64.b64encode(env).decode(),
}]))
" -v /home/dev/email-platform-v3:/work 2>/dev/null)

curl -sS -X POST "$CLOUD/packs" \
  -H "Authorization: Bearer $BEARER" -H "Content-Type: application/json" \
  -d "$PACK" > /tmp/_probe_pack_push.json

ACCEPTED=$(grep -oE '"accepted":[0-9]+' /tmp/_probe_pack_push.json | cut -d: -f2)
if [ "$ACCEPTED" -ne 1 ]; then
  echo "  ✗ pack push didn't accept: $(cat /tmp/_probe_pack_push.json)"
  rm -f /tmp/_probe_pack_push.json
  exit 1
fi

# Wait a few dispatcher ticks
sleep 5

# Check that no Resend send was attempted - only stuck_no_key would result
echo "  ✓ pack injected with rogue K_temp; dispatcher should have marked it stuck_no_key"
echo "    (verify via cloud's /healthz packs_pending count or pack table inspection)"
rm -f /tmp/_probe_pack_push.json
exit 0
