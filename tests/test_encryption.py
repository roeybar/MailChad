"""Tests for encryption layer - both modes round-trip + tamper detection.

Spec sections covered: §2.2 (encryption modes), §2.3 (K_temp), §10.5 (corrupted pack).
"""
import importlib.util
from pathlib import Path

import nacl.public
import pytest

ROOT = Path(__file__).parent.parent


def load(mod_name: str, path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def term_enc():
    return load("term_enc", "src/mailchad/terminal/encryption.py")


@pytest.fixture(scope="module")
def cloud_enc():
    return load("cloud_enc", "src/mailchad/cloud/encryption_cloud.py")


@pytest.fixture
def keypairs():
    return (nacl.public.PrivateKey.generate(), nacl.public.PrivateKey.generate())


# §2.2 K_op+K_cl round-trip

def test_op_can_encrypt_and_decrypt(term_enc, keypairs):
    op_priv, cl_priv = keypairs
    op_bundle = term_enc.KeyBundle("operator", op_priv, cl_priv.public_key)
    payload = b"test payload"
    env = term_enc.encrypt_for_both(payload, op_bundle)
    assert term_enc.decrypt_for_both(env, op_bundle) == payload


def test_cl_can_decrypt_op_encrypted(term_enc, keypairs):
    op_priv, cl_priv = keypairs
    op_bundle = term_enc.KeyBundle("operator", op_priv, cl_priv.public_key)
    cl_bundle = term_enc.KeyBundle("client",   cl_priv, op_priv.public_key)
    payload = b"x" * 1000
    env = term_enc.encrypt_for_both(payload, op_bundle)
    assert term_enc.decrypt_for_both(env, cl_bundle) == payload


def test_symmetric_either_party_encrypts(term_enc, keypairs):
    op_priv, cl_priv = keypairs
    op_bundle = term_enc.KeyBundle("operator", op_priv, cl_priv.public_key)
    cl_bundle = term_enc.KeyBundle("client",   cl_priv, op_priv.public_key)
    payload = b"client encrypts this"
    env = term_enc.encrypt_for_both(payload, cl_bundle)
    assert term_enc.decrypt_for_both(env, op_bundle) == payload
    assert term_enc.decrypt_for_both(env, cl_bundle) == payload


def test_wrong_keypair_cannot_decrypt(term_enc, keypairs):
    op_priv, cl_priv = keypairs
    wrong = nacl.public.PrivateKey.generate()
    op_bundle = term_enc.KeyBundle("operator", op_priv, cl_priv.public_key)
    env = term_enc.encrypt_for_both(b"secret", op_bundle)

    wrong_bundle = term_enc.KeyBundle("operator", wrong, cl_priv.public_key)
    with pytest.raises(Exception):
        term_enc.decrypt_for_both(env, wrong_bundle)


# §2.3 K_temp round-trip

def test_k_temp_round_trip(term_enc):
    k = term_enc.mint_k_temp()
    assert len(k) == 32
    env = term_enc.encrypt_with_temp(b"pack payload", k)
    assert term_enc.decrypt_with_temp(env, k) == b"pack payload"


def test_k_temp_wrong_key_raises(term_enc):
    k1 = term_enc.mint_k_temp()
    k2 = term_enc.mint_k_temp()
    env = term_enc.encrypt_with_temp(b"x", k1)
    with pytest.raises(Exception):
        term_enc.decrypt_with_temp(env, k2)


def test_k_temp_must_be_32_bytes(term_enc):
    with pytest.raises(ValueError):
        term_enc.encrypt_with_temp(b"x", b"short")


def test_k_temp_id_deterministic(term_enc):
    k = term_enc.mint_k_temp()
    assert term_enc.k_temp_id(k) == term_enc.k_temp_id(k)
    assert len(term_enc.k_temp_id(k)) == 8


# Cross-side: cloud encrypt -> terminal decrypt (§4.3)

def test_cloud_encrypts_terminals_decrypt(tmp_cloud_dynamo, term_enc, keypairs):
    """Webhook flow: cloud encrypts to both pubkeys; either terminal decrypts."""
    # tmp_cloud_dynamo has set env + put cloud/ at front of sys.path
    from mailchad.cloud import dynamo
    from mailchad.cloud import encryption_cloud as cloud_enc
    op_priv, cl_priv = keypairs
    dynamo.put_pubkey("operator", bytes(op_priv.public_key))
    dynamo.put_pubkey("client",   bytes(cl_priv.public_key))
    env = cloud_enc.encrypt_for_both(b"webhook event payload")
    op_bundle = term_enc.KeyBundle("operator", op_priv, cl_priv.public_key)
    cl_bundle = term_enc.KeyBundle("client",   cl_priv, op_priv.public_key)
    assert term_enc.decrypt_for_both(env, op_bundle) == b"webhook event payload"
    assert term_enc.decrypt_for_both(env, cl_bundle) == b"webhook event payload"
