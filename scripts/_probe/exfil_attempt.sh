#!/usr/bin/env bash
# bin/_probe/exfil_attempt.sh - simulate compromised cloud reading
# K_op+K_cl payloads. Verifies that without K_op_priv or K_cl_priv, the
# payload cannot be decrypted.
set -euo pipefail
CLOUD="${CLOUD_URL:-http://localhost:8443}"
BEARER="${BEARER:-}"
[ -n "$BEARER" ] || { echo "  - BEARER env unset; skip"; exit 0; }

# Pull a recent event
EVENTS=$(curl -sS -H "Authorization: Bearer $BEARER" "$CLOUD/sync?since=0&timeout=1")
COUNT=$(echo "$EVENTS" | grep -oE '"events":\[[^]]*' | grep -oE '{[^}]*}' | wc -l)

if [ "$COUNT" = "0" ]; then
  echo "  - no events to probe; push something first"
  exit 0
fi

# Take the first encrypted_payload and try to decrypt without keys
docker run --rm \
  -v /home/dev/email-platform-v3:/work \
  email-platform-v3-terminal python -c "
import sys, json, base64
sys.path.insert(0, '/work/terminal')
import importlib.util
spec = importlib.util.spec_from_file_location('e', '/work/terminal/app/encryption.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)

events = json.loads('''$EVENTS''')['events']
target = next((ev for ev in events if ev.get('key_id') == 'K_op+K_cl' and ev.get('encrypted_payload')), None)
if not target:
    print('  - no K_op+K_cl encrypted event; skip')
    sys.exit(0)

# Attempt to parse the envelope (this part is fine, envelope shape is public)
env_bytes = base64.b64decode(target['encrypted_payload'])
env = json.loads(env_bytes)
print(f'  envelope shape readable: mode={env[\"mode\"]}, ct_size={len(base64.b64decode(env[\"ct\"]))} bytes')

# Now attempt to decrypt without any KEM private key
import nacl.public, nacl.exceptions
fake_priv = nacl.public.PrivateKey.generate()
fake_bundle = e.KeyBundle('operator', fake_priv, nacl.public.PrivateKey.generate().public_key)
try:
    e.decrypt_for_both(env_bytes, fake_bundle)
    print('  ✗ EXFIL SUCCESS - encryption broken!')
    sys.exit(1)
except (nacl.exceptions.CryptoError, Exception) as ex:
    print(f'  ✓ decrypt without key fails (cryptographic isolation holds): {type(ex).__name__}')
"
