import os
import io
import json
import asyncio
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import requests
from datetime import datetime, timezone, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from metaapi_cloud_sdk import MetaApi
from google import genai
from google.genai import types
import uvicorn

# ==================== ENVIRONMENT & CONFIGURATION ==================== #
META_API_TOKEN = os.getenv("META_API_TOKEN") or os.getenv("METAAPI_TOKEN")
META_ACCOUNT_ID = os.getenv("META_ACCOUNT_ID") or os.getenv("ACCOUNT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MAX_SPREAD_POINTS = float(os.getenv("MAX_SPREAD_POINTS", "35"))
COOLDOWN_MINUTES = 15
MAX_DAILY_DRAWDOWN_PCT = 3.0
TARGET_RRR = 3.0  # Standard 1:3 Risk-to-Reward Ratio

# Candle Depth Configurations
CANDLE_COUNT_5M = 200  # Deep 5m history for accurate structure
CANDLE_COUNT_H1 = 50   # 1-Hour macro trend filter

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

last_closed_trade_time = datetime.min.replace(tzinfo=timezone.utc)
daily_starting_equity = None
last_reset_day = None


# ==================== MULTI-TIMEFRAME & ENGINE CALCULATIONS ==================== #
def get_h1_macro_bias(df_h1: pd.DataFrame) -> str:
    """Determines H1 macro direction using a 20-period Exponential Moving Average"""
    if len(df_h1) < 20:
        return "NEUTRAL"
    
    df_h1['ema20'] = df_h1['close'].ewm(span=20, adjust=False).mean()
    last_close = df_h1['close'].iloc[-1]
    last_ema = df_h1['ema20'].iloc[-1]
    
    if last_close > last_ema:
        return "BULLISH"
    elif last_close < last_ema:
        return "BEARISH"
    return "NEUTRAL"

def detect_elliott_waves(df: pd.DataFrame) -> dict:
    if len(df) < 30:
        return {"current_wave": "UNKNOWN", "bias": "NEUTRAL"}

    highs = df['high'].values
    lows = df['low'].values
    swing_highs, swing_lows = [], []
    
    for i in range(2, len(df) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append((i, highs[i]))
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append((i, lows[i]))

    if len(swing_highs) < 3 or len(swing_lows) < 3:
        return {"current_wave": "CONSOLIDATION", "bias": "NEUTRAL"}

    h1, h2, h3 = swing_highs[-3][1], swing_highs[-2][1], swing_highs[-1][1]
    l1, l2, l3 = swing_lows[-3][1], swing_lows[-2][1], swing_lows[-1][1]

    if h3 > h2 > h1 and l3 > l2 > l1:
        if (h2 - l2) > (h1 - l1):
            return {"current_wave": "WAVE_5_BULLISH_IMPULSE", "bias": "BULLISH"}
        return {"current_wave": "WAVE_3_BULLISH_EXPANSION", "bias": "BULLISH"}
        
    elif h3 < h2 < h1 and l3 < l2 < l1:
        if (h2 - l2) > (h1 - l1):
            return {"current_wave": "WAVE_5_BEARISH_IMPULSE", "bias": "BEARISH"}
        return {"current_wave": "WAVE_3_BEARISH_EXPANSION", "bias": "BEARISH"}

    elif h3 < h2 and l3 > l2:
        return {"current_wave": "CORRECTIVE_WAVE_ABC", "bias": "NEUTRAL_REVERSAL"}

    return {"current_wave": "WAVE_DEVELOPMENT", "bias": "NEUTRAL"}

def detect_unmitigated_order_blocks(df: pd.DataFrame) -> list[dict]:
    order_blocks = []
    for i in range(3, len(df) - 2):
        if df['close'].iloc[i-1] < df['open'].iloc[i-1]:
            displacement = (df['close'].iloc[i+1] - df['low'].iloc[i-1])
            atr_val = df['atr'].iloc[i] if 'atr' in df.columns else 2.0
            if displacement > (atr_val * 1.5):
                ob_high, ob_low = df['high'].iloc[i-1], df['low'].iloc[i-1]
                future_lows = df['low'].iloc[i:]
                if not (future_lows < ob_low).any():
                    order_blocks.append({'type': 'BULLISH_OB', 'high': ob_high, 'low': ob_low, 'idx': i-1})
        elif df['close'].iloc[i-1] > df['open'].iloc[i-1]:
            displacement = (df['high'].iloc[i-1] - df['close'].iloc[i+1])
            atr_val = df['atr'].iloc[i] if 'atr' in df.columns else 2.0
            if displacement > (atr_val * 1.5):
                ob_high, ob_low = df['high'].iloc[i-1], df['low'].iloc[i-1]
                future_highs = df['high'].iloc[i:]
                if not (future_highs > ob_high).any():
                    order_blocks.append({'type': 'BEARISH_OB', 'high': ob_high, 'low': ob_low, 'idx': i-1})
    return order_blocks

def detect_fvgs(df: pd.DataFrame) -> list[dict]:
    fvgs = []
    for i in range(2, len(df)):
        if df['low'].iloc[i] > df['high'].iloc[i-2]:
            fvgs.append({'type': 'BULLISH_FVG', 'top': df['low'].iloc[i], 'bottom': df['high'].iloc[i-2]})
        elif df['high'].iloc[i] < df['low'].iloc[i-2]:
            fvgs.append({'type': 'BEARISH_FVG', 'top': df['low'].iloc[i-2], 'bottom': df['high'].iloc[i]})
    return fvgs

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(period).mean()

def calculate_fractional_kelly_lots(equity: float, entry: float, sl: float, win_rate: float = 0.55, reward_ratio: float = 3.0) -> float:
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.01
    b, p = reward_ratio, win_rate
    q = 1.0 - p
    kelly_pct = (b * p - q) / b
    safe_risk_pct = max(0.005, min(kelly_pct * 0.25, 0.02))
    risk_amount = equity * safe_risk_pct
    lots = risk_amount / (sl_dist * 100.0)
    return float(np.clip(round(lots, 2), 0.01, 10.0))

def generate_ai_affirmation() -> str:
    if not ai_client:
        return "✨ Maintain strict risk management today. Capital preservation opens the door to high-probability setups."
    try:
        prompt = "Write a short daily trading affirmation for a Gold (XAUUSD) SMC trader with 2-3 bullet points."
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[GEMINI ERROR] {e}")
        return "✨ Maintain strict risk management today."

def is_institutional_session_active(now_utc: datetime) -> tuple[bool, str]:
    current_time = now_utc.time()
    if time(7, 0) <= current_time <= time(10, 0):
        return True, "LONDON_OPEN"
    elif time(12, 0) <= current_time <= time(16, 0):
        return True, "NEW_YORK_OVERLAP"
    return False, "OFF_HOURS_NOISE"

async def query_gemini_async_validator(metrics: dict) -> bool:
    if not ai_client:
        return True
    system_instruction = "You are an elite quantitative trade execution controller validating SMC and Elliott Wave setups."
    prompt = f"Evaluate execution context:\n```json\n{json.dumps(metrics, indent=2)}\n```\nExecute?"
    try:
        response = await ai_client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "decision": {"type": "STRING", "enum": ["APPROVE", "REJECT"]},
                        "confidence_score": {"type": "NUMBER"}
                    },
                    "required": ["decision", "confidence_score"]
                }
            )
        )
        res_data = json.loads(response.text)
        return res_data.get('decision') == "APPROVE" and res_data.get('confidence_score', 0) >= 0.80
    except Exception:
        return True

