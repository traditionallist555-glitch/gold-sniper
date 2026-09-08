import os
import io
import json
import base64
import asyncio
import httpx
import websockets
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import mplfinance as mpf
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn

from google import genai
from google.genai import types

# ==================== ENVIRONMENT CONFIGURATION ==================== #
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "61048").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

SYMBOL = "frxXAUUSD"      # Gold symbol on Deriv
MIN_RRR = 2.0              # Minimum Risk-Reward Ratio
COOLDOWN_MINUTES = 5       # Cycle evaluation interval

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)
post_loss_cooldown_until = datetime.min.replace(tzinfo=timezone.utc)

http_client = httpx.AsyncClient(timeout=25.0)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

AVAILABLE_GEMINI_MODELS = []

# ==================== ACTIVE SETUP STATE TRACKER ==================== #
active_setup = {
    "is_active": False,
    "direction": None,
    "sl_price": 0.0,
    "tp_price": 0.0,
    "be_alert_sent": False
}

def update_and_check_active_setup(current_price: float) -> bool:
    """Manages active trades, locks scan loops, and handles post-loss cooldowns."""
    global active_setup, post_loss_cooldown_until
    now_utc = datetime.now(timezone.utc)

    # 1. Enforce Post-Loss Lockout
    if now_utc < post_loss_cooldown_until:
        remaining_mins = int((post_loss_cooldown_until - now_utc).total_seconds() / 60)
        print(f"⏳ [POST-LOSS LOCKOUT] Engine paused for {remaining_mins} more minutes. Skipping scan.")
        return True

    if not active_setup["is_active"]:
        return False

    direction = active_setup["direction"]
    sl = active_setup["sl_price"]
    tp = active_setup["tp_price"]

    # 2. Check Stop Loss Hit -> Triggers 40-min lockout
    if (direction == "BUY" and current_price <= sl) or (direction == "SELL" and current_price >= sl):
        print(f"❌ [SL HIT] Closed @ {current_price:.2f}. Locking engine for 40 minutes.")
        active_setup["is_active"] = False
        post_loss_cooldown_until = now_utc + timedelta(minutes=40)
        asyncio.create_task(send_telegram_alert(f"❌ *TRADE EXIT:* Stop Loss hit at `${current_price:.2f}`. Engine entering 40-minute lockout."))
        return True

    # 3. Check Take Profit Hit -> Resets engine immediately
    if (direction == "BUY" and current_price >= tp) or (direction == "SELL" and current_price <= tp):
        print(f"✅ [TP HIT] Closed @ {current_price:.2f}. Engine unlocked.")
        active_setup["is_active"] = False
        asyncio.create_task(send_telegram_alert(f"🎯 *TAKE PROFIT HIT:* Trade closed successfully at `${current_price:.2f}`!"))
        return False

    print(f"⏳ [POSITION ACTIVE] Holding {direction}. TP: {tp:.2f} | SL: {sl:.2f}. Scan locked.")
    return True

# ==================== SPREAD & TREND FILTERS ==================== #
async def fetch_live_spread() -> tuple[float, float, float]:
    """Fetches live bid/ask to calculate dynamic spread."""
    req = {"ticks": SYMBOL}
    res = await deriv_request(req)
    tick = res.get("tick", {})
    bid = float(tick.get("bid", 0.0))
    ask = float(tick.get("ask", 0.0))
    spread = ask - bid if (ask > 0 and bid > 0) else 0.0
    return bid, ask, spread

