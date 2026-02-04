# KuCoin Perpetual Scanner (1H) - Telegram-gestuurde Signal Detector

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

API_BASE = "https://api-futures.kucoin.com"
HEADERS = {"Content-Type": "application/json"}
SIGNAL_LOOKBACK = 50

app = Flask(__name__)
actieve_signalen = {}

def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Fout bij versturen bericht: {e}")

def get_contracts():
    url = f"{API_BASE}/api/v1/contracts/active"
    try:
        res = requests.get(url, headers=HEADERS).json()
        contracts = [
            c['symbol'] for c in res.get('data', [])
            if c.get('enableTrading') and not c.get('isInverse')
        ]
        send_telegram_message(f"📱 KuCoin: {len(contracts)} perpetual futures gevonden")
        return contracts
    except Exception as e:
        send_telegram_message(f"❌ Fout bij ophalen contracten: {e}")
        return []

def get_ohlcv(symbol, limit=SIGNAL_LOOKBACK):
    end_ts = int(time.time())
    start_ts = end_ts - limit * 3600

    url = f"{API_BASE}/api/v1/kline/query"
    params = {
        "symbol": symbol,
        "granularity": 3600,
        "from": start_ts,
        "to": end_ts
    }

    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        data = res.get("data", [])
        if len(data) < 25:
            return None  # Onvoldoende candles
        df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','value'])
        df = df.astype(float)
        return df
    except Exception as e:
        print(f"Fout bij ophalen candles voor {symbol}: {e}")
        return None

def check_signals(df):
    df['EMA_9'] = ta.trend.ema_indicator(df['close'], window=9).ema_indicator()
    df['EMA_21'] = ta.trend.ema_indicator(df['close'], window=21).ema_indicator()
    df['RSI_14'] = ta.momentum.rsi(df['close'], window=14)
    adx = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    df['ADX_14'] = adx['ADX_14']

    last = df.iloc[-1]
    if last['ADX_14'] > 25:
        if last['EMA_9'] > last['EMA_21'] and last['RSI_14'] > 55:
            return ("LONG", last['ADX_14'], last['RSI_14'])
        elif last['EMA_9'] < last['EMA_21'] and last['RSI_14'] < 45:
            return ("SHORT", last['ADX_14'], last['RSI_14'])
    elif 22 <= last['ADX_14'] <= 25:
        if last['EMA_9'] > last['EMA_21'] and 50 < last['RSI_14'] <= 55:
            return ("PRE-LONG", last['ADX_14'], last['RSI_14'])
        elif last['EMA_9'] < last['EMA_21'] and 45 <= last['RSI_14'] < 50:
            return ("PRE-SHORT", last['ADX_14'], last['RSI_14'])
    return None

def scan_and_notify():
    global actieve_signalen
    contracts = get_contracts()
    nieuwe_signalen = {}
    symbols_with_data = 0

    for sym in contracts:
        df = get_ohlcv(sym)
        time.sleep(0.12)  # Respecteer API-limieten (~8 req/sec)
        if df is None:
            continue
        symbols_with_data += 1
        result = check_signals(df)
        if result:
            signaal, adx, rsi = result
            nieuwe_signalen[sym] = {
                "signaal": signaal,
                "adx": adx,
                "rsi": rsi
            }

    send_telegram_message(f"⚙️ Debug: {len(contracts)} gecheckt, {symbols_with_data} met data")

    gecombineerde_signalen = {
        **{k: v for k, v in actieve_signalen.items() if k not in nieuwe_signalen},
        **nieuwe_signalen
    }
    actieve_signalen = gecombineerde_signalen

    if actieve_signalen:
        gesorteerd = sorted(actieve_signalen.items(), key=lambda x: -x[1]['adx'])
        msg = "📊 Actieve kansen (1H):\n"
        for sym, data in gesorteerd[:5]:
            emoji = "🟢" if data["signaal"] == "LONG" else "🔴" if data["signaal"] == "SHORT" else "🟡"
            msg += f"{emoji} {data['signaal']} — {sym}\nADX: {data['adx']:.1f} | RSI: {data['rsi']:.1f}\n\n"
        send_telegram_message(msg)
    else:
        send_telegram_message("🔍 Geen huidige kansen volgens jouw strategie.")

@app.route(f"/{WEBHOOK_SECRET}", methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if 'message' in data and 'text' in data['message']:
        text = data['message']['text'].strip().lower()
        if text == "zoek":
            threading.Thread(target=scan_and_notify).start()
    return "ok"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
