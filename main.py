import os
import time
from datetime import datetime
import threading
import requests
import pandas as pd
from flask import Flask  # Keeps Render's automated cloud engine happy!

# ==========================================
# FLOW ENGINE INITIALIZER (FLASK WEB SERVICE)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Anti Gold Sniper PAB Cloud Core is Online and Functional.", 200

# ==========================================
# 1. HARDWARE PIPELINE
# ==========================================
BOT_TOKEN = "8818214054:AAG6O_KU6aHUWVAv2OaVSxFeTdisrjWMOIE"
CHANNEL_ID = "-1004342674434"
TWELVE_DATA_API_KEY = "132022ad17c149bbbfae07933b0d56fc"
FIREBASE_URL = "https://antigold-8417b-default-rtdb.firebaseio.com/settings.json"

LOOKBACK_CANDLES = 25
GOLD_PIP_VALUE = 0.1
BUFFER_PIPS = 15

last_ping = None
last_processed_candle = None

# ==========================================
# 2. TELEGRAM HEARTBEAT & TRANSMITTER
# ==========================================
def send_raw_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram issue: {e}")

def check_heartbeat(app_settings):
    global last_ping
    now = datetime.utcnow()
    
    if last_ping is None:
        last_ping = now
        send_raw_telegram_message("🚀 **Anti Gold PAB Cloud Shift Successful.** Running 24/7 on Render servers.")
        return

    if now >= last_ping + pd.Timedelta(hours=12):
        msg = (
            "✅ **Anti Gold PAB Heartbeat (Render Cloud)**\n"
            "====================================\n"
            f"🕒 **Time:** {now.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"🎯 **Strategy:** {app_settings.get('strategy_mode', 'SMC')}\n"
            f"📈 **Risk Parameter:** {app_settings.get('risk_percent', 1.0)}%\n"
            f"🚫 **Status:** Monitoring H1 & M15 Channels..."
        )
        send_raw_telegram_message(msg)
        last_ping = now

# ==========================================
# 3. CORE EXCHANGE PIPELINES & LOGIC
# ==========================================
def load_app_settings():
    defaults = {"risk_percent": 1.0, "max_daily_trades": 3, "news_shield_active": True, "strategy_mode": "SMC", "min_rr_ratio": 3.0}
    try:
        response = requests.get(FIREBASE_URL, timeout=10)
        if response.status_code == 200 and response.json() is not None:
            return response.json()
    except Exception as e:
        print(f"⚠️ Cloud sync offline: {e}")
    return defaults

def send_telegram_signal(signal_data):
    message = (
        "🚨 **ANTI GOLD AUTOMATED SNIPER SIGNAL** 🚨\n"
        "====================================\n"
        f"🎯 **STRATEGY:** {signal_data['strategy_mode']}\n"
        "📈 **PAIR:** XAUUSD\n"
        f"🔔 **DIRECTION:** {signal_data['direction']}\n"
        f"⏳ **TIMEFRAME:** M15 (H1 Confirmed)\n\n"
        "📥 **EXECUTION ZONE:**\n"
        f"▪️ **Limit Entry Order:** ${signal_data['entry']:.2f}\n"
        f"▪️ **Dynamic Stop Loss:** ${signal_data['sl']:.2f} ({signal_data['sl_pips']} Pips)\n"
        f"▪️ **Dynamic Take Profit:** ${signal_data['tp']:.2f} (1:{signal_data['rr_ratio']} R:R)\n\n"
        f"💰 **App Risk Applied:** {signal_data['app_risk']}%\n"
        "🚫 **MANAGEMENT:** SET-AND-FORGET."
    )
    send_raw_telegram_message(message)

def fetch_dataframe_from_api(interval):
    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&outputsize=50&apikey={TWELVE_DATA_API_KEY}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            res_data = response.json()
            if "values" in res_data:
                df = pd.DataFrame(res_data["values"])
                df = df.iloc[::-1].reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col].astype(float)
                return df
    except Exception as e:
        print(f"❌ API Error: {e}")
    return None

