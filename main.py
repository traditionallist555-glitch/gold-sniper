import os
import time
import io
import requests
import threading
import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from flask import Flask

# Try importing native MT5 package for direct broker synchronization
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# --- 🔌 FLASK PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🔥CLIMAXSongz🔥 Direct-Broker Institutional Sniper is active!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")

TRIGGER_HOUR = 2
TRIGGER_MINUTE = 15

daily_ledger = {
    "date": None,
    "action": None,
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "reasoning": ""
}

# --- 🛰️ DIRECT BROKER DATA EXTRACTION ---

def fetch_broker_market_data(symbol="XAUUSD", num_bars=200):
    highs, lows, closes, opens = [], [], [], []
    current_price = 0.0
    
    if MT5_AVAILABLE and mt5.initialize():
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, num_bars)
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None:
            current_price = tick.bid
        
        if rates is not None and len(rates) > 0:
            highs = [float(r['high']) for r in rates]
            lows = [float(r['low']) for r in rates]
            closes = [float(r['close']) for r in rates]
            opens = [float(r['open']) for r in rates]
            if current_price == 0.0:
                current_price = closes[-1]
        mt5.shutdown()
    
    # Fallback to Yahoo Finance if MT5 terminal bridge is offline
    if not highs:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
        params = {'range': '10d', 'interval': '15m', 'includePrePost': 'false'}
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            res = requests.get(url, params=params, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json().get("chart", {}).get("result", [])[0]
                indicators = data.get("indicators", {}).get("quote", [{}])[0]
                highs = [float(x) for x in indicators.get("high", []) if x is not None][-num_bars:]
                lows = [float(x) for x in indicators.get("low", []) if x is not None][-num_bars:]
                closes = [float(x) for x in indicators.get("close", []) if x is not None][-num_bars:]
                opens = [float(x) for x in indicators.get("open", []) if x is not None][-num_bars:]
        except Exception as e:
            print(f"⚠️ Fallback data fetch error: {e}")
            
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
    macro_text = "Macro sentiment stable; tracking multi-timeframe structural liquidity."
    sentiment_bias = "Neutral"
    try:
        news_url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=GC=F&apikey={ALPHA_VANTAGE_KEY}"
        news_res = requests.get(news_url, timeout=10).json()
        feed = news_res.get("feed", [])
        if feed:
            top_story = feed[0].get("title", "")
            score = float(feed[0].get("overall_sentiment_score", 0.0))
            sentiment_bias = "Bullish" if score > 0.08 else ("Bearish" if score < -0.08 else "Neutral")
            macro_text = f"News Pulse: '{top_story}' | Bias: {sentiment_bias}"
    except:
        pass
    return macro_text, sentiment_bias

# --- 📊 PROFESSIONAL CLEAN WHITE CHART & WATERMARK GENERATOR ---

def generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action):
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#ffffff')
    ax.set_facecolor('#ffffff')
    
    window_size = min(200, len(closes))
    h = highs[-window_size:]
    l = lows[-window_size:]
    c = closes[-window_size:]
    o = opens[-window_size:] if len(opens) >= window_size else [c[max(0, i-1)] for i in range(len(c))]
    
    for i in range(len(c)):
        is_bullish = c[i] >= o[i]
        color = '#27ae60' if is_bullish else '#c0392b'
        
        ax.plot([i, i], [l[i], h[i]], color=color, linewidth=0.9, zorder=2)
        
        body_bottom = min(o[i], c[i])
        body_height = max(abs(c[i] - o[i]), 0.1)
        ax.add_patch(plt.Rectangle((i - 0.35, body_bottom), 0.7, body_height, facecolor=color, edgecolor=color, zorder=3))

    ax.axhline(y=entry, color='#2980b9', linestyle='--', linewidth=1.5, label=f'Entry: {entry}', zorder=4)
    ax.axhline(y=sl, color='#c0392b', linestyle='-', linewidth=1.5, label=f'Stop Loss: {sl}', zorder=4)
    ax.axhline(y=tp, color='#27ae60', linestyle='-', linewidth=1.5, label=f'Take Profit: {tp}', zorder=4)
    
    ax.text(0.5, 0.5, 'CLIMAXSongz', transform=ax.transAxes,
            fontsize=42, fontweight='bold', color='#9b59b6', alpha=0.18,
            ha='center', va='center', rotation=25, zorder=1)

    ax.set_title(f"🔥CLIMAXSongz🔥 XAUUSD Multi-Timeframe — {action}", color='#2c3e50', fontsize=11, fontweight='bold', pad=12)
    ax.tick_params(colors='#7f8c8d', labelsize=8)
    ax.grid(True, color='#ecf0f1', linestyle='--', alpha=0.8, zorder=1)
    ax.legend(loc='upper left', facecolor='#f8f9fa', edgecolor='#bdc3c7', labelcolor='#2c3e50', fontsize=8)
    
    for spine in ax.spines.values():
        spine.set_color('#bdc3c7')
        
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

# --- 🔒 HIGH-PRECISION INSTITUTIONAL ENGINE ---

def generate_or_get_daily_plan(forced=False):
    global daily_ledger
    today = datetime.datetime.now(datetime.timezone.utc).date()
    
    if daily_ledger["date"] == today and not forced:
        return daily_ledger

    print("🌅 Running direct broker structural scan & news confluence...")
    highs, lows, closes, opens, current_price = fetch_broker_market_data()
    macro_text, sentiment_bias = fetch_macro_news()
    
    if not closes or current_price == 0:
        return daily_ledger

    true_resistance = max(highs[-200:])
    true_support = min(lows[-200:])
    atr_value = calculate_atr(highs, lows, closes)

    if sentiment_bias == "Bearish" or (sentiment_bias == "Neutral" and current_price > (true_support + true_resistance) / 2):
        action = "SELL LIMIT"
        entry = round(true_resistance, 2)
        sl = round(entry + (atr_value * 2.0), 2)
        tp = round(entry - (atr_value * 5.0), 2)
        reasoning = f"Direct broker structural resistance boundary sweep ({true_resistance:.2f}). Macro Bias: {sentiment_bias}."
    else:
        action = "BUY LIMIT"
        entry = round(true_support, 2)
        sl = round(entry - (atr_value * 2.0), 2)
        tp = round(entry + (atr_value * 5.0), 2)
        reasoning = f"Direct broker structural support floor test ({true_support:.2f}). Macro Bias: {sentiment_bias}."

    daily_ledger["date"] = today
    daily_ledger["action"] = action
    daily_ledger["entry"] = entry
    daily_ledger["sl"] = sl
    daily_ledger["tp"] = tp
    daily_ledger["reasoning"] = reasoning

    if forced:
        chart_bytes = generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action)
        
        briefing = (
            f"🎯 **🔥CLIMAXSongz🔥 MASTER BLUEPRINT** 🎯\n\n"
            f"• **Action:** **{action}**\n"
            f"• **Spot Reference:** `{current_price:.2f}`\n"
            f"• **Institutional Entry:** `{entry}`\n"
            f"• **Buffered Stop Loss:** `{sl}`\n"
            f"• **Target Take Profit:** `{tp}`\n\n"
            f"📰 **Macro Confluence:**\n> \"{macro_text}\"\n\n"
            f"🧠 **Structural Logic:**\n> \"{reasoning}\"\n\n"
            f"_Locked for 24 hours. Zero midday drift._"
        )
        send_telegram_photo(chart_bytes, briefing)
        
    return daily_ledger

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
    print("🚀 🔥CLIMAXSongz🔥 Direct-Broker Sniper Engine Initialized...")
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=daily_scheduler, daemon=True).start()
    
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
    
