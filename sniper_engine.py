import datetime
import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from flask import Flask, jsonify

# ==============================================================================
# --- ⚙️ CONFIGURATION & GLOBAL CONSTANTS ---
# ==============================================================================

SYMBOL_LABEL = "XAUUSD"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")

# 🕒 Execution Window: 1:00 PM – 3:30 PM WAT (12:00 PM – 2:30 PM UTC)
SESSION_START_HOUR_UTC = 12
SESSION_START_MIN_UTC = 0
SESSION_END_HOUR_UTC = 14
SESSION_END_MIN_UTC = 30

# 🎯 Risk Parameters
RR_MULTIPLE = 3.0
ENTRY_MODE = "EDGE"             # "EDGE" = outer boundary of FVG for rapid fill
MAX_SL_POINTS = 15.0            # Hard cap SL at $1.50 (15 pts on Gold)
MIN_SL_POINTS = 3.0             # Floor SL at $0.30
SL_BUFFER_ATR_MULT = 0.20       # Padding past sweep wick
FVG_MIN_GAP_ATR_MULT = 0.15     # Minimum size threshold for FVG
STRUCTURE_LOOKBACK = 60

# ==============================================================================
# --- 🪵 LOGGING & FLASK INFRASTRUCTURE ---
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("SniperEngine")

app = Flask(__name__)
state_lock = threading.Lock()
chart_lock = threading.Lock()


@dataclass
class MarketData:
    highs: List[float]
    lows: List[float]
    closes: List[float]
    opens: List[float]
    timestamps: List[datetime.datetime]
    current_price: float
    source: str


@dataclass
class SniperLedger:
    date: datetime.date
    status: str                         # "hunting", "signal", "standby", "no_setup"
    action: Optional[str] = None        # "BUY LIMIT" or "SELL LIMIT"
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    reasoning: str = ""
    choch_detected: bool = False
    liquidity_swept: bool = False
    bos_detected: bool = False
    fvg_detected: bool = False
    data_source: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": str(self.date),
            "status": self.status,
            "action": self.action,
            "entry": round(self.entry, 2) if self.entry else None,
            "sl": round(self.sl, 2) if self.sl else None,
            "tp": round(self.tp, 2) if self.tp else None,
            "reasoning": self.reasoning,
            "choch_detected": self.choch_detected,
            "liquidity_swept": self.liquidity_swept,
            "bos_detected": self.bos_detected,
            "fvg_detected": self.fvg_detected,
            "data_source": self.data_source,
        }


class EngineState:
    def __init__(self):
        self.ledger = SniperLedger(
            date=datetime.date.today(),
            status="standby",
            reasoning="Engine initialized and waiting for execution window."
        )


engine_state = EngineState()

# ==============================================================================
# --- 📰 NEWS GUARD FILTER ---
# ==============================================================================

def is_high_impact_news_near(buffer_minutes: int = 15) -> bool:
    """
    Halts execution 15 minutes before and after high-impact USD economic releases.
    """
    try:
        url = "https://raw.githubusercontent.com/sammydow/forex-factory-json/main/calendar.json"
        res = requests.get(url, timeout=4)
        if res.status_code != 200:
            return False

        events = res.json()
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        for event in events:
            if event.get("country") == "USD" and event.get("impact") == "High":
                event_str = event.get("date")
                if not event_str:
                    continue
                
                event_dt = datetime.datetime.fromisoformat(event_str.replace("Z", "+00:00"))
                diff_minutes = abs((now_utc - event_dt).total_seconds()) / 60.0

                if diff_minutes <= buffer_minutes:
                    logger.warning(f"⚠️ High Impact News Blocked Trade: {event.get('title')}")
                    return True

    except Exception as e:
        logger.warning(f"News guard check failed open: {e}")
        return False

    return False

# ==============================================================================
# --- 📊 DATA & TECHNICAL CALCULATION ENGINE ---
# ==============================================================================

