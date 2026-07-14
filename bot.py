#!/usr/bin/env python3
"""
Ape De File Saver - Full Code with Auto Delete + Owner can change time
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional
import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
import shortuuid
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", 0))
FORCE_SUB_CHANNELS = [ch.strip() for ch in os.getenv("FORCE_SUB_CHANNELS", "").split(",") if ch.strip()]
OWNER_ID = int(os.getenv("OWNER_ID", 0))
DB_PATH = os.getenv("DATABASE_PATH", "database.db")

if not all([BOT_TOKEN, API_ID, API_HASH, STORAGE_CHANNEL_ID]):
    raise ValueError("Missing required env vars. Check .env")

batch_sessions: Dict[int, List[Dict]] = {}

# ==================== DATABASE ====================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS shares (
            code TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            protect_content BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            custom_caption TEXT,
            custom_button_text TEXT,
            custom_button_url TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            protect_content BOOLEAN DEFAULT 0,
            custom_caption TEXT DEFAULT '',
            custom_button_text TEXT DEFAULT '',
            custom_button_url TEXT DEFAULT '',
            auto_delete_minutes INTEGER DEFAULT 15,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_user_settings(user_id: int) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "user_id": user_id,
        "protect_content": False,
        "custom_caption": "",
        "custom_button_text": "",
        "custom_button_url": "",
        "auto_delete_minutes": 15
    }

def update_user_setting(user_id: int, key: str, value):
    conn = get_db()
    c = conn.cursor()
    c.execute(f"INSERT INTO user_settings (user_id, {key}) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET {key} = ?, updated_at = CURRENT_TIMESTAMP", (user_id, value, value))
    conn.commit()
    conn.close()

def create_share(owner_id: int, content_list: List[Dict], protect: bool, caption: str = "", button_text: str = "", button_url: str = "") -> str:
    code = shortuuid.ShortUUID().random(length=10)
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO shares (code, owner_id, content, protect_content, custom_caption, custom_button_text, custom_button_url) VALUES (?, ?, ?, ?, ?, ?, ?)", (code, owner_id, json.dumps(content_list), int(protect), caption, button_text, button_url))
    conn.commit()
    conn.close()
    return code

def get_share(code: str) -> Optional[dict]:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM shares WHERE code = ?", (code,))
    row = c.fetchone()
    conn.close()
    if row:
        data = dict(row)
        data["content"] = json.loads(data["content"])
        return data
    return None

# ==================== PYROGRAM ====================
app = Client("file_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=10)

# ==================== AUTO DELETE ====================
async def delete_message_after_delay(chat_id: int, message_id: int, delay_minutes: int):
    await asyncio.sleep(delay_minutes * 60)
    try:
        await app.delete_messages(chat_id=chat_id, message_ids=message_id)
        logger.info(f"Auto deleted message {message_id} after {delay_minutes} minutes")
    except Exception as e:
        logger.error(f"Failed to auto delete: {e}")

async def deliver_share(client: Client, share: dict, chat_id: int):
    protect = bool(share.get("protect_content", 0))
    caption = share.get("custom_caption") or ""
    btn_text = share.get("custom_button_text") or ""
    btn_url = share.get("custom_button_url") or ""
    content_list = share["content"]
    
    if not content_list:
        await client.send_message(chat_id, "⚠️ මෙම ලින්ක් එකේ අන්තර්ගතයක් නැත.")
        return

    keyboard = None
    if btn_text and btn_url:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(text=btn_text, url=btn_url)]])

    sent_count = 0
    settings = get_user_settings(OWNER_ID)  # Owner's setting
    auto_delete_minutes = settings.get("auto_delete_minutes", 15)

    for item in content_list:
        try:
            sent_msg = await client.copy_message(
                chat_id=chat_id,
                from_chat_id=item["chat_id"],
                message_id=item["message_id"],
                protect_content=protect,
                caption=caption if caption else None,
                reply_markup=keyboard if (sent_count == len(content_list) - 1) else None
            )
            sent_count += 1
            
            # Auto Delete
            if auto_delete_minutes > 0:
                asyncio.create_task(delete_message_after_delay(chat_id, sent_msg.id, auto_delete_minutes))
                
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.error(f"Deliver error: {e}")

    if sent_count > 0:
        status = "🔒 Protected" if protect else "🔓 Standard"
        await client.send_message(chat_id, f"✅ Content delivered! ({sent_count} items)\n{status}\n\n🗑️ Auto delete after {auto_delete_minutes} minutes")

# ==================== FORCE SUB ====================
async def get_missing_force_sub_channels(client: Client, user_id: int) -> list:
    if not FORCE_SUB_CHANNELS:
        return []
    missing = []
    for channel in FORCE_SUB_CHANNELS:
        try:
            member = await client.get_chat_member(channel, user_id)
            if member.status not in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                missing.append(channel)
        except Exception:
            missing.append(channel)
    return missing

# ==================== HANDLERS ====================

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    
    if len(args) > 1:
        code = args[1].strip()
        share = get_share(code)
        if share:
            missing_channels = await get_missing_force_sub_channels(client, user_id)
            if missing_channels:
                buttons = []
                for ch in missing_channels:
                    clean_ch = ch.replace('@', '')
                    buttons.append([InlineKeyboardButton(f"🔗 Join {clean_ch}", url=f"https://t.me/{clean_ch}")])
                buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data=f"try_again_{code}")])
                await message.reply("🚫 **Force Subscribe Required**\n\nමෙම link එකේ අන්තර්ගතය ලබා ගැනීමට පෙර සියලු channels වලට join වෙන්න.\n\nJoin වෙලා 'Try Again' button එක click කරන්න.", reply_markup=InlineKeyboardMarkup(buttons))
                return
            await message.reply("📥 ඔබේ අන්තර්ගතය ලබා ගැනීම සඳහා රැඳී සිටින්න...")
            await deliver_share(client, share, user_id)
            return
        else:
            await message.reply("❌ වලංගු නොවන ලින්ක් කේතයකි.")
            return

    settings = get_user_settings(user_id)
    protect_status = "✅ Enabled" if settings["protect_content"] else "❌ Disabled"
    auto_delete = settings.get("auto_delete_minutes", 15)
    
    text = f"👋 **Welcome to Ape De File Saver!**\n\n📁 Send me files to store.\n🔗 I will give you a permanent shareable link.\n\n🛡️ Protect: {protect_status}\n🗑️ Auto Delete: {auto_delete} minutes\n\nCommands:\n/genlink, /batch, /custom_batch, /settings, /autodelete"
    await message.reply(text)

# Try Again
@app.on_callback_query(filters.regex(r"try_again_(.+)"))
async def try_again_callback(client: Client, callback: CallbackQuery):
    code = callback.data.split("_")[-1]
    user_id = callback.from_user.id
    share = get_share(code)
    if not share:
        await callback.answer("Invalid link")
        return
    missing = await get_missing_force_sub_channels(client, user_id)
    if missing:
        await callback.answer("Still not joined all channels!")
        return
    await callback.answer("✅ All joined! Delivering...")
    await deliver_share(client, share, user_id)

# /autodelete (Owner only)
@app.on_message(filters.command("autodelete"))
async def autodelete_cmd(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply("❌ Only owner can change auto delete time.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Usage: /autodelete <minutes>\nExample: /autodelete 30")
        return
    
    try:
        minutes = int(args[1])
        if minutes < 0:
            minutes = 0
        update_user_setting(OWNER_ID, "auto_delete_minutes", minutes)
        await message.reply(f"✅ Auto delete time changed to {minutes} minutes.")
    except:
        await message.reply("❌ Invalid number. Use: /autodelete 15")

# /settings
@app.on_message(filters.command("settings"))
async def settings_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    protect_status = "✅ ON" if settings["protect_content"] else "❌ OFF"
    auto_delete = settings.get("auto_delete_minutes", 15)
    text = f"⚙️ **Your Settings**\n\n🛡️ Protect Content: {protect_status}\n🗑️ Auto Delete: {auto_delete} minutes\n\n📝 Custom Caption: {settings.get('custom_caption') or 'Not set'}"
    await message.reply(text)

# Auto store single file
@app.on_message(filters.media & ~filters.command(["start", "genlink", "batch", "custom_batch", "settings", "done", "autodelete"]))
async def auto_store_single(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in batch_sessions:
        return
    try:
        ref = await store_message(client, message)
        settings = get_user_settings(user_id)
        code = create_share(owner_id=user_id, content_list=[ref], protect=settings["protect_content"], caption=settings.get("custom_caption", ""), button_text=settings.get("custom_button_text", ""), button_url=settings.get("custom_button_url", ""))
        link = f"https://t.me/{(await client.get_me()).username}?start={code}"
        await message.reply(f"✅ File stored!\n\n🔗 {link}")
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# /genlink
@app.on_message(filters.command("genlink"))
async def genlink_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not message.reply_to_message:
        await message.reply("Please reply to a file with /genlink")
        return
    try:
        ref = await store_message(client, message.reply_to_message)
        settings = get_user_settings(user_id)
        code = create_share(owner_id=user_id, content_list=[ref], protect=settings["protect_content"], caption=settings.get("custom_caption", ""), button_text=settings.get("custom_button_text", ""), button_url=settings.get("custom_button_url", ""))
        link = f"https://t.me/{(await client.get_me()).username}?start={code}"
        await message.reply(f"✅ Link Created!\n\n🔗 {link}")
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# /batch
@app.on_message(filters.command("batch"))
async def batch_start(client: Client, message: Message):
    user_id = message.from_user.id
    batch_sessions[user_id] = []
    await message.reply("📦 **Batch Mode Started**\n\nSend files. When finished, send /done")

# /custom_batch
@app.on_message(filters.command("custom_batch"))
async def custom_batch_start(client: Client, message: Message):
    user_id = message.from_user.id
    batch_sessions[user_id] = []
    await message.reply("🧩 **Custom Batch Mode**\n\nSend files. When finished, send /done")

# /done
@app.on_message(filters.command("done"))
async def batch_done(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in batch_sessions or not batch_sessions[user_id]:
        await message.reply("No items collected.")
        return
    items = batch_sessions.pop(user_id)
    settings = get_user_settings(user_id)
    code = create_share(owner_id=user_id, content_list=items, protect=settings["protect_content"], caption=settings.get("custom_caption", ""), button_text=settings.get("custom_button_text", ""), button_url=settings.get("custom_button_url", ""))
    link = f"https://t.me/{(await client.get_me()).username}?start={code}"
    await message.reply(f"✅ Batch Link Created! ({len(items)} items)\n\n🔗 {link}")

# Collect batch items
@app.on_message(filters.media | filters.text, group=1)
async def collect_batch_items(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in batch_sessions:
        if message.text and message.text.startswith("/"):
            return
        try:
            ref = await store_message(client, message)
            batch_sessions[user_id].append(ref)
            await message.reply(f"✅ Item {len(batch_sessions[user_id])} stored. Send /done when finished.", quote=True)
        except Exception as e:
            await message.reply(f"⚠️ Failed: {e}", quote=True)

if __name__ == "__main__":
    logger.info("Starting Ape De File Saver Bot...")
    app.run()
