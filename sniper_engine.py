import os
import io
import json
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

# ==================== ENVIRONMENT CONFIGURATION ==================== #
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROK_API_KEY = os.getenv("GROK_API_KEY", "").strip()

SYMBOL = "frxXAUUSD"      # Gold symbol on Deriv
STAKE_AMOUNT = 2.00        # Stake $2.00 per trade
SL_AMOUNT = 2.00           # Max loss cap (-$2.00)
TP_AMOUNT = 6.00           # Target gain (+ $6.00)
COOLDOWN_MINUTES = 5       # Reduced to catch ~1+ trade per day

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)

# Shared HTTP client for Telegram and Grok API calls
http_client = httpx.AsyncClient(timeout=12.0)

# Initialize Gemini Client
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==================== DERIV WEBSOCKET CALLS ==================== #
async def deriv_request(req: dict, authorize: bool = False) -> dict:
    """Helper to connect to Deriv WS, authorize if required, and send a payload."""
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            if authorize and DERIV_API_TOKEN:
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
    """Fetch OHLC candlestick data directly from Deriv."""
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

async def place_deriv_multiplier_trade(trade_type: str) -> dict:
    """Executes a Multiplier trade on Deriv using a proposal + buy flow."""
    proposal_req = {
        "proposal": 1,
        "amount": STAKE_AMOUNT,
        "basis": "stake",
        "contract_type": trade_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "multiplier": 30,  # Multiplier set to x30 to allow $2 SL room to absorb market noise
        "limit_order": {
            "stop_loss": SL_AMOUNT,
            "take_profit": TP_AMOUNT
        }
    }
    proposal_res = await deriv_request(proposal_req, authorize=True)
    proposal = proposal_res.get("proposal", {})
    proposal_id = proposal.get("id")

    if not proposal_id:
        print(f"[DERIV PROPOSAL FAILED] {proposal_res.get('error', {}).get('message', 'Unknown Error')}")
        return {"status": "FAILED"}

    buy_req = {
        "buy": proposal_id,
        "price": STAKE_AMOUNT
    }
    buy_res = await deriv_request(buy_req, authorize=True)
    if "buy" in buy_res:
        return {"status": "EXECUTED", "contract_id": buy_res["buy"].get("contract_id")}
    return {"status": "FAILED"}

# ==================== STRATEGY ANALYSIS ==================== #
def get_h1_macro_bias(df_h1: pd.DataFrame) -> str:
    if len(df_h1) < 20:
        return "NEUTRAL"
    df_h1['ema20'] = df_h1['close'].ewm(span=20, adjust=False).mean()
    last_close = df_h1['close'].iloc[-1]
    last_ema = df_h1['ema20'].iloc[-1]
    return "BULLISH" if last_close > last_ema else ("BEARISH" if last_close < last_ema else "NEUTRAL")

def detect_elliott_waves(df: pd.DataFrame) -> dict:
    """Relaxed swing identification to detect early trend expansions faster."""
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

# ==================== AI CONSENSUS ENGINE (GEMINI + GROK) ==================== #
async def evaluate_with_gemini(prompt: str) -> dict:
    if not ai_client:
        return {"approved": True, "reason": "Gemini API key missing - auto approved"}
    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        clean = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        print(f"[GEMINI EVAL ERROR] {e}")
        return {"approved": False, "reason": f"Gemini error: {e}"}

async def evaluate_with_grok(prompt: str) -> dict:
    if not GROK_API_KEY:
        return {"approved": True, "reason": "Grok API key missing - fallback approved"}
    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "grok-4.1-fast",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    try:
        res = await http_client.post("https://api.x.ai/v1/chat/completions", headers=headers, json=payload)
        res_data = res.json()
        raw_text = res_data["choices"][0]["message"]["content"]
        clean = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        print(f"[GROK EVAL ERROR] {e}")
        return {"approved": True, "reason": "Grok call failed - defaulting to Gemini decision"}

