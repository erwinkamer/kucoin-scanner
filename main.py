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

granularity = 60  # 1H in minuten

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
        all_contracts = res.get('data', [])
        filtered = [c['symbol'] for c in all_contracts if c.get('quoteCurrency') == 'USDT' and not c.get('isInverse')]
        send_telegram_message(f"📱 KuCoin: {len(filtered)} perpetual futures gevonden")
        return filtered
    except Exception as e:
        send_telegram_message(f"❌ Fout bij ophalen contracten: {e}")
        return []

def get_ohlcv(symbol, limit=SIGNAL_LOOKBACK):
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - (limit * 2 * 3600 * 1000)

    url = f"{API_BASE}/api/v1/kline/query"
    params = {
        "symbol": symbol,
        "granularity": granularity,
        "from": start_ts,
        "to": end_ts
    }

    try:
        print(f"[DEBUG] {symbol} → from={start_ts}, to={end_ts}, granularity={granularity}")
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get("code") != "200000":
            print(f"⚠️ KuCoin API Error {symbol}: {res.get('msg')}")
            return None

        data = res.get("data")
        if not data:
            return None

        df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','value'])
        df = df.astype(float).sort_values('ts').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Fout bij OHLCV {symbol}: {e}")
        return None

def check_signals(df):
    if len(df) < 30:
        return None

    df['EMA_9'] = ta.trend.ema_indicator(df['close'], window=9)
    df['EMA_21'] = ta.trend.ema_indicator(df['close'], window=21)
    df['RSI_14'] = ta.momentum.rsi(df['close'], window=14)
    df['ADX_14'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()

    last = df.iloc[-1]
    if pd.isna(last['ADX_14']) or pd.isna(last['RSI_14']):
        return None

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

def scan_and_notify():
    global actieve_signalen
    contracts = get_contracts()
    print(f"Aantal contracts opgehaald: {len(contracts)}")
    nieuwe_signalen = {}
    symbols_with_data = 0

    for sym in contracts:
        print(f"Scannen: {sym}")
        df = get_ohlcv(sym)
        time.sleep(0.1)
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
    actieve_signalen = nieuwe_signalen

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
