"""
🔥CLIMAXSongz🔥 Deep Liquidity Sniper Engine
=================================================
Changes in this revision (see chat for the full explanation):

  1. Market data source switched from Yahoo GC=F (COMEX gold FUTURES, on an
     unofficial, heavily rate-limited endpoint) to Twelve Data XAU/USD (spot
     gold — the same instrument MT5 quotes as "Gold"). This is the fix for
     prices not matching MT5. Yahoo is kept only as a last-resort fallback,
     and clearly flagged in the logs when used, since it's futures pricing,
     not spot.
  2. The scheduler no longer polls external APIs every ~30 seconds around
     the clock. It only fetches at the daily trigger, or on a manual
     /status?refresh=1 check. The old constant polling did nothing useful
     before the trigger (the ledger it built got thrown away and redone at
     trigger time anyway) while hammering Yahoo/Alpha Vantage all day — a
     strong candidate for why prices looked frozen/wrong.
  3. Stop-loss / take-profit sizing is now a percentage of price instead of
     fixed dollar points, so it won't go stale as gold's price level moves
     over time. The old "safety cap" check was dead code (it checked a
     value that had already been clamped below the cap) — fixed so it can
     actually trigger and stand the bot aside on genuinely volatile days.
  4. The "reasoning" text sent to Telegram is now built from real computed
     values (candle wick ratios, close distribution vs. range midpoint,
     ATR distance, actual news sentiment score) instead of fixed canned
     phrases about "smart money" / "institutional order flow" that the
     code was never actually detecting from the data.
  5. Added a GET /status endpoint so you can compare the bot's live fetched
     price against MT5 at any time, without waiting for the daily post.

New required env var: TWELVEDATA_API_KEY — free key at https://twelvedata.com
(Basic/free plan: 8 requests/min, 800/day — this bot uses about 1-2 a day.)
"""

import os
import time
import io
import requests
import threading
import datetime
from flask import Flask, jsonify, request
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# --- 🔌 FLASK PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🔥CLIMAXSongz🔥 Deep Institutional Sniper Engine is active!", 200

@app.route('/status')
def status():
    """
    Debug endpoint: compare the bot's live fetched gold price against MT5
    at any time. Add ?refresh=1 to force a fresh fetch (cooldown-limited so
    repeated refreshes can't burn through the daily API quota).
    """
    global _last_manual_refresh
    refresh_note = None
    if request.args.get('refresh') == '1':
        now_ts = time.time()
        elapsed = now_ts - _last_manual_refresh
        if elapsed >= STATUS_REFRESH_COOLDOWN_SECONDS:
            _last_manual_refresh = now_ts
            fetch_market_data()
            refresh_note = "refreshed"
        else:
            refresh_note = f"cooldown active, {STATUS_REFRESH_COOLDOWN_SECONDS - elapsed:.0f}s left - showing last fetched value"

    return jsonify({
        "todays_plan": {
            "date": str(daily_ledger["date"]) if daily_ledger["date"] else None,
            "action": daily_ledger["action"],
            "entry": daily_ledger["entry"],
            "sl": daily_ledger["sl"],
            "tp": daily_ledger["tp"],
        },
        "last_price_fetch": last_fetch_info,
        "refresh": refresh_note,
        "tip": "Add ?refresh=1 to force a fresh price fetch and compare against MT5.",
    })

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# --- 🔑 ENVIRONMENT / CONFIG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

TRIGGER_HOUR = 14
TRIGGER_MINUTE = 10

GOLD_SYMBOL_TWELVEDATA = "XAU/USD"

# Stop-loss sizing: percent-of-price instead of fixed dollar points, so this
# doesn't go stale as gold's price level changes. These defaults preserve
# your original 7-15 point behavior at ~$4077 gold; tune freely.
ATR_SL_MULTIPLIER = 1.2
MIN_SL_PCT = 0.0017   # ~7 points at $4077
MAX_SL_PCT = 0.0037   # ~15 points at $4077
RR_MULTIPLE = 3.0

