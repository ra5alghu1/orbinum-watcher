#!/usr/bin/env python3
"""Synthetic tests for stress_event_report.py.

Pure in-memory telemetry. No Docker, validator, VPS or network access.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "analysis" / "stress_event_report.py"

spec = importlib.util.spec_from_file_location("stress_event_report", REPORT)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load stress event report")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def row(ts: int, **changes):
    best = 700000 + ts // 6
    base = {
        "ts": ts,
        "error": None,
        "best": best,
        "finalized": best - 2,
        "sync_target": best,
        "peers": 26,
        "metrics_latency_ms": 25,
        "container_cpu_pct": 7.0,
        "container_mem_pct": 9.5,
        "container_status": "running",
        "container_running": 1,
        "container_restarts": 0,
        "host_cpu_pct": 15.0,
        "finality_gap": 2,
        "sync_gap": 0,
        "container_net_rx_bytes": 2_000_000_000 + ts * 100_000,
        "container_net_tx_bytes": 1_500_000_000 + ts * 80_000,
        "container_block_read_bytes": 8_000_000_000 + ts * 50_000,
        "container_block_write_bytes": 2_500_000_000 + ts * 40_000,
    }
    base.update(changes)
    return base


def normal():
    return [row(ts) for ts in range(0, 181, 5)]


def combined_load():
    rows = []
    for ts in range(0, 241, 5):
        best = 700000 + ts // 6
        changes = {}
        if 40 <= ts <= 80:
            changes["container_cpu_pct"] = 96.0
            changes["host_cpu_pct"] = 93.0
        if 50 <= ts <= 85:
            changes["finality_gap"] = 9
            changes["finalized"] = best - 9
        if 55 <= ts <= 90:
            changes["peers"] = 8
        rows.append(row(ts, **changes))
    return rows


def docker_recovery():
    error = (
        "metrics: unavailable | docker stats: cannot connect to the docker daemon | "
        "docker inspect: cannot connect to the docker daemon"
    )
    rows = [row(ts, error=error) for ts in range(0, 16, 5)]
    rows.extend(row(ts) for ts in range(20, 41, 5))
    return rows


def docker_still_down():
    error = (
        "metrics: unavailable | docker stats: cannot connect to the docker daemon | "
        "docker inspect: cannot connect to the docker daemon"
    )
    return [row(ts, error=error) for ts in range(0, 21, 5)]


def main():
    assert mod.build_stress_events(normal()) == [], "normal telemetry created a stress event"
    events = mod.build_stress_events(combined_load())
    assert len(events) == 1, f"expected 1 grouped event, got {len(events)}"
    e = events[0]
    needed = {"container_cpu_high", "host_cpu_high", "finality_gap", "peers_low", "peer_drop"}
    missing = needed - set(e.kinds)
    assert not missing, f"missing signals: {sorted(missing)}"
    assert e.peak_container_cpu_pct == 96.0
    assert e.max_finality_gap == 9
    assert e.min_peers == 8
    assert e.recovered is True
    recovered = mod.build_stress_events(docker_recovery())
    docker = next(e for e in recovered if "docker_engine_unavailable" in e.kinds)
    assert docker.recovered is True, "Docker incident stayed open after healthy telemetry resumed"

    active = mod.build_stress_events(docker_still_down())
    docker = next(e for e in active if "docker_engine_unavailable" in e.kinds)
    assert docker.recovered is False, "active Docker incident was closed prematurely"

    print("PASS Docker failure -> healthy sample closes incident")
    print("PASS ongoing Docker failure -> incident remains open")
    print("PASS normal -> no load events")
    print("PASS combined load -> one grouped event")
    print("  signals:", ", ".join(e.kinds))
    print("  peak CPU:", e.peak_container_cpu_pct)
    print("  max finality gap:", e.max_finality_gap)
    print("  min peers:", e.min_peers)
    print("  recovered:", e.recovered)
    print("\nALL STRESS EVENT REPORT TESTS PASSED")


if __name__ == "__main__":
    main()
