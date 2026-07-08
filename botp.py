import os
import json
import hmac
import ssl
import time
import hashlib
import logging
import sqlite3
import asyncio
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================================================
# Payment Bot — отдельный бот оплаты для основного бота
# =========================================================
# ENV:
#   PAYMENT_BOT_TOKEN=токен платежного бота
#   MAIN_BOT_USERNAME=username основного бота без @
#   DB_PATH=anime.db
#   YOOKASSA_SHOP_ID=...
#   YOOKASSA_SECRET_KEY=...
#   YOOKASSA_RETURN_URL=https://t.me/твой_платежный_бот или https://t.me/основной_бот
#   CRYPTOBOT_TOKEN=...
#   CRYPTOBOT_API_BASE=https://pay.crypt.bot/api
#   PAYMENT_WEBHOOK_PORT=3000
#   SSL_CERT_FILE=/path/fullchain.pem  # опционально
#   SSL_KEY_FILE=/path/privkey.pem     # опционально

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BOT_TOKEN = os.getenv("PAYMENT_BOT_TOKEN") or os.getenv("BOT_TOKEN")
MAIN_BOT_USERNAME = (os.getenv("MAIN_BOT_USERNAME") or "").strip().lstrip("@")
DB_PATH = os.getenv("DB_PATH", "anime.db")

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN")
CRYPTOBOT_API_BASE = os.getenv("CRYPTOBOT_API_BASE", "https://pay.crypt.bot/api").rstrip("/")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL") or (f"https://t.me/{MAIN_BOT_USERNAME}" if MAIN_BOT_USERNAME else "https://t.me/")
YOOKASSA_API_PAYMENTS = "https://api.yookassa.ru/v3/payments"

WEBHOOK_PORT = int(os.getenv("PAYMENT_WEBHOOK_PORT") or os.getenv("WEBHOOK_PORT", "3000"))
SSL_CERT_FILE = os.getenv("SSL_CERT_FILE")
SSL_KEY_FILE = os.getenv("SSL_KEY_FILE")

RUB_PRICES = {
    "7_days": 39,
    "30_days": 99,
    "180_days": 499,
    "360_days": 899,
    "forever": 1499,
}

TARIFFS = {
    "7_days": {"title": "7 дней", "days": 7},
    "30_days": {"title": "30 дней", "days": 30},
    "180_days": {"title": "180 дней", "days": 180},
    "360_days": {"title": "360 дней", "days": 360},
    "forever": {"title": "Навсегда", "days": None},
}

