import requests
import pandas as pd
import ta
from flask import Flask, request
import threading
import os
import time
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", 8080))

# Hyperliquid public info endpoint
API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"Content-Type": "application/json"}

SIGNAL_LOOKBACK = 50
INTERVAL = "1h"  # Hyperliquid supported intervals include "1h" :contentReference[oaicite:4]{index=4}

app = Flask(__name__)
actieve_signalen = {}

def send_telegram_message(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Fout bij versturen bericht: {e}")

def hl_info(payload: dict):
    """
    Single helper for Hyperliquid info endpoint:
    POST https://api.hyperliquid.xyz/info :contentReference[oaicite:5]{index=5}
    """
    url = f"{API_BASE}/info"
    return requests.post(url, headers=HEADERS, json=payload, timeout=15)

def get_contracts():
    """
    Hyperliquid perps universe via type=meta. :contentReference[oaicite:6]{index=6}
    Returns list of coin names (e.g., "BTC", "ETH", ...).
    """
    try:
        res = hl_info({"type": "meta"}).json()
        universe = res.get("universe", [])
        coins = [u.get("name") for u in universe if u.get("name")]

        # Optioneel: filter out weird remaps/empty (defensief)
        coins = [c for c in coins if isinstance(c, str) and len(c) > 0]

        send_telegram_message(f"📱 Hyperliquid: {len(coins)} perpetuals gevonden")
        return coins
    except Exception as e:
        send_telegram_message(f"❌ Fout bij ophalen Hyperliquid universe: {e}")
        return []

def get_ohlcv(coin: str, limit: int = SIGNAL_LOOKBACK):
    """
    candleSnapshot returns candles with fields: t,o,h,l,c,v,... :contentReference[oaicite:7]{index=7}
    We request a time window wide enough to reliably get >= limit candles.
    """
    end_ts = int(time.time() * 1000)
    # 1h candles: request ~ (limit * 2) hours back as buffer
    start_ts = end_ts - (limit * 2 * 3600 * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": INTERVAL,
            "startTime": start_ts,
            "endTime": end_ts
        }
    }

    try:
        r = hl_info(payload)

        # eenvoudige backoff op rate-limit / transient errors
        if r.status_code in (429, 503):
            time.sleep(1.5)
            r = hl_info(payload)

        data = r.json()
        if not isinstance(data, list) or len(data) == 0:
            return None

        # Hyperliquid keys: t (open time), o,h,l,c,v as strings
        df = pd.DataFrame(data)
        # Normaliseer naar jouw bestaande kolomnamen
        df = df.rename(columns={
            "t": "ts",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "vol"
        })

        # Houd alleen relevante kolommen (defensief)
        keep = ["ts", "open", "high", "low", "close", "vol"]
        df = df[[c for c in keep if c in df.columns]].copy()

        # types
        df["ts"] = df["ts"].astype(float)
        for c in ["open", "high", "low", "close", "vol"]:
            if c in df.columns:
                df[c] = df[c].astype(float)

        df = df.sort_values("ts").reset_index(drop=True)

        # Pak laatste `limit` candles (Hyperliquid kan meer teruggeven)
        if len(df) > limit:
            df = df.iloc[-limit:].reset_index(drop=True)

        return df
    except Exception as e:
        print(f"Fout bij OHLCV {coin}: {e}")
        return None

def check_signals(df):
    if df is None or len(df) < 30:
        return None

    df["EMA_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["EMA_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["RSI_14"] = ta.momentum.rsi(df["close"], window=14)
    df["ADX_14"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()

    last = df.iloc[-1]
    if pd.isna(last["ADX_14"]) or pd.isna(last["RSI_14"]):
        return None

    ema_bullish = last["EMA_9"] > last["EMA_21"]
    ema_bearish = last["EMA_9"] < last["EMA_21"]
    rsi_long = last["RSI_14"] > 55
    rsi_short = last["RSI_14"] < 45
    rsi_near_long = 50 < last["RSI_14"] <= 55
    rsi_near_short = 45 <= last["RSI_14"] < 50
    adx_strong = last["ADX_14"] > 25
    adx_rising = 22 <= last["ADX_14"] <= 25

    if adx_strong:
        if ema_bullish and rsi_long:
            return ("LONG", float(last["ADX_14"]), float(last["RSI_14"]))
        elif ema_bearish and rsi_short:
            return ("SHORT", float(last["ADX_14"]), float(last["RSI_14"]))
    elif adx_rising:
        if ema_bullish and rsi_near_long:
            return ("PRE-LONG", float(last["ADX_14"]), float(last["RSI_14"]))
        elif ema_bearish and rsi_near_short:
            return ("PRE-SHORT", float(last["ADX_14"]), float(last["RSI_14"]))
    return None

def scan_and_notify():
    global actieve_signalen
    contracts = get_contracts()
    print(f"Aantal coins opgehaald: {len(contracts)}")

    nieuwe_signalen = {}
    symbols_with_data = 0

    for coin in contracts:
        print(f"Scannen: {coin}")
        df = get_ohlcv(coin)
        # Hyperliquid rate limits: hou dit conservatief
        time.sleep(0.15)

        if df is None:
            continue

        symbols_with_data += 1
        result = check_signals(df)

        if result:
            signaal, adx, rsi = result
            nieuwe_signalen[coin] = {"signaal": signaal, "adx": adx, "rsi": rsi}

    send_telegram_message(f"⚙️ Debug: {len(contracts)} gecheckt, {symbols_with_data} met data")
    actieve_signalen = nieuwe_signalen

    if actieve_signalen:
        gesorteerd = sorted(actieve_signalen.items(), key=lambda x: -x[1]["adx"])
        msg = "📊 Actieve kansen (1H) — Hyperliquid:\n"
        for coin, data in gesorteerd[:5]:
            emoji = "🟢" if data["signaal"] == "LONG" else "🔴" if data["signaal"] == "SHORT" else "🟡"
            msg += f"{emoji} {data['signaal']} — {coin}\nADX: {data['adx']:.1f} | RSI: {data['rsi']:.1f}\n\n"
        send_telegram_message(msg)
    else:
        send_telegram_message("🔍 Geen huidige kansen volgens jouw strategie.")

@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if data and "message" in data and "text" in data["message"]:
        text = data["message"]["text"].strip().lower()
        if text == "zoek":
            threading.Thread(target=scan_and_notify).start()
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
