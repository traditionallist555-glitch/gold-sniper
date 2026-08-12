import datetime
import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
import matplotlib
import matplotlib.pyplot as plt
import requests

matplotlib.use("Agg")

# --- 🪵 LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
)
logger = logging.getLogger("SniperEngine")

# --- 🔒 THREAD SAFETY ---
state_lock = threading.Lock()
chart_lock = threading.Lock()

app = Flask(__name__)


# --- 📦 DATA STRUCTURES ---
@dataclass
class MarketData:
  highs: List[float]
  lows: List[float]
  closes: List[float]
  opens: List[float]
  current_price: float
  source: str
  timestamp: str


@dataclass
class SniperLedger:
  date: Optional[datetime.date] = None
  status: Optional[str] = None  # "signal" | "no_setup" | "data_unavailable" | "offline_data_only"
  action: Optional[str] = None
  entry: float = 0.0
  sl: float = 0.0
  tp: float = 0.0
  reasoning: str = ""
  choch_detected: bool = False
  liquidity_swept: bool = False
  bos_detected: bool = False
  fvg_detected: bool = False
  data_source: Optional[str] = None

  def to_dict(self) -> Dict[str, Any]:
    return {
        "date": str(self.date) if self.date else None,
        "status": self.status,
        "action": self.action,
        "entry": self.entry,
        "sl": self.sl,
        "tp": self.tp,
        "choch_detected": self.choch_detected,
        "liquidity_swept": self.liquidity_swept,
        "bos_detected": self.bos_detected,
        "fvg_detected": self.fvg_detected,
        "data_source": self.data_source,
    }


class EngineState:
  def __init__(self):
    self.ledger = SniperLedger()
    self.last_fetch: Dict[str, Any] = {"source": None, "price": None, "time": None}
    self.last_manual_refresh: float = 0.0


engine_state = EngineState()

# --- 🔑 ENVIRONMENT / CONFIG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get(
    "TELEGRAM_CHAT_ID"
)
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")
SYMBOL_LABEL = "XAUUSD"

TRIGGER_HOUR = 7        # UTC. 7:00 UTC = 8:00 AM WAT.
TRIGGER_MINUTE = 00

RR_MULTIPLE = 3.0
MIN_RR_MULTIPLE = 2.5  # reserved: only binding if you later target the next
                        # opposing liquidity pool instead of a fixed RR multiple.
STATUS_REFRESH_COOLDOWN_SECONDS = 20

SWING_LEFT = 2
SWING_RIGHT = 2
SL_BUFFER_ATR_MULT = 0.20
MIN_SL_ATR_MULT = 1.0
MAX_SL_ATR_MULT = 5.0
FVG_MIN_GAP_ATR_MULT = 0.15
STRUCTURE_LOOKBACK = 60

LIVE_SOURCES = {"mt5_live_bridge", "alphavantage_live_feed"}


# --- 🌐 FLASK ---
@app.route("/")
def home():
  return "🔥CLIMAXSongz🔥 Deep Liquidity Sniper Engine is active!", 200


@app.route("/status")
def status():
  refresh_note = None
  with state_lock:
    manual_refresh_time = engine_state.last_manual_refresh

  if request.args.get("refresh") == "1":
    now_ts = time.time()
    elapsed = now_ts - manual_refresh_time
    if elapsed >= STATUS_REFRESH_COOLDOWN_SECONDS:
      try:
        fetch_market_data()
        with state_lock:
          engine_state.last_manual_refresh = now_ts
        refresh_note = "refreshed"
      except Exception as e:
        logger.error(f"Manual refresh failed: {e}")
        refresh_note = f"refresh failed: {e}"
    else:
      refresh_note = (
          f"cooldown active, {STATUS_REFRESH_COOLDOWN_SECONDS - elapsed:.0f}s"
          " left - showing last fetched value"
      )

  with state_lock:
    ledger_dict = engine_state.ledger.to_dict()
    last_fetch = dict(engine_state.last_fetch)

  return jsonify({
      "todays_plan": ledger_dict,
      "last_price_fetch": last_fetch,
      "refresh": refresh_note,
  })


