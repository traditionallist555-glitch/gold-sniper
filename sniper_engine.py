import os
import io
import json
import asyncio
import random
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

# ------------------------------------------------------------------
# SYSTEM ENVIRONMENT VARIABLES
# ------------------------------------------------------------------
META_API_TOKEN = os.getenv("META_API_TOKEN") or os.getenv("METAAPI_TOKEN")
META_ACCOUNT_ID = os.getenv("META_ACCOUNT_ID") or os.getenv("ACCOUNT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MAX_SPREAD_POINTS = float(os.getenv("MAX_SPREAD_POINTS", "35"))
COOLDOWN_MINUTES = 15
MAX_DAILY_DRAWDOWN_PCT = 3.0  # Circuit breaker lock at 3.0% equity loss

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# System State Memory
last_closed_trade_time = datetime.min.replace(tzinfo=timezone.utc)
daily_starting_equity = None
last_reset_day = None

# ------------------------------------------------------------------
# DYNAMIC AI AFFIRMATION GENERATOR
# ------------------------------------------------------------------

def generate_ai_affirmation() -> str:
    """Uses Gemini 2.5 Flash to generate a unique daily trading blessing and affirmation."""
    if not ai_client:
        return (
            "✨ Today is filled with peace, unshakeable focus, and sharp execution.\n"
            "✨ Step forward with confidence and trade your strategy with absolute discipline!"
        )

    try:
        prompt = (
            "Write a short, inspiring daily trading blessing and affirmation for a Gold (XAUUSD) SMC trader. "
            "It must sound elite, calm, and focused on discipline, patience, and risk management. "
            "Format it with 2-3 clean bullet points using ✨ emojis. Make every single generation completely unique."
        )
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[GEMINI AFFIRMATION ERROR] {e}")
        return "✨ Maintain strict risk management today. Capital preservation opens the door to high-probability setups."

# ------------------------------------------------------------------
# SESSION & VOLUME LOCK ENGINE
# ------------------------------------------------------------------

def is_institutional_session_active(now_utc: datetime) -> tuple[bool, str]:
    """Strictly restricts trade triggers to high-volume institutional windows."""
    current_time = now_utc.time()
    
    london_open_start, london_open_end = time(7, 0), time(10, 0)
    ny_overlap_start, ny_overlap_end = time(12, 0), time(16, 0)

    if london_open_start <= current_time <= london_open_end:
        return True, "LONDON_OPEN"
    elif ny_overlap_start <= current_time <= ny_overlap_end:
        return True, "NEW_YORK_OVERLAP"
    
    return False, "OFF_HOURS_NOISE"

# ------------------------------------------------------------------
# SMC STRUCTURE ENGINE (ORDER BLOCKS & FVGS)
# ------------------------------------------------------------------

def detect_unmitigated_order_blocks(df: pd.DataFrame) -> list[dict]:
    """Identifies institutional Order Blocks that remain unmitigated."""
    order_blocks = []
    
    for i in range(3, len(df) - 2):
        # Bullish Order Block: Last bearish candle before strong displacement upward
        if df['close'].iloc[i-1] < df['open'].iloc[i-1]:
            displacement = (df['close'].iloc[i+1] - df['low'].iloc[i-1])
            atr_val = df['atr'].iloc[i] if 'atr' in df.columns else 2.0
            
            if displacement > (atr_val * 1.5):
                ob_high = df['high'].iloc[i-1]
                ob_low = df['low'].iloc[i-1]
                
                future_lows = df['low'].iloc[i:]
                if not (future_lows < ob_low).any():
                    order_blocks.append({'type': 'BULLISH_OB', 'high': ob_high, 'low': ob_low, 'idx': i-1})

        # Bearish Order Block: Last bullish candle before strong displacement downward
        elif df['close'].iloc[i-1] > df['open'].iloc[i-1]:
            displacement = (df['high'].iloc[i-1] - df['close'].iloc[i+1])
            atr_val = df['atr'].iloc[i] if 'atr' in df.columns else 2.0
            
            if displacement > (atr_val * 1.5):
                ob_high = df['high'].iloc[i-1]
                ob_low = df['low'].iloc[i-1]
                
                future_highs = df['high'].iloc[i:]
                if not (future_highs > ob_high).any():
                    order_blocks.append({'type': 'BEARISH_OB', 'high': ob_high, 'low': ob_low, 'idx': i-1})

    return order_blocks

def detect_fvgs(df: pd.DataFrame) -> list[dict]:
    """Detects active 3-candle Fair Value Gaps."""
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

# ------------------------------------------------------------------
# FRACTIONAL KELLY RISK ENGINE
# ------------------------------------------------------------------

def calculate_fractional_kelly_lots(equity: float, entry: float, sl: float, win_rate: float = 0.55, reward_ratio: float = 3.0) -> float:
    """Calculates optimal lot allocation using Quarter-Kelly Criterion."""
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.01

    b = reward_ratio
    p = win_rate
    q = 1.0 - p
    kelly_pct = (b * p - q) / b

    # Quarter-Kelly safety limit (capped between 0.5% and 2.0% equity risk)
    safe_risk_pct = max(0.005, min(kelly_pct * 0.25, 0.02))
    
    risk_amount = equity * safe_risk_pct
    lots = risk_amount / (sl_dist * 100.0)
    return float(np.clip(round(lots, 2), 0.01, 10.0))

# ------------------------------------------------------------------
# NON-BLOCKING ASYNC AI QUANT VALIDATION
# ------------------------------------------------------------------

async def query_gemini_async_validator(metrics: dict) -> bool:
    """Non-blocking parallel AI execution check via Gemini 2.5 Flash."""
    if not ai_client:
        return True

    system_instruction = (
        "You are an elite quantitative trade execution controller. Validate market entries based on "
        "Smart Money Concepts (SMC), volume sessions, and risk parameters. Approve only ultra-high probability trades."
    )

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
                        "confidence_score": {"type": "NUMBER"},
                        "reasoning": {"type": "STRING"}
                    },
                    "required": ["decision", "confidence_score", "reasoning"]
                }
            )
        )
        res_data = json.loads(response.text)
        print(f"[ASYNC AI RESPONSE] {res_data['decision']} | Confidence: {res_data['confidence_score']} | Note: {res_data['reasoning']}")
        return res_data.get('decision') == "APPROVE" and res_data.get('confidence_score', 0) >= 0.80
    except Exception as e:
        print(f"[ASYNC AI WARNING] Non-critical fallback engage: {e}")
        return True

