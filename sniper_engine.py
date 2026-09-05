import os
import json
import asyncio
import base64
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import websockets
from datetime import datetime, timezone

# ==================== CONFIGURATION ====================
DERIV_APP_ID = "YOUR_DERIV_APP_ID"
DERIV_TOKEN = "YOUR_DERIV_TOKEN"
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
OPENROUTER_API_KEY = "YOUR_OPENROUTER_API_KEY"

SYMBOL = "frxXAUUSD"         # Gold symbol on Deriv
STAKE_AMOUNT = 2.00          # Fixed Stake ($2.00)
SL_AMOUNT = 2.00             # Fixed Risk Cap ($2.00)
MIN_RRR = 2.0                # Strict minimum 1:2 Risk-Reward Ratio
MAX_MULTIPLIER = 200         # Hard cap to prevent Deriv API rejection

LOG_FILE = "trading_engine.log"

# ==================== LOGGING UTILITY ====================
def log_event(message: str):
    """Logs activity to both terminal console and local log file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    formatted_msg = f"[{timestamp}] {message}"
    print(formatted_msg)
    with open(LOG_FILE, "a") as f:
        f.write(formatted_msg + "\n")

# ==================== SESSION & TREND FILTERS ====================
def is_within_killzone() -> bool:
    """
    Checks if current UTC time falls within high-volume volatility windows:
    - London Open: 07:00 - 11:00 UTC
    - New York Session: 13:00 - 17:00 UTC
    """
    now_utc = datetime.now(timezone.utc).time()
    
    london_start = datetime.strptime("07:00", "%H:%M").time()
    london_end = datetime.strptime("11:00", "%H:%M").time()
    
    ny_start = datetime.strptime("13:00", "%H:%M").time()
    ny_end = datetime.strptime("17:00", "%H:%M").time()
    
    return (london_start <= now_utc <= london_end) or (ny_start <= now_utc <= ny_end)

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculates Average True Range (ATR) for dynamic volatility stops."""
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(window=period).mean().iloc[-1])

# ==================== DERIV WEBSOCKET ENGINE ====================
async def deriv_request(request_payload: dict, authorize: bool = False) -> dict:
    """Connects to Deriv WebSocket API and returns parsed response."""
    url = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    async with websockets.connect(url) as ws:
        if authorize:
            await ws.send(json.dumps({"authorize": DERIV_TOKEN}))
            auth_res = json.loads(await ws.recv())
            if "error" in auth_res:
                log_event(f"[AUTH ERROR] {auth_res['error']['message']}")
                return auth_res

        await ws.send(json.dumps(request_payload))
        return json.loads(await ws.recv())

async def fetch_deriv_candles(granularity: int = 300, count: int = 150) -> pd.DataFrame:
    """Fetches clean OHLC candle data directly from Deriv API."""
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

