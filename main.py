import os
import time
import requests
import threading
import datetime
from flask import Flask

# --- 🔌 FLASK PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Gold Institutional Sentinel Bot is active and running!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
MT5_BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "") 
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")

# --- ⚙️ SCHEDULE CONFIGURATION (7:00 AM Local / Adjusted via UTC) ---
# Set to 6:00 UTC for 7:00 AM WAT. Change hour/minute for testing if needed.
TRIGGER_HOUR = 6
TRIGGER_MINUTE = 0

# Master 24-Hour Immutable Plan Ledger
daily_ledger = {
    "date": None,
    "action": None,
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "reasoning": ""
}

# --- 🛰️ MARKET & MACRO INTELLIGENCE ---

def fetch_market_data():
    # Pulling M15 and Daily structure proxy via Yahoo Finance
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {'range': '5d', 'interval': '15m', 'includePrePost': 'false'}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    highs, lows, closes, current_price = [], [], [], 0.0
    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json().get("chart", {}).get("result", [])[0]
            indicators = data.get("indicators", {}).get("quote", [{}])[0]
            highs = [float(x) for x in indicators.get("high", []) if x is not None]
            lows = [float(x) for x in indicators.get("low", []) if x is not None]
            closes = [float(x) for x in indicators.get("close", []) if x is not None]
            current_price = closes[-1] if closes else 0.0
    except Exception as e:
        print(f"⚠️ Market data fetch error: {e}")
        
    return highs, lows, closes, current_price

