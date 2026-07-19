#!/usr/bin/env python3
"""bin/_init_handshake.py - interactive handshake script (§7, §13.7).

Runs INSIDE the email-platform-v3-terminal container so we have nacl available.

--cloud is the cloud's public base URL. In production this is the AWS API
Gateway URL produced by `bin/v3 deploy-api`, e.g.:
    https://<api-gw-id>.execute-api.us-east-1.amazonaws.com
or a custom domain mapped to it:
    https://api.your-domain.com
For local dev only: http://cloud:8443

Algorithm:
  1. Generate own X25519 keypair -> write to KEYS_DIR/own_kem_priv.key + own_kem_pub.key
  2. POST /init/handshake to cloud with role + own_kem_pub + X-Bootstrap-Token
  3. Receive bearer token from cloud -> store at KEYS_DIR/cloud_bearer.txt
  4. Print own pubkey for the other party to capture
  5. Prompt for peer's pubkey (paste in) -> write to KEYS_DIR/peer_kem_pub.key
  6. Verify peer's pubkey matches what cloud has registered (via /pubkeys)
  7. (Out-of-band): operator's terminal also receives K_<peer>_priv from peer
     via the screen-share - see step 6 of spec §7. NOT automated; user pastes.

Per Reading A (§2.1), each terminal holds both private keys after step 7.
This step is left for the operator to perform manually during the
screen-share session (cannot automate over the wire securely).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from pathlib import Path

import httpx
import nacl.public

KEYS_DIR = Path(os.environ.get("TERMINAL_KEYS_DIR", "/var/lib/terminal/keys"))


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", choices=["operator", "client"], required=True)
    ap.add_argument("--cloud", required=True, help="https://mail.example.com or http://cloud:8443")
    ap.add_argument("--bootstrap-token", required=True,
                    help="The BOOTSTRAP_TOKEN value set on the cloud server")
    args = ap.parse_args()

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEYS_DIR, 0o700)

    own_priv_path = KEYS_DIR / "own_kem_priv.key"
    own_pub_path  = KEYS_DIR / "own_kem_pub.key"
    peer_pub_path = KEYS_DIR / "peer_kem_pub.key"
    bearer_path   = KEYS_DIR / "cloud_bearer.txt"

    # 1. Generate keypair (or load existing).
    if own_priv_path.exists():
        print(f"[handshake] own keypair already exists at {own_priv_path}; reusing")
        own_priv = nacl.public.PrivateKey(own_priv_path.read_bytes())
    else:
        print(f"[handshake] generating {args.role} X25519 keypair...")
        own_priv = nacl.public.PrivateKey.generate()
        own_priv_path.write_bytes(bytes(own_priv))
        os.chmod(own_priv_path, 0o600)
        own_pub_path.write_bytes(bytes(own_priv.public_key))
        print(f"[handshake] keypair written to {KEYS_DIR}")

    own_pub_b64 = b64(bytes(own_priv.public_key))

    # 2. Register with cloud.
    print(f"[handshake] registering with cloud at {args.cloud} as {args.role}...")
    r = httpx.post(
        f"{args.cloud}/init/handshake",
        json={"role": args.role, "kem_pub": own_pub_b64},
        headers={"X-Bootstrap-Token": args.bootstrap_token},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"[handshake] FAILED: HTTP {r.status_code}")
        print(f"  body: {r.text[:300]}")
        return 1
    data = r.json()
    bearer = data["bearer"]
    print(f"[handshake] ✓ registered (fingerprint={data['fingerprint']})")

    # 3. Store bearer.
    bearer_path.write_text(bearer)
    os.chmod(bearer_path, 0o600)
    print(f"[handshake] ✓ bearer stored at {bearer_path}")

    # 4. Print own pubkey for the other party.
    print()
    print("=" * 72)
    print(f"YOUR PUBLIC KEY ({args.role}) - share this with the other party:")
    print()
    print(f"  {own_pub_b64}")
    print()
    print("=" * 72)

    # 5. Prompt for peer's pubkey.
    print()
    print(f"Now paste the OTHER party's public key (the {('client' if args.role == 'operator' else 'operator')}'s):")
    peer_b64 = input("> ").strip()
    try:
        peer_bytes = b64d(peer_b64)
        if len(peer_bytes) != 32:
            raise ValueError(f"expected 32 bytes, got {len(peer_bytes)}")
    except Exception as e:
        print(f"[handshake] bad pubkey input: {e}")
        return 2

    peer_pub_path.write_bytes(peer_bytes)
    os.chmod(peer_pub_path, 0o600)
    print(f"[handshake] ✓ peer pubkey stored at {peer_pub_path}")

    # 6. Verify against cloud's registered pubkeys.
    try:
        rcheck = httpx.get(f"{args.cloud}/pubkeys", timeout=15)
        rcheck.raise_for_status()
        cloud_keys = rcheck.json()
        peer_role = "client" if args.role == "operator" else "operator"
        if peer_role in cloud_keys:
            if cloud_keys[peer_role] == peer_b64:
                print(f"[handshake] ✓ peer pubkey matches cloud's registered {peer_role} pubkey")
            else:
                print(f"[handshake] ✗ MISMATCH between pasted peer pubkey and cloud's registered {peer_role}")
                print("  This is a serious red flag - investigate before proceeding.")
                return 3
        else:
            print(f"[handshake] note: cloud doesn't have {peer_role} registered yet (their handshake hasn't run)")
    except Exception as e:
        print(f"[handshake] could not verify against cloud: {e}")

    # 7. Reminder for the out-of-band private-key exchange (§7 step 5).
    print()
    print("=" * 72)
    print("NEXT MANUAL STEPS (per spec §7, Reading A):")
    print()
    peer_priv_path = KEYS_DIR / "peer_kem_priv.key"
    print(f"1. The other party should send YOU their PRIVATE key (out-of-band, via the")
    print(f"   screen-share session). When they do, save it as:")
    print(f"     {peer_priv_path}")
    print(f"   (operator gets client_priv; client gets operator_priv - Reading A backup)")
    print()
    print(f"2. Once you have peer_kem_priv.key, fire 'bin/v3 backup' on both PCs +")
    print(f"   to 2 offline media (§9). That's the 6-location backup target.")
    print()
    print(f"3. Restart terminal: `bin/v3 bounce` - sync_client will now have")
    print(f"   CLOUD_BEARER configured + KeyBundle ready.")
    print("=" * 72)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
