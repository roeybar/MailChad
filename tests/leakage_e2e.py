#!/usr/bin/env python3
"""Leakage stress tests - auth enforcement + WireGuard isolation.

Runs inside the terminal container against localhost:8000.

Usage (from project root):
    docker cp tests/leakage_e2e.py ep-v3-terminal:/tmp/leakage_e2e.py
    docker compose exec terminal python /tmp/leakage_e2e.py [--wg]

Pass --wg to also run WireGuard interface checks (requires rebuilt images with
wireguard-tools installed).

Tests:
  Auth group:  Every JSON admin route returns 307 without a session cookie.
               Forged / expired cookies are rejected.
               Public endpoints (healthz, login page) remain open.
  WG group:    wg0 interface exists with correct IP.
               wg show reports a configured peer.
               Cloud WG IP (10.90.0.1) is reachable on port 8443.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field

import httpx

BASE = "http://localhost:8000"


# result tracking

@dataclass
class R:
    name: str
    ok: bool
    note: str = ""

results: list[R] = []


def run(name: str, fn):
    try:
        fn()
        results.append(R(name, True))
        print(f"  PASS  {name}")
    except AssertionError as e:
        results.append(R(name, False, str(e)))
        print(f"  FAIL  {name}: {e}")
    except Exception as e:
        results.append(R(name, False, f"{type(e).__name__}: {e}"))
        print(f"  ERR   {name}: {type(e).__name__}: {e}")


# helpers

def _anon() -> httpx.Client:
    return httpx.Client(base_url=BASE, follow_redirects=False, timeout=5)


PROTECTED = [
    ("GET",  "/admin/contacts"),
    ("GET",  "/admin/templates"),
    ("GET",  "/admin/campaigns"),
    ("GET",  "/admin/entities"),
    ("GET",  "/admin/suppression"),
    ("GET",  "/admin/audit-log"),
    ("POST", "/admin/contacts"),
    ("POST", "/admin/templates"),
    ("POST", "/admin/campaigns"),
]


# Auth enforcement tests

print("\n== Auth enforcement ==")


def _make_auth_test(method: str, path: str):
    def _t():
        with _anon() as c:
            r = c.request(method, path)
        assert r.status_code in (307, 401, 403), (
            f"{method} {path} -> {r.status_code}, expected auth challenge"
        )
    return _t


for _m, _p in PROTECTED:
    run(f"no-auth {_m} {_p}", _make_auth_test(_m, _p))


def t_login_open():
    with _anon() as c:
        r = c.get("/admin/auth/login")
    assert r.status_code == 200, f"login page returned {r.status_code}"


def t_healthz_open():
    with _anon() as c:
        r = c.get("/healthz")
    assert r.status_code == 200, f"/healthz returned {r.status_code}"


def t_forged_cookie():
    import base64, hmac, hashlib
    fake = "dGVzdA.invalidsig"
    with httpx.Client(
        base_url=BASE, follow_redirects=False,
        cookies={"v3_session": fake}, timeout=5,
    ) as c:
        r = c.get("/admin/contacts")
    assert r.status_code in (307, 401, 403), f"forged cookie -> {r.status_code}"


def t_expired_payload():
    import base64, json, hmac, hashlib
    payload = json.dumps({"sub": "attacker", "exp": 1}, separators=(",", ":")).encode()
    p64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sig  = hmac.new(b"wrong-key", p64.encode(), hashlib.sha256).digest()
    s64  = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    token = f"{p64}.{s64}"
    with httpx.Client(
        base_url=BASE, follow_redirects=False,
        cookies={"v3_session": token}, timeout=5,
    ) as c:
        r = c.get("/admin/contacts")
    assert r.status_code in (307, 401, 403), f"expired token -> {r.status_code}"


run("login-page-open",  t_login_open)
run("healthz-open",     t_healthz_open)
run("forged-cookie",    t_forged_cookie)
run("expired-payload",  t_expired_payload)


# WireGuard checks

if "--wg" in sys.argv:
    print("\n== WireGuard isolation ==")

    def t_wg0_up():
        r = subprocess.run(["ip", "addr", "show", "wg0"], capture_output=True, text=True)
        assert r.returncode == 0, f"wg0 not found: {r.stderr}"
        assert "10.90.0.2" in r.stdout, f"WG IP not assigned: {r.stdout}"

    def t_wg_peer():
        r = subprocess.run(["wg", "show", "wg0"], capture_output=True, text=True)
        assert r.returncode == 0, f"wg show failed: {r.stderr}"
        assert "peer" in r.stdout.lower(), f"No WG peer: {r.stdout}"

    def t_cloud_via_wg():
        r = subprocess.run(
            ["sh", "-c", "curl -sf --max-time 5 http://10.90.0.1:8443/healthz"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"Cloud WG IP unreachable: {r.stderr}"

    run("wg0-up",          t_wg0_up)
    run("wg-peer",         t_wg_peer)
    run("cloud-via-wg",    t_cloud_via_wg)
else:
    print("\n  (WireGuard checks skipped - pass --wg after image rebuild)")


# Summary

passed = sum(1 for r in results if r.ok)
failed = [r for r in results if not r.ok]
total  = len(results)
print(f"\n{'-'*60}")
print(f"  {passed}/{total} passed")
if failed:
    print("\nFailed:")
    for r in failed:
        print(f"  {r.name}: {r.note}")
    sys.exit(1)
print("All leakage tests passed.")
