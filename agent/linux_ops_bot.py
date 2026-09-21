#!/usr/bin/env python3
"""Read-only Telegram operations panel for the local home/VPS stack.

Runs on the Ubuntu host that owns the Orbinum validator.  It intentionally
contains no start/stop/restart actions: the bot is an observability surface,
not a remote-control plane.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

RPC_URL = os.getenv("ORBINUM_RPC_URL", "http://127.0.0.1:9944")
ENV_FILE = Path(os.getenv("OPS_BOT_ENV_FILE", "/home/rasalghul/orbinum-watcher/.env"))
WATCHER_API = os.getenv("ORBINUM_WATCHER_API", "https://orbinum-watcher.xyz/api/status")
DASHBOARD_URL = os.getenv("ORBINUM_DASHBOARD_URL", "https://orbinum-watcher.xyz")
VPS_HOST = os.getenv("OPS_VPS_HOST", "169.58.246.105")
CHECK_INTERVAL = int(os.getenv("OPS_CHECK_INTERVAL", "30"))

RIALO_SERVICES = (
    "rialo-edge-gateway.service",
    "rialo-edge-anchor.service",
    "rialo-edge-publisher.service",
    "rialo-edge-balance-guard.service",
)

last_state: str | None = None
update_offset = 0


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    with path.open(encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = load_env()
BOT_TOKEN = ENV["BOT_TOKEN"]
CHAT_ID = str(ENV["CHAT_ID"])
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method: str, params: dict[str, Any] | None = None, timeout: int = 30):
    data = urllib.parse.urlencode(params or {}).encode()
    request = urllib.request.Request(f"{TG_BASE}/{method}", data=data)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def main_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "🟢 Infrastructure"}],
            [{"text": "🛰 Orbinum"}, {"text": "⚡ Rialo"}],
            [{"text": "🖥 VPS"}, {"text": "🌿 Canopy"}],
            [{"text": "🚨 Alerts"}, {"text": "📊 Dashboard"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Infrastructure status",
    }


def send_message(text: str, reply_markup: dict[str, Any] | None = None) -> None:
    try:
        params: dict[str, Any] = {"chat_id": CHAT_ID, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        telegram("sendMessage", params)
    except Exception as exc:
        print("Telegram send error:", exc, flush=True)


def send_menu(text: str = "🛰 Infrastructure Monitor") -> None:
    send_message(text, main_keyboard())


def send_dashboard() -> None:
    send_message(
        "📊 Monitoring dashboard",
        {
            "inline_keyboard": [
                [{"text": "Open dashboard", "url": DASHBOARD_URL}],
            ]
        },
    )


def run(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def service_state(name: str) -> str:
    try:
        result = run(["systemctl", "is-active", name], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout.strip() or result.stderr.strip() or "unknown").lower()


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url: str, timeout: int = 8) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "robotek8-infra-bot/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode())
    if not isinstance(value, dict):
        raise ValueError("JSON endpoint did not return an object")
    return value


def http_ok(url: str, timeout: int = 6) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "robotek8-infra-bot/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def rpc(method: str, params: list[Any] | None = None) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode()
    request = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode())
    if "error" in result:
        raise RuntimeError(str(result["error"]))
    return result["result"]


def block_number(header: dict[str, Any] | None) -> int | None:
    if not isinstance(header, dict):
        return None
    value = header.get("number")
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def human_bytes(value: int) -> str:
    gib = value / (1024 ** 3)
    return f"{gib:.1f} GB"


def human_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def cpu_percent() -> float | None:
    def sample() -> tuple[int, int]:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        numbers = [int(value) for value in fields]
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        return sum(numbers), idle

    try:
        total1, idle1 = sample()
        time.sleep(0.15)
        total2, idle2 = sample()
        total_delta = total2 - total1
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, (1 - (idle2 - idle1) / total_delta) * 100))
    except (OSError, ValueError, IndexError):
        return None


def memory_usage() -> tuple[int, int] | None:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            value = raw.strip().split()[0]
            if value.isdigit():
                values[key] = int(value) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        return total - available, total
    except (OSError, KeyError, ValueError):
        return None


def host_snapshot() -> dict[str, Any]:
    cpu = cpu_percent()
    memory = memory_usage()
    disk = shutil.disk_usage("/")
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        uptime = 0.0
    return {
        "cpu": cpu,
        "memory_used": memory[0] if memory else None,
        "memory_total": memory[1] if memory else None,
        "disk_percent": disk.used / disk.total * 100 if disk.total else None,
        "uptime": uptime,
    }


def orbinum_snapshot() -> dict[str, Any]:
    data: dict[str, Any] = {
        "online": False,
        "peers": None,
        "syncing": None,
        "best": None,
        "finalized": None,
        "tunnel_service": service_state("orbinum-tunnel.service"),
        "public_p2p": tcp_open(VPS_HOST, 30333),
    }
    try:
        health = rpc("system_health")
        best = rpc("chain_getHeader")
        finalized_hash = rpc("chain_getFinalizedHead")
        finalized = rpc("chain_getHeader", [finalized_hash])
        data.update(
            online=True,
            peers=health.get("peers", 0),
            syncing=health.get("isSyncing", False),
            best=block_number(best),
            finalized=block_number(finalized),
        )
    except Exception as exc:
        data["error"] = str(exc)

    try:
        external = http_json(WATCHER_API)
        data["external_state"] = external.get("ui_state")
        data["sample_age"] = external.get("sample_age")
        data["latency_ms"] = external.get("latency_ms")
    except Exception as exc:
        data["external_error"] = str(exc)
    return data


def journal_balance() -> float | None:
    try:
        result = run(
            [
                "journalctl",
                "-u",
                "rialo-edge-balance-guard.service",
                "-n",
                "120",
                "--no-pager",
            ],
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    matches = re.findall(r"\[BALANCE(?: AFTER AIRDROP)?\].*?([0-9]+(?:\.[0-9]+)?) RLO", result.stdout)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def rialo_snapshot() -> dict[str, Any]:
    states = {name: service_state(name) for name in RIALO_SERVICES}
    return {
        "states": states,
        "balance": journal_balance(),
        "overall": "ok" if all(value == "active" for value in states.values()) else "problem",
    }


def vps_snapshot() -> dict[str, Any]:
    return {
        "https": tcp_open(VPS_HOST, 443),
        "http": tcp_open(VPS_HOST, 80),
        "watcher": http_ok("https://orbinum-watcher.xyz/health"),
        "p2p": tcp_open(VPS_HOST, 30333),
    }


def canopy_snapshot() -> dict[str, Any]:
    return {"port_9001": tcp_open(VPS_HOST, 9001)}


def infra_snapshot() -> dict[str, Any]:
    home = host_snapshot()
    orbinum = orbinum_snapshot()
    rialo = rialo_snapshot()
    vps = vps_snapshot()
    canopy = canopy_snapshot()

    problems: list[str] = []
    if not orbinum["online"]:
        problems.append("Orbinum RPC is offline")
    elif orbinum.get("syncing"):
        problems.append("Orbinum is syncing")
    elif not orbinum.get("peers"):
        problems.append("Orbinum has no peers")
    if orbinum["tunnel_service"] != "active" or not orbinum["public_p2p"]:
        problems.append("Orbinum P2P tunnel is unavailable")
    if rialo["overall"] != "ok":
        problems.append("One or more Rialo services are not active")
    if rialo.get("balance") is not None and rialo["balance"] < 0.25:
        problems.append(f"Rialo balance is low ({rialo['balance']:.3f} RLO)")
    if not vps["watcher"] or not vps["https"]:
        problems.append("VPS monitoring endpoint is unavailable")
    if not canopy["port_9001"]:
        problems.append("Canopy port 9001 is unreachable")

    return {
        "home": home,
        "orbinum": orbinum,
        "rialo": rialo,
        "vps": vps,
        "canopy": canopy,
        "problems": problems,
        "overall": "ok" if not problems else "degraded",
    }


def fmt_num(value: Any, prefix: str = "") -> str:
    return "—" if value is None else f"{prefix}{value}"


def infrastructure_text(snapshot: dict[str, Any] | None = None) -> str:
    snap = snapshot or infra_snapshot()
    home = snap["home"]
    orb = snap["orbinum"]
    rialo = snap["rialo"]
    vps = snap["vps"]
    canopy = snap["canopy"]

    icon = "🟢" if snap["overall"] == "ok" else "🟡"
    title = "Infrastructure OK" if snap["overall"] == "ok" else "Infrastructure DEGRADED"

    cpu = "—" if home["cpu"] is None else f"{home['cpu']:.0f}%"
    if home["memory_used"] is None:
        memory = "—"
    else:
        memory = f"{human_bytes(home['memory_used'])} / {human_bytes(home['memory_total'])}"
    disk = "—" if home["disk_percent"] is None else f"{home['disk_percent']:.0f}%"

    services_ok = sum(state == "active" for state in rialo["states"].values())
    balance = "—" if rialo["balance"] is None else f"{rialo['balance']:.3f} RLO"

    return (
        f"{icon} {title}\n\n"
        "🏠 Home PC\n"
        f"CPU: {cpu}\n"
        f"RAM: {memory}\n"
        f"Disk: {disk}\n"
        f"Uptime: {human_uptime(home['uptime'])}\n\n"
        "🛰 Orbinum\n"
        f"Node: {'ONLINE' if orb['online'] else 'OFFLINE'}\n"
        f"Peers: {fmt_num(orb['peers'])}\n"
        f"Best: {fmt_num(orb['best'], '#')}\n"
        f"Finalized: {fmt_num(orb['finalized'], '#')}\n"
        f"Tunnel :30333: {'OK' if orb['public_p2p'] and orb['tunnel_service'] == 'active' else 'PROBLEM'}\n\n"
        "⚡ Rialo Edge\n"
        f"Services: {services_ok}/{len(RIALO_SERVICES)} active\n"
        f"RLO: {balance}\n"
        f"Balance guard: {rialo['states']['rialo-edge-balance-guard.service'].upper()}\n\n"
        "🖥 VPS\n"
        f"HTTPS: {'OK' if vps['https'] else 'DOWN'}\n"
        f"Watcher: {'OK' if vps['watcher'] else 'DOWN'}\n"
        f"P2P :30333: {'OK' if vps['p2p'] else 'DOWN'}\n\n"
        "🌿 Canopy\n"
        f"Port :9001: {'OK' if canopy['port_9001'] else 'DOWN'}\n\n"
        f"Updated: {time.strftime('%H:%M:%S')}"
    )


def orbinum_text() -> str:
    data = orbinum_snapshot()
    return (
        "🛰 ORBINUM\n\n"
        f"Node: {'ONLINE' if data['online'] else 'OFFLINE'}\n"
        f"Peers: {fmt_num(data['peers'])}\n"
        f"Syncing: {fmt_num(data['syncing'])}\n"
        f"Best: {fmt_num(data['best'], '#')}\n"
        f"Finalized: {fmt_num(data['finalized'], '#')}\n"
        f"Tunnel service: {data['tunnel_service'].upper()}\n"
        f"Public :30333: {'OK' if data['public_p2p'] else 'DOWN'}\n"
        f"External monitor: {str(data.get('external_state') or '—').upper()}\n"
        f"Metrics latency: {fmt_num(data.get('latency_ms'))} ms"
    )


def rialo_text() -> str:
    data = rialo_snapshot()
    lines = ["⚡ RIALO EDGE", ""]
    labels = {
        "rialo-edge-gateway.service": "Gateway",
        "rialo-edge-anchor.service": "Anchor",
        "rialo-edge-publisher.service": "Publisher",
        "rialo-edge-balance-guard.service": "Balance guard",
    }
    for service, label in labels.items():
        state = data["states"][service]
        lines.append(f"{label}: {'🟢' if state == 'active' else '🔴'} {state.upper()}")
    lines.append("")
    lines.append(
        "RLO: —" if data["balance"] is None else f"RLO: {data['balance']:.6f}"
    )
    return "\n".join(lines)


def vps_text() -> str:
    data = vps_snapshot()
    return (
        "🖥 VPS\n\n"
        f"HTTP :80: {'🟢 OK' if data['http'] else '🔴 DOWN'}\n"
        f"HTTPS :443: {'🟢 OK' if data['https'] else '🔴 DOWN'}\n"
        f"Orbinum Watcher: {'🟢 OK' if data['watcher'] else '🔴 DOWN'}\n"
        f"Reverse P2P :30333: {'🟢 OK' if data['p2p'] else '🔴 DOWN'}"
    )


def canopy_text() -> str:
    data = canopy_snapshot()
    return (
        "🌿 CANOPY\n\n"
        f"Public port :9001: {'🟢 REACHABLE' if data['port_9001'] else '🔴 UNREACHABLE'}"
    )


def alerts_text() -> str:
    snap = infra_snapshot()
    if not snap["problems"]:
        return "🟢 No active infrastructure alerts."
    return "🚨 ACTIVE ALERTS\n\n" + "\n".join(f"• {item}" for item in snap["problems"])


def current_state() -> str:
    snap = infra_snapshot()
    return snap["overall"]


def check_state() -> None:
    global last_state
    state = current_state()
    if last_state is None:
        last_state = state
        return
    if state == last_state:
        return
    if state == "degraded":
        send_message(alerts_text(), main_keyboard())
    else:
        send_message("🟢 Infrastructure recovered.", main_keyboard())
    last_state = state


def handle_message(text: str, chat_id: Any) -> None:
    if str(chat_id) != CHAT_ID:
        return

    value = text.strip()

    if value in ("/start", "/help"):
        send_menu("🛰 Infrastructure Monitor\n\nRead-only monitoring panel.")
    elif value in ("/status", "🟢 Infrastructure"):
        send_message(infrastructure_text(), main_keyboard())
    elif value in ("/orbinum", "🛰 Orbinum"):
        send_message(orbinum_text(), main_keyboard())
    elif value in ("/rialo", "⚡ Rialo"):
        send_message(rialo_text(), main_keyboard())
    elif value in ("/vps", "🖥 VPS"):
        send_message(vps_text(), main_keyboard())
    elif value in ("/canopy", "🌿 Canopy"):
        send_message(canopy_text(), main_keyboard())
    elif value in ("/alerts", "🚨 Alerts"):
        send_message(alerts_text(), main_keyboard())
    elif value in ("/dashboard", "📊 Dashboard"):
        send_dashboard()
    else:
        send_menu("Choose a monitoring view:")


def poll_updates() -> None:
    global update_offset
    try:
        result = telegram(
            "getUpdates",
            {"offset": update_offset, "timeout": 5},
            timeout=10,
        )
        for update in result.get("result", []):
            update_offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            text = message.get("text", "")
            chat_id = message.get("chat", {}).get("id")
            if text:
                handle_message(text, chat_id)
    except Exception as exc:
        print("Telegram poll error:", exc, flush=True)


def main() -> None:
    send_menu("🟢 Infrastructure Monitor started")
    last_check = 0.0

    while True:
        poll_updates()
        now = time.time()
        if now - last_check >= CHECK_INTERVAL:
            check_state()
            last_check = now
        time.sleep(1)


if __name__ == "__main__":
    main()