def check_macro_trend_filter(df_h1: pd.DataFrame, direction: str) -> bool:
    """Prevents taking BUYs below 1H 200 EMA or SELLs above it."""
    ema_200 = df_h1['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    current_price = df_h1['close'].iloc[-1]

    if direction == "BUY" and current_price < ema_200:
        print(f"[REJECTED] Cannot BUY below 1H 200 EMA ({ema_200:.2f}). Market is Macro Bearish.")
        return False

    if direction == "SELL" and current_price > ema_200:
        print(f"[REJECTED] Cannot SELL above 1H 200 EMA ({ema_200:.2f}). Market is Macro Bullish.")
        return False

    return True

def verify_hard_smc_sweep(df_5m: pd.DataFrame, direction: str) -> bool:
    """Mathematical 5M liquidity sweep verification on raw candles."""
    lookback = df_5m.tail(20)
    lowest_low = lookback['low'].min()
    highest_high = lookback['high'].max()
    
    last_candle = lookback.iloc[-1]
    prev_candle = lookback.iloc[-2]

    if direction == "BUY":
        swept_low = (prev_candle['low'] == lowest_low) or (last_candle['low'] == lowest_low)
        strong_rebound = last_candle['close'] > last_candle['open']
        return swept_low and strong_rebound

    if direction == "SELL":
        swept_high = (prev_candle['high'] == highest_high) or (last_candle['high'] == highest_high)
        strong_rejection = last_candle['close'] < last_candle['open']
        return swept_high and strong_rejection

    return False

# ==================== KILLZONE SESSION FILTER ==================== #
def is_within_killzone() -> bool:
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return False

    current_time = now_utc.time()
    london_start = datetime.strptime("07:00", "%H:%M").time()
    london_end = datetime.strptime("11:00", "%H:%M").time()
    ny_start = datetime.strptime("13:00", "%H:%M").time()
    ny_end = datetime.strptime("17:00", "%H:%M").time()

    return (london_start <= current_time <= london_end) or (ny_start <= current_time <= ny_end)

# ==================== DYNAMIC ATR CALCULATOR ==================== #
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df['high'], df['low'], df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(window=period).mean().iloc[-1])

# ==================== MARKET DATA WEBSOCKET ==================== #
async def deriv_request(req: dict) -> dict:
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send(json.dumps(req))
            return json.loads(await ws.recv())
    except Exception as err:
        print(f"[DERIV WS ERROR] {err}")
        return {}

async def fetch_deriv_candles(granularity: int = 300, count: int = 200) -> pd.DataFrame:
    req = {
        "ticks_history": SYMBOL,
        "adjust_start_time": 1,
        "count": count,
        "end": "latest",
        "granularity": granularity,
        "style": "candles"
    }
    res = await deriv_request(req)
    candles = res.get("candles", [])

    if not candles:
        return pd.DataFrame()

    records = [
        {
            'time': pd.to_datetime(c['epoch'], unit='s', utc=True),
            'open': float(c['open']),
            'high': float(c['high']),
            'low': float(c['low']),
            'close': float(c['close'])
        }
        for c in candles
    ]
    df = pd.DataFrame(records)
    df.set_index('time', inplace=True)
    return df

# ==================== TRADINGVIEW-STYLE PROJECTION OVERLAY ==================== #
def draw_smc_projection_overlay(ax, df, entry, sl, tp, direction):
    """Draws position projection box starting precisely at the entry candle."""
    trigger_idx = len(df) - 1
    projection_width = 12

    zone_top = max(entry, sl) if direction == "SELL" else max(entry, sl)
    zone_bottom = min(entry, sl) if direction == "SELL" else min(entry, sl)
    
    zone_box = patches.Rectangle(
        (trigger_idx - 10, zone_bottom), 10, (zone_top - zone_bottom),
        linewidth=0.5, edgecolor='#888888', facecolor='#888888', alpha=0.20, zorder=2
    )
    ax.add_patch(zone_box)

    if direction == "BUY":
        tp_box = patches.Rectangle((trigger_idx, entry), projection_width, (tp - entry), linewidth=0, facecolor='#26a69a', alpha=0.25, zorder=2)
        sl_box = patches.Rectangle((trigger_idx, sl), projection_width, (entry - sl), linewidth=0, facecolor='#ef5350', alpha=0.25, zorder=2)
    else:
        sl_box = patches.Rectangle((trigger_idx, entry), projection_width, (sl - entry), linewidth=0, facecolor='#ef5350', alpha=0.25, zorder=2)
        tp_box = patches.Rectangle((trigger_idx, tp), projection_width, (entry - tp), linewidth=0, facecolor='#26a69a', alpha=0.25, zorder=2)

    ax.add_patch(tp_box)
    ax.add_patch(sl_box)

    ax.axhline(y=entry, color='#3179f5', linestyle='-', linewidth=1.2)
    ax.axhline(y=sl, color='#ef5350', linestyle='--', linewidth=1.2)
    ax.axhline(y=tp, color='#26a69a', linestyle='--', linewidth=1.2)

