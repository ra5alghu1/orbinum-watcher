#!/usr/bin/env python3

import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

DB = os.getenv("ORBINUM_DB", "/var/lib/orbinum-monitor/uptime.db")
URL = os.getenv("ORBINUM_METRICS_URL", "http://127.0.0.1:19615/metrics")
CHAIN = os.getenv("ORBINUM_CHAIN", "orbinum_testnet")
TIMEOUT = float(os.getenv("ORBINUM_HTTP_TIMEOUT", "5"))

STALL_SECONDS = int(os.getenv("ORBINUM_STALL_SECONDS", "300"))
PROGRESS_MAX_GAP = int(os.getenv("ORBINUM_PROGRESS_MAX_GAP", "180"))

METRIC_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)$")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')


def connect():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    con = sqlite3.connect(DB, timeout=5)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER PRIMARY KEY,
            ok INTEGER NOT NULL,
            status TEXT NOT NULL,
            peers INTEGER,
            best INTEGER,
            finalized INTEGER,
            latency_ms INTEGER,
            error TEXT
        )
        """
    )
    con.commit()
    return con


def parse_metrics(text):
    values = {}

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue

        match = METRIC_LINE.match(raw)
        if not match:
            continue

        name = match.group("name")
        labels = dict(LABEL.findall(match.group("labels") or ""))

        if labels.get("chain") not in (None, CHAIN):
            continue

        try:
            value = float(match.group("value"))
        except ValueError:
            continue

        if name == "substrate_block_height":
            status = labels.get("status")
            if status in ("best", "finalized", "sync_target"):
                values[f"block_{status}"] = int(value)

        elif name == "substrate_sub_libp2p_peers_count":
            values["peers"] = int(value)

    return values


def stalled_blocks(con, ts, best, finalized):
    """Require a continuous run of equal heights, independently per counter.

    Missing metrics, missing peers, gaps and counter changes reset evidence.
    Read the persisted samples so separate timer invocations share history.
    """
    if STALL_SECONDS <= 0 or PROGRESS_MAX_GAP <= 0:
        raise ValueError("progress thresholds must be positive")
    rows = con.execute(
        "SELECT ts, status, peers, best, finalized FROM samples "
        "WHERE ts < ? AND ts >= ? ORDER BY ts DESC",
        (ts, ts - STALL_SECONDS - PROGRESS_MAX_GAP),
    ).fetchall()
    stalled = []
    for name, value, column in (("best", best, 3), ("finalized", finalized, 4)):
        previous = ts
        for row in rows:
            stamp, status, peers, old_best, old_finalized = row
            if (previous - stamp > PROGRESS_MAX_GAP or status == "offline"
                    or peers is None or peers <= 0
                    or old_best is None or old_finalized is None
                    or row[column] != value):
                break
            if ts - stamp >= STALL_SECONDS:
                stalled.append(name)
                break
            previous = stamp
    return stalled


def check():
    ts = int(time.time())
    started = time.monotonic()

    ok = 0
    status = "offline"
    peers = None
    best = None
    finalized = None
    error = None

    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as response:
            text = response.read().decode("utf-8", errors="replace")

        latency = int((time.monotonic() - started) * 1000)
        metrics = parse_metrics(text)

        peers = metrics.get("peers")
        best = metrics.get("block_best")
        finalized = metrics.get("block_finalized")

        if peers is None or best is None or finalized is None:
            status = "degraded"
            error = "required metrics missing"
        elif peers <= 0:
            status = "degraded"
            error = "metrics reachable but peers=0"
        else:
            ok = 1
            status = "online"

    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        error = str(exc)

    con = connect()
    if ok:
        stalled = stalled_blocks(con, ts, best, finalized)
        if stalled:
            ok = 0
            status = "degraded"
            error = "block progression stalled: " + ", ".join(stalled) + f" (at least {STALL_SECONDS}s)"
    con.execute(
        """
        INSERT OR REPLACE INTO samples
        (ts, ok, status, peers, best, finalized, latency_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, ok, status, peers, best, finalized, latency, error),
    )
    con.commit()
    con.close()

    stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"{stamp} status={status} peers={peers} best={best} "
        f"finalized={finalized} latency={latency}ms"
    )


def stats_since(seconds=None):
    con = connect()

    if seconds is None:
        rows = con.execute(
            "SELECT ts, ok, status, peers, best, finalized, latency_ms FROM samples ORDER BY ts"
        ).fetchall()
    else:
        cutoff = int(time.time()) - seconds
        rows = con.execute(
            """
            SELECT ts, ok, status, peers, best, finalized, latency_ms
            FROM samples
            WHERE ts >= ?
            ORDER BY ts
            """,
            (cutoff,),
        ).fetchall()

    con.close()

    if not rows:
        return None

    total = len(rows)
    good = sum(row[1] for row in rows)
    incidents = 0
    longest = 0
    current = 0
    previous = 1

    for row in rows:
        online = row[1]
        if not online:
            current += 1
            longest = max(longest, current)
            if previous:
                incidents += 1
        else:
            current = 0
        previous = online

    latest = rows[-1]

    return {
        "uptime": good / total * 100,
        "samples": total,
        "offline": total - good,
        "incidents": incidents,
        "longest": longest,
        "status": latest[2],
        "peers": latest[3],
        "best": latest[4],
        "finalized": latest[5],
        "latency": latest[6],
    }


def report():
    all_time = stats_since(None)

    print("\nORBINUM VALIDATOR MONITOR")
    print("=" * 42)

    if all_time:
        print(f"Status      : {all_time['status'].upper()}")
        print(f"Peers       : {all_time['peers']}")
        print(f"Best        : #{all_time['best']}")
        print(f"Finalized   : #{all_time['finalized']}")
        print(f"Latency     : {all_time['latency']} ms")

    print()

    periods = [
        ("24 HOURS", 86400),
        ("7 DAYS", 604800),
        ("30 DAYS", 2592000),
        ("ALL TIME", None),
    ]

    for name, seconds in periods:
        data = stats_since(seconds)
        if not data:
            print(f"{name:10} : no data")
            continue

        print(
            f"{name:10} : {data['uptime']:.3f}% uptime | "
            f"{data['offline']} offline samples | "
            f"{data['incidents']} incidents | longest {data['longest']} samples"
        )

    print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        check()
