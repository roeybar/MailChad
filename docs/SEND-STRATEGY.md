# Send strategy - batched, windowed, seeded

How MailMan schedules and releases campaign batches.

## The model

**Batches.** A campaign's deliverable contacts split into ordered batches of
`send_batch_size`. Each batch is a row in `campaign_batches` with stored counters.
Normal launch releases batch 1; manual approval releases later pending batches.

**Manual approve-next, with an analytics cooldown.** After a sending batch's
last scheduled `send_at` passes, the scheduler marks it `drained` and sets
`approve_unlock_at` to the next window opening. This tests scheduled time, not
whether every provider outcome has arrived. Approval checks the previous
batch's state and unlock time.

**Daily send window.** Batch `send_at` timestamps fall inside windows of
`send_window_hours`, starting at `send_window_start_hour` in `send_window_tz`
using `zoneinfo`. A batch can span multiple windows. These are due times;
the cloud sweep and HTTP call can dispatch later than the scheduled time.

**Parallel senders + end-of-window rush.** `send_sender_count` send cursors pace
with randomised intervals (`send_jitter_min_s`-`send_jitter_max_s`). In the final
`send_rush_tail_minutes` the gap becomes `send_rush_jitter_s * (0.5 + random())`.
Remaining work rolls to another window if it does not fit.

**Per-day cap.** `send_daily_cap` bounds scheduled sends per calendar window-day
within one `compute_send_ats()` call, including seeds. Zero means unlimited.
It is not a shared quota across batches or campaigns.

**Safety.** The dispatcher computes pacing delays from rate settings and doubles
the delay when its in-process failure ratio reaches the bounce threshold after
more than 10 sends.
The contact lock refuses overlap while another campaign's lock is active;
a body-link click releases that contact early. It is not a strict one-mail-per-day limit.

## How it's enforced (terminal-side scheduling)

The window + pacing is a *terminal* concern. On launch/approval the scheduler computes a
`send_at` per pack that falls inside upcoming windows at the sender + rush + cap cadence,
and pushes the batch with those future `send_at`s; the cloud dispatcher releases each pack
when due. The terminal scheduler still manages batch drain and approval state.

Packs are pushed in chunks (`PACK_PUSH_CHUNK`, default 100) to bound request
sizes.

## Engagement signal: clicks only

The dispatcher and campaign test-send requests set `open_tracking=False`
and `click_tracking=True`.

- `email.opened` webhooks are ignored by the materialiser; the encrypted event
  remains in the inbox.
- The campaign analytics page shows sent, clicked, bounced, complained and
  failed counts, plus the campaign's rate limit and bounce threshold; its
  batches card shows sent, clicked, bounced and unsub counts.

## Seed addresses / inbox-placement monitoring

Active seed addresses are added to a nonempty batch in addition to its contacts.
They do not count toward batch size, but they do count toward its scheduled
send cap. Seed packs have no contact ID or local dispatched-job row, and do
not enter contact analytics or the contact lock. Seed addresses bypass the
contact suppression filter when batch packs are built.

Placement is logged manually as inbox, spam or promotions on the campaign's
batch card. No automated inbox-placement detector is implemented.

## Settings reference

`send_window_tz`, `send_window_start_hour`, `send_window_hours`, `send_sender_count`,
`send_jitter_min_s`, `send_jitter_max_s`, `send_rush_tail_minutes`, `send_rush_jitter_s`,
`send_batch_size` - editable at `/admin/settings/sending`; changes apply to the
next batch loaded. `send_daily_cap` is read from settings but has no field on
that page; it can be set through `/admin/settings/api`.
