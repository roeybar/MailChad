"""Basic email cleaner (llm-chad) - the import gate for compiled/scraped lists.

No-network checks run always (fast, reliable): normalize, strict syntax, role-account
drop, disposable-domain drop, in-batch dedup. MX check is opt-in (network) and only
ever DROPS on a confirmed no-MX domain - never on lookup failure (fail-open), so a flaky
resolver can't nuke a whole import.

This is the gate, not a guarantee: it removes the obvious junk (dead syntax, role inboxes,
throwaway domains, dead domains) that drives bounces + complaints on cold lists. It does
NOT detect spam traps - those need a paid verification service. Surfaced honestly so nobody
mistakes "cleaned" for "safe to blast".
"""
from __future__ import annotations

import re

# Stricter than the import's old "@ and ." check, still permissive enough for real addrs.
_EMAIL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
                       r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")

# Role / distribution addresses - low engagement, high complaint + trap risk on cold lists.
_ROLE_LOCALPARTS = {
    "info", "admin", "administrator", "support", "sales", "contact", "hello", "help",
    "office", "enquiries", "inquiries", "noreply", "no-reply", "donotreply", "postmaster",
    "abuse", "webmaster", "hostmaster", "marketing", "team", "mail", "billing", "accounts",
    "careers", "jobs", "hr", "press", "media", "legal", "privacy", "security", "root",
}

# Throwaway / disposable domains (seed list - extend as needed).
_DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "throwawaymail.com", "yopmail.com", "trashmail.com", "getnada.com", "sharklasers.com",
    "maildrop.cc", "dispostable.com", "fakeinbox.com", "temp-mail.org",
}


def clean_email(raw: str) -> tuple[str | None, str | None]:
    """Return (normalized_email, None) if it passes, else (None, reason). No network."""
    if not raw:
        return None, "empty"
    email = raw.strip().lower()
    # strip a leading mailto: and surrounding angle brackets sometimes seen in scrapes
    email = email.removeprefix("mailto:").strip("<> \t")
    if not _EMAIL_RE.match(email):
        return None, "bad_syntax"
    local, _, domain = email.partition("@")
    if local in _ROLE_LOCALPARTS:
        return None, "role_account"
    if domain in _DISPOSABLE_DOMAINS:
        return None, "disposable_domain"
    return email, None


def _has_mx(domain: str, _cache: dict) -> bool | None:
    """True/False if resolvable, None if we can't tell (fail-open). Cached per domain."""
    if domain in _cache:
        return _cache[domain]
    result: bool | None = None
    try:
        import dns.resolver  # type: ignore
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=4.0)
            result = len(answers) > 0
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            # NXDOMAIN = dead domain; NoAnswer = no MX (try A as last resort)
            try:
                dns.resolver.resolve(domain, "A", lifetime=3.0)
                result = True
            except Exception:
                result = False
        except Exception:
            result = None      # timeout / resolver error -> unknown, don't drop
    except ImportError:
        result = None          # dnspython not installed -> MX check unavailable
    _cache[domain] = result
    return result


def clean_rows(emails: list[str], check_mx: bool = False) -> dict:
    """Clean a batch. Returns {clean:[...], dropped:[{email,reason}], stats:{reason:count}}.
    Dedups within the batch. MX (opt-in) only drops on a *confirmed* dead domain."""
    clean: list[str] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    stats: dict[str, int] = {}
    mx_cache: dict[str, bool | None] = {}

    def _drop(email, reason):
        dropped.append({"email": email, "reason": reason})
        stats[reason] = stats.get(reason, 0) + 1

    for raw in emails:
        email, reason = clean_email(raw or "")
        if reason:
            _drop((raw or "").strip().lower(), reason)
            continue
        if email in seen:
            _drop(email, "duplicate")
            continue
        seen.add(email)
        if check_mx:
            mx = _has_mx(email.split("@")[1], mx_cache)
            if mx is False:
                _drop(email, "no_mx")
                continue
        clean.append(email)

    stats["clean"] = len(clean)
    stats["total_in"] = len(emails)
    return {"clean": clean, "dropped": dropped, "stats": stats}
