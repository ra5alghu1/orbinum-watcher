#!/usr/bin/env python3

import html
import json
import mimetypes
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DB = os.getenv("ORBINUM_DB", "/var/lib/orbinum-monitor/uptime.db")
HOST = os.getenv("ORBINUM_WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("ORBINUM_WEB_PORT", "8787"))

VALIDATOR_NAME = os.getenv("ORBINUM_VALIDATOR_NAME", "robotek8-orbinum")
PUBLIC_URL = os.getenv("ORBINUM_PUBLIC_URL", "https://orbinum-watcher.xyz").rstrip("/")
STATIC_DIR = Path(os.getenv("ORBINUM_STATIC_DIR", "/app/static"))

TELEGRAM_URL = os.getenv("ORBINUM_TELEGRAM_URL", "https://t.me/Ras_a1_Ghu1")
GITHUB_URL = os.getenv("ORBINUM_GITHUB_URL", "https://github.com/ra5alghu1/orbinum-watcher")

KZ = timezone(timedelta(hours=5))
POLL_SECONDS = 15
COLLECTOR_SECONDS = 60
STALE_AFTER_SECONDS = 180


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)


def normalize_status(value):
    value = str(value or "").strip().lower()
    if value in {"degraded", "warn", "warning"}:
        return "degraded"
    if value in {"offline", "down", "failed", "error"}:
        return "offline"
    if value in {"online", "ok", "healthy", "up"}:
        return "online"
    return value


def fmt_time(ts, with_seconds=False):
    if not ts:
        return "—"
    fmt = "%d.%m.%Y %H:%M:%S UTC+5" if with_seconds else "%d.%m.%Y %H:%M UTC+5"
    return datetime.fromtimestamp(ts, KZ).strftime(fmt)


def fmt_clock(ts):
    return datetime.fromtimestamp(ts, KZ).strftime("%H:%M")


def human_duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def latest():
    con = connect()
    row = con.execute(
        """
        SELECT ts, ok, status, peers, best, finalized, latency_ms, error
        FROM samples
        ORDER BY ts DESC
        LIMIT 1
        """
    ).fetchone()
    con.close()
    return row


def first_sample():
    con = connect()
    row = con.execute("SELECT MIN(ts) FROM samples").fetchone()
    con.close()
    return row[0] if row and row[0] else None


