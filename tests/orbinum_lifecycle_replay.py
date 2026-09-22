#!/usr/bin/env python3
"""Regression checks for Orbinum validator lifecycle detection."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "agent" / "orbinum_lifecycle.py"
spec = importlib.util.spec_from_file_location("orbinum_lifecycle", path)
m = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(m)

# Known Substrate storage prefix vectors.
assert m.twox128("System").hex() == "26aa394eea5630e07c48ae0c9558cef7"
assert m.twox128("Account").hex() == "b99d880ec681799c0cf30e8886371da9"

address = "5FCGETv4VTwWjoQtbTqNhckuU7q54K6RX8XoXz83W613SgWh"
account = m.ss58_account_id(address)
assert len(account) == 32

def scale_vec(items):
    assert len(items) < 64
    return "0x" + bytes([len(items) << 2]).hex() + b"".join(items).hex()

def fake_rpc(method, params=None):
    assert method == "state_getStorage"
    key = params[0]
    if key == m.storage_key("ValidatorSet", "ApprovedValidators"):
        return scale_vec([account])
    if key == m.storage_key("Session", "Validators"):
        return scale_vec([])
    raise AssertionError(key)

snap = m.lifecycle_snapshot(fake_rpc, address)
assert snap["approved"] is True
assert snap["active"] is False
assert snap["state"] == "approved"

assert m.transition(None, "candidate") is None
assert m.transition("candidate", "approved") == "approved"
assert m.transition("approved", "active") == "activated"
assert m.transition("active", "active") is None
assert m.transition("active", "approved") == "deactivated"

print("Orbinum lifecycle replay passed")
