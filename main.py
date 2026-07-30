import os
import time
import io
import requests
import threading
import datetime
import matplotlib
matplotlib.use('Agg') # Headless backend for cloud servers
import matplotlib.pyplot as plt
from flask import Flask

# --- 🔌 FLASK PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Gold Elite Structural Sniper & Visualizer is live and operational!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
MT5_BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "") 
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")

# --- ⚙️ SCHEDULE CONFIGURATION (7:00 AM Local WAT -> 6:00 UTC) ---
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

# --- 🛰️ MARKET DATA & STRUCTURAL INTELLIGENCE ---

def fetch_market_data():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {'range': '5d', 'interval': '15m', 'includePrePost': 'false'}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    highs, lows, closes, opens = [], [], [], []
    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json().get("chart", {}).get("result", [])[0]
            indicators = data.get("indicators", {}).get("quote", [{}])[0]
            highs = [float(x) for x in indicators.get("high", []) if x is not None]
            lows = [float(x) for x in indicators.get("low", []) if x is not None]
            closes = [float(x) for x in indicators.get("close", []) if x is not None]
            opens = [float(x) for x in indicators.get("open", []) if x is not None]
    except Exception as e:
        print(f"⚠️ Market data fetch error: {e}")
        
    current_price = closes[-1] if closes else 0.0
    return highs, lows, closes, opens, current_price

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 3.0 
    tr_list = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i-1])
        low_close = abs(lows[i] - closes[i-1])
        tr_list.append(max(high_low, high_close, low_close))
    atr = sum(tr_list[-period:]) / period
    return round(atr, 2)

def fetch_macro_news():
    macro_text = "Macro sentiment stable; tracking structural liquidity."
    sentiment_bias = "Neutral"
    try:
        news_url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=GC=F&apikey={ALPHA_VANTAGE_KEY}"
        news_res = requests.get(news_url, timeout=10).json()
        feed = news_res.get("feed", [])
        if feed:
            top_story = feed[0].get("title", "")
            score = float(feed[0].get("overall_sentiment_score", 0.0))
            sentiment_bias = "Bullish" if score > 0.10 else ("Bearish" if score < -0.10 else "Neutral")
            macro_text = f"News Pulse: '{top_story}' | Bias: {sentiment_bias}"
    except:
        pass
    return macro_text, sentiment_bias

# --- 📊 PROFESSIONAL MT5-STYLE CANDLESTICK CHART GENERATOR ---

def generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action):
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#121212')
    ax.set_facecolor('#1e1e1e')
    
    h = highs[-30:]
    l = lows[-30:]
    c = closes[-30:]
    o = opens[-30:] if len(opens) >= 30 else [c[max(0, i-1)] for i in range(len(c))]
    
    for i in range(len(c)):
        is_bullish = c[i] >= o[i]
        color = '#2ecc71' if is_bullish else '#e74c3c'  
        
        ax.plot([i, i], [l[i], h[i]], color=color, linewidth=1, zorder=2)
        
        body_bottom = min(o[i], c[i])
        body_height = max(abs(c[i] - o[i]), 0.1)
        ax.add_patch(plt.Rectangle((i - 0.35, body_bottom), 0.7, body_height, facecolor=color, edgecolor=color, zorder=3))

    ax.axhline(y=entry, color='#3498db', linestyle='--', linewidth=1.5, label=f'Entry: {entry}', zorder=4)
    ax.axhline(y=sl, color='#e74c3c', linestyle='-', linewidth=1.5, label=f'Stop Loss: {sl}', zorder=4)
    ax.axhline(y=tp, color='#2ecc71', linestyle='-', linewidth=1.5, label=f'Take Profit: {tp}', zorder=4)
    
    ax.set_title(f"XAUUSD M15 — {action}", color='#ffffff', fontsize=11, fontweight='bold', pad=12)
    ax.tick_params(colors='#888888', labelsize=8)
    ax.grid(True, color='#332222', linestyle='--', alpha=0.6, zorder=1)
    ax.legend(loc='upper left', facecolor='#2b2b2b', edgecolor='none', labelcolor='white', fontsize=8)
    
    for spine in ax.spines.values():
        spine.set_color('#444444')
        
    plt.tight_layout()
    
    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format='png', dpi=150, facecolor=fig.get_facecolor(), edgecolor='none')
    img_buffer.seek(0)
    plt.close(fig)
    return img_buffer

# --- 📱 TELEGRAM TRANSMISSION HELPERS ---