def render_apex_candlestick_chart(df_5m: pd.DataFrame, obs: list[dict], fvgs: list[dict], setup_name: str, entry: float, sl: float, tp: float) -> bytes:
    chart_df = df_5m.tail(45).copy()
    chart_df['time'] = pd.to_datetime(chart_df['time'])
    chart_df.set_index('time', inplace=True)
    chart_df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)

    mc = mpf.make_marketcolors(up='#089981', down='#F23645', edge='inherit', wick='inherit', ohlc='inherit')
    s = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', gridcolor='#E0E0E0', y_on_right=False)
    h_lines = dict(hlines=[entry, sl, tp], colors=['#2962FF', '#F23645', '#089981'], linestyle='--', linewidths=1.2)

    fig, axlist = mpf.plot(chart_df, type='candle', style=s, hlines=h_lines, figsize=(10, 5), returnfig=True, title=f"\nTrigger: {setup_name}")
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
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}, files=files, timeout=10)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

async def manage_open_positions_breakeven(connection, current_bid: float, current_ask: float):
    """Moves Stop Loss to Break-Even (Entry Price) once position hits 1:1 RRR"""
    try:
        positions = await connection.get_positions()
        for pos in positions:
            if pos.get('symbol') != 'XAUUSD':
                continue
            open_price = float(pos['openPrice'])
            current_sl = float(pos.get('stopLoss', 0))
            pos_id, pos_type, tp_price = pos['id'], pos['type'], float(pos.get('takeProfit', 0))

            if pos_type == 'POSITION_TYPE_BUY':
                risk_dist = open_price - current_sl
                if risk_dist > 0 and current_bid >= (open_price + risk_dist) and current_sl < open_price:
                    await connection.modify_position(pos_id, stop_loss=open_price, take_profit=tp_price)
                    await send_telegram_alert(f"🔒 *BREAK-EVEN TRIGGERED*\nBuy `#{pos_id}` SL moved to entry `{open_price:.2f}` (Risk Free).")

            elif pos_type == 'POSITION_TYPE_SELL':
                risk_dist = current_sl - open_price
                if risk_dist > 0 and current_ask <= (open_price - risk_dist) and (current_sl > open_price or current_sl == 0):
                    await connection.modify_position(pos_id, stop_loss=open_price, take_profit=tp_price)
                    await send_telegram_alert(f"🔒 *BREAK-EVEN TRIGGERED*\nSell `#{pos_id}` SL moved to entry `{open_price:.2f}` (Risk Free).")
    except Exception as err:
        print(f"[BREAK EVEN ERROR] {err}")


