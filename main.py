#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AvtoReklama — Telegram avto-reklama boti
Foydalanuvchi o'z akkauntini QR orqali ulaydi va tanlangan guruhlarga
belgilangan intervalda avtomatik xabar yuboradi.

Texnologiya: aiogram 3.x (boshqaruv boti) + Telethon (userbot/QR ulash) + SQLite
"""

import asyncio
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, UserDeactivatedBanError,
    ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError,
    SlowModeWaitError,
)
from telethon.tl.types import LoginToken

import qrcode
from io import BytesIO
import os

# ============================================================
#  SOZLAMALAR (shu yerga o'zingizning ma'lumotlaringizni kiriting)
# ============================================================

BOT_TOKEN = "8541076638:AAF6kxwTA89-RlybB-8j0KmeQLR04M2vxsA"
API_ID = 38048373
API_HASH = "864d949f4d06ed60b349a9e11f20fde2"

# Admin ID'lar: birinchisi yashirin super-admin (statistikada ko'rinmaydi),
# qolganlari oddiy adminlar (mijoz va h.k.)
SUPER_ADMIN_ID = 8311219981      # Sizning ID'ingiz — yashirin, lekin to'liq huquqli
VISIBLE_ADMIN_IDS = [6293975283]  # Mijoz admin ID — statistikada ko'rinadi

ALL_ADMIN_IDS = {SUPER_ADMIN_ID, *VISIBLE_ADMIN_IDS}

DB_PATH = "avtoreklama.db"

GROUP_SAFE_LIMIT = 50      # shu songacha ogohlantirishsiz
GROUP_MAX_LIMIT = 120      # undan ortiq ruxsat etilmaydi

MIN_INTERVAL_SEC = 60        # eng kichik interval (1 daqiqa)
DEFAULT_INTERVAL_SEC = 300   # standart interval (5 daqiqa)

QR_LOGIN_TIMEOUT = 180  # 3 daqiqa

MEDIA_DIR = "media"  # yuklab olingan media fayllar shu papkada saqlanadi
os.makedirs(MEDIA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("avtoreklama")

# ============================================================
#  BAZA (SQLite)
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT DEFAULT 'uz',
            session_string TEXT,
            account_name TEXT,
            account_username TEXT,
            is_premium INTEGER DEFAULT 0,
            message_text TEXT,
            media_file_id TEXT,
            media_type TEXT,          -- photo / video / document / audio / voice / video_note
            premium_emoji INTEGER DEFAULT 1,
            interval_sec INTEGER DEFAULT 300,
            is_running INTEGER DEFAULT 0,
            run_until INTEGER,        -- unix timestamp, NULL = cheksiz
            created_at INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id INTEGER,
            chat_id INTEGER,
            title TEXT,
            selected INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id INTEGER,
            sent_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def db_get_user(user_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def db_ensure_user(user_id: int):
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)",
        (user_id, int(time.time())),
    )
    conn.execute(
        "INSERT OR IGNORE INTO stats (user_id, sent_count, error_count) VALUES (?, 0, 0)",
        (user_id,),
    )
    conn.commit()
    conn.close()


def db_set_lang(user_id: int, lang: str):
    conn = db_connect()
    conn.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))
    conn.commit()
    conn.close()


def db_set_session(user_id: int, session_string, name, username, is_premium):
    conn = db_connect()
    conn.execute(
        "UPDATE users SET session_string=?, account_name=?, account_username=?, is_premium=? WHERE user_id=?",
        (session_string, name, username, int(is_premium), user_id),
    )
    conn.commit()
    conn.close()


def db_clear_session(user_id: int):
    conn = db_connect()
    row = conn.execute("SELECT media_file_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["media_file_id"] and os.path.exists(row["media_file_id"]):
        try:
            os.remove(row["media_file_id"])
        except Exception:
            pass
    conn.execute(
        """UPDATE users SET session_string=NULL, account_name=NULL, account_username=NULL,
           is_premium=0, is_running=0, run_until=NULL, message_text=NULL,
           media_file_id=NULL, media_type=NULL WHERE user_id=?""",
        (user_id,),
    )
    conn.execute("DELETE FROM user_groups WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def db_set_message(user_id: int, text, media_file_id, media_type):
    conn = db_connect()
    conn.execute(
        "UPDATE users SET message_text=?, media_file_id=?, media_type=? WHERE user_id=?",
        (text, media_file_id, media_type, user_id),
    )
    conn.commit()
    conn.close()


def db_toggle_premium_emoji(user_id: int, value: bool):
    conn = db_connect()
    conn.execute("UPDATE users SET premium_emoji=? WHERE user_id=?", (int(value), user_id))
    conn.commit()
    conn.close()


def db_set_interval(user_id: int, seconds: int):
    conn = db_connect()
    conn.execute("UPDATE users SET interval_sec=? WHERE user_id=?", (seconds, user_id))
    conn.commit()
    conn.close()


def db_save_groups(user_id: int, groups: list):
    """groups: list of (chat_id, title)"""
    conn = db_connect()
    conn.execute("DELETE FROM user_groups WHERE user_id=?", (user_id,))
    for chat_id, title in groups:
        conn.execute(
            "INSERT INTO user_groups (user_id, chat_id, title, selected) VALUES (?, ?, ?, 0)",
            (user_id, chat_id, title),
        )
    conn.commit()
    conn.close()


def db_toggle_group(user_id: int, chat_id: int):
    conn = db_connect()
    row = conn.execute(
        "SELECT selected FROM user_groups WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    if row:
        new_val = 0 if row["selected"] else 1
        conn.execute(
            "UPDATE user_groups SET selected=? WHERE user_id=? AND chat_id=?",
            (new_val, user_id, chat_id),
        )
    conn.commit()
    conn.close()


def db_select_all_groups(user_id: int, select: bool):
    conn = db_connect()
    conn.execute(
        "UPDATE user_groups SET selected=? WHERE user_id=?", (int(select), user_id)
    )
    conn.commit()
    conn.close()


def db_get_groups(user_id: int):
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM user_groups WHERE user_id=? ORDER BY title", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def db_get_selected_groups(user_id: int):
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM user_groups WHERE user_id=? AND selected=1", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def db_set_running(user_id: int, running: bool, run_until=None):
    conn = db_connect()
    conn.execute(
        "UPDATE users SET is_running=?, run_until=? WHERE user_id=?",
        (int(running), run_until, user_id),
    )
    conn.commit()
    conn.close()


def db_get_all_running_users():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM users WHERE is_running=1").fetchall()
    conn.close()
    return rows


def db_get_all_users():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def db_bump_stat(user_id: int, sent=0, error=0):
    conn = db_connect()
    conn.execute(
        "UPDATE stats SET sent_count = sent_count + ?, error_count = error_count + ? WHERE user_id=?",
        (sent, error, user_id),
    )
    conn.commit()
    conn.close()


def db_get_stats(user_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM stats WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


# ============================================================
#  TILLAR (lug'at)
# ============================================================

T = {
    "uz": {
        "choose_lang": "👋 Assalomu alaykum! Xush kelibsiz!\nIltimos, tilni tanlang:",
        "welcome": "👋 Xush kelibsiz, {name}!\n\nBu bot orqali siz akkauntingizni ulab, tanlagan guruhlaringizga avtomatik reklama yubora olasiz.",
        "menu_prompt": "Menyudan birini tanlang:",
        "btn_connect": "🔗 Akkauntni ulash",
        "btn_disconnect": "🗑 Akkauntni uzish",
        "btn_account": "👤 Akkauntim",
        "btn_settings": "⚙️ Sozlamalar",
        "btn_start_job": "▶️ Ishga tushirish",
        "btn_stop_job": "⏸ To'xtatish",
        "btn_help": "ℹ️ Yordam",
        "btn_back": "⬅️ Orqaga",
        "qr_instruction": "📱 Telegram ilovangizda:\nSozlamalar → Qurilmalar → Qurilma qo'shish\n\n...va shu QR kodni skanerlang.\n\n⏱ {sec} soniya vaqtingiz bor.",
        "qr_timeout": "⏱ Vaqt tugadi. Qaytadan urinib ko'ring.",
        "qr_wrong_account": "❌ Bu sizning akkauntingiz emas. Ulash bekor qilindi.\nFaqat o'zingizning akkauntingizni ulashingiz mumkin.",
        "qr_need_password": "🔒 Akkauntingizda ikki bosqichli tekshiruv (parol) yoqilgan.\nIltimos, parolingizni yuboring:",
        "qr_wrong_password": "❌ Parol noto'g'ri. Qaytadan urinib ko'ring yoki /start bosing.",
        "connected_ok": "✅ Akkaunt ulandi!\n👤 {name} (@{username})\n{premium}",
        "already_connected": "⚠️ Sizda allaqachon ulangan akkaunt bor. Avval uni uzing.",
        "not_connected": "❌ Hali akkaunt ulanmagan.",
        "disconnect_confirm": "🗑 Akkauntni uzmoqchimisiz? Barcha sozlamalar (matn, guruhlar) ham o'chadi.",
        "disconnect_yes": "✅ Ha, uzish",
        "disconnect_no": "❌ Yo'q",
        "disconnected": "✅ Akkaunt uzildi.",
        "account_status": "👤 Akkaunt holati:\n\n{status}",
        "status_active": "✅ Faol (ulangan: {name})",
        "status_stopped": "⏸ To'xtagan",
        "status_none": "❌ Ulanmagan",
        "settings_menu": "⚙️ Sozlamalar:",
        "btn_text": "📝 Xabar matni",
        "btn_premium_emoji": "😀 Premium emoji",
        "btn_groups": "👥 Guruhlar",
        "btn_interval": "⏱ Interval",
        "ask_message": "✍️ Reklama xabaringizni yuboring:\n(matn, rasm, video, fayl, audio yoki ovozli xabar — qaysi birini xohlasangiz, shuni yuboring)",
        "message_saved": "✅ Xabar saqlandi!",
        "emoji_on": "😀 Premium emoji: ✅ Yoqilgan",
        "emoji_off": "😀 Premium emoji: ⬜ O'chirilgan",
        "ask_interval": "⏱ Necha daqiqada bitta xabar yuborilsin? (raqam kiriting, masalan: 5)",
        "interval_saved": "✅ Interval saqlandi: har {min} daqiqada",
        "interval_invalid": "❗ Iltimos, to'g'ri raqam kiriting (kamida 1 daqiqa).",
        "groups_header": "👥 Guruhlaringiz ({page}/{total_pages}-bet):\n\nTanlangan: {selected}/{max}",
        "groups_warning": "\n⚠️ 50 tadan ortiq tanlash bloklanish xavfini oshiradi!",
        "groups_max_reached": "❌ Maksimal {max} ta guruh tanlash mumkin.",
        "groups_empty": "❌ Akkauntingiz hech qanday guruhda emas.",
        "btn_select_all": "✅ Hammasini belgilash",
        "btn_deselect_all": "❌ Hammasini bekor qilish",
        "btn_save": "💾 Saqlash",
        "groups_saved": "✅ Guruhlar saqlandi.",
        "btn_prev": "⬅️ Oldingi",
        "btn_next": "Keyingi ➡️",
        "need_text_and_groups": "❗ Avval xabar matni (yoki media) va kamida 1 ta guruh tanlang.",
        "ask_duration": "⏰ Qachongacha ishlasin?",
        "btn_unlimited": "♾️ To'xtatmaguncha",
        "btn_until_time": "🕐 Belgilangan vaqtgacha",
        "ask_minutes": "🕐 Necha daqiqa ishlasin? (raqam kiriting, masalan: 180)",
        "job_started": "✅ Yuborish boshlandi!\n👥 Guruhlar: {count}\n⏱ Interval: {interval} daqiqa",
        "job_started_until": "\n⏰ Tugash: {until}",
        "job_stopped": "⏸ To'xtatildi.",
        "job_not_running": "⚠️ Hozir hech narsa ishlamayapti.",
        "job_time_up": "⏰ Belgilangan vaqt tugadi, reklama to'xtatildi.",
        "send_error": "⚠️ \"{title}\" guruhiga yuborib bo'lmadi: {reason}",
        "help_text": "ℹ️ <b>Yordam</b>\n\n1️⃣ Avval akkauntingizni ulang (QR orqali)\n2️⃣ Sozlamalarda xabar matni/media va guruhlarni tanlang\n3️⃣ Intervalni belgilang\n4️⃣ \"Ishga tushirish\" bosing\n\nSavollar bo'lsa, admin bilan bog'laning.",
        "lang_name": "O'zbek",
    },
    "ru": {
        "choose_lang": "👋 Добро пожаловать!\nПожалуйста, выберите язык:",
        "welcome": "👋 Добро пожаловать, {name}!\n\nЭтот бот позволяет подключить аккаунт и автоматически отправлять рекламу в выбранные группы.",
        "menu_prompt": "Выберите пункт меню:",
        "btn_connect": "🔗 Подключить аккаунт",
        "btn_disconnect": "🗑 Отключить аккаунт",
        "btn_account": "👤 Мой аккаунт",
        "btn_settings": "⚙️ Настройки",
        "btn_start_job": "▶️ Запустить",
        "btn_stop_job": "⏸ Остановить",
        "btn_help": "ℹ️ Помощь",
        "btn_back": "⬅️ Назад",
        "qr_instruction": "📱 В вашем Telegram:\nНастройки → Устройства → Подключить устройство\n\n...и отсканируйте этот QR-код.\n\n⏱ У вас есть {sec} секунд.",
        "qr_timeout": "⏱ Время истекло. Попробуйте снова.",
        "qr_wrong_account": "❌ Это не ваш аккаунт. Подключение отменено.\nВы можете подключить только свой собственный аккаунт.",
        "qr_need_password": "🔒 На вашем аккаунте включена двухфакторная защита.\nПожалуйста, отправьте пароль:",
        "qr_wrong_password": "❌ Неверный пароль. Попробуйте снова или нажмите /start.",
        "connected_ok": "✅ Аккаунт подключен!\n👤 {name} (@{username})\n{premium}",
        "already_connected": "⚠️ У вас уже есть подключенный аккаунт. Сначала отключите его.",
        "not_connected": "❌ Аккаунт еще не подключен.",
        "disconnect_confirm": "🗑 Отключить аккаунт? Все настройки (текст, группы) также будут удалены.",
        "disconnect_yes": "✅ Да, отключить",
        "disconnect_no": "❌ Нет",
        "disconnected": "✅ Аккаунт отключен.",
        "account_status": "👤 Статус аккаунта:\n\n{status}",
        "status_active": "✅ Активен (подключен: {name})",
        "status_stopped": "⏸ Остановлен",
        "status_none": "❌ Не подключен",
        "settings_menu": "⚙️ Настройки:",
        "btn_text": "📝 Текст сообщения",
        "btn_premium_emoji": "😀 Премиум эмодзи",
        "btn_groups": "👥 Группы",
        "btn_interval": "⏱ Интервал",
        "ask_message": "✍️ Отправьте ваше рекламное сообщение:\n(текст, фото, видео, файл, аудио или голосовое — что угодно)",
        "message_saved": "✅ Сообщение сохранено!",
        "emoji_on": "😀 Премиум эмодзи: ✅ Включено",
        "emoji_off": "😀 Премиум эмодзи: ⬜ Выключено",
        "ask_interval": "⏱ Через сколько минут отправлять сообщение? (введите число, например: 5)",
        "interval_saved": "✅ Интервал сохранен: каждые {min} мин.",
        "interval_invalid": "❗ Введите корректное число (минимум 1 минута).",
        "groups_header": "👥 Ваши группы (стр. {page}/{total_pages}):\n\nВыбрано: {selected}/{max}",
        "groups_warning": "\n⚠️ Выбор более 50 групп повышает риск блокировки!",
        "groups_max_reached": "❌ Максимум {max} групп можно выбрать.",
        "groups_empty": "❌ Ваш аккаунт не состоит ни в одной группе.",
        "btn_select_all": "✅ Выбрать все",
        "btn_deselect_all": "❌ Снять все",
        "btn_save": "💾 Сохранить",
        "groups_saved": "✅ Группы сохранены.",
        "btn_prev": "⬅️ Назад",
        "btn_next": "Далее ➡️",
        "need_text_and_groups": "❗ Сначала задайте текст (или медиа) и выберите хотя бы 1 группу.",
        "ask_duration": "⏰ До какого времени работать?",
        "btn_unlimited": "♾️ Бессрочно",
        "btn_until_time": "🕐 До указанного времени",
        "ask_minutes": "🕐 Сколько минут работать? (например: 180)",
        "job_started": "✅ Рассылка запущена!\n👥 Групп: {count}\n⏱ Интервал: {interval} мин.",
        "job_started_until": "\n⏰ Окончание: {until}",
        "job_stopped": "⏸ Остановлено.",
        "job_not_running": "⚠️ Сейчас ничего не запущено.",
        "job_time_up": "⏰ Время истекло, рассылка остановлена.",
        "send_error": "⚠️ Не удалось отправить в \"{title}\": {reason}",
        "help_text": "ℹ️ <b>Помощь</b>\n\n1️⃣ Подключите аккаунт (через QR)\n2️⃣ В настройках задайте текст/медиа и группы\n3️⃣ Укажите интервал\n4️⃣ Нажмите \"Запустить\"\n\nПо вопросам обращайтесь к админу.",
        "lang_name": "Русский",
    },
    "en": {
        "choose_lang": "👋 Welcome!\nPlease select a language:",
        "welcome": "👋 Welcome, {name}!\n\nThis bot lets you connect your account and automatically send ads to your selected groups.",
        "menu_prompt": "Choose an option from the menu:",
        "btn_connect": "🔗 Connect account",
        "btn_disconnect": "🗑 Disconnect account",
        "btn_account": "👤 My account",
        "btn_settings": "⚙️ Settings",
        "btn_start_job": "▶️ Start",
        "btn_stop_job": "⏸ Stop",
        "btn_help": "ℹ️ Help",
        "btn_back": "⬅️ Back",
        "qr_instruction": "📱 In your Telegram app:\nSettings → Devices → Link Desktop Device\n\n...then scan this QR code.\n\n⏱ You have {sec} seconds.",
        "qr_timeout": "⏱ Time's up. Please try again.",
        "qr_wrong_account": "❌ This is not your account. Connection cancelled.\nYou can only connect your own account.",
        "qr_need_password": "🔒 Two-step verification is enabled on this account.\nPlease send your password:",
        "qr_wrong_password": "❌ Wrong password. Try again or press /start.",
        "connected_ok": "✅ Account connected!\n👤 {name} (@{username})\n{premium}",
        "already_connected": "⚠️ You already have a connected account. Disconnect it first.",
        "not_connected": "❌ No account connected yet.",
        "disconnect_confirm": "🗑 Disconnect account? All settings (text, groups) will also be deleted.",
        "disconnect_yes": "✅ Yes, disconnect",
        "disconnect_no": "❌ No",
        "disconnected": "✅ Account disconnected.",
        "account_status": "👤 Account status:\n\n{status}",
        "status_active": "✅ Active ({name})",
        "status_stopped": "⏸ Stopped",
        "status_none": "❌ Not connected",
        "settings_menu": "⚙️ Settings:",
        "btn_text": "📝 Message text",
        "btn_premium_emoji": "😀 Premium emoji",
        "btn_groups": "👥 Groups",
        "btn_interval": "⏱ Interval",
        "ask_message": "✍️ Send your ad message:\n(text, photo, video, file, audio, or voice — anything)",
        "message_saved": "✅ Message saved!",
        "emoji_on": "😀 Premium emoji: ✅ On",
        "emoji_off": "😀 Premium emoji: ⬜ Off",
        "ask_interval": "⏱ Send a message every how many minutes? (enter a number, e.g. 5)",
        "interval_saved": "✅ Interval saved: every {min} min",
        "interval_invalid": "❗ Please enter a valid number (minimum 1 minute).",
        "groups_header": "👥 Your groups (page {page}/{total_pages}):\n\nSelected: {selected}/{max}",
        "groups_warning": "\n⚠️ Selecting more than 50 groups increases ban risk!",
        "groups_max_reached": "❌ You can select at most {max} groups.",
        "groups_empty": "❌ Your account is not a member of any group.",
        "btn_select_all": "✅ Select all",
        "btn_deselect_all": "❌ Deselect all",
        "btn_save": "💾 Save",
        "groups_saved": "✅ Groups saved.",
        "btn_prev": "⬅️ Prev",
        "btn_next": "Next ➡️",
        "need_text_and_groups": "❗ First set a message (or media) and select at least 1 group.",
        "ask_duration": "⏰ Run until when?",
        "btn_unlimited": "♾️ Until stopped",
        "btn_until_time": "🕐 For a set duration",
        "ask_minutes": "🕐 How many minutes should it run? (e.g. 180)",
        "job_started": "✅ Sending started!\n👥 Groups: {count}\n⏱ Interval: {interval} min",
        "job_started_until": "\n⏰ Ends at: {until}",
        "job_stopped": "⏸ Stopped.",
        "job_not_running": "⚠️ Nothing is running right now.",
        "job_time_up": "⏰ Time's up, ad sending stopped.",
        "send_error": "⚠️ Failed to send to \"{title}\": {reason}",
        "help_text": "ℹ️ <b>Help</b>\n\n1️⃣ Connect your account (via QR)\n2️⃣ Set message text/media and groups in settings\n3️⃣ Set the interval\n4️⃣ Press \"Start\"\n\nContact the admin for questions.",
        "lang_name": "English",
    },
}


def tr(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in T else "uz"
    text = T[lang].get(key, T["uz"].get(key, key))
    return text.format(**kwargs) if kwargs else text


# ============================================================
#  KLAVIATURALAR
# ============================================================

def kb_lang_select() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="lang:uz")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")],
    ])


def kb_main_menu(lang: str, connected: bool) -> ReplyKeyboardMarkup:
    connect_btn = tr(lang, "btn_disconnect") if connected else tr(lang, "btn_connect")
    rows = [
        [KeyboardButton(text=connect_btn), KeyboardButton(text=tr(lang, "btn_account"))],
        [KeyboardButton(text=tr(lang, "btn_settings")), KeyboardButton(text=tr(lang, "btn_start_job"))],
        [KeyboardButton(text=tr(lang, "btn_help"))],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_settings(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr(lang, "btn_text"), callback_data="set:text")],
        [InlineKeyboardButton(text=tr(lang, "btn_premium_emoji"), callback_data="set:emoji")],
        [InlineKeyboardButton(text=tr(lang, "btn_groups"), callback_data="set:groups")],
        [InlineKeyboardButton(text=tr(lang, "btn_interval"), callback_data="set:interval")],
    ])


def kb_disconnect_confirm(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr(lang, "disconnect_yes"), callback_data="disc:yes")],
        [InlineKeyboardButton(text=tr(lang, "disconnect_no"), callback_data="disc:no")],
    ])


def kb_duration(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr(lang, "btn_unlimited"), callback_data="dur:unlimited")],
        [InlineKeyboardButton(text=tr(lang, "btn_until_time"), callback_data="dur:timed")],
    ])


GROUPS_PER_PAGE = 10


def kb_groups(lang: str, user_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    groups = db_get_groups(user_id)
    total = len(groups)
    selected_count = sum(1 for g in groups if g["selected"])
    total_pages = max(1, (total + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * GROUPS_PER_PAGE
    page_groups = groups[start:start + GROUPS_PER_PAGE]

    header = tr(lang, "groups_header", page=page + 1, total_pages=total_pages,
                selected=selected_count, max=GROUP_MAX_LIMIT)
    if selected_count > GROUP_SAFE_LIMIT:
        header += tr(lang, "groups_warning")

    rows = []
    for g in page_groups:
        mark = "☑️" if g["selected"] else "⬜"
        title = g["title"][:35]
        rows.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"grp:{g['chat_id']}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=tr(lang, "btn_prev"), callback_data=f"grppage:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text=tr(lang, "btn_next"), callback_data=f"grppage:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text=tr(lang, "btn_select_all"), callback_data=f"grpall:1:{page}"),
        InlineKeyboardButton(text=tr(lang, "btn_deselect_all"), callback_data=f"grpall:0:{page}"),
    ])
    rows.append([InlineKeyboardButton(text=tr(lang, "btn_save"), callback_data="grpsave")])

    return header, InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
#  FSM HOLATLARI
# ============================================================

class Form(StatesGroup):
    waiting_password = State()
    waiting_message = State()
    waiting_interval = State()
    waiting_duration_minutes = State()


# ============================================================
#  USERBOT MANAGER (Telethon)
# ============================================================

running_jobs: dict[int, asyncio.Task] = {}  # user_id -> sender task
qr_login_tasks: dict[int, asyncio.Task] = {}  # user_id -> qr task


async def do_qr_login(bot: Bot, chat_id: int, user_id: int, lang: str):
    """QR login jarayonini boshqaradi: QR yuboradi, kutadi, user_id tekshiradi."""
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        qr_login = await client.qr_login()

        # QR kodni rasm sifatida yaratish
        qr_img = qrcode.make(qr_login.url)
        buf = BytesIO()
        qr_img.save(buf, format="PNG")
        buf.seek(0)

        photo = BufferedInputFile(buf.read(), filename="qr.png")
        msg = await bot.send_photo(
            chat_id, photo,
            caption=tr(lang, "qr_instruction", sec=QR_LOGIN_TIMEOUT)
        )

        try:
            await asyncio.wait_for(qr_login.wait(), timeout=QR_LOGIN_TIMEOUT)
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, tr(lang, "qr_timeout"))
            await client.disconnect()
            return
        except SessionPasswordNeededError:
            # 2FA kerak
            await bot.send_message(chat_id, tr(lang, "qr_need_password"))
            dp_fsm_storage = STORAGE  # global storage, quyida aniqlanadi
            key = _fsm_key(chat_id, user_id)
            await dp_fsm_storage.set_state(key, Form.waiting_password)
            await dp_fsm_storage.set_data(key, {"_client_session": client.session.save()})
            await client.disconnect()
            return

        # Muvaffaqiyatli kirildi — akkaunt ma'lumotlarini olish
        me = await client.get_me()

        if me.id != user_id:
            await bot.send_message(chat_id, tr(lang, "qr_wrong_account"))
            await client.log_out()
            await client.disconnect()
            return

        session_str = client.session.save()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "—"
        username = me.username or "—"
        is_premium = bool(getattr(me, "premium", False))

        db_set_session(user_id, session_str, name, username, is_premium)

        premium_line = "⭐ Telegram Premium" if is_premium else ""
        await bot.send_message(
            chat_id,
            tr(lang, "connected_ok", name=name, username=username, premium=premium_line),
            reply_markup=kb_main_menu(lang, connected=True),
        )

        # Guruhlar ro'yxatini oldindan yuklab olish
        await refresh_user_groups(client, user_id)

        await client.disconnect()

    except Exception as e:
        log.exception("QR login xatosi: %s", e)
        try:
            await bot.send_message(chat_id, f"❌ Xatolik: {e}")
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass


async def finish_qr_login_with_password(bot: Bot, chat_id: int, user_id: int, lang: str, session_str: str, password: str):
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(password=password)
        me = await client.get_me()

        if me.id != user_id:
            await bot.send_message(chat_id, tr(lang, "qr_wrong_account"))
            await client.log_out()
            await client.disconnect()
            return

        session_str_final = client.session.save()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "—"
        username = me.username or "—"
        is_premium = bool(getattr(me, "premium", False))

        db_set_session(user_id, session_str_final, name, username, is_premium)

        premium_line = "⭐ Telegram Premium" if is_premium else ""
        await bot.send_message(
            chat_id,
            tr(lang, "connected_ok", name=name, username=username, premium=premium_line),
            reply_markup=kb_main_menu(lang, connected=True),
        )
        await refresh_user_groups(client, user_id)
        await client.disconnect()

    except Exception:
        await bot.send_message(chat_id, tr(lang, "qr_wrong_password"))
        try:
            await client.disconnect()
        except Exception:
            pass


async def refresh_user_groups(client: TelegramClient, user_id: int):
    """Akkaunt a'zo bo'lgan guruh/superguruhlarni bazaga yozadi (shaxsiy chat va kanallarsiz)."""
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group:
            groups.append((dialog.id, dialog.name or str(dialog.id)))
    db_save_groups(user_id, groups)


async def get_client_for_user(user_id: int) -> TelegramClient | None:
    user = db_get_user(user_id)
    if not user or not user["session_string"]:
        return None
    client = TelegramClient(StringSession(user["session_string"]), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    return client


# ============================================================
#  REKLAMA YUBORISH FON JARAYONI
# ============================================================

async def sender_loop(bot: Bot, user_id: int):
    user = db_get_user(user_id)
    if not user:
        return
    lang = user["lang"]

    client = await get_client_for_user(user_id)
    if not client:
        db_set_running(user_id, False)
        return

    interval = user["interval_sec"] or DEFAULT_INTERVAL_SEC
    run_until = user["run_until"]

    try:
        while True:
            user = db_get_user(user_id)
            if not user or not user["is_running"]:
                break

            if run_until and time.time() >= run_until:
                db_set_running(user_id, False, None)
                try:
                    await bot.send_message(user_id, tr(lang, "job_time_up"))
                except Exception:
                    pass
                break

            groups = db_get_selected_groups(user_id)
            if not groups:
                break

            for g in groups:
                user_check = db_get_user(user_id)
                if not user_check or not user_check["is_running"]:
                    break

                try:
                    media_path = user["media_file_id"]  # bu ustunda endi local fayl yo'li saqlanadi
                    caption = user["message_text"] or ""

                    if media_path and os.path.exists(media_path):
                        if user["media_type"] == "voice":
                            await client.send_file(g["chat_id"], media_path, voice_note=True, caption=caption)
                        elif user["media_type"] == "video_note":
                            await client.send_file(g["chat_id"], media_path, video_note=True)
                        else:
                            await client.send_file(g["chat_id"], media_path, caption=caption)
                    else:
                        await client.send_message(g["chat_id"], caption)
                    db_bump_stat(user_id, sent=1)

                except FloodWaitError as e:
                    log.warning("FloodWait %s soniya, user=%s", e.seconds, user_id)
                    await asyncio.sleep(min(e.seconds, 600))
                except SlowModeWaitError as e:
                    db_bump_stat(user_id, error=1)
                    try:
                        await bot.send_message(user_id, tr(lang, "send_error", title=g["title"], reason="slow mode"))
                    except Exception:
                        pass
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError) as e:
                    db_bump_stat(user_id, error=1)
                    try:
                        await bot.send_message(user_id, tr(lang, "send_error", title=g["title"], reason=str(e)))
                    except Exception:
                        pass
                except UserDeactivatedBanError:
                    db_set_running(user_id, False, None)
                    try:
                        await bot.send_message(user_id, "🚫 Akkaunt bloklangan.")
                    except Exception:
                        pass
                    return
                except Exception as e:
                    db_bump_stat(user_id, error=1)
                    log.exception("Yuborish xatosi: %s", e)

                # intervaldan kichik tasodifiy pauza (xabarlar orasida)
                jitter = random.uniform(2, 8)
                await asyncio.sleep(jitter)

                # navbatdagi guruhgacha to'liq intervalni kutish
                remaining = interval - jitter
                if remaining > 0:
                    await asyncio.sleep(remaining)

    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()


def start_sender_task(bot: Bot, user_id: int):
    if user_id in running_jobs and not running_jobs[user_id].done():
        return
    task = asyncio.create_task(sender_loop(bot, user_id))
    running_jobs[user_id] = task


def stop_sender_task(user_id: int):
    db_set_running(user_id, False, None)
    task = running_jobs.get(user_id)
    if task and not task.done():
        task.cancel()


# ============================================================
#  AIOGRAM BOT / DISPATCHER
# ============================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
STORAGE = MemoryStorage()
dp = Dispatcher(storage=STORAGE)
router = Router()
dp.include_router(router)


def _fsm_key(chat_id: int, user_id: int):
    from aiogram.fsm.storage.base import StorageKey
    return StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)


def get_lang(user_id: int) -> str:
    user = db_get_user(user_id)
    return user["lang"] if user else "uz"


def is_connected(user_id: int) -> bool:
    user = db_get_user(user_id)
    return bool(user and user["session_string"])


# ----------------- /start -----------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    db_ensure_user(user_id)
    user = db_get_user(user_id)

    if user["lang"] is None:
        await message.answer(tr("uz", "choose_lang"), reply_markup=kb_lang_select())
        return

    lang = user["lang"]
    connected = bool(user["session_string"])
    await message.answer(tr(lang, "menu_prompt"), reply_markup=kb_main_menu(lang, connected))


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    lang = call.data.split(":")[1]
    user_id = call.from_user.id
    db_ensure_user(user_id)
    db_set_lang(user_id, lang)

    name = call.from_user.first_name or ""
    await call.message.delete()
    await call.message.answer(tr(lang, "welcome", name=name))
    connected = is_connected(user_id)
    await call.message.answer(tr(lang, "menu_prompt"), reply_markup=kb_main_menu(lang, connected))
    await call.answer()


# ----------------- Akkauntni ulash / uzish -----------------

@router.message(F.text.in_([tr(l, "btn_connect") for l in T]))
async def msg_connect(message: Message, state: FSMContext):
    user_id = message.from_user.id
    lang = get_lang(user_id)

    if is_connected(user_id):
        await message.answer(tr(lang, "already_connected"))
        return

    # Eski QR vazifasi bo'lsa bekor qilish
    old = qr_login_tasks.get(user_id)
    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(do_qr_login(bot, message.chat.id, user_id, lang))
    qr_login_tasks[user_id] = task


@router.message(F.text.in_([tr(l, "btn_disconnect") for l in T]))
async def msg_disconnect(message: Message):
    user_id = message.from_user.id
    lang = get_lang(user_id)
    if not is_connected(user_id):
        await message.answer(tr(lang, "not_connected"))
        return
    await message.answer(tr(lang, "disconnect_confirm"), reply_markup=kb_disconnect_confirm(lang))


@router.callback_query(F.data == "disc:yes")
async def cb_disconnect_yes(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)
    stop_sender_task(user_id)
    db_clear_session(user_id)
    await call.message.edit_text(tr(lang, "disconnected"))
    await call.message.answer(tr(lang, "menu_prompt"), reply_markup=kb_main_menu(lang, connected=False))
    await call.answer()


@router.callback_query(F.data == "disc:no")
async def cb_disconnect_no(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


@router.message(Form.waiting_password)
async def process_password(message: Message, state: FSMContext):
    user_id = message.from_user.id
    lang = get_lang(user_id)
    data = await state.get_data()
    session_str = data.get("_client_session")
    await state.clear()

    if not session_str:
        await message.answer(tr(lang, "qr_wrong_password"))
        return

    await finish_qr_login_with_password(bot, message.chat.id, user_id, lang, session_str, message.text.strip())


# ----------------- Akkauntim -----------------

@router.message(F.text.in_([tr(l, "btn_account") for l in T]))
async def msg_account(message: Message):
    user_id = message.from_user.id
    lang = get_lang(user_id)
    user = db_get_user(user_id)

    if not user or not user["session_string"]:
        status = tr(lang, "status_none")
    elif user["is_running"]:
        status = tr(lang, "status_active", name=user["account_name"])
    else:
        status = tr(lang, "status_stopped")

    await message.answer(tr(lang, "account_status", status=status))


# ----------------- Sozlamalar -----------------

@router.message(F.text.in_([tr(l, "btn_settings") for l in T]))
async def msg_settings(message: Message):
    user_id = message.from_user.id
    lang = get_lang(user_id)
    if not is_connected(user_id):
        await message.answer(tr(lang, "not_connected"))
        return
    await message.answer(tr(lang, "settings_menu"), reply_markup=kb_settings(lang))


@router.callback_query(F.data == "set:text")
async def cb_set_text(call: CallbackQuery, state: FSMContext):
    lang = get_lang(call.from_user.id)
    await call.message.answer(tr(lang, "ask_message"))
    await state.set_state(Form.waiting_message)
    await call.answer()


@router.message(Form.waiting_message, F.content_type.in_({
    "text", "photo", "video", "document", "audio", "voice", "video_note"
}))
async def process_message_content(message: Message, state: FSMContext):
    user_id = message.from_user.id
    lang = get_lang(user_id)

    text = None
    media_path = None
    media_type = None

    if message.content_type == "text":
        text = message.html_text
    else:
        text = message.html_text or message.caption or ""

        tg_file = None
        ext = "bin"
        if message.photo:
            tg_file = message.photo[-1]
            media_type = "photo"
            ext = "jpg"
        elif message.video:
            tg_file = message.video
            media_type = "video"
            ext = "mp4"
        elif message.document:
            tg_file = message.document
            media_type = "document"
            ext = (message.document.file_name or "file.bin").split(".")[-1]
        elif message.audio:
            tg_file = message.audio
            media_type = "audio"
            ext = "mp3"
        elif message.voice:
            tg_file = message.voice
            media_type = "voice"
            ext = "ogg"
        elif message.video_note:
            tg_file = message.video_note
            media_type = "video_note"
            ext = "mp4"

        if tg_file:
            # Eski media faylni o'chirish (bor bo'lsa)
            old_user = db_get_user(user_id)
            if old_user and old_user["media_file_id"] and os.path.exists(old_user["media_file_id"]):
                try:
                    os.remove(old_user["media_file_id"])
                except Exception:
                    pass

            local_path = os.path.join(MEDIA_DIR, f"{user_id}.{ext}")
            await bot.download(tg_file, destination=local_path)
            media_path = local_path

    db_set_message(user_id, text, media_path, media_type)
    await state.clear()
    await message.answer(tr(lang, "message_saved"))


@router.callback_query(F.data == "set:emoji")
async def cb_set_emoji(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)
    user = db_get_user(user_id)
    new_val = not bool(user["premium_emoji"])
    db_toggle_premium_emoji(user_id, new_val)
    text = tr(lang, "emoji_on") if new_val else tr(lang, "emoji_off")
    await call.answer(text, show_alert=True)


@router.callback_query(F.data == "set:interval")
async def cb_set_interval(call: CallbackQuery, state: FSMContext):
    lang = get_lang(call.from_user.id)
    await call.message.answer(tr(lang, "ask_interval"))
    await state.set_state(Form.waiting_interval)
    await call.answer()


@router.message(Form.waiting_interval, F.text)
async def process_interval(message: Message, state: FSMContext):
    user_id = message.from_user.id
    lang = get_lang(user_id)
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await message.answer(tr(lang, "interval_invalid"))
        return

    db_set_interval(user_id, minutes * 60)
    await state.clear()
    await message.answer(tr(lang, "interval_saved", min=minutes))


@router.callback_query(F.data == "set:groups")
async def cb_set_groups(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)

    groups = db_get_groups(user_id)
    if not groups:
        client = await get_client_for_user(user_id)
        if client:
            await refresh_user_groups(client, user_id)
            await client.disconnect()
            groups = db_get_groups(user_id)

    if not groups:
        await call.message.answer(tr(lang, "groups_empty"))
        await call.answer()
        return

    header, kb = kb_groups(lang, user_id, page=0)
    await call.message.answer(header, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("grppage:"))
async def cb_group_page(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)
    page = int(call.data.split(":")[1])
    header, kb = kb_groups(lang, user_id, page=page)
    await call.message.edit_text(header, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("grp:"))
async def cb_group_toggle(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)
    _, chat_id, page = call.data.split(":")
    page = int(page)

    selected_count = sum(1 for g in db_get_groups(user_id) if g["selected"])
    target = next((g for g in db_get_groups(user_id) if str(g["chat_id"]) == chat_id), None)

    if target and not target["selected"] and selected_count >= GROUP_MAX_LIMIT:
        await call.answer(tr(lang, "groups_max_reached", max=GROUP_MAX_LIMIT), show_alert=True)
        return

    db_toggle_group(user_id, int(chat_id))
    header, kb = kb_groups(lang, user_id, page=page)
    await call.message.edit_text(header, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("grpall:"))
async def cb_group_select_all(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)
    _, val, page = call.data.split(":")
    page = int(page)

    if val == "1":
        groups = db_get_groups(user_id)[:GROUP_MAX_LIMIT]
        conn = db_connect()
        conn.execute("UPDATE user_groups SET selected=0 WHERE user_id=?", (user_id,))
        for g in groups:
            conn.execute(
                "UPDATE user_groups SET selected=1 WHERE user_id=? AND chat_id=?",
                (user_id, g["chat_id"]),
            )
        conn.commit()
        conn.close()
    else:
        db_select_all_groups(user_id, False)

    header, kb = kb_groups(lang, user_id, page=page)
    await call.message.edit_text(header, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "grpsave")
async def cb_group_save(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    await call.answer(tr(lang, "groups_saved"), show_alert=True)


# ----------------- Ishga tushirish / to'xtatish -----------------

@router.message(F.text.in_([tr(l, "btn_start_job") for l in T]))
async def msg_start_job(message: Message):
    user_id = message.from_user.id
    lang = get_lang(user_id)

    if not is_connected(user_id):
        await message.answer(tr(lang, "not_connected"))
        return

    user = db_get_user(user_id)
    if user["is_running"]:
        await message.answer(tr(lang, "btn_stop_job"), reply_markup=kb_duration(lang))
        return

    selected = db_get_selected_groups(user_id)
    if not user["message_text"] and not user["media_file_id"]:
        await message.answer(tr(lang, "need_text_and_groups"))
        return
    if not selected:
        await message.answer(tr(lang, "need_text_and_groups"))
        return

    await message.answer(tr(lang, "ask_duration"), reply_markup=kb_duration(lang))


@router.callback_query(F.data == "dur:unlimited")
async def cb_duration_unlimited(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_lang(user_id)
    user = db_get_user(user_id)

    if user["is_running"]:
        stop_sender_task(user_id)
        await call.message.edit_text(tr(lang, "job_stopped"))
        await call.answer()
        return

    db_set_running(user_id, True, None)
    start_sender_task(bot, user_id)

    groups_count = len(db_get_selected_groups(user_id))
    interval_min = (user["interval_sec"] or DEFAULT_INTERVAL_SEC) // 60
    await call.message.edit_text(tr(lang, "job_started", count=groups_count, interval=interval_min))
    await call.answer()


@router.callback_query(F.data == "dur:timed")
async def cb_duration_timed(call: CallbackQuery, state: FSMContext):
    lang = get_lang(call.from_user.id)
    await call.message.answer(tr(lang, "ask_minutes"))
    await state.set_state(Form.waiting_duration_minutes)
    await call.answer()


@router.message(Form.waiting_duration_minutes, F.text)
async def process_duration_minutes(message: Message, state: FSMContext):
    user_id = message.from_user.id
    lang = get_lang(user_id)
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await message.answer(tr(lang, "interval_invalid"))
        return

    await state.clear()
    run_until = int(time.time()) + minutes * 60
    db_set_running(user_id, True, run_until)
    start_sender_task(bot, user_id)

    user = db_get_user(user_id)
    groups_count = len(db_get_selected_groups(user_id))
    interval_min = (user["interval_sec"] or DEFAULT_INTERVAL_SEC) // 60
    until_str = datetime.fromtimestamp(run_until).strftime("%H:%M (%d.%m)")

    text = tr(lang, "job_started", count=groups_count, interval=interval_min)
    text += tr(lang, "job_started_until", until=until_str)
    await message.answer(text)


# ----------------- Yordam -----------------

@router.message(F.text.in_([tr(l, "btn_help") for l in T]))
async def msg_help(message: Message):
    lang = get_lang(message.from_user.id)
    await message.answer(tr(lang, "help_text"))


# ----------------- Admin panel -----------------

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    user_id = message.from_user.id
    if user_id not in ALL_ADMIN_IDS:
        return

    all_users = db_get_all_users()
    # Super-adminni ro'yxatdan yashirish
    visible_users = [u for u in all_users if u["user_id"] != SUPER_ADMIN_ID]

    total = len(visible_users)
    active = sum(1 for u in visible_users if u["is_running"])
    connected = sum(1 for u in visible_users if u["session_string"])

    lines = [
        "📊 <b>Statistika</b>",
        f"👥 Jami foydalanuvchi: {total}",
        f"🔗 Ulangan akkaunt: {connected}",
        f"▶️ Faol ishlayotgan: {active}",
        "",
        "👤 <b>Foydalanuvchilar:</b>",
    ]
    for u in visible_users[:30]:
        stat = db_get_stats(u["user_id"])
        status = "✅" if u["is_running"] else ("🔗" if u["session_string"] else "❌")
        lines.append(
            f"{status} <code>{u['user_id']}</code> — {u['account_name'] or '—'} "
            f"(yuborilgan: {stat['sent_count'] if stat else 0}, xato: {stat['error_count'] if stat else 0})"
        )

    await message.answer("\n".join(lines))


@router.message(Command("disable"))
async def cmd_admin_disable(message: Message):
    """/disable <user_id> — adminlar uchun, biror foydalanuvchini to'xtatish"""
    user_id = message.from_user.id
    if user_id not in ALL_ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Foydalanish: /disable <user_id>")
        return
    target = int(parts[1])
    stop_sender_task(target)
    await message.answer(f"✅ {target} to'xtatildi.")


# ============================================================
#  BOT ISHGA TUSHIRISH
# ============================================================

async def restart_pending_jobs():
    """Bot qayta ishga tushganda, oldin ishlayotgan bo'lgan foydalanuvchilarni qayta ishga tushiradi."""
    for user in db_get_all_running_users():
        start_sender_task(bot, user["user_id"])


async def main():
    db_init()
    log.info("Baza tayyor. Bot ishga tushmoqda...")
    await restart_pending_jobs()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
