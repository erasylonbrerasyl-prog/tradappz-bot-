# TradeMind AI Telegram Bot

Kazakh-language educational market-analysis Telegram bot.

## Features
1. Chart screenshot analysis (OpenAI vision; optional API key)
2. RSI, MACD, EMA indicators
3. General market headlines (Finnhub; optional API key)
4. Asset quotes (Twelve Data)
5. Learning mode for FVG, liquidity, SNR, SMC and indicators
6. Price alerts (SQLite; checked every 60 seconds)
7. Technical-analysis explainer with conditional bullish/bearish scenarios

The bot is an educational/demo tool. It does not guarantee market direction and does not issue instructions to trade real money.

## Render setup
- Runtime: Python 3.12
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Service type: Background Worker

Set these Environment Variables in Render:
- `TELEGRAM_BOT_TOKEN` (required)
- `TWELVE_DATA_API_KEY` (required for quotes/indicators/alerts)
- `OPENAI_API_KEY` (required for image analysis)
- `FINNHUB_API_KEY` (optional; news)
- `VISION_MODEL` (optional; defaults to `gpt-4.1-mini`)

Never paste API keys into chat or commit them to GitHub. Add them only in Render Environment.
