"""Read-only collector freshness check, independent of validator health."""

import sqlite3
import time


def collector_health(db_path, now=None, stale_after=180):
    """Return (HTTP status, payload). A stale collector is not a node outage."""
    if stale_after <= 0:
        raise ValueError("stale_after must be positive")
    now = int(time.time()) if now is None else int(now)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT MAX(ts) FROM samples").fetchone()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        return 503, {"status": "MONITOR_UNAVAILABLE", "sample_age_seconds": None,
                     "stale_after_seconds": stale_after, "error": type(exc).__name__}

    ts = row[0] if row else None
    if ts is None:
        return 503, {"status": "MONITOR_NO_DATA", "sample_age_seconds": None,
                     "stale_after_seconds": stale_after}
    age = now - int(ts)
    if age < 0:
        return 503, {"status": "MONITOR_CLOCK_SKEW", "sample_age_seconds": age,
                     "stale_after_seconds": stale_after}
    if age > stale_after:
        return 503, {"status": "MONITOR_STALE", "sample_age_seconds": age,
                     "stale_after_seconds": stale_after}
    return 200, {"status": "MONITOR_OK", "sample_age_seconds": age,
                 "stale_after_seconds": stale_after}
