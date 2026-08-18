import os
import io
import logging
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HFM_Engine")

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

SYMBOL = "XAUUSD"
RR_RATIO = 3.0
LOOKBACK_CANDLES = 24

def check_smc_setup(df):
    if len(df) < LOOKBACK_CANDLES + 2:
        return None
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    
    tr = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]))
    atr = np.mean(tr[-14:]) if len(tr) >= 14 else 2.0
    
    recent_high = np.max(highs[-(LOOKBACK_CANDLES+1):-2])
    recent_low = np.min(lows[-(LOOKBACK_CANDLES+1):-2])
    
    curr_high, curr_low, curr_close = highs[-1], lows[-1], closes[-1]
    
    if curr_high > recent_high and curr_close < recent_high:
        entry = curr_close
        sl = curr_high + max(atr * 0.5, 1.5)
        tp = entry - ((sl - entry) * RR_RATIO)
        return {"signal": "SELL", "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "pattern": "Liquidity Sweep (High)"}

    if curr_low < recent_low and curr_close > recent_low:
        entry = curr_close
        sl = curr_low - max(atr * 0.5, 1.5)
        tp = entry + ((entry - sl) * RR_RATIO)
        return {"signal": "BUY", "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "pattern": "Liquidity Sweep (Low)"}
        
    return None

def send_telegram_update(df, setup):
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
        f"🎯 *HOLY GRAIL SIGNAL*\n\n"
        f"📍 *Action:* `{setup['signal']}`\n"
        f"💰 *Entry:* `{setup['entry']}`\n"
        f"🛑 *Stop Loss:* `{setup['sl']}`\n"
        f"🌟 *Take Profit (1:3):* `{setup['tp']}`\n\n"
        f"📋 *Pattern:* `{setup['pattern']}`"
    )
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", buf, "image/png")}
    data = {"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "Markdown"}
    requests.post(url, data=data, files=files, timeout=15)

@app.route("/webhook", methods=["POST"])
def webhook_receiver():
    req_data = request.get_json(silent=True)
    if not req_data or "candles" not in req_data:
        return jsonify({"status": "error"}), 400
        
    df = pd.DataFrame(req_data["candles"])
    df['time'] = pd.to_datetime(df['time'])
    df.set_index('time', inplace=True)
    
    setup = check_smc_setup(df)
    if setup:
        send_telegram_update(df, setup)
        return jsonify({"status": "signal_found", "trade": setup}), 200
            
    return jsonify({"status": "scanning"}), 200

@app.route("/")
def health_check():
    return jsonify({"status": "online", "engine": "Active"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
