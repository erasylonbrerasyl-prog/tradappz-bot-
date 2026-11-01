import os
import logging
import asyncio
from dotenv import load_dotenv
load_dotenv()
from typing import Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("market-study-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()

# All results are educational market summaries, not trading instructions.
ASSETS = {
    "EUR/USD": ("EUR/USD", "forex"),
    "GBP/USD": ("GBP/USD", "forex"),
    "USD/JPY": ("USD/JPY", "forex"),
    "AUD/USD": ("AUD/USD", "forex"),
    "USD/CAD": ("USD/CAD", "forex"),
    "USD/CHF": ("USD/CHF", "forex"),
    "NZD/USD": ("NZD/USD", "forex"),
    "GOLD (XAU/USD)": ("XAU/USD", "forex"),
    "SILVER (XAG/USD)": ("XAG/USD", "forex"),
    "AAPL": ("AAPL", "stock"),
    "TSLA": ("TSLA", "stock"),
    "NVDA": ("NVDA", "stock"),
    "MSFT": ("MSFT", "stock"),
    "AMZN": ("AMZN", "stock"),
    "GOOGL": ("GOOGL", "stock"),
    "META": ("META", "stock"),
    "BTC/USD": ("BTC/USD", "crypto"),
}
INTERVALS = {"1 минут": "1min", "5 минут": "5min", "15 минут": "15min"}
user_choices = {}

def chunks(items, n):
    return [items[i:i+n] for i in range(0, len(items), n)]

def keyboard_assets():
    rows = []
    labels = list(ASSETS.keys())
    for group in chunks(labels, 2):
        rows.append([InlineKeyboardButton(x, callback_data=f"asset|{x}") for x in group])
    return InlineKeyboardMarkup(rows)

def keyboard_intervals():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"interval|{value}")
        for label, value in INTERVALS.items()
    ]])

async def fetch_twelve_data(symbol: str, interval: str):
    if not TWELVE_DATA_KEY:
        raise RuntimeError("Twelve Data API кілті .env файлында көрсетілмеген.")
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 120,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.twelvedata.com/time_series", params=params)
        r.raise_for_status()
        data = r.json()
    if "values" not in data:
        msg = data.get("message") or data.get("注意") or str(data)
        raise RuntimeError(f"Twelve Data дерек қайтармады: {msg[:250]}")
    # API normally returns newest first; indicators need oldest first.
    vals = list(reversed(data["values"]))
    closes = [float(x["close"]) for x in vals if x.get("close") is not None]
    times = [x.get("datetime", "") for x in vals if x.get("close") is not None]
    if len(closes) < 35:
        raise RuntimeError("Индикаторларды есептеу үшін дерек жеткіліксіз.")
    return closes, times[-1] if times else "—"

