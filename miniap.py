import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3
import time
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    CallbackQuery,
)

import uvicorn


# =========================================================
# SOZLAMALAR
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "baraban_bonusbot").strip().lstrip("@")
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
ADMIN_IDS = os.getenv("ADMIN_IDS", "").strip()

# Agar .env ichida xato qilib:
# MINI_APP_URL=MINI_APP_URL=https://...
# yozilgan bo'lsa ham avtomatik tuzatadi.
if MINI_APP_URL.startswith("MINI_APP_URL="):
    MINI_APP_URL = MINI_APP_URL.split("=", 1)[1].strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi. .env faylni tekshiring.")

if not MINI_APP_URL or not MINI_APP_URL.startswith("https://"):
    raise ValueError(
        "MINI_APP_URL noto'g'ri. .env ichida "
        "MINI_APP_URL=https://...trycloudflare.com bo'lishi kerak."
    )

ADMINS = {
    int(x.strip())
    for x in ADMIN_IDS.split(",")
    if x.strip().isdigit()
}

DB = "bot.db"


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            referrer_id INTEGER,
            referrals INTEGER DEFAULT 0,
            prize INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE,
            title TEXT DEFAULT '',
            invite_link TEXT DEFAULT '',
            active INTEGER DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)

    conn.commit()
    conn.close()