def run_health_server():
  port = int(os.environ.get("PORT", 8000))
  app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# --- 🛰️ LIVE MARKET DATA ---


def generate_offline_demo_series(current_price=4071.12, length=100, seed=42):
  """Deterministic synthetic OHLC. Exists ONLY so the app has something to
  render if you run it locally with no feeds configured. It is NOT real
  market data -- `generate_or_get_daily_plan` checks `source` against
  LIVE_SOURCES and will never post a trade signal built on this."""
  import random
  rng = random.Random(seed)
  opens, highs, lows, closes = [], [], [], []
  prev_close = current_price - 16.0
  for _ in range(length):
    op = prev_close
    change = rng.uniform(-1.5, 2.0)
    cl = op + change
    hi = max(op, cl) + rng.uniform(0.1, 1.0)
    lo = min(op, cl) - rng.uniform(0.1, 1.0)
    opens.append(round(op, 2))
    highs.append(round(hi, 2))
    lows.append(round(lo, 2))
    closes.append(round(cl, 2))
    prev_close = cl
  closes[-1] = current_price
  highs[-1] = max(opens[-1], current_price) + 0.8
  lows[-1] = min(opens[-1], current_price) - 0.8
  return highs, lows, closes, opens


def fetch_market_data() -> MarketData:
  """Tries the MT5 bridge, then Alpha Vantage, then an offline demo series.
  Only 'mt5_live_bridge' and 'alphavantage_live_feed' are LIVE_SOURCES --
  callers must never treat anything else as tradeable. Real OHLC history
  in, real OHLC history out -- nothing here is reconstructed from a single
  spot price."""
  timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

  mt5_bridge_url = os.environ.get("MT5_BRIDGE_URL")
  if mt5_bridge_url:
    try:
      res = requests.get(mt5_bridge_url, timeout=5).json()
      if "price" in res and "highs" in res:
        current_price = float(res["price"])
        highs = [float(x) for x in res["highs"]]
        lows = [float(x) for x in res["lows"]]
        closes = [float(x) for x in res["closes"]]
        opens = (
            [float(x) for x in res["opens"]]
            if "opens" in res
            else [closes[max(0, i - 1)] for i in range(len(closes))]
        )
        source = "mt5_live_bridge"
        data = MarketData(highs, lows, closes, opens, current_price, source, timestamp)
        with state_lock:
          engine_state.last_fetch = {"source": source, "price": current_price, "time": timestamp}
        return data
    except Exception as e:
      logger.warning(f"MT5 bridge sync error: {e}")

  try:
    av_url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_INTRADAY", "from_symbol": "XAU", "to_symbol": "USD",
        "interval": "15min", "apikey": ALPHA_VANTAGE_KEY,
    }
    av_res = requests.get(av_url, params=params, timeout=10).json()
    time_series = av_res.get("Time Series FX (15min)", {})
    if time_series:
      sorted_times = sorted(time_series.keys(), reverse=True)
      closes, highs, lows, opens = [], [], [], []
      for t in sorted_times[:100]:
        candle = time_series[t]
        opens.append(float(candle["1. open"]))
        highs.append(float(candle["2. high"]))
        lows.append(float(candle["3. low"]))
        closes.append(float(candle["4. close"]))
      opens.reverse(); highs.reverse(); lows.reverse(); closes.reverse()
      current_price = closes[-1]
      source = "alphavantage_live_feed"
      data = MarketData(highs, lows, closes, opens, current_price, source, timestamp)
      with state_lock:
        engine_state.last_fetch = {"source": source, "price": current_price, "time": timestamp}
      return data
  except Exception as e:
    logger.warning(f"Alpha Vantage feed error: {e}")

  current_price = 4071.12
  highs, lows, closes, opens = generate_offline_demo_series(current_price)
  source = "offline_demo_fallback"
  data = MarketData(highs, lows, closes, opens, current_price, source, timestamp)
  with state_lock:
    engine_state.last_fetch = {"source": source, "price": current_price, "time": timestamp}
  return data


