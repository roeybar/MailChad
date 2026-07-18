# Send strategy - batched, windowed, seeded

How MailChad paces a campaign. Goal: organic-looking, deliverability-safe, fully
observable sending, with hard caps so an ESP's limits are never blown.

## The model

**Batches.** A campaign's deliverable contacts split into ordered batches of
`send_batch_size`. Each batch is a row in `campaign_batches` with rolling counts.
Batch 1 releases on launch; every later batch is gated.

**Manual approve-next, with an analytics cooldown.** When a batch fully drains it
enters `cooldown`. Approve-next stays locked until the next day's window (~24h) so
bounce/click/unsub data propagates first - you approve the next batch on real data,
never blind.

**Daily send window.** Sending happens only inside a window of `send_window_hours`
starting at `send_window_start_hour` in `send_window_tz` (DST-aware). Outside the
window, sending pauses and resumes next day. A batch spans as many windows as needed.

**Simulated senders + end-of-shift rush.** `send_sender_count` senders pace with
human jitter (`send_jitter_min_s`–`send_jitter_max_s`). In the final
`send_rush_tail_minutes` the jitter collapses toward `send_rush_jitter_s` - a burst to
clear the queue before end of shift, mirroring real human behaviour and defeating
fixed-interval detection.

**Per-day cap.** `send_daily_cap` bounds sends per calendar window-day; when a day
fills, all senders roll to the next window. Set this **under your ESP's daily limit**
(e.g. a 100/day plan -> cap 90). Without it, the sender pacing can exceed the plan's
quota and sends start failing.

**Safety.** Bounce-pause threshold and per-minute rate cap remain; the per-day
single-campaign contact lock prevents a contact receiving more than one campaign per day.

## How it's enforced (terminal-side scheduling)

The window + pacing is a *terminal* concern. On launch/approval the scheduler computes a
`send_at` per pack that falls inside upcoming windows at the sender + rush + cap cadence,
and pushes the batch with those future `send_at`s; the cloud dispatcher releases each pack
when it comes due (§4.2). The terminal is uninvolved during sending.

Packs are pushed in chunks (`PACK_PUSH_CHUNK`) - each carries an encrypted payload, so a
full batch in one request exceeds API-gateway request-size limits.

## Engagement signal: clicks only

Opens are **not tracked**. Image proxies and security scanners fire the open pixel (and
often the click redirect too), so opens are bot/MPP noise:
- `open_tracking=False` on all sends; `click_tracking` stays on.
- `email.opened` webhooks are ignored by the materialiser (kept raw for forensics only).
- Opens are absent from analytics, batch-health, and agent surfaces.

Engagement = body-link clicks. Note clicks still carry some scanner noise (a scanner
click typically lands seconds after delivery; a human's arrives later) - treat click
counts as an upper bound.

## Seed addresses / inbox-placement monitoring

Placement must be measured, not assumed. Maintain a seed list of mailboxes you control
across the providers that matter (Gmail, Outlook, Yahoo, iCloud, corporate).

- **Salt every batch** - the active seed set rides every batch; batch 1's salt doubles as
  the pre-flight check. Seeds are additive (they don't count toward batch size) and are
  excluded from campaign analytics, suppression and the contact lock.
- **Placement detection** - manual: check the seed inboxes and log inbox/spam per provider
  on the campaign's batch card. (IMAP auto-detection is a possible future addition.)

**Seeding measures placement; it does not fix it.** If seeds land in spam, the levers are
SPF/DKIM/DMARC alignment, domain/IP warmup, list hygiene, content (spam-trigger phrases,
link/image ratio, branded vs raw compliance links), and recipient engagement.

## Settings reference

`send_window_tz`, `send_window_start_hour`, `send_window_hours`, `send_sender_count`,
`send_jitter_min_s`, `send_jitter_max_s`, `send_rush_tail_minutes`, `send_rush_jitter_s`,
`send_batch_size`, `send_daily_cap` - all editable at `/admin/settings/sending`; changes
apply to the next batch loaded, no redeploy.
