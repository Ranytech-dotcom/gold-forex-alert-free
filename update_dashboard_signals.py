from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Africa/Lagos")
OUTPUT_FILE = Path("latest_signals.json")
SCAN_FILE = Path("scan_output.txt")
SIGNAL_TTL_MINUTES = 45

MARKET_ALIASES = {
    "GOLD — DERIV XAUUSD": "XAUUSD",
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
}


def load_existing() -> dict:
    if not OUTPUT_FILE.exists():
        return {"updated_at": None, "signals": {}}
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("snapshot root must be an object")
        data.setdefault("signals", {})
        return data
    except Exception:
        return {"updated_at": None, "signals": {}}


def parse_price(value: str) -> float:
    return float(value.strip())


def parse_scan(text: str, now: datetime) -> dict[str, dict]:
    found: dict[str, dict] = {}
    current_key: str | None = None
    current: dict[str, object] = {}

    def flush() -> None:
        nonlocal current_key, current
        if current_key and current.get("direction") in {"BUY", "SELL"}:
            required = ["entry_low", "entry_high", "stop", "tp1", "tp2", "tp3"]
            if all(name in current for name in required):
                entry_low = float(current["entry_low"])
                entry_high = float(current["entry_high"])
                found[current_key] = {
                    "market": current_key,
                    "direction": current["direction"],
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "entry": (entry_low + entry_high) / 2,
                    "stop": float(current["stop"]),
                    "tp1": float(current["tp1"]),
                    "tp2": float(current["tp2"]),
                    "tp3": float(current["tp3"]),
                    "reason": current.get("reason", "Qualified V4 setup"),
                    "closed_candle": current.get("closed_candle"),
                    "generated_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=SIGNAL_TTL_MINUTES)).isoformat(),
                }
        current_key = None
        current = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading = re.match(r"(.+?) SIGNAL ALERT V4$", line)
        if heading:
            flush()
            current_key = MARKET_ALIASES.get(heading.group(1).strip())
            continue

        if not current_key:
            continue

        if line.startswith("Signal:"):
            current["direction"] = line.split(":", 1)[1].strip().upper()
        elif line.startswith("Completed candle:"):
            current["closed_candle"] = line.split(":", 1)[1].strip()
        elif line.startswith("Reason:"):
            current["reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("Entry zone:"):
            match = re.search(r"Entry zone:\s*([-+0-9.]+)\s*-\s*([-+0-9.]+)", line)
            if match:
                current["entry_low"] = parse_price(match.group(1))
                current["entry_high"] = parse_price(match.group(2))
        elif line.startswith("Stop Loss:"):
            current["stop"] = parse_price(line.split(":", 1)[1])
        elif line.startswith("TP1 / TP2 / TP3:"):
            values = [part.strip() for part in line.split(":", 1)[1].split("/")]
            if len(values) == 3:
                current["tp1"], current["tp2"], current["tp3"] = map(parse_price, values)

    flush()
    return found


def is_active(signal: dict, now: datetime) -> bool:
    try:
        expiry = datetime.fromisoformat(str(signal.get("expires_at", "")))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=TIMEZONE)
        return expiry > now
    except Exception:
        return False


def main() -> None:
    now = datetime.now(TIMEZONE)
    existing = load_existing()
    signals = {
        key: value
        for key, value in existing.get("signals", {}).items()
        if isinstance(value, dict) and is_active(value, now)
    }

    if SCAN_FILE.exists():
        new_signals = parse_scan(SCAN_FILE.read_text(encoding="utf-8", errors="replace"), now)
        signals.update(new_signals)

    payload = {
        "updated_at": now.isoformat(),
        "signals": signals,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Dashboard snapshot updated: {len(signals)} active signal(s).")


if __name__ == "__main__":
    main()
