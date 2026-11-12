"""
bybit_test.py - READ-ONLY connection test for your Bybit DEMO account.
It places no orders. It only reads the price, a few candles and your demo balance.
"""
import datetime

import ccxt

from bot_settings import BYBIT_API_KEY, BYBIT_API_SECRET

SYMBOL = "BTC/USDT:USDT"   # Bitcoin perpetual (USDT-settled)

exchange = ccxt.bybit({
    "apiKey": BYBIT_API_KEY.strip(),
    "secret": BYBIT_API_SECRET.strip(),
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
exchange.enable_demo_trading(True)   # DEMO money only - never touches real funds

try:
    print("Loading markets...")
    exchange.load_markets()

    ticker = exchange.fetch_ticker(SYMBOL)
    print(f"{SYMBOL} | bid {ticker['bid']} | ask {ticker['ask']} | last {ticker['last']}")

    print("Last 5 five-minute candles (time, open, high, low, close):")
    for c in exchange.fetch_ohlcv(SYMBOL, timeframe="5m", limit=5):
        t = datetime.datetime.fromtimestamp(c[0] / 1000, datetime.timezone.utc)
        print(t.strftime("%Y-%m-%d %H:%M"), c[1], c[2], c[3], c[4])

    balance = exchange.fetch_balance()
    usdt = balance.get("USDT", {})
    print("Demo USDT balance - total:", usdt.get("total"), "| free:", usdt.get("free"))
    print("\nSUCCESS: connected to Bybit DEMO.")
except ccxt.BaseError as e:
    print("\nFAILED:", type(e).__name__)
    print(str(e)[:400])
