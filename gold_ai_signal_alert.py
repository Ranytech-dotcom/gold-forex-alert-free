"""
MULTI-MARKET SIGNAL ALERT V4

Educational / paper-testing system only. It never places a live order.
Gold and all three forex pairs use Deriv's public market-data feed.
Every price must still be checked against the live Deriv MT5 quote and spread.
"""

from __future__ import annotations

import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from email.message import EmailMessage

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import websocket
from sklearn.ensemble import RandomForestClassifier


TIMEZONE = "Africa/Lagos"
DERIV_WS_URL = os.getenv(
    "DERIV_WS_URL",
    "wss://api.derivws.com/trading/v1/options/ws/public",
)
DERIV_GRANULARITY_SECONDS = 15 * 60
DERIV_CANDLE_COUNT = 5000

ASSETS = [
    {
        "name": "GOLD — DERIV XAUUSD",
        "ticker": os.getenv("DERIV_GOLD_SYMBOL", "frxXAUUSD"),
        "source": "deriv",
        "source_label": "Deriv XAUUSD (frxXAUUSD)",
        "decimals": 2,
        "min_atr_pct": 0.0007,
        "max_atr_pct": 0.0120,
    },
    {
        "name": "EUR/USD",
        "ticker": "frxEURUSD",
        "source": "deriv",
        "source_label": "Deriv EUR/USD (frxEURUSD)",
        "decimals": 5,
        "min_atr_pct": 0.00015,
        "max_atr_pct": 0.0040,
    },
    {
        "name": "GBP/USD",
        "ticker": "frxGBPUSD",
        "source": "deriv",
        "source_label": "Deriv GBP/USD (frxGBPUSD)",
        "decimals": 5,
        "min_atr_pct": 0.00015,
        "max_atr_pct": 0.0045,
    },
    {
        "name": "USD/JPY",
        "ticker": "frxUSDJPY",
        "source": "deriv",
        "source_label": "Deriv USD/JPY (frxUSDJPY)",
        "decimals": 3,
        "min_atr_pct": 0.00015,
        "max_atr_pct": 0.0040,
    },
]

BUY_THRESHOLD = 0.60
SELL_THRESHOLD = 0.40
MIN_CONFLUENCE = 6
MIN_ADX = 18.0
MAX_STOP_ATR = 2.30
ENTRY_ZONE_ATR = 0.10
STOP_BUFFER_ATR = 0.15
MIN_STOP_ATR = 1.25
SIGNAL_EXPIRY_MINUTES = 45
MAX_DATA_AGE_MINUTES = 90
RANDOM_STATE = 42


@dataclass
class TradePlan:
    signal: str
    probability_up: float
    score: int
    entry: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    risk_distance: float | None = None
    reason: str = ""


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def directional_index(df: pd.DataFrame, period: int = 14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )
    current_atr = atr(df, period).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / current_atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / current_atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, plus_di, minus_di


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["ret_1"] = x["close"].pct_change(1)
    x["ret_3"] = x["close"].pct_change(3)
    x["ret_6"] = x["close"].pct_change(6)
    x["ret_12"] = x["close"].pct_change(12)

    x["ema_9"] = ema(x["close"], 9)
    x["ema_21"] = ema(x["close"], 21)
    x["ema_50"] = ema(x["close"], 50)
    x["ema_84"] = ema(x["close"], 84)
    x["ema_200"] = ema(x["close"], 200)
    x["ema9_21_gap"] = (x["ema_9"] - x["ema_21"]) / x["close"]
    x["ema21_50_gap"] = (x["ema_21"] - x["ema_50"]) / x["close"]
    x["ema50_200_gap"] = (x["ema_50"] - x["ema_200"]) / x["close"]

    x["rsi_14"] = rsi(x["close"], 14)
    x["atr_14"] = atr(x, 14)
    x["atr_pct"] = x["atr_14"] / x["close"]
    x["adx_14"], x["plus_di"], x["minus_di"] = directional_index(x, 14)

    macd = ema(x["close"], 12) - ema(x["close"], 26)
    x["macd_hist"] = macd - ema(macd, 9)

    x["range_pct"] = (x["high"] - x["low"]) / x["close"]
    x["body_pct"] = (x["close"] - x["open"]) / x["close"]
    x["volatility_12"] = x["ret_1"].rolling(12).std()
    x["volatility_48"] = x["ret_1"].rolling(48).std()

    x["high_20"] = x["high"].rolling(20).max()
    x["low_20"] = x["low"].rolling(20).min()
    band = (x["high_20"] - x["low_20"]).replace(0, np.nan)
    x["position_20"] = (x["close"] - x["low_20"]) / band
    x["target"] = (x["close"].shift(-1) > x["close"]).astype(int)
    return x


