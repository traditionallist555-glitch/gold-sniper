import os
import time
import logging
from datetime import datetime
import requests
import pandas as pd
import numpy as np

# =====================================================================
# 1. LOGGING & ENVIRONMENT CONFIGURATION
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trading_bot.log", mode="a")
    ]
)
logger = logging.getLogger("SignalBot")

# Telegram Settings (Use environment variables or fallbacks)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID_HERE")

# Trading Config
SYMBOL = "GC=F"       # Gold Spot/Futures ticker for Yahoo Finance (use 'XAUUSD=X' for Spot)
INTERVAL = "15m"      # Signal evaluation bar size (e.g., '15m', '1h', '1d')
LOOKBACK_RANGE = "2d" # Matches interval (e.g., '2d' for '15m', '60d' for '1h')
POLL_INTERVAL = 300   # Time in seconds between tick cycles (e.g., 5 minutes)

# =====================================================================
# 2. RAW YAHOO FINANCE DATA RETRIEVER
# =====================================================================
def fetch_market_data(symbol: str, interval: str, data_range: str) -> pd.DataFrame:
    """
    Fetches market data directly from the Yahoo Finance V8 Chart API.
    Replaces 'yfinance' to avoid random crashes, delisting errors, and heavy dependencies.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "range": data_range,
        "interval": interval,
        "includePrePost": "false"
    }
    # Emulate browser headers to bypass block guards
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        result = data.get("chart", {}).get("result", [])
        if not result:
            logger.error(f"No result field returned from Yahoo API for {symbol}.")
            return pd.DataFrame()

        chart_data = result[0]
        timestamps = chart_data.get("timestamp", [])
        indicators = chart_data.get("indicators", {}).get("quote", [{}])[0]
        
        opens = indicators.get("open", [])
        highs = indicators.get("high", [])
        lows = indicators.get("low", [])
        closes = indicators.get("close", [])
        volumes = indicators.get("volume", [])

        if not timestamps or not closes:
            logger.warning(f"No valid price points retrieved for {symbol}.")
            return pd.DataFrame()

        # Build DataFrame
        df = pd.DataFrame({
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes
        }, index=pd.to_datetime(timestamps, unit="s"))

        # Drop invalid rows and forward-fill occasional NaN values
        df = df.dropna(subset=["Close"]).ffill()
        return df

    except Exception as e:
        logger.error(f"Error executing raw API call for {symbol}: {e}")
        return pd.DataFrame()

# =====================================================================
# 3. ROBUST PURE-PYTHON INDICATOR IMPLEMENTATIONS
# =====================================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates moving averages, Relative Strength Index (RSI), and MACD 
    without relying on TA-Lib or external C wrappers.
    """
    df = df.copy()
    if len(df) < 30:
        return df  # Return unmodified if there is insufficient historical data

    # 1. Simple Moving Averages (SMA)
    df["SMA_20"] = df["Close"].rolling(window=20).mean()

    # 2. Exponential Moving Averages (EMA)
    df["EMA_12"] = df["Close"].ewm(span=12, adjust=False).mean()
    df["EMA_26"] = df["Close"].ewm(span=26, adjust=False).mean()

    # 3. MACD Calculation
    df["MACD"] = df["EMA_12"] - df["EMA_26"]
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]

    # 4. Relative Strength Index (RSI)
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()

    # Apply Wilder's smoothing technique for classic RSI values
    for i in range(14, len(df)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * 13 + gain.iloc[i]) / 14
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * 13 + loss.iloc[i]) / 14

    rs = avg_gain / (avg_loss + 1e-10)  # Avoid zero division errors
    df["RSI"] = 100 - (100 / (1 + rs))

    return df

