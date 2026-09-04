# Gold & Forex Signal Alert V4

Educational/demo market scanner. It does not place trades.

## Markets

- Deriv XAUUSD
- Deriv EUR/USD
- Deriv GBP/USD
- Deriv USD/JPY

The workflow checks completed 15-minute candles during the configured WAT session. It sends qualified BUY/SELL alerts to Telegram and Gmail. WAIT results are logged silently.

## Required GitHub Actions repository secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

Never place passwords or tokens directly in public files.

Use **Actions → Gold and Forex Signal Alert V4 → Run workflow** to send connection tests.

Manual demo/paper-testing only. Verify every quoted level and spread on Deriv before acting.
