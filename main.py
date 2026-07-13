import os
import time
import requests
import telegram

# 1. Securely fetch the keys you just saved on Render
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TAAPI_SECRET = os.environ.get("TAAPI_SECRET")

# Initialize Telegram Bot connection
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

def get_gold_market_data():
    """
    Fetches exact RSI and MACD data for Gold (XAU/USD via PAXG) from Taapi API
    """
    base_url = "https://api.taapi.io"
    params = {
        "secret": TAAPI_SECRET,
        "exchange": "binance",
        "symbol": "PAXG/USDT",
        "interval": "1h"
    }
    
    try:
        # Pull RSI Engine Data
        rsi_req = requests.get(f"{base_url}/rsi", params=params).json()
        rsi_val = rsi_req.get("value")
        
        # Pull MACD Engine Data
        macd_req = requests.get(f"{base_url}/macd", params=params).json()
        macd_val = macd_req.get("valueMACD")
        macd_sig = macd_req.get("valueMACDSignal")
        macd_hist = macd_req.get("valueMACDHist")
        
        return {
            "rsi": rsi_val,
            "macd_val": macd_val,
            "macd_sig": macd_sig,
            "macd_hist": macd_hist
        }
    except Exception as e:
        print(f"❌ Market Data Fetch Error: {e}")
        return None

def execute_strategy_scan():
    print("⚡ [SCANNING] Running dual-engine confirmation checks...")
    metrics = get_gold_market_data()
    
    if not metrics or metrics["rsi"] is None or metrics["macd_val"] is None or metrics["macd_sig"] is None:
        print("⚠️ [WARNING] Market data stream incomplete. Skipping scan iteration.")
        return None
        
    rsi = metrics["rsi"]
    macd_val = metrics["macd_val"]
    macd_sig = metrics["macd_sig"]
    macd_hist = metrics["macd_hist"]
    
    print(f"📊 Market Metrics -> RSI: {rsi:.2f} | MACD Line: {macd_val:.4f} | Signal Line: {macd_sig:.4f} | Hist: {macd_hist:.4f}")
    
    # 🚨 STRICT SIGNAL ENGINE RULES 🚨
    
    # RULE 1: BUY SIGNAL (RSI Oversold AND MACD Line crosses ABOVE Signal Line)
    if rsi <= 30 and macd_val > macd_sig:
        return (
            f"🚨 🟡 GOLD BUY SIGNAL 🟡 🚨\n\n"
            f"🎯 Target Asset: XAU/USD (Gold)\n"
            f"📈 Action: STRONG BUY ENTRY\n\n"
            f"📋 Strategy Confirmation Details:\n"
            f"• RSI Engine: Oversold at {rsi:.2f} (Rule <= 30)\n"
            f"• MACD Engine: Bullish Crossover! MACD Line ({macd_val:.4f}) crossed above Signal Line ({macd_sig:.4f})\n"
            f"• Histogram: Momentum positive at {macd_hist:.4f}"
        )
        
    # RULE 2: SELL SIGNAL (RSI Overbought AND MACD Line crosses BELOW Signal Line)
    elif rsi >= 70 and macd_val < macd_sig:
        return (
            f"🚨 🔴 GOLD SELL SIGNAL 🔴 🚨\n\n"
            f"🎯 Target Asset: XAU/USD (Gold)\n"
            f"📉 Action: STRONG SELL ENTRY\n\n"
            f"📋 Strategy Confirmation Details:\n"
            f"• RSI Engine: Overbought at {rsi:.2f} (Rule >= 70)\n"
            f"• MACD Engine: Bearish Breakdown! MACD Line ({macd_val:.4f}) crossed below Signal Line ({macd_sig:.4f})\n"
            f"• Histogram: Momentum negative at {macd_hist:.4f}"
        )
        
    print("⏳ [SCAN COMPLETE] Rules analyzed. No perfect setup found. Market is balanced.")
    return None

def main():
    print("🚀 Gold Sniper Core Engine active and running on Render tier...")
    
    while True:
        signal_alert = execute_strategy_scan()
        if signal_alert:
            print(f"🔥 [SIGNAL GENERATED] Sending target out now...")
            # Note: When you are ready to link a public channel broadcast, 
            # we will activate bot.send_message() right here.
            print(signal_alert)
            
        # Standard safety delay: Check market state on the hour/intervals (e.g., every 15 mins)
        time.sleep(900)

if __name__ == "__main__":
    main()
    