# ==================== DUAL-PANEL CHART GENERATOR ==================== #
def render_dual_panel_chart(df_5m: pd.DataFrame, df_h1: pd.DataFrame, entry: float = 0.0, sl: float = 0.0, tp: float = 0.0, direction: str = None) -> bytes:
    chart_5m = df_5m.tail(50).copy()
    chart_h1 = df_h1.tail(30).copy()

    for df in [chart_5m, chart_h1]:
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)

    chart_h1['EMA200'] = chart_h1['Close'].ewm(span=200, adjust=False).mean()

    mc = mpf.make_marketcolors(up='#089981', down='#F23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=False)

    fig = mpf.figure(figsize=(14, 6), style=style)
    ax1 = fig.add_subplot(1, 2, 1)
    ax2 = fig.add_subplot(1, 2, 2)

    addplots_h1 = [mpf.make_addplot(chart_h1['EMA200'], ax=ax1, color='gold', width=1.2)]

    mpf.plot(chart_h1, type='candle', ax=ax1, addplot=addplots_h1, axtitle="1-Hour Macro Context (EMA 200)")
    mpf.plot(chart_5m, type='candle', ax=ax2, axtitle="5-Minute Execution Structure (50 candles)")

    if direction and entry > 0:
        draw_smc_projection_overlay(ax2, chart_5m, entry, sl, tp, direction)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ==================== DYNAMIC GEMINI MODEL RESOLVER ==================== #
def refresh_gemini_models():
    global AVAILABLE_GEMINI_MODELS
    if not gemini_client:
        return
    try:
        fetched = []
        for m in gemini_client.models.list():
            model_id = str(getattr(m, 'name', '')).replace("models/", "")
            if ("flash" in model_id.lower() or "pro" in model_id.lower()) and "embedding" not in model_id.lower():
                fetched.append(model_id)
        if fetched:
            AVAILABLE_GEMINI_MODELS = fetched
            print(f"✅ [GEMINI DISCOVERY] Active Models: {AVAILABLE_GEMINI_MODELS[:3]}")
    except Exception as e:
        print(f"[GEMINI DISCOVERY WARNING] {e}")

# ==================== DUAL-AI VISION ENGINE ==================== #
SYSTEM_PROMPT = """
You are an elite Smart Money Concepts (SMC) trader evaluating Gold (XAUUSD).
Evaluate structural context, liquidity sweeps, OBs, and FVGs on the provided dual-panel chart.

Respond strictly in raw JSON:
{
  "trade_approved": true/false,
  "direction": "BUY" or "SELL",
  "stop_loss_price": float,
  "take_profit_price": float,
  "strategy_detected": "5M Liquidity Sweep into 1H Demand/Supply Zone",
  "reason": "Brief technical reasoning..."
}
"""

def sync_gemini_generate(chart_bytes: bytes, prompt_content: str, model_name: str) -> dict:
    response = gemini_client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_bytes(data=chart_bytes, mime_type="image/png"),
            prompt_content
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(response.text)

async def evaluate_with_gemini(chart_bytes: bytes, market_summary: str) -> dict:
    if not gemini_client:
        return {"trade_approved": False, "reason": "Gemini Key missing", "failed": True}

    prompt_content = f"{SYSTEM_PROMPT}\n\nLive Market: {market_summary}"
    models_to_try = AVAILABLE_GEMINI_MODELS if AVAILABLE_GEMINI_MODELS else ["gemini-2.5-flash", "gemini-3.6-flash"]

    for model_name in models_to_try:
        try:
            parsed = await asyncio.to_thread(sync_gemini_generate, chart_bytes, prompt_content, model_name)
            parsed["failed"] = False
            return parsed
        except Exception as err:
            print(f"[GEMINI ERROR {model_name}] {err}")
            continue

    return {"trade_approved": False, "reason": "Gemini models offline", "failed": True}

async def evaluate_with_openrouter_free(chart_bytes: bytes, market_summary: str) -> dict:
    if not OPENROUTER_API_KEY:
        return {"trade_approved": False, "reason": "OpenRouter Key missing", "failed": True}

    base64_img = base64.b64encode(chart_bytes).decode('utf-8')
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://deriv-bot.local",
        "X-Title": "Deriv Engine"
    }

    for model_name in ["google/gemma-4-31b-it:free", "minimax/minimax-m3:free", "openrouter/free"]:
        try:
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"{SYSTEM_PROMPT}\n\nLive Market: {market_summary}"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}}
                        ]
                    }
                ],
                "response_format": {"type": "json_object"}
            }
            res = await http_client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                if "choices" in data and len(data["choices"]) > 0:
                    parsed = json.loads(data["choices"][0]["message"]["content"])
                    parsed["failed"] = False
                    return parsed
        except Exception as e:
            print(f"[OPENROUTER ERROR] {e}")

    return {"trade_approved": False, "reason": "OpenRouter Vision offline", "failed": True}

