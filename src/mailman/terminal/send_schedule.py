"""Send-time scheduler (v3.22) - the human-simulation engine.

Compiles a list of N recipients into N `send_at` UTC timestamps spread across
daily work-hours windows, by `sender_count` parallel simulated senders, with an
end-of-shift rush (denser sends in the last `rush_tail_minutes`). The terminal
emits these on the packs; the cloud dispatcher fires each when due (§4.2). The
terminal is not involved during sending - the schedule lives entirely in send_at.

Pure function (seedable) so it's unit-testable without a clock.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

_UTC = timezone.utc


def _fmt(dt: datetime) -> str:
    return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_send_ats(
    n: int,
    start_after: datetime,
    *,
    tz: str = "America/Los_Angeles",
    start_hour: int = 9,
    window_hours: int = 4,
    sender_count: int = 3,
    jitter_min_s: int = 60,
    jitter_max_s: int = 210,
    rush_tail_minutes: int = 30,
    rush_jitter_s: int = 15,
    daily_cap: int | None = None,
    seed: int | None = None,
) -> list[str]:
    """Return n UTC-ISO send_at strings, oldest-first.

    start_after: aware datetime; scheduling begins at the first window boundary
    at/after this (or immediately if we're already inside a window).
    daily_cap: max sends per calendar window-day (e.g. Resend free tier = 100/day).
    When a day fills, all senders roll to the next day's window. None = unlimited.
    """
    if n <= 0:
        return []
    if start_after.tzinfo is None:
        start_after = start_after.replace(tzinfo=_UTC)
    rng = random.Random(seed)
    z = ZoneInfo(tz) if ZoneInfo else _UTC

    def window_for(local_date) -> tuple[datetime, datetime]:
        open_local = datetime(local_date.year, local_date.month, local_date.day,
                              start_hour, 0, 0, tzinfo=z)
        close_local = open_local + timedelta(hours=window_hours)
        return open_local.astimezone(_UTC), close_local.astimezone(_UTC)

    def next_window(after: datetime) -> tuple[datetime, datetime, datetime]:
        """First window whose close is after `after`. Returns (open, close, start)
        where start = max(open, after) clamped inside the window."""
        d = after.astimezone(z).date()
        for _ in range(400):  # search up to ~1 year forward
            o, c = window_for(d)
            if c > after:
                start = max(o, after)
                if start < c:
                    return o, c, start
            d = d + timedelta(days=1)
        raise RuntimeError("no send window found within a year")

    o, c, start = next_window(start_after)
    # Stagger the 3 senders slightly so they interleave rather than fire in lockstep.
    senders = []
    for i in range(max(sender_count, 1)):
        t = start + timedelta(seconds=i * (jitter_min_s / max(sender_count, 1)))
        if t >= c:
            o2, c2, t = next_window(c)
            senders.append({"open": o2, "close": c2, "t": t})
        else:
            senders.append({"open": o, "close": c, "t": t})

    out: list[str] = []
    day_counts: dict = {}          # local window-date -> sends scheduled that day
    emitted = 0
    guard = 0
    while emitted < n:
        guard += 1
        if guard > n * 8 + 2000:   # safety - never spin forever
            break
        s = min(senders, key=lambda x: x["t"])
        day = s["t"].astimezone(z).date()
        # Daily cap: if this sender's window-day is full, roll it to the next window.
        if daily_cap and day_counts.get(day, 0) >= daily_cap:
            o2, c2, start2 = next_window(s["close"])
            s["open"], s["close"], s["t"] = o2, c2, start2
            continue
        out.append(_fmt(s["t"]))
        day_counts[day] = day_counts.get(day, 0) + 1
        emitted += 1
        rush_start = s["close"] - timedelta(minutes=rush_tail_minutes)
        if s["t"] >= rush_start:
            gap = rush_jitter_s * (0.5 + rng.random())   # end-of-shift rush
        else:
            gap = rng.uniform(jitter_min_s, jitter_max_s)
        nxt = s["t"] + timedelta(seconds=gap)
        if nxt >= s["close"]:
            o2, c2, start2 = next_window(s["close"])
            s["open"], s["close"], s["t"] = o2, c2, start2
        else:
            s["t"] = nxt
    out.sort()
    return out


def next_window_open_after(
    after: datetime, *, tz: str = "America/Los_Angeles",
    start_hour: int = 9, window_hours: int = 4,
) -> str:
    """UTC-ISO of the next window's OPEN strictly after `after` (the cooldown
    'next day's window' gate). Independent of how many sends are scheduled."""
    if after.tzinfo is None:
        after = after.replace(tzinfo=_UTC)
    z = ZoneInfo(tz) if ZoneInfo else _UTC
    d = after.astimezone(z).date()
    for _ in range(400):
        open_local = datetime(d.year, d.month, d.day, start_hour, 0, 0, tzinfo=z)
        open_utc = open_local.astimezone(_UTC)
        if open_utc > after:
            return _fmt(open_utc)
        d = d + timedelta(days=1)
    raise RuntimeError("no window open found within a year")


def estimate_finish(n: int, start_after: datetime, **kw) -> str:
    """Convenience: the send_at of the last of n recipients (campaign ETA)."""
    ats = compute_send_ats(n, start_after, **kw)
    return ats[-1] if ats else ""