def setting(key, default=""):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute("""
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


# =========================================================
# USERS / REFERRALS
# =========================================================

def add_user(user_id, username="", first_name="", referrer_id=None):
    conn = db()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username or "", first_name or "", user_id)
        )
        conn.commit()
        conn.close()
        return

    if referrer_id == user_id:
        referrer_id = None

    valid_referrer = None

    if referrer_id:
        found = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (referrer_id,)
        ).fetchone()

        if found:
            valid_referrer = referrer_id

    conn.execute("""
        INSERT INTO users
        (user_id, username, first_name, referrer_id, referrals, prize, created_at)
        VALUES (?, ?, ?, ?, 0, 0, ?)
    """, (
        user_id,
        username or "",
        first_name or "",
        valid_referrer,
        int(time.time())
    ))

    if valid_referrer:
        conn.execute(
            "UPDATE users SET referrals=referrals+1 WHERE user_id=?",
            (valid_referrer,)
        )

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# =========================================================
# TELEGRAM WEB APP SECURITY
# =========================================================

def check_webapp_data(init_data: str):
    if not init_data:
        return None

    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = data.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={data[key]}"
            for key in sorted(data)
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(data.get("auth_date", "0"))

        # WebApp ma'lumoti 24 soatgacha qabul qilinadi.
        if time.time() - auth_date > 86400:
            return None

        return json.loads(data.get("user", "{}"))

    except Exception:
        return None


def require_webapp(init_data):
    user = check_webapp_data(init_data)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Telegram ma'lumotlari noto'g'ri yoki muddati tugagan."
        )

    return user


# =========================================================
# BOT
# =========================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def main_keyboard():
    buttons = [[
        InlineKeyboardButton(
            text="🎯 AKSIYADA QATNASHISH",
            web_app=WebAppInfo(url=MINI_APP_URL)
        )
    ]]

    proof_url = setting("proof_url")

    if proof_url:
        buttons.append([InlineKeyboardButton(
            text="✅ AKSIYA ISBOTLARI",
            url=proof_url
        )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(CommandStart())
async def start_handler(message: Message):
    user = message.from_user
    args = message.text.split(maxsplit=1)

    referrer_id = None

    if len(args) > 1:
        payload = args[1].strip()

        if payload.startswith("ref_"):
            value = payload[4:]
            if value.isdigit():
                referrer_id = int(value)

    add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        referrer_id=referrer_id
    )

    await message.answer(
        f"🎉 Assalomu alaykum, {user.first_name}!\n\n"
        "💰 UZM Bank aksiyasidan foydalaning!\n\n"
        "🎯 Ruletkani aylantiring va mukofotingizni aniqlang.\n\n"
        "👇 Quyidagi tugmani bosing:",
        reply_markup=main_keyboard()
    )


# =========================================================
# ADMIN PANEL
# =========================================================

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Kanallar",
            callback_data="admin_channels"
        )],
        [InlineKeyboardButton(
            text="➕ Kanal qo'shish",
            callback_data="add_channel"
        )],
        [InlineKeyboardButton(
            text="🔗 Isbotlar havolasi",
            callback_data="set_proof"
        )],
        [InlineKeyboardButton(
            text="📊 Statistika",
            callback_data="statistics"
        )],
    ])


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id not in ADMINS:
        return

    await message.answer(
        "⚙️ ADMIN PANEL\n\nKerakli bo'limni tanlang:",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "admin_channels")
async def channels_admin(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    conn = db()
    rows = conn.execute(
        "SELECT * FROM channels ORDER BY id"
    ).fetchall()
    conn.close()

    buttons = []

    for row in rows:
        status = "🟢" if row["active"] else "🔴"

        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {row['title'] or row['chat_id']}",
                callback_data=f"channel_toggle:{row['id']}"
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"channel_delete:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="➕ Kanal qo'shish",
            callback_data="add_channel"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            text="⬅️ Admin",
            callback_data="admin_back"
        )
    ])

    await call.message.edit_text(
        "📢 MAJBURIY OBUNA KANALLARI\n\n"
        "🟢 faol\n"
        "🔴 o'chirilgan\n\n"
        "Bot kanalga administrator qilib qo'yilgan bo'lishi kerak.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    await call.message.edit_text(
        "⚙️ ADMIN PANEL\n\nKerakli bo'limni tanlang:",
        reply_markup=admin_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    set_setting(f"waiting_channel_id_{call.from_user.id}", "1")
    set_setting(f"waiting_channel_link_{call.from_user.id}", "0")

    await call.message.answer(
        "📢 KANAL QO'SHISH — 1/2\n\n"
        "Kanalning @username yoki -100... chat ID sini yuboring.\n\n"
        "Masalan:\n"
        "@mychannel\n"
        "yoki\n"
        "-1001234567890"
    )
    await call.answer()


@dp.callback_query(F.data == "set_proof")
async def set_proof(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    set_setting(f"waiting_proof_{call.from_user.id}", "1")

    await call.message.answer(
        "🔗 AKSIYA ISBOTLARI\n\n"
        "Endi isbotlar uchun havolani yuboring.\n\n"
        "Masalan:\n"
        "https://t.me/kanal"
    )
    await call.answer()


@dp.message()
async def admin_text_handler(message: Message):
    if message.from_user.id not in ADMINS:
        return

    admin_id = message.from_user.id
    text = (message.text or "").strip()

    # -----------------------------------------
    # 1-qadam: kanal ID / username
    # -----------------------------------------
    if setting(f"waiting_channel_id_{admin_id}") == "1":
        # Qulay format:
        # -1001234567890 | https://t.me/+XXXXXXXX
        # Shu formatda shaxsiy kanalni bir xabarda saqlash mumkin.
        if "|" in text:
            parts = [x.strip() for x in text.split("|", 1)]
            channel_input = parts[0]
            invite_link = parts[1]

            if not channel_input or not invite_link:
                await message.answer(
                    "❌ Format noto'g'ri.\n\n"
                    "Masalan:\n"
                    "-1001234567890 | https://t.me/+XXXXXXXX"
                )
                return

            if not (
                invite_link.startswith("https://t.me/")
                or invite_link.startswith("http://t.me/")
                or invite_link.startswith("https://telegram.me/")
                or invite_link.startswith("http://telegram.me/")
            ):
                await message.answer(
                    "❌ Invite havola noto'g'ri.\n\n"
                    "Masalan:\n"
                    "https://t.me/+XXXXXXXX"
                )
                return

            try:
                chat = await bot.get_chat(channel_input)
                title = chat.title or chat.username or str(chat.id)

                conn = db()
                conn.execute("""
                    INSERT INTO channels(chat_id, title, invite_link, active)
                    VALUES(?, ?, ?, 1)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title=excluded.title,
                        invite_link=excluded.invite_link,
                        active=1
                """, (str(chat.id), title, invite_link))
                conn.commit()
                conn.close()

                set_setting(f"waiting_channel_id_{admin_id}", "0")
                set_setting(f"waiting_channel_link_{admin_id}", "0")

                await message.answer(
                    "✅ KANAL SAQLANDI!\n\n"
                    f"📢 {title}\n"
                    f"🆔 {chat.id}\n"
                    f"🔗 {invite_link}\n\n"
                    "Mini App'da majburiy obuna sifatida chiqadi."
                )
            except Exception:
                await message.answer(
                    "❌ Kanal topilmadi.\n\n"
                    "Bot kanalga administrator qilib qo'yilganini tekshiring.\n\n"
                    "To'g'ri format:\n"
                    "-1001234567890 | https://t.me/+XXXXXXXX"
                )
            return

        # Faqat invite link yuborilgan bo'lsa, Telegram Bot API
        # undan kanal ID sini aniqlay olmaydi.
        if text.startswith(("https://t.me/+", "http://t.me/+", "https://telegram.me/+", "http://telegram.me/+")):
            await message.answer(
                "❌ Faqat shaxsiy havola bilan kanalni aniqlab bo'lmaydi.\n\n"
                "Kanal ID + havolani bitta xabarda yuboring:\n\n"
                "-1001234567890 | https://t.me/+XXXXXXXX\n\n"
                "Bot kanalga administrator qilib qo'yilgan bo'lishi kerak."
            )
            return

        try:
            chat = await bot.get_chat(text)

            title = chat.title or chat.username or str(chat.id)

            set_setting(f"pending_channel_chat_{admin_id}", str(chat.id))
            set_setting(f"pending_channel_title_{admin_id}", title)
            set_setting(f"waiting_channel_id_{admin_id}", "0")
            set_setting(f"waiting_channel_link_{admin_id}", "1")

            auto_link = ""
            if chat.username:
                auto_link = f"https://t.me/{chat.username}"

            await message.answer(
                "✅ Kanal topildi!\n\n"
                f"📢 {title}\n"
                f"🆔 {chat.id}\n\n"
                "📢 KANAL QO'SHISH — 2/2\n\n"
                "Endi kanalga kirish havolasini yuboring.\n"
                "Ommaviy kanal bo'lsa @username havolasi avtomatik ishlatiladi.\n\n"
                "Shaxsiy kanal bo'lsa:\n"
                "https://t.me/+XXXXXXXXXX\n\n"
                "Agar ommaviy kanal bo'lsa, avtomatik havola uchun "
                "AUTO deb yozishingiz mumkin."
            )

            if auto_link:
                await message.answer(
                    f"💡 Ommaviy kanal havolasi:\n{auto_link}\n\n"
                    "Xohlasangiz AUTO deb yuboring."
                )

        except Exception:
            await message.answer(
                "❌ Kanal topilmadi.\n\n"
                "Bot kanalga qo'shilgan va administrator ekanini tekshiring."
            )

        return

    # -----------------------------------------
    # 2-qadam: kanal invite link
    # -----------------------------------------
    if setting(f"waiting_channel_link_{admin_id}") == "1":
        pending_chat_id = setting(f"pending_channel_chat_{admin_id}")
        pending_title = setting(f"pending_channel_title_{admin_id}")

        if not pending_chat_id:
            set_setting(f"waiting_channel_link_{admin_id}", "0")
            await message.answer("❌ Kanal ma'lumotlari topilmadi. Qaytadan qo'shing.")
            return

        if text.upper() == "AUTO":
            try:
                chat = await bot.get_chat(pending_chat_id)
                if not chat.username:
                    await message.answer(
                        "❌ Bu kanal shaxsiy kanal.\n"
                        "AUTO ishlamaydi. Shaxsiy kanalning invite havolasini yuboring."
                    )
                    return

                invite_link = f"https://t.me/{chat.username}"

            except Exception:
                await message.answer("❌ Kanalni qayta tekshirib bo'lmadi.")
                return
        else:
            invite_link = text

            if not (
                invite_link.startswith("https://t.me/")
                or invite_link.startswith("http://t.me/")
                or invite_link.startswith("https://telegram.me/")
                or invite_link.startswith("http://telegram.me/")
            ):
                await message.answer(
                    "❌ Havola noto'g'ri.\n\n"
                    "Masalan:\n"
                    "https://t.me/+XXXXXXXXXX"
                )
                return

        conn = db()
        conn.execute("""
            INSERT INTO channels(chat_id, title, invite_link, active)
            VALUES(?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                invite_link=excluded.invite_link,
                active=1
        """, (
            pending_chat_id,
            pending_title,
            invite_link
        ))
        conn.commit()
        conn.close()

        set_setting(f"waiting_channel_link_{admin_id}", "0")
        set_setting(f"pending_channel_chat_{admin_id}", "")
        set_setting(f"pending_channel_title_{admin_id}", "")

        await message.answer(
            "✅ KANAL SAQLANDI!\n\n"
            f"📢 {pending_title}\n"
            f"🆔 {pending_chat_id}\n"
            f"🔗 {invite_link}\n\n"
            "Endi bu kanal Mini App'dagi majburiy obunada chiqadi."
        )
        return

    # -----------------------------------------
    # Isbotlar havolasi
    # -----------------------------------------
    if setting(f"waiting_proof_{admin_id}") == "1":
        if not (
            text.startswith("https://")
            or text.startswith("http://")
        ):
            await message.answer("❌ To'g'ri https:// yoki http:// havola yuboring.")
            return

        set_setting("proof_url", text)
        set_setting(f"waiting_proof_{admin_id}", "0")

        await message.answer(
            "✅ Aksiya isbotlari havolasi saqlandi."
        )


@dp.callback_query(F.data.startswith("channel_toggle:"))
async def toggle_channel(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    channel_id = int(call.data.split(":")[1])

    conn = db()
    conn.execute("""
        UPDATE channels
        SET active=CASE active WHEN 1 THEN 0 ELSE 1 END
        WHERE id=?
    """, (channel_id,))
    conn.commit()
    conn.close()

    await call.answer("Holati o'zgartirildi")
    await channels_admin(call)


@dp.callback_query(F.data.startswith("channel_delete:"))
async def delete_channel(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    channel_id = int(call.data.split(":")[1])

    conn = db()
    conn.execute(
        "DELETE FROM channels WHERE id=?",
        (channel_id,)
    )
    conn.commit()
    conn.close()

    await call.answer("Kanal o'chirildi")
    await channels_admin(call)


@dp.callback_query(F.data == "statistics")
async def statistics(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return

    conn = db()

    users = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    referrals = conn.execute(
        "SELECT COALESCE(SUM(referrals),0) FROM users"
    ).fetchone()[0]

    prizes = conn.execute(
        "SELECT COALESCE(SUM(prize),0) FROM users"
    ).fetchone()[0]

    conn.close()

    await call.message.answer(
        "📊 STATISTIKA\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"👫 Takliflar: {referrals}\n"
        f"💰 Yutuqlar jami: {prizes:,} so'm"
    )
    await call.answer()


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI()


class SpinRequest(BaseModel):
    initData: str


class ClaimRequest(BaseModel):
    initData: str


class SubscriptionRequest(BaseModel):
    initData: str


class ReferralRequest(BaseModel):
    initData: str


# ---------------------------------------------------------
# Mini App HTML
# ---------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse("index.html")


# ---------------------------------------------------------
# USER
# ---------------------------------------------------------

@app.get("/api/user")
async def api_user(initData: str):
    user = require_webapp(initData)
    user_id = int(user["id"])

    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    data = get_user(user_id)

    return {
        "ok": True,
        "user_id": user_id,
        "first_name": data["first_name"],
        "referrals": int(data["referrals"] or 0),
        "prize": int(data["prize"] or 0),
        "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    }


# ---------------------------------------------------------
# CHANNELS
# ---------------------------------------------------------

@app.get("/api/channels")
async def api_channels(initData: str):
    require_webapp(initData)

    conn = db()
    rows = conn.execute("""
        SELECT chat_id, title, invite_link
        FROM channels
        WHERE active=1
        ORDER BY id
    """).fetchall()
    conn.close()

    return [
        {
            "id": row["chat_id"],
            "title": row["title"],
            "link": row["invite_link"] or ""
        }
        for row in rows
    ]


# ---------------------------------------------------------
# SUBSCRIPTION CHECK
# ---------------------------------------------------------

async def get_subscription_status(user_id: int):
    conn = db()
    rows = conn.execute("""
        SELECT chat_id, title, invite_link
        FROM channels
        WHERE active=1
        ORDER BY id
    """).fetchall()
    conn.close()

    results = []

    for row in rows:
        subscribed = False

        try:
            member = await bot.get_chat_member(
                chat_id=row["chat_id"],
                user_id=user_id
            )

            subscribed = member.status in {
                "member",
                "administrator",
                "creator"
            }

        except Exception:
            subscribed = False

        results.append({
            "id": row["chat_id"],
            "title": row["title"],
            "link": row["invite_link"] or "",
            "subscribed": subscribed
        })

    return {
        "channels": results,
        "all_subscribed": all(x["subscribed"] for x in results)
    }


@app.get("/api/subscription")
async def api_subscription(initData: str):
    user = require_webapp(initData)
    user_id = int(user["id"])

    return await get_subscription_status(user_id)


@app.post("/api/check-subscription")
async def api_check_subscription(data: SubscriptionRequest):
    user = require_webapp(data.initData)
    user_id = int(user["id"])

    result = await get_subscription_status(user_id)

    return {
        "ok": True,
        **result
    }


# ---------------------------------------------------------
# SPIN
# ---------------------------------------------------------

PRIZES = [
    100000,
    150000,
    200000,
    250000,
    300000,
    350000,
    400000,
    450000,
    500000,
    550000,
    600000,
    650000,
    700000,
]


@app.post("/api/spin")
async def api_spin(data: SpinRequest):
    user = require_webapp(data.initData)
    user_id = int(user["id"])

    # Mini App orqali kirgan userni bazaga qo'shib qo'yamiz.
    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    conn = db()

    row = conn.execute(
        "SELECT prize FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    if row["prize"] and int(row["prize"]) > 0:
        saved_prize = int(row["prize"])
        conn.close()

        return {
            "ok": True,
            "prize": saved_prize,
            "already": True
        }

    prize = random.choice(PRIZES)

    conn.execute(
        "UPDATE users SET prize=? WHERE user_id=?",
        (prize, user_id)
    )

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "prize": prize,
        "already": False
    }


# ---------------------------------------------------------
# CLAIM
# ---------------------------------------------------------

@app.post("/api/claim")
async def api_claim(data: ClaimRequest):
    user = require_webapp(data.initData)
    user_id = int(user["id"])

    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    user_data = get_user(user_id)

    if not user_data:
        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    prize = int(user_data["prize"] or 0)

    if prize <= 0:
        raise HTTPException(
            status_code=400,
            detail="Avval ruletkani aylantiring."
        )

    subscription = await get_subscription_status(user_id)

    if not subscription["all_subscribed"]:
        return {
            "ok": True,
            "stage": "subscription",
            "prize": prize,
            **subscription
        }

    referrals = int(user_data["referrals"] or 0)

    if referrals < 10:
        return {
            "ok": True,
            "stage": "referral",
            "prize": prize,
            "referrals": referrals,
            "required": 10,
            "completed": False,
            "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        }

    return {
        "ok": True,
        "stage": "ready",
        "prize": prize,
        "referrals": referrals,
        "required": 10,
        "completed": True
    }


# ---------------------------------------------------------
# REFERRALS
# ---------------------------------------------------------

@app.get("/api/referral")
async def api_referral(initData: str):
    user = require_webapp(initData)
    user_id = int(user["id"])

    # Referral oynasi /api/user dan oldin ochilsa ham
    # foydalanuvchini avtomatik bazaga qo'shamiz.
    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    data = get_user(user_id)

    if not data:
        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    referrals = int(data["referrals"] or 0)

    return {
        "ok": True,
        "referrals": referrals,
        "required": 10,
        "completed": referrals >= 10,
        "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    }


@app.post("/api/check-referrals")
async def api_check_referrals(data: ReferralRequest):
    user = require_webapp(data.initData)
    user_id = int(user["id"])

    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    user_data = get_user(user_id)

    if not user_data:
        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    referrals = int(user_data["referrals"] or 0)

    return {
        "ok": True,
        "referrals": referrals,
        "required": 10,
        "completed": referrals >= 10,
        "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    }


# ---------------------------------------------------------
# FINAL CLAIM STATUS
# ---------------------------------------------------------

@app.post("/api/claim-status")
async def api_claim_status(data: ClaimRequest):
    user = require_webapp(data.initData)
    user_id = int(user["id"])

    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    user_data = get_user(user_id)

    if not user_data:
        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    prize = int(user_data["prize"] or 0)
    referrals = int(user_data["referrals"] or 0)

    subscription = await get_subscription_status(user_id)

    if not subscription["all_subscribed"]:
        stage = "subscription"
    elif referrals < 10:
        stage = "referral"
    else:
        stage = "ready"

    return {
        "ok": True,
        "stage": stage,
        "prize": prize,
        "referrals": referrals,
        "required": 10,
        "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}",
        **subscription
    }


# =========================================================
# SERVER
# =========================================================

async def run_bot():
    print("🤖 BOT ISHLAYAPTI!")
    await dp.start_polling(bot)


async def run_api():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="info"
    )

    server = uvicorn.Server(config)
    await server.serve()


async def main():
    init_db()

    print("🤖 BOT ISHLAYAPTI!")
    print("🌐 MINI APP:", MINI_APP_URL)

    await asyncio.gather(
        run_bot(),
        run_api()
    )


if __name__ == "__main__":
    asyncio.run(main())