def calculate_atr(highs, lows, closes, period=14, current_price=None):
  if len(closes) < period + 1:
    if current_price:
      return round(current_price * 0.0015, 2)
    return 3.0
  tr_list = []
  for i in range(1, len(closes)):
    high_low = highs[i] - lows[i]
    high_close = abs(highs[i] - closes[i - 1])
    low_close = abs(lows[i] - closes[i - 1])
    tr_list.append(max(high_low, high_close, low_close))
  atr = sum(tr_list[-period:]) / period
  return round(max(atr, 0.01), 2)


def fetch_macro_news():
  """Placeholder -- not wired to a real news/economic-calendar API. Its
  output is intentionally NOT used in the posted signal, since it can't be
  verified. Kept here in case you want to wire up a real feed later."""
  return "Gold institutional liquidity sweep verified; macro calendar stable.", "Bullish", +0.5


# --- 🧠 SMC STRUCTURE DETECTION ENGINE ---
# Candle-by-candle, off real open/high/low/close: swing points -> liquidity
# sweep (wick beyond an INTACT prior swing level, close back past it) ->
# CHoCH (close beyond the prior opposite swing) -> optional BOS (a further
# break of a NEW swing that forms after CHoCH) -> FVG / order-block entry.


def find_swing_points(highs, lows, left=SWING_LEFT, right=SWING_RIGHT):
  swing_highs, swing_lows = [], []
  n = len(highs)
  for i in range(left, n - right):
    h_window = highs[i - left:i + right + 1]
    l_window = lows[i - left:i + right + 1]
    if highs[i] == max(h_window):
      swing_highs.append((i, highs[i]))
    if lows[i] == min(l_window):
      swing_lows.append((i, lows[i]))
  return swing_highs, swing_lows


def find_fvg(highs, lows, start_idx, end_idx, direction):
  """Returns the gap CLOSEST to start_idx (the displacement right off the
  sweep -- the real sniper-entry imbalance), as (mid_index, top, bottom)."""
  lo = max(start_idx, 1)
  hi = min(end_idx, len(highs) - 2)
  for i in range(lo, hi + 1):
    if direction == "bullish":
      if lows[i + 1] > highs[i - 1]:
        return (i, lows[i + 1], highs[i - 1])
    else:
      if highs[i + 1] < lows[i - 1]:
        return (i, highs[i - 1], lows[i + 1])
  return None


def find_order_block(opens, closes, start_idx, end_idx, direction):
  for i in range(end_idx, start_idx - 1, -1):
    if direction == "bullish" and closes[i] < opens[i]:
      return i
    if direction == "bearish" and closes[i] > opens[i]:
      return i
  return None


def _level_still_intact(closes, swept_idx, sweep_j, swept_price, direction):
  """A level is real, untapped liquidity only if price hasn't already
  CLOSED beyond it since it formed."""
  for k in range(swept_idx + 1, sweep_j):
    if direction == "bullish" and closes[k] < swept_price:
      return False
    if direction == "bearish" and closes[k] > swept_price:
      return False
  return True


