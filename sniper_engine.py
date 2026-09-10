import os
import io
import json
import base64
import asyncio
import httpx
import pandas as pd
import websockets
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

# ==================== CONFIGURATION & GLOBAL STATE ==================== #
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "61048").strip()
SYMBOL = "frxXAUUSD"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

http_client = httpx.AsyncClient(timeout=25.0)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
AVAILABLE_GEMINI_MODELS = []

last_trade_time = datetime.min.replace(tzinfo=timezone.utc)
post_loss_cooldown_until = datetime.min.replace(tzinfo=timezone.utc)
COOLDOWN_MINUTES = 5
MIN_RRR = 2.0
ENTRY_BUFFER = 0.25  # $0.25 spread buffer for guaranteed limit fills

active_setup = {
    "is_active": False,
    "order_type": "LIMIT",  # "MARKET" or "LIMIT"
    "direction": None,
    "entry_price": 0.0,
    "sl_price": 0.0,
    "tp1_price": 0.0,
    "tp2_price": 0.0,
    "tp1_hit": False,
    "entry_filled": False
}

# ==================== WEBSOCKET & MARKET DATA ==================== #
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

async def fetch_live_spread() -> tuple[float, float, float]:
    req = {"ticks": SYMBOL}
    res = await deriv_request(req)
    tick = res.get("tick", {})
    bid = float(tick.get("bid", 0.0))
    ask = float(tick.get("ask", 0.0))
    spread = ask - bid if (ask > 0 and bid > 0) else 0.0
    return bid, ask, spread

# ==================== TECHNICAL INDICATORS & FILTERS ==================== #
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df['high'], df['low'], df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(window=period).mean().iloc[-1])

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

def check_macro_trend_filter(df_h1: pd.DataFrame, direction: str) -> bool:
    if df_h1.empty or len(df_h1) < 20:
        return False

    df_h1['ema200'] = df_h1['close'].ewm(span=200, adjust=False).mean()
    last_close = df_h1['close'].iloc[-1]
    ema_val = df_h1['ema200'].iloc[-1]

    if direction == "BUY" and last_close < ema_val:
        print(f"[REJECTED] Cannot BUY below 1H 200 EMA ({last_close:.2f} < {ema_val:.2f})")
        return False

    if direction == "SELL" and last_close > ema_val:
        print(f"[REJECTED] Cannot SELL above 1H 200 EMA ({last_close:.2f} > {ema_val:.2f})")
        return False

    return True

def verify_hard_smc_sweep(df_5m: pd.DataFrame, direction: str) -> bool:
    if df_5m.empty or len(df_5m) < 20:
        return False

    lookback = df_5m.tail(15)
    last_candle = lookback.iloc[-1]
    prev_candles = lookback.iloc[:-1]

    if direction == "BUY":
        swing_low = prev_candles['low'].min()
        swept = lookback['low'].min() <= swing_low
        closed_above = last_candle['close'] > swing_low
        is_bullish = last_candle['close'] > last_candle['open']
        return swept and closed_above and is_bullish

    if direction == "SELL":
        swing_high = prev_candles['high'].max()
        swept = lookback['high'].max() >= swing_high
        closed_below = last_candle['close'] < swing_high
        is_bearish = last_candle['close'] < last_candle['open']
        return swept and closed_below and is_bearish

    return False

# ==================== DUAL-ENTRY & POSITION TRACKING ==================== #
def determine_execution_type(current_price: float, raw_entry: float, direction: str) -> tuple[str, float]:
    distance = abs(current_price - raw_entry)
    
    # If market has already left zone (within 1.00 point after BOS), execute MARKET immediately
    if distance <= 1.00:
        return "MARKET", current_price

    # Apply execution buffer for LIMIT orders to guarantee fill on MT5
    buffered_entry = (raw_entry - ENTRY_BUFFER) if direction == "SELL" else (raw_entry + ENTRY_BUFFER)
    return "LIMIT", buffered_entry

