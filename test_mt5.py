import datetime
import MetaTrader5 as mt5

if not mt5.initialize():
    print("Could not connect to MT5:", mt5.last_error())
    quit()

info = mt5.account_info()
print("Account:", info.login, "| Server:", info.server)
print("Balance:", info.balance, "| Demo account:", info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO)

mt5.symbol_select("EURUSD", True)
tick = mt5.symbol_info_tick("EURUSD")
print("EURUSD bid:", tick.bid, "ask:", tick.ask)

rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_M5, 0, 5)
print("Last 5 five-minute candles (time, open, high, low, close):")
for r in rates:
    print(datetime.datetime.fromtimestamp(r["time"]), r["open"], r["high"], r["low"], r["close"])

mt5.shutdown()