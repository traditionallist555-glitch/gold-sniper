import os
import time
import requests
import telegram
import threading
from datetime import datetime, timezone
from flask import Flask

# --- 🔌 FLASK PORT BINDING (Keeps your bot alive on Render) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Gold Sniper Bot is active and running natively!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
# -------------------------------------------------------------

# Fetch your keys from Render environment variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
MT5_BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "") 

# Initialize Telegram Bot
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

# --- 🧮 NATIVE MATHEMATICAL INDICATOR CALCULATIONS ---

def calculate_ema(prices, period):
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

# --- 🛰️ GEOGRAPHIC-PROOF MARKET DATA FETCH (Direct Yahoo Spot Gold) ---

def get_gold_market_data():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {
        'range': '5d',
        'interval': '15m',
        'includePrePost': 'false'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"❌ Failed to fetch data from Yahoo: Code {response.status_code}")
            return None
            
        json_data = response.json()
        result = json_data.get("chart", {}).get("result", [])
        if not result:
            return None
            
        indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [float(x) for x in indicators.get("close", []) if x is not None]
        highs = [float(x) for x in indicators.get("high", []) if x is not None]
        lows = [float(x) for x in indicators.get("low", []) if x is not None]
        
        if len(closes) < 200:
            print("⚠️ Insufficient candle history from Yahoo Finance.")
            return None
            
        ema_200_list = calculate_ema(closes, 200)
        atr_list = calculate_atr(highs, lows, closes)
        
        return {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "price": closes[-1],
            "ema_200": ema_200_list[-1] if ema_200_list else None,
            "atr": atr_list[-1]
        }
    except Exception as e:
        print(f"❌ Market Data Retrieval Error: {e}")
        return None

# --- 🛡️ TRADE ACTIONS & SAFETY CHECKS ---

def send_to_mt5_bridge(action, entry, sl, tp):
    if not MT5_BRIDGE_URL:
        print("ℹ️ MT5 Bridge URL not configured. Skipping automated trade execution.")
        return

    payload = {
        "symbol": "XAUUSD",
        "action": action,      
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "volume": 0.01         
    }

    try:
        response = requests.post(MT5_BRIDGE_URL, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"🚀 [MT5 SENT] Trade order dispatched: {action} @ {entry}")
        else:
            print(f"❌ [MT5 ERROR] Bridge error {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ [MT5 CONNECTION FAILED] Could not contact bridge: {e}")

