"""Cloud-side encryption helpers.

Cloud has K_temp (during TTL) + both K_pub keys (always, plaintext -
pubkeys aren't secret). Cloud does NOT have any private keys.

Three functions:
  - decrypt_with_temp:   unwrap a K_temp envelope (for dispatcher)
  - encrypt_for_both:    wrap a payload to K_op_pub AND K_cl_pub
                         (so either terminal can decrypt; cloud cannot)
  - load_pubkeys:        fetch K_op_pub + K_cl_pub from the pubkey table
"""
from __future__ import annotations

import base64
import json
import os

import nacl.public
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mailchad.cloud import dynamo


def encrypt_with_temp(plaintext: bytes, k_temp: bytes) -> bytes:
    """K_temp mode encrypt - mirrors terminal/app/encryption.py:encrypt_with_temp.
    Used by tests and by the terminal when building packs.
    """
    if len(k_temp) != 32:
        raise ValueError(f"K_temp must be 32 bytes, got {len(k_temp)}")
    nonce = os.urandom(12)
    ct = AESGCM(k_temp).encrypt(nonce, plaintext, associated_data=None)
    envelope = json.dumps({
        "v": 1, "mode": "K_temp",
        "nonce": base64.b64encode(nonce).decode(),
        "ct":    base64.b64encode(ct).decode(),
    }).encode()
    return envelope


def decrypt_with_temp(envelope_bytes: bytes, k_temp: bytes) -> bytes:
    """K_temp mode - matches terminal/app/encryption.py:decrypt_with_temp."""
    if len(k_temp) != 32:
        raise ValueError(f"K_temp must be 32 bytes, got {len(k_temp)}")
    envelope = json.loads(envelope_bytes)
    if envelope.get("mode") != "K_temp":
        raise ValueError(f"wrong mode: {envelope.get('mode')!r}")
    nonce = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    return AESGCM(k_temp).decrypt(nonce, ct, associated_data=None)


def load_pubkeys() -> dict[str, nacl.public.PublicKey]:
    """Load K_op_pub + K_cl_pub from DynamoDB. Raises if either missing."""
    raw = dynamo.get_all_pubkeys()  # {actor: bytes}
    bundle = {actor: nacl.public.PublicKey(pub) for actor, pub in raw.items()}
    if "operator" not in bundle or "client" not in bundle:
        raise RuntimeError(
            f"pubkey table missing {{operator,client}}; got {list(bundle.keys())}"
        )
    return bundle


def encrypt_for_both(plaintext: bytes,
                     pubkeys: dict[str, nacl.public.PublicKey] | None = None) -> bytes:
    """Wrap payload so both terminals can decrypt; cloud cannot.

    Matches the K_op+K_cl envelope produced by terminal/app/encryption.py:
    encrypt_for_both. The wrapping key (random per-call AES-256-GCM) is
    encrypted via NaCl SealedBox to each party's pubkey separately.
    """
    if pubkeys is None:
        pubkeys = load_pubkeys()
    k = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(k).encrypt(nonce, plaintext, associated_data=None)
    wrap_op = nacl.public.SealedBox(pubkeys["operator"]).encrypt(k)
    wrap_cl = nacl.public.SealedBox(pubkeys["client"]).encrypt(k)
    envelope = {
        "v": 1, "mode": "K_op+K_cl",
        "nonce":   base64.b64encode(nonce).decode("ascii"),
        "ct":      base64.b64encode(ct).decode("ascii"),
        "wrap_op": base64.b64encode(wrap_op).decode("ascii"),
        "wrap_cl": base64.b64encode(wrap_cl).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")
