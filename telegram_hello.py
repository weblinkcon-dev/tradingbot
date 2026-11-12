import requests
from bot_settings import TELEGRAM_TOKEN, CHAT_ID

url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
reply = requests.post(url, data={"chat_id": CHAT_ID, "text": "Hello from my trading bot (demo)"}, timeout=15)
print(reply.status_code)
print(reply.json().get("ok"), reply.json().get("description", ""))