async def fetch_alpha_stock(symbol: str, interval: str):
    if not ALPHA_VANTAGE_KEY:
        raise RuntimeError("Alpha Vantage қосалқы кілті берілмеген.")
    params = {
        "function": "TIME_SERIES_INTRADAY",
        "symbol": symbol,
        "interval": interval,
        "outputsize": "compact",
        "apikey": ALPHA_VANTAGE_KEY,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://www.alphavantage.co/query", params=params)
        r.raise_for_status()
        data = r.json()
    series_key = next((k for k in data if "Time Series" in k), None)
    if not series_key:
        msg = data.get("Note") or data.get("Information") or data.get("Error Message") or str(data)
        raise RuntimeError(f"Alpha Vantage дерек қайтармады: {msg[:250]}")
    rows = data[series_key]
    stamps = sorted(rows.keys())
    closes = [float(rows[t]["4. close"]) for t in stamps]
    if len(closes) < 35:
        raise RuntimeError("Акция бойынша дерек жеткіліксіз.")
    return closes, stamps[-1]

async def get_market_data(symbol: str, asset_type: str, interval: str):
    try:
        return await fetch_twelve_data(symbol, interval), "Twelve Data"
    except Exception as primary_error:
        if asset_type == "stock" and ALPHA_VANTAGE_KEY:
            try:
                return await fetch_alpha_stock(symbol, interval), "Alpha Vantage"
            except Exception as fallback_error:
                raise RuntimeError(f"Негізгі провайдер қатесі: {primary_error}\nҚосалқы провайдер қатесі: {fallback_error}")
        raise primary_error

def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result

def rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze(closes):
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    macd_line = [a-b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    signal_line = ema(macd_line, 9)
    r = rsi(closes)
    trend_up = e9[-1] > e21[-1]
    macd_up = macd_line[-1] > signal_line[-1]
    points = int(trend_up) + int(macd_up) + int(50 < r < 70)
    down_points = int(not trend_up) + int(not macd_up) + int(30 < r < 50)
    if points >= 2 and points > down_points:
        bias = "ӨСУ БАҒЫТЫ БАСЫМ"
    elif down_points >= 2 and down_points > points:
        bias = "ТӨМЕНДЕУ БАҒЫТЫ БАСЫМ"
    else:
        bias = "АЙҚЫН БАҒЫТ ЖОҚ"
    return {
        "price": closes[-1], "ema9": e9[-1], "ema21": e21[-1],
        "rsi": r, "macd": macd_line[-1], "macd_signal": signal_line[-1],
        "bias": bias,
    }

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Сәлем! Бұл — нарықты үйренуге арналған Market Study Bot.\n\n"
        "«Анализ жасау» батырмасын басып, актив пен уақыт аралығын таңда."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Анализ жасау", callback_data="begin")]])
    await update.message.reply_text(text, reply_markup=markup)

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Қай активті зерттейміз?", reply_markup=keyboard_assets())

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id

    if data == "begin":
        await q.edit_message_text("Қай активті зерттейміз?", reply_markup=keyboard_assets())
        return

    if data.startswith("asset|"):
        asset = data.split("|", 1)[1]
        user_choices.setdefault(uid, {})["asset"] = asset
        await q.edit_message_text(
            f"Таңдалған актив: {asset}\nҚай уақыт аралығын таңдайсың?",
            reply_markup=keyboard_intervals(),
        )
        return

    if data.startswith("interval|"):
        interval = data.split("|", 1)[1]
        choice = user_choices.get(uid, {})
        asset = choice.get("asset")
        if not asset:
            await q.edit_message_text("Алдымен активті таңда. /start")
            return
        symbol, asset_type = ASSETS[asset]
        await q.edit_message_text(
            f"⏳ {asset} · {interval} деректері жүктеліп, индикаторлар есептеліп жатыр..."
        )
        try:
            (closes, last_time), provider = await get_market_data(symbol, asset_type, interval)
            result = analyze(closes)
            msg = (
                f"📊 {asset} — оқу талдауы\n"
                f"Уақыт аралығы: {interval}\n"
                f"Соңғы баға: {result['price']:.6g}\n"
                f"Дерек уақыты: {last_time}\n"
                f"Провайдер: {provider}\n\n"
                f"Бағыттық бағалау: {result['bias']}\n\n"
                f"EMA 9: {result['ema9']:.6g}\n"
                f"EMA 21: {result['ema21']:.6g}\n"
                f"RSI (14): {result['rsi']:.1f}\n"
                f"MACD: {result['macd']:.6g}\n"
                f"MACD signal: {result['macd_signal']:.6g}\n\n"
                "Бұл индикаторларға негізделген оқу қорытындысы ғана. "
                "BUY/SELL нұсқауы емес және Pocket Option бағасымен бірдей болмауы мүмкін."
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Басқа анализ", callback_data="begin")],
                [InlineKeyboardButton("🏠 Басты мәзір", callback_data="begin")],
            ])
            await q.edit_message_text(msg, reply_markup=markup)
        except Exception as e:
            log.exception("Analysis failed")
            await q.edit_message_text(
                f"Деректі алу мүмкін болмады.\n{str(e)[:700]}\n\n"
                "API кілтін, тариф лимитін және активтің қолжетімділігін тексер.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Қайта таңдау", callback_data="begin")]]),
            )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — басты мәзір\n"
        "/analyze — актив таңдап талдау\n\n"
        "Бот тек оқу мақсатындағы техникалық көрсеткіштерді көрсетеді."
    )

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Анализ жасау", callback_data="begin")]])
    await update.message.reply_text("Активті таңдаудан баста:", reply_markup=markup)

def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN .env ішінде көрсетілмеген.")
    if not TWELVE_DATA_KEY:
        raise SystemExit("TWELVE_DATA_API_KEY .env ішінде көрсетілмеген.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    log.info("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