def stats(seconds=None):
    con = connect()
    if seconds:
        cutoff = int(time.time()) - seconds
        rows = con.execute(
            """
            SELECT ts, ok
            FROM samples
            WHERE ts >= ?
            ORDER BY ts
            """,
            (cutoff,),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT ts, ok
            FROM samples
            ORDER BY ts
            """
        ).fetchall()
    con.close()

    if not rows:
        return {
            "uptime": 0.0,
            "samples": 0,
            "offline": 0,
            "incidents": 0,
            "observed": 0,
        }

    total = len(rows)
    online = sum(1 for _, ok in rows if ok)
    offline = total - online

    incident_count = 0
    previous_ok = True
    for _, ok in rows:
        current_ok = bool(ok)
        if not current_ok and previous_ok:
            incident_count += 1
        previous_ok = current_ok

    return {
        "uptime": online / total * 100.0,
        "samples": total,
        "offline": offline,
        "incidents": incident_count,
        "observed": max(0, rows[-1][0] - rows[0][0]),
    }


def build_incidents(rows, now, stale=STALE_AFTER_SECONDS):
    """Group sampled symptoms; missing observations never prove node downtime.

    Rows are ordered (ts, ok, status, peers, best, finalized). Durations are
    observation-based estimates, not exact failure/recovery timestamps.
    """
    out=[]; current=None; previous=None

    def emit(kind, start, end, ending):
        active=ending=='ongoing'
        labels={'metrics_unavailable':'Metrics unavailable',
                'no_peers':'No peers', 'metrics_incomplete':'Metrics incomplete',
                'degraded':'Degraded', 'observation_gap':'No observations'}
        out.append({'kind':kind, 'started':fmt_time(start),
                    'recovered':fmt_time(end) if ending=='recovered' else '—',
                    'duration':human_duration(end-start), 'duration_seconds':max(0,end-start),
                    'started_ts':start, 'ended_ts':None if active else end,
                    'status':labels[kind]+' · '+ending.replace('_',' '),
                    'active':active, 'ending':ending})

    for ts,ok,status,peers,best,finalized in rows:
        if previous is not None and ts-previous>stale:
            if current:
                emit(*current,previous,'unknown'); current=None
            emit('observation_gap',previous,ts,'observations_resumed')
        if ok:
            kind=None
        elif status=='offline':
            kind='metrics_unavailable'
        elif peers is None or best is None or finalized is None:
            kind='metrics_incomplete'
        elif peers<=0:
            kind='no_peers'
        else:
            kind='degraded'
        if current and current[0]!=kind:
            emit(*current,ts,'recovered' if kind is None else 'symptom_changed')
            current=None
        if kind and current is None:current=(kind,ts)
        previous=ts
    if previous is not None and now-previous>stale:
        if current:emit(*current,previous,'unknown'); current=None
        emit('observation_gap',previous,now,'ongoing')
    elif current:
        emit(*current,now,'ongoing')
    return out


def incident_rows():
    now=int(time.time()); cutoff=now-2592000
    c=connect()
    try:
        rows=c.execute('SELECT ts,ok,status,peers,best,finalized FROM samples WHERE ts>=? ORDER BY ts',(cutoff,)).fetchall()
        # Include one boundary sample so a collector stopped over 30 days ago
        # still appears as missing observations rather than a healthy empty list.
        before=c.execute('SELECT ts,ok,status,peers,best,finalized FROM samples WHERE ts<? ORDER BY ts DESC LIMIT 1',(cutoff,)).fetchone()
    finally:c.close()
    if before:rows.insert(0,(cutoff,*before[1:]))
    return build_incidents(rows,now)[-10:][::-1]


def stress_incidents(limit=50):
    path = Path(os.getenv("ORBINUM_EVENTS_FILE", "/data/events.json"))

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []

    now = int(time.time())
    result = []

    def pct(value):
        try:
            return f"{float(value):.1f}%"
        except (TypeError, ValueError):
            return None

    def integer(value):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return None

    def fmt_bytes(value):
        try:
            n = float(value)
        except (TypeError, ValueError):
            return None

        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while abs(n) >= 1000 and idx < len(units) - 1:
            n /= 1000.0
            idx += 1

        return f"{n:.1f} {units[idx]}"

    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue

        try:
            started = int(event.get("started"))
        except (TypeError, ValueError):
            continue

        event_type = str(event.get("type") or "Load event")

        # External uptime monitoring remains the canonical source for outages.
        # This avoids showing the same outage twice.
        if event_type == "Node offline":
            continue

        recovered = bool(event.get("recovered"))

        try:
            ended = int(event.get("ended")) if event.get("ended") is not None else None
        except (TypeError, ValueError):
            ended = None

        try:
            duration_s = max(0, int(event.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration_s = 0

        if recovered and ended is not None:
            duration_s = max(duration_s, ended - started)
        elif not recovered:
            duration_s = max(duration_s, now - started)

        details = []

        def add(label, value):
            if value is not None and value != "":
                details.append({
                    "label": label,
                    "value": str(value),
                })

        kinds = event.get("kinds") or []
        if isinstance(kinds, list):
            add("Signals", ", ".join(str(x) for x in kinds))

        add("Validator CPU peak", pct(event.get("peak_container_cpu_pct")))
        add("Host CPU peak", pct(event.get("peak_host_cpu_pct")))
        add("Validator RAM peak", pct(event.get("peak_container_mem_pct")))
        add("Finality gap max", integer(event.get("max_finality_gap")))
        add("Sync gap max", integer(event.get("max_sync_gap")))
        add("Peers min", integer(event.get("min_peers")))

        latency = integer(event.get("max_metrics_latency_ms"))
        add("Metrics latency max", f"{latency} ms" if latency is not None else None)

        add("Container restarts", integer(event.get("container_restarts_delta")))

        best_adv = integer(event.get("best_advanced_by"))
        fin_adv = integer(event.get("finalized_advanced_by"))
        if best_adv is not None or fin_adv is not None:
            add("Best / finalized advanced", f"{best_adv or '—'} / {fin_adv or '—'}")

        rx = fmt_bytes(event.get("net_rx_delta_bytes"))
        tx = fmt_bytes(event.get("net_tx_delta_bytes"))
        if rx is not None or tx is not None:
            add("Network RX / TX", f"{rx or '—'} / {tx or '—'}")

        rd = fmt_bytes(event.get("block_read_delta_bytes"))
        wr = fmt_bytes(event.get("block_write_delta_bytes"))
        if rd is not None or wr is not None:
            add("Disk read / write", f"{rd or '—'} / {wr or '—'}")

        result.append({
            "sort_ts": started,
            "kind": "stress",
            "type": event_type,
            "severity": str(event.get("severity") or "warning").lower(),
            "started": fmt_time(started),
            "recovered": (
                fmt_time(ended)
                if recovered and ended is not None
                else ("Now" if not recovered else "—")
            ),
            "duration": human_duration(duration_s),
            "status": "Recovered" if recovered else "Open",
            "active": not recovered,
            "details": details,
        })

    result.sort(key=lambda item: item["sort_ts"], reverse=True)
    return result[:limit]


def timeline_24h():
    """96 rolling 15-minute buckets. Existing API timeline stays a list of states;
    timeline_detail adds labels for the richer UI without breaking old consumers.
    """
    now = int(time.time())
    slot_seconds = 900
    slots = 96
    start = now - slots * slot_seconds

    con = connect()
    rows = con.execute(
        """
        SELECT ts, ok, status, latency_ms, error
        FROM samples
        WHERE ts >= ?
        ORDER BY ts
        """,
        (start,),
    ).fetchall()
    con.close()

    buckets = [[] for _ in range(slots)]
    for row in rows:
        ts = row[0]
        idx = int((ts - start) / slot_seconds)
        if 0 <= idx < slots:
            buckets[idx].append(row)

    states = []
    detail = []

    for idx, bucket in enumerate(buckets):
        bucket_start = start + idx * slot_seconds
        bucket_end = bucket_start + slot_seconds
        label_prefix = f"{fmt_clock(bucket_start)}–{fmt_clock(bucket_end)}"

        if not bucket:
            state = "none"
            label = f"{label_prefix} — no data"
        else:
            has_down = any(not bool(row[1]) for row in bucket)
            has_degraded = any(normalize_status(row[2]) == "degraded" for row in bucket)

            if has_down:
                state = "down"
                errors = [str(row[4]).strip() for row in bucket if row[4]]
                reason = errors[-1] if errors else "unreachable"
                label = f"{label_prefix} — {reason}"
            elif has_degraded:
                state = "degraded"
                latencies = [row[3] for row in bucket if row[3] is not None]
                reason = f"latency {max(latencies)} ms" if latencies else "degraded"
                label = f"{label_prefix} — {reason}"
            else:
                state = "up"
                label = f"{label_prefix} — operational"

        states.append(state)
        detail.append({"state": state, "label": label})

    return states, detail


def snapshot():
    now = int(time.time())
    row = latest()
    first = first_sample()
    monitor_age = max(0, now - first) if first else 0

    if row:
        ts, ok, raw_status, peers, best, finalized, latency, error = row
        sample_age = max(0, now - ts)
        normalized = normalize_status(raw_status)

        if normalized == "degraded":
            state = "degraded"
        elif ok:
            state = "online"
        else:
            state = "offline"

        if sample_age > STALE_AFTER_SECONDS:
            ui_state = "stale"
        elif state == "offline":
            ui_state = "down"
        else:
            ui_state = state
    else:
        ts = None
        peers = best = finalized = latency = None
        error = "No monitoring data"
        sample_age = None
        state = "offline"
        ui_state = "stale"

    def window(seconds, label):
        s = stats(seconds)
        progress = min(100.0, (monitor_age / seconds * 100.0) if seconds else 100.0)
        return {
            "label": label,
            "ready": monitor_age >= seconds,
            "uptime": s["uptime"],
            "samples": s["samples"],
            "offline": s["offline"],
            "incidents": s["incidents"],
            "progress": progress,
        }

    incident_payload = []

    labels = {'metrics_unavailable': 'Metrics unavailable', 'no_peers': 'No peers',
              'metrics_incomplete': 'Metrics incomplete', 'degraded': 'Degraded',
              'observation_gap': 'No observations'}
    for event in incident_rows():
        event.update({'sort_ts': event['started_ts'],
                      'type': labels[event['kind']], 'severity': 'warning',
                      'status': event['ending'].replace('_', ' ').capitalize(),
                      'details': []})
        incident_payload.append(event)

    incident_payload.extend(stress_incidents())

    incident_payload.sort(
        key=lambda item: int(item.get("sort_ts") or 0),
        reverse=True,
    )

    incident_payload = incident_payload[:20]

    for item in incident_payload:
        item.pop("sort_ts", None)

    all_time = stats()
    timeline, timeline_detail = timeline_24h()

    return {
        "validator": VALIDATOR_NAME,
        "state": state,
        "ui_state": ui_state,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "peers": peers,
        "best": best,
        "finalized": finalized,
        "latency_ms": latency,
        "error": error,
        "sample_age": sample_age,
        "last_sample": fmt_time(ts),
        "monitoring_since": fmt_time(first),
        "monitor_age": monitor_age,
        "poll_seconds": POLL_SECONDS,
        "collector_seconds": COLLECTOR_SECONDS,
        "windows": {
            "24h": window(86400, "24 hours"),
            "7d": window(604800, "7 days"),
            "30d": window(2592000, "30 days"),
            "all": {
                "label": "All time",
                "ready": True,
                "uptime": all_time["uptime"],
                "samples": all_time["samples"],
                "offline": all_time["offline"],
                "incidents": all_time["incidents"],
                "progress": 100.0,
            },
        },
        "timeline": timeline,
        "timeline_detail": timeline_detail,
        "incidents": incident_payload,
    }


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="__DESCRIPTION__">
<meta name="theme-color" content="#0E0724">

<link rel="canonical" href="__PUBLIC_URL__/">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">

<meta property="og:type" content="website">
<meta property="og:url" content="__PUBLIC_URL__/">
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="__DESCRIPTION__">
<meta property="og:image" content="__PUBLIC_URL__/og-image.png?v=2">
<meta property="og:image:secure_url" content="__PUBLIC_URL__/og-image.png?v=2">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Orbinum Watcher monitoring dashboard">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="__TITLE__">
<meta name="twitter:description" content="__DESCRIPTION__">
<meta name="twitter:image" content="__PUBLIC_URL__/og-image.png?v=2">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">

<style>
:root{
  --ink:#0E0724;
  --ink-2:#160C39;
  --panel:rgba(255,255,255,.035);
  --panel-2:rgba(255,255,255,.055);
  --hair:rgba(169,139,255,.18);
  --hair-soft:rgba(169,139,255,.10);
  --txt:#F1ECFF;
  --txt-dim:#A79AD9;
  --txt-faint:#6D62A6;
  --mint:#33E7AC;
  --cyan:#46C9FF;
  --violet:#8B5CFF;
  --amber:#FFC15C;
  --rose:#FF6B8A;
  --online-bg:rgba(51,231,172,.10);
  --online-border:rgba(51,231,172,.38);
  --online-text:#B8FFE6;
  --degraded-bg:rgba(255,193,92,.10);
  --degraded-border:rgba(255,193,92,.40);
  --degraded-text:#FFE3B0;
  --down-bg:rgba(255,107,138,.10);
  --down-border:rgba(255,107,138,.40);
  --down-text:#FFC2CE;
  --tag-ok-bg:rgba(51,231,172,.12);
  --tag-ok-text:#9CF3D3;
  --tag-open-bg:rgba(255,107,138,.14);
  --tag-open-text:#FFB3C1;
  --tick-none:rgba(169,139,255,.14);
  --tick-up-a:#5CF0BE;
  --tick-up-b:#25C892;
  --tick-warn-a:#FFD08A;
  --tick-warn-b:#F0A227;
  --tick-down-a:#FF8AA3;
  --tick-down-b:#E04766;
  --hover-mint:#CFF7E7;
  --hover-mint-border:rgba(51,231,172,.45);
  --halo-violet:rgba(90,52,200,.55);
  --halo-mint:rgba(51,231,172,.14);
  --r-lg:22px;
  --r-md:14px;
  --r-sm:4px;
  --r-pill:999px;
  --display:"Space Grotesk",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
  --gutter:clamp(18px,4vw,44px);
  --stack:clamp(38px,5vw,62px);
}

*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--ink);color:var(--txt);font-family:var(--display);
  font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;overflow-x:hidden;
}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.halo{
  position:fixed;inset:-20% -20% auto -20%;height:70vh;pointer-events:none;z-index:0;
  background:radial-gradient(50% 60% at 18% 0%,var(--halo-violet),transparent 70%),
             radial-gradient(40% 55% at 82% 8%,var(--halo-mint),transparent 72%);
}
.grid-bg{
  position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.35;
  background-image:linear-gradient(var(--hair-soft) 1px,transparent 1px),
                   linear-gradient(90deg,var(--hair-soft) 1px,transparent 1px);
  background-size:46px 46px;
  mask-image:linear-gradient(180deg,#000 0%,transparent 62%);
  -webkit-mask-image:linear-gradient(180deg,#000 0%,transparent 62%);
}
.shell{position:relative;z-index:1;max-width:1080px;margin:0 auto;padding:0 var(--gutter) 72px}

.masthead{display:flex;align-items:center;gap:14px;padding:26px 0 var(--stack)}
.mark{width:46px;height:46px;flex:none}
.mark img{width:100%;height:100%;display:block;object-fit:contain}
.brand{display:flex;flex-direction:column;line-height:1.2}
.brand__name{font-weight:700;font-size:17px;letter-spacing:-.2px}

.identity{margin-bottom:var(--stack)}
.identity__top{display:flex;align-items:flex-start;gap:20px;flex-wrap:wrap}
.identity__name{
  margin:0;font-size:clamp(2.3rem,6.4vw,4.1rem);font-weight:700;letter-spacing:-.03em;
  line-height:1.02;overflow-wrap:anywhere;
}
.status{
  display:inline-flex;align-items:center;gap:10px;margin-top:8px;padding:9px 18px 9px 14px;
  border-radius:var(--r-pill);font-family:var(--mono);font-size:14px;letter-spacing:.2px;white-space:nowrap;
}
.status--online{background:var(--online-bg);border:1px solid var(--online-border);color:var(--online-text)}
.status--degraded,.status--stale{background:var(--degraded-bg);border:1px solid var(--degraded-border);color:var(--degraded-text)}
.status--down{background:var(--down-bg);border:1px solid var(--down-border);color:var(--down-text)}
.beacon{width:9px;height:9px;border-radius:50%;background:currentColor;position:relative;flex:none}
.beacon::after{
  content:"";position:absolute;inset:-5px;border-radius:50%;border:1px solid currentColor;
  opacity:.5;animation:ping 2.4s ease-out infinite;
}
@keyframes ping{0%{transform:scale(.6);opacity:.7}70%{transform:scale(1.5);opacity:0}100%{opacity:0}}
.status__sep{color:var(--txt-faint)}

.pulse{margin:30px 0 0}
.pulse__head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px;gap:16px}
.pulse__title{font-size:14px;color:var(--txt-dim)}
.pulse__legend{display:flex;gap:16px;font-family:var(--mono);font-size:12px;color:var(--txt-faint)}
.pulse__legend span{display:inline-flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:2px;display:inline-block}
.dot--up{background:var(--mint)}
.dot--degraded{background:var(--amber)}
.dot--down{background:var(--rose)}
.dot--none{background:var(--hair)}
.strip{
  display:flex;gap:3px;height:58px;align-items:stretch;padding:12px;border:1px solid var(--hair);
  border-radius:var(--r-md);background:var(--panel);
}
.tick{flex:1 1 0;min-width:0;border-radius:var(--r-sm);transition:transform .12s ease}
.tick:hover{transform:scaleY(1.12)}
.tick--up{background:linear-gradient(180deg,var(--tick-up-a),var(--tick-up-b))}
.tick--degraded{background:linear-gradient(180deg,var(--tick-warn-a),var(--tick-warn-b))}
.tick--down{background:linear-gradient(180deg,var(--tick-down-a),var(--tick-down-b))}
.tick--none{background:var(--tick-none)}
.strip__scale{display:flex;justify-content:space-between;margin-top:9px;font-family:var(--mono);font-size:12px;color:var(--txt-faint)}

.rail{
  display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--hair);border-radius:var(--r-lg);
  background:var(--panel);overflow:hidden;margin-bottom:var(--stack);
}
.cell{padding:22px 24px;border-left:1px solid var(--hair-soft)}
.cell:first-child{border-left:0}
.cell__label{display:block;font-size:13px;color:var(--txt-dim);margin-bottom:6px}
.cell__value{display:block;font-size:clamp(1.5rem,3.4vw,2rem);font-weight:500;letter-spacing:-.02em;line-height:1.15}
.cell__unit{font-size:.55em;color:var(--txt-dim);margin-left:3px}
.cell__note{display:block;margin-top:6px;font-size:12px;color:var(--txt-faint);min-height:19px}

.section__title{font-size:15px;font-weight:500;color:var(--txt-dim);margin:0 0 16px}
.windows{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:var(--stack)}
.window{
  border:1px solid var(--hair);border-radius:var(--r-lg);background:var(--panel);padding:20px 22px 0;
  display:flex;flex-direction:column;min-height:150px;
}
.window__span{font-size:13px;color:var(--txt-dim)}
.window__pct{font-size:clamp(1.6rem,3.6vw,2.15rem);font-weight:500;letter-spacing:-.02em;margin-top:auto;line-height:1.1}
.window__pct--pending{font-size:1.05rem;color:var(--txt-faint);letter-spacing:0}
.window__sub{font-size:12px;color:var(--txt-faint);margin:6px 0 14px;min-height:19px}
.window__coverage{height:3px;margin:0 -22px;background:var(--hair-soft);border-radius:0 0 var(--r-lg) var(--r-lg);overflow:hidden}
.window__coverage i{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),var(--mint));width:0}

.incidents{border:1px solid var(--hair);border-radius:var(--r-lg);background:var(--panel);overflow:hidden}
.itable{width:100%;border-collapse:collapse;font-size:14px}
.itable th{text-align:left;font-weight:500;font-size:13px;color:var(--txt-dim);padding:15px 22px;border-bottom:1px solid var(--hair-soft);background:var(--panel)}
.itable td{padding:15px 22px;border-bottom:1px solid var(--hair-soft);color:var(--txt)}
.itable tr:last-child td{border-bottom:0}
.tag{font-family:var(--mono);font-size:12px;padding:4px 11px;border-radius:var(--r-pill)}
.tag--resolved{background:var(--tag-ok-bg);color:var(--tag-ok-text)}
.tag--open{background:var(--tag-open-bg);color:var(--tag-open-text)}
.empty{padding:38px 24px;text-align:center}
.empty__line{margin:0;color:var(--txt)}
.empty__hint{margin:6px 0 0;font-size:13px;color:var(--txt-faint)}

.footer{margin-top:var(--stack);padding-top:26px;border-top:1px solid var(--hair-soft)}
.footer__lead{max-width:60ch;color:var(--txt-dim);font-size:14px;margin:0 0 24px}
.specs{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin:0}
.specs dt{font-size:12px;color:var(--txt-faint);margin-bottom:4px}
.specs dd{margin:0;font-family:var(--mono);font-size:13px;color:var(--txt-dim);font-variant-numeric:tabular-nums}
.colophon{
  display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;
  margin-top:30px;padding-top:22px;border-top:1px solid var(--hair-soft);
}
.colophon__note{font-family:var(--mono);font-size:12px;color:var(--txt-faint)}
.social{display:flex;gap:10px;flex-wrap:wrap}
.social__link{
  display:inline-flex;align-items:center;gap:9px;text-decoration:none;padding:9px 16px 9px 13px;
  border:1px solid var(--hair);border-radius:var(--r-pill);background:var(--panel);color:var(--txt-dim);
  font-family:var(--mono);font-size:13px;white-space:nowrap;
  transition:color .15s ease,border-color .15s ease,background .15s ease;
}
.social__link:hover,.social__link:focus-visible{color:var(--hover-mint);border-color:var(--hover-mint-border);background:var(--panel-2)}
.social__link svg{width:17px;height:17px;fill:currentColor;flex:none}

@media (max-width:860px){
  .rail{grid-template-columns:repeat(2,1fr)}
  .cell:nth-child(3){border-left:0}
  .cell:nth-child(n+3){border-top:1px solid var(--hair-soft)}
  .windows{grid-template-columns:repeat(2,1fr)}
  .specs{grid-template-columns:repeat(2,1fr)}
}
@media (max-width:520px){
  .strip{height:46px;gap:1px;padding:9px}
  .tick{border-radius:2px}
  .pulse__legend{display:none}
  .itable th:nth-child(2),.itable td:nth-child(2){display:none}
  .identity__top{gap:12px}
  .status{font-size:12px;padding:8px 13px 8px 11px}
}
@media (max-width:390px){
  .cell{padding:18px}
  .window{padding-left:18px;padding-right:18px}
  .window__coverage{margin-left:-18px;margin-right:-18px}
  .social{width:100%}
  .social__link{flex:1;justify-content:center}
}
@media (prefers-reduced-motion:reduce){
  .beacon::after{animation:none}
  .tick{transition:none}
}
:focus-visible{outline:2px solid var(--cyan);outline-offset:3px;border-radius:4px}

/* mobile-right-status */
@media (max-width: 700px) {
    .check-badge {
        align-self: flex-end !important;
        margin-left: auto !important;
        margin-right: 0 !important;
    }
}


/* final-alignment-and-clip-fix */

/* Status pill shares the exact right edge with the rest of the dashboard */
.identity__top {
    width: 100%;
}

.identity__name {
    min-width: 0;
}

.status {
    margin-left: auto !important;
    margin-right: 0 !important;
    flex: none;
}

/* Keep uptime coverage bars inside rounded cards */
.window {
    overflow: hidden !important;
}

.window__coverage {
    box-sizing: border-box;
}



.incidents{
  overflow:hidden;
}

.itable{
  width:100%;
  table-layout:fixed;
}

.itable th:nth-child(1),
.itable td:nth-child(1){width:16%}

.itable th:nth-child(2),
.itable td:nth-child(2){width:19%}

.itable th:nth-child(3),
.itable td:nth-child(3){width:16%}

.itable th:nth-child(4),
.itable td:nth-child(4){width:13%}

.itable th:nth-child(5),
.itable td:nth-child(5){width:16%}

.event-detail-row td{
  padding-top:0;
  border-top:0;
}

.event-type{
  display:inline-flex;
  align-items:center;
  white-space:nowrap;
  padding:5px 9px;
  border-radius:999px;
  font:500 11px/1.2 "JetBrains Mono",monospace;
  background:rgba(139,92,255,.12);
  color:#CDBFFF;
  border:1px solid rgba(139,92,255,.22);
}

.event-type--warning{
  background:rgba(255,193,92,.10);
  color:#FFE0A3;
  border-color:rgba(255,193,92,.24);
}

.event-type--critical{
  background:rgba(255,107,138,.10);
  color:#FFC2CE;
  border-color:rgba(255,107,138,.26);
}

.event-details summary{
  cursor:pointer;
  color:var(--cyan);
  font:500 11px/1.3 "JetBrains Mono",monospace;
  user-select:none;
}

.event-details__panel{
  width:100%;
  margin-top:10px;
  padding:10px 12px;
  border:1px solid var(--hair);
  border-radius:10px;
  background:rgba(255,255,255,.025);
}

.event-details__row{
  display:grid;
  grid-template-columns:minmax(180px,1fr) minmax(120px,auto);
  align-items:center;
  gap:20px;
  padding:4px 0;
  color:var(--txt-dim);
  font-size:11px;
}

.event-details__row strong{
  color:var(--txt);
  font-weight:500;
  white-space:nowrap;
}


@media(max-width:700px){
  .itable{
    table-layout:auto;
  }

  .itable th,
  .itable td{
    padding:10px 8px;
    font-size:11px;
  }

  .event-details__row{
    grid-template-columns:1fr;
    gap:2px;
  }

  .event-details__row strong{
    white-space:normal;
  }
}


.social{display:flex;gap:12px;align-items:center}
.social__link{width:52px;min-width:52px;height:52px;padding:0;justify-content:center}
.social__link svg{width:18px;height:18px}

</style>
</head>

<body>
<div class="halo"></div>
<div class="grid-bg"></div>

<div class="shell">
  <header class="masthead">
    <div class="mark"><img src="/static/icon-192.png" alt="" aria-hidden="true"></div>
    <div class="brand"><span class="brand__name">Orbinum Watcher</span></div>
  </header>

  <section class="identity">
    <div class="identity__top">
      <h1 class="identity__name num" id="validator">__VALIDATOR__</h1>
      <span class="status status--stale" id="statusPill" role="status" aria-live="polite">
        <span class="beacon"></span>
        <span id="statusText">Loading</span>
      </span>
    </div>

    <figure class="pulse">
      <div class="pulse__head">
        <figcaption class="pulse__title">Last 24 hours, <span class="num" id="bucketLabel">15-minute</span> buckets</figcaption>
        <div class="pulse__legend">
          <span><i class="dot dot--up"></i>up</span>
          <span><i class="dot dot--degraded"></i>degraded</span>
          <span><i class="dot dot--down"></i>down</span>
          <span><i class="dot dot--none"></i>no data</span>
        </div>
      </div>
      <div class="strip" id="timeline" aria-label="24 hour validator availability"></div>
      <div class="strip__scale"><span class="num">24h ago</span><span class="num">now</span></div>
    </figure>
  </section>

  <section class="rail" aria-label="Chain state">
    <div class="cell">
      <span class="cell__label">Peers</span>
      <span class="cell__value num" id="peers">—</span>
      <span class="cell__note">connected</span>
    </div>
    <div class="cell">
      <span class="cell__label">Best block</span>
      <span class="cell__value num" id="bestBlock">—</span>
      <span class="cell__note">chain head</span>
    </div>
    <div class="cell">
      <span class="cell__label">Finalized</span>
      <span class="cell__value num" id="finalized">—</span>
      <span class="cell__note num" id="finalizedNote">—</span>
    </div>
    <div class="cell">
      <span class="cell__label">Latency</span>
      <span class="cell__value num"><span id="latency">—</span><span class="cell__unit">ms</span></span>
      <span class="cell__note">external probe</span>
    </div>
  </section>

  <section aria-labelledby="uptimeTitle">
    <h2 class="section__title" id="uptimeTitle">Validator uptime</h2>
    <div class="windows">
      <div class="window">
        <span class="window__span">24 hours</span>
        <span class="window__pct window__pct--pending num" id="pct24">Collecting</span>
        <span class="window__sub num" id="sub24">—</span>
        <span class="window__coverage"><i id="cov24"></i></span>
      </div>
      <div class="window">
        <span class="window__span">7 days</span>
        <span class="window__pct window__pct--pending num" id="pct7">Collecting</span>
        <span class="window__sub num" id="sub7">—</span>
        <span class="window__coverage"><i id="cov7"></i></span>
      </div>
      <div class="window">
        <span class="window__span">30 days</span>
        <span class="window__pct window__pct--pending num" id="pct30">Collecting</span>
        <span class="window__sub num" id="sub30">—</span>
        <span class="window__coverage"><i id="cov30"></i></span>
      </div>
      <div class="window">
        <span class="window__span">All time</span>
        <span class="window__pct num" id="pctAll">—</span>
        <span class="window__sub num" id="subAll">—</span>
        <span class="window__coverage"><i id="covAll" style="width:100%"></i></span>
      </div>
    </div>
  </section>

  <section aria-labelledby="incTitle">
    <h2 class="section__title" id="incTitle">Incident history</h2>
    <p>Durations are estimated from samples. Missing metrics do not prove node downtime; gaps in observations leave recovery unknown.</p><div class="incidents" id="incidentBox"></div>
  </section>

  <footer class="footer">
    <p class="footer__lead">
      Best block, finalized height, peer count and reachability are observed from outside the validator host,
      so an outage on the machine itself cannot hide from this page.
    </p>

    <dl class="specs">
      <div><dt>Monitoring since</dt><dd class="num" id="since">—</dd></div>
      <div><dt>Check interval</dt><dd class="num">60 seconds</dd></div>
      <div><dt>Alerts</dt><dd>Telegram, on state change</dd></div>
      <div><dt>Last sample</dt><dd class="num" id="lastSample">—</dd></div>
    </dl>

    <div class="colophon">
      <span class="colophon__note">Not affiliated with Orbinum Network</span>
      <nav class="social" aria-label="Links">
        <a class="social__link" href="__TELEGRAM_URL__" target="_blank" rel="noopener noreferrer" aria-label="Telegram" title="Telegram">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>
        </a>
        <a class="social__link" href="__GITHUB_URL__" target="_blank" rel="noopener noreferrer" aria-label="GitHub" title="GitHub">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg>
        </a>
        <a class="social__link" href="__X_URL__" target="_blank" rel="noopener noreferrer" aria-label="X" title="X">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24h-6.657l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231 5.45-6.231zm-1.161 17.52h1.833L7.084 4.126H5.117L17.083 19.77z"/></svg>
        </a>
      </nav>
    </div>
  </footer>
</div>

<script>
let currentData = null;
let receivedAt = 0;

const $ = id => document.getElementById(id);
const setText = (id, value) => { const el = $(id); if (el) el.textContent = value; };

function ageSeconds(){
  if (!currentData || currentData.sample_age == null) return null;
  return Math.max(0, Number(currentData.sample_age) + Math.floor((Date.now() - receivedAt) / 1000));
}

function humanAge(sec){
  sec = Math.max(0, Math.floor(Number(sec || 0)));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function humanObserved(sec){
  sec = Math.max(0, Math.floor(Number(sec || 0)));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return h ? `${d}d ${h}h` : `${d}d`;
}

function formatBlock(value){
  if (value == null) return '—';
  return Number(value).toLocaleString('en-US').replaceAll(',', ' ');
}

function renderStatus(){
  if (!currentData) return;

  const age = ageSeconds();
  let uiState = currentData.ui_state || (currentData.state === 'offline' ? 'down' : currentData.state);

  if (age != null && age > Number(currentData.stale_after_seconds || 180)) {
    uiState = 'stale';
  }

  const pill = $('statusPill');
  pill.className = `status status--${uiState}`;

  if (uiState === 'stale') {
    setText('statusText', 'Stale');
  } else if (uiState === 'down') {
    setText('statusText', 'Down');
  } else if (uiState === 'degraded') {
    setText('statusText', 'Degraded');
  } else {
    setText('statusText', 'Online');
  }
}

function compressTimeline(detail){
  const rank = { none: 0, up: 1, degraded: 2, down: 3 };
  const out = [];
  for (let i = 0; i < detail.length; i += 2) {
    const pair = detail.slice(i, i + 2);
    let chosen = pair[0] || { state: 'none', label: 'no data' };
    for (const item of pair) {
      if ((rank[item.state] || 0) > (rank[chosen.state] || 0)) chosen = item;
    }
    out.push({
      state: chosen.state,
      label: pair.map(x => x.label).filter(Boolean).join(' · ')
    });
  }
  return out;
}

function renderTimeline(){
  if (!currentData) return;

  let detail = Array.isArray(currentData.timeline_detail)
    ? currentData.timeline_detail
    : (currentData.timeline || []).map(state => ({ state, label: state }));

  const mobile = window.matchMedia('(max-width: 520px)').matches;
  if (mobile) detail = compressTimeline(detail);

  setText('bucketLabel', mobile ? '30-minute' : '15-minute');

  const strip = $('timeline');
  strip.replaceChildren();

  for (const bucket of detail) {
    const tick = document.createElement('i');
    tick.className = `tick tick--${bucket.state || 'none'}`;
    tick.title = bucket.label || '';
    strip.appendChild(tick);
  }
}

function renderWindow(key, pctId, subId, covId){
  const w = currentData.windows[key];
  const pct = $(pctId);
  const cov = $(covId);
  const progress = Math.max(0, Math.min(100, Number(w.progress || 0)));

  cov.style.width = `${progress}%`;

  if (w.ready) {
    pct.className = 'window__pct num';
    pct.textContent = `${Number(w.uptime).toFixed(1)}%`;
    setText(subId, `${w.samples} samples · ${w.incidents} incidents`);
  } else {
    pct.className = 'window__pct window__pct--pending num';
    pct.textContent = 'Collecting';
    setText(subId, `${humanObserved(currentData.monitor_age)} observed · ${progress.toFixed(1)}%`);
  }
}

function renderIncidents(){
  const box = $('incidentBox');
  box.replaceChildren();
  const items = currentData.incidents || [];

  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';

    const line = document.createElement('p');
    line.className = 'empty__line';
    line.textContent = 'No incidents observed in the last 30 days.';

    const hint = document.createElement('p');
    hint.className = 'empty__hint';
    hint.textContent = 'Metrics availability, missing observations and sustained load anomalies appear here.';

    empty.append(line, hint);
    box.appendChild(empty);
    return;
  }

  const table = document.createElement('table');
  table.className = 'itable';
  table.innerHTML =
    '<thead><tr>' +
    '<th>Started</th>' +
    '<th>Type</th>' +
    '<th>Recovery observed</th>' +
    '<th>Duration</th>' +
    '<th>Status</th>' +
    '</tr></thead>';

  const tbody = document.createElement('tbody');

  for (const item of items) {
    const tr = document.createElement('tr');

    const started = document.createElement('td');
    started.className = 'num';
    started.textContent = item.started || '—';

    const type = document.createElement('td');
    const typeTag = document.createElement('span');
    typeTag.className =
      `event-type event-type--${item.severity || 'info'}`;
    typeTag.textContent = item.type || 'Incident';
    type.appendChild(typeTag);

    const recovered = document.createElement('td');
    recovered.className = 'num';
    recovered.textContent = item.recovered || '—';

    const duration = document.createElement('td');
    duration.className = 'num';
    duration.textContent = item.duration || '—';

    const status = document.createElement('td');
    const tag = document.createElement('span');
    tag.className = item.active
      ? 'tag tag--open'
      : 'tag tag--resolved';
    tag.textContent = item.status || (item.active ? 'open' : 'recovered');
    status.appendChild(tag);

    tr.append(started, type, recovered, duration, status);
    tbody.appendChild(tr);

    if (Array.isArray(item.details) && item.details.length) {
      const detailTr = document.createElement('tr');
      detailTr.className = 'event-detail-row';

      const detailTd = document.createElement('td');
      detailTd.colSpan = 5;

      const details = document.createElement('details');
      details.className = 'event-details';

      const summary = document.createElement('summary');
      summary.textContent = 'metrics';

      const panel = document.createElement('div');
      panel.className = 'event-details__panel';

      for (const metric of item.details) {
        const row = document.createElement('div');
        row.className = 'event-details__row';

        const label = document.createElement('span');
        label.textContent = metric.label || '—';

        const value = document.createElement('strong');
        value.className = 'num';
        value.textContent = metric.value || '—';

        row.append(label, value);
        panel.appendChild(row);
      }

      details.append(summary, panel);
      detailTd.appendChild(details);
      detailTr.appendChild(detailTd);
      tbody.appendChild(detailTr);
    }
  }

  table.appendChild(tbody);
  box.appendChild(table);
}

function render(data){
  currentData = data;
  receivedAt = Date.now();

  setText('validator', data.validator || '—');
  setText('peers', data.peers ?? '—');
  setText('bestBlock', formatBlock(data.best));
  setText('finalized', formatBlock(data.finalized));
  setText('latency', data.latency_ms ?? '—');

  if (data.best != null && data.finalized != null) {
    const behind = Math.max(0, Number(data.best) - Number(data.finalized));
    setText('finalizedNote', behind === 1 ? '1 block behind head' : `${behind} blocks behind head`);
  } else {
    setText('finalizedNote', '—');
  }

  renderStatus();
  renderTimeline();

  renderWindow('24h', 'pct24', 'sub24', 'cov24');
  renderWindow('7d', 'pct7', 'sub7', 'cov7');
  renderWindow('30d', 'pct30', 'sub30', 'cov30');

  const all = data.windows.all;
  setText('pctAll', `${Number(all.uptime).toFixed(1)}%`);
  setText('subAll', `${all.samples} samples · ${all.incidents} incidents`);
  $('covAll').style.width = '100%';

  renderIncidents();
  setText('since', data.monitoring_since || '—');
  setText('lastSample', data.last_sample || '—');
}

async function refresh(){
  try {
    const response = await fetch('/api/status', {
      cache: 'no-store',
      headers: { Accept: 'application/json' }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    console.error('Orbinum Watcher refresh failed:', error);
  }
}

setInterval(renderStatus, 1000);
setInterval(() => { if (!document.hidden) refresh(); }, __POLL_MS__);

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
window.addEventListener('focus', refresh);
window.addEventListener('resize', () => {
  if (currentData) renderTimeline();
});

refresh();
</script>
</body>
</html>
'''


def page():
    title = f"Orbinum Watcher — {VALIDATOR_NAME}"
    description = (
        "External uptime monitoring for an Orbinum validator with live chain health, "
        "uptime history and Telegram incident alerts."
    )

    replacements = {
        "__TITLE__": html.escape(title, quote=True),
        "__DESCRIPTION__": html.escape(description, quote=True),
        "__PUBLIC_URL__": html.escape(PUBLIC_URL, quote=True),
        "__VALIDATOR__": html.escape(VALIDATOR_NAME),
        "__TELEGRAM_URL__": html.escape(TELEGRAM_URL, quote=True),
        "__GITHUB_URL__": html.escape(GITHUB_URL, quote=True),
        "__X_URL__": html.escape("https://x.com/ra5alghul", quote=True),
        "__POLL_MS__": str(POLL_SECONDS * 1000),
    }

    result = HTML_TEMPLATE
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STATIC_ALIASES = {
    "/favicon.ico": "favicon.ico",
    "/favicon.svg": "favicon.svg",
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/og-image.png": "og-image.png",
}


class Handler(BaseHTTPRequestHandler):
    def send_payload_headers(self, status, content_type, length, cache_control="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()

    def serve_static(self, file_name, head_only=False):
        if not file_name or "/" in file_name or "\\" in file_name or ".." in file_name:
            self.send_error(404)
            return

        file_path = STATIC_DIR / file_name
        if not file_path.is_file():
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        size = file_path.stat().st_size
        self.send_payload_headers(200, content_type, size, "public, max-age=3600")
        if not head_only:
            self.wfile.write(file_path.read_bytes())

    def handle_request(self, head_only=False):
        path = urlparse(self.path).path

        if path == "/health":
            body = b"ok\n"
            self.send_payload_headers(200, "text/plain; charset=utf-8", len(body))
            if not head_only:
                self.wfile.write(body)
            return

        if path == "/api/status":
            try:
                body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
                self.send_payload_headers(200, "application/json; charset=utf-8", len(body))
                if not head_only:
                    self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.send_payload_headers(500, "application/json; charset=utf-8", len(body))
                if not head_only:
                    self.wfile.write(body)
            return

        if path in STATIC_ALIASES:
            self.serve_static(STATIC_ALIASES[path], head_only=head_only)
            return

        if path.startswith("/static/"):
            self.serve_static(path[len("/static/"):], head_only=head_only)
            return

        if path not in ("/", "/index.html"):
            self.send_error(404)
            return

        body = page().encode("utf-8")
        self.send_payload_headers(200, "text/html; charset=utf-8", len(body))
        if not head_only:
            self.wfile.write(body)

    def do_GET(self):
        self.handle_request(head_only=False)

    def do_HEAD(self):
        self.handle_request(head_only=True)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Orbinum Watcher listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
