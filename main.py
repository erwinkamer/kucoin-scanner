import os
import time
import math
import threading
import requests
import pandas as pd
import ta

from flask import Flask, request
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

# Hyperliquid public info endpoint
API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"Content-Type": "application/json"}

# Strategy / scan config
SIGNAL_LOOKBACK = int(os.getenv("SIGNAL_LOOKBACK", "50"))  # candles used for signals
CANDLE_BUFFER = int(os.getenv("CANDLE_BUFFER", "20"))      # extra candles for indicator warmup
CANDLE_INTERVAL = os.getenv("CANDLE_INTERVAL", "1h")       # keep "1h" per your strategy

# Top-N selection on activity/liquidity (free, via metaAndAssetCtxs)
TOP_N = int(os.getenv("TOP_N", "120"))                     # "vrij groot"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))           # parallel candle fetch workers

# High conviction ping (optional)
HC_ADX = float(os.getenv("HC_ADX", "30"))
HC_ATR_MIN = float(os.getenv("HC_ATR_MIN", "0.8"))
HC_ATR_MAX = float(os.getenv("HC_ATR_MAX", "3.5"))
HC_RSI_LONG = float(os.getenv("HC_RSI_LONG", "60"))
HC_RSI_SHORT = float(os.getenv("HC_RSI_SHORT", "40"))

app = Flask(__name__)


# =========================
# TELEGRAM
# =========================
def send_telegram_message(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, json=payload, timeout=12)
    except Exception as e:
        print(f"Telegram send error: {e}")


# =========================
# HYPERLIQUID HELPERS
# =========================
def hl_info(payload: dict):
    r = requests.post(f"{API_BASE}/info", headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def get_topn_coins_by_activity(top_n: int) -> list:
    """
    Uses Hyperliquid info type=metaAndAssetCtxs to rank coins by:
      - dayNtlVlm (activity proxy)
      - impact spread proxy (liquidity penalty)
    Returns Top-N coin names (e.g., "BTC", "ETH", ...).
    """
    res = hl_info({"type": "metaAndAssetCtxs"})
    # Expected shape: [ { "universe":[...] }, [ ctx0, ctx1, ... ] ]
    meta = res[0] if isinstance(res, list) and len(res) >= 2 else {}
    ctxs = res[1] if isinstance(res, list) and len(res) >= 2 else []

    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    rows = []

    for i, u in enumerate(universe):
        if not isinstance(u, dict):
            continue
        name = u.get("name")
        if not name:
            continue

        c = ctxs[i] if i < len(ctxs) and isinstance(ctxs[i], dict) else {}
        day_ntl = float(c.get("dayNtlVlm", 0.0) or 0.0)
        mid = float(c.get("midPx", 0.0) or 0.0)
        impact = c.get("impactPxs") or [None, None]

        impact_spread = 1.0
        try:
            if mid > 0 and isinstance(impact, list) and len(impact) == 2 and impact[0] and impact[1]:
                buy_imp = float(impact[0])
                sell_imp = float(impact[1])
                impact_spread = abs(sell_imp - buy_imp) / mid
        except Exception:
            impact_spread = 1.0

        # Activity score: volume heavy + liquidity penalty (bounded)
        activity_score = math.log1p(day_ntl) * (1.0 / (1.0 + 20.0 * impact_spread))
        rows.append((name, activity_score, day_ntl, impact_spread))

    rows.sort(key=lambda x: x[1], reverse=True)
    top = [r[0] for r in rows[:top_n]]

    send_telegram_message(f"📈 Hyperliquid: Top-{top_n} geselecteerd op activiteit/liquideit (gratis)")
    return top


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.8, min=0.8, max=6),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def get_ohlcv_hl(coin: str, limit: int):
    """
    Fetch 1H candles via candleSnapshot and return DataFrame with:
    ['ts','open','high','low','close','vol']
    """
    end_ts = int(time.time() * 1000)
    hours = limit + CANDLE_BUFFER
    start_ts = end_ts - hours * 3600 * 1000

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": CANDLE_INTERVAL, "startTime": start_ts, "endTime": end_ts},
    }
    data = hl_info(payload)
    if not data or not isinstance(data, list):
        return None

    rows = []
    for x in data:
        if isinstance(x, dict):
            ts = x.get("t") or x.get("T") or x.get("time") or x.get("ts")
            o = x.get("o") or x.get("open")
            h = x.get("h") or x.get("high")
            l = x.get("l") or x.get("low")
            c = x.get("c") or x.get("close")
            v = x.get("v") or x.get("vol") or x.get("volume") or 0.0
            if ts is None or o is None or h is None or l is None or c is None:
                continue
            rows.append([float(ts), float(o), float(h), float(l), float(c), float(v)])
        elif isinstance(x, list) and len(x) >= 6:
            # fallback: [ts, open, high, low, close, volume]
            rows.append([float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])])

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df = df.sort_values("ts").reset_index(drop=True)

    if len(df) > limit:
        df = df.iloc[-limit:].reset_index(drop=True)

    return df


