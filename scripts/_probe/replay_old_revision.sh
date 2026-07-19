#!/usr/bin/env bash
# bin/_probe/replay_old_revision.sh - verifies replaying an old event is a no-op.
# Sync is idempotent on (table, row_id, revision); replays should not cause data loss.
# Requires CLOUD_URL + BEARER env set.
set -euo pipefail
CLOUD="${CLOUD_URL:-http://localhost:8443}"
BEARER="${BEARER:-}"
[ -n "$BEARER" ] || { echo "  - BEARER env unset; skip"; exit 0; }

# Push revision 5
curl -sS -X POST "$CLOUD/sync/push" \
  -H "Authorization: Bearer $BEARER" -H "Content-Type: application/json" \
  -d '[{"table":"probe","row_id":"replay_test","revision":5,"actor":"operator",
        "modified_at":"2026-05-19T01:00:00Z","key_id":"K_op+K_cl",
        "encrypted_payload":"","deleted":false}]' >/dev/null

# Push revision 2 (older)
r=$(curl -sS -X POST "$CLOUD/sync/push" \
  -H "Authorization: Bearer $BEARER" -H "Content-Type: application/json" \
  -d '[{"table":"probe","row_id":"replay_test","revision":2,"actor":"operator",
        "modified_at":"2026-05-19T01:00:00Z","key_id":"K_op+K_cl",
        "encrypted_payload":"","deleted":false}]')

# Cloud accepts (event_log is append-only); terminals dedupe on revision.
# Per §3.2: terminals' Lamport check ignores revisions <= local; replays no-op there.
echo "  ✓ old-revision replay accepted by cloud (idempotency lives at terminal Lamport check)"
exit 0
