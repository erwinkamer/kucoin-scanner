# main.py — Hyperliquid 1H scanner -> Telegram shortlist -> TradingView (manual trading)
# Improvements:
# - Rate-limit safe (HL: 1200 weight/min; most info endpoints weight 20; candleSnapshot extra per 60 items)  :contentReference[oaicite:2]{index=2}
# - TTL caching for meta/universe
# - Liquidity/impact filtering (dayNtlVlm + impact spread proxy)
# - Optional CHOP filter (skip entries in chop)
# - Clear PRE vs ENTRY messaging
# - Score only used for ranking (does not change signal logic)

import os
import time
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
import ta

from flask import Flask, request
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"Content-Type": "application/json"}

# Strategy timeframe
INTERVAL = os.getenv("CANDLE_INTERVAL", "1h")  # supported intervals include "1h" :contentReference[oaicite:3]{index=3}

# Candles needed:
# - Strategy uses EMA(9/21), RSI(14), ADX(14) -> needs warmup; fetch ~70 bars is enough.
LOOKBACK = int(os.getenv("SIGNAL_LOOKBACK", "70"))  # total candles requested from HL
# IMPORTANT: candleSnapshot returns by time range; we'll request a range sized ~LOOKBACK candles.

# Universe selection
TOP_N = int(os.getenv("TOP_N", "60"))  # IMPORTANT: 60 is the safe default under 1200 weight/min :contentReference[oaicite:4]{index=4}
MAX_RESULTS_IN_TELEGRAM = int(os.getenv("TOP_K_TELEGRAM", "10"))

# Filters (free, based on metaAndAssetCtxs)
MIN_DAY_NTL_VLM = float(os.getenv("MIN_DAY_NTL_VLM", "1000000"))  # $1m notional/day default
MAX_IMPACT_SPREAD_PCT = float(os.getenv("MAX_IMPACT_SPREAD_PCT", "0.80"))  # % (proxy slippage)
FILTER_CHOP_ENTRIES = os.getenv("FILTER_CHOP_ENTRIES", "1").strip() == "1"  # skip ENTRY trades in chop (recommended)

# ADX regime thresholds (must match TradingView)
ADX_TREND_THR = float(os.getenv("ADX_TREND_THR", "25"))
ADX_CHOP_THR = float(os.getenv("ADX_CHOP_THR", "20"))

# PRE thresholds (match your original logic)
ADX_PRE_MIN = float(os.getenv("ADX_PRE_MIN", "22"))
ADX_PRE_MAX = float(os.getenv("ADX_PRE_MAX", "25"))

# Simple safety against spam / rate-limit
MIN_SECONDS_BETWEEN_SCANS = int(os.getenv("MIN_SECONDS_BETWEEN_SCANS", "30"))

# Caching
META_TTL_SEC = int(os.getenv("META_TTL_SEC", "60"))

# TradingView link (no mapping; generic CRYPTO feed)
TV_PREFIX = os.getenv("TV_PREFIX", "https://www.tradingview.com/chart/?symbol=CRYPTO:")

# =========================
# HL RATE LIMIT (weight-based)
# =========================
# HL docs: REST requests share aggregated weight limit of 1200 per minute/IP.
# Most info endpoints weight 20; candleSnapshot has extra per 60 items. :contentReference[oaicite:5]{index=5}
@dataclass
class RateLimiter:
    max_weight_per_min: int = 1100  # keep headroom below 1200
    window_sec: int = 60

    def __post_init__(self):
        self._lock = threading.Lock()
        self._events = deque()  # (ts, weight)

    def _prune(self, now: float):
        cutoff = now - self.window_sec
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def acquire(self, weight: int):
        while True:
            with self._lock:
                now = time.time()
                self._prune(now)
                used = sum(w for _, w in self._events)
                if used + weight <= self.max_weight_per_min:
                    self._events.append((now, weight))
                    return
                # need to wait until some weight expires
                oldest_ts = self._events[0][0] if self._events else now
                sleep_for = max(0.05, (oldest_ts + self.window_sec) - now)
            time.sleep(min(sleep_for, 2.0))

