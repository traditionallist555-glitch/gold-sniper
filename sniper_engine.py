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
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn
import google.generativeai as genai

# ==================== ENVIRONMENT CONFIGURATION ==================== #
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROK_API_KEY = os.getenv("GROK_API_KEY", "").strip()

SYMBOL = "frxXAUUSD"      # Gold symbol on Deriv
STAKE_AMOUNT = 2.00        # Stake $2.00 per trade
SL_AMOUNT = 2.00           # Target loss cap ($2.00)
TP_AMOUNT = 6.00           # Target gain cap ($6.00)
COOLDOWN_MINUTES = 10      # Cooldown between trade evaluations

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)

# Shared HTTP client for Telegram and Grok API calls
http_client = httpx.AsyncClient(timeout=15.0)

# Configure Gemini Model
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    gemini_model = None

# ==================== DERIV WEBSOCKET CALLS ==================== #
async def deriv_request(req: dict, authorize: bool = False) -> dict:
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            if authorize:
                if not DERIV_API_TOKEN:
                    print("[DERIV AUTH ERROR] DERIV_API_TOKEN environment variable is missing!")
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

async def place_deriv_multiplier_trade(trade_type: str, dynamic_multiplier: int) -> dict:
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
            "take_profit": TP_AMOUNT
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

# ==================== STRATEGY & DYNAMIC RISK CALCULATIONS ==================== #
def get_h1_macro_bias(df_h1: pd.DataFrame) -> str:
    if len(df_h1) < 20:
        return "NEUTRAL"
    df_h1['ema20'] = df_h1['close'].ewm(span=20, adjust=False).mean()
    last_close = df_h1['close'].iloc[-1]
    last_ema = df_h1['ema20'].iloc[-1]
    return "BULLISH" if last_close > last_ema else ("BEARISH" if last_close < last_ema else "NEUTRAL")

def detect_elliott_waves(df: pd.DataFrame) -> dict:
    if len(df) < 20:
        return {"current_wave": "UNKNOWN", "bias": "NEUTRAL"}
    highs, lows = df['high'].values, df['low'].values
    swing_highs, swing_lows = [], []

    for i in range(2, len(df) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1]:
            swing_highs.append((i, highs[i]))
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1]:
            swing_lows.append((i, lows[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"current_wave": "DEVELOPMENT", "bias": "NEUTRAL"}

    h1, h2 = swing_highs[-2][1], swing_highs[-1][1]
    l1, l2 = swing_lows[-2][1], swing_lows[-1][1]

    if h2 > h1 and l2 > l1:
        return {"current_wave": "WAVE_3_EXPANSION", "bias": "BULLISH"}
    elif h2 < h1 and l2 < l1:
        return {"current_wave": "WAVE_3_EXPANSION", "bias": "BEARISH"}

    return {"current_wave": "DEVELOPMENT", "bias": "NEUTRAL"}

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(period).mean()

def calculate_dynamic_risk(close_price: float, current_atr: float):
    sl_points = max(current_atr * 2.5, 12.0)
    tp_points = sl_points * 3.0
    
    calculated_multiplier = int((SL_AMOUNT * close_price) / (STAKE_AMOUNT * sl_points))
    
    # Deriv Multiplier valid discrete steps
    valid_multipliers = [10, 20, 30, 50, 100]
    final_multiplier = min(valid_multipliers, key=lambda x: abs(x - calculated_multiplier))

    return {
        "sl_points": sl_points,
        "tp_points": tp_points,
        "multiplier": final_multiplier
    }

def render_chart_image(df_5m: pd.DataFrame, setup_name: str, entry: float, sl: float, tp: float) -> bytes:
    chart_df = df_5m.tail(45).copy()
    chart_df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)

    mc = mpf.make_marketcolors(up='#089981', down='#F23645', edge='inherit', wick='inherit')
    s = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=False)
    h_lines = dict(hlines=[entry, sl, tp], colors=['#2962FF', '#F23645', '#089981'], linestyle='--')

    fig, _ = mpf.plot(
        chart_df,
        type='candle',
        style=s,
        hlines=h_lines,
        figsize=(10, 5),
        returnfig=True,
        title=f"\nClimax Sniper Alert: {setup_name}"
    )

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ==================== VISUAL AI SCANNING ENGINE ==================== #
async def evaluate_with_gemini_vision(chart_bytes: bytes, prompt: str) -> dict:
    if not gemini_model:
        return {"approved": False, "reason": "Gemini Key missing", "failed": True}
    
    for attempt in range(3):
        try:
            image = Image.open(io.BytesIO(chart_bytes))
            response = await asyncio.to_thread(gemini_model.generate_content, [prompt, image])
            clean = response.text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                parsed["failed"] = False
                return parsed
        except Exception as e:
            print(f"[GEMINI VISION ERROR attempt {attempt+1}] {e}")
            await asyncio.sleep(2)
            
    return {"approved": False, "reason": "Gemini Vision Error / High Demand", "failed": True}