async def get_dual_ai_consensus(chart_bytes: bytes, current_price: float, atr_val: float) -> dict:
    market_summary = f"Price: {current_price:.2f} USD | 14-ATR: {atr_val:.2f}"

    gemini_res, openrouter_res = await asyncio.gather(
        evaluate_with_gemini(chart_bytes, market_summary),
        evaluate_with_openrouter_free(chart_bytes, market_summary)
    )

    g_fail, o_fail = gemini_res.get("failed", True), openrouter_res.get("failed", True)
    g_app, o_app = gemini_res.get("trade_approved", False), openrouter_res.get("trade_approved", False)

    if g_fail and not o_fail:
        active_res, consensus_approved = openrouter_res, o_app
        consensus_reason = f"OpenRouter: {openrouter_res.get('reason')}"
    elif o_fail and not g_fail:
        active_res, consensus_approved = gemini_res, g_app
        consensus_reason = f"Gemini: {gemini_res.get('reason')}"
    elif g_fail and o_fail:
        return {"approved": False, "reason": "Both Vision APIs unreachable."}
    else:
        same_direction = gemini_res.get("direction") == openrouter_res.get("direction")
        consensus_approved = g_app and o_app and same_direction
        active_res = gemini_res
        consensus_reason = f"Gemini: {gemini_res.get('reason')} | OpenRouter: {openrouter_res.get('reason')}"

    if not consensus_approved:
        return {"approved": False, "reason": consensus_reason}

    SL_BUFFER = 0.30
    TP_OFFSET = 1.00

    direction = active_res.get("direction")
    sl_distance = max(atr_val * 2.0, 1.50)

    if direction == "BUY":
        sl_price = (current_price - sl_distance) - SL_BUFFER
        raw_tp = float(active_res.get("take_profit_price", current_price + (sl_distance * 2.2)))
        tp_price = raw_tp - TP_OFFSET
        tp_distance = tp_price - current_price
    else:
        sl_price = (current_price + sl_distance) + SL_BUFFER
        raw_tp = float(active_res.get("take_profit_price", current_price - (sl_distance * 2.2)))
        tp_price = raw_tp + TP_OFFSET
        tp_distance = current_price - tp_price

    calculated_rrr = tp_distance / (abs(current_price - sl_price)) if abs(current_price - sl_price) > 0 else 0.0

    if calculated_rrr < MIN_RRR:
        return {"approved": False, "reason": f"Discarded: RRR is 1:{calculated_rrr:.2f} (Minimum required: 1:{MIN_RRR:.1f})."}

    return {
        "approved": True,
        "direction": direction,
        "entry_price": current_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "rrr_str": f"1:{calculated_rrr:.2f}",
        "strategy": active_res.get("strategy_detected", "SMC Setup"),
        "reason": consensus_reason
    }

# ==================== TELEGRAM NOTIFIER ==================== #
async def send_telegram_alert(message: str, image_bytes: bytes = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        if image_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', image_bytes, 'image/png')}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}
            await http_client.post(url, data=data, files=files)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
            await http_client.post(url, json=payload)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ==================== MAIN WORKER LOOP ==================== #
async def deriv_trading_worker():
    global last_trade_time, active_setup
    print("🚀 DUAL-AI ENGINE v5.3 MASTER ONLINE")
    
    refresh_gemini_models()

    while True:
        try:
            await asyncio.sleep(30)
            now_utc = datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            if not is_within_killzone():
                continue

            df_5m = await fetch_deriv_candles(granularity=300, count=200)
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50)

            if df_5m.empty or df_h1.empty:
                continue

            current_price = df_5m['close'].iloc[-1]

            if update_and_check_active_setup(current_price):
                continue

            # Check Dynamic Spread Safety Filter
            _, _, live_spread = await fetch_live_spread()
            if live_spread > 0.45:
                print(f"[REJECTED] High Spread Detected: ${live_spread:.2f} (Max: $0.45)")
                continue

            atr_val = calculate_atr(df_5m, period=14)
            eval_chart_bytes = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1)
            consensus = await get_dual_ai_consensus(eval_chart_bytes, current_price, atr_val)

            if not consensus["approved"]:
                print(f"[AI NO-TRADE] {consensus['reason']}")
                continue

            direction = consensus["direction"]

            # Programmatic Structural Filters
            if not check_macro_trend_filter(df_h1, direction):
                continue

            if not verify_hard_smc_sweep(df_5m, direction):
                print("[SKIP] No valid mathematical sweep detected on 5M DataFrame.")
                continue

            entry_price = consensus["entry_price"]
            sl_price = consensus["sl_price"]
            