FEATURES = [
    "ret_1", "ret_3", "ret_6", "ret_12",
    "ema9_21_gap", "ema21_50_gap", "ema50_200_gap",
    "rsi_14", "atr_pct", "adx_14", "plus_di", "minus_di", "macd_hist",
    "range_pct", "body_pct", "volatility_12", "volatility_48", "position_20",
]


def make_model() -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=240,
        max_depth=6,
        min_samples_leaf=18,
        max_features="sqrt",
        class_weight="balanced_subsample",
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )


def download_deriv_market(symbol: str) -> pd.DataFrame:
    """Download completed 15-minute XAUUSD candles from Deriv's public feed."""
    request = {
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": DERIV_CANDLE_COUNT,
        "end": "latest",
        "start": 1,
        "style": "candles",
        "granularity": DERIV_GRANULARITY_SECONDS,
    }

    connection = websocket.create_connection(DERIV_WS_URL, timeout=30)
    try:
        connection.send(json.dumps(request))
        response = json.loads(connection.recv())
    finally:
        connection.close()

    if response.get("error"):
        error = response["error"]
        raise RuntimeError(
            f"Deriv API error {error.get('code', 'unknown')}: "
            f"{error.get('message', 'No message')}"
        )

    candles = response.get("candles") or []
    if not candles:
        raise RuntimeError(f"No Deriv candles returned for {symbol}.")

    raw = pd.DataFrame(candles)
    required = ["epoch", "open", "high", "low", "close"]
    missing = [column for column in required if column not in raw.columns]
    if missing:
        raise RuntimeError(f"Deriv response is missing: {', '.join(missing)}")

    raw = raw.rename(columns={"epoch": "timestamp"})
    raw = raw[["timestamp", "open", "high", "low", "close"]].copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], unit="s", utc=True)
    for column in ["open", "high", "low", "close"]:
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    raw = raw.dropna().sort_values("timestamp").drop_duplicates("timestamp")

    # Keep only completed candles. Deriv may include the currently forming candle.
    current_bucket_epoch = (
        int(time.time()) // DERIV_GRANULARITY_SECONDS
    ) * DERIV_GRANULARITY_SECONDS
    current_bucket = pd.to_datetime(current_bucket_epoch, unit="s", utc=True)
    raw = raw[raw["timestamp"] < current_bucket].reset_index(drop=True)
    if raw.empty:
        raise RuntimeError(f"No completed Deriv candles returned for {symbol}.")
    return raw


def download_market(asset: dict) -> pd.DataFrame:
    return download_deriv_market(asset["ticker"])