def scan_direction(highs, lows, closes, opens, swing_highs, swing_lows,
                    atr_value, current_price, direction):
  n = len(closes)
  start = max(0, n - STRUCTURE_LOOKBACK)

  if direction == "bullish":
    reference_swings = [s for s in swing_lows if s[0] >= start]
    opposite_swings = swing_highs
  else:
    reference_swings = [s for s in swing_highs if s[0] >= start]
    opposite_swings = swing_lows

  if not reference_swings:
    return None

  sweep = None
  for swept_idx, swept_price in reference_swings:
    for j in range(swept_idx + 1, n):
      if direction == "bullish":
        pierced = lows[j] < swept_price
        reclaimed = closes[j] > swept_price
      else:
        pierced = highs[j] > swept_price
        reclaimed = closes[j] < swept_price
      if pierced and reclaimed and _level_still_intact(closes, swept_idx, j, swept_price, direction):
        wick = lows[j] if direction == "bullish" else highs[j]
        key = (j, swept_idx)
        if sweep is None or key > (sweep["sweep_index"], sweep["swept_level_index"]):
          sweep = {
              "swept_level_index": swept_idx, "swept_level_price": swept_price,
              "sweep_index": j, "sweep_wick_price": wick,
          }
  if sweep is None:
    return None
  sweep_idx = sweep["sweep_index"]

  prior_opposite = [s for s in opposite_swings if s[0] < sweep_idx]
  if not prior_opposite:
    return None
  choch_level_idx, choch_level_price = prior_opposite[-1]

  choch_index = None
  for j in range(sweep_idx + 1, n):
    if direction == "bullish" and closes[j] > choch_level_price:
      choch_index = j
      break
    if direction == "bearish" and closes[j] < choch_level_price:
      choch_index = j
      break
  if choch_index is None:
    return None

  bos_index = None
  bos_level = None
  post_choch_swings = [s for s in opposite_swings if s[0] > choch_index]
  if post_choch_swings:
    target_idx, target_price = post_choch_swings[0]
    for j in range(target_idx + 1, n):
      if direction == "bullish" and closes[j] > target_price:
        bos_index, bos_level = j, target_price
        break
      if direction == "bearish" and closes[j] < target_price:
        bos_index, bos_level = j, target_price
        break

  ob_index = find_order_block(opens, closes, sweep_idx, choch_index, direction)
  fvg = find_fvg(highs, lows, sweep_idx, choch_index, direction)
  if fvg and (fvg[1] - fvg[2]) < FVG_MIN_GAP_ATR_MULT * atr_value:
    fvg = None

  if fvg:
    _, fvg_top, fvg_bottom = fvg
    entry = (fvg_top + fvg_bottom) / 2
  elif ob_index is not None:
    entry = (opens[ob_index] + closes[ob_index]) / 2
  else:
    entry = sweep["sweep_wick_price"] + 0.5 * (choch_level_price - sweep["sweep_wick_price"])

  if direction == "bullish" and entry >= current_price:
    return None
  if direction == "bearish" and entry <= current_price:
    return None

  buffer = SL_BUFFER_ATR_MULT * atr_value
  sl = sweep["sweep_wick_price"] - buffer if direction == "bullish" else sweep["sweep_wick_price"] + buffer
  sl_distance = abs(entry - sl)
  if sl_distance < MIN_SL_ATR_MULT * atr_value or sl_distance > MAX_SL_ATR_MULT * atr_value:
    return None

  tp = entry + RR_MULTIPLE * sl_distance if direction == "bullish" else entry - RR_MULTIPLE * sl_distance

  return {
      "direction": direction, "sweep_index": sweep_idx,
      "sweep_level_index": sweep["swept_level_index"], "sweep_level_price": sweep["swept_level_price"],
      "sweep_wick_price": sweep["sweep_wick_price"], "choch_level_index": choch_level_idx,
      "choch_level_price": choch_level_price, "choch_index": choch_index,
      "bos_index": bos_index, "bos_level": bos_level, "order_block_index": ob_index, "fvg": fvg,
      "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "sl_distance": round(sl_distance, 2),
  }


def detect_smart_money_structure(highs, lows, closes, opens, atr_value, current_price):
  """Full scan, both directions. Returns the most-recently-confirmed setup,
  or None if nothing qualifies -- expected on plenty of days, not a bug."""
  atr_value = max(atr_value, 0.01)
  swing_highs, swing_lows = find_swing_points(highs, lows)
  bullish = scan_direction(highs, lows, closes, opens, swing_highs, swing_lows, atr_value, current_price, "bullish")
  bearish = scan_direction(highs, lows, closes, opens, swing_highs, swing_lows, atr_value, current_price, "bearish")
  candidates = [c for c in (bullish, bearish) if c is not None]
  if not candidates:
    return None
  candidates.sort(key=lambda c: c["choch_index"])
  return candidates[-1]

