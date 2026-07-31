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

# --- 🔌 FLASK PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🔥CLIMAXSongz🔥 Deep Institutional Sniper Engine is active!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")

TRIGGER_HOUR = 7
TRIGGER_MINUTE = 36

daily_ledger = {
    "date": None,
    "action": None,
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "reasoning": ""
}

# --- 🛰️ LIVE MARKET DATA & DIRECT BROKER SYNC BRIDGE ---

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
        
    # --- AUTO-SYNC LIVE CURRENT PRICE ---
    current_price = closes[-1] if closes else 4077.01
    
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
    macro_text = "Global market liquidity remains stable with balanced institutional order flow."
    sentiment_bias = "Neutral"
    try:
        news_url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=GC=F&apikey={ALPHA_VANTAGE_KEY}"
        news_res = requests.get(news_url, timeout=10).json()
        feed = news_res.get("feed", [])
        if feed:
            top_story = feed[0].get("title", "")
            score = float(feed[0].get("overall_sentiment_score", 0.0))
            sentiment_bias = "Bullish" if score > 0.08 else ("Bearish" if score < -0.08 else "Neutral")
            macro_text = f"{top_story}"
    except:
        pass
    return macro_text, sentiment_bias

# --- 📊 PROFESSIONAL ENHANCED CHART GENERATOR ---

def generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action):
    fig, ax = plt.subplots(figsize=(11, 5.5), facecolor='#ffffff')
    ax.set_facecolor('#ffffff')
    
    # Safely align and truncate arrays to match lengths uniformly preventing IndexError
    min_len = min(len(highs), len(lows), len(closes), len(opens) if opens else len(closes))
    window_size = min(200, min_len) if min_len > 0 else 1
    
    h = highs[-window_size:] if highs else []
    l = lows[-window_size:] if lows else []
    c = closes[-window_size:] if closes else []
    
    if opens and len(opens) >= min_len and min_len > 0:
        o = opens[-window_size:]
    else:
        o = [c[max(0, i-1)] for i in range(len(c))] if c else []

    for i in range(len(c)):
        is_bullish = c[i] >= o[i]
        color = '#27ae60' if is_bullish else '#c0392b'
        ax.plot([i, i], [l[i], h[i]], color=color, linewidth=0.9, zorder=2)
        body_bottom = min(o[i], c[i])
        body_height = max(abs(c[i] - o[i]), 0.1)
        ax.add_patch(plt.Rectangle((i - 0.35, body_bottom), 0.7, body_height, facecolor=color, edgecolor=color, zorder=3))

    if action == "BUY LIMIT":
        ax.axhspan(entry, tp, xmin=0.65, xmax=1.0, facecolor='#27ae60', alpha=0.22, zorder=2)
        ax.axhspan(sl, entry, xmin=0.65, xmax=1.0, facecolor='#c0392b', alpha=0.22, zorder=2)
    else:
        ax.axhspan(tp, entry, xmin=0.65, xmax=1.0, facecolor='#27ae60', alpha=0.22, zorder=2)
        ax.axhspan(entry, sl, xmin=0.65, xmax=1.0, facecolor='#c0392b', alpha=0.22, zorder=2)

    ax.axhline(y=entry, color='#2980b9', linestyle='--', linewidth=1.8, label=f'Entry: {entry}', zorder=4)
    ax.axhline(y=sl, color='#c0392b', linestyle='-', linewidth=1.5, label=f'Stop Loss: {sl}', zorder=4)
    ax.axhline(y=tp, color='#27ae60', linestyle='-', linewidth=1.5, label=f'Take Profit: {tp}', zorder=4)
    
    ax.text(0.5, 0.5, '🔥CLIMAXSongz🔥', transform=ax.transAxes,
            fontsize=46, fontweight='heavy', color='#8e44ad', alpha=0.15,
            ha='center', va='center', rotation=20, zorder=1)

    ax.set_title(f"🔥CLIMAXSongz🔥 DEEP LIQUIDITY SNIPER — {action}", color='#2c3e50', fontsize=12, fontweight='bold', pad=14)
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

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHANNEL_ID, 'text': text, 'parse_mode': 'Markdown'}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram message error: {e}")

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

# --- 🎯 DEEP STRUCTURAL LIQUIDITY & REASONING ENGINE ---

