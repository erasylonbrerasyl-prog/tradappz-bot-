import os
import re
import json
import base64
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("trademind")
DB_PATH = os.getenv("ALERTS_DB", "alerts.sqlite3")
TD_URL = "https://api.twelvedata.com"
OPENAI_URL = "https://api.openai.com/v1/responses"

DISCLAIMER = (
    "\n\n⚠️ Бұл — оқу және демо-талдау құралы. Нарық бағытын ешкім нақты болжай алмайды. "
    "Мұны нақты ақшаға сауда жасауға арналған сигнал деп қабылдама."
)

MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Графикті суреттен талдау", callback_data="chart")],
    [InlineKeyboardButton("📈 RSI / MACD / EMA", callback_data="indicators"),
     InlineKeyboardButton("📰 Нарық жаңалықтары", callback_data="news")],
    [InlineKeyboardButton("💹 Актив бағасы", callback_data="price"),
     InlineKeyboardButton("📚 Оқу режимі", callback_data="learn")],
    [InlineKeyboardButton("🔔 Баға ескертуі", callback_data="alert"),
     InlineKeyboardButton("🧠 AI түсіндірмесі", callback_data="advisor")],
])

ASSETS = {
    "BTC/USD": "Bitcoin",
    "ETH/USD": "Ethereum",
    "EUR/USD": "Euro / US Dollar",
    "XAU/USD": "Gold / US Dollar",
    "USD/JPY": "US Dollar / Japanese Yen",
}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        target REAL NOT NULL,
        direction TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn

def main_menu_text():
    return (
        "🤖 TradeMind AI\n\n"
        "Қажетті бөлімді таңда. График суретін осы чатқа тікелей жібере аласың.\n"
        "Актив мысалдары: BTC/USD, EUR/USD, XAU/USD."
        + DISCLAIMER
    )

async def td_get(path: str, params: dict):
    if not TWELVE_DATA_KEY:
        raise RuntimeError("Twelve Data API кілті Render Environment ішінде орнатылмаған.")
    params = dict(params)
    params["apikey"] = TWELVE_DATA_KEY
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(f"{TD_URL}/{path}", params=params)
        response.raise_for_status()
        data = response.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data API қатесі"))
    return data

async def get_quote(symbol: str):
    data = await td_get("quote", {"symbol": symbol})
    return data

async def get_candles(symbol: str, interval: str = "15min", outputsize: int = 100):
    data = await td_get("time_series", {
        "symbol": symbol, "interval": interval, "outputsize": outputsize,
        "order": "ASC", "format": "JSON",
    })
    values = data.get("values")
    if not values:
        raise RuntimeError(data.get("message", "Шам деректері табылмады."))
    return [{
        "time": x.get("datetime"),
        "open": float(x["open"]), "high": float(x["high"]),
        "low": float(x["low"]), "close": float(x["close"]),
        "volume": float(x.get("volume") or 0),
    } for x in values]

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * k + current * (1-k)
    return current

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)

def macd(values):
    if len(values) < 35:
        return None, None
    # Build EMA series for accurate MACD signal estimate.
    def series(period):
        k = 2/(period+1)
        out = [None] * (period-1)
        e = sum(values[:period])/period
        out.append(e)
        for v in values[period:]:
            e = v*k + e*(1-k)
            out.append(e)
        return out
    e12, e26 = series(12), series(26)
    line = [a-b for a,b in zip(e12, e26) if a is not None and b is not None]
    return line[-1], ema(line, 9)

def levels(candles):
    # Approximate swing zones; not an order-book liquidity measurement.
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    resistance = sorted(highs[-40:], reverse=True)[:3]
    support = sorted(lows[-40:])[:3]
    return support, resistance

def find_fvg(candles):
    found = []
    for i in range(2, len(candles)):
        a, c = candles[i-2], candles[i]
        if c["low"] > a["high"]:
            found.append(("Bullish FVG", a["high"], c["low"], c["time"]))
        elif c["high"] < a["low"]:
            found.append(("Bearish FVG", c["high"], a["low"], c["time"]))
    return found[-4:]