# ==================== MAIN APEX WORKER ==================== #
async def apex_trading_worker():
    global last_closed_trade_time, daily_starting_equity, last_reset_day

    if not META_API_TOKEN or not META_ACCOUNT_ID:
        print("[WARNING] Credentials missing.")
        while True:
            await asyncio.sleep(60)

    api = MetaApi(META_API_TOKEN)
    while True:
        try:
            account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
            if account.state != 'DEPLOYED':
                await account.deploy()
            await account.wait_connected()
            connection = account.get_rpc_connection()
            await connection.connect()
            await connection.wait_synchronized()
            print("💎 GOD MODE APEX TRADING ENGINE CONNECTED & LIVE")
            break
        except Exception as e:
            print(f"[RETRY] Connecting: {e}")
            await asyncio.sleep(30)

    while True:
        try:
            await asyncio.sleep(8)
            now_utc = datetime.now(timezone.utc)

            account_info = await connection.get_account_information()
            equity = account_info.get("equity", 0)

            price_data = await connection.get_symbol_price('XAUUSD')
            bid, ask = price_data['bid'], price_data['ask']

            # Run Break-Even Engine on Active Positions
            await manage_open_positions_breakeven(connection, bid, ask)

            session_active, session_name = is_institutional_session_active(now_utc)
            if not session_active or (now_utc - last_closed_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            spread_points = (ask - bid) * 100
            if spread_points > MAX_SPREAD_POINTS or len(await connection.get_positions()) > 0:
                continue

            # Fetch Multi-Timeframe Data (200 candles on 5m, 50 candles on H1)
            candles_5m = await account.get_historical_candles('XAUUSD', '5m', None, CANDLE_COUNT_5M)
            candles_h1 = await account.get_historical_candles('XAUUSD', '1h', None, CANDLE_COUNT_H1)

            df_5m = pd.DataFrame(candles_5m).sort_values('time').reset_index(drop=True)
            df_h1 = pd.DataFrame(candles_h1).sort_values('time').reset_index(drop=True)

            df_5m['atr'] = calculate_atr(df_5m)
            current_atr = df_5m['atr'].iloc[-1]

            # Analyze HTF Trend & LTF Setups
            h1_bias = get_h1_macro_bias(df_h1)
            obs = detect_unmitigated_order_blocks(df_5m)
            fvgs = detect_fvgs(df_5m)
            elliott_data = detect_elliott_waves(df_5m)

            close_price = df_5m['close'].iloc[-1]
            has_bullish_ob = any(ob['type'] == 'BULLISH_OB' for ob in obs)
            has_bullish_fvg = any(fvg['type'] == 'BULLISH_FVG' for fvg in fvgs[-4:])
            has_bearish_ob = any(ob['type'] == 'BEARISH_OB' for ob in obs)
            has_bearish_fvg = any(fvg['type'] == 'BEARISH_FVG' for fvg in fvgs[-4:])

            setup_triggered = None
            sl_buffer = max(current_atr * 2.5, 12.0)

            # Triple Confluence: 5m SMC + 5m Elliott Wave + 1H Macro Trend
            if (has_bullish_ob or has_bullish_fvg) and elliott_data['bias'] == 'BULLISH' and h1_bias == 'BULLISH':
                setup_triggered = f"BULLISH_SMC_{elliott_data['current_wave']}"
                entry = close_price
                sl = entry - sl_buffer
                tp = entry + (abs(entry - sl) * TARGET_RRR)

            elif (has_bearish_ob or has_bearish_fvg) and elliott_data['bias'] == 'BEARISH' and h1_bias == 'BEARISH':
                setup_triggered = f"BEARISH_SMC_{elliott_data['current_wave']}"
                entry = close_price
                sl = entry + sl_buffer
                tp = entry - (abs(entry - sl) * TARGET_RRR)

            if setup_triggered:
                metrics = {"setup": setup_triggered, "h1_bias": h1_bias, "entry": entry, "sl": sl, "tp": tp}
                if await query_gemini_async_validator(metrics):
                    lots = calculate_fractional_kelly_lots(equity, entry, sl, win_rate=0.55, reward_ratio=TARGET_RRR)
                    chart_bytes = render_apex_candlestick_chart(df_5m, obs, fvgs, setup_triggered, entry, sl, tp)

                    trade_executed, execution_error_msg = False, ""
                    try:
                        if "BULLISH" in setup_triggered:
                            await connection.create_market_buy_order(symbol='XAUUSD', volume=lots, stop_loss=sl, take_profit=tp)
                        else:
                            await connection.create_market_sell_order(symbol='XAUUSD', volume=lots, stop_loss=sl, take_profit=tp)
                        trade_executed = True
                    except Exception as err:
                        execution_error_msg = str(err)

                    status_header = "🎯 *GOLD (XAUUSD) SIGNAL EXECUTED*" if trade_executed else "⚠️ *SIGNAL TRIGGERED (BROKER REJECTED)*"
                    msg = (
                        f"{status_header}\n\n"
                        f"• *Type:* `{setup_triggered}`\n"
                        f"• *H1 Bias:* `{h1_bias}`\n"
                        f"• *Entry:* `{entry:.2f}`\n"
                        f"• *SL:* `{sl:.2f}` (Buffered)\n"
                        f"• *TP:* `{tp:.2f}` (1:{TARGET_RRR:g} RRR)\n"
                        f"• *Lots:* `{lots}`\n"
                    )
                    if not trade_executed:
                        msg += f"\n❌ *Broker Error:* `{execution_error_msg}`"

                    await send_telegram_alert(msg, chart_bytes)
                    last_closed_trade_time = now_utc

        except Exception as loop_error:
            print(f"[ENGINE ERROR] {loop_error}")
            await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(apex_trading_worker())
    yield
    worker_task.cancel()

app = FastAPI(title="Apex Quantitative Engine", lifespan=lifespan)

@app.get("/")
async def status():
    return {"status": "GOD_MODE_APEX_ONLINE", "engine": "MULTI_TIMEFRAME_H1_5M"}

@app.api_route("/daily-affirmation", methods=["GET", "POST"])
async def trigger_daily_affirmation(background_tasks: BackgroundTasks):
    async def task_process():
        content = generate_ai_affirmation()
        await send_telegram_alert(f"🌅 DAILY AFFIRMATION\n\n{content}")
    background_tasks.add_task(task_process)
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
                                     