async def evaluate_with_grok_vision(chart_bytes: bytes, prompt: str) -> dict:
    if not GROK_API_KEY:
        return {"approved": False, "reason": "Grok Key missing", "failed": True}

    base64_img = base64.b64encode(chart_bytes).decode('utf-8')
    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "grok-2-vision-1212",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{base64_img}"}
                    }
                ]
            }
        ],
        "temperature": 0.1
    }

    try:
        res = await http_client.post("https://api.x.ai/v1/chat/completions", headers=headers, json=payload)
        if res.status_code == 200:
            res_data = res.json()
            raw_text = res_data["choices"][0]["message"]["content"]
            clean = raw_text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                parsed["failed"] = False
                return parsed
        else:
            print(f"[GROK VISION HTTP {res.status_code}] {res.text[:100]}")
    except Exception as e:
        print(f"[GROK VISION ERROR] {e}")

    return {"approved": False, "reason": "Grok Vision API Error", "failed": True}

async def get_dual_ai_approval(df_5m: pd.DataFrame, h1_bias: str, proposed_signal: str, chart_bytes: bytes) -> dict:
    prompt = f"""
    You are an expert Smart Money Concepts (SMC) & Elliott Wave Trader.
    Analyze this candlestick chart image for XAUUSD (Gold).

    Current 1H Bias: {h1_bias}
    Proposed Signal: {proposed_signal}

    Visual Task:
    1. Scan the candlestick price action directly on the image.
    2. Check if the market is stuck in horizontal, messy consolidation/chop.
    3. Verify if market structure visually aligns with the proposed {proposed_signal}.

    Respond ONLY in strict JSON format:
    {{"approved": true, "reason": "Short visual analysis confirmation..."}}
    """

    gemini_res, grok_res = await asyncio.gather(
        evaluate_with_gemini_vision(chart_bytes, prompt),
        evaluate_with_grok_vision(chart_bytes, prompt)
    )

    g_fail = gemini_res.get("failed", True)
    x_fail = grok_res.get("failed", True)

    g_app = gemini_res.get("approved", False)
    x_app = grok_res.get("approved", False)

    # Consensus and Failover Resolution Logic
    if g_fail and not x_fail:
        final_approval = x_app
        combined_reason = f"Grok Vision Only: {grok_res.get('reason', 'N/A')}"
    elif x_fail and not g_fail:
        final_approval = g_app
        combined_reason = f"Gemini Vision Only: {gemini_res.get('reason', 'N/A')}"
    elif g_fail and x_fail:
        final_approval = False
        combined_reason = "Both AI Vision models failed or timed out."
    else:
        # Both models available -> require mutual agreement (CONSENSUS)
        final_approval = g_app and x_app
        combined_reason = f"Gemini: {gemini_res.get('reason', 'N/A')} | Grok: {grok_res.get('reason', 'N/A')}"

    return {"approved": final_approval, "reason": combined_reason}

