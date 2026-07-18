"""Email cleaner gate (llm-chad) - unit tests. Pure (no network; MX off)."""
from __future__ import annotations

from mailchad.terminal import email_clean as ec


def test_valid_passes_and_normalizes():
    assert ec.clean_email("  John.Doe@Example.COM ") == ("john.doe@example.com", None)
    assert ec.clean_email("mailto:a@b.io") == ("a@b.io", None)


def test_bad_syntax_dropped():
    for bad in ["nope", "a@", "@b.com", "a@b", "a b@c.com", "a@@b.com", ""]:
        email, reason = ec.clean_email(bad)
        assert email is None and reason in ("bad_syntax", "empty"), bad


def test_role_accounts_dropped():
    for r in ["info@acme.com", "support@acme.com", "noreply@acme.com", "admin@x.io"]:
        assert ec.clean_email(r)[1] == "role_account", r


def test_disposable_dropped():
    assert ec.clean_email("guy@mailinator.com")[1] == "disposable_domain"


def test_real_address_survives():
    assert ec.clean_email("jane.smith@example.com")[0] == "jane.smith@example.com"


def test_clean_rows_dedups_and_reports():
    emails = ["a@b.com", "A@B.COM", "info@b.com", "bad", "x@mailinator.com", "real@firm.co"]
    out = ec.clean_rows(emails, check_mx=False)
    assert out["clean"] == ["a@b.com", "real@firm.co"]
    reasons = {d["reason"] for d in out["dropped"]}
    assert reasons == {"duplicate", "role_account", "bad_syntax", "disposable_domain"}
    assert out["stats"]["clean"] == 2 and out["stats"]["total_in"] == 6


def test_mx_failopen_when_unknown(monkeypatch):
    # _has_mx returns None (unknown) -> must NOT drop (fail-open)
    monkeypatch.setattr(ec, "_has_mx", lambda d, c: None)
    out = ec.clean_rows(["real@firm.co"], check_mx=True)
    assert out["clean"] == ["real@firm.co"]


def test_mx_drops_confirmed_dead(monkeypatch):
    monkeypatch.setattr(ec, "_has_mx", lambda d, c: False)
    out = ec.clean_rows(["real@deaddomain.co"], check_mx=True)
    assert out["clean"] == [] and out["dropped"][0]["reason"] == "no_mx"