STATUS_REFRESH_COOLDOWN_SECONDS = 20

daily_ledger = {
    "date": None,
    "action": None,
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "reasoning": ""
}

last_fetch_info = {"source": None, "price": None, "time": None}
_last_manual_refresh = 0

# --- 🛰️ LIVE MARKET DATA (spot gold, matches what MT5 shows) ---

def _fetch_from_twelvedata():
    """Primary source: XAU/USD spot - the same instrument MT5 quotes as Gold."""
    if not TWELVEDATA_API_KEY:
        print("⚠️ TWELVEDATA_API_KEY not set — skipping primary data source.")
        return None
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": GOLD_SYMBOL_TWELVEDATA,
        "interval": "15min",
        "outputsize": 200,
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        if data.get("status") == "error" or "values" not in data:
            print(f"⚠️ Twelve Data error: {data.get('message', data)}")
            return None

        rows = sorted(data["values"], key=lambda r: r["datetime"])
        opens = [float(r["open"]) for r in rows]
        highs = [float(r["high"]) for r in rows]
        lows = [float(r["low"]) for r in rows]
        closes = [float(r["close"]) for r in rows]

        if not closes:
            return None
        return highs, lows, closes, opens, closes[-1]
    except Exception as e:
        print(f"⚠️ Twelve Data fetch error: {e}")
        return None


def _fetch_from_yahoo_fallback():
    """
    Last-resort fallback only. GC=F is COMEX gold FUTURES, not spot - expect
    a basis gap vs. what MT5 shows for XAUUSD. Also an unofficial endpoint
    that Yahoo actively rate-limits, especially from cloud/datacenter IPs.
    """
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {'range': '5d', 'interval': '15m', 'includePrePost': 'false'}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ Yahoo fallback HTTP {res.status_code}")
            return None
        data = res.json().get("chart", {}).get("result", [])[0]
        indicators = data.get("indicators", {}).get("quote", [{}])[0]
        highs = [float(x) for x in indicators.get("high", []) if x is not None]
        lows = [float(x) for x in indicators.get("low", []) if x is not None]
        closes = [float(x) for x in indicators.get("close", []) if x is not None]
        opens = [float(x) for x in indicators.get("open", []) if x is not None]
        if not closes:
            return None
        print("⚠️ Using Yahoo GC=F fallback — this is FUTURES pricing and will likely diverge from MT5 spot gold.")
        return highs, lows, closes, opens, closes[-1]
    except Exception as e:
        print(f"⚠️ Yahoo fallback fetch error: {e}")
        return None


def fetch_market_data():
    global last_fetch_info

    result = _fetch_from_twelvedata()
    source = "twelvedata_spot"

    if result is None:
        result = _fetch_from_yahoo_fallback()
        source = "yahoo_futures_fallback"

    if result is None:
        print("🚨 All market data sources failed this cycle — no live price available.")
        last_fetch_info = {
            "source": "static_fallback",
            "price": 4077.01,
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "note": "This is a static placeholder, not a live price. Check TWELVEDATA_API_KEY and network access.",
        }
        return [], [], [], [], 4077.01

    highs, lows, closes, opens, current_price = result
    last_fetch_info = {
        "source": source,
        "price": current_price,
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return highs, lows, closes, opens, current_price


def calculate_atr(highs, lows, closes, period=14, current_price=None):
    if len(closes) < period + 1:
        # Not enough bars for a real ATR yet - estimate as a % of price
        # instead of a fixed constant that goes stale as price moves.
        if current_price:
            return round(current_price * 0.0015, 2)
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
    sentiment_score = 0.0
    try:
        # "GLD" (the gold ETF) is a ticker Alpha Vantage actually recognizes.
        # The previous "GC=F" is a Yahoo-specific futures ticker format that
        # Alpha Vantage's NEWS_SENTIMENT endpoint doesn't resolve, so that
        # call was very likely returning an empty feed most of the time.
        news_url = "https://www.alphavantage.co/query"
        params = {"function": "NEWS_SENTIMENT", "tickers": "GLD", "apikey": ALPHA_VANTAGE_KEY}
        news_res = requests.get(news_url, params=params, timeout=10).json()
        feed = news_res.get("feed", [])
        if feed:
            top_story = feed[0].get("title", "")
            sentiment_score = float(feed[0].get("overall_sentiment_score", 0.0))
            sentiment_bias = "Bullish" if sentiment_score > 0.08 else ("Bearish" if sentiment_score < -0.08 else "Neutral")
            macro_text = top_story
    except Exception as e:
        print(f"⚠️ Macro news fetch error: {e}")
    return macro_text, sentiment_bias, sentiment_score

# --- 📊 PROFESSIONAL ENHANCED CHART GENERATOR ---

def generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action):
    fig, ax = plt.subplots(figsize=(11, 5.5), facecolor='#ffffff')
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

