## 1) Local run
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# fill .env from .env.example, then:
export BOT_TOKEN='YOUR_TOKEN'
export DATABASE_URL='YOUR_DB_URL'
export ADMIN_TELEGRAM_ID='698037613'
python irantamir_bot.py# iranTamir-bot
