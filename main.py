# KuCoin Perpetual Scanner (1H) - Telegram-gestuurde Signal Detector

import requests
import pandas as pd
import ta
from flask import Flask, request
import threading

# === CONFIGURATIE ===
import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", 8080))

API_BASE = "https://api-futures.kucoin.com"
HEADERS = {"Content-Type": "application/json"}
SIGNAL_LOOKBACK = 50

app = Flask(__name__)

# === MARKT EN INDICATORFUNCTIES ===
def get_contracts():
    url = f"{API_BASE}/api/v1/contracts/active"
    try:
        res = requests.get(url, headers=HEADERS).json()
        return [c['symbol'] for c in res['data'] if c['type'] == 'FFutures']
    except Exception as e:
        print(f"Fout bij ophalen contracten: {e}")
        return []

def get_ohlcv(symbol, limit=SIGNAL_LOOKBACK):
    url = f"{API_BASE}/api/v1/kline/query?symbol={symbol}&granularity=3600"
    try:
        res = requests.get(url, headers=HEADERS).json()
        if not res.get("data"):
            raise Exception(f"Geen data voor {symbol}")
        data = res['data'][-limit:]
        df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','value'])
        df = df.astype(float)
        df['close'] = pd.to_numeric(df['close'])
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        return df
    except Exception as e:
        print(f"Fout bij OHLCV {symbol}: {e}")
        return None

def check_signals(df):
    df['EMA_9'] = ta.trend.ema_indicator(df['close'], window=9).ema_indicator()
    df['EMA_21'] = ta.trend.ema_indicator(df['close'], window=21).ema_indicator()
    df['RSI_14'] = ta.momentum.rsi(df['close'], window=14)
    adx = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    df['ADX_14'] = adx['ADX_14']

    last = df.iloc[-1]
    ema_bullish = last['EMA_9'] > last['EMA_21']
    ema_bearish = last['EMA_9'] < last['EMA_21']
    rsi_long = last['RSI_14'] > 55
    rsi_short = last['RSI_14'] < 45
    rsi_near_long = 50 < last['RSI_14'] <= 55
    rsi_near_short = 45 <= last['RSI_14'] < 50
    adx_strong = last['ADX_14'] > 25
    adx_rising = 22 <= last['ADX_14'] <= 25

    if adx_strong:
        if ema_bullish and rsi_long:
            return ("LONG", last['ADX_14'], last['RSI_14'])
        elif ema_bearish and rsi_short:
            return ("SHORT", last['ADX_14'], last['RSI_14'])
    elif adx_rising:
        if ema_bullish and rsi_near_long:
            return ("PRE-LONG", last['ADX_14'], last['RSI_14'])
        elif ema_bearish and rsi_near_short:
            return ("PRE-SHORT", last['ADX_14'], last['RSI_14'])
    return None

def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Fout bij versturen bericht: {e}")

# === SCAN FUNCTIE ===
def scan_and_notify():
    contracts = get_contracts()
    results = []
    for sym in contracts:
        df = get_ohlcv(sym)
        if df is not None:
            result = check_signals(df)
            if result:
                signaal, adx, rsi = result
                emoji = "🟢" if signaal == "LONG" else "🔴" if signaal == "SHORT" else "🟡"
                results.append((sym, signaal, adx, rsi, emoji))

    if results:
        results.sort(key=lambda x: -x[2])
        msg = "📊 Huidige kansen (1H):\n"
        for sym, sig, adx, rsi, emoji in results[:5]:
            msg += f"{emoji} {sig} — {sym}\nADX: {adx:.1f} | RSI: {rsi:.1f}\n\n"
        send_telegram_message(msg)
    else:
        send_telegram_message("🔍 Geen huidige kansen volgens jouw strategie.")

# === TELEGRAM WEBHOOK ===
@app.route(f"/{WEBHOOK_SECRET}", methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if 'message' in data and 'text' in data['message']:
        text = data['message']['text'].strip().lower()
        if text == "zoek":
            threading.Thread(target=scan_and_notify).start()
    return "ok"

# === SERVER START ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)