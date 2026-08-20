import io
import os
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Headless backend for cloud deployment
import matplotlib.pyplot as plt
import mplfinance as mpf
from flask import Flask, jsonify, request

app = Flask(__name__)

# Environment Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculates Average True Range for dynamic volatility stop-loss sizing."""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def analyze_xauusd_structure(df: pd.DataFrame):
    """
    Evaluates candle data for Liquidity Sweeps, Fair Value Gaps (FVG), 
    and 1:3 RRR parameters dynamically across ALL sessions (24/5).
    """
    if len(df) < 20:
        return None

    df['atr'] = calculate_atr(df)
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    prev_2 = df.iloc[-3]

    # Dynamic ATR stop distance (9 - 16 points based on volatility)
    raw_atr = curr['atr'] if not np.isnan(curr['atr']) else 2.5
    stop_distance = max(9.0, min(16.0, raw_atr * 1.5))
    reward_distance = stop_distance * 3.0  # Strict 1:3 RRR Target

    # Bullish Liquidity Sweep & Imbalance Logic
    sweep_low = prev['low'] < df['low'].iloc[-10:-2].min()
    bullish_fvg = curr['low'] > prev_2['high']
    
    if sweep_low and bullish_fvg and curr['close'] > curr['open']:
        entry_price = curr['close']
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + reward_distance
        
        return {
            "signal": "BUY",
            "entry": round(entry_price, 2),
            "sl": round(stop_loss, 2),
            "tp": round(take_profit, 2),
            "risk_pts": round(stop_distance, 2),
            "reward_pts": round(reward_distance, 2)
        }

    # Bearish Liquidity Sweep & Imbalance Logic
    sweep_high = prev['high'] > df['high'].iloc[-10:-2].max()
    bearish_fvg = curr['high'] < prev_2['low']
    
    if sweep_high and bearish_fvg and curr['close'] < curr['open']:
        entry_price = curr['close']
        stop_loss = entry_price + stop_distance
        take_profit = entry_price - reward_distance

        return {
            "signal": "SELL",
            "entry": round(entry_price, 2),
            "sl": round(stop_loss, 2),
            "tp": round(take_profit, 2),
            "risk_pts": round(stop_distance, 2),
            "reward_pts": round(reward_distance, 2)
        }

    return None

def generate_signal_chart(df: pd.DataFrame, signal: dict) -> io.BytesIO:
    """Renders a TradingView-style candlestick chart with RRR zones & badges."""
    chart_df = df.tail(35).copy()
    
    if 'time' in chart_df.columns:
        chart_df['time'] = pd.to_datetime(chart_df['time'])
        chart_df.set_index('time', inplace=True)
    else:
        chart_df.index = pd.date_range(end=pd.Timestamp.now(), periods=len(chart_df), freq='15min')

    # Color Scheme (TradingView Style)
    mc = mpf.make_marketcolors(up='#089981', down='#f23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

    fig, axlist = mpf.plot(
        chart_df[['open', 'high', 'low', 'close']],
        type='candle',
        style=style,
        returnfig=True,
        figsize=(10, 6)
    )
    ax = axlist[0]

    entry = signal['entry']
    sl = signal['sl']
    tp = signal['tp']
    sig_type = signal['signal']
    total_candles = len(chart_df)

    # Risk / Reward Shaded Rectangles
    box_start = total_candles - 8
    box_end = total_candles + 2
    ax.set_xlim(-1, total_candles + 6)

    if sig_type == "BUY":
        ax.fill_between(range(box_start, box_end), entry, tp, color='#089981', alpha=0.25)
        ax.fill_between(range(box_start, box_end), sl, entry, color='#f23645', alpha=0.25)
    else:  # SELL
        ax.fill_between(range(box_start, box_end), sl, entry, color='#f23645', alpha=0.25)
        ax.fill_between(range(box_start, box_end), entry, tp, color='#089981', alpha=0.25)

    # Reference Price Lines
    ax.axhline(tp, color='#089981', linestyle='--', linewidth=1.2)
    ax.axhline(entry, color='#2962ff', linestyle='--', linewidth=1.2)
    ax.axhline(sl, color='#f23645', linestyle='--', linewidth=1.2)

    # Callout Badges
    bbox_tp = dict(boxstyle="round,pad=0.4", fc="#ffffff", ec="#089981", lw=1.5)
    bbox_entry = dict(boxstyle="round,pad=0.4", fc="#ffffff", ec="#2962ff", lw=1.5)
    bbox_sl = dict(boxstyle="round,pad=0.4", fc="#ffffff", ec="#f23645", lw=1.5)

    label_x = total_candles + 2.5
    ax.text(label_x, tp, f"TP: {tp}", va='center', bbox=bbox_tp, fontsize=9, fontweight='bold', color='#089981')
    ax.text(label_x, entry, f"Entry: {entry}", va='center', bbox=bbox_entry, fontsize=9, fontweight='bold', color='#2962ff')
    ax.text(label_x, sl, f"SL: {sl}", va='center', bbox=bbox_sl, fontsize=9, fontweight='bold', color='#f23645')

    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', bbox_inches='tight', dpi=150)
    img_buf.seek(0)
    plt.close(fig)
    return img_buf

def send_telegram_chart(image_buf: io.BytesIO, caption: str):
    """Dispatches generated chart image with caption to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", image_buf, "image/png")}
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=data, files=files, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram photo alert: {e}")

@app.route("/", methods=["GET"])
def health_check():
    """UptimeRobot ping target to keep worker active on Render."""
    return jsonify({"status": "active", "bot": "SniperEngine Gold 1:3 RRR"}), 200

@app.route("/webhook", methods=["POST"])
def receive_market_data():
    """Receives candles payload, calculates liquidity signals, renders chart, and alerts Telegram."""
    data = request.get_json()
    if not data or "candles" not in data:
        return jsonify({"error": "Invalid payload format"}), 400

    df = pd.DataFrame(data["candles"])
    signal = analyze_xauusd_structure(df)

    if signal:
        # Standard Copier-Friendly Text Format
        caption = (
            f"⚡ *GOLD (XAU/USD) SNIPER SIGNAL* ⚡\n\n"
            f"{signal['signal']} XAUUSD\n"
            f"Entry: `{signal['entry']}`\n"
            f"SL: `{signal['sl']}`\n"
            f"TP: `{signal['tp']}`\n\n"
            f"Risk: {signal['risk_pts']} pts | Reward: {signal['reward_pts']} pts\n"
            f"Target RRR: *1:3*"
        )
        
        # Render image and dispatch to Telegram
        chart_buffer = generate_signal_chart(df, signal)
        send_telegram_chart(chart_buffer, caption)
        
        return jsonify({"status": "signal_processed_with_chart", "data": signal}), 200

    return jsonify({"status": "no_signal"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
    
