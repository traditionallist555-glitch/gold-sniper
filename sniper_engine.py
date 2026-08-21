import os
import io
import json
import base64
import gc
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
from flask import Flask, jsonify
from google import genai
from google.genai import types

app = Flask(__name__)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "1000")) # Default $1,000 balance
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))         # Risk 1.0% per trade
MAX_ALLOWED_SPREAD = 2.5                                      # Max spread threshold in Gold points

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
LAST_PROCESSED_CANDLE = None

def fetch_gold_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    url_1h = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=2d&interval=1h"
    url_1m = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m"

    def parse_json(url):
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()['chart']['result'][0]
        timestamps = data['timestamp']
        quote = data['indicators']['quote'][0]
        df = pd.DataFrame({
            'Open': quote['open'],
            'High': quote['high'],
            'Low': quote['low'],
            'Close': quote['close']
        }, index=pd.to_datetime(timestamps, unit='s'))
        return df.dropna()

    return parse_json(url_1h), parse_json(url_1m)

def check_market_spread(df_1m):
    """Calculates spread estimate from recent high-low/close volatility to detect high-spread periods."""
    last_candle = df_1m.iloc[-1]
    estimated_spread = round(abs(last_candle['High'] - last_candle['Low']), 2)
    return estimated_spread

def calculate_position_size(entry, sl, balance, risk_pct):
    """Calculates dynamic lot sizing based on account risk percentage and SL point distance."""
    try:
        sl_points = abs(entry - sl)
        if sl_points == 0:
            return 0.01
        
        dollar_risk = balance * (risk_pct / 100.0)
        # Standard Gold Contract: 1 Lot = $100 per $1 move (1 point = $100 per lot)
        lot_size = dollar_risk / (sl_points * 100.0)
        return round(max(0.01, lot_size), 2)
    except Exception:
        return 0.01

def generate_multi_tf_chart(df_1h, df_1m):
    c_1h = df_1h.tail(30)
    c_1m = df_1m.tail(30)

    mc = mpf.make_marketcolors(up='#089981', down='#f23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

    fig = plt.figure(figsize=(7, 5), dpi=80)
    ax1 = fig.add_subplot(2, 1, 1)
    ax2 = fig.add_subplot(2, 1, 2)

    mpf.plot(c_1h[['Open', 'High', 'Low', 'Close']], type='candle', ax=ax1, style=style)
    ax1.set_title("1H STRUCTURE", fontsize=8, fontweight='bold', loc='left')

    mpf.plot(c_1m[['Open', 'High', 'Low', 'Close']], type='candle', ax=ax2, style=style)
    ax2.set_title("1M EXECUTION", fontsize=8, fontweight='bold', loc='left')

    plt.tight_layout()
    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', bbox_inches='tight')
    img_buf.seek(0)
    
    plt.close('all')
    gc.collect()
    return img_buf

def evaluate_chart_with_gemini(img_buf):
    image_bytes = img_buf.getvalue()

    prompt = (
        "You are an elite SMC Gold Scalper.\n"
        "Analyze the attached chart (Top: 1H Macro, Bottom: 1M Execution).\n"
        "Rules:\n"
        "1. Never trade against 1H macro trend.\n"
        "2. Look for clear liquidity sweeps on 1M followed by displacement.\n"
        "3. Maintain a 1:3 risk-to-reward ratio with dynamic stop-loss between 9 and 16 points.\n"
        "4. If setup is choppy or ambiguous, set decision to 'WAIT'.\n\n"
        "Output ONLY a raw JSON object (no markdown formatting, no backticks) in this exact structure:\n"
        '{"decision": "BUY"|"SELL"|"WAIT", "confidence": 0-100, "entry": float, "sl": float, "tp": float, "rationale": "Short explanation"}'
    )

    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.7-flash',
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type='image/png'),
                prompt
            ]
        )
        
        content = response.text.strip()
        
        # Clean formatting backticks if present
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
            
        return json.loads(content)

    except Exception as e:
        return {
            "decision": "WAIT",
            "confidence": 0,
            "entry": 0.0,
            "sl": 0.0,
            "tp": 0.0,
            "rationale": f"Gemini processing error: {str(e)}"
        }