def update_and_check_active_setup(current_price: float, send_alert_func) -> bool:
    global active_setup, post_loss_cooldown_until
    now_utc = datetime.now(timezone.utc)

    if now_utc < post_loss_cooldown_until:
        remaining_mins = int((post_loss_cooldown_until - now_utc).total_seconds() / 60)
        print(f"⏳ [POST-LOSS LOCKOUT] Cooldown active for {remaining_mins}m. Skipping scan.")
        return True

    if not active_setup["is_active"]:
        return False

    direction = active_setup["direction"]
    entry = active_setup["entry_price"]
    sl = active_setup["sl_price"]
    tp1 = active_setup["tp1_price"]
    tp2 = active_setup["tp2_price"]
    tp1_hit = active_setup["tp1_hit"]
    entry_filled = active_setup["entry_filled"]

    # STEP 1: Verify Limit Order Fill or Target-Reached Invalidation
    if not entry_filled:
        # Check if price touched entry level
        if (direction == "BUY" and current_price <= entry) or (direction == "SELL" and current_price >= entry):
            active_setup["entry_filled"] = True
            print(f"✅ [LIMIT ORDER FILLED] {direction} triggered @ {current_price:.2f}")
            asyncio.create_task(
                send_alert_func(f"⚡ *LIMIT ORDER FILLED:* {direction} executed on MT5 at `${current_price:.2f}`. Trade is now LIVE.")
            )
            return True

        # TARGET-REACHED CANCELLATION: Price hit TP before retesting limit entry
        if (direction == "BUY" and current_price >= tp1) or (direction == "SELL" and current_price <= tp1):
            active_setup["is_active"] = False
            print(f"⚠️ [SETUP CANCELED] Target hit before filling limit entry @ {entry:.2f}.")
            asyncio.create_task(
                send_alert_func(
                    f"⚠️ *PENDING ORDER CANCELED:* Price reached target level without filling limit entry `${entry:.2f}`.\n"
                    f"❌ *Action:* CANCEL pending {direction} LIMIT order on MT5 immediately."
                )
            )
            return False

        print(f"⏳ [PENDING LIMIT] Waiting for retest to entry `${entry:.2f}` | Current: `${current_price:.2f}`")
        return True

    # STEP 2: Position Management (Runs strictly AFTER Entry Fill)
    if not tp1_hit:
        if (direction == "BUY" and current_price >= tp1) or (direction == "SELL" and current_price <= tp1):
            active_setup["tp1_hit"] = True
            active_setup["sl_price"] = entry
            print(f"🎯 [TP1 HIT] Reached 1:1 RRR @ {current_price:.2f}. SL moved to BE (${entry:.2f}).")
            asyncio.create_task(
                send_alert_func(
                    f"🎯 *TAKE PROFIT 1 HIT:* `${current_price:.2f}`!\n"
                    f"🛡️ *RISK-FREE TRADE:* Stop Loss automatically moved to Break-Even (`${entry:.2f}`)."
                )
            )

    if (direction == "BUY" and current_price <= sl) or (direction == "SELL" and current_price >= sl):
        active_setup["is_active"] = False
        if tp1_hit:
            print(f"🛡️ [BE EXIT] Closed at Break-Even @ {current_price:.2f}.")
            asyncio.create_task(
                send_alert_func(f"🛡️ *TRADE CLOSED AT BREAK-EVEN:* Entry level `${entry:.2f}` retested. Profits secured from TP1.")
            )
            return False
        else:
            print(f"❌ [SL HIT] Closed @ {current_price:.2f}. Locking engine for 40 mins.")
            post_loss_cooldown_until = now_utc + timedelta(minutes=40)
            asyncio.create_task(
                send_alert_func(f"❌ *STOP LOSS HIT:* Closed at `${current_price:.2f}`. Engine entering 40-minute lockout.")
            )
            return True

    if (direction == "BUY" and current_price >= tp2) or (direction == "SELL" and current_price <= tp2):
        print(f"🚀 [TP2 HIT] Closed @ {current_price:.2f}. Full SMC target smashed!")
        active_setup["is_active"] = False
        asyncio.create_task(
            send_alert_func(f"🚀 *FINAL TAKE PROFIT 2 HIT:* Full target reached at `${current_price:.2f}`! All profits secured.")
        )
        return False

    print(f"⏳ [POSITION ACTIVE] Holding {direction} | TP1: {tp1:.2f} | TP2: {tp2:.2f} | Current SL: {sl:.2f}")
    return True

