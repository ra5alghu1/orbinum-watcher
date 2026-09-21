#!/usr/bin/env python3
"""Small regression checks for the Telegram button UI."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_PAIR_CODE", "test-pair")

import bot


def main():
    keyboard = bot.main_keyboard()
    rows = keyboard["keyboard"]

    assert keyboard["resize_keyboard"] is True
    assert keyboard["is_persistent"] is True
    assert rows[0][0]["text"] == "🟢 Status"
    assert rows[0][1]["text"] == "📊 Uptime"
    assert rows[1][0]["text"] == "🚨 Incidents"
    assert rows[1][1]["text"] == "🧭 Diagnostics"
    assert rows[2][0]["text"] == "🌐 Dashboard"
    assert bot.DASHBOARD_URL.startswith("https://")

    print("Telegram button UI replay passed")


if __name__ == "__main__":
    main()