def equal_liquidity(candles, tolerance=0.0015):
    highs = [c["high"] for c in candles[-35:]]
    lows = [c["low"] for c in candles[-35:]]
    eqh, eql = [], []
    for i, x in enumerate(highs):
        for y in highs[i+1:]:
            if abs(x-y) / max(abs(x), abs(y), 1e-9) <= tolerance:
                eqh.append((x+y)/2)
    for i, x in enumerate(lows):
        for y in lows[i+1:]:
            if abs(x-y) / max(abs(x), abs(y), 1e-9) <= tolerance:
                eql.append((x+y)/2)
    return sorted(set(round(x, 6) for x in eqh))[-3:], sorted(set(round(x, 6) for x in eql))[-3:]

def fmt(value):
    if value is None:
        return "дерек жеткіліксіз"
    return f"{value:,.6f}".rstrip("0").rstrip(".")

def technical_report(symbol, interval, candles):
    closes = [c["close"] for c in candles]
    last = closes[-1]
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    rv = rsi(closes)
    ml, ms = macd(closes)
    support, resistance = levels(candles)
    fvgs = find_fvg(candles)
    eqh, eql = equal_liquidity(candles)

    if e9 and e21 and e50 and e9 > e21 > e50:
        trend = "Жоғары бағыттағы құрылым"
    elif e9 and e21 and e50 and e9 < e21 < e50:
        trend = "Төмен бағыттағы құрылым"
    else:
        trend = "Аралас / бүйір қозғалыс"

    lines = [
        f"📊 {symbol} | {interval}",
        f"Соңғы жабылу бағасы: {fmt(last)}",
        f"Тренд: {trend}",
        f"EMA 9 / 21 / 50: {fmt(e9)} / {fmt(e21)} / {fmt(e50)}",
        f"RSI(14): {fmt(rv)}",
        f"MACD / Signal: {fmt(ml)} / {fmt(ms)}",
        "",
        "🧱 SNR аймақтары (жуық):",
        "Support: " + (", ".join(fmt(x) for x in support) if support else "табылмады"),
        "Resistance: " + (", ".join(fmt(x) for x in resistance) if resistance else "табылмады"),
        "",
        "🟦 FVG / имбаланс (соңғы аймақтар):",
    ]
    if fvgs:
        lines += [f"• {kind}: {fmt(lo)} – {fmt(hi)} ({when})" for kind,lo,hi,when in fvgs]
    else:
        lines.append("• Айқын 3-candle FVG табылмады.")
    lines += [
        "",
        "💧 Ықтимал тең максимумдар/минимумдар:",
        "EQH: " + (", ".join(fmt(x) for x in eqh) if eqh else "айқын емес"),
        "EQL: " + (", ".join(fmt(x) for x in eql) if eql else "айқын емес"),
        "",
        "🧭 Сценарийлер:",
        "• Жоғары сценарий: баға жақын resistance үстінде бекіп, қайта тест жасап ұстаса ғана күшейеді.",
        "• Төмен сценарий: баға жақын support астында бекіп, қайта тесттен өте алмаса ғана күшейеді.",
        "• Растау жоқ болса, бағыт белгісіз деп қарастыр.",
        "",
        "Ескерту: EQH/EQL — шамдардан жасалған жуық бағалау; нақты биржалық ликвидтілік дерегі емес.",
    ]
    return "\n".join(lines) + DISCLAIMER