def telegram_send(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured. Signal calculated, but no phone alert sent.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    try:
        with urllib.request.urlopen(url, data=payload, timeout=20) as response:
            response.read()
        print("Telegram alert sent.")
        return True
    except Exception as exc:
        print("Telegram send failed:", exc)
        return False


def email_send(subject: str, message: str) -> bool:
    """Send an alert to the configured Gmail inbox without exposing credentials."""
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_app_password = "".join(
        os.getenv("GMAIL_APP_PASSWORD", "").split()
    )
    if not gmail_user or not gmail_app_password:
        print("Gmail not configured. Signal calculated, but no email alert sent.")
        return False

    email = EmailMessage()
    email["From"] = gmail_user
    email["To"] = gmail_user
    email["Subject"] = subject
    email.set_content(message)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(gmail_user, gmail_app_password)
            smtp.send_message(email)
        print("Gmail alert sent.")
        return True
    except Exception as exc:
        print("Gmail send failed:", exc)
        return False


def build_trade_plan(
    full: pd.DataFrame,
    probability_up: float,
    min_atr_pct: float = 0.0007,
    max_atr_pct: float = 0.0120,
) -> TradePlan:
    latest = full.iloc[-1]
    price = float(latest["close"])
    current_atr = float(latest["atr_14"])
    atr_pct = float(latest["atr_pct"])

    if not min_atr_pct <= atr_pct <= max_atr_pct:
        return TradePlan("WAIT", probability_up, 0, reason="Volatility is outside the safe gate")

    oversized_candle = abs(float(latest["close"] - latest["open"])) > 1.20 * current_atr
    if oversized_candle:
        return TradePlan("WAIT", probability_up, 0, reason="Latest candle is stretched; no chasing")

    buy_checks = [
        latest["ema_9"] > latest["ema_21"],
        latest["ema_21"] > latest["ema_50"],
        latest["ema_50"] > latest["ema_200"],
        latest["close"] > latest["ema_21"],
        52 <= latest["rsi_14"] <= 72,
        latest["macd_hist"] > 0,
        latest["adx_14"] >= MIN_ADX and latest["plus_di"] > latest["minus_di"],
        latest["ret_3"] > 0,
        0.45 <= latest["position_20"] <= 0.92,
    ]
    sell_checks = [
        latest["ema_9"] < latest["ema_21"],
        latest["ema_21"] < latest["ema_50"],
        latest["ema_50"] < latest["ema_200"],
        latest["close"] < latest["ema_21"],
        28 <= latest["rsi_14"] <= 48,
        latest["macd_hist"] < 0,
        latest["adx_14"] >= MIN_ADX and latest["minus_di"] > latest["plus_di"],
        latest["ret_3"] < 0,
        0.08 <= latest["position_20"] <= 0.55,
    ]
    buy_score = int(sum(bool(item) for item in buy_checks))
    sell_score = int(sum(bool(item) for item in sell_checks))
    hard_buy = all([buy_checks[1], buy_checks[2], buy_checks[4], buy_checks[6]])
    hard_sell = all([sell_checks[1], sell_checks[2], sell_checks[4], sell_checks[6]])

    if probability_up >= BUY_THRESHOLD and buy_score >= MIN_CONFLUENCE and hard_buy:
        direction, score = "BUY", buy_score
    elif probability_up <= SELL_THRESHOLD and sell_score >= MIN_CONFLUENCE and hard_sell:
        direction, score = "SELL", sell_score
    else:
        reason = f"No full alignment (BUY {buy_score}/9, SELL {sell_score}/9, model up {probability_up * 100:.1f}%)"
        return TradePlan("WAIT", probability_up, max(buy_score, sell_score), reason=reason)

    recent = full.iloc[-12:]
    if direction == "BUY":
        swing_stop = float(recent["low"].min() - STOP_BUFFER_ATR * current_atr)
        atr_stop = price - MIN_STOP_ATR * current_atr
        stop = min(swing_stop, atr_stop)
        risk = price - stop
        entry_low = price - ENTRY_ZONE_ATR * current_atr
        entry_high = price + ENTRY_ZONE_ATR * current_atr
        tp1, tp2, tp3 = price + risk, price + 2 * risk, price + 3 * risk
    else:
        swing_stop = float(recent["high"].max() + STOP_BUFFER_ATR * current_atr)
        atr_stop = price + MIN_STOP_ATR * current_atr
        stop = max(swing_stop, atr_stop)
        risk = stop - price
        entry_low = price - ENTRY_ZONE_ATR * current_atr
        entry_high = price + ENTRY_ZONE_ATR * current_atr
        tp1, tp2, tp3 = price - risk, price - 2 * risk, price - 3 * risk

    if risk > MAX_STOP_ATR * current_atr:
        return TradePlan("WAIT", probability_up, score, reason="Required structural stop is too wide")

    return TradePlan(
        signal=direction,
        probability_up=probability_up,
        score=score,
        entry=price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        risk_distance=risk,
        reason="Model, trend, strength, momentum and volatility aligned",
    )


def analyze_asset(asset: dict, now_utc: pd.Timestamp, now_ng: pd.Timestamp) -> None:
    name = asset["name"]
    decimals = asset["decimals"]
    raw = download_market(asset)
    full = build_features(raw).dropna(subset=FEATURES + ["atr_14"]).reset_index(drop=True)
    if len(full) < 500:
        raise RuntimeError(f"Not enough usable {name} candles.")

    train_live = full.iloc[:-1].tail(2500).copy()
    latest = full.iloc[-1]
    model = make_model()
    model.fit(train_live[FEATURES], train_live["target"])
    probability_up = float(model.predict_proba(full.iloc[[-1]][FEATURES])[0, 1])
    plan = build_trade_plan(
        full,
        probability_up,
        min_atr_pct=asset["min_atr_pct"],
        max_atr_pct=asset["max_atr_pct"],
    )

    signal_time_utc = pd.Timestamp(latest["timestamp"])
    signal_time_ng = signal_time_utc.tz_convert(TIMEZONE)
    data_age_minutes = (now_utc - signal_time_utc).total_seconds() / 60
    if data_age_minutes > MAX_DATA_AGE_MINUTES:
        print(f"{name}: WAIT — data is stale or the market is closed.")
        print("No Telegram or email trade alert sent.")
        return

    expiry_ng = now_ng + pd.Timedelta(minutes=SIGNAL_EXPIRY_MINUTES)

    print("=" * 54)
    print(f"{name} SIGNAL ALERT V4")
    print("=" * 54)
    print("Signal:", plan.signal)
    print("Completed candle:", signal_time_ng.strftime("%Y-%m-%d %I:%M %p WAT"))
    print("Reason:", plan.reason)

    if plan.signal == "WAIT":
        print("No Telegram trade alert sent because the setup is WAIT.")
        return

    number = lambda value: f"{value:.{decimals}f}"
    print(f"Entry zone: {number(plan.entry_low)} - {number(plan.entry_high)}")
    print(f"Stop Loss: {number(plan.stop)}")
    print(f"TP1 / TP2 / TP3: {number(plan.tp1)} / {number(plan.tp2)} / {number(plan.tp3)}")

    message = (
        f"{name} SIGNAL ACTIVE - V4\n\n"
        f"Direction: {plan.signal}\n"
        f"Price source: {asset['source_label']}\n"
        f"Entry zone: {number(plan.entry_low)} - {number(plan.entry_high)}\n"
        f"Reference entry: {number(plan.entry)}\n"
        f"Stop Loss: {number(plan.stop)}\n"
        f"TP1 (1R): {number(plan.tp1)}\n"
        f"TP2 (2R): {number(plan.tp2)}\n"
        f"TP3 (3R): {number(plan.tp3)}\n"
        f"Risk distance: {number(plan.risk_distance)}\n"
        f"Confluence: {plan.score}/9\n"
        f"Model up reading: {plan.probability_up * 100:.1f}%\n"
        f"Closed candle: {signal_time_ng.strftime('%Y-%m-%d %I:%M %p WAT')}\n"
        f"Entry expires: {expiry_ng.strftime('%I:%M %p WAT')}\n\n"
        "Management: At TP1, take partial profit and move SL to entry. "
        "At TP2, secure at least +1R. TP3 is the final target.\n\n"
        "Do not enter outside the zone or after expiry. All levels use Deriv "
        "public market data; verify the live Deriv MT5 quote and spread before "
        "entry. Manual demo/paper-testing only."
    )
    telegram_send(message)
    email_send(f"{name} {plan.signal} signal - V4", message)


def main() -> None:
    now_utc = pd.Timestamp.now(tz="UTC")
    now_ng = now_utc.tz_convert(TIMEZONE)
    if not 7 <= now_ng.hour < 22:
        print("ALL MARKETS: WAIT")
        print("Reason: Outside the configured 7:00 AM-10:00 PM WAT session gate.")
        return

    print("Scanning Deriv XAUUSD, EUR/USD, GBP/USD and USD/JPY...")
    for asset in ASSETS:
        try:
            analyze_asset(asset, now_utc, now_ng)
        except Exception as exc:
            print(f"{asset['name']}: scan failed — {exc}")


if __name__ == "__main__":
    main()