def fetch_market_data() -> MarketData:
    url = (
        f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY"
        f"&symbol={SYMBOL_LABEL}&interval=15min&apikey={ALPHA_VANTAGE_KEY}"
    )
    try:
        res = requests.get(url, timeout=6)
        data = res.json()
        ts_key = "Time Series (15min)"
        if ts_key in data:
            raw_ts = data[ts_key]
            dates, opens, highs, lows, closes = [], [], [], [], []
            for t_str in sorted(raw_ts.keys()):
                dt = datetime.datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=datetime.timezone.utc)
                dates.append(dt)
                opens.append(float(raw_ts[t_str]["1. open"]))
                highs.append(float(raw_ts[t_str]["2. high"]))
                lows.append(float(raw_ts[t_str]["3. low"]))
                closes.append(float(raw_ts[t_str]["4. close"]))

            return MarketData(
                highs=highs, lows=lows, closes=closes, opens=opens,
                timestamps=dates, current_price=closes[-1], source="alpha_vantage_live"
            )
    except Exception as err:
        logger.warning(f"Live API unavailable ({err}). Triggering fallback simulation.")

    return generate_offline_demo_series()


def generate_offline_demo_series() -> MarketData:
    base_price = 4375.00
    now = datetime.datetime.now(datetime.timezone.utc)
    dates = [now - datetime.timedelta(minutes=15 * (100 - i)) for i in range(100)]
    
    highs, lows, closes, opens = [], [], [], []
    curr = base_price
    
    for i in range(100):
        o = curr
        if i == 85:  # Simulated Liquidity Sweep
            h, l, c = o + 8.50, o - 2.00, o + 7.00
        elif i == 88: # CHoCH & Imbalance
            h, l, c = o + 2.00, o - 12.00, o - 10.50
        else:
            h = o + (1.5 if i % 2 == 0 else -0.5)
            l = o - (1.5 if i % 2 != 0 else -0.5)
            c = (h + l) / 2.0
        
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        curr = c

    return MarketData(
        highs=highs, lows=lows, closes=closes, opens=opens,
        timestamps=dates, current_price=closes[-1], source="simulated_feed"
    )


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14, current_price: float = 0.0) -> float:
    if len(closes) < period + 1:
        return max(1.5, current_price * 0.001) if current_price > 0 else 2.5
    
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)
    
    atr = sum(tr_list[-period:]) / float(period)
    return max(atr, 1.2)

# ==============================================================================
# --- 🧩 SMART MONEY CONFLUENCE ENGINE ---
# ==============================================================================

