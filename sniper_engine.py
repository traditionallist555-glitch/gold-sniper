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
from PIL import Image
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn

# Google Gen AI SDK (Official package)
from google import genai
from google.genai import types

# ==================== ENVIRONMENT CONFIGURATION ==================== #
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROK_API_KEY = os.getenv("GROK_API_KEY", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

SYMBOL = "frxXAUUSD"      # Gold symbol on Deriv
STAKE_AMOUNT = 2.00        # Fixed Stake $2.00 per trade
SL_AMOUNT = 2.00           # Target loss cap ($2.00)
COOLDOWN_MINUTES = 5       # Evaluation interval (5m candle cycles)

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)

# Shared HTTP client for Telegram, Grok, and Finnhub APIs
http_client = httpx.AsyncClient(timeout=20.0)

# Initialize Gemini Client (Using official google-genai SDK)
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None

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
            res = json.loads(await ws.recv())
            return res
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
    proposal = proposal_res.get("proposal", {})
    proposal_id = proposal.get("id")

    if not proposal_id:
        err_msg = proposal_res.get('error', {}).get('message', 'Unknown Broker Error')
        print(f"[DERIV PROPOSAL FAILED] {err_msg}")
        return {"status": "FAILED", "reason": err_msg}

    buy_req = {
        "buy": proposal_id,
        "price": STAKE_AMOUNT
    }
    buy_res = await deriv_request(buy_req, authorize=True)
    if "buy" in buy_res:
        return {"status": "EXECUTED", "contract_id": buy_res["buy"].get("contract_id")}
    return {"status": "FAILED", "reason": "Buy execution failed"}

# ==================== NEWS & SESSION GUARDRAILS ==================== #
async def check_news_guardrail() -> bool:
    """Pauses trading if USD high-impact news occurs within ±15 minutes."""
    if not FINNHUB_API_KEY:
        return False  # Pass through if news API key isn't set

    try:
        url = f"https://finnhub.io/api/v1/calendar/economic?token={FINNHUB_API_KEY}"
        res = await http_client.get(url)
        if res.status_code == 200:
            events = res.json().get("economicCalendar", [])
            now_utc = datetime.now(timezone.utc)
            
            for event in events:
                if event.get("country") == "US" and event.get("impact") == "high":
                    event_time_str = event.get("time")
                    if event_time_str:
                        event_dt = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
                        time_diff = abs((event_dt - now_utc).total_seconds()) / 60.0
                        if time_diff <= 15:
                            print(f"[NEWS GUARDRAIL TRIGGERED] {event.get('event')} in {time_diff:.1f} mins.")
                            return True
    except Exception as e:
        print(f"[NEWS CHECK WARNING] {e}")

    return False