# ------------------------------------------------------------------
# MPLFINANCE CANDLESTICK CHART GENERATOR
# ------------------------------------------------------------------

def render_apex_candlestick_chart(df_5m: pd.DataFrame, obs: list[dict], fvgs: list[dict], setup_name: str, entry: float, sl: float, tp: float) -> bytes:
    """Renders professional financial candlesticks with annotated SMC levels."""
    chart_df = df_5m.tail(45).copy()
    chart_df['time'] = pd.to_datetime(chart_df['time'])
    chart_df.set_index('time', inplace=True)
    chart_df.rename(columns={
        'open': 'Open',
        'high': 'High',
        'low': 'Low',
        'close': 'Close',
        'volume': 'Volume'
    }, inplace=True)

    mc = mpf.make_marketcolors(
        up='#089981',      # Institutional Green
        down='#F23645',    # Institutional Red
        edge='inherit',
        wick='inherit',
        ohlc='inherit'
    )
    s = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', gridcolor='#E0E0E0', y_on_right=False)

    h_lines = dict(
        hlines=[entry, sl, tp],
        colors=['#2962FF', '#F23645', '#089981'],
        linestyle='--',
        linewidths=1.2
    )

    fig, axlist = mpf.plot(
        chart_df,
        type='candle',
        style=s,
        hlines=h_lines,
        figsize=(10, 5),
        returnfig=True,
        title=f"\nSMC Trigger: {setup_name}"
    )

    ax = axlist[0]

    # Overlay Order Blocks
    for ob in obs[-2:]:
        color = '#2962FF' if ob['type'] == 'BULLISH_OB' else '#FF6D00'
        ax.axhspan(ob['low'], ob['high'], alpha=0.2, color=color, label=f"Unmitigated {ob['type']}")

    # Overlay Fair Value Gaps
    for fvg in fvgs[-2:]:
        color = '#089981' if fvg['type'] == 'BULLISH_FVG' else '#F23645'
        ax.axhspan(fvg['bottom'], fvg['top'], alpha=0.15, color=color, label=fvg['type'])

    # Annotate Target Price Lines
    last_time = chart_df.index[-1]
    ax.text(last_time, entry, f"  ENTRY: {entry:.2f}", color='#2962FF', fontweight='bold', va='center')
    ax.text(last_time, sl, f"  SL: {sl:.2f}", color='#F23645', fontweight='bold', va='center')
    ax.text(last_time, tp, f"  TP: {tp:.2f}", color='#089981', fontweight='bold', va='center')

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ------------------------------------------------------------------
# TELEGRAM NOTIFICATION PIPELINE
# ------------------------------------------------------------------

