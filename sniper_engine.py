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
from groq import Groq

app = Flask(__name__)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)
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

def evaluate_chart_with_groq(img_buf):
    base64_image = base64.b64encode(img_buf.getvalue()).decode('utf-8')

    prompt = (
        "You are an elite SMC Gold Scalper.\n"
        "Analyze the attached chart (Top: 1H Macro, Bottom: 1M Execution).\n"
        "Rules:\n"
        "1. Never trade against 1H macro trend.\n"
        "2. Look for clear liquidity sweeps on 1M followed by displacement.\n"
        "3. Maintain a 1:3 risk-to-reward ratio with dynamic stop-loss between 9 and 16 points.\n"
        "4. If setup is choppy or ambiguous, set decision to 'WAIT'.\n\n"
        "Output ONLY a raw JSON object (no markdown, no backticks) in this exact structure:\n"
        '{"decision": "BUY"|"SELL"|"WAIT", "confidence": 0-100, "entry": float, "sl": float, "tp": float, "rationale": "Short explanation"}'
    )

    # Uses Groq's active multimodal vision model endpoint
    response = groq_client.chat.completions.create(
        model="llama-3.2-11b-vision-preview",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{base64_image}"
                        },
                    },
                ],
            }
        ],
        temperature=0.2,
        max_tokens=300
    )
    
    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.replace("```json", "").replace("```", "").strip()
        
    return json.loads(content)

def send_telegram_alert(img_buf, caption):
    img_buf.seek(0)
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendPhoto"
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

        LAST_PROCESSED_CANDLE = current_candle_time

        chart_buf = generate_multi_tf_chart(df_1h, df_1m)
        ai_res = evaluate_chart_with_groq(chart_buf)

        decision = ai_res.get("decision")
        confidence = ai_res.get("confidence", 0)

        if decision in ["BUY", "SELL"] and confidence >= 85:
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
    