# ==================== CHART GENERATION ==================== #
def draw_smc_projection_overlay(ax, df, entry, sl, tp, direction):
    trigger_idx = len(df) - 1
    projection_width = 10

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

    ax2.set_xlim(-1, len(chart_5m) + 10)

    if direction and entry > 0:
        draw_smc_projection_overlay(ax2, chart_5m, entry, sl, tp, direction)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ==================== DUAL-AI VISION ENGINE ==================== #
SYSTEM_PROMPT = """
You are an elite Smart Money Concepts (SMC) trader evaluating Gold (XAUUSD).
Do NOT approve market orders at the top or bottom of strong expansion candles. Demand a retest/pullback entry zone.

Respond strictly in raw JSON:
{
  "trade_approved": true/false,
  "direction": "BUY" or "SELL",
  "recommended_entry": float,
  "take_profit_price": float,
  "strategy_detected": "5M Liquidity Sweep into 1H Demand/Supply Zone",
  "reason": "Brief technical reasoning..."
}
"""

def refresh_gemini_models():
    global AVAILABLE_GEMINI_MODELS
    if not gemini_client:
        return
    try:
        fetched = [m.name.replace("models/", "") for m in gemini_client.models.list() if "flash" in m.name.lower() or "pro" in m.name.lower()]
        if fetched:
            AVAILABLE_GEMINI_MODELS = fetched
    except Exception as e:
        print(f"[GEMINI DISCOVERY WARNING] {e}")

async def evaluate_with_gemini(chart_bytes: bytes, market_summary: str) -> dict:
    if not gemini_client:
        return {"trade_approved": False, "failed": True}

    prompt = f"{SYSTEM_PROMPT}\n\nLive Market: {market_summary}"
    models = AVAILABLE_GEMINI_MODELS if AVAILABLE_GEMINI_MODELS else ["gemini-2.5-flash"]

    for model in models:
        try:
            res = await asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=chart_bytes, mime_type="image/png"), prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
            )
            parsed = json.loads(res.text)
            parsed["failed"] = False
            return parsed
        except Exception:
            continue

    return {"trade_approved": False, "failed": True}

async def evaluate_with_openrouter_free(chart_bytes: bytes, market_summary: str) -> dict:
    if not OPENROUTER_API_KEY:
        return {"trade_approved": False, "failed": True}

    base64_img = base64.b64encode(chart_bytes).decode('utf-8')
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}

    for model_name in ["google/gemma-4-31b-it:free", "openrouter/free"]:
        try:
            payload = {
                "model": model_name,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{SYSTEM_PROMPT}\n\nLive Market: {market_summary}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}}
                    ]
                }],
                "response_format": {"type": "json_object"}
            }
            res = await http_client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
            if res.status_code == 200:
                parsed = json.loads(res.json()["choices"][0]["message"]["content"])
                parsed["failed"] = False
                return parsed
        except Exception:
            continue

    return {"trade_approved": False, "failed": True}

async def get_dual_ai_consensus(chart_bytes: bytes, current_price: float, atr_val: float) -> dict:
    summary = f"Price: {current_price:.2f} USD | ATR: {atr_val:.2f}"
    g_res, o_res = await asyncio.gather(evaluate_with_gemini(chart_bytes, summary), evaluate_with_openrouter_free(chart_bytes, summary))

    if g_res.get("failed") and o_res.get("failed"):
        return {"approved": False, "reason": "Both Vision APIs offline."}

    active_res = g_res if not g_res.get("failed") else o_res
    if not active_res.get("trade_approved", False):
        return {"approved": False, "reason": active_res.get("reason", "Not approved")}

    direction = active_res.get("direction")
    raw_entry = float(active_res.get("recommended_entry", current_price))
    
    order_type, entry_price = determine_execution_type(current_price, raw_entry, direction)

    sl_distance = max(atr_val * 2.0, 1.50)
    sl_price = (entry_price - sl_distance - 0.30) if direction == "BUY" else (entry_price + sl_distance + 0.30)
    raw_tp = float(active_res.get("take_profit_price", entry_price + (sl_distance * 2.2 if direction == "BUY" else -sl_distance * 2.2)))
    tp2_price = raw_tp - 1.00 if direction == "BUY" else raw_tp + 1.00

    tp_dist = abs(tp2_price - entry_price)
    sl_dist = abs(entry_price - sl_price)
    rrr = tp_dist / sl_dist if sl_dist > 0 else 0.0

    if rrr < MIN_RRR:
        return {"approved": False, "reason": f"RRR too low (1:{rrr:.2f})"}

    return {
        "approved": True, "direction": direction, "order_type": order_type,
        "entry_price": entry_price, "sl_price": sl_price, "tp2_price": tp2_price,
        "rrr_str": f"1:{rrr:.2f}", "strategy": active_res.get("strategy_detected", "SMC Setup"),
        "reason": active_res.get("reason", "Approved")
    }