CRYPTO_MARGIN = Decimal(os.getenv("CRYPTO_MARGIN", "0.30"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан PAYMENT_BOT_TOKEN или BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =========================================================
# DB
# =========================================================

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    type TEXT,
    expire_date TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS pending_payments (
    user_id INTEGER PRIMARY KEY,
    invoice_id TEXT,
    period_key TEXT,
    created_at TEXT,
    pay_url TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS processed_invoices (
    invoice_id TEXT PRIMARY KEY,
    user_id INTEGER,
    period_key TEXT,
    created_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    user_id INTEGER PRIMARY KEY,
    my_code TEXT UNIQUE,
    used_code TEXT,
    referred_by INTEGER,
    bonus_given INTEGER DEFAULT 0,
    months_awarded INTEGER DEFAULT 0,
    first_name TEXT,
    username TEXT,
    created_at TEXT
)
""")

db.commit()

# =========================================================
# Helpers
# =========================================================

def main_bot_url() -> str | None:
    if not MAIN_BOT_USERNAME:
        return None
    return f"https://t.me/{MAIN_BOT_USERNAME}"


def period_title(period_key: str) -> str:
    return TARIFFS.get(period_key, {}).get("title", period_key.replace("_", " "))


def rub_amount(period_key: str) -> int:
    return int(RUB_PRICES.get(period_key, 0))


def build_back_to_main_keyboard() -> InlineKeyboardMarkup | None:
    url = main_bot_url()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=" Вернуться в основной бот", url=url)]
    ])


def give_subscription(user_id: int, days: int | None):
    now = datetime.now()
    cursor.execute("SELECT expire_date FROM subscriptions WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if days is None:
        if row:
            cursor.execute(
                "UPDATE subscriptions SET type=?, expire_date=? WHERE user_id=?",
                ("forever", "forever", user_id)
            )
        else:
            cursor.execute(
                "INSERT INTO subscriptions (user_id, type, expire_date) VALUES (?, ?, ?)",
                (user_id, "forever", "forever")
            )
        db.commit()
        return

    if row and row[0] == "forever":
        return

    if row and row[0]:
        try:
            old_expire = datetime.fromisoformat(row[0])
        except Exception:
            old_expire = now
        if old_expire > now:
            new_expire = old_expire + timedelta(days=days)
        else:
            new_expire = now + timedelta(days=days)
        cursor.execute(
            "UPDATE subscriptions SET type=?, expire_date=? WHERE user_id=?",
            (f"{days}_days", new_expire.isoformat(), user_id)
        )
    else:
        new_expire = now + timedelta(days=days)
        cursor.execute(
            "INSERT INTO subscriptions (user_id, type, expire_date) VALUES (?, ?, ?)",
            (user_id, f"{days}_days", new_expire.isoformat())
        )
    db.commit()


def get_subscription_status(user_id: int) -> str:
    cursor.execute("SELECT type, expire_date FROM subscriptions WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return "❌ Подписка неактивна"
    sub_type, expire_date = row
    if expire_date == "forever" or sub_type == "forever":
        return "✅ Подписка активна навсегда"
    try:
        dt = datetime.fromisoformat(expire_date)
        if dt > datetime.now():
            return f"✅ Подписка активна до {dt.strftime('%d.%m.%Y %H:%M')}"
    except Exception:
        pass
    return "❌ Подписка неактивна"


async def process_referral_bonus(user_id: int, period_key: str):
    if period_key not in ("30_days", "180_days", "360_days", "forever"):
        return

    cursor.execute("SELECT referred_by, bonus_given FROM referrals WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return

    referred_by, bonus_given = row
    if not referred_by or bonus_given == 1:
        return

    inviter_id = int(referred_by)

    give_subscription(user_id, 7)
    give_subscription(inviter_id, 7)

    cursor.execute("UPDATE referrals SET bonus_given=1 WHERE user_id=?", (user_id,))

    cursor.execute("SELECT COUNT(*) FROM referrals WHERE referred_by=? AND bonus_given=1", (inviter_id,))
    count = cursor.fetchone()[0]

    cursor.execute("SELECT months_awarded FROM referrals WHERE user_id=?", (inviter_id,))
    row_months = cursor.fetchone()
    months_awarded = row_months[0] if row_months else 0
    should_have_months = count // 5
    month_awarded = False

    if should_have_months > months_awarded:
        give_subscription(inviter_id, 30)
        cursor.execute("UPDATE referrals SET months_awarded=? WHERE user_id=?", (should_have_months, inviter_id))
        month_awarded = True

    db.commit()

    try:
        await bot.send_message(user_id, "🎁 Бонус активирован! Вы получили 7 дней подписки.")
    except Exception:
        pass

    try:
        if month_awarded:
            await bot.send_message(inviter_id, "🎁 Вам начислен 1 месяц подписки за каждые 5 рефералов!")
        else:
            await bot.send_message(inviter_id, "🎁 Новый реферал! Вам начислено +7 дней подписки.")
    except Exception:
        pass


async def activate_paid_subscription(user_id: int, period_key: str, payment_id: str, provider: str):
    processed_id = f"{provider}:{payment_id}"
    cursor.execute("SELECT 1 FROM processed_invoices WHERE invoice_id=?", (processed_id,))
    if cursor.fetchone():
        logging.warning(f"[{provider}] Payment already processed: {processed_id}")
        return False

    if period_key not in TARIFFS:
        logging.error(f"[{provider}] Unknown period_key={period_key}")
        return False

    days = TARIFFS[period_key]["days"]
    give_subscription(user_id, days)

    cursor.execute(
        "INSERT INTO processed_invoices (invoice_id, user_id, period_key, created_at) VALUES (?, ?, ?, ?)",
        (processed_id, user_id, period_key, datetime.now().isoformat())
    )
    cursor.execute("DELETE FROM pending_payments WHERE user_id=?", (user_id,))
    db.commit()

    await process_referral_bonus(user_id, period_key)

    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Тариф: <b>{period_title(period_key)}</b>\n"
            f"Платёж: <code>{payment_id}</code>",
            parse_mode="HTML",
            reply_markup=build_back_to_main_keyboard()
        )
    except Exception as e:
        logging.error(f"[{provider}] Could not notify user={user_id}: {e}")

    logging.info(f"[{provider}] Subscription activated user={user_id}, period={period_key}, payment={payment_id}")
    return True

# =========================================================
# Menu
# =========================================================

def build_tariffs_keyboard() -> InlineKeyboardMarkup:
    # Такой же выбор тарифов, как в основном боте.
    rows = [
        [InlineKeyboardButton(text="7 дней — 39₽", callback_data="buy_7")],
        [InlineKeyboardButton(text="30 дней — 99₽", callback_data="buy_30")],
        [InlineKeyboardButton(text="180 дней — 499₽", callback_data="buy_180")],
        [InlineKeyboardButton(text="360 дней — 899₽", callback_data="buy_360")],
        [InlineKeyboardButton(text="Навсегда (только 100 чел.) — 1499₽", callback_data="buy_forever")],
    ]
    if MAIN_BOT_USERNAME:
        rows.append([InlineKeyboardButton(text="⬅️ Вернуться в основной бот", url=main_bot_url())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payment_methods_keyboard(period_key: str) -> InlineKeyboardMarkup:
    # Важно: style="warning" Telegram/BotHost не принимает — из-за этого была ошибка
    # "invalid button style specified". Поэтому для Stars используем оранжевый значок в тексте.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🟢 Оплатить рублями",
            callback_data=f"pay_rub|{period_key}",
            style="success",
            icon_custom_emoji_id="5231449120635370684"
        )],
        [InlineKeyboardButton(
            text="🔴 Оплатить криптовалютой",
            callback_data=f"pay_crypto|{period_key}",
            style="danger",
            icon_custom_emoji_id="5231005931550030290"
        )],
        [InlineKeyboardButton(
            text="🟠 Оплатить звёздами",
            callback_data=f"pay_stars|{period_key}",
            icon_custom_emoji_id="5438496463044752972"
        )],
        [InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="tariffs")]
    ])


async def send_tariff_menu(target, user_id: int):
    text = (
        "💳 <b>Оплата подписки</b>\n\n"
        f"{get_subscription_status(user_id)}\n\n"
        "Выберите тариф:"
    )
    kb = build_tariffs_keyboard()
    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


async def send_payment_methods(target, period_key: str):
    amount = rub_amount(period_key)
    text = (
        f"💳 <b>Вы выбрали:</b> {period_title(period_key)}\n\n"
        f"Цена: <b>{amount}₽</b> или <b>{amount}⭐</b>\n\n"
        "Выберите способ оплаты:"
    )
    kb = build_payment_methods_keyboard(period_key)
    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("start"))
async def start(message: types.Message):
    args = ""
    if message.text and len(message.text.split()) > 1:
        args = message.text.split(maxsplit=1)[1]

    if args.startswith("plan_"):
        period_key = args.replace("plan_", "", 1)
        if period_key in RUB_PRICES:
            await send_payment_methods(message, period_key)
            return

    await send_tariff_menu(message, message.from_user.id)


@router.callback_query(F.data == "tariffs")
async def tariffs_handler(call: types.CallbackQuery):
    await send_tariff_menu(call, call.from_user.id)


@router.callback_query(F.data.startswith("plan|"))
async def plan_handler(call: types.CallbackQuery):
    _, period_key = call.data.split("|", 1)
    if period_key not in RUB_PRICES:
        await call.answer("Ошибка тарифа", show_alert=True)
        return
    await send_payment_methods(call, period_key)


@router.callback_query(F.data.startswith("buy_"))
async def buy_handler(call: types.CallbackQuery):
    tariffs_map = {
        "buy_7": "7_days",
        "buy_30": "30_days",
        "buy_180": "180_days",
        "buy_360": "360_days",
        "buy_forever": "forever",
    }
    period_key = tariffs_map.get(call.data)
    if not period_key:
        await call.answer("Ошибка тарифа", show_alert=True)
        return
    await send_payment_methods(call, period_key)

# =========================================================
# YooKassa
# =========================================================

async def get_yookassa_payment(payment_id: str):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logging.error("[YooKassa] Не заданы YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")
        return None
    try:
        auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(f"{YOOKASSA_API_PAYMENTS}/{payment_id}", timeout=15) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logging.error(f"[YooKassa] get payment error {resp.status}: {data}")
                    return None
                return data
    except Exception as e:
        logging.error(f"[YooKassa] get payment exception: {e}")
        return None


async def create_yookassa_payment(user_id: int, amount: int, period_key: str):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logging.error("[YooKassa] Не заданы YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")
        return None, None

    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "description": f"Подписка {period_title(period_key)} для Telegram user {user_id}",
        "metadata": {
            "user_id": str(user_id),
            "period_key": period_key,
            "provider": "yookassa",
            "service": "payment_bot"
        }
    }
    headers = {"Idempotence-Key": str(uuid4()), "Content-Type": "application/json"}

    try:
        auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(YOOKASSA_API_PAYMENTS, json=payload, headers=headers, timeout=20) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logging.error(f"[YooKassa] create payment error {resp.status}: {data}")
                    return None, None

                payment_id = data.get("id")
                pay_url = data.get("confirmation", {}).get("confirmation_url")
                if not payment_id or not pay_url:
                    logging.error(f"[YooKassa] Нет payment_id или confirmation_url: {data}")
                    return None, None

                cursor.execute(
                    "INSERT OR REPLACE INTO pending_payments (user_id, invoice_id, period_key, created_at, pay_url) VALUES (?, ?, ?, ?, ?)",
                    (user_id, payment_id, period_key, datetime.now().isoformat(), pay_url)
                )
                db.commit()
                logging.info(f"[YooKassa] Payment created user={user_id}, period={period_key}, payment_id={payment_id}")
                return payment_id, pay_url
    except Exception as e:
        logging.error(f"[YooKassa] create payment exception: {e}")
        return None, None


@router.callback_query(lambda c: c.data and (c.data.startswith("pay_rub|") or c.data.startswith("rub|")))
async def rub_handler(call: types.CallbackQuery):
    _, period_key = call.data.split("|", 1)
    amount = rub_amount(period_key)
    if amount <= 0:
        await call.answer("Ошибка тарифа", show_alert=True)
        return

    payment_id, pay_url = await create_yookassa_payment(call.from_user.id, amount, period_key)
    if not payment_id or not pay_url:
        await call.message.answer("❌ Не удалось создать платёж YooKassa. Проверь переменные и логи сервера.")
        await call.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=" Перейти к оплате", url=pay_url)],
        [InlineKeyboardButton(text=" Проверить оплату", callback_data=f"check_yoo|{payment_id}")],
        [InlineKeyboardButton(text=" Назад", callback_data=f"plan|{period_key}")]
    ])
    await call.message.edit_text(
        f"💚 <b>Оплата рублями</b>\n\n"
        f"Тариф: <b>{period_title(period_key)}</b>\n"
        f"Сумма: <b>{amount}₽</b>\n\n"
        "После оплаты подписка активируется автоматически.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("check_yoo|"))
async def check_yoo_handler(call: types.CallbackQuery):
    _, payment_id = call.data.split("|", 1)
    payment = await get_yookassa_payment(payment_id)
    if not payment:
        await call.answer("Не удалось проверить платёж", show_alert=True)
        return

    if payment.get("status") == "succeeded" and bool(payment.get("paid")):
        metadata = payment.get("metadata") or {}
        user_id = int(metadata.get("user_id") or call.from_user.id)
        period_key = metadata.get("period_key")
        activated = await activate_paid_subscription(user_id, period_key, payment_id, "yookassa")
        await call.answer("✅ Оплата найдена" if activated else "✅ Оплата уже обработана", show_alert=True)
        return

    await call.answer(f"Платёж пока не оплачен. Статус: {payment.get('status')}", show_alert=True)


async def handle_yookassa_webhook(request):
    try:
        data = await request.json()
        logging.info(f"[YOOKASSA_WEBHOOK] Получены данные: {data}")

        if data.get("type") != "notification":
            return web.Response(text="Ignored")

        event = data.get("event")
        obj = data.get("object") or {}
        if event != "payment.succeeded":
            return web.Response(text="Ignored")

        payment_id = obj.get("id")
        if not payment_id:
            return web.Response(text="No payment id", status=400)

        verified = await get_yookassa_payment(payment_id)
        payment = verified or obj
        if payment.get("status") != "succeeded" or not bool(payment.get("paid")):
            return web.Response(text="Not paid")

        metadata = payment.get("metadata") or obj.get("metadata") or {}
        user_id = int(metadata.get("user_id"))
        period_key = metadata.get("period_key")
        await activate_paid_subscription(user_id, period_key, payment_id, "yookassa")
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"[YOOKASSA_WEBHOOK] Ошибка: {e}")
        return web.Response(text="Server error", status=500)

# =========================================================
# Telegram Stars
# =========================================================

def make_stars_payload(user_id: int, period_key: str) -> str:
    return f"stars|{user_id}|{period_key}|{int(time.time())}"


def parse_stars_payload(payload: str):
    try:
        parts = payload.split("|")
        if len(parts) < 3 or parts[0] != "stars":
            return None, None
        return int(parts[1]), parts[2]
    except Exception:
        return None, None


@router.callback_query(lambda c: c.data and (c.data.startswith("pay_stars|") or c.data.startswith("stars|")))
async def stars_handler(call: types.CallbackQuery):
    _, period_key = call.data.split("|", 1)
    amount = rub_amount(period_key)
    if amount <= 0:
        await call.answer("Ошибка тарифа", show_alert=True)
        return

    payload = make_stars_payload(call.from_user.id, period_key)
    cursor.execute(
        "INSERT OR REPLACE INTO pending_payments (user_id, invoice_id, period_key, created_at, pay_url) VALUES (?, ?, ?, ?, ?)",
        (call.from_user.id, payload, period_key, datetime.now().isoformat(), "telegram_stars")
    )
    db.commit()

    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"Подписка {period_title(period_key)}",
            description=f"Оплата подписки через Telegram Stars. Соотношение 1 ⭐ = 1 ₽. К оплате: {amount} ⭐",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"Подписка {period_title(period_key)}", amount=amount)],
        )
        await call.answer()
    except Exception as e:
        logging.error(f"[TelegramStars] send_invoice error: {e}")
        await call.message.answer("❌ Не удалось создать счёт Telegram Stars. Проверь логи сервера.")
        await call.answer()


@router.pre_checkout_query()
async def stars_pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    user_id, period_key = parse_stars_payload(pre_checkout_query.invoice_payload)
    if not user_id or period_key not in RUB_PRICES:
        await pre_checkout_query.answer(ok=False, error_message="Ошибка платежа")
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def stars_success_handler(message: types.Message):
    payment = message.successful_payment
    if payment.currency != "XTR":
        return

    user_id, period_key = parse_stars_payload(payment.invoice_payload)
    if not user_id or period_key not in RUB_PRICES:
        logging.error(f"[TelegramStars] Invalid payload: {payment.invoice_payload}")
        return

    expected = rub_amount(period_key)
    if payment.total_amount != expected:
        logging.error(f"[TelegramStars] amount mismatch user={user_id}: got={payment.total_amount}, expected={expected}")
        await message.answer("❌ Ошибка суммы платежа. Напишите в поддержку.")
        return

    payment_id = payment.telegram_payment_charge_id or payment.provider_payment_charge_id or payment.invoice_payload
    await activate_paid_subscription(user_id, period_key, payment_id, "telegram_stars")

# =========================================================
# CryptoBot
# =========================================================

async def create_crypto_invoice(user_id: int, amount: int, period_key: str):
    if not CRYPTOBOT_TOKEN:
        logging.error("[CryptoBot] Не задан CRYPTOBOT_TOKEN")
        return None, None

    url = f"{CRYPTOBOT_API_BASE}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": str(amount),
        "description": f"Подписка {period_title(period_key)}",
        "payload": f"{user_id}|{period_key}",
        "hidden_message": "Спасибо за оплату! Подписка будет активирована автоматически."
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=20) as resp:
                result = await resp.json(content_type=None)
                if resp.status >= 400 or not result.get("ok"):
                    logging.error(f"[CryptoBot] createInvoice error {resp.status}: {result}")
                    return None, None
                invoice = result.get("result") or {}
                invoice_id = str(invoice.get("invoice_id") or "")
                invoice_url = invoice.get("bot_invoice_url") or invoice.get("pay_url") or invoice.get("mini_app_invoice_url")
                if not invoice_id or not invoice_url:
                    logging.error(f"[CryptoBot] Нет invoice_id или url: {invoice}")
                    return None, None
                cursor.execute(
                    "INSERT OR REPLACE INTO pending_payments (user_id, invoice_id, period_key, created_at, pay_url) VALUES (?, ?, ?, ?, ?)",
                    (user_id, invoice_id, period_key, datetime.now().isoformat(), invoice_url)
                )
                db.commit()
                logging.info(f"[CryptoBot] Invoice created user={user_id}, period={period_key}, invoice_id={invoice_id}")
                return invoice_id, invoice_url
    except Exception as e:
        logging.error(f"[CryptoBot] createInvoice exception: {e}")
        return None, None


async def get_crypto_invoice(invoice_id: str):
    if not CRYPTOBOT_TOKEN:
        logging.error("[CryptoBot] Не задан CRYPTOBOT_TOKEN")
        return None
    url = f"{CRYPTOBOT_API_BASE}/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    params = {"invoice_ids": invoice_id}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=15) as resp:
                result = await resp.json(content_type=None)
                if resp.status >= 400 or not result.get("ok"):
                    logging.error(f"[CryptoBot] getInvoices error {resp.status}: {result}")
                    return None
                items = result.get("result", {}).get("items") or []
                return items[0] if items else None
    except Exception as e:
        logging.error(f"[CryptoBot] getInvoices exception: {e}")
        return None


def verify_cryptobot_signature(raw_body: bytes, headers) -> bool:
    if not CRYPTOBOT_TOKEN:
        logging.error("[CRYPTO_WEBHOOK] Не задан CRYPTOBOT_TOKEN")
        return False
    signature = headers.get("crypto-pay-api-signature") or headers.get("Crypto-Pay-API-Signature")
    if not signature:
        logging.error("[CRYPTO_WEBHOOK] Нет crypto-pay-api-signature")
        return False
    secret = hashlib.sha256(CRYPTOBOT_TOKEN.encode("utf-8")).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.callback_query(lambda c: c.data and (c.data.startswith("pay_crypto|") or c.data.startswith("crypto|")))
async def crypto_handler(call: types.CallbackQuery):
    _, period_key = call.data.split("|", 1)
    amount = rub_amount(period_key)
    if amount <= 0:
        await call.answer("Ошибка тарифа", show_alert=True)
        return

    invoice_id, invoice_url = await create_crypto_invoice(call.from_user.id, amount, period_key)
    if not invoice_id or not invoice_url:
        await call.message.answer("❌ Не удалось создать счёт CryptoBot. Проверь CRYPTOBOT_TOKEN и логи сервера.")
        await call.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=" Перейти к оплате", url=invoice_url)],
        [InlineKeyboardButton(text=" Проверить оплату", callback_data=f"check_crypto|{invoice_id}")],
        [InlineKeyboardButton(text=" Назад", callback_data=f"plan|{period_key}")]
    ])
    await call.message.edit_text(
        f"❤️ <b>Оплата криптовалютой</b>\n\n"
        f"Тариф: <b>{period_title(period_key)}</b>\n"
        f"Сумма: <b>{amount}₽</b>\n\n"
        "После оплаты подписка активируется автоматически через webhook.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("check_crypto|"))
async def check_crypto_handler(call: types.CallbackQuery):
    _, invoice_id = call.data.split("|", 1)
    invoice = await get_crypto_invoice(invoice_id)
    if not invoice:
        await call.answer("Не удалось проверить счёт", show_alert=True)
        return

    if invoice.get("status") == "paid":
        cursor.execute("SELECT user_id, period_key FROM pending_payments WHERE invoice_id=?", (invoice_id,))
        row = cursor.fetchone()
        if row:
            user_id, period_key = row
        else:
            payload = invoice.get("payload") or ""
            user_id_str, period_key = str(payload).split("|", 1)
            user_id = int(user_id_str)
        activated = await activate_paid_subscription(int(user_id), period_key, invoice_id, "cryptobot")
        await call.answer("✅ Оплата найдена" if activated else "✅ Оплата уже обработана", show_alert=True)
        return

    await call.answer(f"Счёт пока не оплачен. Статус: {invoice.get('status')}", show_alert=True)


async def handle_crypto_webhook(request):
    try:
        raw_body = await request.read()
        if not verify_cryptobot_signature(raw_body, request.headers):
            return web.Response(text="Bad signature", status=403)
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return web.Response(text="Invalid JSON", status=400)

        logging.info(f"[CRYPTO_WEBHOOK] Получены данные: {data}")
        if data.get("update_type") != "invoice_paid":
            return web.Response(text="Ignored")

        payload = data.get("payload") or {}
        invoice_id = str(payload.get("invoice_id") or "")
        invoice_payload = payload.get("payload")
        status = payload.get("status")

        if not invoice_id:
            return web.Response(text="No invoice_id", status=400)
        if status and status != "paid":
            return web.Response(text="Not paid")

        cursor.execute("SELECT user_id, period_key FROM pending_payments WHERE invoice_id=?", (invoice_id,))
        row = cursor.fetchone()
        if row:
            user_id, period_key = row
        elif invoice_payload:
            user_id_str, period_key = str(invoice_payload).split("|", 1)
            user_id = int(user_id_str)
        else:
            logging.error(f"[CRYPTO_WEBHOOK] Не удалось определить user/period для invoice={invoice_id}")
            return web.Response(text="Unknown invoice")

        await activate_paid_subscription(int(user_id), period_key, invoice_id, "cryptobot")
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"[CRYPTO_WEBHOOK] Ошибка: {e}")
        return web.Response(text="Server error", status=500)

# =========================================================
# Web server + polling
# =========================================================

async def health(request):
    return web.Response(text="Payment bot OK")


def build_ssl_context():
    if SSL_CERT_FILE and SSL_KEY_FILE:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        return ctx
    return None


app = web.Application()
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.router.add_post("/yookassa", handle_yookassa_webhook)
app.router.add_post("/cryptobot", handle_crypto_webhook)
app.router.add_post("/webhook", handle_crypto_webhook)  # совместимость со старым путём CryptoBot


async def start_web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_context = build_ssl_context()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT, ssl_context=ssl_context)
    await site.start()
    scheme = "HTTPS" if ssl_context else "HTTP"
    logging.info(f"Payment webhook server started on port {WEBHOOK_PORT} ({scheme})")


async def cleanup_old_records():
    while True:
        try:
            cursor.execute("DELETE FROM processed_invoices WHERE created_at < datetime('now', '-30 days')")
            db.commit()
        except Exception as e:
            logging.error(f"[Cleanup] Ошибка: {e}")
        await asyncio.sleep(24 * 60 * 60)


async def on_startup():
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню оплаты")
    ])
    logging.info("Payment bot started")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web_server()
    asyncio.create_task(cleanup_old_records())
    await on_startup()
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
