#!/usr/bin/env python3
"""Regression checks for the read-only Linux ops bot."""

import importlib.util
import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    env_file = Path(tmp) / ".env"
    env_file.write_text(
        "BOT_TOKEN=test-token\n"
        "CHAT_ID=123\n"
        "ORBINUM_VALIDATOR_ACCOUNT=5FakeValidatorAccount\n"
        f"ORBINUM_LIFECYCLE_STATE_FILE={tmp}/lifecycle.json\n",
        encoding="utf-8",
    )
    os.environ["OPS_BOT_ENV_FILE"] = str(env_file)
    os.environ.pop("ORBINUM_VALIDATOR_ACCOUNT", None)

    path = Path(__file__).resolve().parents[1] / "agent" / "linux_ops_bot.py"
    spec = importlib.util.spec_from_file_location("linux_ops_bot", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.VALIDATOR_ACCOUNT == "5FakeValidatorAccount"
    assert module.LIFECYCLE_STATE_FILE == Path(tmp) / "lifecycle.json"

    keyboard = module.main_keyboard()
    rows = keyboard["keyboard"]
    assert rows[0][0]["text"] == "🟢 Infrastructure"
    assert rows[1][0]["text"] == "🛰 Orbinum"
    assert rows[1][1]["text"] == "⚡ Rialo"
    assert rows[2][0]["text"] == "🖥 VPS"
    assert rows[2][1]["text"] == "🌿 Canopy"
    assert rows[3][0]["text"] == "🚨 Alerts"
    assert rows[3][1]["text"] == "📊 Dashboard"

    # No remote-control actions are allowed in the menu.
    labels = " ".join(button["text"] for row in rows for button in row)
    assert "Restart" not in labels
    assert "Stop" not in labels
    assert "Start" not in labels

    assert module.block_number({"number": "0x10"}) == 16
    assert module.block_number({"number": "bad"}) == 2989  # valid hexadecimal
    assert module.block_number({}) is None
    assert module.human_uptime(90061) == "1d 1h"

    fake = {
        "home": {
            "cpu": 11.2,
            "memory_used": 6 * 1024**3,
            "memory_total": 32 * 1024**3,
            "disk_percent": 38.0,
            "uptime": 3 * 86400 + 14 * 3600,
        },
        "orbinum": {
            "online": True,
            "peers": 19,
            "syncing": False,
            "best": 941203,
            "finalized": 941190,
            "tunnel_service": "active",
            "public_p2p": True,
        },
        "rialo": {
            "states": {name: "active" for name in module.RIALO_SERVICES},
            "balance": 1.42,
            "overall": "ok",
        },
        "vps": {"https": True, "http": True, "watcher": True, "p2p": True},
        "canopy": {"port_9001": True},
        "problems": [],
        "overall": "ok",
    }
    text = module.infrastructure_text(fake)
    assert "🟢 Infrastructure OK" in text
    assert "Peers: 19" in text
    assert "RLO: 1.420 RLO" in text
    assert "Port :9001: OK" in text

print("Linux ops bot replay passed")