# --- 📊 CHART GENERATOR (thread-safe; sizes itself off the real data length) ---


def generate_candlestick_chart(highs, lows, closes, opens, structure, current_price):
  with chart_lock:
    n = len(closes)
    h, l, c, o = highs, lows, closes, opens

    fig, ax = plt.subplots(figsize=(12, 6), facecolor="#f3efe6")
    ax.set_facecolor("#f3efe6")

    for i in range(n):
      is_bullish = c[i] >= o[i]
      color = "#0f9d58" if is_bullish else "#db4437"
      ax.plot([i, i], [l[i], h[i]], color=color, linewidth=1.1, zorder=2)
      body_bottom = min(o[i], c[i])
      body_height = max(abs(c[i] - o[i]), 0.05)
      ax.add_patch(plt.Rectangle((i - 0.38, body_bottom), 0.76, body_height,
                                  facecolor=color, edgecolor=color, zorder=3))

    if structure is None:
      ax.set_title(f"CLIMAXSongz {SYMBOL_LABEL} M15 — Daily Scan (No Confirmed Setup)",
                    color="#2c3e50", fontsize=10, fontweight="bold", pad=8)
      ax.tick_params(colors="#7f8c8d", labelsize=8)
      ax.grid(True, color="#e5ddd0", linestyle="--", alpha=0.6, zorder=0)
      ax.set_xlim(-2, n + 5)
      pad = (max(h) - min(l)) * 0.1 + 1
      ax.set_ylim(min(l) - pad, max(h) + pad)
      for spine in ax.spines.values():
        spine.set_color("#d5ccc0")
      plt.tight_layout()
      buf = io.BytesIO()
      plt.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
      buf.seek(0)
      plt.close(fig)
      return buf

    direction = structure["direction"]
    is_bull = direction == "bullish"
    action = "BUY LIMIT" if is_bull else "SELL LIMIT"
    entry, sl, tp = structure["entry"], structure["sl"], structure["tp"]
    sweep_idx = structure["sweep_index"]
    choch_index = structure["choch_index"]
    bos_index = structure["bos_index"]
    ob_index = structure["order_block_index"]
    fvg = structure["fvg"]

    if is_bull:
      ax.axhspan(entry, tp, xmin=0.55, xmax=0.92, facecolor="#0f9d58", alpha=0.25, zorder=1)
      ax.axhspan(sl, entry, xmin=0.55, xmax=0.92, facecolor="#db4437", alpha=0.2, zorder=1)
    else:
      ax.axhspan(tp, entry, xmin=0.55, xmax=0.92, facecolor="#0f9d58", alpha=0.25, zorder=1)
      ax.axhspan(entry, sl, xmin=0.55, xmax=0.92, facecolor="#db4437", alpha=0.2, zorder=1)

    box_start = min(sweep_idx, ob_index) if ob_index is not None else sweep_idx
    box_end = max(choch_index, sweep_idx + 1)
    box_low = min(l[box_start:box_end + 1]) - 0.4
    box_high = max(h[box_start:box_end + 1]) + 0.4
    ax.add_patch(plt.Rectangle((box_start, box_low), box_end - box_start, box_high - box_low,
                                facecolor="#95a5a6", edgecolor="#34495e", linestyle="--",
                                linewidth=1.2, alpha=0.35, zorder=3))

    if fvg:
      fvg_idx, fvg_top, fvg_bottom = fvg
      fvg_width = max(box_end - fvg_idx + 4, 3)
      ax.add_patch(plt.Rectangle((fvg_idx - 1, fvg_bottom), fvg_width, fvg_top - fvg_bottom,
                                  facecolor="#8e44ad", edgecolor="none", alpha=0.3, zorder=2))
      ax.text(fvg_idx - 1, fvg_top + 0.15, "FVG", color="#8e44ad", fontsize=8, fontweight="bold")

    ax.axhline(y=entry, color="#3498db", linestyle="--", linewidth=1.3, zorder=4)
    ax.axhline(y=sl, color="#c0392b", linestyle="-", linewidth=1.2, zorder=4)
    ax.axhline(y=tp, color="#27ae60", linestyle="-", linewidth=1.2, zorder=4)

    label_x = n * 0.93
    def price_tag(y, text, bg):
      ax.text(label_x, y, text, color="#ffffff", fontsize=9, fontweight="bold",
              va="center", ha="left", bbox=dict(boxstyle="square,pad=0.3", facecolor=bg, edgecolor="none"))
    price_tag(entry, f"Entry: {entry:.2f}", "#2980b9")
    price_tag(sl, f"Stop Loss: {sl:.2f}", "#c0392b")
    price_tag(tp, f"Take Profit: {tp:.2f}", "#27ae60")

    sweep_y = l[sweep_idx] if is_bull else h[sweep_idx]
    ax.annotate("Liquidity Sweep", xy=(sweep_idx, sweep_y),
                xytext=(sweep_idx, sweep_y + (-3.0 if is_bull else 3.0)),
                arrowprops=dict(facecolor="#7f8c8d", shrink=0.05, width=1, headwidth=5),
                fontsize=8, fontweight="bold", color="#7f8c8d", ha="center")

    choch_y = c[choch_index]
    ax.annotate("CHoCH", xy=(choch_index, choch_y),
                xytext=(choch_index, choch_y + (2.6 if is_bull else -2.6)),
                arrowprops=dict(facecolor="#2980b9", shrink=0.05, width=1, headwidth=5),
                fontsize=8, fontweight="bold", color="#2980b9", ha="center")

    if bos_index is not None:
      bos_y = c[bos_index]
      ax.annotate("BOS", xy=(bos_index, bos_y),
                  xytext=(bos_index, bos_y + (2.6 if is_bull else -2.6)),
                  arrowprops=dict(facecolor="#2c3e50", shrink=0.05, width=1, headwidth=5),
                  fontsize=8, fontweight="bold", color="#2c3e50", ha="center")

    arrow_end_x = min(n + 5, (bos_index if bos_index is not None else choch_index) + 6)
    ax.annotate("", xy=(arrow_end_x, tp - (1.5 if is_bull else -1.5)),
                xytext=(choch_index, entry),
                arrowprops=dict(arrowstyle="->", color="#e74c3c", lw=1.8,
                                 connectionstyle=f"arc3,rad={0.35 if is_bull else -0.35}"),
                zorder=5)

    ax.text(0, max(h) + 1.5, "Gold", color="#2c3e50", fontsize=9, fontweight="bold", ha="left")
    ax.text(n, max(h) + 1.5, f"{tp:.2f}", color="#2c3e50", fontsize=9, fontweight="bold", ha="right")

    ax.set_title(f"CLIMAXSongz {SYMBOL_LABEL} M15 — SNIPER SETUP ({action})",
                 color="#2c3e50", fontsize=10, fontweight="bold", pad=8)
    ax.tick_params(colors="#7f8c8d", labelsize=8)
    ax.grid(True, color="#e5ddd0", linestyle="--", alpha=0.6, zorder=0)
    ax.set_xlim(-2, n + 14)
    min_p = min(min(l), sl) - 2
    max_p = max(max(h), tp) + 3
    ax.set_ylim(min_p, max_p)
    for spine in ax.spines.values():
      spine.set_color("#d5ccc0")

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    buf.seek(0)
    plt.close(fig)
    plt.clf()
    return buf


