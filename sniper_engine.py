import os
import io
import csv
import json
import base64
import asyncio
import datetime
from datetime import timezone, timedelta
from contextlib import asynccontextmanager

import httpx
import websockets
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import mplfinance as mpf
from fastapi import FastAPI
import uvicorn
from google import genai
from google.genai import types

# ---------------------------------------------------------
# CONFIGURATION & ENVIRONMENT VARIABLES
# ---------------------------------------------------------
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "61048").strip()
SYMBOL = "frxXAUUSD"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

TRADE_LOG_FILE = "trade_history.csv"
COOLDOWN_MINUTES = 5
MIN_RRR = 2.0
ENTRY_BUFFER = 0.25
MIN_WICK_PCT = 0.25  
ATR_MULTIPLIER = 1.5

http_client = httpx.AsyncClient(timeout=25.0)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
AVAILABLE_GEMINI_MODELS = []

last_trade_time = datetime.datetime.min.replace(tzinfo=timezone.utc)
post_loss_cooldown_until = datetime.datetime.min.replace(tzinfo=timezone.utc)

active_setup = {
    "is_active": False,
    "order_type": "LIMIT",
    "direction": None,
    "entry_price": 0.0,
    "sl_price": 0.0,
    "tp1_price": 0.0,
    "tp2_price": 0.0,
    "tp1_hit": False,
    "entry_filled": False
}

SYSTEM_PROMPT = """
You are an elite SMC Gold Trader. Analyze the chart and market summary.
Respond strictly in raw JSON:
{
  "trade_approved": true/false,
  "direction": "BUY" or "SELL",
  "recommended_entry": float,
  "take_profit_price": float,
  "strategy_detected": "5M Liquidity Sweep into Supply/Demand Zone",
  "reason": "Brief technical reasoning..."
}
"""

# ---------------------------------------------------------
# TECHNICAL INDICATORS & FILTERS
# ---------------------------------------------------------
def calculate_atr(df: pd.DataFrame, period: int = 14, multiplier: float = ATR_MULTIPLIER) -> float:
    high, low, close = df['high'], df['low'], df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    base_atr = float(tr.rolling(window=period).mean().iloc[-1])
    return base_atr * multiplier

def is_within_killzone() -> bool:
    now_utc = datetime.datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return False
    current_time = now_utc.time()
    london_start = datetime.datetime.strptime("07:00", "%H:%M").time()
    london_end = datetime.datetime.strptime("11:00", "%H:%M").time()
    ny_start = datetime.datetime.strptime("13:00", "%H:%M").time()
    ny_end = datetime.datetime.strptime("21:00", "%H:%M").time()
    return (london_start <= current_time <= london_end) or (ny_start <= current_time <= ny_end)

def check_heavy_momentum_filter(df_5m: pd.DataFrame, df_h1: pd.DataFrame, direction: str) -> bool:
    if df_5m.empty or len(df_5m) < 5:
        return True
    
    if not df_h1.empty and len(df_h1) >= 1:
        h1_last = df_h1.iloc[-1]
        h1_move = abs(h1_last['close'] - h1_last['open'])
        if h1_move > 15.0:
            if direction == "SELL" and h1_last['close'] > h1_last['open']:
                return False
            if direction == "BUY" and h1_last['close'] < h1_last['open']:
                return False

    recent = df_5m.tail(3)
    scaled_atr = calculate_atr(df_5m)
    if direction == "BUY":
        red_count = sum(recent['close'] < recent['open'])
        total_drop = recent['open'].iloc[0] - recent['close'].iloc[-1]
        if red_count >= 2 and total_drop > (scaled_atr * 2.5):
            return False
    if direction == "SELL":
        green_count = sum(recent['close'] > recent['open'])
        total_surge = recent['close'].iloc[-1] - recent['open'].iloc[0]
        if green_count >= 2 and total_surge > (scaled_atr * 2.5):
            return False

    return True

