import os
import io
import json
import gc
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # Non-GUI headless backend
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

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
LAST_ALERTED_CANDLE_TIME = None

def fetch_gold_data():
    """Fetches gold candles with minimal memory usage."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
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

def generate_multi_tf_chart(df_1h, df_1m):
    """Generates visual chart using low DPI (80) to preserve memory on Render Free Tier."""
    c_1h = df_1h.tail(30)
    c_1m = df_1m.tail(30)

    mc = mpf.make_marketcolors(up='#089981', down='#f23645', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

    # Low DPI (80) and compact figure size prevents SIGKILL memory crashes
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
    
    # Close figures immediately and force garbage collector
    plt.close('all')
    gc.collect()
    
    return img_buf

def evaluate_chart_with_gemini(img_buf):
    """Evaluates image via Gemini 3.6 Flash."""
    image_bytes = img_buf.getvalue()

    prompt = (
        "You are an elite SMC Gold Scalper.\n"
        "Analyze the composite chart (Top: 1H Macro, Bottom: 1M Execution).\n"
        "Rules:\n"
        "1. Never trade against 1H macro trend.\n"
        "2. Look for clear liquidity sweeps on 1M followed by displacement.\n"
        "3. If setup is choppy or ambiguous, return 'WAIT'.\n\n"
        "Respond STRICTLY in valid JSON format:\n"
        '{"decision": "BUY"|"SELL"|"WAIT", "confidence": 0-100, "entry": float, "sl": float, "tp": float, "rationale": "Short explanation"}'
    )

    response = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
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
    """Sends chart photo and alert to Telegram."""
    img_buf.seek(0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", img_buf, "image/png")}
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption,
        "parse_mode": "Markdown"
    }
    resp = requests.post(url, data=data, files=files, timeout=10)
    return resp.json()

@app.route("/", methods=["GET"])
def run_autonomous_scanner():
    global LAST_ALERTED_CANDLE_TIME
    try:
        df_1h, df_1m = fetch_gold_data()
        latest_time = str(df_1m.index[-1])

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
                f"🧠 *Reason:* _{ai_res.get('rationale')}_"
            )
            tg_status = send_telegram_alert(chart_buf, caption)
            return jsonify({"status": "signal_fired", "telegram": tg_status, "data": ai_res}), 200

        return jsonify({"status": "scanning", "ai_eval": ai_res}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    