# ==================== CHART GENERATOR (DUAL TIME FRAME) ====================
def generate_dual_timeframe_chart(df_1h: pd.DataFrame, df_5m: pd.DataFrame, file_path: str = "chart.png"):
    """Generates side-by-side context (1H) and execution (5M) charts."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6), facecolor='#0e1117')

    for ax, df, title in [(ax1, df_1h, "1-Hour Macro Context"), (ax2, df_5m, "5-Minute Local Structure")]:
        ax.set_facecolor('#0e1117')
        ax.tick_params(colors='white')
        ax.grid(True, color='#262730', linestyle='--', alpha=0.5)
        ax.set_title(title, color='white', fontsize=12, fontweight='bold')

        df['EMA200'] = df['close'].ewm(span=200, adjust=False).mean()
        ax.plot(df.index, df['close'], color='#00e676', linewidth=1.5, label='Price')
        ax.plot(df.index, df['EMA200'], color='#ff9100', linewidth=1.2, linestyle=':', label='200 EMA')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    plt.tight_layout()
    plt.savefig(file_path, dpi=150)
    plt.close()

# ==================== DERIV ORDER EXECUTION ====================
async def execute_deriv_multiplier(action: str, target_tp_usd: float, dynamic_multiplier: int) -> dict:
    """Submits multiplier order payload to Deriv API."""
    multiplier = min(dynamic_multiplier, MAX_MULTIPLIER)
    
    proposal_req = {
        "proposal": 1,
        "amount": STAKE_AMOUNT,
        "basis": "stake",
        "contract_type": action,
        "currency": "USD",
        "symbol": SYMBOL,
        "multiplier": multiplier,
        "limit_order": {
            "stop_loss": float(SL_AMOUNT),
            "take_profit": float(round(target_tp_usd, 2))
        }
    }

    proposal_res = await deriv_request(proposal_req, authorize=True)
    proposal = proposal_res.get("proposal", {})
    proposal_id = proposal.get("id")

    if not proposal_id:
        reason = proposal_res.get("error", {}).get("message", "Invalid Proposal Parameters")
        return {"status": "FAILED", "reason": reason}

    buy_req = {"buy": proposal_id, "price": STAKE_AMOUNT}
    buy_res = await deriv_request(buy_req, authorize=True)

    if "buy" in buy_res:
        return {"status": "EXECUTED", "contract_id": buy_res["buy"].get("contract_id")}
    
    return {"status": "FAILED", "reason": buy_res.get("error", {}).get("message", "Order Buy Failed")}

# ==================== MAIN AUTOMATION ENGINE ====================
async def run_trading_engine():
    # 1. Check Trading Session (Killzone Filter)
    if not is_within_killzone():
        log_event("[FILTERED] Outside London/NY Killzones. Execution paused to avoid low-volume range chop.")
        return

    # 2. Fetch Candle Data
    df_1h = await fetch_deriv_candles(granularity=3600, count=100)
    df_5m = await fetch_deriv_candles(granularity=300, count=100)

    if df_1h.empty or df_5m.empty:
        log_event("[DATA ERROR] Failed to fetch price data from Deriv API.")
        return

    current_price = df_5m['close'].iloc[-1]
    ema_200_1h = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]

    # Macro Trend Rule
    macro_bias = "BULLISH" if current_price > ema_200_1h else "BEARISH"

    # 3. Dynamic Volatility & RRR Validation
    atr_val = calculate_atr(df_5m, period=14)
    sl_distance = atr_val * 2.0  # Dynamic Stop Loss based on 2x ATR

    # Example setup evaluation (BUY in Bullish HTF, SELL in Bearish HTF)
    if macro_bias == "BULLISH":
        proposed_action = "BUY"
        contract_type = "MULTUP"
        signal_display = "🟢 BUY (LONG)"
        sl_price = current_price - sl_distance
        tp_target_price = current_price + (sl_distance * 2.2)  # Targets 2.2 RRR
    else:
        proposed_action = "SELL"
        contract_type = "MULTDOWN"
        signal_display = "🔴 SELL (SHORT)"
        sl_price = current_price + sl_distance
        tp_target_price = current_price - (sl_distance * 2.2)

    tp_distance = abs(tp_target_price - current_price)
    calculated_rrr = tp_distance / sl_distance

    # Hard Enforcement: Minimum 1:2 Risk-to-Reward Ratio
    if calculated_rrr < MIN_RRR:
        log_event(f"[FILTERED] RRR too low ({calculated_rrr:.2f}:1). Requires minimum {MIN_RRR}:1. Trade discarded.")
        return

    # 4. Dynamic Multiplier Calculation
    raw_multiplier = int((SL_AMOUNT * current_price) / (STAKE_AMOUNT * sl_distance))
    valid_steps = [10, 20, 30, 50, 100, 200, 300]
    final_multiplier = min(valid_steps, key=lambda x: abs(x - raw_multiplier))

    target_tp_usd = SL_AMOUNT * calculated_rrr

    # Generate visual chart before execution
    generate_dual_timeframe_chart(df_1h, df_5m, "chart_analysis.png")

    # 5. Order Execution
    exec_result = await execute_deriv_multiplier(contract_type, target_tp_usd, final_multiplier)

    # 6. Telegram Broadcast Payload
    status_text = f"EXECUTED (Contract ID: {exec_result.get('contract_id')})" if exec_result["status"] == "EXECUTED" else f"FAILED ({exec_result.get('reason')})"

    broadcast_message = (
        f"🎯 **DUAL-AI ENGINE v4.3**\n"
        f"⚡ **HIGH-CONFLUENCE SMC SETUP**\n\n"
        f"🏆 **Asset:** XAUUSD (Gold)\n"
        f"📢 **ACTION:** {signal_display}\n"
        f"📍 **Entry Price:** ${current_price:.2f}\n"
        f"🛑 **Stop Loss Price:** ${sl_price:.2f}\n"
        f"🎯 **Take Profit Target:** ${tp_target_price:.2f}\n"
        f"⚖️ **Calculated RRR:** 1:{calculated_rrr:.2f}\n"
        f"💵 **Risk Cap (SL):** -${SL_AMOUNT:.2f}\n"
        f"💰 **Target Gain (TP):** +${target_tp_usd:.2f}\n"
        f"🚀 **Leverage:** {final_multiplier}x Multiplier\n"
        f"-----------------------------------\n"
        f"🧠 **MACRO BIAS:** {macro_bias} (1H Structure)\n"
        f"📡 **Execution Status:** {status_text}"
    )

    log_event(f"\n{broadcast_message}")

if __name__ == "__main__":
    asyncio.run(run_trading_engine())