def verify_post_sweep_rejection(df_5m: pd.DataFrame, direction: str, swing_lookback: int = 15) -> dict:
    if len(df_5m) < swing_lookback + 1:
        return {"valid": False, "reason": "Insufficient candle history"}
    
    current = df_5m.iloc[-1]
    history = df_5m.iloc[-(swing_lookback + 1):-1]
    
    candle_high, candle_low = current['high'], current['low']
    candle_open, candle_close = current['open'], current['close']
    
    total_range = candle_high - candle_low
    if total_range == 0:
        return {"valid": False, "reason": "Doji / Zero Range Candle"}

    upper_wick = candle_high - max(candle_open, candle_close)
    lower_wick = min(candle_open, candle_close) - candle_low

    upper_pct = upper_wick / total_range
    lower_pct = lower_wick / total_range

    if direction == "SELL":
        swing_high = history['high'].max()
        if candle_high > swing_high and upper_pct >= MIN_WICK_PCT:
            sl_price = round(candle_high + 0.50, 2)
            return {
                "valid": True, 
                "action": "SELL", 
                "entry_price": candle_close,
                "stop_loss": sl_price,
                "reason": f"🔥 BSL Sweep Approved! Swept high at ${candle_high:.2f} with {upper_pct*100:.1f}% rejection wick."
            }
        return {"valid": False, "reason": "No BSL sweep or rejection wick < 25%"}

    elif direction == "BUY":
        swing_low = history['low'].min()
        if candle_low < swing_low and lower_pct >= MIN_WICK_PCT:
            sl_price = round(candle_low - 0.50, 2)
            return {
                "valid": True, 
                "action": "BUY", 
                "entry_price": candle_close,
                "stop_loss": sl_price,
                "reason": f"🔥 SSL Sweep Approved! Swept low at ${candle_low:.2f} with {lower_pct*100:.1f}% rejection wick."
            }
        return {"valid": False, "reason": "No SSL sweep or rejection wick < 25%"}

    return {"valid": False, "reason": "Invalid direction requested"}

# ---------------------------------------------------------
# CHART RENDERING & LOGGING
# ---------------------------------------------------------
def render_dual_panel_chart(df_5m: pd.DataFrame, df_h1: pd.DataFrame, entry: float = 0.0, sl: float = 0.0, tp: float = 0.0, direction: str = None) -> bytes:
    chart_5m, chart_h1 = df_5m.tail(50).copy(), df_h1.tail(30).copy()
    for df in [chart_5m, chart_h1]:
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)

    chart_h1['EMA200'] = chart_h1['Close'].ewm(span=200, adjust=False).mean()
    mc = mpf.make_marketcolors(up='#089981', down='#F23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=False)

    fig = mpf.figure(figsize=(14, 6), style=style)
    ax1, ax2 = fig.add_subplot(1, 2, 1), fig.add_subplot(1, 2, 2)

    mpf.plot(chart_h1, type='candle', ax=ax1, addplot=[mpf.make_addplot(chart_h1['EMA200'], ax=ax1, color='gold')], axtitle="1H Macro")
    mpf.plot(chart_5m, type='candle', ax=ax2, axtitle="5M Structure")

    if direction and entry > 0:
        trg = len(chart_5m) - 1
        tp_b = patches.Rectangle((trg, entry), 10, (tp - entry if direction == "BUY" else entry - tp), facecolor='#26a69a', alpha=0.25)
        sl_b = patches.Rectangle((trg, sl if direction == "SELL" else entry), 10, (entry - sl if direction == "BUY" else sl - entry), facecolor='#ef5350', alpha=0.25)
        ax2.add_patch(tp_b)
        ax2.add_patch(sl_b)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

def log_trade_result(direction: str, entry: float, exit_price: float, result_type: str, points: float):
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["timestamp", "direction", "entry", "exit_price", "result", "points"])
        timestamp = datetime.datetime.now(timezone.utc).isoformat()
        writer.writerow([timestamp, direction, entry, exit_price, result_type, round(points, 2)])

