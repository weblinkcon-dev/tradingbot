import MetaTrader5 as mt5
import pandas as pd
import sys

SYMBOL = "EURUSD"

if not mt5.initialize():
    print("Could not connect to MT5:", mt5.last_error())
    sys.exit()
mt5.symbol_select(SYMBOL, True)
rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 300)
mt5.shutdown()

df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s")
df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

delta = df["close"].diff()
gain = delta.clip(lower=0).rolling(14).mean()
loss = (-delta.clip(upper=0)).rolling(14).mean()
df["rsi"] = 100 - 100 / (1 + gain / loss)

tr = pd.concat([
    df["high"] - df["low"],
    (df["high"] - df["close"].shift()).abs(),
    (df["low"] - df["close"].shift()).abs(),
], axis=1).max(axis=1)
df["atr"] = tr.rolling(14).mean()

c = df.iloc[-2]  # last fully closed candle
uptrend = c["ema20"] > c["ema50"] and c["close"] > c["ema50"]
downtrend = c["ema20"] < c["ema50"] and c["close"] < c["ema50"]

signal = "NO TRADE"
if uptrend and c["rsi"] < 40:
    signal = "BUY"
elif downtrend and c["rsi"] > 60:
    signal = "SELL"

print("Candle time:", c["time"])
print("Close:", c["close"], "| EMA20:", round(c["ema20"], 5), "| EMA50:", round(c["ema50"], 5))
print("RSI:", round(c["rsi"], 1), "| ATR:", round(c["atr"], 5))
print("SIGNAL:", signal)
if signal != "NO TRADE":
    sign = 1 if signal == "BUY" else -1
    print("Stop-loss:", round(c["close"] - sign * 1.5 * c["atr"], 5))
    print("Take-profit:", round(c["close"] + sign * 2.0 * c["atr"], 5))