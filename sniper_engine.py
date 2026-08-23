import os
import io
import json
import asyncio
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import requests
from flask import Flask, jsonify
from metaapi_cloud_sdk import MetaApi
from google import genai
from google.genai import types

app = Flask(__name__)

# --- ENVIRONMENT VARIABLES ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
META_API_TOKEN = os.getenv("META_API_TOKEN")
META_ACCOUNT_ID = os.getenv("META_ACCOUNT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "1000"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))
MAX_ALLOWED_SPREAD = float(os.getenv("MAX_SPREAD_PIPS", "2.5"))

# --- INITIALIZATION ---
ai_client = genai.Client(api_key=GEMINI_API_KEY)
LAST_PROCESSED_CANDLE = None

SYSTEM_INSTRUCTION = """
You are an elite SMC Gold Scalper analyzing 15M and 1M chart images with extreme microscopic scrutiny.

CRITICAL EXECUTION RULES:
1. 15M BIAS: Determine structure relative to 200 EMA and swing points. Never trade against 15M macro trend.
2. 1M LIQUIDITY SWEEP: Identify sweep candle with sharp wick extension past key levels and rejection (min 40% wick ratio).
3. 1M DISPLACEMENT & FVG: Look for aggressive candles following the sweep leaving visible 3-candle FVG gaps.
4. 1M MARKET STRUCTURE SHIFT (MSS): Candle MUST close past internal swing high (BUY) or low (SELL).
5. RISK MANAGEMENT: Maintain 1:3 R:R. Dynamic stop-loss between 9.0 and 16.0 points.
6. If choppy or ambiguous, output "decision": "WAIT".

Output ONLY JSON matching structure:
{"decision": "BUY"|"SELL"|"WAIT", "confidence": 0-100, "entry": float, "sl": float, "tp": float, "rationale": "Short explanation"}
"""

# --- UTILITY FUNCTIONS ---
def calculate_position_size(entry, sl, balance, risk_pct):
    try:
        sl_points = abs(entry - sl)
        if sl_points == 0:
            return 0.01
        dollar_risk = balance * (risk_pct / 100.0)
        lot_size = dollar_risk / (sl_points * 100.0)
        return round(max(0.01, lot_size), 2)
    except Exception:
        return 0.01

def send_telegram_alert(img_bytes, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", img_bytes, "image/png")}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=data, files=files, timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def generate_chart_bytes(df_15m, df_1m):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), dpi=100)
    
    # 15M Chart
    ax1.plot(df_15m.index, df_15m['close'], color='black', linewidth=1, label='Price')
    if 'EMA200' in df_15m.columns:
        ax1.plot(df_15m.index, df_15m['EMA200'], color='blue', linestyle='--', label='200 EMA')
    ax1.set_title("15M MACRO STRUCTURE", fontsize=9, fontweight='bold')
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc='upper left')

    # 1M Chart
    ax2.plot(df_1m.index, df_1m['close'], color='darkgreen', linewidth=1, label='Price')
    ax2.set_title("1M EXECUTION FRAME", fontsize=9, fontweight='bold')
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    img_data = buf.getvalue()
    fig.clf()
    plt.close(fig)
    return img_data

# --- METAAPI FETCH & EXECUTE ---
async def fetch_metaapi_data():
    api = MetaApi(META_API_TOKEN)
    account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    candles_15m = await connection.get_historical_candles('XAUUSD', '15m', None, 100)
    candles_1m = await connection.get_historical_candles('XAUUSD', '1m', None, 40)
    
    price = await connection.get_symbol_price('XAUUSD')
    spread = abs(price['ask'] - price['bid'])

    df_15m = pd.DataFrame(candles_15m)
    df_15m['time'] = pd.to_datetime(df_15m['time'])
    df_15m.set_index('time', inplace=True)
    df_15m['EMA200'] = df_15m['close'].ewm(span=200, adjust=False).mean()

    df_1m = pd.DataFrame(candles_1m)
    df_1m['time'] = pd.to_datetime(df_1m['time'])
    df_1m.set_index('time', inplace=True)

    return df_15m, df_1m, spread, connection

async def execute_metaapi_order(connection, action, lot_size, sl, tp):
    symbol = "XAUUSD"
    if action == "BUY":
        return await connection.create_market_buy_order(symbol=symbol, volume=lot_size, stop_loss=sl, take_profit=tp)
    elif action == "SELL":
        return await connection.create_market_sell_order(symbol=symbol, volume=lot_size, stop_loss=sl, take_profit=tp)

# --- ENDPOINTS ---
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "online", "engine": "Gold Sniper Bot", "endpoint": "/scan"})

@app.route("/scan", methods=["GET", "POST"])
def scan_and_execute():
    global LAST_PROCESSED_CANDLE
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        df_15m, df_1m, spread, connection = loop.run_until_complete(fetch_metaapi_data())
    except Exception as e:
        return jsonify({"status": "error", "message": f"MetaApi data fetch failed: {str(e)}"}), 500

    current_candle_time = str(df_1m.index[-1])
    if LAST_PROCESSED_CANDLE == current_candle_time:
        loop.close()
        return jsonify({"status": "skipped_duplicate_candle", "time": current_candle_time}), 200

    if spread > MAX_ALLOWED_SPREAD:
        loop.close()
        return jsonify({"status": "skipped_high_spread", "spread": round(spread, 2)}), 200

    LAST_PROCESSED_CANDLE = current_candle_time
    chart_bytes = generate_chart_bytes(df_15m, df_1m)

    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=chart_bytes, mime_type="image/png"),
            "Analyze chart for active SMC setups."
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.1,
            response_mime_type="application/json"
        )
    )

    ai_res = json.loads(response.text)
    decision = ai_res.get("decision")
    confidence = ai_res.get("confidence", 0)

    if decision in ["BUY", "SELL"] and confidence >= 85:
        entry = float(ai_res.get('entry', 0))
        sl = float(ai_res.get('sl', 0))
        tp = float(ai_res.get('tp', 0))
        lot_size = calculate_position_size(entry, sl, ACCOUNT_BALANCE, RISK_PERCENT)

        # Execute Order via MetaApi
        try:
            order_res = loop.run_until_complete(execute_metaapi_order(connection, decision, lot_size, sl, tp))
            ai_res["execution_status"] = "SUCCESS"
            ai_res["order_details"] = str(order_res)
        except Exception as exec_err:
            ai_res["execution_status"] = "FAILED"
            ai_res["execution_error"] = str(exec_err)

        # Dispatch Telegram Alert
        caption = (
            f"⚡ *GOLD SCALPING SIGNAL FIRED* ⚡\n\n"
            f"Action: *{decision} XAUUSD*\n"
            f"Entry: `{entry}`\nSL: `{sl}` | TP: `{tp}`\n"
            f"Lot Size: `{lot_size}` ({RISK_PERCENT}% Risk)\n"
            f"Confidence: *{confidence}%*\n\n"
            f"🧠 *Rationale:* _{ai_res.get('rationale')}_"
        )
        send_telegram_alert(chart_bytes, caption)

    loop.close()
    return jsonify({"status": "scanned", "result": ai_res})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    