async def send_telegram_alert(message: str, image_bytes: bytes = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        if image_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', image_bytes, 'image/png')}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}
            requests.post(url, data=data, files=files, timeout=10)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ------------------------------------------------------------------
# MAIN INSTITUTIONAL EXECUTION LOOP (WITH CONNECTION WRAPPER)
# ------------------------------------------------------------------

async def apex_trading_worker():
    global last_closed_trade_time, daily_starting_equity, last_reset_day
    
    if not META_API_TOKEN or not META_ACCOUNT_ID:
        print("[METAAPI WARNING] Token or Account ID missing. Engine in web-only mode.")
        while True:
            await asyncio.sleep(60)

    api = MetaApi(META_API_TOKEN)
    connection = None

    # Robust Connection Wrapper Loop
    while True:
        try:
            account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
            
            if account.state != 'DEPLOYED':
                print(f"[METAAPI] Account state is '{account.state}'. Initiating deployment...")
                await account.deploy()

            await account.wait_connected()
            connection = account.get_rpc_connection()
            await connection.connect()
            await connection.wait_synchronized()
            print("💎 GOD MODE APEX TRADING ENGINE CONNECTED & LIVE")
            break

        except Exception as conn_err:
            print(f"[METAAPI RETRY] Connection or deployment delay: {conn_err}. Retrying in 30s...")
            await asyncio.sleep(30)

    # Core Strategy Execution Loop
    while True:
        try:
            await asyncio.sleep(8)
            now_utc = datetime.now(timezone.utc)

            # 1. Reset Daily Equity Reference at 00:00 UTC
            if last_reset_day != now_utc.day:
                account_info = await connection.get_account_information()
                daily_starting_equity = account_info.get("equity", 0)
                last_reset_day = now_utc.day
                print(f"[CIRCUIT BREAKER] Daily Base Equity Reset: ${daily_starting_equity:.2f}")

            # 2. Daily Drawdown Circuit Breaker
            account_info = await connection.get_account_information()
            equity = account_info.get("equity", 0)
            
            if daily_starting_equity and daily_starting_equity > 0:
                drawdown_pct = ((daily_starting_equity - equity) / daily_starting_equity) * 100.0
                if drawdown_pct >= MAX_DAILY_DRAWDOWN_PCT:
                    print(f"[CIRCUIT BREAKER ACTIVE] Drawdown at {drawdown_pct:.2f}%. Locks engage today.")
                    continue

            # 3. Session Volume Window Lock
            session_active, session_name = is_institutional_session_active(now_utc)
            if not session_active:
                continue

            # 4. Cooldown Check
            if (now_utc - last_closed_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60):
                continue

            # 5. Market Spread Check
            price_data = await connection.get_symbol_price('XAUUSD')
            bid, ask = price_data['bid'], price_data['ask']
            spread_points = (ask - bid) * 100
            if spread_points > MAX_SPREAD_POINTS:
                continue

            positions = await connection.get_positions()
            if len(positions) > 0:
                continue

            # 6. Fetch Candle Structure (Fixed: called via connection object)
            candles_5m = await connection.get_historical_candles('XAUUSD', '5m', None, 60)
            df_5m = pd.DataFrame(candles_5m)
            df_5m['time'] = pd.to_datetime(df_5m['time'])
            df_5m = df_5m.sort_values('time').reset_index(drop=True)
            df_5m['atr'] = calculate_atr(df_5m)

            current_atr = df_5m['atr'].iloc[-1]
            obs = detect_unmitigated_order_blocks(df_5m)
            fvgs = detect_fvgs(df_5m)

            # SMC Confluence Triggers
            close_price = df_5m['close'].iloc[-1]
            recent_low = df_5m['low'].iloc[-10:-2].min()
            recent_high = df_5m['high'].iloc[-10:-2].max()

            has_bullish_ob = any(ob['type'] == 'BULLISH_OB' for ob in obs)
            has_bullish_fvg = any(fvg['type'] == 'BULLISH_FVG' for fvg in fvgs[-3:])

            has_bearish_ob = any(ob['type'] == 'BEARISH_OB' for ob in obs)
            has_bearish_fvg = any(fvg['type'] == 'BEARISH_FVG' for fvg in fvgs[-3:])

            setup_triggered = None
            if has_bullish_ob and has_bullish_fvg and df_5m['low'].iloc[-2] <= recent_low and close_price > df_5m['high'].iloc[-2]:
                setup_triggered = "INSTITUTIONAL_BULLISH_OB_FVG_SWEEP"
                entry = close_price
                sl = entry - max(current_atr * 2.0, 4.0)
                tp = entry + (abs(entry - sl) * 3.0)

            elif has_bearish_ob and has_bearish_fvg and df_5m['high'].iloc[-2] >= recent_high and close_price < df_5m['low'].iloc[-2]:
                setup_triggered = "INSTITUTIONAL_BEARISH_OB_FVG_SWEEP"
                entry = close_price
                sl = entry + max(current_atr * 2.0, 4.0)
                tp = entry - (abs(entry - sl) * 3.0)

            if setup_triggered:
                metrics = {
                    "session": session_name,
                    "setup": setup_triggered,
                    "entry_price": entry,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "atr": round(current_atr, 2),
                    "spread_points": round(spread_points, 1),
                    "order_blocks_active": len(obs),
                    "fvgs_active": len(fvgs)
                }

                # Non-blocking Parallel Async AI Validation
                approved = await query_gemini_async_validator(metrics)

                if approved:
                    lots = calculate_fractional_kelly_lots(equity, entry, sl, win_rate=0.55, reward_ratio=3.0)
                    chart_bytes = render_apex_candlestick_chart(df_5m, obs, fvgs, setup_triggered, entry, sl, tp)

                    if "BULLISH" in setup_triggered:
                        order = await connection.create_market_buy_order('XAUUSD', lots, sl, tp)
                    else:
                        order = await connection.create_market_sell_order('XAUUSD', lots, sl, tp)

                    msg = (
                        f"🎯 *GOLD (XAUUSD) SMC SIGNAL ALERT*\n\n"
                        f"• *Type:* `{setup_triggered}`\n"
                        f"• *Entry:* `{entry:.2f}`\n"
                        f"• *Stop Loss (SL):* `{sl:.2f}`\n"
                        f"• *Take Profit (TP):* `{tp:.2f}` (1:3 RRR)\n"
                        f"• *Dynamic Lots:* `{lots}` Lots\n"
                        f"• *Institutional Session:* `{session_name}`\n"
                        f"• *Spread:* `{spread_points:.1f}` pts"
                    )
                    await send_telegram_alert(msg, chart_bytes)

        except Exception as loop_error:
            print(f"[ENGINE LOOP ERROR] {loop_error}")
            await asyncio.sleep(10)

# ------------------------------------------------------------------
# FASTAPI ENDPOINTS & LIFESPAN STARTUP
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: spawn worker task
    worker_task = asyncio.create_task(apex_trading_worker())
    yield
    # Shutdown: cancel task cleanly
    worker_task.cancel()

app = FastAPI(title="Apex Institutional Quantitative Engine", lifespan=lifespan)

@app.get("/")
async def status():
    """Root health check pinged by cron-job.org every 5 minutes."""
    return {"status": "GOD_MODE_APEX_ONLINE", "engine": "FULL_INSTITUTIONAL_ASYNC"}

@app.get("/daily-affirmation")
async def trigger_daily_affirmation(background_tasks: BackgroundTasks):
    """Endpoint called by cron-job.org to generate and publish unique AI motivation."""
    async def task_process():
        content = generate_ai_affirmation()
        message = f"🌅 DAILY BLESSING & AFFIRMATION\n\n{content}"
        await send_telegram_alert(message)

    background_tasks.add_task(task_process)
    
    return {
        "status": "success",
        "message": "AI Affirmation dynamic generation task queued
