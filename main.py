import os
import time
from datetime import datetime
import threading
import requests
import pandas as pd
from flask import Flask

# =====================================================================
# 1. CORE APP & FLASK DEFINITION
# =====================================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Anti Gold Sniper PAB Cloud Core is Online and Functional."

# =====================================================================
# 2. HELPER FUNCTIONS & BOT CONFIGURATION
# =====================================================================
def load_app_settings():
    """
    Returns required tokens, channel targets, and trading pairs.
    """
    return {
        "bot_token": "7249007464:AAEMH_8N08P5Y4uU3rYp9vH8fT7W8mN4bX0", # Hardcoded secure bot token
        "channel_id": "-1002213745281",                                # Target broadcast channel
        "symbol": "XAUUSD"
    }

def send_telegram_alert(message, settings):
    """
    Dispatches notifications and trading signals directly to Telegram.
    """
    url = f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage"
    payload = {
        "chat_id": settings['channel_id'],
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Telegram transmission failure: {e}")
        return False

def check_heartbeat(settings):
    """
    Ensures the live server deployment notifies the channel upon initial startup.
    """
    if not hasattr(check_heartbeat, "has_fired"):
        msg = "🚀 **Anti Gold PAB Cloud Shift Successful**\n\nSmart Money Concepts scanner is online and monitoring XAU/USD H1 trend bias and M15 setups 24/7 on Render servers."
        send_telegram_alert(msg, settings)
        check_heartbeat.has_fired = True

def fetch_dataframe_from_api(timeframe):
    """
    Fetches raw OHLCV market metrics from the trading data pipeline.
    """
    url = f"https://api.taapi.io/candles"
    settings = load_app_settings()
    params = {
        "secret": "YOUR_TAAPI_SECRET_KEY", # Replace with your active TAAPI token if needed
        "exchange": "binance",
        "symbol": "GOLD/USDT",
        "interval": "1h" if timeframe == "1H" else "15m",
        "backcandles": "50"
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            df = pd.DataFrame(data)
            df['close'] = pd.to_numeric(df['close'])
            df['high'] = pd.to_numeric(df['high'])
            df['low'] = pd.to_numeric(df['low'])
            df['open'] = pd.to_numeric(df['open'])
            return df
        return None
    except Exception as e:
        print(f"❌ API Connection failure on {timeframe}: {e}")
        return None

# =====================================================================
# 3. TRADING ENGINE: SMC STRUCTURE & ANALYSIS
# =====================================================================
def check_h1_trend(df):
    """
    Analyzes H1 market structure via swing points to establish clear trend bias.
    Returns 'BULLISH', 'BEARISH', or 'RANGING'.
    """
    closes = df['close'].tolist()
    highs = df['high'].tolist()
    lows = df['low'].tolist()
    
    # Simple high/low sequence check over the last 15 candles
    last_highs = highs[-15:]
    last_lows = lows[-15:]
    
    if max(last_highs) == last_highs[-1] or closes[-1] > closes[-5]:
        return "BULLISH"
    elif min(last_lows) == last_lows[-1] or closes[-1] < closes[-5]:
        return "BEARISH"
    return "RANGING"

def scan_for_sniper_setups(df_m15, trend_bias):
    """
    Scans M15 structure for mitigating Order Blocks matching H1 direction.
    Fires direct signals when a valid setup hits structural criteria.
    """
    settings = load_app_settings()
    current_price = df_m15['close'].iloc[-1]
    
    # Simple SMC Order Block Identification (Last counter-candle before expansion)
    last_candle_green = df_m15['close'].iloc[-1] > df_m15['open'].iloc[-1]
    prev_candle_red = df_m15['close'].iloc[-2] < df_m15['open'].iloc[-2]
    
    if trend_bias == "BULLISH" and last_candle_green and prev_candle_red:
        ob_zone = df_m15['low'].iloc[-2]
        if current_price <= ob_zone * 1.001:  # Price within mitigation range
            msg = (
                f"⚡ **ANTI GOLD SNIPER SIGNAL** ⚡\n\n"
                f"🏆 **Pair:** {settings['symbol']} (Gold)\n"
                f"📈 **Direction:** BUY / LONG\n"
                f"🔥 **H1 Bias:** {trend_bias}\n"
                f"🧱 **M15 Mitigation:** Order Block Zone Hit near {ob_zone:.2f}\n"
                f"💵 **Current Entry:** {current_price:.2f}"
            )
            send_telegram_alert(msg, settings)
            
    elif trend_bias == "BEARISH" and not last_candle_green and not prev_candle_red:
        ob_zone = df_m15['high'].iloc[-2]
        if current_price >= ob_zone * 0.999:  # Price within mitigation range
            msg = (
                f"⚡ **ANTI GOLD SNIPER SIGNAL** ⚡\n\n"
                f"🏆 **Pair:** {settings['symbol']} (Gold)\n"
                f"📉 **Direction:** SELL / SHORT\n"
                f"🔥 **H1 Bias:** {trend_bias}\n"
                f"🧱 **M15 Mitigation:** Order Block Zone Hit near {ob_zone:.2f}\n"
                f"💵 **Current Entry:** {current_price:.2f}"
            )
            send_telegram_alert(msg, settings)

# =====================================================================
# 4. RUN ENGINE (BACKGROUND WORKER THREAD)
# =====================================================================
def market_scanner_loop():
    print("🎯 Cloud Scanning Engine Initialized")
    current_settings = load_app_settings()
    check_heartbeat(current_settings)
    
    while True:
        try:
            current_settings = load_app_settings()
            check_heartbeat(current_settings)
            
            h1 = fetch_dataframe_from_api("1H")
            m15 = fetch_dataframe_from_api("15M")
            
            if h1 is not None and m15 is not None:
                trend = check_h1_trend(h1)
                scan_for_sniper_setups(m15, trend)
                
            print("🛩️ Scan complete. Sleeping for 15 minutes...")
        except Exception as e:
            print(f"⚠️ Loop error caught: {e}")
            
        time.sleep(900)

# Kick off the scanner loop safely after everything above has fully loaded
threading.Thread(target=market_scanner_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    
