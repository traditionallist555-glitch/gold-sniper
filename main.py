import os
import time
import requests
import threading
from datetime import datetime, timezone
from flask import Flask

# --- 🔌 FLASK PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Gold Sniper Bot is active and running natively!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
MT5_BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "") 

# --- 🧮 INDICATORS ---

def calculate_ema(prices, period=100):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]  
    for price in prices[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    
    tr_list = []
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i-1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
        
    atr = [sum(tr_list[:period]) / period]
    for tr in tr_list[period:]:
        atr.append((atr[-1] * (period - 1) + tr) / period)
        
    return [None] * (period + 1) + atr

# --- 🛰️ MARKET DATA FETCH ---

def get_gold_market_data():
    urls = [
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
        "https://query2.finance.yahoo.com/v8/finance/chart/GC=F"
    ]
    params = {
        'range': '60d', 
        'interval': '15m',
        'includePrePost': 'false'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    for url in urls:
        for attempt in range(3):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=10)
                if response.status_code == 200:
                    json_data = response.json()
                    result = json_data.get("chart", {}).get("result", [])
                    if not result:
                        continue
                        
                    indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
                    closes = [float(x) for x in indicators.get("close", []) if x is not None]
                    highs = [float(x) for x in indicators.get("high", []) if x is not None]
                    lows = [float(x) for x in indicators.get("low", []) if x is not None]
                    
                    if len(closes) < 100:
                        return None
                        
                    ema_100_list = calculate_ema(closes, 100)
                    atr_list = calculate_atr(highs, lows, closes)
                    
                    return {
                        "closes": closes,
                        "highs": highs,
                        "lows": lows,
                        "price": closes[-1],
                        "ema_100": ema_100_list[-1] if ema_100_list else None,
                        "atr": atr_list[-1]
                    }
            except Exception as e:
                time.sleep(2)
    return None

# --- 🛡️ ACTIONS & SAFETY ---

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def send_to_mt5_bridge(action, entry, sl, tp):
    if not MT5_BRIDGE_URL:
        return
    payload = {"symbol": "XAUUSD", "action": action, "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "volume": 0.01}
    try:
        requests.post(MT5_BRIDGE_URL, json=payload, timeout=10)
    except:
        pass

def execute_strategy_scan():
    metrics = get_gold_market_data()
    if not metrics or any(v is None for v in [metrics["price"], metrics["ema_100"], metrics["atr"]]):
        print("⚠️ [WARNING] Live calculations waiting for data...")
        return None
        
    closes = metrics["closes"]
    highs = metrics["highs"]
    lows = metrics["lows"]
    entry_price = metrics["price"]
    ema_100 = metrics["ema_100"]
    atr = metrics["atr"]
    
    # Use a faster 100 EMA trend guide for smoother responsiveness
    macro_trend = "BULLISH" if entry_price > ema_100 else "BEARISH"
    
    # --- BALANCED STRUCTURE LOOKBACK (40 bars) ---
    recent_support = min(lows[-40:-1])
    recent_resistance = max(highs[-40:-1])
    
    current_low = lows[-1]
    current_high = highs[-1]
    current_close = closes[-1]
    
    signal_alert = None
    
    print(f"🔍 [SCAN] Price: {entry_price:.2f} | Trend: {macro_trend} | Sup: {recent_support:.2f} | Res: {recent_resistance:.2f} | ATR: {atr:.2f}")

    # BUY SETUP: Sweeps or tests support cleanly in a bullish or neutral environment
    if current_low <= recent_support + 2.0 and current_close > recent_support:
        sl_distance = max(4.0, min(abs(entry_price - current_low) + (atr * 0.5), 18.0))
        sl_price = entry_price - sl_distance
        tp_price = entry_price + (sl_distance * 3.0)
        
        send_to_mt5_bridge("BUY", entry_price, sl_price, tp_price)
        signal_alert = (
            f"🟢 GOLD LIQUIDITY BUY SIGNAL 🟢\n\n"
            f"🎯 Instrument: XAU/USD (Gold)\n"
            f"📈 Setup: Support Liquidity Sweep & Reclaim\n\n"
            f"📊 Target Coordinates:\n"
            f"• Entry Price: {entry_price:.2f}\n"
            f"• Stop Loss: {sl_price:.2f}\n"
            f"• Take Profit: {tp_price:.2f} (1:3 RRR)\n"
            f"• Trend State: {macro_trend}"
        )
        
    # SELL SETUP: Sweeps or tests resistance cleanly
    elif current_high >= recent_resistance - 2.0 and current_close < recent_resistance:
        sl_distance = max(4.0, min(abs(current_high - entry_price) + (atr * 0.5), 18.0))
        sl_price = entry_price + sl_distance
        tp_price = entry_price - (sl_distance * 3.0)
        
        send_to_mt5_bridge("SELL", entry_price, sl_price, tp_price)
        signal_alert = (
            f"🔴 GOLD LIQUIDITY SELL SIGNAL 🔴\n\n"
            f"🎯 Instrument: XAU/USD (Gold)\n"
            f"📉 Setup: Resistance Liquidity Sweep & Rejection\n\n"
            f"📊 Target Coordinates:\n"
            f"• Entry Price: {entry_price:.2f}\n"
            f"• Stop Loss: {sl_price:.2f}\n"
            f"• Take Profit: {tp_price:.2f} (1:3 RRR)\n"
            f"• Trend State: {macro_trend}"
        )
    else:
        print(f"⏳ [NO SETUP] Candle within normal range, waiting for boundary reaction.")
        
    return signal_alert

def main():
    print("🚀 Gold Sniper Core Engine Active...")
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()
    time.sleep(2)
    
    if TELEGRAM_CHANNEL_ID:
        try:
            send_telegram_alert("🛠️ **GOLD SNIPER STRATEGY ACTIVE** 🛠️\n\n• 40-bar structural lookback\n• 100 EMA responsive trend check\n• Active market scanning enabled!")
        except:
            pass
    
    while True:
        try:
            signal_alert = execute_strategy_scan()
            if signal_alert:
                send_telegram_alert(signal_alert)
        except Exception as e:
            print(f"❌ Loop Error: {e}")
            
        time.sleep(900)

if __name__ == "__main__":
    main()
    
