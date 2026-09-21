#!/usr/bin/env python3

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

DB = os.getenv("ORBINUM_DB", "/var/lib/orbinum-monitor/uptime.db")
OWNER_FILE = os.getenv("ORBINUM_BOT_OWNER_FILE", "/var/lib/orbinum-monitor/bot_chat_id")
STATE_FILE = os.getenv("ORBINUM_BOT_STATE_FILE", "/var/lib/orbinum-monitor/bot_state.json")
EVENTS_FILE = os.getenv("ORBINUM_EVENTS_FILE", "/var/lib/orbinum-monitor/events.json")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PAIR_CODE = os.environ["TELEGRAM_PAIR_CODE"]
VALIDATOR_NAME = os.getenv("ORBINUM_VALIDATOR_NAME", "robotek8-orbinum")
DASHBOARD_URL = os.getenv("ORBINUM_DASHBOARD_URL", "https://orbinum-watcher.xyz")

API = f"https://api.telegram.org/bot{TOKEN}/"
KZ_TZ = timezone(timedelta(hours=5))


def connect():
    return sqlite3.connect(DB, timeout=5)


def telegram(method, data=None, timeout=35):
    encoded = urllib.parse.urlencode(data or {}).encode()
    request = urllib.request.Request(API + method, data=encoded)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🟢 Status"}, {"text": "📊 Uptime"}],
            [{"text": "🚨 Incidents"}, {"text": "🧭 Diagnostics"}],
            [{"text": "🌐 Dashboard"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Choose a monitor view",
    }


def send(chat_id, text, reply_markup=None):
    try:
        data = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        telegram("sendMessage", data)
    except Exception as exc:
        print("Telegram send error:", exc, flush=True)


def send_menu(chat_id, text="🛰 Orbinum Validator Monitor"):
    send(chat_id, text, main_keyboard())


def send_dashboard(chat_id):
    send(
        chat_id,
        "🌐 Public Orbinum Watcher dashboard",
        {
            "inline_keyboard": [
                [{"text": "Open dashboard", "url": DASHBOARD_URL}],
            ]
        },
    )


def load_owner():
    try:
        with open(OWNER_FILE, "r", encoding="utf-8") as file:
            return int(file.read().strip())
    except Exception:
        return None


def save_owner(chat_id):
    with open(OWNER_FILE, "w", encoding="utf-8") as file:
        file.write(str(chat_id))


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file)


def load_event_snapshot():
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def fmt_time(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, KZ_TZ).strftime("%d.%m.%Y %H:%M:%S")


def latest_sample():
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