# --- 📱 TELEGRAM ---


def send_telegram_message(text):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  data = {"chat_id": TELEGRAM_CHANNEL_ID, "text": text, "parse_mode": "Markdown"}
  try:
    requests.post(url, data=data, timeout=10)
  except Exception as e:
    logger.error(f"Telegram message error: {e}")


def send_telegram_photo(img_buffer, caption):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
  files = {"photo": ("chart.png", img_buffer, "image/png")}
  data = {"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "Markdown"}
  try:
    requests.post(url, files=files, data=data, timeout=15)
  except Exception as e:
    logger.error(f"Telegram photo error: {e}")


# --- 🧠 REASONING (built from the actual detected structure) ---


def generate_reasoning(structure, atr_value, current_price, source):
  direction = structure["direction"]
  bias_word = "Bullish" if direction == "bullish" else "Bearish"
  ref_word = "swing low" if direction == "bullish" else "swing high"
  parts = [
      f"{bias_word} liquidity swept at {structure['sweep_wick_price']:.2f}"
      f" (prior {ref_word} at {structure['sweep_level_price']:.2f}), then reclaimed."
  ]
  parts.append(f"CHoCH confirmed on a close beyond {structure['choch_level_price']:.2f}.")
  if structure["bos_index"] is not None:
    parts.append(f"BOS confirmed beyond {structure['bos_level']:.2f}.")
  if structure["fvg"]:
    _, fvg_top, fvg_bottom = structure["fvg"]
    parts.append(f"Entry aligned to the open FVG at {fvg_bottom:.2f}-{fvg_top:.2f}.")
  elif structure["order_block_index"] is not None:
    parts.append("Entry aligned to the last opposing order block before the impulse leg.")
  parts.append(f"ATR(14) {atr_value:.2f}; source: {source.replace('_', ' ')}.")
  return " ".join(parts)


# --- 🎯 DAILY PLAN ---


def generate_or_get_daily_plan(forced: bool = False) -> Dict[str, Any]:
  today = datetime.datetime.now(datetime.timezone.utc).date()

  with state_lock:
    if engine_state.ledger.date == today and not forced:
      return engine_state.ledger.to_dict()

  logger.info("Running institutional SMC scan...")
  try:
    market_data = fetch_market_data()
  except Exception as e:
    logger.critical(f"Fetch failed: {e}")
    with state_lock:
      return engine_state.ledger.to_dict()

  if not market_data.closes or market_data.current_price == 0:
    ledger = SniperLedger(date=today, status="data_unavailable",
                           reasoning="No price data returned from any source.",
                           data_source=market_data.source)
    with state_lock:
      engine_state.ledger = ledger
    if forced:
      send_telegram_message(
          "⚠️ *Market data unavailable* — MT5 bridge and Alpha Vantage both "
          "failed to return data. No scan run today."
      )
    return ledger.to_dict()

  window = min(100, len(market_data.closes))
  w_highs = market_data.highs[-window:]
  w_lows = market_data.lows[-window:]
  w_closes = market_data.closes[-window:]
  w_opens = (
      market_data.opens[-window:] if len(market_data.opens) >= window
      else [w_closes[max(0, i - 1)] for i in range(window)]
  )
  current_price = market_data.current_price
  source = market_data.source

  atr_value = calculate_atr(w_highs, w_lows, w_closes, current_price=current_price)

  if source not in LIVE_SOURCES:
    ledger = SniperLedger(date=today, status="offline_data_only",
                           reasoning="Only offline demo data was available; no live signal generated.",
                           data_source=source)
    with state_lock:
      engine_state.ledger = ledger
    if forced:
      send_telegram_message(
          "⚠️ *Live feed unreachable* (MT5 bridge / Alpha Vantage) — skipping "
          "today's signal rather than posting on non-live data."
      )
    return ledger.to_dict()

  structure = detect_smart_money_structure(w_highs, w_lows, w_closes, w_opens, atr_value, current_price)

  if structure is None:
    ledger = SniperLedger(date=today, status="no_setup",
                           reasoning="Scan complete: no liquidity sweep + confirmed CHoCH in current structure.",
                           data_source=source)
    with state_lock:
      engine_state.ledger = ledger
    if forced:
      chart_bytes = generate_candlestick_chart(w_highs, w_lows, w_closes, w_opens, None, current_price)
      send_telegram_photo(
          chart_bytes,
          "🔎 *Daily SMC Scan Complete*\n\nNo confirmed liquidity sweep + CHoCH "
          f"setup on {SYMBOL_LABEL} M15 today. Standing by for the next scan."
      )
    return ledger.to_dict()

  action = "BUY LIMIT" if structure["direction"] == "bullish" else "SELL LIMIT"
  entry, sl, tp = structure["entry"], structure["sl"], structure["tp"]
  reasoning = generate_reasoning(structure, atr_value, current_price, source)

  ledger = SniperLedger(
      date=today, status="signal", action=action, entry=entry, sl=sl, tp=tp,
      reasoning=reasoning, choch_detected=True, liquidity_swept=True,
      bos_detected=structure["bos_index"] is not None,
      fvg_detected=bool(structure["fvg"]), data_source=source,
  )
  with state_lock:
    engine_state.ledger = ledger

  if forced:
    chart_bytes = generate_candlestick_chart(w_highs, w_lows, w_closes, w_opens, structure, current_price)
    sl_points = abs(entry - sl)
    bos_line = ("- **BOS Confirmation:** `Detected 🟢`\n" if structure["bos_index"] is not None
                else "- **BOS Confirmation:** `Not yet formed 🔸`\n")
    fvg_line = ("- **FVG:** `Present 🟢`\n\n" if structure["fvg"]
                else "- **FVG:** `None in range 🔸`\n\n")
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
        f"_Mechanically generated from {source.replace('_', ' ')} data at scan "
        f"time — confirm against your own analysis before trading._"
    )
    send_telegram_photo(chart_bytes, briefing)

  return ledger.to_dict()