def find_swing_points(highs: List[float], lows: List[float], window: int = 3) -> Tuple[List[int], List[int]]:
    swing_highs, swing_lows = [], []
    n = len(highs)
    for i in range(window, n - window):
        if all(highs[i] > highs[i - j] for j in range(1, window + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
            swing_highs.append(i)
            
        if all(lows[i] < lows[i - j] for j in range(1, window + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
            swing_lows.append(i)
            
    return swing_highs, swing_lows


def find_fvg(highs: List[float], lows: List[float], start_idx: int, direction: str, min_gap: float) -> Optional[Tuple[int, float, float]]:
    n = len(highs)
    for i in range(start_idx, n - 2):
        if direction == "bearish":
            gap = lows[i] - highs[i + 2]
            if gap >= min_gap:
                return (i + 1, lows[i], highs[i + 2])
        else:
            gap = lows[i + 2] - highs[i]
            if gap >= min_gap:
                return (i + 1, lows[i + 2], highs[i])
    return None


def detect_smart_money_structure(highs: List[float], lows: List[float], closes: List[float], opens: List[float], atr: float, current_price: float) -> Optional[Dict[str, Any]]:
    n = len(closes)
    if n < 30:
        return None

    swing_highs, swing_lows = find_swing_points(highs, lows, window=2)
    if not swing_highs or not swing_lows:
        return None

    # Bearish Sweep & Setup Detection
    recent_sh = [idx for idx in swing_highs if idx < n - 6]
    if recent_sh:
        target_sh = recent_sh[-1]
        prior_high = highs[target_sh]
        
        for i in range(target_sh + 1, n - 3):
            if highs[i] > prior_high and closes[i] < prior_high:
                sweep_idx = i
                sweep_high = highs[i]
                
                recent_sls = [idx for idx in swing_lows if idx > target_sh and idx < sweep_idx]
                choch_threshold = lows[recent_sls[-1]] if recent_sls else lows[sweep_idx - 1]
                
                for j in range(sweep_idx + 1, n):
                    if closes[j] < choch_threshold:
                        fvg = find_fvg(highs, lows, sweep_idx, "bearish", min_gap=atr * FVG_MIN_GAP_ATR_MULT)
                        
                        if fvg:
                            fvg_idx, fvg_top, fvg_bottom = fvg
                            entry = fvg_top if ENTRY_MODE == "EDGE" else (fvg_top + fvg_bottom) / 2.0
                        else:
                            entry = current_price

                        raw_sl = sweep_high + (atr * SL_BUFFER_ATR_MULT)
                        sl_points = min(MAX_SL_POINTS, max(MIN_SL_POINTS, raw_sl - entry))
                        sl = entry + sl_points
                        tp = entry - (sl_points * RR_MULTIPLE)

                        return {
                            "direction": "bearish",
                            "sweep_index": sweep_idx,
                            "sweep_level": sweep_high,
                            "prior_high": prior_high,
                            "choch_index": j,
                            "choch_level": choch_threshold,
                            "bos_index": None,
                            "fvg": fvg,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                        }

    # Bullish Sweep & Setup Detection
    recent_sl = [idx for idx in swing_lows if idx < n - 6]
    if recent_sl:
        target_sl = recent_sl[-1]
        prior_low = lows[target_sl]
        
        for i in range(target_sl + 1, n - 3):
            if lows[i] < prior_low and closes[i] > prior_low:
                sweep_idx = i
                sweep_low = lows[i]
                
                recent_shs = [idx for idx in swing_highs if idx > target_sl and idx < sweep_idx]
                choch_threshold = highs[recent_shs[-1]] if recent_shs else highs[sweep_idx - 1]
                
                for j in range(sweep_idx + 1, n):
                    if closes[j] > choch_threshold:
                        fvg = find_fvg(highs, lows, sweep_idx, "bullish", min_gap=atr * FVG_MIN_GAP_ATR_MULT)
                        
                        if fvg:
                            fvg_idx, fvg_top, fvg_bottom = fvg
                            entry = fvg_bottom if ENTRY_MODE == "EDGE" else (fvg_top + fvg_bottom) / 2.0
                        else:
                            entry = current_price

                        raw_sl = sweep_low - (atr * SL_BUFFER_ATR_MULT)
                        sl_points = min(MAX_SL_POINTS, max(MIN_SL_POINTS, entry - raw_sl))
                        sl = entry - sl_points
                        tp = entry + (sl_points * RR_MULTIPLE)

                        return {
                            "direction": "bullish",
                            "sweep_index": sweep_idx,
                            "sweep_level": sweep_low,
                            "prior_low": prior_low,
                            "choch_index": j,
                            "choch_level": choch_threshold,
                            "bos_index": None,
                            "fvg": fvg,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                        }

    return None

# ==============================================================================
# --- 📝 TEXT FORMATTING (BUG FIX APPLIED) ---
# ==============================================================================

def generate_reasoning(structure: Dict[str, Any], atr: float, current_price: float, source: str) -> str:
    direction = structure["direction"]
    parts = []

    if direction == "bearish":
        parts.append(
            f"Bearish liquidity swept at {structure['sweep_level']:.2f} "
            f"(prior swing high at {structure['prior_high']:.2f}), then reclaimed."
        )
        parts.append(
            f"CHoCH confirmed on candle close beyond {structure['choch_level']:.2f}."
        )
        if structure["fvg"]:
            _, fvg_top, fvg_bottom = structure["fvg"]
            parts.append(f"Entry aligned to boundary of FVG at {fvg_bottom:.2f}-{fvg_top:.2f}.")
    else:
        parts.append(
            f"Bullish liquidity swept at {structure['sweep_level']:.2f} "
            f"(prior swing low at {structure['prior_low']:.2f}), then reclaimed."
        )
        parts.append(
            f"CHoCH confirmed on candle close beyond {structure['choch_level']:.2f}."
        )
        if structure["fvg"]:
            _, fvg_top, fvg_bottom = structure["fvg"]
            parts.append(f"Entry aligned to boundary of FVG at {fvg_bottom:.2f}-{fvg_top:.2f}.")

    parts.append(f"ATR buffer calculated at {atr:.2f} pts.")
    return " ".join(parts)

# ==============================================================================
# --- 🎨 CHART RENDERING & TELEGRAM BROADCASTING ---
# ==============================================================================

def generate_candlestick_chart(highs: List[float], lows: List[float], closes: List[float], opens: List[float], structure: Dict[str, Any], current_price: float) -> bytes:
    with chart_lock:
        fig, ax = plt.subplots(figsize=(10, 5), dpi=130)
        fig.patch.set_facecolor("#0e1117")
        ax.set_facecolor("#0e1117")

        n = len(closes)
        indices = list(range(n))

        for i in indices:
            color = "#00c853" if closes[i] >= opens[i] else "#ff3d00"
            ax.plot([i, i], [lows[i], highs[i]], color=color, linewidth=1.0)
            ax.plot([i, i], [opens[i], closes[i]], color=color, linewidth=3.2)

        entry, sl, tp = structure["entry"], structure["sl"], structure["tp"]
        ax.axhline(entry, color="#29b6f6", linestyle="--", linewidth=1.2, label=f"Entry: {entry:.2f}")
        ax.axhline(sl, color="#ff1744", linestyle="-.", linewidth=1.2, label=f"SL: {sl:.2f}")
        ax.axhline(tp, color="#00e676", linestyle="-.", linewidth=1.2, label=f"TP: {tp:.2f}")

        ax.set_title(f"{SYMBOL_LABEL} SMC SNIPER BLUEPRINT", color="#ffffff", fontsize=12, pad=10)
        ax.tick_params(colors="#888888", labelsize=8)
        ax.grid(True, color="#1e222d", linestyle=":", alpha=0.6)
        ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#333333", labelcolor="#ffffff", fontsize=8)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()


def send_telegram_photo(photo_bytes: bytes, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.info("Telegram tokens unconfigured. Dispatch suppressed.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        files = {"photo": ("chart.png", photo_bytes, "image/png")}
        data = {"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "Markdown"}
        res = requests.post(url, data=data, files=files, timeout=10)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Telegram dispatch failure: {e}")
        return False

# ==============================================================================
# --- ⏱️ SESSION MANAGEMENT & CONTINUOUS SCHEDULER ---
# ==============================================================================

def is_in_trading_window(dt_utc: datetime.datetime) -> bool:
    start_time = datetime.time(SESSION_START_HOUR_UTC, SESSION_START_MIN_UTC)
    end_time = datetime.time(SESSION_END_HOUR_UTC, SESSION_END_MIN_UTC)
    return start_time <= dt_utc.time() <= end_time


def generate_or_get_daily_plan(forced: bool = False) -> Dict[str, Any]:
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today = now_utc.date()

    with state_lock:
        if engine_state.ledger.date == today and engine_state.ledger.status == "signal" and not forced:
            return engine_state.ledger.to_dict()

    if not is_in_trading_window(now_utc) and not forced:
        ledger = SniperLedger(
            date=today, status="standby",
            reasoning="Outside execution window (1:00 PM – 3:30 PM WAT). Engine standby."
        )
        with state_lock:
            engine_state.ledger = ledger
        return ledger.to_dict()

    if is_high_impact_news_near(buffer_minutes=15):
        ledger = SniperLedger(
            date=today, status="standby",
            reasoning="High-impact USD news release detected inside window. Execution paused."
        )
        with state_lock:
            engine_state.ledger = ledger
        return ledger.to_dict()

    try:
        market_data = fetch_market_data()
    except Exception as e:
        logger.critical(f"Data acquisition error: {e}")
        with state_lock:
            return engine_state.ledger.to_dict()

    window = min(100, len(market_data.closes))
    w_highs = market_data.highs[-window:]
    w_lows = market_data.lows[-window:]
    w_closes = market_data.closes[-window:]
    w_opens = market_data.opens[-window:]
    current_price = market_data.current_price
    source = market_data.source

    atr_value = calculate_atr(w_highs, w_lows, w_closes, current_price=current_price)
    structure = detect_smart_money_structure(w_highs, w_lows, w_closes, w_opens, atr_value, current_price)

    if structure is None:
        ledger = SniperLedger(
            date=today, status="hunting",
            reasoning="Continuous scanning active: searching for liquidity sweep + CHoCH...",
            data_source=source
        )
        with state_lock:
            engine_state.ledger = ledger
        return ledger.to_dict()

    action = "BUY LIMIT" if structure["direction"] == "bullish" else "SELL LIMIT"
    entry, sl, tp = structure["entry"], structure["sl"], structure["tp"]
    reasoning = generate_reasoning(structure, atr_value, current_price, source)

    ledger = SniperLedger(
        date=today, status="signal", action=action, entry=entry, sl=sl, tp=tp,
        reasoning=reasoning, choch_detected=True, liquidity_swept=True,
        bos_detected=structure["bos_index"] is not None,
        fvg_detected=bool(structure["fvg"]), data_source=source
    )

    with state_lock:
        engine_state.ledger = ledger

    chart_bytes = generate_candlestick_chart(w_highs, w_lows, w_closes, w_opens, structure, current_price)
    sl_points = abs(entry - sl)
    bos_line = "- **BOS Confirmation:** `Detected 🟢`\n" if structure["bos_index"] else "- **BOS Confirmation:** `Not yet formed 🔸`\n"
    fvg_line = "- **FVG:** `Present 🟢`\n\n" if structure["fvg"] else "- **FVG:** `None in range 🔸`\n\n"

    briefing = (
        f"🎯 **DEEP LIQUIDITY SNIPER BLUEPRINT** 🎯\n\n"
        f"• **Action:** **{action}**\n"
        f"• **Spot Reference:** `{current_price:.2f}`\n"
        f"• **Sniper Entry:** `{entry:.2f}`\n"
        f"• **Stop Loss ({sl_points:.1f} pts):** `{sl:.2f}`\n"
        f"• **Target TP (1:{RR_MULTIPLE:.1f} RR):** `{tp:.2f}`\n\n"
        f"⚡ **SMC Triggers:**\n"
        f"- **Liquidity Sweep:** `Active 🟢`\n"
        f"- **CHoCH Formation:** `Detected 🟢`\n"
        f"{bos_line}"
        f"{fvg_line}"
        f"🧠 **Structural Breakdown:**\n> \"{reasoning}\"\n\n"
        f"_Mechanically generated from {source.replace('_', ' ')} data._"
    )
    
    send_telegram_photo(chart_bytes, briefing)
    logger.info("Daily blueprint dispatched successfully.")
    return ledger.to_dict()


def daily_scheduler():
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            if is_in_trading_window(now):
                with state_lock:
                    already_signaled = (
                        engine_state.ledger.date == now.date() and 
                        engine_state.ledger.status == "signal"
                    )

                if not already_signaled:
                    generate_or_get_daily_plan(forced=False)

                time.sleep(180)
            else:
                time.sleep(120)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)

# ==============================================================================
# --- 🌐 ENDPOINTS & SERVER LAUNCH ---
# ==============================================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "engine": "Deep Liquidity Sniper Engine",
        "status": "online",
        "active_plan": generate_or_get_daily_plan(forced=False)
    })


@app.route("/status", methods=["GET"])
def status():
    with state_lock:
        return jsonify(engine_state.ledger.to_dict())


if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=daily_scheduler, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
                      
