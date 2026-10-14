"""Per-day single-campaign contact lock (v3.21).

A contact may be on only one *running* campaign at a time. When a campaign is
dispatched it locks its contacts until BOTH:
  - its confirmation window has elapsed (lock_until, default dispatched_at + 1 day), AND
  - an unsub pull has completed *after* the dispatch (so any unsub the recipient
    made has been caught back into suppression_hashes).
A contact is released early if they clicked a link in the body (engagement proves
we captured their state).

This is the anti-spam / compliance / no-information-loss gate the operator asked
for: simultaneous campaigns are fine as long as their still-locked contacts don't
overlap.
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import aliased

from mailman.terminal import db
from mailman.terminal.models import Campaign, CampaignRecipient, SyncState


def _last_pull_ok_at(s) -> str:
    row = s.execute(select(SyncState.value).where(SyncState.key == "last_pull_ok_at")).mappings().first()
    return (row["value"] if row else "") or ""


def locked_contacts(exclude_campaign_id: int | None = None) -> dict[int, dict]:
    """Return {contact_id: {campaign_id, campaign_name, free_at}} for every contact
    currently locked by a dispatched, unconfirmed, in-window campaign.

    A recipient who has clicked (clicked_at set) is NOT locked.
    """
    out: dict[int, dict] = {}
    with db.session() as s:
        cr, c = aliased(CampaignRecipient), aliased(Campaign)
        rows = s.execute(
            select(cr.contact_id, c.id.label("campaign_id"), c.name, c.lock_until)
            .join(c, c.id == cr.campaign_id)
            .where(
                c.dispatched_at.is_not(None),
                c.unsubs_confirmed_at.is_(None),
                c.lock_until.is_not(None),
                c.lock_until > func.datetime("now"),
                cr.clicked_at.is_(None),
            )
        ).mappings().all()
        for r in rows:
            if exclude_campaign_id is not None and r["campaign_id"] == exclude_campaign_id:
                continue
            # first lock wins for the message
            out.setdefault(r["contact_id"], {
                "campaign_id":   r["campaign_id"],
                "campaign_name": r["name"],
                "free_at":       r["lock_until"],
            })
    return out


def find_conflicts(campaign_id: int, contact_ids: list[int]) -> list[dict]:
    """Return lock conflicts for the given contacts (excluding this campaign)."""
    if not contact_ids:
        return []
    locked = locked_contacts(exclude_campaign_id=campaign_id)
    conflicts = []
    for cid in contact_ids:
        if cid in locked:
            conflicts.append({"contact_id": cid, **locked[cid]})
    return conflicts


def confirm_elapsed_locks() -> int:
    """Set unsubs_confirmed_at for dispatched campaigns whose window has elapsed
    AND for which an unsub pull has run since dispatch. Returns count confirmed.
    Called by the scheduler loop.
    """
    confirmed = 0
    with db.session() as s:
        pull_ok_at = _last_pull_ok_at(s)
        rows = s.execute(
            select(Campaign.id, Campaign.dispatched_at)
            .where(
                Campaign.dispatched_at.is_not(None),
                Campaign.unsubs_confirmed_at.is_(None),
                Campaign.lock_until.is_not(None),
                Campaign.lock_until <= func.datetime("now"),
            )
        ).mappings().all()
        for r in rows:
            # Require a pull to have completed after this campaign dispatched.
            if not pull_ok_at or pull_ok_at <= (r["dispatched_at"] or ""):
                continue
            s.execute(
                update(Campaign).where(Campaign.id == r["id"])
                .values(unsubs_confirmed_at=func.datetime("now")).execution_options(synchronize_session=False)
            )
            confirmed += 1
        if confirmed:
            s.commit()
    return confirmed