# ==================== DUAL-PANEL MULTI-TIMEFRAME CHART GENERATOR ==================== #
def render_dual_panel_chart(df_5m: pd.DataFrame, df_h1: pd.DataFrame) -> bytes:
    """Renders 1H Macro Context (Left) and 5M Local Structure (Right) in one PNG."""
    chart_5m = df_5m.tail(50).copy()
    chart_h1 = df_h1.tail(30).copy()

    for df in [chart_5m, chart_h1]:
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)

    mc = mpf.make_marketcolors(up='#089981', down='#F23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=False)

    fig = mpf.figure(figsize=(14, 6), style=style)
    ax1 = fig.add_subplot(1, 2, 1)
    ax2 = fig.add_subplot(1, 2, 2)

    mpf.plot(chart_h1, type='candle', ax=ax1, axtitle="1-Hour Macro Structure (Context)")
    mpf.plot(chart_5m, type='candle', ax=ax2, axtitle="5-Minute Local Structure (Execution)")

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ==================== DUAL-AI VISION & SMC ENGINE ==================== #
SYSTEM_PROMPT = """
You are an elite Smart Money Concepts (SMC), ICT, and Elliott Wave Trader evaluating Gold (XAUUSD).
Look at the multi-panel chart image (Left: 1H Macro Context, Right: 5M Execution).

Your Task:
1. Identify institutional market structures: Fair Value Gaps (FVG), Order Blocks (OB), Liquidity Sweeps, and Market Structure Shifts (MSS).
2. Look for high-probability entries following a clear liquidity sweep.
3. Reject trades if price action is choppy, stuck in horizontal range, or near liquidity traps.
4. Set an precise Stop Loss (SL) near recent market structure invalidation.
5. Set an realistic Take Profit (TP) target (RRR between 1:1.2 to 1:3.0).

Respond ONLY in this exact JSON format:
{
  "trade_approved": true/false,
  "direction": "MULTUP" or "MULTDOWN",
  "stop_loss_price": float,
  "take_profit_price": float,
  "risk_reward_ratio": "1:2.0",
  "strategy_detected": "5M Liquidity Sweep into 1H FVG",
  "reason": "Brief technical analysis explanation..."
}
"""

async def evaluate_with_gemini(chart_bytes: bytes, market_summary: str) -> dict:
    if not gemini_client:
        return {"trade_approved": False, "reason": "Gemini Key missing", "failed": True}

    try:
        prompt_content = f"{SYSTEM_PROMPT}\n\nLive Market Summary: {market_summary}"
        
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model="gemini-1.5-flash",  # Fixed stable model endpoint
            contents=[
                types.Part.from_bytes(data=chart_bytes, mime_type="image/png"),
                prompt_content
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        parsed = json.loads(response.text)
        parsed["failed"] = False
        return parsed
    except Exception as e:
        print(f"[GEMINI VISION ERROR] {e}")
        return {"trade_approved": False, "reason": f"Gemini Error: {e}", "failed": True}

async def evaluate_with_grok(chart_bytes: bytes, market_summary: str) -> dict:
    if not GROK_API_KEY:
        return {"trade_approved": False, "reason": "Grok Key missing", "failed": True}

    try:
        base64_img = base64.b64encode(chart_bytes).decode('utf-8')
        headers = {
            "Authorization": f"Bearer {GROK_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "grok-2-vision",  # Fixed xAI endpoint name
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{SYSTEM_PROMPT}\n\nLive Market Summary: {market_summary}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}}
                    ]
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }
        res = await http_client.post("https://api.x.ai/v1/chat/completions", headers=headers, json=payload)
        if res.status_code == 200:
            raw_text = res.json()["choices"][0]["message"]["content"]
            parsed = json.loads(raw_text)
            parsed["failed"] = False
            return parsed
        else:
            print(f"[GROK HTTP {res.status_code}] {res.text[:100]}")
    except Exception as e:
        print(f"[GROK VISION ERROR] {e}")

    return {"trade_approved": False, "reason": "Grok API error", "failed": True}

