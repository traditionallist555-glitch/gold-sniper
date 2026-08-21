import os
import io
import json
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
from flask import Flask, jsonify
from google import genai
from google.genai import types

app = Flask(__name__)

# Environment Variables (Set on Render.com)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
LAST_ALERTED_CANDLE_TIME = None

def fetch_gold_market_data():
    """Fetches real-time 1H and 1M candle data for Gold using direct session requests."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    ticker = yf.Ticker("GC=F", session=session)
    gold_1h = ticker.history(period="5d", interval="1h")
    gold_1m = ticker.history(period="1d", interval="1m")

    for df in [gold_1h, gold_1m]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    df_1h = gold_1h[['Open', 'High', 'Low', 'Close']].copy().dropna()
    df_1m = gold_1m[['Open', 'High', 'Low', 'Close']].copy().dropna()
    
    return df_1h, df_1m

def generate_multi_tf_chart(df_1h, df_1m):
    """Renders multi-timeframe chart for Gemini Vision review."""
    df_1h['ema200'] = df_1h['Close'].ewm(span=200, adjust=False).mean()
    
    c_1h = df_1h.iloc[:-1].tail(40).copy()
    c_1m = df_1m.iloc[:-1].tail(50).copy()

    mc = mpf.make_marketcolors(up='#089981', down='#f23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

    fig = plt.figure(figsize=(10, 8), dpi=150)
    ax1 = fig.add_subplot(2, 1, 1)
    ax2 = fig.add_subplot(2, 1, 2)

    # 1H Macro Chart
    mpf.plot(c_1h[['Open', 'High', 'Low', 'Close']], type='candle', ax=ax1, style=style)
    ax1.set_title("1H MACRO TREND & STRUCTURE", fontsize=10, fontweight='bold', loc='left')

    # 1M Execution Chart
    mpf.plot(c_1m[['Open', 'High', 'Low', 'Close']], type='candle', ax=ax2, style=style)
    ax2.set_title("1M EXECUTION WINDOW (LIQUIDITY SWEEPS)", fontsize=10, fontweight='bold', loc='left')

    plt.tight_layout()
    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', bbox_inches='tight')
    img_buf.seek(0)
    plt.close(fig)
    return img_buf

def evaluate_chart_with_gemini(img_buf):
    """Passes visual market charts to Gemini 2.5 Flash Vision engine."""
    img_buf.seek(0)
    image_bytes = img_buf.read()
    img_buf.seek(0)

    prompt = (
        "You are an elite Smart Money Concepts (SMC) Gold Scalper.\n"
        "Analyze the composite chart provided:\n"
        "- Top: 1H Macro Trend.\n"
        "- Bottom: 1M Execution Window.\n\n"
        "Rules:\n"
        "1. Never trade against 1H macro trend.\n"
        "2. Look for clear liquidity sweeps on 1M followed by strong displacement.\n"
        "3. If setup is choppy or ambiguous, return 'WAIT'.\n\n"
        "Respond STRICTLY in valid JSON format with keys:\n"
        '{"decision": "BUY"|"SELL"|"WAIT", "confidence": 0-100, "entry": float, "sl": float, "tp": float, "rationale": "Short explanation"}'
    )

    response = gemini_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type='image/png'),
            prompt
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    return json.loads(response.text)

def send_telegram_alert(img_buf, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", img_buf, "image/png")}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    requests.post(url, data=data, files=files, timeout=10)

@app.route("/", methods=["GET"])
def run_autonomous_scanner():
    global LAST_ALERTED_CANDLE_TIME
    try:
        df_1h, df_1m = fetch_gold_market_data()
        
        if df_1m.empty or len(df_1m) < 50:
            return jsonify({"status": "insufficient_data"}), 200

        latest_time = str(df_1m.index[-2])
        if LAST_ALERTED_CANDLE_TIME == latest_time:
            return jsonify({"status": "waiting_for_next_candle"}), 200

        chart_buf = generate_multi_tf_chart(df_1h, df_1m)
        ai_res = evaluate_chart_with_gemini(chart_buf)

        decision = ai_res.get("decision")
        confidence = ai_res.get("confidence", 0)

        if decision in ["BUY", "SELL"] and confidence >= 85:
            LAST_ALERTED_CANDLE_TIME = latest_time
            caption = (
                f"⚡ *GOLD M1 SCALPING SIGNAL* ⚡\n\n"
                f"*{decision} XAU/USD*\n"
                f"Entry: `{ai_res.get('entry')}`\n"
                f"SL: `{ai_res.get('sl')}`\n"
                f"TP: `{ai_res.get('tp')}`\n"
                f"Confidence: *{confidence}%*\n\n"
                f"🧠 *Gemini Vision Reason:* _{ai_res.get('rationale')}_"
            )
            send_telegram_alert(chart_buf, caption)
            return jsonify({"status": "signal_fired", "data": ai_res}), 200

        return jsonify({"status": "scanning", "ai_eval": ai_res}), 200

    except Exception as e:
        print(f"Pipeline Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
    