def generate_daily_affirmation():
    """Generates a fresh 10 to 15 line encouraging affirmation and blessing."""
    prompt = (
        "Write a powerful, uplifting, and faith-filled daily affirmation and blessing for a trader.\n"
        "Keep it strictly between 10 to 15 lines.\n"
        "Focus on clarity of mind, patience, discipline, continuous abundance, wisdom, and peace of heart.\n"
        "Make it direct, deeply encouraging, and inspiring."
    )
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.7-flash',
            contents=[prompt]
        )
        return response.text.strip()
    except Exception as e:
        return "✨ Today is filled with peace, unshakeable focus, and boundless opportunity. Step forward with confidence and wisdom!"

def send_telegram_text(text):
    """Sends pure text messages to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, data=data, timeout=10)
    return resp.json()

def send_telegram_alert(img_buf, caption):
    img_buf.seek(0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", img_buf, "image/png")}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    resp = requests.post(url, data=data, files=files, timeout=10)
    return resp.json()

@app.route("/", methods=["GET"])
def run_autonomous_scanner():
    global LAST_PROCESSED_CANDLE
    try:
        df_1h, df_1m = fetch_gold_data()
        current_candle_time = str(df_1m.index[-1])

        if LAST_PROCESSED_CANDLE == current_candle_time:
            return jsonify({"status": "skipped_duplicate_candle", "candle_time": current_candle_time}), 200

        # Check market volatility / spread filter
        estimated_spread = check_market_spread(df_1m)
        if estimated_spread > MAX_ALLOWED_SPREAD:
            return jsonify({"status": "skipped_high_spread", "spread": estimated_spread}), 200

        LAST_PROCESSED_CANDLE = current_candle_time

        chart_buf = generate_multi_tf_chart(df_1h, df_1m)
        ai_res = evaluate_chart_with_gemini(chart_buf)

        decision = ai_res.get("decision")
        confidence = ai_res.get("confidence", 0)

        if decision in ["BUY", "SELL"] and confidence >= 85:
            entry = float(ai_res.get('entry', 0))
            sl = float(ai_res.get('sl', 0))
            
            # Dynamic Lot Sizing
            recommended_lot = calculate_position_size(entry, sl, ACCOUNT_BALANCE, RISK_PERCENT)

            caption = (
                f"⚡ *GOLD M1 SCALPING SIGNAL* ⚡\n\n"
                f"*{decision} XAU/USD*\n"
                f"Entry: `{entry}`\n"
                f"SL: `{sl}`\n"
                f"TP: `{ai_res.get('tp')}`\n"
                f"Rec. Lot Size: `{recommended_lot}` (1% Risk)\n"
                f"Confidence: *{confidence}%*\n\n"
                f"🧠 *Reason:* _{ai_res.get('rationale')}_"
            )
            tg_status = send_telegram_alert(chart_buf, caption)
            return jsonify({"status": "signal_fired", "telegram": tg_status, "data": ai_res, "lot_size": recommended_lot}), 200

        return jsonify({"status": "scanning", "ai_eval": ai_res}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/daily_affirmation", methods=["GET"])
def trigger_daily_affirmation():
    """Endpoint to trigger daily affirmation delivery via cron at 1 AM."""
    try:
        affirmation_text = generate_daily_affirmation()
        formatted_message = (
            "🌅 *DAILY BLESSING & AFFIRMATION* 🌅\n\n"
            f"{affirmation_text}\n\n"
            "✨ *May your mind stay sharp and your decisions remain disciplined today.*"
        )
        tg_res = send_telegram_text(formatted_message)
        return jsonify({"status": "affirmation_sent", "telegram_response": tg_res}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
