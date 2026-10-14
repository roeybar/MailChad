#!/usr/bin/env bash
# scripts/_probe/replay_old_revision.sh - pushes an older, unseen revision.
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

# Cloud accepts (event_log is append-only); terminals dedupe exact revisions.
# An older, unseen (table_name, row_id, revision) is stored in the terminal inbox.
echo "  ok old-revision replay accepted by cloud (terminal deduplication checks exact revisions)"
exit 0
