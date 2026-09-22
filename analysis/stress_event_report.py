#!/usr/bin/env python3
"""Build high-level stress/load events from Orbinum Watcher telemetry.

Read-only with respect to the validator and telemetry database. It scans stored
telemetry, reuses anomaly_detector.py, groups nearby anomaly signals into one
event, and prints a compact summary with peaks and recovery information.

No Docker control, no network traffic generation, no tunnel changes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DETECTOR_PATH = ROOT / "analysis" / "anomaly_detector.py"
DEFAULT_DB = Path(r"C:\ProgramData\OrbinumWatcher\telemetry.db")

spec = importlib.util.spec_from_file_location("anomaly_detector", DETECTOR_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load anomaly detector")
detector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = detector
spec.loader.exec_module(detector)

STRESS_KINDS = {
    "container_cpu_high",
    "host_cpu_high",
    "container_memory_high",
    "finality_gap",
    "sync_lag",
    "peers_low",
    "peer_drop",
    "metrics_slow",
    "block_stall",
    "finality_stall",
    "telemetry_error",
    "docker_engine_unavailable",
    "container_down",
    "container_restart",
}

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class StressEvent:
    started: int
    ended: int
    duration_s: int
    severity: str
    kinds: list[str]
    samples: int
    peak_container_cpu_pct: float | None
    peak_host_cpu_pct: float | None
    peak_container_mem_pct: float | None
    max_finality_gap: int | None
    max_sync_gap: int | None
    min_peers: int | None
    max_metrics_latency_ms: int | None
    container_restarts_delta: int | None
    best_advanced_by: int | None
    finalized_advanced_by: int | None
    net_rx_delta_bytes: int | None
    net_tx_delta_bytes: int | None
    block_read_delta_bytes: int | None
    block_write_delta_bytes: int | None
    recovered: bool
    summary: str


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ival(v: Any) -> int | None:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def _max_num(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [_num(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def _max_int(rows: list[dict[str, Any]], key: str) -> int | None:
    vals = [_ival(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def _min_int(rows: list[dict[str, Any]], key: str) -> int | None:
    vals = [_ival(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def _delta(rows: list[dict[str, Any]], key: str) -> int | None:
    vals = [_ival(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    d = vals[-1] - vals[0]
    return d if d >= 0 else None


def _advance(rows: list[dict[str, Any]], key: str) -> int | None:
    vals = [_ival(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    return vals[-1] - vals[0]


def read_sqlite(path: Path, seconds: int | None) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    latest = con.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0]
    if latest is None:
        con.close()
        return []
    if seconds is None:
        rows = con.execute("SELECT * FROM telemetry ORDER BY ts").fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM telemetry WHERE ts >= ? ORDER BY ts",
            (int(latest) - seconds,),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def cluster_anomalies(events, merge_gap_s: int = 90):
    relevant = [e for e in events if e.kind in STRESS_KINDS]
    relevant.sort(key=lambda e: (e.started, e.ended, e.kind))
    if not relevant:
        return []

    clusters = [[relevant[0]]]
    for e in relevant[1:]:
        last_end = max(x.ended for x in clusters[-1])
        if e.started - last_end <= merge_gap_s:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    return clusters


def _rows_for_window(
    rows: list[dict[str, Any]],
    start: int,
    end: int,
    pad_before_s: int,
    pad_after_s: int,
) -> list[dict[str, Any]]:
    lo = start - pad_before_s
    hi = end + pad_after_s
    return [r for r in rows if lo <= int(r["ts"]) <= hi]


def build_stress_events(
    rows: list[dict[str, Any]],
    merge_gap_s: int = 90,
    pad_before_s: int = 30,
    pad_after_s: int = 60,
) -> list[StressEvent]:
    rows = sorted((dict(r) for r in rows if r.get("ts") is not None), key=lambda r: int(r["ts"]))
    if not rows:
        return []

    anomalies = detector.detect(rows)
    clusters = cluster_anomalies(anomalies, merge_gap_s=merge_gap_s)
    out: list[StressEvent] = []

    latest_ts = int(rows[-1]["ts"])

    for cluster in clusters:
        start = min(e.started for e in cluster)
        end = max(e.ended for e in cluster)
        seg = _rows_for_window(rows, start, end, pad_before_s, pad_after_s)
        if not seg:
            continue

        severity = max(
            (e.severity for e in cluster),
            key=lambda s: SEVERITY_RANK.get(s, 0),
        )
        kinds = sorted({e.kind for e in cluster})

        restarts = [_ival(r.get("container_restarts")) for r in seg]
        restarts = [v for v in restarts if v is not None]
        restart_delta = (restarts[-1] - restarts[0]) if len(restarts) >= 2 else None
        if restart_delta is not None and restart_delta < 0:
            restart_delta = None

        # An anomaly segment that ends at the newest sample is still active.  It is
        # recovered only when telemetry continued after the anomaly ended.
        # Comparing against end + pad_after_s made a stale/finished segment look
        # open for the rest of the reporting window when sample cadence was sparse.
        recovered = latest_ts > end

        summary = ", ".join(kinds)
        out.append(
            StressEvent(
                started=start,
                ended=end,
                duration_s=max(0, end - start),
                severity=severity,
                kinds=kinds,
                samples=len(seg),
                peak_container_cpu_pct=_max_num(seg, "container_cpu_pct"),
                peak_host_cpu_pct=_max_num(seg, "host_cpu_pct"),
                peak_container_mem_pct=_max_num(seg, "container_mem_pct"),
                max_finality_gap=_max_int(seg, "finality_gap"),
                max_sync_gap=_max_int(seg, "sync_gap"),
                min_peers=_min_int(seg, "peers"),
                max_metrics_latency_ms=_max_int(seg, "metrics_latency_ms"),
                container_restarts_delta=restart_delta,
                best_advanced_by=_advance(seg, "best"),
                finalized_advanced_by=_advance(seg, "finalized"),
                net_rx_delta_bytes=_delta(seg, "container_net_rx_bytes"),
                net_tx_delta_bytes=_delta(seg, "container_net_tx_bytes"),
                block_read_delta_bytes=_delta(seg, "container_block_read_bytes"),
                block_write_delta_bytes=_delta(seg, "container_block_write_bytes"),
                recovered=recovered,
                summary=summary,
            )
        )

    return out


def fmt_bytes(v: int | None) -> str:
    if v is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(v)
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000.0
        i += 1
    return f"{n:.1f} {units[i]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--jsonl", type=Path)
    ap.add_argument("--seconds", type=int, default=86400, help="look-back window; use 0 for all stored data")
    ap.add_argument("--merge-gap", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    seconds = None if args.seconds == 0 else args.seconds
    rows = read_jsonl(args.jsonl) if args.jsonl else read_sqlite(args.db, seconds)
    events = build_stress_events(rows, merge_gap_s=args.merge_gap)

    if args.json:
        print(json.dumps([asdict(e) for e in events], ensure_ascii=False, indent=2))
        return 0

    print(f"samples: {len(rows)}")
    print(f"stress events: {len(events)}")
    for i, e in enumerate(events, 1):
        state = "RECOVERED" if e.recovered else "OPEN"
        print()
        print(f"LOAD EVENT #{i} [{e.severity.upper()}] {state}")
        print(f"duration: {e.duration_s}s")
        print(f"signals: {', '.join(e.kinds)}")
        print(f"CPU validator/host: {e.peak_container_cpu_pct or 0:.1f}% / {e.peak_host_cpu_pct or 0:.1f}%")
        print(f"RAM validator peak: {e.peak_container_mem_pct or 0:.1f}%")
        print(f"finality gap max: {e.max_finality_gap if e.max_finality_gap is not None else '—'}")
        print(f"sync gap max: {e.max_sync_gap if e.max_sync_gap is not None else '—'}")
        print(f"peers min: {e.min_peers if e.min_peers is not None else '—'}")
        print(f"metrics latency max: {e.max_metrics_latency_ms if e.max_metrics_latency_ms is not None else '—'} ms")
        print(f"container restarts: {e.container_restarts_delta if e.container_restarts_delta is not None else '—'}")
        print(f"blocks advanced best/finalized: {e.best_advanced_by} / {e.finalized_advanced_by}")
        print(f"net RX/TX: {fmt_bytes(e.net_rx_delta_bytes)} / {fmt_bytes(e.net_tx_delta_bytes)}")
        print(f"disk read/write: {fmt_bytes(e.block_read_delta_bytes)} / {fmt_bytes(e.block_write_delta_bytes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