def send_telegram_photo(img_buffer, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {'photo': ('chart.png', img_buffer, 'image/png')}
    data = {'chat_id': TELEGRAM_CHANNEL_ID, 'caption': caption, 'parse_mode': 'Markdown'}
    try:
        requests.post(url, files=files, data=data, timeout=15)
    except Exception as e:
        print(f"⚠️ Telegram photo error: {e}")

def send_to_mt5_bridge(action, entry, sl, tp):
    if not MT5_BRIDGE_URL:
        return
    trade_action = "BUY" if "BUY" in action else "SELL"
    payload = {"symbol": "XAUUSD", "action": trade_action, "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "volume": 0.01}
    try:
        requests.post(MT5_BRIDGE_URL, json=payload, timeout=10)
    except:
        pass

# --- 🔒 TRUE STRUCTURAL DIRECTION ENGINE (NO HARDCODED NARROW OFFSETS) ---

def generate_or_get_daily_plan(forced=False):
    global daily_ledger
    today = datetime.datetime.now(datetime.timezone.utc).date()
    
    if daily_ledger["date"] == today and not forced:
        return daily_ledger

    print("🌅 Analyzing structural swing liquidity and true market direction...")
    highs, lows, closes, opens, current_price = fetch_market_data()
    macro_text, sentiment_bias = fetch_macro_news()
    
    if not closes or current_price == 0:
        return daily_ledger

    # True multi-period structural boundaries (50 candles back, avoiding micro-noise)
    true_resistance = max(highs[-50:])
    true_support = min(lows[-50:])
    atr_value = calculate_atr(highs, lows, closes)

    # Dynamic Directional Decision Matrix based on Macro Sentiment and Deep Swing Architecture
    if sentiment_bias == "Bearish" or (sentiment_bias == "Neutral" and current_price > (true_support + true_resistance) / 2):
        action = "SELL LIMIT"
        # Anchored directly to structural resistance ceiling with proper volatility padding
        entry = round(true_resistance, 2)
        sl = round(entry + (atr_value * 1.8), 2)
        tp = round(entry - (atr_value * 4.2), 2)
        reasoning = f"Deep structural sweep at resistance ceiling ({true_resistance:.2f}). Macro bias: {sentiment_bias}."
    else:
        action = "BUY LIMIT"
        # Anchored directly to structural support floor with proper volatility padding
        entry = round(true_support, 2)
        sl = round(entry - (atr_value * 1.8), 2)
        tp = round(entry + (atr_value * 4.2), 2)
        reasoning = f"Deep structural test at support floor ({true_support:.2f}). Macro bias: {sentiment_bias}."

    daily_ledger["date"] = today
    daily_ledger["action"] = action
    daily_ledger["entry"] = entry
    daily_ledger["sl"] = sl
    daily_ledger["tp"] = tp
    daily_ledger["reasoning"] = reasoning

    if forced:
        send_to_mt5_bridge(action, entry, sl, tp)
        
        chart_bytes = generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action)
        
        briefing = (
            f"🎯 **GOLD STRUCTURAL SNIPER BLUEPRINT** 🎯\n\n"
            f"• **Action:** **{action}**\n"
            f"• **Spot Reference:** `{current_price:.2f}`\n"
            f"• **Deep Structural Entry:** `{entry}`\n"
            f"• **Volatility-Buffered SL:** `{sl}`\n"
            f"• **Target TP:** `{tp}`\n\n"
            f"📰 **Macro Pulse:**\n> \"{macro_text}\"\n\n"
            f"🧠 **Structural Logic:**\n> \"{reasoning}\"\n\n"
            f"_Locked for 24 hours based on true swing boundaries._"
        )
        send_telegram_photo(chart_bytes, briefing)
        
    return daily_ledger

# --- 👁️ SENTINEL RISK WATCHDOG ---

def sentinel_market_monitor():
    while True:
        time.sleep(300)
        if not daily_ledger["action"]:
            continue
        highs, lows, closes, opens, current_price = fetch_market_data()
        macro_text, bias = fetch_macro_news()
        action = daily_ledger["action"]
        entry = daily_ledger["entry"]
        
        if ("BUY" in action and bias == "Bearish") or ("SELL" in action and bias == "Bullish"):
            alert_chart = generate_candlestick_chart(highs, lows, closes, opens, entry, daily_ledger["sl"], daily_ledger["tp"], f"⚠️ {action} [MACRO SHIFT]")
            
            warning_caption = (
                f"🚨 **SENTINEL WARNING: MACRO SHIFT** 🚨\n\n"
                f"Active Plan: {action} (Entry: `{entry}`)\n"
                f"⚠️ **News Shift Detected:** {macro_text}\n\n"
                f"_Consider securing break-even or adjusting risk profile._"
            )
            send_telegram_photo(alert_chart, warning_caption)
            time.sleep(3600)

def daily_scheduler():
    already_triggered_date = None
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        current_date = now.date()
        
        if now.hour == TRIGGER_HOUR and now.minute >= TRIGGER_MINUTE and already_triggered_date != current_date:
            try:
                generate_or_get_daily_plan(forced=True)
                already_triggered_date = current_date
            except Exception as e:
                print(f"❌ Error: {e}")
            time.sleep(60)
        else:
            generate_or_get_daily_plan(forced=False)
            time.sleep(30)

def main():
    print("🚀 Gold Structural Sniper Engine Initialized...")
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=daily_scheduler, daemon=True).start()
    threading.Thread(target=sentinel_market_monitor, daemon=True).start()
    
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