# =========================
# SIGNALS (UNCHANGED LOGIC)
# + ATR% ONLY FOR RANKING
# =========================
def check_signals(df: pd.DataFrame):
    if df is None or len(df) < 30:
        return None

    df["EMA_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["EMA_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["RSI_14"] = ta.momentum.rsi(df["close"], window=14)
    df["ADX_14"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()

    # ATR% (ranking only)
    df["ATR_14"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
    df["ATRp_14"] = (df["ATR_14"] / df["close"]) * 100.0

    last = df.iloc[-1]
    if pd.isna(last["ADX_14"]) or pd.isna(last["RSI_14"]) or pd.isna(last["EMA_9"]) or pd.isna(last["EMA_21"]):
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
            return ("LONG", float(last["ADX_14"]), float(last["RSI_14"]), float(last["ATRp_14"]))
        elif ema_bearish and rsi_short:
            return ("SHORT", float(last["ADX_14"]), float(last["RSI_14"]), float(last["ATRp_14"]))
    elif adx_rising:
        if ema_bullish and rsi_near_long:
            return ("PRE-LONG", float(last["ADX_14"]), float(last["RSI_14"]), float(last["ATRp_14"]))
        elif ema_bearish and rsi_near_short:
            return ("PRE-SHORT", float(last["ADX_14"]), float(last["RSI_14"]), float(last["ATRp_14"]))
    return None


def compute_score(df: pd.DataFrame, signal: str) -> float:
    last = df.iloc[-1]
    adx = float(last["ADX_14"])
    rsi = float(last["RSI_14"])
    close = float(last["close"])
    ema_sep = abs(float(last["EMA_9"]) - float(last["EMA_21"])) / close if close > 0 else 0.0
    atrp = float(last["ATRp_14"])

    # RSI distance from 50 in the direction of the signal
    if signal in ("LONG", "PRE-LONG"):
        rsi_dist = max(0.0, rsi - 50.0)
    else:
        rsi_dist = max(0.0, 50.0 - rsi)

    # ATR sweet spot (day trading)
    atr_bonus = 0.0
    if 0.8 <= atrp <= 3.5:
        atr_bonus = 1.0
    elif atrp < 0.5:
        atr_bonus = -1.0
    elif atrp > 6.0:
        atr_bonus = -0.5

    pre_penalty = -2.0 if "PRE" in signal else 0.0

    # score scaling: ema_sep is tiny -> scale up
    score = (adx * 1.0) + (rsi_dist * 0.6) + (ema_sep * 500.0) + (atr_bonus * 3.0) + pre_penalty
    return float(score)


def is_high_conviction(signaal: str, adx: float, rsi: float, atrp: float) -> bool:
    if adx < HC_ADX:
        return False
    if not (HC_ATR_MIN <= atrp <= HC_ATR_MAX):
        return False
    if signaal == "LONG" and rsi >= HC_RSI_LONG:
        return True
    if signaal == "SHORT" and rsi <= HC_RSI_SHORT:
        return True
    return False


# =========================
# SCAN AND NOTIFY
# =========================
def scan_and_notify():
    t0 = time.time()

    # 1) Select Top-N coins (free, 1 call)
    try:
        coins = get_topn_coins_by_activity(TOP_N)
    except Exception as e:
        send_telegram_message(f"❌ Fout bij Top-N selectie (metaAndAssetCtxs): {e}")
        return

    # 2) Fetch candles in parallel and compute signals
    results = []
    count_no_data = 0
    count_no_signal = 0
    count_with_signal = 0

    def worker(coin: str):
        df = get_ohlcv_hl(coin, limit=SIGNAL_LOOKBACK)
        if df is None or df.empty:
            return ("NO_DATA", coin, None)
        sig = check_signals(df)
        if not sig:
            return ("NO_SIGNAL", coin, None)
        signaal, adx, rsi, atrp = sig
        score = compute_score(df, signaal)
        return ("SIGNAL", coin, (signaal, adx, rsi, atrp, score))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(worker, c) for c in coins]
        for f in as_completed(futs):
            status, coin, payload = f.result()
            if status == "NO_DATA":
                count_no_data += 1
            elif status == "NO_SIGNAL":
                count_no_signal += 1
            else:
                count_with_signal += 1
                signaal, adx, rsi, atrp, score = payload
                results.append((coin, signaal, adx, rsi, atrp, score))

    dt = time.time() - t0
    send_telegram_message(
        f"⚙️ Debug: Top-{len(coins)} gescand | signals={count_with_signal} | no_signal={count_no_signal} | no_data={count_no_data} | {dt:.1f}s"
    )

    # 3) Always show Top-5 (A)
    if not results:
        send_telegram_message("🔍 Geen huidige kansen volgens jouw strategie (Top-N scan).")
        return

    results.sort(key=lambda x: x[5], reverse=True)

    msg = "📊 Beste kansen (1H) — Hyperliquid\n"
    for coin, signaal, adx, rsi, atrp, score in results[:5]:
        emoji = "🟢" if signaal == "LONG" else "🔴" if signaal == "SHORT" else "🟡"
        msg += (
            f"{emoji} {signaal} — {coin}\n"
            f"ADX: {adx:.1f} | RSI: {rsi:.1f} | ATR%: {atrp:.2f} | Score: {score:.1f}\n\n"
        )
    send_telegram_message(msg)

    # 4) Optional: High conviction ping (free)
    best = results[0]
    coin, signaal, adx, rsi, atrp, score = best
    if is_high_conviction(signaal, adx, rsi, atrp):
        send_telegram_message(
            f"🔥 HIGH CONVICTION: {signaal} — {coin} | ADX {adx:.1f} | RSI {rsi:.1f} | ATR% {atrp:.2f}"
        )


# =========================
# TELEGRAM WEBHOOK
# =========================
@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()

    if text == "zoek":
        threading.Thread(target=scan_and_notify, daemon=True).start()
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