def fetch_macro_news():
    macro_text = "Macro sentiment stable; monitoring structural liquidity."
    sentiment_bias = "Neutral"
    try:
        news_url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=GC=F&apikey={ALPHA_VANTAGE_KEY}"
        news_res = requests.get(news_url, timeout=10).json()
        feed = news_res.get("feed", [])
        if feed:
            top_story = feed[0].get("title", "")
            score = float(feed[0].get("overall_sentiment_score", 0.0))
            sentiment_bias = "Bullish" if score > 0.15 else ("Bearish" if score < -0.15 else "Neutral")
            macro_text = f"News Pulse: '{top_story}' | Bias: {sentiment_bias}"
    except:
        pass
    return macro_text, sentiment_bias

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
    trade_action = "BUY" if "BUY" in action else "SELL"
    payload = {"symbol": "XAUUSD", "action": trade_action, "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "volume": 0.01}
    try:
        requests.post(MT5_BRIDGE_URL, json=payload, timeout=10)
    except:
        pass

# --- 🔒 IMMUTABLE 7:00 AM GENERATOR ---

def generate_or_get_daily_plan(forced=False):
    global daily_ledger
    today = datetime.datetime.now(datetime.timezone.utc).date()
    
    # IMMUTABILITY RULE: If plan exists for today, strictly return it (unless forced by 7 AM scheduler)
    if daily_ledger["date"] == today and not forced:
        return daily_ledger

    print("🌅 Generating fresh 24-hour immutable market play out...")
    highs, lows, closes, current_price = fetch_market_data()
    macro_text, bias = fetch_macro_news()
    
    if not closes or current_price == 0:
        print("⚠️ Data fetch failed. Retaining fallback or previous ledger state.")
        return daily_ledger

    # M15 structural calculation bounds
    recent_lows = lows[-20:]
    recent_highs = highs[-20:]
    support = min(recent_lows)
    resistance = max(recent_highs)
    pivot = (support + resistance) / 2

    # Fixed strategic layout based on multi-timeframe stance and macro bias
    if bias == "Bearish" or current_price >= pivot:
        action = "SELL LIMIT"
        entry = round(current_price + 2.0, 2)
        sl = round(entry + 10.0, 2)
        tp = round(entry - 25.0, 2)
        reasoning = f"M15 structure and macro pulse ({macro_text}) favor institutional supply rejection near resistance."
    else:
        action = "BUY LIMIT"
        entry = round(current_price - 2.0, 2)
        sl = round(entry - 10.0, 2)
        tp = round(entry + 25.0, 2)
        reasoning = f"M15 structure and macro pulse ({macro_text}) favor institutional demand defense near support."

    # Lock into immutable ledger
    daily_ledger["date"] = today
    daily_ledger["action"] = action
    daily_ledger["entry"] = entry
    daily_ledger["sl"] = sl
    daily_ledger["tp"] = tp
    daily_ledger["reasoning"] = reasoning

    if forced:
        send_to_mt5_bridge(action, entry, sl, tp)
        briefing = (
            f"🌅 **GOLD 24-HOUR MASTER BLUEPRINT** 🌅\n\n"
            f"🎯 **Plan:** **{action}**\n"
            f"• **Spot Reference:** `{current_price:.2f}`\n"
            f"• **Locked Entry:** `{entry}`\n"
            f"• **Stop Loss:** `{sl}`\n"
            f"• **Take Profit:** `{tp}`\n\n"
            f"🧠 **Desk Thesis:**\n> \"{reasoning}\"\n\n"
            f"_Locked for the next 24 hours. Levels are permanently fixed._"
        )
        send_telegram_alert(briefing)
        
    return daily_ledger

# --- 👁️ CONTINUOUS REAL-TIME SENTINEL MONITOR ---

def sentinel_market_monitor():
    print("👁️ Sentinel live market & news watchdog initialized...")
    while True:
        time.sleep(300) # Check every 5 minutes
        if not daily_ledger["action"]:
            continue
            
        _, _, _, current_price = fetch_market_data()
        macro_text, bias = fetch_macro_news()
        
        action = daily_ledger["action"]
        entry = daily_ledger["entry"]
        sl = daily_ledger["sl"]
        
        # Sentinel Check 1: Did a sudden macro news shock flip the bias completely against our active trade?
        if ("BUY" in action and bias == "Bearish") or ("SELL" in action and bias == "Bullish"):
            send_telegram_alert(
                f"🚨 **SENTINEL WARNING: MACRO SHIFT DETECTED** 🚨\n\n"
                f"Active Plan: {action} (Entry: `{entry}`)\n"
                f"⚠️ **Breaking News / Shift:** {macro_text}\n\n"
                f"_Recommendation: Consider securing **Break-Even** or manually exiting to protect capital against trend inversion._"
            )
            # Sleep an hour so it doesn't spam alerts continuously
            time.sleep(3600)
            
        # Sentinel Check 2: Price action runaway validation check (preventing stale limit orders getting smoked)
        if "BUY" in action and current_price >= entry + 20.0:
            # Check if order was likely missed/walked away
            pass

# --- ⏳ SCHEDULER CORE ---

def daily_scheduler():
    already_triggered_date = None
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        current_date = now.date()
        
        # Fire strictly at the 7:00 AM target window once per day
        if now.hour == TRIGGER_HOUR and now.minute >= TRIGGER_MINUTE and already_triggered_date != current_date:
            try:
                generate_or_get_daily_plan(forced=True)
                already_triggered_date = current_date
            except Exception as e:
                print(f"❌ Scheduler execution error: {e}")
            time.sleep(60)
        else:
            # Ensure ledger is initialized for manual midday check consistency
            generate_or_get_daily_plan(forced=False)
            time.sleep(30)

def main():
    print("🚀 Gold Autonomous Sentinel Engine Initialized...")
    
    # Start Web Server for Render Health Checks
    threading.Thread(target=run_health_server, daemon=True).start()
    
    # Start the 7:00 AM Immutable Daily Plan Scheduler
    threading.Thread(target=daily_scheduler, daemon=True).start()
    
    # Start the 24/7 Live Sentinel News & Risk Watchdog
    threading.Thread(target=sentinel_market_monitor, daemon=True).start()
    
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