async def get_dual_ai_approval(df_5m: pd.DataFrame, h1_bias: str, proposed_signal: str) -> dict:
    recent_candles = df_5m[['open', 'high', 'low', 'close']].tail(15).to_string()
    current_atr = round(float(df_5m['atr'].iloc[-1]), 4) if 'atr' in df_5m else 0.0

    prompt = f"""
    You are an expert Smart Money Concepts (SMC) & Elliott Wave Trader.
    Current Symbol: XAUUSD (Gold 5m)
    1H Macro Bias: {h1_bias}
    Proposed Signal: {proposed_signal}
    5-min ATR: {current_atr}

    Recent 15 Candles:
    {recent_candles}

    Task:
    Check if the market is stuck in severe horizontal chop. If YES, reject.
    Otherwise, if there is a reasonable trend, FVG, or Order Block structure, approve.

    Respond ONLY with a JSON object:
    {{"approved": true, "reason": "Brief explanation..."}}
    """

    gemini_task = evaluate_with_gemini(prompt)
    grok_task = evaluate_with_grok(prompt)

    gemini_res, grok_res = await asyncio.gather(gemini_task, grok_task)

    g_app = gemini_res.get("approved", False)
    x_app = grok_res.get("approved", False)

    # Both models must agree to approve the trade
    final_approval = g_app and x_app
    combined_reason = f"Gemini: {gemini_res.get('reason', 'N/A')} | Grok: {grok_res.get('reason', 'N/A')}"

    return {"approved": final_approval, "reason": combined_reason}

# ==================== TELEGRAM NOTIFICATIONS ==================== #
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
        title=f"\nSignal: {setup_name}"
    )

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

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
    print("🚀 DERIV AI-POWERED TRADING ENGINE RUNNING (GEMINI + GROK DUAL CONSENSUS)")

    while True:
        try:
            await asyncio.sleep(60)
            now_utc = datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            df_5m = await fetch_deriv_candles(granularity=300, count=200) # 5m
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50) # 1h

            if df_5m.empty or df_h1.empty:
                continue

            df_5m['atr'] = calculate_atr(df_5m)
            current_atr = df_5m['atr'].iloc[-1]

            h1_bias = get_h1_macro_bias(df_h1)
            elliott_data = detect_elliott_waves(df_5m)
            close_price = df_5m['close'].iloc[-1]

            signal_type = None
            deriv_contract = None
            sl_points = max(current_atr * 2.5, 12.0)

            if elliott_data['bias'] == 'BULLISH' and h1_bias == 'BULLISH':
                signal_type = f"BUY_{elliott_data['current_wave']}"
                deriv_contract = "MULTUP"
                entry = close_price
                sl_price = entry - sl_points
                tp_price = entry + (sl_points * 3.0)

            elif elliott_data['bias'] == 'BEARISH' and h1_bias == 'BEARISH':
                signal_type = f"SELL_{elliott_data['current_wave']}"
                deriv_contract = "MULTDOWN"
                entry = close_price
                sl_price = entry + sl_points
                tp_price = entry - (sl_points * 3.0)

            if signal_type:
                print(f"[SETUP FOUND] {signal_type} @ {entry:.2f}. Requesting Gemini + Grok review...")

                ai_eval = await get_dual_ai_approval(df_5m, h1_bias, signal_type)
                if not ai_eval["approved"]:
                    print(f"[AI VETO] Signal Rejected. Reason: {ai_eval['reason']}")
                    continue

                print(f"[AI CONSENSUS APPROVED] Reason: {ai_eval['reason']}")

                # 1. Place Order on Deriv
                trade_res = await place_deriv_multiplier_trade(deriv_contract)
                trade_status = trade_res.get("status", "FAILED")
                contract_id = trade_res.get("contract_id", "N/A")

                # 2. Render Chart
                chart_bytes = await asyncio.to_thread(
                    render_chart_image, df_5m, signal_type, entry, sl_price, tp_price
                )

                # 3. Send Telegram Alert
                msg = (
                    f"⚡ *AI CONSENSUS TRADE EXECUTED*\n\n"
                    f"• *Symbol:* `XAUUSD (Gold)`\n"
                    f"• *Action:* `{signal_type}`\n"
                    f"• *Entry Price:* `{entry:.2f}`\n"
                    f"• *Tech SL Level:* `{sl_price:.2f}`\n"
                    f"• *Tech TP Level:* `{tp_price:.2f}`\n"
                    f"-------------------------------------\n"
                    f"🧠 *AI REVIEW*\n"
                    f"_{ai_eval['reason']}_\n"
                    f"-------------------------------------\n"
                    f"• *Position Stake:* `${STAKE_AMOUNT:.2f}`\n"
                    f"• *Max Risk Cap:* `-${SL_AMOUNT:.2f}`\n"
                    f"• *Target Gain:* `+${TP_AMOUNT:.2f}`\n"
                    f"• *Broker Status:* `{trade_status}` (ID: `{contract_id}`)\n"
                )
                await send_telegram_alert(msg, chart_bytes)
                last_trade_time = now_utc

        except Exception as err:
            print(f"[WORKER ERROR] {err}")
            await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(deriv_trading_worker())
    yield
    worker_task.cancel()
    await http_client.aclose()

app = FastAPI(title="Deriv Trading Engine", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "DERIV_TRADING_ENGINE_ONLINE"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    