# --- 🧠 REASONING ENGINE (grounded in real computed values) ---

def generate_reasoning(action, entry, sl, tp, current_price, closes, opens, highs, lows,
                        sl_distance, atr_value, sentiment_bias, sentiment_score):
    """
    Every specific claim below is computed from the fetched data (wick
    ratios, close distribution vs. range midpoint, ATR distance, real
    sentiment score) rather than being fixed template text. It deliberately
    does not claim things this code can't actually detect from OHLC bars
    alone (e.g. "smart money distribution", "institutional order flow") -
    those aren't derivable from this data, so asserting them was just
    flavor text dressed up as analysis.
    """
    lookback_n = min(20, len(closes))
    lookback_closes = closes[-lookback_n:]
    lookback_highs = highs[-lookback_n:]
    lookback_lows = lows[-lookback_n:]
    mid_range = (max(lookback_highs) + min(lookback_lows)) / 2

    last_o, last_h, last_l, last_c = opens[-1], highs[-1], lows[-1], closes[-1]
    candle_range = max(last_h - last_l, 0.01)
    upper_wick_pct = (last_h - max(last_o, last_c)) / candle_range * 100
    lower_wick_pct = (min(last_o, last_c) - last_l) / candle_range * 100

    dist_in_atr = abs(current_price - entry) / atr_value if atr_value else 0.0
    sl_pct_of_price = (sl_distance / entry) * 100 if entry else 0.0

    if action == "SELL LIMIT":
        closes_note_count = sum(1 for cl in lookback_closes if cl > mid_range)
        if upper_wick_pct > lower_wick_pct:
            wick_line = f"the most recent M15 candle printed an upper wick covering {upper_wick_pct:.0f}% of its range"
        else:
            wick_line = f"the most recent M15 candle closed without a strong upper rejection (upper wick only {upper_wick_pct:.0f}% of range)"
        level_word = "resistance"
        side_word = "upper"
    else:
        closes_note_count = sum(1 for cl in lookback_closes if cl < mid_range)
        if lower_wick_pct > upper_wick_pct:
            wick_line = f"the most recent M15 candle printed a lower wick covering {lower_wick_pct:.0f}% of its range"
        else:
            wick_line = f"the most recent M15 candle closed without a strong lower rejection (lower wick only {lower_wick_pct:.0f}% of range)"
        level_word = "support"
        side_word = "lower"

    reasoning = (
        f"Price is currently {dist_in_atr:.1f}x ATR away from the {entry:.2f} {level_word} level "
        f"that the last {lookback_n} M15 bars have tested. On the latest candle, {wick_line}, "
        f"and {closes_note_count} of the last {lookback_n} closes sit on the {side_word} side of the "
        f"recent range midpoint ({mid_range:.2f}). Gold-related news sentiment currently reads "
        f"{sentiment_bias.lower()} (score {sentiment_score:+.2f}). Stop-loss is sized at {sl_distance:.1f} "
        f"points ({sl_pct_of_price:.2f}% of price) from entry, with take-profit set at a "
        f"{RR_MULTIPLE:.1f}:1 reward-to-risk multiple."
    )
    return reasoning

