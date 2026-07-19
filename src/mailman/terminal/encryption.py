"""Encryption layer for v3.1 - terminal side.

Implements the two modes from §2.2 of the v3.1 spec:

  K_op+K_cl mode  - random per-payload AES-256-GCM key, wrapped twice
                    (once for each party's KEM pubkey via NaCl SealedBox).
                    Cloud CANNOT decrypt. Used for everything except packs.

  K_temp mode      - AES-256-GCM directly with K_temp. Cloud CAN decrypt
                    during TTL. Used for packs only.

Reading A (§2.1): each terminal holds BOTH KEM private keys (own + peer copy).
Allows either terminal to decrypt anything if the other PC dies.
Single-PC compromise = full breach; mitigated only by the 6-location backup
property (§9), not by cryptographic separation.

Crypto stack (§2.4):
  - KEM: NaCl SealedBox (X25519 + XSalsa20-Poly1305)
  - Symmetric: AES-256-GCM via cryptography library
  - Library: PyNaCl + cryptography (widely audited, no algo-choice landmines)
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import nacl.public
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYS_DIR = Path(os.environ.get("TERMINAL_KEYS_DIR", "/var/lib/terminal/keys"))


class KeyBundleNotReady(RuntimeError):
    """Init handshake hasn't run yet."""


class KeyBundle:
    """The four keys a terminal needs after init handshake (§7).

    own_role:        'operator' or 'client'
    own_kem_priv:    nacl.public.PrivateKey - for decrypting messages encrypted to our pubkey
    peer_kem_pub:    nacl.public.PublicKey  - for encrypting messages to the other party
    own_kem_pub:     nacl.public.PublicKey  - derived; for self-encrypting our copy of K_op+K_cl payloads
    k_temp:          bytes (32) | None      - current symmetric key for K_temp mode, refreshed periodically
    """

    def __init__(self, own_role: str, own_kem_priv: nacl.public.PrivateKey,
                 peer_kem_pub: nacl.public.PublicKey, k_temp: bytes | None = None):
        assert own_role in ("operator", "client")
        self.own_role = own_role
        self.own_kem_priv = own_kem_priv
        self.own_kem_pub = own_kem_priv.public_key
        self.peer_kem_pub = peer_kem_pub
        self.k_temp = k_temp

    @classmethod
    def load(cls, role: str) -> "KeyBundle":
        """Load from KEYS_DIR. Raises KeyBundleNotReady if handshake not done."""
        own_priv_path = KEYS_DIR / f"own_kem_priv.key"
        peer_pub_path = KEYS_DIR / f"peer_kem_pub.key"
        if not own_priv_path.exists() or not peer_pub_path.exists():
            raise KeyBundleNotReady("run `bin/v3 init-handshake` to set up keys (§7)")
        own_priv = nacl.public.PrivateKey(own_priv_path.read_bytes())
        peer_pub = nacl.public.PublicKey(peer_pub_path.read_bytes())
        k_temp_path = KEYS_DIR / "k_temp.bin"
        k_temp = k_temp_path.read_bytes() if k_temp_path.exists() else None
        return cls(role, own_priv, peer_pub, k_temp=k_temp)


# K_op+K_cl mode (§2.2)

def encrypt_for_both(plaintext: bytes, bundle: KeyBundle) -> bytes:
    """Random AES-256-GCM key wrapped twice (once for each party).
    Returns a JSON envelope (UTF-8 bytes)."""
    k = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(k).encrypt(nonce, plaintext, associated_data=None)

    op_pub = bundle.own_kem_pub if bundle.own_role == "operator" else bundle.peer_kem_pub
    cl_pub = bundle.own_kem_pub if bundle.own_role == "client" else bundle.peer_kem_pub
    wrap_op = nacl.public.SealedBox(op_pub).encrypt(k)
    wrap_cl = nacl.public.SealedBox(cl_pub).encrypt(k)

    envelope = {
        "v": 1, "mode": "K_op+K_cl",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct":    base64.b64encode(ct).decode("ascii"),
        "wrap_op": base64.b64encode(wrap_op).decode("ascii"),
        "wrap_cl": base64.b64encode(wrap_cl).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decrypt_for_both(envelope_bytes: bytes, bundle: KeyBundle) -> bytes:
    """Decrypt using our own KEM private key (works whether we're operator or client)."""
    envelope = json.loads(envelope_bytes)
    if envelope.get("mode") != "K_op+K_cl":
        raise ValueError(f"wrong mode: {envelope.get('mode')!r}")
    wrap_field = "wrap_op" if bundle.own_role == "operator" else "wrap_cl"
    wrap = base64.b64decode(envelope[wrap_field])
    k = nacl.public.SealedBox(bundle.own_kem_priv).decrypt(wrap)
    nonce = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    return AESGCM(k).decrypt(nonce, ct, associated_data=None)


# K_temp mode (§2.2)

def encrypt_with_temp(plaintext: bytes, k_temp: bytes) -> bytes:
    """For packs only. Cloud will decrypt at dispatch time."""
    if len(k_temp) != 32:
        raise ValueError(f"K_temp must be 32 bytes, got {len(k_temp)}")
    nonce = os.urandom(12)
    ct = AESGCM(k_temp).encrypt(nonce, plaintext, associated_data=None)
    envelope = {
        "v": 1, "mode": "K_temp",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct":    base64.b64encode(ct).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decrypt_with_temp(envelope_bytes: bytes, k_temp: bytes) -> bytes:
    """Used by the cloud dispatcher at the microsecond plaintext window (§4.2)."""
    if len(k_temp) != 32:
        raise ValueError(f"K_temp must be 32 bytes, got {len(k_temp)}")
    envelope = json.loads(envelope_bytes)
    if envelope.get("mode") != "K_temp":
        raise ValueError(f"wrong mode: {envelope.get('mode')!r}")
    nonce = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    return AESGCM(k_temp).decrypt(nonce, ct, associated_data=None)


# K_temp minting (terminal side, §2.3)

def mint_k_temp() -> bytes:
    """Generate a fresh K_temp. Terminal does this; cloud receives it."""
    return os.urandom(32)


def k_temp_id(k_temp: bytes) -> str:
    """8-char human-visible identifier for a K_temp. SHA256-truncated.
    Used so operators can see which key is currently active without seeing the key itself."""
    import hashlib
    return hashlib.sha256(k_temp).hexdigest()[:8]