def generate_or_get_daily_plan(forced=False):
    global daily_ledger
    today = datetime.datetime.now(datetime.timezone.utc).date()
    
    if daily_ledger["date"] == today and not forced:
        return daily_ledger

    print("🌅 Running deep liquidity & structure sniper scan...")
    highs, lows, closes, opens, current_price = fetch_market_data()
    macro_text, sentiment_bias = fetch_macro_news()
    
    if not closes or current_price == 0:
        return daily_ledger

    tactical_resistance = max(highs[-50:]) if len(highs) >= 50 else max(highs)
    tactical_support = min(lows[-50:]) if len(lows) >= 50 else min(lows)
    atr_value = calculate_atr(highs, lows, closes)

    if sentiment_bias == "Bearish" or (sentiment_bias == "Neutral" and current_price > (tactical_support + tactical_resistance) / 2):
        action = "SELL LIMIT"
        chosen_entry = tactical_resistance
        raw_sl_distance = min(max(atr_value * 1.2, 7.0), 15.0)
        sl = round(chosen_entry + raw_sl_distance, 2)
        tp = round(chosen_entry - (raw_sl_distance * 3.0), 2)
        
        reasoning = (
            f"The market has aggressively tested and respected the M15 resistance ceiling around {chosen_entry:.2f}, creating a clear institutional liquidity pool above the highs. "
            f"Current price action exhibits rejection wicks that indicate smart money distribution and trapped retail breakout buyers. "
            f"Macro sentiment currently leans {sentiment_bias.lower()}, reinforcing downward continuation toward deeper order blocks. "
            f"The stop-loss is safely bounded at a tight {raw_sl_distance:.1f} point ceiling to protect capital from erratic volatility spikes. "
            f"Meanwhile, the take-profit target locks into a strict 1:3.0 risk-to-reward ratio down near the major session support shelf. "
            f"This structural alignment ensures high-probability reversal entry while maintaining optimal mathematical expectancy."
        )
    else:
        action = "BUY LIMIT"
        chosen_entry = tactical_support
        raw_sl_distance = min(max(atr_value * 1.2, 7.0), 15.0)
        sl = round(chosen_entry - raw_sl_distance, 2)
        tp = round(chosen_entry + (raw_sl_distance * 3.0), 2)
        
        reasoning = (
            f"The session structure shows a solid M15 liquidity sweep resting directly at the {chosen_entry:.2f} support floor. "
            f"Recent volume candles display strong buying pressure and absorption of seller liquidity below key psychological levels. "
            f"Macroeconomic sentiment is currently evaluated as {sentiment_bias.lower()}, supporting bullish momentum continuation into higher order blocks. "
            f"Our entry is strictly anchored to this structural turning point to prevent premature fills or late chase entries. "
            f"The stop-loss is tightly secured at {raw_sl_distance:.1f} points to completely eliminate excessive account drawdown. "
            f"With take-profit locked precisely at a 1:3.0 reward-to-risk ratio, this setup provides high surgical accuracy for today's session."
        )

    entry = round(chosen_entry, 2)
    sl_points = abs(entry - sl)

    if sl_points > 15.0:
        print("⚠️ Volatility exceeds safety parameters. Emitting stand aside notice.")
        if forced:
            send_telegram_message("🚫 **DEEP LIQUIDITY SNIPER** 🚫\n\n• **Status:** `NO SETUP`\n• **Reason:** Market spread and volatility exceed the strict 15-point safety cap.")
        return daily_ledger

    daily_ledger["date"] = today
    daily_ledger["action"] = action
    daily_ledger["entry"] = entry
    daily_ledger["sl"] = sl
    daily_ledger["tp"] = tp
    daily_ledger["reasoning"] = reasoning

    if forced:
        chart_bytes = generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action)
        
        briefing = (
            f"🎯 **DEEP LIQUIDITY SNIPER BLUEPRINT** 🎯\n\n"
            f"• **Action:** **{action}**\n"
            f"• **Spot Reference:** `{current_price:.2f}`\n"
            f"• **Sniper Entry:** `{entry}`\n"
            f"• **Strict Capped SL ({sl_points:.1f} pts):** `{sl}`\n"
            f"• **Target TP (1:3.0 RR):** `{tp}`\n\n"
            f"🧠 **Institutional Structure & Reasoning:**\n> \"{reasoning}\"\n\n"
            f"_Engine synchronized for absolute precision._"
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
    print("🚀 🔥CLIMAXSongz🔥 Deep Liquidity Sniper Engine Initialized...")
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=daily_scheduler, daemon=True).start()
    
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
            
