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
    return "Gold Institutional Intelligence Bot is active and running!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
MT5_BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "") 
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")

# --- ⚙️ SCHEDULE CONFIGURATION ---
# TEST MODE: Currently set to trigger at 10:20 AM for immediate verification.
# Once verified, change these back to: TRIGGER_HOUR = 7, TRIGGER_MINUTE = 0
TRIGGER_HOUR = 10
TRIGGER_MINUTE = 20

# Global tracker for active daily setup expiration
active_setup = {
    "action": None,
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "date": None
}

# --- 🛰️ MARKET & MACRO INTELLIGENCE ---

def get_market_intelligence():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {'range': '30d', 'interval': '1d', 'includePrePost': 'false'}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    closes, highs, lows, current_price = [], [], [], 0.0
    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json().get("chart", {}).get("result", [])[0]
            indicators = data.get("indicators", {}).get("quote", [{}])[0]
            closes = [float(x) for x in indicators.get("close", []) if x is not None]
            highs = [float(x) for x in indicators.get("high", []) if x is not None]
            lows = [float(x) for x in indicators.get("low", []) if x is not None]
            current_price = closes[-1]
    except Exception as e:
        print(f"⚠️ Price fetch error: {e}")

    macro_sentiment = "Market sentiment neutral; tracking immediate liquidity boundaries."
    try:
        news_url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=GC=F&apikey={ALPHA_VANTAGE_KEY}"
        news_res = requests.get(news_url, timeout=10).json()
        feed = news_res.get("feed", [])
        if feed:
            top_story = feed[0].get("title", "")
            sentiment_score = float(feed[0].get("overall_sentiment_score", 0.0))
            mood = "Bullish" if sentiment_score > 0.15 else ("Bearish" if sentiment_score < -0.15 else "Neutral")
            macro_sentiment = f"News Pulse: '{top_story}' | Macro Bias: {mood}"
    except:
        pass

    return closes, highs, lows, current_price, macro_sentiment

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

def generate_daily_briefing():
    global active_setup
    print("🌅 Generating structural and macro briefing...")
    closes, highs, lows, current_price, macro_sentiment = get_market_intelligence()
    
    if not closes or current_price == 0:
        print("⚠️ Failed to pull complete market data.")
        return
        
    # FIX: Use immediate recent swing boundaries so entries cluster tightly around live spot price
    recent_lows = lows[-5:]
    recent_highs = highs[-5:]
    support_zone = min(recent_lows)
    resistance_zone = max(recent_highs)
    pivot_mid = (support_zone + resistance_zone) / 2
    
    # Dynamic structural placement close to current market price
    if current_price >= pivot_mid:
        action = "SELL LIMIT"
        entry = round(current_price + 2.5, 2)
        sl = round(entry + 10.0, 2)   
        tp = round(entry - 25.0, 2) 
        reasoning = (
            f"Price is testing upper local resistance near {resistance_zone:.2f}. "
            f"{macro_sentiment}. Expecting institutional rejection with strict structural invalidation above {sl}."
        )
    else:
        action = "BUY LIMIT"
        entry = round(current_price - 2.5, 2)
        sl = round(entry - 10.0, 2)   
        tp = round(entry + 25.0, 2) 
        reasoning = (
            f"Price is pressing local demand around {support_zone:.2f}. "
            f"{macro_sentiment}. Anticipating buyers to defend structure above {sl} before a recovery toward {tp}."
        )

    # Save setup state for expiration tracking
    active_setup["action"] = action
    active_setup["entry"] = entry
    active_setup["sl"] = sl
    active_setup["tp"] = tp
    active_setup["date"] = datetime.datetime.now(datetime.timezone.utc).date()

    trade_action = "BUY" if "BUY" in action else "SELL"
    send_to_mt5_bridge(trade_action, entry, sl, tp)

    briefing_message = (
        f"🌅 **GOLD DAILY INTELLIGENCE BRIEFING** 🌅\n\n"
        f"🎯 **Action Plan:** **{action}**\n\n"
        f"• **Spot Price:** `{current_price:.2f}`\n"
        f"• **Expected Entry:** `{entry}`\n"
        f"• **Dynamic Stop Loss:** `{sl}` *(Structural Invalidation)*\n"
        f"• **Dynamic Take Profit:** `{tp}` *(Structural Target)*\n\n"
        f"🧠 **Desk Analysis & Macro Context:**\n"
        f"> \"{reasoning}\"\n\n"
        f"_Pending limit order transmitted. Tracking execution status._"
    )
    
    send_telegram_alert(briefing_message)
    print("✅ Daily structural brief sent successfully.")

def check_setup_expiration():
    global active_setup
    if not active_setup["action"]:
        return
        
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {'range': '1d', 'interval': '5m'}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json().get("chart", {}).get("result", [])[0]
            current_price = data.get("indicators", {}).get("quote", [{}])[0].get("close", [])[-1]
            
            action = active_setup["action"]
            entry = active_setup["entry"]
            
            if "BUY" in action and current_price >= entry + 18.0:
                send_telegram_alert(
                    f"⚠️ **GOLD SETUP EXPIRED / MISSED** ⚠️\n\n"
                    f"🎯 Plan: {action} (Entry: `{entry}`)\n"
                    f"📊 Current Price (`{current_price:.2f}`) rallied away without pulling back to our limit.\n\n"
                    f"_Recommendation: Clear pending limit order from your MT5 terminal._"
                )
                active_setup["action"] = None
                
            elif "SELL" in action and current_price <= entry - 18.0:
                send_telegram_alert(
                    f"⚠️ **GOLD SETUP EXPIRED / MISSED** ⚠️\n\n"
                    f"🎯 Plan: {action} (Entry: `{entry}`)\n"
                    f"📊 Current Price (`{current_price:.2f}`) dropped away without pulling back to our limit.\n\n"
                    f"_Recommendation: Clear pending limit order from your MT5 terminal._"
                )
                active_setup["action"] = None
    except Exception as e:
        print(f"⚠️ Expiry check error: {e}")

def daily_scheduler():
    print(f"⏳ Time-lock scheduler started. Target trigger set to {TRIGGER_HOUR}:{TRIGGER_MINUTE:02d}...")
    already_triggered_today = None
    
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        current_date = now.date()
        
        # Trigger at designated time and ensure it only fires once per day
        if now.hour == TRIGGER_HOUR and now.minute >= TRIGGER_MINUTE and already_triggered_today != current_date:
            try:
                generate_daily_briefing()
                already_triggered_today = current_date
            except Exception as e:
                print(f"❌ Error in briefing run: {e}")
            time.sleep(60)
            
        # Check every 30 minutes if the order missed its entry and expired
        elif now.minute % 30 == 0:
            check_setup_expiration()
            time.sleep(60)
        else:
            time.sleep(30)

def main():
    print("🚀 Gold Autonomous Macro Engine Initialized...")
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()
    
    scheduler_thread = threading.Thread(target=daily_scheduler, daemon=True)
    scheduler_thread.start()
    
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
