import os
import io
import asyncio
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import requests
from flask import Flask, jsonify
from metaapi_cloud_sdk import MetaApi

app = Flask(__name__)

# Environment Configuration
META_API_TOKEN = os.getenv("META_API_TOKEN")
META_ACCOUNT_ID = os.getenv("META_ACCOUNT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_alert(message, image_bytes=None):
    """Dispatches text messages and generated charts to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARNING] Missing Telegram configuration. Skipping notification.")
        return

    try:
        if image_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', image_bytes, 'image/png')}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}
            requests.post(url, data=data, files=files, timeout=10)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")


def generate_chart_image(df_15m, df_1m, setup_type, entry_price, sl_price, tp_price):
    """Renders 15M trend context and 1M execution entry levels."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw={'height_ratios': [2, 1]})
    
    # 15M Higher Timeframe Context
    ax1.plot(df_15m.index, df_15m['close'], label='15M Price', color='black', alpha=0.7)
    ax1.plot(df_15m.index, df_15m['EMA200'], label='200 EMA', color='orange', linewidth=1.5)
    ax1.set_title(f"XAUUSD 15M Trend Filter ({setup_type})", fontsize=12, fontweight='bold')
    ax1.legend(loc='upper left')
    ax1.grid(True, linestyle='--', alpha=0.5)

    # 1M Lower Timeframe Execution
    ax2.plot(df_1m.index, df_1m['close'], label='1M Price', color='blue')
    ax2.axhline(entry_price, color='gray', linestyle='--', label=f'Entry: {entry_price:.2f}')
    ax2.axhline(sl_price, color='red', linestyle='-', label=f'SL: {sl_price:.2f}')
    ax2.axhline(tp_price, color='green', linestyle='-', label=f'TP: {tp_price:.2f}')
    ax2.set_title("1M Structure Execution", fontsize=10)
    ax2.legend(loc='upper left')
    ax2.grid(True, linestyle='--', alpha=0.5)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120)
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()


async def fetch_metaapi_data():
    """Fetches candle data using RPC connection get_candles method."""
    api = MetaApi(META_API_TOKEN)
    account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    # Candles requested via RPC connection instance
    candles_15m = await connection.get_candles('XAUUSD', '15m', None, 100)
    candles_1m = await connection.get_candles('XAUUSD', '1m', None, 40)
    
    price = await connection.get_symbol_price('XAUUSD')
    spread = abs(price['ask'] - price['bid'])

    # Format 15M Data
    df_15m = pd.DataFrame(candles_15m)
    df_15m['time'] = pd.to_datetime(df_15m['time'])
    df_15m.set_index('time', inplace=True)
    df_15m['EMA200'] = df_15m['close'].ewm(span=200, adjust=False).mean()

    # Format 1M Data
    df_1m = pd.DataFrame(candles_1m)
    df_1m['time'] = pd.to_datetime(df_1m['time'])
    df_1m.set_index('time', inplace=True)

    return df_15m, df_1m, spread


def analyze_market_structure(df_15m, df_1m):
    """SMC Logic Engine: Trend alignment, liquidity sweeps, MSS, and FVGs."""
    latest_15m_close = df_15m['close'].iloc[-1]
    latest_15m_ema = df_15m['EMA200'].iloc[-1]

    is_bullish_trend = latest_15m_close > latest_15m_ema
    is_bearish_trend = latest_15m_close < latest_15m_ema

    highs = df_1m['high']
    lows = df_1m['low']
    closes = df_1m['close']

    recent_high = highs.iloc[-15:-3].max()
    recent_low = lows.iloc[-15:-3].min()

    # Dynamic SL Range (9 to 16 points)
    sl_points = np.clip(abs(recent_high - recent_low), 9.0, 16.0)

    # Bullish Signal Validation
    if is_bullish_trend:
        sweep = lows.iloc[-3] < recent_low
        mss = closes.iloc[-1] > highs.iloc[-2]
        fvg = lows.iloc[-1] > highs.iloc[-3]

        if sweep and mss and fvg:
            entry = closes.iloc[-1]
            sl = entry - sl_points
            tp = entry + (sl_points * 3.0)  # Fixed 1:3 RR
            return "BULLISH_BUY", entry, sl, tp

    # Bearish Signal Validation
    elif is_bearish_trend:
        sweep = highs.iloc[-3] > recent_high
        mss = closes.iloc[-1] < lows.iloc[-2]
        fvg = highs.iloc[-1] < lows.iloc[-3]

        if sweep and mss and fvg:
            entry = closes.iloc[-1]
            sl = entry + sl_points
            tp = entry - (sl_points * 3.0)  # Fixed 1:3 RR
            return "BEARISH_SELL", entry, sl, tp

    return None, None, None, None


async def run_scan_logic():
    """Executes the analysis pipeline during cron requests."""
    df_15m, df_1m, spread = await fetch_metaapi_data()
    setup, entry, sl, tp = analyze_market_structure(df_15m, df_1m)

    if setup:
        chart_bytes = generate_chart_image(df_15m, df_1m, setup, entry, sl, tp)
        msg = (
            f"🎯 *GOLD (XAUUSD) SMC SIGNAL ALERT*\n\n"
            f"• *Type:* `{setup}`\n"
            f"• *Entry:* `{entry:.2f}`\n"
            f"• *Stop Loss:* `{sl:.2f}`\n"
            f"• *Take Profit (1:3):* `{tp:.2f}`\n"
            f"• *Spread:* `{spread:.2f} pts`"
        )
        send_telegram_alert(msg, chart_bytes)
        return {"status": "SIGNAL_DETECTED", "setup": setup, "entry": entry}

    return {"status": "NO_SETUP", "spread": spread}


@app.route('/')
def home():
    return jsonify({"service": "XAUUSD Sniper Engine", "status": "active"}), 200


@app.route('/scan')
def scan():
    try:
        result = asyncio.run(run_scan_logic())
        return jsonify(result), 200
    except Exception as e:
        print(f"[SCAN ERROR] {e}")
        return jsonify({"status": "ERROR", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