def check_news_safety():
    url = "https://www.jblanked.com/news/api/forex-factory/calendar/today/"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return True, "Safe (Bypassed due to API status)"
            
        events = response.json()
        now_utc = datetime.now(timezone.utc)
        
        for event in events:
            if event.get("Currency") == "USD" and event.get("Impact") == "High":
                event_time_str = event.get("Date")
                if not event_time_str: continue
                
                try:
                    event_time = datetime.strptime(event_time_str, "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
                
                time_diff_mins = (event_time - now_utc).total_seconds() / 60.0
                if -60 <= time_diff_mins <= 60:
                    return False, f"⚠️ USD HIGH IMPACT: {event.get('Name')}"
                    
        return True, "No critical news conflicts."
    except Exception as e:
        print(f"⚠️ News API bypassed: {e}")
        return True, "Safe (Bypassed error)"

def execute_strategy_scan():
    is_safe, news_status = check_news_safety()
    if not is_safe:
        print(f"🛡️ [NEWS SHIELD ACTIVE] Scan paused: {news_status}")
        return None
        
    print("⚡ [SCANNING] Analyzing structural liquidity sweeps on Gold...")
    metrics = get_gold_market_data()
    
    if not metrics or any(v is None for v in [metrics["price"], metrics["ema_200"], metrics["atr"]]):
        print("⚠️ [WARNING] Live calculations not yet complete.")
        return None
        
    closes = metrics["closes"]
    highs = metrics["highs"]
    lows = metrics["lows"]
    entry_price = metrics["price"]
    ema_200 = metrics["ema_200"]
    atr = metrics["atr"]
    
    macro_trend = "BULLISH" if entry_price > ema_200 else "BEARISH"
    
    # --- LIQUIDITY SWEEP & STRUCTURE LOGIC ---
    recent_support = min(lows[-25:-2])
    recent_resistance = max(highs[-25:-2])
    
    current_low = lows[-1]
    current_high = highs[-1]
    current_close = closes[-1]
    
    signal_alert = None
    
    # BUY SETUP: Price sweeps below support wick and snaps back bullish
    if macro_trend == "BULLISH" and current_low < recent_support and current_close > recent_support:
        # Strict 8.0 to 12.0 point SL rule (80-120 pips)
        sl_distance = max(8.0, min(abs(entry_price - current_low) + (atr * 0.5), 12.0))
        if sl_distance < 8.0 or sl_distance > 12.0:
            print(f"⏳ [FILTERED] Buy SL distance {sl_distance:.2f} outside 8.0-12.0 range.")
            return None
            
        sl_price = entry_price - sl_distance
        tp_price = entry_price + (sl_distance * 3.0) # Strict 1:3 RRR
        
        send_to_mt5_bridge("BUY", entry_price, sl_price, tp_price)
        signal_alert = (
            f"🟢 GOLD LIQUIDITY BUY SIGNAL 🟢\n\n"
            f"🎯 Instrument: XAU/USD (Gold)\n"
            f"📈 Setup: Support Liquidity Sweep & Reclaim\n\n"
            f"📊 Target Coordinates:\n"
            f"• Entry Price: {entry_price:.2f}\n"
            f"• Stop Loss: {sl_price:.2f} ({sl_distance*10:.0f} pips)\n"
            f"• Take Profit: {tp_price:.2f} (1:3 RRR)\n\n"
            f"📋 Market Context:\n"
            f"• Macro Trend: Bullish (Above 200 EMA)\n"
            f"• Volatility ATR: {atr:.2f}"
        )
        
    # SELL SETUP: Price sweeps above resistance wick and rejects bearish
    elif macro_trend == "BEARISH" and current_high > recent_resistance and current_close < recent_resistance:
        # Strict 8.0 to 12.0 point SL rule (80-120 pips)
        sl_distance = max(8.0, min(abs(current_high - entry_price) + (atr * 0.5), 12.0))
        if sl_distance < 8.0 or sl_distance > 12.0:
            print(f"⏳ [FILTERED] Sell SL distance {sl_distance:.2f} outside 8.0-12.0 range.")
            return None
            
        sl_price = entry_price + sl_distance
        tp_price = entry_price - (sl_distance * 3.0) # Strict 1:3 RRR
        
        send_to_mt5_bridge("SELL", entry_price, sl_price, tp_price)
        signal_alert = (
            f"🔴 GOLD LIQUIDITY SELL SIGNAL 🔴\n\n"
            f"🎯 Instrument: XAU/USD (Gold)\n"
            f"📉 Setup: Resistance Liquidity Sweep & Rejection\n\n"
            f"📊 Target Coordinates:\n"
            f"• Entry Price: {entry_price:.2f}\n"
            f"• Stop Loss: {sl_price:.2f} ({sl_distance*10:.0f} pips)\n"
            f"• Take Profit: {tp_price:.2f} (1:3 RRR)\n\n"
            f"📋 Market Context:\n"
            f"• Macro Trend: Bearish (Below 200 EMA)\n"
            f"• Volatility ATR: {atr:.2f}"
        )
    else:
        print(f"⏳ [SCAN COMPLETE] Price: {entry_price:.2f} | Trend: {macro_trend} | Monitoring for 8-12pt sweep setups...")
        
    return signal_alert

def main():
    print("🚀 Gold Sniper Core Engine active and running natively on Render...")
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()
    time.sleep(2)
    
    if TELEGRAM_CHANNEL_ID:
        try:
            print("📣 [TEST TRIGGER] Firing pipeline verification test to Telegram...")
            test_msg = (
                "🛠️ **GOLD SNIPER SYSTEM RESTARTED** 🛠️\n\n"
                "• **Status:** Operational & Upgraded 🟢\n"
                "• **SL Rule:** Strictly 8.0 - 12.0 pts (80-120 pips) ⚖️\n"
                "• **Reward Ratio:** Strict 1:3 RRR Target 🎯\n\n"
                "_Your bot is now live and scanning for optimal sweeps!_"
            )
            bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=test_msg, parse_mode="Markdown")
            print("✅ [TEST SENT] Message displayed in channel successfully.")
        except Exception as te:
            print(f"❌ [TEST FAILED] Could not contact Telegram API on boot: {te}")
    
    while True:
        try:
            signal_alert = execute_strategy_scan()
            if signal_alert and TELEGRAM_CHANNEL_ID:
                print("🔥 [SIGNAL GENERATED] Broadcasting signal out to Telegram...")
                bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=signal_alert)
        except Exception as e:
            print(f"❌ Loop Error Encountered: {e}")
            
        time.sleep(900)

if __name__ == "__main__":
    main()
        