def period_stats(seconds):
    cutoff = int(time.time()) - seconds
    con = connect()
    rows = con.execute(
        "SELECT ts, ok FROM samples WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    ).fetchall()
    con.close()

    if not rows:
        return None

    total = len(rows)
    online = sum(row[1] for row in rows)
    incidents = 0
    longest = 0
    current = 0
    previous = 1

    for _, ok in rows:
        if not ok:
            current += 1
            longest = max(longest, current)
            if previous:
                incidents += 1
        else:
            current = 0
        previous = ok

    return {
        "uptime": online / total * 100,
        "samples": total,
        "offline": total - online,
        "incidents": incidents,
        "longest": longest,
    }


def current_state():
    row = latest_sample()
    if not row:
        return "offline", None

    ts, ok, status, *_ = row
    if int(time.time()) - ts > 150:
        return "offline", row
    if ok == 1 and status == "online":
        return "online", row
    if status == "degraded":
        return "degraded", row
    return "offline", row


def latest_diagnostic(max_source_age=180):
    snapshot = load_event_snapshot()
    if not snapshot:
        return None

    now = int(time.time())
    source_ts = snapshot.get("source_latest_ts")
    source_age = snapshot.get("source_sample_age_s")
    if not isinstance(source_age, int) and isinstance(source_ts, int):
        source_age = max(0, now - source_ts)
    if isinstance(source_age, int) and source_age > max_source_age:
        return None

    events = snapshot.get("events")
    if not isinstance(events, list):
        return None
    candidates = [event for event in events if isinstance(event, dict)]
    if not candidates:
        return None

    candidates.sort(
        key=lambda event: int(event.get("ended") or event.get("started") or 0),
        reverse=True,
    )
    event = candidates[0].copy()
    event["source_age_s"] = source_age
    return event


def diagnostic_hint():
    event = latest_diagnostic()
    if not event:
        return None
    event_type = str(event.get("type") or "")
    if event_type == "Docker engine unavailable":
        return "Docker Engine unavailable on Windows host"
    if event_type == "Validator restart":
        return "validator container restart detected"
    if event_type == "Node offline":
        return "validator container reported offline locally"
    if event_type == "Block stall":
        return "best block stalled locally"
    return None


def diagnostics_text():
    snapshot = load_event_snapshot()
    if not snapshot:
        return "🧭 LOCAL DIAGNOSTICS\n\nNo Windows diagnostic snapshot is available yet."

    now = int(time.time())
    source_ts = snapshot.get("source_latest_ts")
    source_age = snapshot.get("source_sample_age_s")
    if not isinstance(source_age, int) and isinstance(source_ts, int):
        source_age = max(0, now - source_ts)

    events = [event for event in snapshot.get("events", []) if isinstance(event, dict)]
    events.sort(
        key=lambda event: int(event.get("ended") or event.get("started") or 0),
        reverse=True,
    )

    text = "🧭 LOCAL DIAGNOSTICS\n\n"
    text += f"Telemetry age: {source_age if source_age is not None else '—'}s\n"
    if not events:
        return text + "Latest event: none"

    event = events[0]
    kinds = ", ".join(event.get("kinds") or []) or "—"
    text += (
        f"Latest event: {event.get('type') or 'Load event'}\n"
        f"Status: {event.get('status') or '—'}\n"
        f"Severity: {str(event.get('severity') or '—').upper()}\n"
        f"Started: {fmt_time(event.get('started'))} UTC+5\n"
        f"Signals: {kinds}"
    )
    return text


def status_text():
    state, row = current_state()
    icons = {"online": "🟢", "degraded": "🟠", "offline": "🔴"}

    if not row:
        return "🔴 No monitoring data yet."

    ts, _, _, peers, best, finalized, latency, _ = row
    s24 = period_stats(86400)
    s7 = period_stats(604800)
    s30 = period_stats(2592000)

    def uptime(data):
        return f"{data['uptime']:.3f}%" if data else "—"

    text = (
        f"{icons[state]} {VALIDATOR_NAME}\n\n"
        f"Status: {state.upper()}\n"
        f"Peers: {peers if peers is not None else '—'}\n"
        f"Best: #{best if best is not None else '—'}\n"
        f"Finalized: #{finalized if finalized is not None else '—'}\n"
        f"Latency: {latency if latency is not None else '—'} ms\n\n"
        f"24h uptime: {uptime(s24)}\n"
        f"7d uptime: {uptime(s7)}\n"
        f"30d uptime: {uptime(s30)}\n\n"
        f"Last sample: {fmt_time(ts)} UTC+5\n"
        f"Sample age: {int(time.time()) - ts}s"
    )
    hint = diagnostic_hint()
    if hint:
        text += f"\nLocal diagnostic: {hint}"
    return text


def uptime_text():
    periods = [
        ("24 hours", 86400),
        ("7 days", 604800),
        ("30 days", 2592000),
        ("All time", 315360000),
    ]

    text = "📊 ORBINUM UPTIME\n\n"

    for name, seconds in periods:
        data = period_stats(seconds)
        if not data:
            text += f"{name}: no data\n"
            continue

        text += (
            f"{name}\n"
            f"Uptime: {data['uptime']:.3f}%\n"
            f"Offline samples: {data['offline']}\n"
            f"Incidents: {data['incidents']}\n"
            f"Longest: {data['longest']} samples\n\n"
        )

    return text.strip()


def incidents_text():
    cutoff = int(time.time()) - 2592000
    con = connect()
    rows = con.execute(
        "SELECT ts, ok FROM samples WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    ).fetchall()
    con.close()

    incidents = []
    started = None

    for ts, ok in rows:
        if not ok and started is None:
            started = ts
        elif ok and started is not None:
            incidents.append((started, ts))
            started = None

    if started is not None:
        incidents.append((started, None))

    if not incidents:
        return "🟢 No incidents recorded in the last 30 days."

    text = "🚨 LAST INCIDENTS\n\n"

    for start, end in incidents[-10:][::-1]:
        if end:
            duration = max(1, round((end - start) / 60))
            text += f"{fmt_time(start)} → {fmt_time(end)}\nDowntime: {duration} min\n\n"
        else:
            text += f"{fmt_time(start)} → NOW\nStatus: ACTIVE INCIDENT\n\n"

    return text.strip()


def handle_message(message):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    owner = load_owner()

    if text.startswith("/pair"):
        parts = text.split(maxsplit=1)

        if owner is not None:
            send(chat_id, "✅ Bot is already paired with you." if owner == chat_id else "⛔ Bot is already paired.")
            return

        if len(parts) != 2 or parts[1].strip() != PAIR_CODE:
            send(chat_id, "⛔ Invalid pairing code.")
            return

        save_owner(chat_id)
        send_menu(
            chat_id,
            "✅ Orbinum Validator Monitor paired.\n\nUse the buttons below; slash commands still work.",
        )
        print("Paired with chat:", chat_id, flush=True)
        return

    if owner is None:
        send(chat_id, "🔐 Private Orbinum monitor.\nPair using:\n/pair YOUR_CODE")
        return

    if chat_id != owner:
        send(chat_id, "⛔ Private monitoring bot.")
        return

    actions = {
        "/status": "status",
        "🟢 Status": "status",
        "/uptime": "uptime",
        "📊 Uptime": "uptime",
        "/incidents": "incidents",
        "🚨 Incidents": "incidents",
        "/diag": "diagnostics",
        "/diagnostics": "diagnostics",
        "🧭 Diagnostics": "diagnostics",
        "🌐 Dashboard": "dashboard",
    }

    if text in ("/start", "/help"):
        send_menu(
            chat_id,
            "🛰 Orbinum Validator Monitor\n\nTap a button for the current view. Slash commands remain available.",
        )
        return

    action = actions.get(text)
    if action == "status":
        send(chat_id, status_text(), main_keyboard())
    elif action == "uptime":
        send(chat_id, uptime_text(), main_keyboard())
    elif action == "incidents":
        send(chat_id, incidents_text(), main_keyboard())
    elif action == "diagnostics":
        send(chat_id, diagnostics_text(), main_keyboard())
    elif action == "dashboard":
        send_dashboard(chat_id)
    else:
        send_menu(chat_id, "Choose an action:")


def check_alerts():
    owner = load_owner()
    if owner is None:
        return

    current, row = current_state()
    state = load_state()
    previous = state.get("state")

    if previous is None:
        state["state"] = current
        if current != "online":
            state["incident_started"] = int(time.time())
        save_state(state)
        return

    if current == previous:
        return

    now = int(time.time())

    if current == "offline":
        state["incident_started"] = now
        if row:
            _, _, _, peers, best, finalized, _, error = row
            text = (
                "🔴 ORBINUM VALIDATOR OFFLINE\n\n"
                f"Detected: {fmt_time(now)} UTC+5\n"
                f"Peers: {peers if peers is not None else '—'}\n"
                f"Last best: #{best if best is not None else '—'}\n"
                f"Last finalized: #{finalized if finalized is not None else '—'}"
            )
            if error:
                text += f"\nError: {error}"
            hint = diagnostic_hint()
            if hint:
                text += f"\nLikely cause: {hint}"
        else:
            text = "🔴 ORBINUM VALIDATOR OFFLINE\n\nMonitoring data unavailable."
        send(owner, text)

    elif current == "degraded":
        if row:
            _, _, _, peers, best, finalized, _, error = row
            text = (
                "🟠 ORBINUM VALIDATOR DEGRADED\n\n"
                f"Peers: {peers if peers is not None else '—'}\n"
                f"Best: #{best if best is not None else '—'}\n"
                f"Finalized: #{finalized if finalized is not None else '—'}"
            )
            if error:
                text += f"\nReason: {error}"
            send(owner, text)

    elif current == "online":
        started = state.get("incident_started")
        downtime = max(1, round((now - started) / 60)) if started else "unknown"
        row = latest_sample()

        if row:
            _, _, _, peers, best, finalized, _, _ = row
            s24 = period_stats(86400)
            s30 = period_stats(2592000)
            send(
                owner,
                "🟢 ORBINUM VALIDATOR RECOVERED\n\n"
                f"Downtime: {downtime} min\n"
                f"Peers: {peers}\nBest: #{best}\nFinalized: #{finalized}\n\n"
                f"24h uptime: {s24['uptime']:.3f}%\n"
                f"30d uptime: {s30['uptime']:.3f}%",
            )

        state.pop("incident_started", None)

    state["state"] = current
    save_state(state)


def check_diagnostic_alerts():
    owner = load_owner()
    if owner is None:
        return

    event = latest_diagnostic()
    if not event or event.get("type") != "Docker engine unavailable":
        return

    event_id = str(event.get("id") or f"docker-{event.get('started')}")
    event_status = str(event.get("status") or "Open")
    state = load_state()
    previous_id = state.get("diagnostic_event_id")
    previous_status = state.get("diagnostic_event_status")

    if event_id == previous_id and event_status == previous_status:
        return

    if event_status == "Open":
        send(
            owner,
            "🐳 DOCKER ENGINE UNAVAILABLE\n\n"
            f"Detected by Windows telemetry: {fmt_time(event.get('started'))} UTC+5\n"
            "Docker stats/inspect cannot reach the engine.\n"
            "The validator container may be unavailable because Docker Desktop/WSL backend is down.\n\n"
            "No automatic restart was attempted.",
        )
    elif previous_id == event_id and previous_status == "Open":
        send(
            owner,
            "🐳 DOCKER ENGINE RECOVERED\n\n"
            "Windows telemetry can reach Docker again.\n"
            "Validator recovery is tracked separately by the external uptime monitor.",
        )

    state["diagnostic_event_id"] = event_id
    state["diagnostic_event_status"] = event_status
    save_state(state)


def main():
    print("Orbinum Telegram bot started", flush=True)
    offset = 0

    while True:
        try:
            result = telegram("getUpdates", {"timeout": 20, "offset": offset}, timeout=30)

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_message(message)

            check_alerts()
            check_diagnostic_alerts()

        except Exception as exc:
            print("Bot loop error:", exc, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