def should_trigger_now(now, already_triggered_date, trigger_hour=TRIGGER_HOUR,
                        trigger_minute=TRIGGER_MINUTE):
  """Pure, unit-testable trigger check -- kept separate from the loop on
  purpose so it can be verified directly instead of by eye. (This exact
  spot broke twice already: a line-broken token crashed the original, then
  a bad indent made the check unreachable in the next version.)"""
  return (
      now.hour == trigger_hour
      and now.minute >= trigger_minute
      and already_triggered_date != now.date()
  )


def daily_scheduler():
  already_triggered_date = None
  while True:
    try:
      now = datetime.datetime.now(datetime.timezone.utc)
      if should_trigger_now(now, already_triggered_date):
        generate_or_get_daily_plan(forced=True)
        already_triggered_date = now.date()
        time.sleep(60)
      else:
        time.sleep(300)
    except Exception as e:
      logger.error(f"Scheduler error: {e}")
      time.sleep(60)


def main():
  logger.info("🚀 CLIMAXSongz Sniper Daemon initializing...")

  health_thread = threading.Thread(target=run_health_server, name="HealthServerThread", daemon=True)
  scheduler_thread = threading.Thread(target=daily_scheduler, name="SchedulerThread", daemon=True)
  health_thread.start()
  scheduler_thread.start()

  try:
    while True:
      if not health_thread.is_alive():
        logger.critical("Health server thread crashed. Restarting...")
        health_thread = threading.Thread(target=run_health_server, name="HealthServerThread", daemon=True)
        health_thread.start()

      if not scheduler_thread.is_alive():
        logger.critical("Scheduler thread crashed. Restarting...")
        scheduler_thread = threading.Thread(target=daily_scheduler, name="SchedulerThread", daemon=True)
        scheduler_thread.start()

      time.sleep(30)
  except KeyboardInterrupt:
    logger.info("Graceful shutdown.")


if __name__ == "__main__":
  main()
    
