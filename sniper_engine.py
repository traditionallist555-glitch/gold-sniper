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
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn

from google import genai
from google.genai import types

# ==================== ENVIRONMENT CONFIGURATION ==================== #
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "61048").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

SYMBOL = "frxXAUUSD"      # Gold symbol on Deriv
STAKE_AMOUNT = 2.00        # Fixed Stake $2.00 per trade
SL_AMOUNT = 2.00           # Target loss cap ($2.00)
MIN_RRR = 2.0              # Dynamic minimum 1:2 Risk-Reward Ratio
COOLDOWN_MINUTES = 5       # Evaluation interval (5m candle cycles)

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)

http_client = httpx.AsyncClient(timeout=25.0)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==================== KILLZONE SESSION FILTER ==================== #
def is_within_killzone() -> bool:
    """
    Filters trades to high-volume volatility windows:
    - London Open: 07:00 - 11:00 UTC
    - New York Session: 13:00 - 17:00 UTC
    """
    now_utc = datetime.now(timezone.utc).time()
    
    london_start = datetime.strptime("07:00", "%H:%M").time()
    london_end = datetime.strptime("11:00", "%H:%M").time()
    ny_start = datetime.strptime("13:00", "%H:%M").time()
    ny_end = datetime.strptime("17:00", "%H:%M").time()
    
    return (london_start <= now_utc <= london_end) or (ny_start <= now_utc <= ny_end)

# ==================== DYNAMIC ATR VOLATILITY CALCULATOR ==================== #
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculates 14-period Average True Range for dynamic stops."""
    high, low, close = df['high'], df['low'], df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(window=period).mean().iloc[-1])

# ==================== DERIV WEBSOCKET API CALLS ==================== #
async def deriv_request(req: dict, authorize: bool = False) -> dict:
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            if authorize:
                if not DERIV_API_TOKEN:
                    print("[DERIV AUTH ERROR] DERIV_API_TOKEN is missing!")
                    return {}
                await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
                auth_res = json.loads(await ws.recv())
                if "error" in auth_res:
                    print(f"[DERIV AUTH ERROR] {auth_res['error']['message']}")
                    return {}

            await ws.send(json.dumps(req))
            return json.loads(await ws.recv())
    except Exception as err:
        print(f"[DERIV WS ERROR] {err}")
        return {}

async def check_active_positions() -> bool:
    """Checks portfolio to prevent duplicate concurrent positions."""
    req = {"portfolio": 1}
    res = await deriv_request(req, authorize=True)
    portfolio = res.get("portfolio", {}).get("contracts", [])
    for contract in portfolio:
        if contract.get("symbol") == SYMBOL:
            return True
    return False

async def fetch_deriv_candles(granularity: int = 300, count: int = 200) -> pd.DataFrame:
    req = {
        "ticks_history": SYMBOL,
        "adjust_start_time": 1,
        "count": count,
        "end": "latest",
        "granularity": granularity,
        "style": "candles"
    }
    res = await deriv_request(req, authorize=False)
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

async def place_deriv_multiplier_trade(trade_type: str, dynamic_multiplier: int, tp_dollar_amount: float) -> dict:
    proposal_req = {
        "proposal": 1,
        "amount": STAKE_AMOUNT,
        "basis": "stake",
        "contract_type": trade_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "multiplier": dynamic_multiplier,
        "limit_order": {
            "stop_loss": SL_AMOUNT,
            "take_profit": round(tp_dollar_amount, 2)
        }
    }
    proposal_res = await deriv_request(proposal_req, authorize=True)
    proposal_id = proposal_res.get("proposal", {}).get("id")

    if not proposal_id:
        err_msg = proposal_res.get('error', {}).get('message', 'Unknown Broker Error')
        print(f"[DERIV PROPOSAL FAILED] {err_msg}")
        return {"status": "FAILED", "reason": err_msg}

    buy_req = {"buy": proposal_id, "price": STAKE_AMOUNT}
    buy_res = await deriv_request(buy_req, authorize=True)
    if "buy" in buy_res:
        return {"status": "EXECUTED", "contract_id": buy_res["buy"].get("contract_id")}
    return {"status": "FAILED", "reason": "Buy execution failed"}

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
                                print(f"[NEWS GUARDRAIL TRIGGERED] {event.get('event')} in {time_diff:.1f} mins.")
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

    mpf.plot(chart_h1, type='candle', ax=ax1, addplot=addplots_h1, axtitle="1-Hour Macro Structure (Context + 200 EMA)")
    mpf.plot(chart_5m, type='candle', ax=ax2, axtitle="5-Minute Local Structure (Execution)")

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ==================== DUAL-AI VISION ENGINE ==================== #
SYSTEM_PROMPT = """
You are an unconstrained price-action analyzer for Gold (XAUUSD).
Examine the multi-panel chart (Left: 1H Macro Context, Right: 5M Local Structure).

