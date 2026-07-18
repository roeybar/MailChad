"""Tests for K_temp lifecycle on cloud - spec §2.3, §10.2."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture
def keys(tmp_cloud_db):
    import sys
    sys.path.insert(0, str(ROOT / "cloud"))
    # Force re-import to pick up the env var change
    if "mailchad.cloud.keys" in sys.modules:
        del sys.modules["mailchad.cloud.keys"]
    from mailchad.cloud import keys as k
    yield k


def test_initial_state_no_key(keys):
    assert keys.get_active_k_temp() is None
    assert keys.k_temp_status() == {"present": False}


def test_set_and_get(keys):
    import os
    k = os.urandom(32)
    meta = keys.set_k_temp(k, 86400, "operator")
    assert keys.get_active_k_temp() == k
    assert meta["key_id"] == keys._key_id(k)
    assert meta["set_by"] == "operator"


def test_status_reports_metadata_not_key(keys):
    import os
    k = os.urandom(32)
    keys.set_k_temp(k, 3600, "client")
    s = keys.k_temp_status()
    assert s["present"] is True
    assert "k_temp" not in s   # never leak the key
    assert s["ttl_seconds"] == 3600
    assert s["set_by"] == "client"


def test_wipe_removes_key(keys):
    import os
    keys.set_k_temp(os.urandom(32), 3600, "operator")
    assert keys.get_active_k_temp() is not None
    keys.wipe_k_temp()
    assert keys.get_active_k_temp() is None


def test_invalid_ttl_rejected(keys):
    import os
    with pytest.raises(keys.TTLViolation):
        keys.set_k_temp(os.urandom(32), 12345, "operator")


def test_invalid_actor_rejected(keys):
    import os
    with pytest.raises(ValueError):
        keys.set_k_temp(os.urandom(32), 3600, "nope")


def test_wrong_size_key_rejected(keys):
    with pytest.raises(ValueError):
        keys.set_k_temp(b"short", 3600, "operator")
