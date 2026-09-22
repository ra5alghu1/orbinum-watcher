#!/usr/bin/env python3
"""Read-only Orbinum validator lifecycle checks.

Queries chain storage directly over JSON-RPC. No extrinsics are submitted and
no validator/node state is modified.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

MASK64 = (1 << 64) - 1
P1, P2, P3 = 11400714785074694791, 14029467366897019727, 1609587929392839161
P4, P5 = 9650029242287828579, 2870177450012600261
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _rol(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & MASK64


def _round(acc: int, value: int) -> int:
    acc = (acc + value * P2) & MASK64
    acc = _rol(acc, 31)
    return (acc * P1) & MASK64


def xxh64(data: bytes, seed: int = 0) -> int:
    """Small dependency-free xxHash64 implementation used by Substrate Twox."""
    n, pos = len(data), 0
    if n >= 32:
        v1 = (seed + P1 + P2) & MASK64
        v2 = (seed + P2) & MASK64
        v3 = seed & MASK64
        v4 = (seed - P1) & MASK64
        while pos <= n - 32:
            vals = [int.from_bytes(data[pos + i:pos + i + 8], "little") for i in (0, 8, 16, 24)]
            pos += 32
            v1, v2, v3, v4 = (_round(v1, vals[0]), _round(v2, vals[1]), _round(v3, vals[2]), _round(v4, vals[3]))
        h = (_rol(v1, 1) + _rol(v2, 7) + _rol(v3, 12) + _rol(v4, 18)) & MASK64
        for value in (v1, v2, v3, v4):
            h ^= _round(0, value)
            h = (h * P1 + P4) & MASK64
    else:
        h = (seed + P5) & MASK64
    h = (h + n) & MASK64
    while pos <= n - 8:
        k1 = _round(0, int.from_bytes(data[pos:pos + 8], "little"))
        h ^= k1
        h = (_rol(h, 27) * P1 + P4) & MASK64
        pos += 8
    if pos <= n - 4:
        h ^= (int.from_bytes(data[pos:pos + 4], "little") * P1) & MASK64
        h = (_rol(h, 23) * P2 + P3) & MASK64
        pos += 4
    while pos < n:
        h ^= (data[pos] * P5) & MASK64
        h = (_rol(h, 11) * P1) & MASK64
        pos += 1
    h ^= h >> 33
    h = (h * P2) & MASK64
    h ^= h >> 29
    h = (h * P3) & MASK64
    h ^= h >> 32
    return h & MASK64


def twox128(text: str) -> bytes:
    raw = text.encode()
    return xxh64(raw, 0).to_bytes(8, "little") + xxh64(raw, 1).to_bytes(8, "little")


def storage_key(pallet: str, item: str) -> str:
    return "0x" + (twox128(pallet) + twox128(item)).hex()


def ss58_account_id(address: str) -> bytes:
    value = 0
    for char in address:
        value = value * 58 + B58.index(char)
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    pad = len(address) - len(address.lstrip("1"))
    raw = b"\x00" * pad + raw
    if len(raw) != 35:
        raise ValueError("expected one-byte SS58 prefix + AccountId32 + checksum")
    body, checksum = raw[:-2], raw[-2:]
    expected = hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()[:2]
    if checksum != expected:
        raise ValueError("invalid SS58 checksum")
    return body[1:33]


def _compact(data: bytes, pos: int = 0) -> tuple[int, int]:
    first = data[pos]
    mode = first & 3
    if mode == 0:
        return first >> 2, pos + 1
    if mode == 1:
        return int.from_bytes(data[pos:pos + 2], "little") >> 2, pos + 2
    if mode == 2:
        return int.from_bytes(data[pos:pos + 4], "little") >> 2, pos + 4
    size = (first >> 2) + 4
    return int.from_bytes(data[pos + 1:pos + 1 + size], "little"), pos + 1 + size


def decode_accounts(value: str | None) -> list[bytes]:
    if not value:
        return []
    data = bytes.fromhex(value.removeprefix("0x"))
    count, pos = _compact(data)
    end = pos + count * 32
    if end > len(data):
        raise ValueError("truncated AccountId vector")
    return [data[pos + i * 32:pos + (i + 1) * 32] for i in range(count)]


def lifecycle_snapshot(rpc: Callable[[str, list[Any] | None], Any], address: str) -> dict[str, Any]:
    """Return approval/active membership for one validator account."""
    account = ss58_account_id(address)
    approved_raw = rpc("state_getStorage", [storage_key("ValidatorSet", "ApprovedValidators")])
    active_raw = rpc("state_getStorage", [storage_key("Session", "Validators")])
    approved = account in decode_accounts(approved_raw)
    active = account in decode_accounts(active_raw)
    return {
        "account": address,
        "approved": approved,
        "active": active,
        "state": "active" if active else ("approved" if approved else "candidate"),
    }


def transition(previous: str | None, current: str) -> str | None:
    if previous is None or previous == current:
        return None
    if current == "active":
        return "activated"
    if previous == "active":
        return "deactivated"
    if current == "approved":
        return "approved"
    return None