Your Task:
1. Scan for institutional SMC setups (Liquidity Sweeps, Order Blocks, Fair Value Gaps, or Market Structure Shifts).
2. Determine direction: "MULTUP" (Buy) or "MULTDOWN" (Sell).
3. Identify structural Stop Loss level (SL) and realistic Take Profit level (TP).

Respond ONLY in valid raw JSON matching this format:
{
  "trade_approved": true/false,
  "direction": "MULTUP" or "MULTDOWN",
  "stop_loss_price": float,
  "take_profit_price": float,
  "strategy_detected": "Liquidity Sweep into FVG",
  "reason": "Brief technical analysis explanation..."
}
"""

def sync_gemini_generate(chart_bytes: bytes, prompt_content: str, model_name: str) -> dict:
    response = gemini_client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_bytes(data=chart_bytes, mime_type="image/png"),
            prompt_content
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    return json.loads(response.text)

async def evaluate_with_gemini(chart_bytes: bytes, market_summary: str) -> dict:
    if not gemini_client:
        return {"trade_approved": False, "reason": "Gemini Key missing", "failed": True}

    prompt_content = f"{SYSTEM_PROMPT}\n\nLive Market Summary: {market_summary}"

    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
        try:
            parsed = await asyncio.to_thread(sync_gemini_generate, chart_bytes, prompt_content, model_name)
            parsed["failed"] = False
            return parsed
        except Exception as err:
            print(f"[GEMINI MODEL {model_name} ERROR] {err}")
            continue

    return {"trade_approved": False, "reason": "Gemini models failed", "failed": True}

async def evaluate_with_openrouter_free(chart_bytes: bytes, market_summary: str) -> dict:
    if not OPENROUTER_API_KEY:
        return {"trade_approved": False, "reason": "OpenRouter Key missing", "failed": True}

    base64_img = base64.b64encode(chart_bytes).decode('utf-8')
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://deriv-bot.local", 
        "X-Title": "Deriv Trading Engine"
    }

    free_vision_models = ["google/gemini-2.5-flash:free", "openrouter/free"]

    for model_name in free_vision_models:
        try:
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"{SYSTEM_PROMPT}\n\nLive Market Summary: {market_summary}"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}}
                        ]
                    }
                ],
                "response_format": {"type": "json_object"}
            }
            res = await http_client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
            if res.status_code == 200:
                raw_text = res.json()["choices"][0]["message"]["content"]
                parsed = json.loads(raw_text)
                parsed["failed"] = False
                return parsed
        except Exception as e:
            print(f"[OPENROUTER API ERROR] {e}")

    return {"trade_approved": False, "reason": "OpenRouter free vision API timeout or error", "failed": True}

async def get_dual_ai_consensus(chart_bytes: bytes, current_price: float, atr_val: float) -> dict:
    market_summary = f"Current Price: {current_price:.2f} USD | 14-ATR: {atr_val:.2f}"

    gemini_res, openrouter_res = await asyncio.gather(
        evaluate_with_gemini(chart_bytes, market_summary),
        evaluate_with_openrouter_free(chart_bytes, market_summary)
    )

    g_fail = gemini_res.get("failed", True)
    o_fail = openrouter_res.get("failed", True)

    g_app = gemini_res.get("trade_approved", False)
    o_app = openrouter_res.get("trade_approved", False)

    # Fallback Handling
    if g_fail and not o_fail:
        active_res = openrouter_res
        consensus_approved = o_app
        consensus_reason = f"OpenRouter: {openrouter_res.get('reason')}"
    elif o_fail and not g_fail:
        active_res = gemini_res
        consensus_approved = g_app
        consensus_reason = f"Gemini: {gemini_res.get('reason')}"
    elif g_fail and o_fail:
        return {"approved": False, "reason": "Both AI Vision endpoints failed."}
    else:
        same_direction = gemini_res.get("direction") == openrouter_res.get("direction")
        consensus_approved = g_app and o_app and same_direction
        active_res = gemini_res
        consensus_reason = f"Gemini: {gemini_res.get('reason')} | OpenRouter: {openrouter_res.get('reason')}"

    if not consensus_approved:
        return {"approved": False, "reason": consensus_reason}

    # Dynamic ATR Distance Guardrail (SL based on 2x ATR)
    sl_distance = max(atr_val * 2.0, 1.50)
    direction = active_res.get("direction")

    if direction == "MULTUP":
        sl_price = current_price - sl_distance
        tp_price = float(active_res.get("take_profit_price", current_price + (sl_distance * 2.2)))
        tp_distance = tp_price - current_price
    else:
        sl_price = current_price + sl_distance
        tp_price = float(active_res.get("take_profit_price", current_price - (sl_distance * 2.2)))
        tp_distance = current_price - tp_price

    # Hard Enforcement: Minimum 1:2 RRR Rule
    calculated_rrr = tp_distance / sl_distance if sl_distance > 0 else 0.0

    if calculated_rrr < MIN_RRR:
        return {
            "approved": False, 
            "reason": f"Discarded: Calculated RRR is 1:{calculated_rrr:.2f} (Minimum required is 1:{MIN_RRR:.1f})."
        }

    # Deriv Multiplier Auto-Scale Calculation
    raw_mult = int((SL_AMOUNT * current_price) / (STAKE_AMOUNT * sl_distance))
    valid_multipliers = [10, 20, 30, 50, 100, 200, 300]
    final_multiplier = min(valid_multipliers, key=lambda x: abs(x - raw_mult))

    calculated_tp_dollars = round(SL_AMOUNT * calculated_rrr, 2)

    return {
        "approved": True,
        "direction": direction,
        "entry_price": current_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "multiplier": final_multiplier,
        "tp_dollars": calculated_tp_dollars,
        "rrr_str": f"1:{calculated_rrr:.2f}",
        "strategy": active_res.get("strategy_detected", "Dynamic Dynamic SMC Setup"),
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
    global last_trade_time
    print("🚀 DUAL-AI ENGINE v4.3 ONLINE (KILLZONE + ATR DYNAMIC RRR)")

    while True:
        try:
            await asyncio.sleep(60)
            now_utc = datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            # 1. Killzone Filter
            if not is_within_killzone():
                print("[FILTERED] Outside London/NY Killzones.")
                continue

            # 2. Check Active Positions
            if await check_active_positions():
                print("[WORKER] Active position open on Deriv. Waiting for close.")
                continue

            # 3. News Guardrail Check
            if await check_news_guardrail():
                continue

            # 4. Fetch Market Data
            df_5m = await fetch_deriv_candles(granularity=300, count=200)
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50)

            if df_5m.empty or df_h1.empty:
                continue

            current_price = df_5m['close'].iloc[-1]
            atr_val = calculate_atr(df_5m, period=14)

            # 5. Render Chart & Evaluate Consensus
            chart_bytes = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1)
            consensus = await get_dual_ai_consensus(chart_bytes, current_price, atr_val)

            if not consensus["approved"]:
                print(f"[AI NO-TRADE] {consensus['reason']}")
                continue

            last_trade_time = now_utc
            direction = consensus["direction"]
            multiplier = consensus["multiplier"]
            tp_dollars = consensus["tp_dollars"]

            print(f"[AI APPROVED SETUP] Executing {direction} @ {current_price:.2f} ({multiplier}x)")

            # 6. Execute Order
            trade_res = await place_deriv_multiplier_trade(direction, multiplier, tp_dollars)
            trade_status = trade_res.get("status", "FAILED")
            contract_id = trade_res.get("contract_id", "N/A")

            # 7. Broadcast Telegram Payload
            msg = (
                f"🎯 *DUAL-AI ENGINE v4.3*\n"
                f"⚡ *HIGH-CONFLUENCE SMC SETUP*\n\n"
                f"🏆 *Asset:* `XAUUSD (Gold)`\n"
                f"⚔️ *Action:* `{direction}`\n"
                f"📍 *Entry Price:* `${current_price:.2f}`\n"
                f"🛑 *Stop Loss:* `${consensus['sl_price']:.2f}`\n"
                f"🎯 *Take Profit:* `${consensus['tp_price']:.2f}`\n"
                f"🚀 *Leverage:* `{multiplier}x Multiplier`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *STRATEGY & AI REASONING*\n"
                f"📌 *Setup:* `{consensus['strategy']}`\n"
                f"_{consensus['reason']}_\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💵 *Stake:* `${STAKE_AMOUNT:.2f}`\n"
                f"🛡️ *Risk Cap:* `-${SL_AMOUNT:.2f}`\n"
                f"💰 *Target Gain:* `+${tp_dollars:.2f}`\n"
                f"⚖️ *Target RRR:* `{consensus['rrr_str']}`\n"
                f"📡 *Execution Status:* `{trade_status}` (ID: `{contract_id}`)\n"
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
    return {"status": "DUAL_AI_ENGINE_ONLINE", "version": "4.3"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