# ==================== TELEGRAM ALERTS ==================== #
async def send_telegram_alert(message: str, image_bytes: bytes = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        if image_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', image_bytes, 'image/png')}
            await http_client.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}, files=files)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            await http_client.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ==================== MAIN WORKER & SERVER ==================== #
async def deriv_trading_worker():
    global last_trade_time, active_setup
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

            if update_and_check_active_setup(current_price, send_telegram_alert):
                continue

            _, _, spread = await fetch_live_spread()
            if spread > 0.45:
                print(f"[SKIP] Spread high: ${spread:.2f}")
                continue

            atr_val = calculate_atr(df_5m)
            eval_bytes = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1)
            consensus = await get_dual_ai_consensus(eval_bytes, current_price, atr_val)

            if not consensus["approved"]:
                continue

            direction = consensus["direction"]

            if not check_macro_trend_filter(df_h1, direction) or not verify_hard_smc_sweep(df_5m, direction):
                continue

            order_type = consensus["order_type"]
            entry_price = consensus["entry_price"]
            sl_price = consensus["sl_price"]
            tp2_price = consensus["tp2_price"]
            
            sl_dist = abs(entry_price - sl_price)
            tp1_price = (entry_price + sl_dist) if direction == "BUY" else (entry_price - sl_dist)

            last_trade_time = now_utc
            active_setup.update({
                "is_active": True,
                "order_type": order_type,
                "direction": direction,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp1_price": tp1_price,
                "tp2_price": tp2_price,
                "tp1_hit": False,
                "entry_filled": True if order_type == "MARKET" else False
            })

            final_chart = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1, entry_price, sl_price, tp2_price, direction)

            action_str = f"{direction} NOW (MARKET EXECUTION)" if order_type == "MARKET" else f"{direction} LIMIT / RETEST"
            entry_zone_str = f"${entry_price:.2f}" if order_type == "MARKET" else f"${entry_price - 0.50:.2f} - ${entry_price + 0.50:.2f}"

            msg = (
                f"⚡ *AURA AI TRADING ENGINE v6.0*\n"
                f"🏷️ _Gold Accelerator Institutional Setup_\n\n"
                f"🏆 *Asset:* `XAUUSD (Gold)`\n"
                f"⚔️ *Action:* `{action_str}`\n"
                f"📍 *Entry Level:* `{entry_zone_str}`\n"
                f"🛑 *Initial Stop Loss:* `${sl_price:.2f}`\n"
                f"🎯 *Take Profit 1 (1:1):* `${tp1_price:.2f}`\n"
                f"🚀 *Take Profit 2 (SMC Target):* `${tp2_price:.2f}`\n"
                f"⚖️ *Target RRR:* `{consensus['rrr_str']}`\n\n"
                f"🛡️ *AUTOMATED RISK PROTOCOL:* SL automatically moves to Break-Even (`${entry_price:.2f}`) once TP1 is hit.\n\n"
                f"📌 *Strategy:* `{consensus['strategy']}`\n"
                f"_{consensus['reason']}_"
            )
            await send_telegram_alert(msg, final_chart)

        except Exception as err:
            print(f"[WORKER ERROR] {err}")
            await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(deriv_trading_worker())
    yield
    worker_task.cancel()
    await http_client.aclose()

app = FastAPI(title="Aura AI Engine", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ONLINE"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
 
