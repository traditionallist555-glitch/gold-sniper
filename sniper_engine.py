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
SL_AMOUNT = 2.00           # Target loss cap ($2.00)
TP_AMOUNT = 6.00           # Target gain cap ($6.00)
COOLDOWN_MINUTES = 10      # Cooldown between trade evaluations

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
last_trade_time = datetime.min.replace(tzinfo=timezone.utc)

# Shared HTTP client for Telegram and Grok API calls
http_client = httpx.AsyncClient(timeout=12.0)

# Initialize Google GenAI Client
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

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
    final_multiplier = max(10, min(calculated_multiplier, 100))

    return {
        "sl_points": sl_points,
        "tp_points": tp_points,
        "multiplier": final_multiplier
    }

# ==================== AI CONSENSUS ENGINE ==================== #
async def evaluate_with_gemini(prompt: str) -> dict:
    if not ai_client:
        return {"approved": False, "reason": "Gemini API key missing"}
    
    try:
        # Updated model endpoint to gemini-2.5-flash
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        clean = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        print(f"[GEMINI EVAL ERROR] {e}")
        return {"approved": False, "reason": f"Gemini API Error"}

async def evaluate_with_grok(prompt: str) -> dict:
    if not GROK_API_KEY:
        return {"approved": False, "reason": "Grok API key missing"}
    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "grok-2",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    try:
        res = await http_client.post("https://api.x.ai/v1/chat/completions", headers=headers, json=payload)
        res_data = res.json()
        
        if isinstance(res_data, dict) and "choices" in res_data:
            raw_text = res_data["choices"][0]["message"]["content"]
            clean = raw_text.replace("```json", "").replace("
