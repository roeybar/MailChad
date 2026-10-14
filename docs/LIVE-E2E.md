# Live E2E checks

## Purpose and safety

`tests/live_e2e.py` runs inside this project's terminal container against
`http://localhost:8000` by default. Use an attended, disposable demo rig.
`TARGET_EMAILS` remains contact-only test data; real-send blocks are gated.

Real mail requires `E2E_REAL_SEND=1` and an explicit comma-separated
`E2E_REAL_SEND_TO` recipient list. The real-send leg is currently deferred:
the gate always returns a SKIP reason, even when every prerequisite is met.

Recipients with no `@` are refused. The domain is the lower-cased text after
the last `@`. The gate refuses `example.com`, `example.net`, `example.org`
and their subdomains, and any domain whose last label is `test`, `example`,
`invalid` or `localhost`, including those bare domains (RFC 2606/6761).
It reports `E2E_REAL_SEND_TO refused: malformed or reserved (RFC 2606/6761) address`.
The script never mails example.com or the other reserved names.

## Environment variables

| Name | Meaning |
| --- | --- |
| `E2E_BASE` | Terminal URL; defaults to `http://localhost:8000` inside the container. |
| `E2E_ADMIN` | Required login email for the disposable terminal; no default. |
| `E2E_PASSWD` | Required login password for that account; no default. Supply through the environment. |
| `CLOUD_URL` | Cloud endpoint; cloud checks can also consult terminal settings where supported. |
| `E2E_REAL_SEND` | Only the literal `1` opts in. Unset or any other value skips real-send checks. |
| `E2E_REAL_SEND_TO` | Comma-separated recipients, trimmed and lower-cased. Empty lists and addresses without `@` are refused. Domains `example.com`, `example.net`, `example.org` and their subdomains, plus domains with last label `test`, `example`, `invalid` or `localhost` (including bare domains), are refused under RFC 2606/6761. |
| `RESEND_API_KEY` | This project's own secret. Checked for presence only. |

If either login variable is unset or empty, the script prints a one-line error
naming the missing variable and exits with code `2` before any request.

No key or recipient value is printed in the startup summary: it reports opt-in,
key presence and recipient count. Cloud-dependent real-send checks also require
the container's own cloud bearer and a successful `/healthz` response.
The direct test-send block uses `need_cloud=False`; it still ends in DEFERRED.

## Sections

| Class | Sections or labels |
| --- | --- |
| Needs neither cloud nor key | Auth, navigation, contact/template/entity/campaign CRUD, suppression, edge-case and stress CRUD, audit, settings, system status, schedules, campaign operations/stages, rejected quicksend, selective export/import, contact/campaign edits, template/tag deletion, backup, test-send chain, add-to-campaign, batch tag import and seed CRUD. Local assertions in rate limiting and open tracking also run. |
| Needs cloud | `lambda rate limit`, `lambda pack dispatch`, lambda webhook and sync/pull checks. Pack dispatch and sync/pull also need the container's bearer. |
| Real mail, gated | `REAL SEND (global key)` |
| Real mail, gated | `initial launch -> 200` (retry and cancel remain unconditional) |
| Real mail, gated | `test-send -> handled (200 or 400)` |
| Real mail, gated | `per-day lock launches (A, B)` |
| Real mail, gated | `batches/seeds launch` |
| Fake per-entity key | Entity set-key, stress key checks, entity campaign launch, and entity test-connection checks. These retain their assertions; test-connection may call Resend but does not send mail. |

The pre-flight cleanup purges `seed-e2e@example.com`. Seed CRUD deletes its
created seed after its checks, before the real-send gate. The gate precedes
the batch-size setting change.
Before cleanup, the script refuses (exit 2) a terminal that still holds an
agent_token setting from an older remote-API version; it never prints the value.

## Attended demo run

Run only against a dedicated disposable terminal with explicit `E2E_ADMIN`
and `E2E_PASSWD`. Configure its URL using `E2E_BASE`. The script's `main()`
performs cleanup and test-data mutations after the pre-flight check.
The `scripts/v3 test` default suite excludes this script; there is no dedicated
live-e2e wrapper verb in this checkout.

## Real-send run: DEFERRED

The gate returns `real-send leg deferred: recipients not wired into send sections`
when all prerequisites are satisfied. A missing prerequisite or reserved
recipient reports its earlier SKIP reason.

## Reading results

PASS means an executed assertion succeeded. FAIL means an assertion failed,
including an unexpected exception. SKIP names an unmet prerequisite or the
deferred real-send leg; it does not prove a send succeeded. The final summary
counts passed, failed and skipped results separately. Exit code is `0` when
there are no failures, `1` when a failure occurs or the terminal is unreachable,
and `2` when the pre-flight refuses.

The script reports each executed assertion and each skipped block. Seed CRUD
expects JSON from `/admin/seeds`, while that route is shadowed by an HTML page;
this can raise an exception before the batches/seeds gate is reached.