RL = RateLimiter()

def weight_info_default() -> int:
    # "All other documented info requests have weight 20" :contentReference[oaicite:6]{index=6}
    return 20

def weight_candle_snapshot(approx_items: int) -> int:
    # base 20 + extra per 60 items returned :contentReference[oaicite:7]{index=7}
    extra = approx_items // 60
    return 20 + extra

# =========================
# TELEGRAM
# =========================
def send_telegram_message(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=12)
    except Exception as e:
        print(f"Telegram send error: {e}")

# =========================
# HELPERS
# =========================
def tv_link_for_coin(coin: str) -> str:
    # generic; TradingView decides which exchange feed to show
    # you can change TV_PREFIX to your preferred template
    return f"{TV_PREFIX}{coin}USD"

def regime_from_adx(adx: float) -> str:
    if adx > ADX_TREND_THR:
        return "TREND"
    if adx < ADX_CHOP_THR:
        return "CHOP"
    return "NEUTRAL"

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

# =========================
# HL INFO + CACHING
# =========================
_meta_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=6),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def hl_info(payload: dict, weight: int) -> Any:
    RL.acquire(weight)
    r = requests.post(f"{API_BASE}/info", headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def get_meta_and_ctxs_cached() -> Any:
    now = time.time()
    if _meta_cache["data"] is not None and (now - _meta_cache["ts"]) < META_TTL_SEC:
        return _meta_cache["data"]
    data = hl_info({"type": "metaAndAssetCtxs"}, weight=weight_info_default())
    _meta_cache["ts"] = now
    _meta_cache["data"] = data
    return data

def select_topn_filtered(top_n: int) -> List[Tuple[str, float, float, float]]:
    """
    Returns list of (coin, activity_score, dayNtlVlm, impact_spread_pct) sorted desc by score.
    Uses metaAndAssetCtxs -> universe + assetCtxs (dayNtlVlm, midPx, impactPxs).
    """
    res = get_meta_and_ctxs_cached()
    if not (isinstance(res, list) and len(res) >= 2):
        return []

    meta = res[0] if isinstance(res[0], dict) else {}
    ctxs = res[1] if isinstance(res[1], list) else []

    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    rows = []

    for i, u in enumerate(universe):
        if not isinstance(u, dict):
            continue
        coin = u.get("name")
        if not coin:
            continue

        c = ctxs[i] if i < len(ctxs) and isinstance(ctxs[i], dict) else {}
        day_ntl = safe_float(c.get("dayNtlVlm", 0.0), 0.0)
        mid = safe_float(c.get("midPx", 0.0), 0.0)
        impact = c.get("impactPxs") or [None, None]

        spread_pct = 999.0
        if mid > 0 and isinstance(impact, list) and len(impact) == 2 and impact[0] and impact[1]:
            buy_imp = safe_float(impact[0], mid)
            sell_imp = safe_float(impact[1], mid)
            spread_pct = abs(sell_imp - buy_imp) / mid * 100.0

        # Filters
        if day_ntl < MIN_DAY_NTL_VLM:
            continue
        if spread_pct > MAX_IMPACT_SPREAD_PCT:
            continue

        # Score: volume heavy, penalize spread
        activity_score = math.log1p(day_ntl) / (1.0 + 0.6 * spread_pct)
        rows.append((coin, activity_score, day_ntl, spread_pct))

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top_n]

# =========================
# CANDLES
# =========================
@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=6),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def get_ohlcv(coin: str, lookback: int) -> Optional[pd.DataFrame]:
    # candleSnapshot expects (coin, interval, startTime, endTime) :contentReference[oaicite:8]{index=8}
    end_ts = int(time.time() * 1000)
    # request roughly lookback candles (1h each)
    start_ts = end_ts - int(lookback * 3600 * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": INTERVAL, "startTime": start_ts, "endTime": end_ts},
    }

    # Assume response items ~lookback (or less), use that for weight estimation
    w = weight_candle_snapshot(approx_items=lookback)
    data = hl_info(payload, weight=w)

    if not data or not isinstance(data, list):
        return None

    rows = []
    for x in data:
        if not isinstance(x, dict):
            continue
        # HL candle fields: t (open time), T (close time), o/h/l/c/v strings :contentReference[oaicite:9]{index=9}
        t = safe_float(x.get("t"))
        o = safe_float(x.get("o"))
        h = safe_float(x.get("h"))
        l = safe_float(x.get("l"))
        c = safe_float(x.get("c"))
        v = safe_float(x.get("v"), 0.0)
        if t == 0.0 or c == 0.0:
            continue
        rows.append([t, o, h, l, c, v])

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df = df.sort_values("ts").reset_index(drop=True)
    # keep last N
    if len(df) > lookback:
        df = df.iloc[-lookback:].reset_index(drop=True)
    return df