async def get_dual_ai_consensus(chart_bytes: bytes, current_price: float) -> dict:
    market_summary = f"Current Price: {current_price:.2f} USD"

    gemini_res, grok_res = await asyncio.gather(
        evaluate_with_gemini(chart_bytes, market_summary),
        evaluate_with_grok(chart_bytes, market_summary)
    )

    g_fail = gemini_res.get("failed", True)
    x_fail = grok_res.get("failed", True)

    g_app = gemini_res.get("trade_approved", False)
    x_app = grok_res.get("trade_approved", False)

    # Failover / Consensus Check Logic
    if g_fail and not x_fail:
        active_res = grok_res
        consensus_approved = x_app
        consensus_reason = f"Grok Only: {grok_res.get('reason')}"
    elif x_fail and not g_fail:
        active_res = gemini_res
        consensus_approved = g_app
        consensus_reason = f"Gemini Only: {gemini_res.get('reason')}"
    elif g_fail and x_fail:
        return {"approved": False, "reason": "Both AI Vision models failed or timed out."}
    else:
        # Both models active -> Ensure BOTH agree on direction and approval
        same_direction = gemini_res.get("direction") == grok_res.get("direction")
        consensus_approved = g_app and x_app and same_direction
        active_res = gemini_res  # Default primary params from Gemini
        consensus_reason = f"Gemini: {gemini_res.get('reason')} | Grok: {grok_res.get('reason')}"

    if not consensus_approved:
        return {"approved": False, "reason": consensus_reason}

    # Derive dynamic risk metrics from AI parameters
    sl_price = float(active_res.get("stop_loss_price", current_price))
    tp_price = float(active_res.get("take_profit_price", current_price))
    sl_points = abs(current_price - sl_price)
    tp_points = abs(tp_price - current_price)

    if sl_points <= 0.5:
        return {"approved": False, "reason": "AI calculated unviable/too tight Stop Loss."}

    # Map SL price distance to Deriv Multiplier Leverage Step
    calc_mult = int((SL_AMOUNT * current_price) / (STAKE_AMOUNT * sl_points))
    valid_multipliers = [10, 20, 30, 50, 100]
    final_multiplier = min(valid_multipliers, key=lambda x: abs(x - calc_mult))

    # Dynamic Take Profit dollar calculation matching target ratio
    rrr_ratio = tp_points / sl_points if sl_points > 0 else 1.5
    calculated_tp_dollars = round(SL_AMOUNT * rrr_ratio, 2)

    return {
        "approved": True,
        "direction": active_res.get("direction"),
        "entry_price": current_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "multiplier": final_multiplier,
        "tp_dollars": calculated_tp_dollars,
        "rrr_str": f"1:{rrr_ratio:.1f}",
        "strategy": active_res.get("strategy_detected", "SMC Multi-Confluence"),
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
    print("🚀 DUAL-AI DISCRETION ENGINE v4.0 ONLINE (GEMINI + GROK VISION)")

    while True:
        try:
            await asyncio.sleep(60)
            now_utc = datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            # 1. Check High-Impact News Guardrail
            if await check_news_guardrail():
                continue

            # 2. Fetch Multi-Timeframe Candles
            df_5m = await fetch_deriv_candles(granularity=300, count=200)
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50)

            if df_5m.empty or df_h1.empty:
                continue

            current_price = df_5m['close'].iloc[-1]

            # 3. Render Multi-Timeframe Chart PNG
            chart_bytes = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1)

            # 4. Run Dual-AI SMC & Discretion Engine
            consensus = await get_dual_ai_consensus(chart_bytes, current_price)

            if not consensus["approved"]:
                print(f"[AI NO-TRADE] {consensus['reason']}")
                continue

            # 5. Consensus Reached -> Execute Trade
            last_trade_time = now_utc
            direction = consensus["direction"]
            multiplier = consensus["multiplier"]
            tp_dollars = consensus["tp_dollars"]

            print(f"[AI APPROVED SETUP] Executing {direction} @ {current_price:.2f} ({multiplier}x)")

            trade_res = await place_deriv_multiplier_trade(direction, multiplier, tp_dollars)
            trade_status = trade_res.get("status", "FAILED")
            contract_id = trade_res.get("contract_id", "N/A")

            # 6. Broadcast Signal to Telegram
            msg = (
                f"🎯 *DUAL-AI DISCRETION ENGINE v4.0*\n"
                f"⚡ *HIGH-CONFLUENCE SMC SETUP*\n\n"
                f"🏆 *Asset:* `XAUUSD (Gold)`\n"
                f"⚔️ *Action:* `{direction}`\n"
                f"📍 *Entry Price:* `{current_price:.2f}`\n"
                f"🛑 *Stop Loss:* `{consensus['sl_price']:.2f}`\n"
                f"🎯 *Take Profit:* `{consensus['tp_price']:.2f}`\n"
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

app = FastAPI(title="Dual-AI Discretion Engine", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "DUAL_AI_DISCRETION_ENGINE_ONLINE"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
