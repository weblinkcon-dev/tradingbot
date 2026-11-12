"""
trade_test.py - a menu to try MT5 order types by hand (demo by default).
Stop-loss and take-profit are set automatically as a % of the price, so it works for gold and forex.
"""
import argparse

import MetaTrader5 as mt5
import mt5_trader as t

SL_PCT = 0.30     # stop-loss distance, % of price
TP_PCT = 0.45     # take-profit distance, % of price
PEND_PCT = 0.15   # how far from the current price pending orders are placed, %


def levels(side, price):
    if side == "BUY":
        return price * (1 - SL_PCT / 100), price * (1 + TP_PCT / 100)
    return price * (1 + SL_PCT / 100), price * (1 - TP_PCT / 100)


def ask_float(text):
    try:
        return float(input(text).strip())
    except ValueError:
        print("That is not a number.")
        return None


def ask_int(text):
    try:
        return int(input(text).strip())
    except ValueError:
        print("That is not a whole number.")
        return None


parser = argparse.ArgumentParser(description="Manually place MT5 test orders (demo by default).")
parser.add_argument(
    "--live",
    action="store_true",
    help="Allow a real account for this session; requires exact account confirmation.",
)
args = parser.parse_args()

if not t.connect(allow_live=args.live):
    raise SystemExit

symbol = input("Symbol to trade [XAUUSD]: ").strip() or "XAUUSD"
if not mt5.symbol_select(symbol, True):
    print("Symbol not found:", symbol)
    t.disconnect()
    raise SystemExit

MENU = """
1  Market BUY            5  BUY STOP
2  Market SELL           6  SELL STOP
3  BUY LIMIT             7  Show positions and pending orders
4  SELL LIMIT            8  Change SL/TP of a position
9  Close one position    10 Close ALL bot positions
11 Cancel ALL pending    0  Exit
"""

try:
    while True:
        print(MENU)
        choice = input("Choose: ").strip()
        tick = mt5.symbol_info_tick(symbol)
        if choice == "0":
            break
        elif choice in ("1", "2"):
            side = "BUY" if choice == "1" else "SELL"
            price = tick.ask if side == "BUY" else tick.bid
            sl, tp = levels(side, price)
            t.market_order(symbol, side, sl, tp)
        elif choice in ("3", "4", "5", "6"):
            kind = {"3": "BUY_LIMIT", "4": "SELL_LIMIT", "5": "BUY_STOP", "6": "SELL_STOP"}[choice]
            side = "BUY" if kind.startswith("BUY") else "SELL"
            ref = tick.ask if side == "BUY" else tick.bid
            below = kind in ("BUY_LIMIT", "SELL_STOP")
            price = ref * (1 - PEND_PCT / 100) if below else ref * (1 + PEND_PCT / 100)
            sl, tp = levels(side, price)
            t.pending_order(symbol, kind, price, sl, tp)
        elif choice == "7":
            t.show_status()
        elif choice == "8":
            ticket = ask_int("Position ticket: ")
            new_sl = ask_float("New stop-loss price: ")
            new_tp = ask_float("New take-profit price (0 = keep current): ")
            if ticket and new_sl:
                t.modify_position(ticket, new_sl, new_tp if new_tp else None)
        elif choice == "9":
            ticket = ask_int("Position ticket to close: ")
            if ticket:
                t.close_position(ticket)
        elif choice == "10":
            t.close_all()
        elif choice == "11":
            t.cancel_all_pending()
        else:
            print("Please choose a number from the menu.")
finally:
    t.disconnect()
