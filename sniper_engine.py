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
import mplfinance as mpf
from datetime import datetime, timezone
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
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

SYMBOL = "frxXAUUSD"      # Gold symbol on Deriv
STAKE_AMOUNT = 2.00        # Fixed Stake $2.00 for target calculations
SL_AMOUNT = 2.00           # Risk cap
MIN_RRR = 2.0              # Hard minimum 1:2 Risk-Reward Ratio
COOLDOWN_MINUTES = 5       # Evaluation interval (5m candle cycles)

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)

http_client = httpx.AsyncClient(timeout=25.0)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==================== ACTIVE SETUP STATE TRACKER ==================== #
active_setup = {
    "is_active": False,
    "direction": None,
    "sl_price": 0.0,
    "tp_price": 0.0,
}

def update_and_check_active_setup(current_price: float) -> bool:
    """
    Prevents repetitive signals. Returns True if a setup is active and running.
    Clears state and returns False if price reaches TP or SL.
    """
    global active_setup

    if not active_setup["is_active"]:
        return False

    direction = active_setup["direction"]
    sl = active_setup["sl_price"]
    tp = active_setup["tp_price"]

    if direction == "BUY":
        if current_price >= tp or current_price <= sl:
            print(f"✅ [SETUP COMPLETED] BUY setup finished @ {current_price:.2f}. Unlocking AI engine.")
            active_setup["is_active"] = False
            return False
    elif direction == "SELL":
        if current_price <= tp or current_price >= sl:
            print(f"✅ [SETUP COMPLETED] SELL setup finished @ {current_price:.2f}. Unlocking AI engine.")
            active_setup["is_active"] = False
            return False

    print(f"⏳ [SETUP IN PROGRESS] Holding {direction}. Target TP: {tp:.2f} | SL: {sl:.2f}. AI scanning locked.")
    return True

# ==================== KILLZONE SESSION & WEEKEND FILTER ==================== #
def is_within_killzone() -> bool:
    """Blocks weekends and limits trading to high-volume London/NY Killzones."""
    now_utc = datetime.now(timezone.utc)

    # Weekend check (5 = Saturday, 6 = Sunday)
    if now_utc.weekday() >= 5:
        print("[FILTERED] Market closed on weekends.")
        return False

    current_time = now_utc.time()
    london_start = datetime.strptime("07:00", "%H:%M").time()
    london_end = datetime.strptime("11:00", "%H:%M").time()
    ny_start = datetime.strptime("13:00", "%H:%M").time()
    ny_end = datetime.strptime("17:00", "%H:%M").time()

    return (london_start <= current_time <= london_end) or (ny_start <= current_time <= ny_end)

# ==================== DYNAMIC ATR VOLATILITY CALCULATOR ==================== #
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculates 14-period Average True Range."""
    high, low, close = df['high'], df['low'], df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(window=period).mean().iloc[-1])

# ==================== DERIV FREE MARKET DATA WEBSOCKET ==================== #
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

# ==================== NEWS GUARDRAIL ==================== #
async def check_news_guardrail() -> bool:
    if not FINNHUB_API_KEY:
        return False

    try:
        url = f"https://finnhub.io/api/v1/calendar/economic?token={FINNHUB_API_KEY}"
        res = await http_client.get(url)
        if res.status_code == 200:
            events = res.json().get("economicCalendar", [])
            now_utc = datetime.now(timezone.utc)

            for event in events:
                if event.get("country") == "US" and str(event.get("impact")).lower() in ["high", "3"]:
                    event_time_str = event.get("time") or event.get("date")
                    if event_time_str:
                        try:
                            event_dt = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
                            time_diff = abs((event_dt - now_utc).total_seconds()) / 60.0
                            if time_diff <= 15:
                                print(f"[NEWS GUARDRAIL] High-impact event {event.get('event')} in {time_diff:.1f}m.")
                                return True
                        except ValueError:
                            continue
    except Exception as e:
        print(f"[NEWS CHECK WARNING] {e}")

    return False

# ==================== DUAL-PANEL CHART GENERATOR ==================== #
def render_dual_panel_chart(df_5m: pd.DataFrame, df_h1: pd.DataFrame) -> bytes:
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
    mpf.plot(chart_5m, type='candle', ax=ax2, axtitle="5-Minute Local Structure")

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ==================== DUAL-AI VISION ENGINE ==================== #
SYSTEM_PROMPT = """
You are an elite Smart Money Concepts (SMC) trader evaluating Gold (XAUUSD).
Multi-panel chart (Left: 1H Macro Context, Right: 5M Execution).