# =====================================================================
# 4. TRADING ALGORITHM ENGINE
# =====================================================================
def evaluate_market_signals(df: pd.DataFrame) -> str:
    """
    Evaluates indicators on the latest completed candle.
    Returns: 'BUY', 'SELL', or 'HOLD'.
    """
    if len(df) < 2 or "RSI" not in df.columns or df["SMA_20"].isna().iloc[-2]:
        return "HOLD"

    # Evaluate the most recently completed, historical candle (-2 index)
    # This prevents 'repainting' and reacting to mid-candle price fluctuations.
    current_candle = df.iloc[-2]
    previous_candle = df.iloc[-3] if len(df) > 2 else current_candle

    close_val = current_candle["Close"]
    rsi_val = current_candle["RSI"]
    sma_val = current_candle["SMA_20"]
    macd_val = current_candle["MACD"]
    macd_sig = current_candle["MACD_Signal"]

    # Prev candle metrics for crossover checking
    prev_macd_val = previous_candle["MACD"]
    prev_macd_sig = previous_candle["MACD_Signal"]

    # Trigger Rules
    bullish_crossover = (prev_macd_val <= prev_macd_sig) and (macd_val > macd_sig)
    bearish_crossover = (prev_macd_val >= prev_macd_sig) and (macd_val < macd_sig)

    # Strategy 1: Bullish momentum check
    if bullish_crossover and rsi_val < 65 and close_val > sma_val:
        return "BUY"

    # Strategy 2: Bearish momentum check
    if bearish_crossover and rsi_val > 35 and close_val < sma_val:
        return "SELL"

    return "HOLD"

# =====================================================================
# 5. TELEGRAM INTEGRATION
# =====================================================================
def broadcast_telegram_signal(action: str, symbol: str, price: float, rsi: float) -> bool:
    """
    Dispatches styled transactional alerts to your Telegram API endpoint.
    """
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.warning("Telegram Bot Token is unconfigured. Alert broadcast skipped.")
        return False

    emoji = "🟢 [BUY ALERT]" if action == "BUY" else "🔴 [SELL ALERT]"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    message = (
        f"{emoji}\n"
        f"<b>Asset:</b> {symbol}\n"
        f"<b>Trigger Price:</b> ${price:,.2f}\n"
        f"<b>RSI:</b> {rsi:.2f}\n"
        f"<b>Timestamp:</b> {timestamp}\n"
    )

    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        res = requests.post(telegram_url, json=payload, timeout=10)
        if res.status_code == 200:
            logger.info(f"Signal alert dispatched to Telegram: {action}")
            return True
        else:
            logger.error(f"Failed to post to Telegram: {res.status_code} - {res.text}")
            return False
    except Exception as e:
        logger.error(f"Network error trying to contact Telegram API: {e}")
        return False

# =====================================================================
# 6. MONITORED SERVICE RUNTIME
# =====================================================================
def start_bot_engine():
    """
    Core long-running loop that polls the exchange, updates metrics,
    and publishes alerts when trends reverse.
    """
    logger.info("==========================================")
    logger.info(f"Trading Signal Engine Online | Target: {SYMBOL}")
    logger.info(f"Resolution: {INTERVAL} | Polling: Every {POLL_INTERVAL}s")
    logger.info("==========================================")

    last_processed_timestamp = None

    while True:
        try:
            # 1. Fetch
            df = fetch_market_data(SYMBOL, INTERVAL, LOOKBACK_RANGE)
            if df.empty:
                logger.warning("No data retrieved this loop. Retrying on next cycle...")
                time.sleep(15)
                continue

            # 2. Calculate Indicators
            df = calculate_indicators(df)

            # Check if we have received a new candle
            latest_candle_time = df.index[-2] # Target the closed bar
            if latest_candle_time == last_processed_timestamp:
                logger.debug("Active candle is unchanged. Waiting for next interval...")
                time.sleep(15)
                continue

            # 3. Analyze Trend & Indicators
            signal = evaluate_market_signals(df)
            close_price = df.iloc[-2]["Close"]
            rsi_value = df.iloc[-2]["RSI"]

            logger.info(
                f"Tick: Bar Time {latest_candle_time} | Price: ${close_price:,.2f} | "
                f"RSI: {rsi_value:.2f} | Position: {signal}"
            )

            # 4. Signal Action & Push
            if signal in ["BUY", "SELL"]:
                broadcast_telegram_signal(signal, SYMBOL, close_price, rsi_value)

            # Lock step so we only evaluate once per candle period
            last_processed_timestamp = latest_candle_time

        except KeyboardInterrupt:
            logger.info("User requested shutdown. Stopping bot...")
            break
        except Exception as err:
            logger.error(f"System Loop Exception occurred: {err}", exc_info=True)
            time.sleep(30) # Delay briefly after an unexpected failure to prevent rapid loops

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    start_bot_engine()