# =========================
# SIGNALS (same strategy, improved reporting)
# =========================
def check_signals(df: pd.DataFrame) -> Optional[Tuple[str, float, float, float, float]]:
    """
    Returns (signal, adx, rsi, atrp, score)
    signal in {LONG, SHORT, PRE-LONG, PRE-SHORT}
    """
    if df is None or len(df) < 35:
        return None

    df["EMA_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["EMA_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["RSI_14"] = ta.momentum.rsi(df["close"], window=14)
    df["ADX_14"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    df["ATR_14"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
    df["ATRp_14"] = (df["ATR_14"] / df["close"]) * 100.0

    last = df.iloc[-1]
    if any(pd.isna(last[k]) for k in ["EMA_9", "EMA_21", "RSI_14", "ADX_14", "ATRp_14"]):
        return None

    ema_bull = last["EMA_9"] > last["EMA_21"]
    ema_bear = last["EMA_9"] < last["EMA_21"]

    rsi = float(last["RSI_14"])
    adx = float(last["ADX_14"])
    atrp = float(last["ATRp_14"])
    close = float(last["close"])

    adx_strong = adx > ADX_TREND_THR
    adx_rising = ADX_PRE_MIN <= adx <= ADX_PRE_MAX

    rsi_long = rsi > 55
    rsi_short = rsi < 45
    rsi_near_long = 50 < rsi <= 55
    rsi_near_short = 45 <= rsi < 50

    signal = None
    if adx_strong:
        if ema_bull and rsi_long:
            signal = "LONG"
        elif ema_bear and rsi_short:
            signal = "SHORT"
    elif adx_rising:
        if ema_bull and rsi_near_long:
            signal = "PRE-LONG"
        elif ema_bear and rsi_near_short:
            signal = "PRE-SHORT"

    if not signal:
        return None

    # Ranking score (does not change signal; only sorts output)
    ema_sep = abs(float(last["EMA_9"]) - float(last["EMA_21"])) / close if close > 0 else 0.0
    if "LONG" in signal:
        rsi_dist = max(0.0, rsi - 50.0)
    else:
        rsi_dist = max(0.0, 50.0 - rsi)

    # prefer "reasonable" volatility window, penalize extremes
    atr_bonus = 0.0
    if 0.8 <= atrp <= 3.5:
        atr_bonus = 1.0
    elif atrp < 0.5:
        atr_bonus = -1.0
    elif atrp > 6.0:
        atr_bonus = -0.5

    pre_penalty = -2.0 if "PRE" in signal else 0.0
    score = float((adx * 1.0) + (rsi_dist * 0.6) + (ema_sep * 500.0) + (atr_bonus * 3.0) + pre_penalty)

    return (signal, adx, rsi, atrp, score)

def size_hint(score: float) -> str:
    if score >= 40:
        return "Setup: A (normale size)"
    if score >= 34:
        return "Setup: B (kleiner)"
    return "Setup: C (heel klein / alleen als chart perfect is)"

# =========================
# SCAN
# =========================
_last_scan_ts = 0.0

def scan_and_notify():
    global _last_scan_ts

    now = time.time()
    if now - _last_scan_ts < MIN_SECONDS_BETWEEN_SCANS:
        send_telegram_message(f"⏳ Even wachten: scan cooldown ({MIN_SECONDS_BETWEEN_SCANS}s).")
        return
    _last_scan_ts = now

    t0 = time.time()

    # Universe selection (cached meta)
    top = select_topn_filtered(TOP_N)
    if not top:
        send_telegram_message("❌ Geen coins gevonden (filters te streng of API issue).")
        return

    # Scan sequentially (safest under weight limits).
    # If you want faster, lower TOP_N or loosen filters—otherwise you risk 429. :contentReference[oaicite:10]{index=10}
    found = []
    scanned = 0
    no_data = 0
    no_signal = 0

    for coin, activity_score, day_ntl, spread_pct in top:
        scanned += 1
        try:
            df = get_ohlcv(coin, LOOKBACK)
        except Exception:
            no_data += 1
            continue

        if df is None or df.empty:
            no_data += 1
            continue

        res = check_signals(df)
        if not res:
            no_signal += 1
            continue

        signal, adx, rsi, atrp, score = res
        regime = regime_from_adx(adx)

        # Optional improvement: skip ENTRY trades in CHOP regime (keeps strategy signals but reduces bad candidates)
        if FILTER_CHOP_ENTRIES and regime == "CHOP" and signal in ("LONG", "SHORT"):
            no_signal += 1
            continue

        found.append((coin, signal, adx, rsi, atrp, score, regime, day_ntl, spread_pct))

    dt = time.time() - t0

    send_telegram_message(
        f"⚙️ Debug: gescand={scanned} | signals={len(found)} | no_signal={no_signal} | no_data={no_data} | {dt:.1f}s\n"
        f"Filters: dayNtlVlm>={MIN_DAY_NTL_VLM:.0f} | impactSpread<={MAX_IMPACT_SPREAD_PCT:.2f}% | TOP_N={TOP_N}"
    )

    if not found:
        send_telegram_message("🔍 Geen kansen volgens jouw strategie (met huidige filters).")
        return

    found.sort(key=lambda x: x[5], reverse=True)
    topk = found[:MAX_RESULTS_IN_TELEGRAM]

    msg = "📊 Beste kansen (1H) — Hyperliquid\nKlik link → check chart (manual)\n\n"

    for coin, signal, adx, rsi, atrp, score, regime, day_ntl, spread_pct in topk:
        tv = tv_link_for_coin(coin)
        hint = size_hint(score)

        if "PRE" in signal:
            msg += (
                f"🟡 ⚠️ PRE — {coin}\n"
                f"Regime: {regime}\n"
                f"ADX: {adx:.1f} | RSI: {rsi:.1f} | ATR%: {atrp:.2f} | Score: {score:.1f}\n"
                f"Dit is een VOOR-signaal (kijken, niet blind traden)\n"
                f"Liquidity: dayNtlVlm={day_ntl/1e6:.1f}M | impact≈{spread_pct:.2f}%\n"
                f"{hint}\n"
                f"Open chart:\n{tv}\n\n"
            )
        else:
            emoji = "🟢" if signal == "LONG" else "🔴"
            msg += (
                f"{emoji} {signal} — {coin}\n"
                f"Regime: {regime}\n"
                f"ADX: {adx:.1f} | RSI: {rsi:.1f} | ATR%: {atrp:.2f} | Score: {score:.1f}\n"
                f"Liquidity: dayNtlVlm={day_ntl/1e6:.1f}M | impact≈{spread_pct:.2f}%\n"
                f"{hint}\n"
                f"Open chart:\n{tv}\n\n"
            )

    send_telegram_message(msg)

# =========================
# FLASK WEBHOOK
# =========================
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "ok"

@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()

    # Commands
    if text == "zoek":
        threading.Thread(target=scan_and_notify, daemon=True).start()
        return "ok"

    if text == "status":
        send_telegram_message(
            f"✅ Bot online.\n"
            f"TOP_N={TOP_N}, LOOKBACK={LOOKBACK}, FILTER_CHOP_ENTRIES={FILTER_CHOP_ENTRIES}\n"
            f"Filters: MIN_DAY_NTL_VLM={MIN_DAY_NTL_VLM:.0f}, MAX_IMPACT_SPREAD_PCT={MAX_IMPACT_SPREAD_PCT:.2f}"
        )
        return "ok"

    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
