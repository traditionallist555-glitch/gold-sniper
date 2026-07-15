import os
import time
import requests
import telegram
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- 🔌 RENDER PORT BINDING (Keeps your bot alive for free) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Gold Sniper Bot is active and running!")

    def log_message(self, format, *args):
        return  # Keeps logs completely clean of internal web pings

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"📡 Internal Health Server active on port {port}")
    server.serve_forever()
# -------------------------------------------------------------

# Fetch your saved keys from Render
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
TAAPI_SECRET = os.environ.get("TAAPI_SECRET")

# MT5 Webhook Bridge Configuration (Set this env var in Render when your bridge is ready!)
MT5_BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "") 

# Initialize Telegram Bot (Fully Synchronous - v13.13 Safe!)
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

def send_to_mt5_bridge(action, entry, sl, tp):
    """
    Sends the signal directly to your MT5 bridge API to execute the trade instantly.
    """
    if not MT5_BRIDGE_URL:
        print("ℹ️ MT5 Bridge URL not configured. Skipping automated trade execution.")
        return

    payload = {
        "symbol": "XAUUSD",
        "action": action,      # "BUY" or "SELL"
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "volume": 0.10         # Default lot size (adjust as needed)
    }

    try:
        response = requests.post(MT5_BRIDGE_URL, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"🚀 [MT5 SENT] Trade order dispatched successfully: {action} @ {entry}")
        else:
            print(f"❌ [MT5 ERROR] Bridge returned code {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ [MT5 CONNECTION FAILED] Could not contact MT5 bridge: {e}")

def check_news_safety():
    """
    Bypasses trading if we are within 60 minutes of high-impact USD economic news.
    """
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

def get_gold_market_data():
    """
    Fetches indicators on the 15m timeframe for faster, high-probability execution.
    """
    base_url = "https://api.taapi.io"
    params = {
        "secret": TAAPI_SECRET,
        "exchange": "binance",
        "symbol": "PAXG/USDT",
        "interval": "15m"
    }
    
    try:
        # Pull RSI, MACD, and Current Price
        rsi = requests.get(f"{base_url}/rsi", params=params).json().get("value")
        macd_res = requests.get(f"{base_url}/macd", params=params).json()
        macd_val, macd_sig = macd_res.get("valueMACD"), macd_res.get("valueMACDSignal")
        price = requests.get(f"{base_url}/price", params=params).json().get("value")
        
        # 200 EMA for Macro Trend Direction
        ema_params = params.copy()
        ema_params["optInTimePeriod"] = 200
        ema_200 = requests.get(f"{base_url}/ema", params=ema_params).json().get("value")
        
        # 14 ATR for Dynamic Stop Loss / Take Profit
        atr_params = params.copy()
        atr_params["optInTimePeriod"] = 14
        atr = requests.get(f"{base_url}/atr", params=atr_params).json().get("value")
        
        return {
            "rsi": rsi, 
            "macd_val": macd_val, 
            "macd_sig": macd_sig, 
            "ema_200": ema_200, 
            "atr": atr, 
            "price": price
        }
    except Exception as e:
        print(f"❌ Market Data Fetch Error: {e}")
        return None

def execute_strategy_scan():
    # 1. Macro Economic Check
    is_safe, news_status = check_news_safety()
    if not is_safe:
        print(f"🛡️ [NEWS SHIELD ACTIVE] Scan paused: {news_status}")
        return None
        
    print("⚡ [SCANNING] Running dual-engine confirmation checks...")
    metrics = get_gold_market_data()
    
    if not metrics or any(v is None for v in metrics.values()):
        print("⚠️ [WARNING] Market data stream incomplete. Skipping scan.")
        return None
        
    rsi, macd_val, macd_sig = metrics["rsi"], metrics["macd_val"], metrics["macd_sig"]
    ema_200, atr, entry_price = metrics["ema_200"], metrics["atr"], metrics["price"]
    
    # Establish dynamic 1:3 reward-to-risk bracket levels
    sl_distance = atr * 1.5
    tp_distance = sl_distance * 3.0
    
    # Trend alignment direction
    macro_trend = "BULLISH" if entry_price > ema_200 else "BEARISH"
    
    print(f"📊 Metrics | Price: {entry_price:.2f} | EMA 200: {ema_200:.2f} ({macro_trend}) | RSI: {rsi:.2f} | ATR: {atr:.2f}")

    # BUY RULES: Uptrend, Oversold RSI, and MACD Bullish Crossover
    if macro_trend == "BULLISH" and rsi <= 35 and macd_val > macd_sig:
        sl_price = entry_price - sl_distance
        tp_price = entry_price + tp_distance
        
        # Trigger Auto-Trading execution
        send_to_mt5_bridge("BUY", entry_price, sl_price, tp_price)
        
        return (
            f"🟢 GOLD BUY SIGNAL 🟢\n\n"
            f"🎯 Instrument: XAU/USD (Gold)\n"
            f"📈 Order Type: BUY ENTRY\n\n"
            f"📊 Target Coordinates:\n"
            f"• Entry Price: {entry_price:.2f}\n"
            f"• Stop Loss: {sl_price:.2f}\n"
            f"• Take Profit: {tp_price:.2f}\n\n"
            f"📋 Technical Data:\n"
            f"• RSI: {rsi:.2f} (Oversold confirmation)\n"
            f"• Volatility ATR: {atr:.2f}"
        )
        
    # SELL RULES: Downtrend, Overbought RSI, and MACD Bearish Crossover
    elif macro_trend == "BEARISH" and rsi >= 65 and macd_val < macd_sig:
        sl_price = entry_price + sl_distance
        tp_price = entry_price - tp_distance
        
        # Trigger Auto-Trading execution
        send_to_mt5_bridge("SELL", entry_price, sl_price, tp_price)
        
        return (
            f"🔴 GOLD SELL SIGNAL 🔴\n\n"
            f"🎯 Instrument: XAU/USD (Gold)\n"
            f"📉 Order Type: SELL ENTRY\n\n"
            f"📊 Target Coordinates:\n"
            f"• Entry Price: {entry_price:.2f}\n"
            f"• Stop Loss: {sl_price:.2f}\n"
            f"• Take Profit: {tp_price:.2f}\n\n"
            f"📋 Technical Data:\n"
            f"• RSI: {rsi:.2f} (Overbought confirmation)\n"
            f"• Volatility ATR: {atr:.2f}"
        )
        
    print("⏳ [SCAN COMPLETE] Market structural rules analyzed. No perfect setups found.")
    return None

def main():
    print("🚀 Gold Sniper Core Engine active and running on Render...")
    
    # Start the background health server so Render's port scan immediately passes
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()
    
    # Let the server bind successfully before initiating the first API scan
    time.sleep(2)
    
    while True:
        try:
            signal_alert = execute_strategy_scan()
            if signal_alert and TELEGRAM_CHANNEL_ID:
                print("🔥 [SIGNAL GENERATED] Broadcasting signal out to Telegram...")
                # Completely synchronous v13.13 Telegram execution
                bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=signal_alert)
        except Exception as e:
            print(f"❌ Loop Error Encountered: {e}")
            
        # Standard safety delay: Check market state on 15m intervals
        time.sleep(900)

if __name__ == "__main__":
    main() # <-- Fixed missing parentheses to actually launch the script!