def generate_weekly_performance_report() -> str:
    if not os.path.exists(TRADE_LOG_FILE):
        return "📊 *WEEKLY PERFORMANCE REPORT*\n\n⚠️ No trade history file found."
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty:
            return "📊 *WEEKLY PERFORMANCE REPORT*\n\nNo trades logged yet."

        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        now_utc = datetime.datetime.now(timezone.utc)
        monday_start = (now_utc - datetime.timedelta(days=now_utc.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_df = df[(df['timestamp'] >= monday_start) & (df['timestamp'] <= now_utc)]

        if weekly_df.empty:
            return f"📈 *WEEKLY TRADING SUMMARY*\n\nZero trades executed in this window."

        total_trades = len(weekly_df)
        tp_trades = weekly_df[weekly_df['result'].str.contains('TP')]
        sl_trades = weekly_df[weekly_df['result'] == 'SL_HIT']
        be_trades = weekly_df[weekly_df['result'] == 'BREAK_EVEN']

        tp_count, sl_count, be_count = len(tp_trades), len(sl_trades), len(be_trades)
        gross_tp = tp_trades['points'].sum() if not tp_trades.empty else 0.0
        gross_sl = abs(sl_trades['points'].sum()) if not sl_trades.empty else 0.0
        net_points = gross_tp - gross_sl
        win_rate = (tp_count / total_trades * 100) if total_trades > 0 else 0.0

        return (
            f"🏆 *WEEKLY ACCOUNTABILITY REPORT* 🏆\n"
            f"🗓 `Session: {monday_start.strftime('%b %d')} – {now_utc.strftime('%b %d')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 Total Trades: `{total_trades}` | Win Rate: `{win_rate:.1f}%`\n"
            f"✅ TP Hit: `{tp_count}` | ❌ SL Hit: `{sl_count}` | 🛡️ BE Exits: `{be_count}`\n\n"
            f"💰 *NET BALANCE:* *{'+' if net_points >= 0 else ''}{net_points:.2f} POINTS*\n"
            f"🔒 Market Closed for Weekend."
        )
    except Exception as err:
        return f"⚠️ Error generating weekly report: `{err}`"

async def friday_10pm_accountability_scheduler(send_alert_func):
    while True:
        try:
            now = datetime.datetime.now(timezone.utc)
            days_until_friday = (4 - now.weekday()) % 7
            target_friday = (now + datetime.timedelta(days=days_until_friday)).replace(hour=22, minute=0, second=0, microsecond=0)
            if now >= target_friday:
                target_friday += datetime.timedelta(days=7)

            sleep_seconds = (target_friday - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            report_msg = generate_weekly_performance_report()
            await send_alert_func(report_msg)
            await asyncio.sleep(120)
        except Exception as e:
            print(f"[SCHEDULER ERROR] {e}", flush=True)
            await asyncio.sleep(60)

def refresh_gemini_models():
    global AVAILABLE_GEMINI_MODELS
    if gemini_client:
        try:
            fetched = [m.name.replace("models/", "") for m in gemini_client.models.list() if "flash" in m.name.lower()]
            if fetched:
                AVAILABLE_GEMINI_MODELS = fetched
        except Exception as e:
            print(f"[GEMINI DISCOVERY WARNING] {e}", flush=True)

# ---------------------------------------------------------
# AI EVALUATION ENGINES
# ---------------------------------------------------------
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
    summary = f"Price: {current_price:.2f} USD | ATR (Scaled): {atr_val:.2f}"
    g_res, o_res = await asyncio.gather(evaluate_with_gemini(chart_bytes, summary), evaluate_with_openrouter_free(chart_bytes, summary))

    if g_res.get("failed") and o_res.get("failed"):
        return {"approved": False, "reason": "Both Vision APIs offline."}

    active_res = g_res if not g_res.get("failed") else o_res
    if not active_res.get("trade_approved", False):
        return {"approved": False, "reason": active_res.get("reason", "Not approved")}

    direction = active_res.get("direction")
    raw_entry = float(active_res.get("recommended_entry", current_price))
    
    order_type = "MARKET" if abs(current_price - raw_entry) <= 1.00 else "LIMIT"
    entry_price = current_price if order_type == "MARKET" else (raw_entry - ENTRY_BUFFER if direction == "SELL" else raw_entry + ENTRY_BUFFER)

    sl_distance = max(atr_val, 1.50)
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

# ---------------------------------------------------------
# DERIV API & ACTIVE TRADE ENGINE
# ---------------------------------------------------------
async def deriv_request(req: dict) -> dict:
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send(json.dumps(req))
            return json.loads(await ws.recv())
    except Exception:
        return {}

async def fetch_deriv_candles(granularity: int = 300, count: int = 200) -> pd.DataFrame:
    res = await deriv_request({"ticks_history": SYMBOL, "adjust_start_time": 1, "count": count, "end": "latest", "granularity": granularity, "style": "candles"})
    candles = res.get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame([{'time': pd.to_datetime(c['epoch'], unit='s', utc=True), 'open': float(c['open']), 'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close'])} for c in candles])
    df.set_index('time', inplace=True)
    return df

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
        print(f"[TELEGRAM ERROR] {e}", flush=True)

def update_and_check_active_setup(current_price: float) -> bool:
    global post_loss_cooldown_until
    now_utc = datetime.datetime.now(timezone.utc)
    if now_utc < post_loss_cooldown_until or not active_setup["is_active"]:
        return False

    direction = active_setup["direction"]
    entry = active_setup["entry_price"]
    sl = active_setup["sl_price"]
    tp1 = active_setup["tp1_price"]
    tp2 = active_setup["tp2_price"]

    if not active_setup["entry_filled"]:
        if (direction == "BUY" and current_price <= entry) or (direction == "SELL" and current_price >= entry):
            active_setup["entry_filled"] = True
            asyncio.create_task(send_telegram_alert(f"✅ *LIMIT ORDER FILLED:* {direction} @ `${current_price:.2f}`"))
            return True
        return True

    if not active_setup["tp1_hit"] and ((direction == "BUY" and current_price >= tp1) or (direction == "SELL" and current_price <= tp1)):
        active_setup["tp1_hit"] = True
        active_setup["sl_price"] = entry
        log_trade_result(direction, entry, tp1, "TP1_HIT", abs(tp1 - entry))
        asyncio.create_task(send_telegram_alert(f"🎯 *TAKE PROFIT 1 HIT:* SL moved to Break-Even (`${entry:.2f}`)."))

    if (direction == "BUY" and current_price <= sl) or (direction == "SELL" and current_price >= sl):
        active_setup["is_active"] = False
        if active_setup["tp1_hit"]:
            log_trade_result(direction, entry, entry, "BREAK_EVEN", 0.0)
            asyncio.create_task(send_telegram_alert(f"🛡️ *CLOSED AT BREAK-EVEN*"))
        else:
            log_trade_result(direction, entry, sl, "SL_HIT", -abs(entry - sl))
            post_loss_cooldown_until = now_utc + timedelta(minutes=40)
            asyncio.create_task(send_telegram_alert(f"❌ *STOP LOSS HIT:* Locking trading worker for 40 mins."))
        return True

    if (direction == "BUY" and current_price >= tp2) or (direction == "SELL" and current_price <= tp2):
        log_trade_result(direction, entry, tp2, "TP2_HIT", abs(tp2 - entry))
        active_setup["is_active"] = False
        asyncio.create_task(send_telegram_alert(f"🚀 *FINAL TAKE PROFIT 2 HIT!* All targets smashed."))
        return False

    return True

# ---------------------------------------------------------
# MAIN WORKER LOOP & FASTAPI LIFESPAN
# ---------------------------------------------------------
async def deriv_trading_worker():
    global last_trade_time, active_setup
    
    # Safe Model Initialization (Prevents Server Shutdown on Network Errors)
    try:
        refresh_gemini_models()
    except Exception as init_err:
        print(f"[WORKER INITIALIZATION WARNING] {init_err}", flush=True)

    while True:
        try:
            await asyncio.sleep(30)
            now_utc = datetime.datetime.now(timezone.utc)

            if (now_utc - last_trade_time).total_seconds() < (COOLDOWN_MINUTES * 60) or not is_within_killzone():
                continue

            df_5m = await fetch_deriv_candles(granularity=300, count=200)
            df_h1 = await fetch_deriv_candles(granularity=3600, count=50)

            if df_5m.empty or df_h1.empty:
                continue

            current_price = df_5m['close'].iloc[-1]

            if update_and_check_active_setup(current_price):
                continue

            atr_val = calculate_atr(df_5m, multiplier=ATR_MULTIPLIER)
            eval_bytes = await asyncio.to_thread(render_dual_panel_chart, df_5m, df_h1)
            consensus = await get_dual_ai_consensus(eval_bytes, current_price, atr_val)

            if not consensus["approved"]:
                continue

            direction = consensus["direction"]

            if not check_heavy_momentum_filter(df_5m, df_h1, direction):
                continue

            rejection_check = verify_post_sweep_rejection(df_5m, direction)
            if not rejection_check["valid"]:
                print(f"[REJECTED EXECUTION] {rejection_check['reason']}", flush=True)
                continue

            sl_price = rejection_check["stop_loss"]
            entry_price = rejection_check["entry_price"]
            order_type = consensus["order_type"]

            sl_dist = abs(entry_price - sl_price)
            tp1_price = (entry_price + sl_dist) if direction == "BUY" else (entry_price - sl_dist)
            tp2_price = consensus["tp2_price"]

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
                f"_{rejection_check['reason']}_"
            )
            await send_telegram_alert(msg, final_chart)

        except Exception as err:
            print(f"[WORKER ERROR] {err}", flush=True)
            await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(deriv_trading_worker())
    summary_task = asyncio.create_task(friday_10pm_accountability_scheduler(send_telegram_alert))
    yield
    worker_task.cancel()
    summary_task.cancel()
    await http_client.aclose()

app = FastAPI(title="Aura AI Engine", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ONLINE"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