# --- 🎯 DAILY PLAN ---

def generate_or_get_daily_plan(forced=False):
    global daily_ledger
    today = datetime.datetime.now(datetime.timezone.utc).date()

    if daily_ledger["date"] == today and not forced:
        return daily_ledger

    print("🌅 Running deep liquidity & structure sniper scan...")
    highs, lows, closes, opens, current_price = fetch_market_data()
    macro_text, sentiment_bias, sentiment_score = fetch_macro_news()

    if not closes or current_price == 0:
        print("⚠️ No usable market data this cycle — leaving today's ledger untouched.")
        return daily_ledger

    tactical_resistance = max(highs[-50:]) if len(highs) >= 50 else max(highs)
    tactical_support = min(lows[-50:]) if len(lows) >= 50 else min(lows)
    atr_value = calculate_atr(highs, lows, closes, current_price=current_price)

    min_sl_distance = current_price * MIN_SL_PCT
    max_sl_distance = current_price * MAX_SL_PCT
    natural_sl_distance = atr_value * ATR_SL_MULTIPLIER
    raw_sl_distance = min(max(natural_sl_distance, min_sl_distance), max_sl_distance)

    if sentiment_bias == "Bearish" or (sentiment_bias == "Neutral" and current_price > (tactical_support + tactical_resistance) / 2):
        action = "SELL LIMIT"
        chosen_entry = tactical_resistance
        sl = round(chosen_entry + raw_sl_distance, 2)
        tp = round(chosen_entry - (raw_sl_distance * RR_MULTIPLE), 2)
    else:
        action = "BUY LIMIT"
        chosen_entry = tactical_support
        sl = round(chosen_entry - raw_sl_distance, 2)
        tp = round(chosen_entry + (raw_sl_distance * RR_MULTIPLE), 2)

    entry = round(chosen_entry, 2)

    # This check now runs against the *unclamped* ATR-implied distance, so it
    # can actually fire. Previously raw_sl_distance was already capped at a
    # fixed 15.0 before this ran, so `sl_points > 15.0` could never be true -
    # it was dead code.
    if natural_sl_distance > max_sl_distance:
        print(f"⚠️ ATR-implied stop ({natural_sl_distance:.1f} pts) exceeds the {MAX_SL_PCT*100:.2f}% safety cap. Standing aside.")
        if forced:
            send_telegram_message(
                "🚫 **DEEP LIQUIDITY SNIPER** 🚫\n\n"
                "• **Status:** `NO SETUP`\n"
                "• **Reason:** Current volatility exceeds the strict safety cap for today's stop-loss sizing."
            )
        return daily_ledger

    reasoning = generate_reasoning(action, entry, sl, tp, current_price, closes, opens, highs, lows,
                                    raw_sl_distance, atr_value, sentiment_bias, sentiment_score)

    daily_ledger["date"] = today
    daily_ledger["action"] = action
    daily_ledger["entry"] = entry
    daily_ledger["sl"] = sl
    daily_ledger["tp"] = tp
    daily_ledger["reasoning"] = reasoning

    if forced:
        chart_bytes = generate_candlestick_chart(highs, lows, closes, opens, entry, sl, tp, action)
        sl_points = abs(entry - sl)
        briefing = (
            f"🎯 **DEEP LIQUIDITY SNIPER BLUEPRINT** 🎯\n\n"
            f"• **Action:** **{action}**\n"
            f"• **Spot Reference:** `{current_price:.2f}`\n"
            f"• **Sniper Entry:** `{entry}`\n"
            f"• **Stop Loss ({sl_points:.1f} pts):** `{sl}`\n"
            f"• **Target TP (1:{RR_MULTIPLE:.1f} RR):** `{tp}`\n\n"
 