# ==================== TELEGRAM NOTIFICATIONS ==================== #
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
    print("🚀 CLIMAX SNIPER DETECTOR v3.0 RUNNING (VISUAL ENGINE ENABLED)")

    while True:
        try:
            await asyncio.sleep(60)
            now_utc = datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            df_5m = await fetch_deriv_candles(granularity=300, count=200)
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50)

            if df_5m.empty or df_h1.empty:
                continue

            df_5m['atr'] = calculate_atr(df_5m)
            current_atr = df_5m['atr'].iloc[-1]
            close_price = df_5m['close'].iloc[-1]

            h1_bias = get_h1_macro_bias(df_h1)
            elliott_data = detect_elliott_waves(df_5m)

            signal_type = None
            deriv_contract = None

            risk_params = calculate_dynamic_risk(close_price, current_atr)
            sl_points = risk_params["sl_points"]
            tp_points = risk_params["tp_points"]
            dynamic_multiplier = risk_params["multiplier"]

            if elliott_data['bias'] == 'BULLISH' and h1_bias == 'BULLISH':
                signal_type = f"BUY_{elliott_data['current_wave']}"
                deriv_contract = "MULTUP"
                entry = close_price
                sl_price = entry - sl_points
                tp_price = entry + tp_points

            elif elliott_data['bias'] == 'BEARISH' and h1_bias == 'BEARISH':
                signal_type = f"SELL_{elliott_data['current_wave']}"
                deriv_contract = "MULTDOWN"
                entry = close_price
                sl_price = entry + sl_points
                tp_price = entry - tp_points

            if signal_type:
                print(f"[SETUP FOUND] {signal_type} @ {entry:.2f} | Multiplier: {dynamic_multiplier}x")

                last_trade_time = now_utc

                # Render chart image for Visual Scanning
                chart_bytes = await asyncio.to_thread(
                    render_chart_image, df_5m, signal_type, entry, sl_price, tp_price
                )

                # Send chart image to Gemini and Grok for Visual Scanning
                ai_eval = await get_dual_ai_approval(df_5m, h1_bias, signal_type, chart_bytes)
                
                if not ai_eval["approved"]:
                    print(f"[AI VETO] Signal Rejected: {ai_eval['reason']}")
                    continue

                print(f"[AI CONSENSUS APPROVED] Reason: {ai_eval['reason']}")

                trade_res = await place_deriv_multiplier_trade(deriv_contract, dynamic_multiplier)
                trade_status = trade_res.get("status", "FAILED")
                contract_id = trade_res.get("contract_id", "N/A")

                msg = (
                    f"🎯 *CLIMAX SNIPER DETECTOR v3.0*\n"
                    f"⚡ *HIGH-PROBABILITY ENGINE SETUP*\n\n"
                    f"🏆 *Asset:* `XAUUSD (Gold)`\n"
                    f"⚔️ *Action:* `{signal_type}`\n"
                    f"📍 *Entry Price:* `{entry:.2f}`\n"
                    f"🛑 *Stop Loss Level:* `{sl_price:.2f}` (-{sl_points:.2f} pts)\n"
                    f"🎯 *Take Profit Level:* `{tp_price:.2f}` (+{tp_points:.2f} pts)\n"
                    f"🚀 *Dynamic Leverage:* `{dynamic_multiplier}x Multiplier`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🧠 *VISUAL AI CONSENSUS REVIEW*\n"
                    f"_{ai_eval['reason']}_\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💵 *Stake Amount:* `${STAKE_AMOUNT:.2f}`\n"
                    f"🛡️ *Max Loss Risk:* `-${SL_AMOUNT:.2f}`\n"
                    f"💰 *Target Gain:* `+${TP_AMOUNT:.2f}`\n"
                    f"⚖️ *Risk/Reward Ratio:* `1:3 RRR`\n"
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

app = FastAPI(title="Climax Sniper Detector", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "CLIMAX_SNIPER_DETECTOR_ONLINE"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