Your Task:
1. Scan for institutional setups (Liquidity Sweeps, Order Blocks, FVGs).
2. Determine direction: "BUY" or "SELL".
3. Specify structural Stop Loss (SL) and realistic Take Profit (TP) target.

Respond ONLY in raw JSON:
{
  "trade_approved": true/false,
  "direction": "BUY" or "SELL",
  "stop_loss_price": float,
  "take_profit_price": float,
  "strategy_detected": "5M Sweep into 1H FVG",
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

    # Updated active model endpoint
    for model_name in ["gemini-2.5-flash"]:
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

    for model_name in ["google/gemini-2.5-flash:free", "openrouter/free"]:
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
                    raw_text = data["choices"][0]["message"]["content"]
                    parsed = json.loads(raw_text)
                    parsed["failed"] = False
                    return parsed
            else:
                print(f"[OPENROUTER HTTP {res.status_code}] {res.text[:100]}")
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
        active_res = openrouter_res
        consensus_approved = o_app
        consensus_reason = f"OpenRouter: {openrouter_res.get('reason')}"
    elif o_fail and not g_fail:
        active_res = gemini_res
        consensus_approved = g_app
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

    sl_distance = max(atr_val * 2.0, 1.50)
    direction = active_res.get("direction")

    if direction == "BUY":
        sl_price = current_price - sl_distance
        tp_price = float(active_res.get("take_profit_price", current_price + (sl_distance * 2.2)))
        tp_distance = tp_price - current_price
    else:
        sl_price = current_price + sl_distance
        tp_price = float(active_res.get("take_profit_price", current_price - (sl_distance * 2.2)))
        tp_distance = current_price - tp_price

    calculated_rrr = tp_distance / sl_distance if sl_distance > 0 else 0.0

    if calculated_rrr < MIN_RRR:
        return {
            "approved": False,
            "reason": f"Discarded: Calculated RRR is 1:{calculated_rrr:.2f} (Minimum required: 1:{MIN_RRR:.1f})."
        }

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
    print("🚀 DUAL-AI ENGINE v4.5 ONLINE (TELEGRAM SIGNAL ENGINE)")

    while True:
        try:
            await asyncio.sleep(60)
            now_utc = datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            if not is_within_killzone():
                continue

            if await check_news_guardrail():
                continue

            df_5m = await fetch_deriv_candles(granularity=300, count=200)
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50)

            if df_5m.empty or df_h1.empty:
                continue

            current_price = df_5m['close'].iloc[-1]

            # 🛑 LOCKOUT CHECK: Is a setup currently playing out?
            if update_and_check_active_setup(current_price):
                continue

            atr_val = calculate_atr(df_5m, period=14)
            chart_bytes = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1)

            consensus = await get_dual_ai_consensus(chart_bytes, current_price, atr_val)

            if not consensus["approved"]:
                print(f"[AI NO-TRADE] {consensus['reason']}")
                continue

            direction = consensus["direction"]

            print(f"[AI APPROVED SETUP] Broadcasting {direction} @ {current_price:.2f}")

            last_trade_time = now_utc
            
            # Lock state tracking so duplicate setups are blocked until target or stop is hit
            active_setup["is_active"] = True
            active_setup["direction"] = direction
            active_setup["sl_price"] = consensus["sl_price"]
            active_setup["tp_price"] = consensus["tp_price"]

            # Broadcast structured signal formatted for manual or Telegram Copier execution
            msg = (
                f"🎯 *DUAL-AI SIGNAL v4.5*\n"
                f"⚡ *HIGH-CONFLUENCE SMC SETUP*\n\n"
                f"🏆 *Asset:* `XAUUSD (Gold)`\n"
                f"⚔️ *Action:* `{direction}`\n"
                f"📍 *Entry Price:* `${current_price:.2f}`\n"
                f"🛑 *Stop Loss:* `${consensus['sl_price']:.2f}`\n"
                f"🎯 *Take Profit:* `${consensus['tp_price']:.2f}`\n"
                f"⚖️ *Target RRR:* `{consensus['rrr_str']}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *STRATEGY & REASONING*\n"
                f"📌 *Setup:* `{consensus['strategy']}`\n"
                f"_{consensus['reason']}_\n"
            )
            await send_telegram_alert(msg, chart_bytes)

        except Exception as err:
            print(f"[WORKER ERROR] {err}")
            await asyncio.sleep(15)

# ==================== FASTAPI APP LIFECYCLE ==================== #
@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(deriv_trading_worker())
    yield
    worker_task.cancel()
    await http_client.aclose()

app = FastAPI(title="Dual-AI Engine", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "DUAL_AI_ENGINE_ONLINE", "version": "4.5"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
