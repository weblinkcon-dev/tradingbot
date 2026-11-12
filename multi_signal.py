import time
import MetaTrader5 as mt5
import pandas as pd
import requests

try:
    from bot_settings import TELEGRAM_TOKEN, CHAT_ID
except ImportError:
    TELEGRAM_TOKEN = CHAT_ID = None

# Change these names if your broker uses different ones
WANTED = {
    "GOLD":     ["XAUUSD", "GOLD"],
    "BITCOIN":  ["BTCUSD"],
    "ETHEREUM": ["ETHUSD"],
}
TIMEFRAME = mt5.TIMEFRAME_M5
CHECK_SECONDS = 300  # check every 5 minutes


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def find_symbol(names):
    for n in names:
        if mt5.symbol_info(n) is not None:
            mt5.symbol_select(n, True)
            return n
    found = mt5.symbols_get(f"*{names[0]}*")
    if found:
        mt5.symbol_select(found[0].name, True)
        return found[0].name
    return None


def analyse(symbol):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 300)
    if rates is None or len(rates) < 100:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + avg_gain / avg_loss)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["vol_avg"] = df["tick_volume"].rolling(20).mean()

    c = df.iloc[-2]  # last fully closed candle
    uptrend = c["ema20"] > c["ema50"] and c["close"] > c["ema50"]
    downtrend = c["ema20"] < c["ema50"] and c["close"] < c["ema50"]

    vol_ok = c["tick_volume"] > c["vol_avg"]
    signal = "NO TRADE"
    if uptrend and c["rsi"] < 45 and c["rsi"] > 25 and vol_ok:
        signal = "BUY"
    elif downtrend and c["rsi"] > 55 and c["rsi"] < 75 and vol_ok:
        signal = "SELL"

    result = {"signal": signal, "time": c["time"], "close": c["close"],
              "rsi": round(c["rsi"], 1), "atr": c["atr"], "sl": None, "tp": None}
    if signal != "NO TRADE":
        sign = 1 if signal == "BUY" else -1
        result["sl"] = round(c["close"] - sign * 1.5 * c["atr"], 5)
        result["tp"] = round(c["close"] + sign * 2.0 * c["atr"], 5)
    return result


if not mt5.initialize():
    print("Could not connect to MT5:", mt5.last_error())
    quit()

symbols = {}
for label, names in WANTED.items():
    s = find_symbol(names)
    if s:
        symbols[label] = s
        print(f"{label}: using symbol {s}")
    else:
        print(f"{label}: symbol not found, skipping")

if not symbols:
    print("No symbols found. Check names in Market Watch.")
    mt5.shutdown()
    quit()

send_telegram("Signal watcher started (DEMO, signals only): " + ", ".join(symbols.values()))
last_signal = {}
print("Running. Press Ctrl+C in this window to stop.\n")

try:
    while True:
        for label, sym in symbols.items():
            r = analyse(sym)
            if r is None:
                print(f"{sym}: no data")
                continue
            spread = mt5.symbol_info(sym).spread
            print(f"{r['time']} | {sym} | close {r['close']} | RSI {r['rsi']} | spread {spread} pts | {r['signal']}")
            if r["signal"] != "NO TRADE" and last_signal.get(sym) != r["signal"]:
                msg = (f"{r['signal']} signal on {sym} (demo, no order placed)\n"
                       f"Price: {r['close']}\nStop-loss: {r['sl']}\nTake-profit: {r['tp']}")
                print(msg)
                send_telegram(msg)
            last_signal[sym] = r["signal"]
        print("-" * 40)
        time.sleep(CHECK_SECONDS)
except KeyboardInterrupt:
    print("Stopped by you.")
finally:
    send_telegram("Signal watcher stopped.")
    mt5.shutdown()