async def analyze_chart_image(photo_bytes: bytes, mime_type: str):
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "Суретке AI талдау жасау үшін OPENAI_API_KEY қажет. Оны Render → Environment ішіне өзің қосуың керек."
        )
    encoded = base64.b64encode(photo_bytes).decode("ascii")
    prompt = (
        "Сен қазақ тілінде жауап беретін техникалық графикті оқу көмекшісісің. "
        "Суреттегі деректерді ғана пайдалан. Көрінбейтін баға/таймфреймді ойдан шығарма. "
        "Мына құрылыммен талда: 1) актив пен таймфрейм көрінсе ата; 2) жалпы тренд және market structure; "
        "3) анық көрінсе ғана bullish/bearish FVG аймақтары; 4) ықтимал buy-side/sell-side liquidity, "
        "equal highs/lows және sweep белгілері; 5) SNR support/resistance аймақтары; "
        "6) SMC/ICT бойынша ықтимал түсіндірме; 7) жоғары және төмен екі шартты сценарий; "
        "8) сценарийді жоққа шығаратын белгі; 9) сурет сапасы/дерек шектеулері. "
        "Суреттен нақты баға шкаласын оқи алмасаң, сан жазба. Кепілді болжам немесе нақты саудаға кіру бұйрығын берме. "
        "Мұның оқу/демо талдау екенін ескерт."
    )
    body = {
        "model": os.getenv("VISION_MODEL", "gpt-4.1-mini"),
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"},
            ],
        }],
        "max_output_tokens": 1100,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(OPENAI_URL, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))
    result = "\n".join(chunks).strip()
    if not result:
        raise RuntimeError("AI талдау мәтіні қайтпады. Кейінірек қайталап көр.")
    return result + DISCLAIMER

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(main_menu_text(), reply_markup=MENU)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Командалар:\n/start — мәзір\n/price BTC/USD — баға\n"
        "/analyze EUR/USD 15min — индикатор және SNR талдауы\n"
        "/alert BTC/USD 70000 above — баға ескертуі\n"
        "График суретін жай ғана чатқа жібер — AI талдайды."
        + DISCLAIMER,
        reply_markup=MENU,
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🧠 График суретін қарап жатырмын. Бір сәт күте тұр...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        data = await file.download_as_bytearray()
        result = await analyze_chart_image(bytes(data), "image/jpeg")
        # Telegram message limit safety
        await msg.edit_text(result[:4000])
        if len(result) > 4000:
            await update.message.reply_text(result[4000:8000])
    except Exception as exc:
        log.exception("Chart image analysis failed")
        await msg.edit_text(f"Суретті талдау орындалмады: {str(exc)[:500]}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = " ".join(context.args).upper() if context.args else "BTC/USD"
    try:
        q = await get_quote(symbol)
        await update.message.reply_text(
            f"💹 {q.get('symbol', symbol)}\n"
            f"Баға: {q.get('close', q.get('price', '—'))}\n"
            f"Өзгеріс: {q.get('percent_change', '—')}%\n"
            f"Уақыты: {q.get('datetime', '—')}" + DISCLAIMER
        )
    except Exception as exc:
        await update.message.reply_text(f"Баға алынбады: {str(exc)[:400]}")

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC/USD"
    interval = context.args[1] if len(context.args) > 1 else "15min"
    allowed = {"1min", "5min", "15min", "30min", "1h", "4h", "1day"}
    if interval not in allowed:
        await update.message.reply_text("Таймфрейм: 1min, 5min, 15min, 30min, 1h, 4h, 1day")
        return
    try:
        candles = await get_candles(symbol, interval)
        await update.message.reply_text(technical_report(symbol, interval, candles)[:4000])
    except Exception as exc:
        await update.message.reply_text(f"Талдау орындалмады: {str(exc)[:400]}")

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FINNHUB_API_KEY:
        await update.message.reply_text(
            "📰 Жаңалықтар функциясы үшін FINNHUB_API_KEY қосылмаған.\n"
            "Render Environment ішіне FINNHUB_API_KEY орнатылса, жаңалықтар ашылады."
        )
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://finnhub.io/api/v1/news",
                params={"category": "general", "token": FINNHUB_API_KEY},
            )
            r.raise_for_status()
            items = r.json()[:5]
        if not items:
            await update.message.reply_text("Қазір жаңалық табылмады.")
            return
        lines = ["📰 Соңғы жаңалықтар:"]
        for x in items:
            lines.append(f"• {x.get('headline','Тақырып жоқ')}\n{x.get('url','')}")
        await update.message.reply_text("\n\n".join(lines)[:4000])
    except Exception as exc:
        await update.message.reply_text(f"Жаңалықтар алынбады: {str(exc)[:300]}")

LEARN = {
    "FVG": "FVG (Fair Value Gap) — үш шам құрылымында ортаңғы импульстен кейін алдыңғы және соңғы шам диапазондары арасында қалған баға аралығы. Ол әрдайым міндетті түрде толтырылады деген сөз емес.",
    "Ликвидтілік": "Ликвидтілік аймақтары ретінде трейдерлер жиі алдыңғы максимум/минимумдар мен тең максимум/минимумдарды бақылайды. Графиктен көрінгені нақты ордерлер кітабы емес.",
    "SNR": "Support — баға бұрын төмендеуді баяулатқан аймақ; Resistance — жоғарылауды баяулатқан аймақ. Бұлар бір нүкте емес, көбіне диапазон.",
    "RSI": "RSI импульсті өлшейді. 70-тен жоғары немесе 30-дан төмен болуы автоматты түрде сату/сатып алу белгісі емес.",
    "MACD": "MACD екі EMA айырмасын және оның сигнал сызығын салыстырады. Кроссовер кешігіп келуі мүмкін.",
    "EMA": "EMA соңғы бағаларға көбірек салмақ береді. EMA 9/21/50 трендті салыстыруға көмектеседі, бірақ жалғыз өзі шешім емес.",
    "SMC": "SMC — market structure, BOS/CHOCH, liquidity sweep және FVG сияқты ұғымдарды біріктіретін техникалық талдау тәсілі. Интерпретация субъективті болуы мүмкін.",
}
async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "📚 Оқу тақырыптары:\n" + "\n".join(f"• {k}" for k in LEARN)
    await update.message.reply_text(text + "\n\nТақырыпты таңда: /learn FVG")
