"""Shared pytest fixtures.

Tests run inside email-platform-v3-terminal container (has BOTH pynacl +
bcrypt + cryptography + pytest, since it imports everything).

Each test file manages its own sys.path to avoid cloud vs terminal
`app.*` collisions. conftest.py only provides DB fixtures.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# AWS env needed by moto / boto3 even in mock mode
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("DYNAMODB_TABLE", "ep-v3-test")

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def tmp_cloud_db(monkeypatch):
    """Fresh per-test cloud DB + keys dir."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        path = tmp.name
    keys_dir = tempfile.mkdtemp(prefix="cloud-keys-")
    monkeypatch.setenv("CLOUD_DB_PATH", path)
    monkeypatch.setenv("CLOUD_KEYS_DIR", keys_dir)
    # Purge any previously-loaded cloud modules so they re-read env on import
    for mod in list(sys.modules):
        if mod == "mailchad" or mod.startswith("mailchad."):
            del sys.modules[mod]
    # Ensure cloud is FIRST on path for the test that follows
    cloud_path = str(PROJECT_ROOT / "cloud")
    if cloud_path in sys.path:
        sys.path.remove(cloud_path)
    sys.path.insert(0, cloud_path)
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@pytest.fixture
def tmp_cloud_dynamo(monkeypatch):
    """Fresh per-test moto DynamoDB + cloud on sys.path + fresh KEK dir."""
    from moto import mock_aws
    keys_dir = tempfile.mkdtemp(prefix="cloud-keys-")
    monkeypatch.setenv("CLOUD_KEYS_DIR", keys_dir)
    for mod in list(sys.modules):
        if mod == "mailchad" or mod.startswith("mailchad."):
            del sys.modules[mod]
    cloud_path = str(PROJECT_ROOT / "cloud")
    if cloud_path in sys.path:
        sys.path.remove(cloud_path)
    sys.path.insert(0, cloud_path)
    with mock_aws():
        from mailchad.cloud import dynamo
        dynamo._reset_clients()
        dynamo.init()
        yield
        dynamo._reset_clients()
    for mod in list(sys.modules):
        if mod == "mailchad" or mod.startswith("mailchad."):
            del sys.modules[mod]


@pytest.fixture
def tmp_terminal_db(monkeypatch):
    """Fresh per-test terminal DB."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        path = tmp.name
    monkeypatch.setenv("VAULT_DB_PATH", path)
    monkeypatch.setenv("TERMINAL_KEYS_DIR", tempfile.mkdtemp(prefix="term-keys-"))
    for mod in list(sys.modules):
        if mod == "mailchad" or mod.startswith("mailchad."):
            del sys.modules[mod]
    term_path = str(PROJECT_ROOT / "terminal")
    if term_path in sys.path:
        sys.path.remove(term_path)
    sys.path.insert(0, term_path)
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
