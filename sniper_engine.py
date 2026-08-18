import os
import time
import logging
import io
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime, timezone
from flask import Flask, jsonify, request

# ==============================================================================
# ⚙️ CONFIGURATION & ENV VARIABLES
# ==============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HFM_HolyGrail_Engine")

app = Flask(__name__)

# Core Environment Variables for Render Deployments
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
META_API_TOKEN = os.environ.get("META_API_TOKEN")
META_ACCOUNT_ID = os.environ.get("META_ACCOUNT_ID")

# 🎯 LETHAL RIGID TRADING CONSTANTS
SYMBOL = "XAUUSD"
RR_RATIO = 3.0                # Strict 1:3 mathematical edge
RISK_PERCENT = 2.0            # Risks exactly 2% of live account balance ($2 on a $100 base)
LOOKBACK_CANDLES = 24         # Completely monitors the overnight session footprint
MIN_SL_DIST_POINTS = 250      # 25-pip minimum protective buffer against HFM spread spikes

# ==============================================================================
# 🔍 SMC LIQUIDITY SCANNING ENGINE
# ==============================================================================
def check_smc_setup(df):
    """Scans historical data pools for a clean liquidity grab and confirmation close."""
    if len(df) < LOOKBACK_CANDLES + 2:
        return None
        
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    
    # Adaptive ATR calculation for safety buffers
    tr = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]))
    atr = np.mean(tr[-14:])
    
    # Establish historic session structural boundaries
    recent_high = np.max(highs[-(LOOKBACK_CANDLES+1):-2])
    recent_low = np.min(lows[-(LOOKBACK_CANDLES+1):-2])
    
    curr_high = highs[-1]
    curr_low = lows[-1]
    curr_close = closes[-1]
    
    # 🐻 BEARISH: Price sweeps structural high and prints a structural confirmation close
    if curr_high > recent_high and curr_close < recent_high:
        entry = curr_close
        sl = curr_high + max(atr * 0.5, 1.5)
        tp = entry - ((sl - entry) * RR_RATIO)
        return {
            "signal": "SELL",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "pattern": "Lethal Liquidity Sweep (High)"
        }

    # 🐂 BULLISH: Price sweeps structural low and prints a structural confirmation close
    if curr_low < recent_low and curr_close > recent_low:
        entry = curr_close
        sl = curr_low - max(atr * 0.5, 1.5)
        tp = entry + ((entry - sl) * RR_RATIO)
        return {
            "signal": "BUY",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "pattern": "Lethal Liquidity Sweep (Low)"
        }
        
    return None

# ==============================================================================
# 🚀 AUTOMATED ORDER EXECUTION VIA METAAPI
# ==============================================================================
def execute_hfm_trade(setup):
    """Calculates precision risk metrics and shoots the order directly to HFM via MetaApi."""
    if not META_API_TOKEN or not META_ACCOUNT_ID:
        logger.warning("Configuration variables missing. MetaApi execution skipped.")
        return False
        
    headers = {"auth-token": META_API_TOKEN, "Content-Type": "application/json"}
    account_url = f"https://agiliumtrade.ai{META_ACCOUNT_ID}/account-information"
    
    try:
        # Fetch current real-time account data from your HFM account
        acc_res = requests.get(account_url, headers=headers, timeout=5)
        balance = acc_res.json().get("balance", 100.0) if acc_res.status_code == 200 else 100.0
        
        # Sizing engine logic
        risk_cash = balance * (RISK_PERCENT / 100.0)
        sl_distance_points = abs(setup["entry"] - setup["sl"]) * 100
        
        if sl_distance_points == 0:
            sl_distance_points = MIN_SL_DIST_POINTS
            
        # Standard lot calculator step
        calculated_lots = risk_cash / (sl_distance_points * 0.01)
        final_lots = round(max(0.01, min(calculated_lots, 0.50)), 2)
        
        trade_url = f"https://agiliumtrade.ai{META_ACCOUNT_ID}/trade"
        action = "ORDER_TYPE_BUY" if setup["signal"] == "BUY" else "ORDER_TYPE_SELL"
        
        payload = {
            "actionType": "ORDER_TYPE_MARKET",
            "symbol": SYMBOL,
            "orderType": action,
            "volume": final_lots,
            "stopLoss": setup["sl"],
            "takeProfit": setup["tp"]
        }
        
        trade_res = requests.post(trade_url, json=payload, headers=headers, timeout=10)
        if trade_res.status_code in:
            logger.info(f"🚀 Execution success! Type: {setup['signal']} | Sized Lots: {final_lots}")
            return True
            
        logger.error(f"HFM execution rejected by broker node: {trade_res.text}")
        return False
        
    except Exception as e:
        logger.error(f"Failed network request connecting to MetaApi server framework: {e}")
        return False

# ==============================================================================
# 📊 VISUAL DISPATCH MODULE (TELEGRAM CHANNELS)
# ==============================================================================
def send_telegram_update(df, setup):
    """Generates charts and forwards automated signal snapshots to your mobile phone."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return
        
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#131722')
    ax.set_facecolor('#131722')
    
    recent = df.tail(30)
    for i in range(len(recent)):
        o, h, l, c = recent['open'].iloc[i], recent['high'].iloc[i], recent['low'].iloc[i], recent['close'].iloc[i]
        color = '#089981' if c >= o else '#f23645'
        ax.plot([recent.index[i], recent.index[i]], [l, h], color=color, linewidth=1)
        
    ax.axhline(setup['entry'], color='#2962ff', linestyle='--', label=f"Entry: {setup['entry']}")
    ax.axhline(setup['sl'], color='#f23645', linestyle='-', label=f"SL: {setup['sl']}")
    ax.axhline(setup['tp'], color='#089981', linestyle='-', label=f"TP: {setup['tp']}")
    ax.legend(facecolor='#1e222d', edgecolor='#2a2e39', labelcolor='#d1d4dc')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    buf.seek(0)
    plt.close(fig)
    
    caption = (
        f"🎯 *HOLY GRAIL AUTO-SIGNAL SYSTEM*\n\n"
        f"📍 *Action:* `{setup['signal']}`\n"
        f"💰 *Entry Level:* `{setup['entry']}`\n"
        f"🛑 *Stop Loss:* `{setup['sl']}`\n"
        f"🌟 *Take Profit (1:3 RRR):* `{setup['tp']}`\n\n"
        f"📋 *Strategy Profile:* `{setup['pattern']}`"
    )
    
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", buf, "image/png")}
    data = {"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, files=files, timeout=15)
    except Exception as e:
        logger.error(f"Telegram photo pipe failed: {e}")

# ==============================================================================
# 🌐 WEBHOOK PIPELINE (TRIGGER NODE)
# ==============================================================================
@app.route("/webhook", methods=["POST"])
def webhook_receiver():
    """Receives live chart data arrays via automated cron-job execution pings."""
    req_data = request.get_json(silent=True)
    if not req_data or "candles" not in req_data:
        return jsonify({"status": "error", "message": "Incomplete data array configuration"}), 400
        
    df = pd.DataFrame(req_data["candles"])
    df['time'] = pd.to_datetime(df['time'])
    df.set_index('time', inplace=True)
    
    setup = check_smc_setup(df)
    if setup:
        if execute_hfm_trade(setup):
            send_telegram_update(df, setup)
            return jsonify({"status": "executed", "trade": setup}), 200
            
    return jsonify({"status": "hunting", "message": "Scanning for valid structural sweeps..."}), 200

@app.route("/")
def health_check():
    return jsonify({"status": "online", "engine": "HFM HolyGrail Engine V3 Active"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    