def check_h1_trend(h1_df):
    last_highs = h1_df['high'].rolling(window=10).max()
    last_lows = h1_df['low'].rolling(window=10).min()
    if h1_df['close'].iloc[-1] > last_highs.iloc[-5]: return "BULLISH"
    elif h1_df['close'].iloc[-1] < last_lows.iloc[-5]: return "BEARISH"
    return "CONSOLIDATION"

def scan_for_sniper_setups(m15_df, h1_trend, settings):
    global last_processed_candle
    if len(m15_df) < LOOKBACK_CANDLES + 5: return
    
    current_candle_time = str(m15_df.iloc[-1].get('datetime', time.time()))
    if last_processed_candle == current_candle_time:
        return
        
    idx_current = -1; idx_trigger = -2; idx_sweep = -3
    session_data = m15_df.iloc[-LOOKBACK_CANDLES-3:-3]
    session_high = session_data['high'].max()
    session_low = session_data['low'].min()
    target_rr = float(settings.get('min_rr_ratio', 3.0))

    signal_sent = False

    if h1_trend == "BULLISH":
        swept = m15_df['low'].iloc[idx_sweep] < session_low and m15_df['close'].iloc[idx_sweep] > session_low
        shift = m15_df['close'].iloc[idx_trigger] > m15_df['high'].iloc[idx_sweep]
        fvg = m15_df['low'].iloc[idx_current] > m15_df['high'].iloc[idx_sweep]
        if swept and shift and fvg:
            entry = m15_df['low'].iloc[idx_current]
            sl = m15_df['low'].iloc[idx_sweep] - (BUFFER_PIPS * GOLD_PIP_VALUE)
            tp = entry + ((entry - sl) * target_rr)
            send_telegram_signal({
                "direction": "BUY 🔵", "entry": entry, "sl": sl, "sl_pips": int((entry-sl)/GOLD_PIP_VALUE),
                "tp": tp, "rr_ratio": f"{target_rr:.1f}", "strategy_mode": settings['strategy_mode'], "app_risk": settings['risk_percent']
            })
            signal_sent = True

    elif h1_trend == "BEARISH":
        swept = m15_df['high'].iloc[idx_sweep] > session_high and m15_df['close'].iloc[idx_sweep] < session_high
        shift = m15_df['close'].iloc[idx_trigger] < m15_df['low'].iloc[idx_sweep]
        fvg = m15_df['high'].iloc[idx_current] < m15_df['low'].iloc[idx_sweep]
        if swept and shift and fvg:
            entry = m15_df['high'].iloc[idx_current]
            sl = m15_df['high'].iloc[idx_sweep] + (BUFFER_PIPS * GOLD_PIP_VALUE)
            tp = entry - ((sl - entry) * target_rr)
            send_telegram_signal({
                "direction": "SELL 🔴", "entry": entry, "sl": sl, "sl_pips": int((sl-entry)/GOLD_PIP_VALUE),
                "tp": tp, "rr_ratio": f"{target_rr:.1f}", "strategy_mode": settings['strategy_mode'], "app_risk": settings['risk_percent']
            })
            signal_sent = True

    if signal_sent:
        last_processed_candle = current_candle_time

# ==========================================
# 4. RUN ENGINE (BACKGROUND WORKERTHREAD)
# ==========================================
def market_scanner_loop():
    print("🎯 Cloud Scanning Engine Initiated.")
    current_settings = load_app_settings()
    check_heartbeat(current_settings)
    
    while True:
        current_settings = load_app_settings()
        check_heartbeat(current_settings)
        
        h1 = fetch_dataframe_from_api("1h")
        m15 = fetch_dataframe_from_api("15min")
        
        if h1 is not None and m15 is not None:
            trend = check_h1_trend(h1)
            scan_for_sniper_setups(m15, trend, current_settings)
            
        print("🛰️ Scan complete. Sleeping 15 mins...")
        time.sleep(900)

if __name__ == "__main__":
    # Start the market structure tracker background loop thread
    threading.Thread(target=market_scanner_loop, daemon=True).start()
    
    # Run the web listener application to satisfy the cloud port routing
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
  