async def learn_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip()
    if not topic:
        await learn_command(update, context)
        return
    match = next((v for k,v in LEARN.items() if k.lower() == topic.lower()), None)
    await update.message.reply_text(match or "Тақырып табылмады. /learn деп жаз.")

async def advisor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Актив пен таймфреймді көрсет: /advisor BTC/USD 15min\n"
            "Мен индикаторлар мен құрылымды түсіндіріп, шартты сценарийлерді салыстырамын."
        )
        return
    symbol = context.args[0].upper()
    interval = context.args[1] if len(context.args) > 1 else "15min"
    try:
        candles = await get_candles(symbol, interval)
        report = technical_report(symbol, interval, candles)
        await update.message.reply_text("🧠 AI түсіндірмесі\n\n" + report[:3900])
    except Exception as exc:
        await update.message.reply_text(f"Түсіндіру мүмкін болмады: {str(exc)[:350]}")

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /alert BTC/USD 70000 above\n"
            "above — баға нысанадан жоғарыласа; below — төмендесе."
        )
        return
    symbol = context.args[0].upper()
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Баға сан болуы керек.")
        return
    direction = context.args[2].lower()
    if target <= 0 or direction not in ("above", "below"):
        await update.message.reply_text("Баға оң сан, бағыт above немесе below болуы керек.")
        return
    conn = db()
    conn.execute(
        "INSERT INTO alerts(user_id,symbol,target,direction,created_at) VALUES(?,?,?,?,?)",
        (update.effective_user.id, symbol, target, direction, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🔔 Ескерту сақталды: {symbol} {target} {direction}.\n"
        "Баға тексерісі фондық режимде жүреді."
    )

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute("SELECT id,user_id,symbol,target,direction FROM alerts").fetchall()
    conn.close()
    for alert_id, user_id, symbol, target, direction in rows:
        try:
            q = await get_quote(symbol)
            price = float(q.get("close") or q.get("price"))
            hit = price >= target if direction == "above" else price <= target
            if hit:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🔔 Баға ескертуі\n{symbol}: {fmt(price)}\nНысана: {fmt(target)}\n"
                         "Бұл тек баға туралы хабарлама, сауда нұсқауы емес."
                )
                conn = db()
                conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                conn.commit()
                conn.close()
        except Exception:
            log.exception("Alert check failed for %s", symbol)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data
    if key == "chart":
        text = "📊 График скриншотын осы чатқа жібер. Баға шкаласы мен таймфрейм анық көрінсін."
    elif key == "indicators":
        text = "📈 Актив пен таймфреймді жаз:\n/analyze BTC/USD 15min"
    elif key == "news":
        text = "📰 Жалпы нарық жаңалықтарын алу үшін FINNHUB_API_KEY қажет. /news"
    elif key == "price":
        text = "💹 Бағаны көру: /price BTC/USD\nМысал: /price XAU/USD"
    elif key == "learn":
        text = "📚 Оқу режимі: /learn\nМысал: /learn FVG"
    elif key == "alert":
        text = "🔔 Ескерту қою:\n/alert BTC/USD 70000 above\n/alert EUR/USD 1.05 below"
    elif key == "advisor":
        text = "🧠 Активті түсіндіру:\n/advisor BTC/USD 15min"
    else:
        text = "Мәзір"
    await query.message.reply_text(text, reply_markup=MENU)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)

def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN Render Environment ішінде көрсетілмеген.")
    if not TWELVE_DATA_KEY:
        log.warning("TWELVE_DATA_API_KEY жоқ: нарық деректері жұмыс істемейді.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(CommandHandler("learn", learn_detail))
    app.add_handler(CommandHandler("advisor", advisor_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(check_alerts, interval=60, first=20)
    log.info("TradeMind AI bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
