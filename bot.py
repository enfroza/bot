#!/usr/bin/env python3
"""
Telegram File Store Bot - Custom version with Protect Content feature
Inspired by Ape De File Saver style.
Features:
- Store single or batch files/messages via /genlink, /batch, /custom_batch
- Generate shareable t.me/bot?start=CODE links
- Per-user /settings: Protect Content toggle, Custom Caption, Custom Button
- When delivering, uses protect_content=True if enabled for that share
- Storage via private channel (bot must be admin there)
- SQLite DB for persistence

Setup:
1. Create bot with @BotFather, get token
2. Get API_ID, API_HASH from https://my.telegram.org
3. Create a PRIVATE channel, add bot as administrator with all permissions (post messages, etc.)
4. Get channel ID using @getidsbot or by forwarding a message and checking
5. Set .env 
6. pip install -r requirements.txt
7. python bot.py

For production: Use screen/tmux, or deploy to VPS/Render/Railway.
Protect Content works via Bot API / Pyrogram copy_message(..., protect_content=bool)
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, 
    CallbackQuery, InputMediaDocument, InputMediaPhoto, InputMediaVideo
)
from pyrogram.errors import FloodWait, MessageNotModified
import shortuuid
import asyncio
from dotenv import load_dotenv

# Load env
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", 0))
FORCE_SUB_CHANNELS = [ch.strip() for ch in os.getenv("FORCE_SUB_CHANNELS", "").split(",") if ch.strip()]  # comma separated @ch1,@ch2 or -100xxx
OWNER_ID = int(os.getenv("OWNER_ID", 0))
DB_PATH = os.getenv("DATABASE_PATH", "database.db")

if not all([BOT_TOKEN, API_ID, API_HASH, STORAGE_CHANNEL_ID]):
    raise ValueError("Missing required env vars. Check .env")

# In-memory temp storage for batch collection (per user)
batch_sessions: Dict[int, List[Dict]] = {}  # user_id -> list of {"chat_id": , "message_id": }

# DB helpers
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    # Shares table: one code -> multiple content items + protect flag at creation time
    c.execute('''
        CREATE TABLE IF NOT EXISTS shares (
            code TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            content TEXT NOT NULL,  -- JSON list of {"chat_id": int, "message_id": int}
            protect_content BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            custom_caption TEXT,
            custom_button_text TEXT,
            custom_button_url TEXT
        )
    ''')
    # Per user settings
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            protect_content BOOLEAN DEFAULT 0,
            custom_caption TEXT DEFAULT '',
            custom_button_text TEXT DEFAULT '',
            custom_button_url TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

init_db()

def get_user_settings(user_id: int) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    # Default
    return {
        "user_id": user_id,
        "protect_content": False,
        "custom_caption": "",
        "custom_button_text": "",
        "custom_button_url": ""
    }

def update_user_setting(user_id: int, key: str, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_settings (user_id, {0}) 
        VALUES (?, ?) 
        ON CONFLICT(user_id) DO UPDATE SET {0} = ?, updated_at = CURRENT_TIMESTAMP
    """.format(key), (user_id, value, value))
    conn.commit()
    conn.close()

def create_share(owner_id: int, content_list: List[Dict], protect: bool, 
                 caption: str = "", button_text: str = "", button_url: str = "") -> str:
    code = shortuuid.ShortUUID().random(length=10)  # short unique code
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO shares (code, owner_id, content, protect_content, custom_caption, custom_button_text, custom_button_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (code, owner_id, json.dumps(content_list), int(protect), caption, button_text, button_url))
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

# Bot client
app = Client(
    "file_store_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=10
)

# Helper to store a message (copy to storage channel)
async def store_message(client: Client, message: Message) -> Dict:
    """Forward/copy user message to storage channel and return ref"""
    try:
        # Use copy_message to preserve everything
        copied = await client.copy_message(
            chat_id=STORAGE_CHANNEL_ID,
            from_chat_id=message.chat.id,
            message_id=message.id
        )
        return {"chat_id": STORAGE_CHANNEL_ID, "message_id": copied.id}
    except Exception as e:
        logger.error(f"Failed to store message: {e}")
        raise

# Force Subscribe Check - Multiple channels support
async def get_missing_force_sub_channels(client: Client, user_id: int) -> list:
    """Returns list of channels user has NOT joined"""
    if not FORCE_SUB_CHANNELS:
        return []  # No force sub
    
    missing = []
    for channel in FORCE_SUB_CHANNELS:
        try:
            member = await client.get_chat_member(channel, user_id)
            if member.status not in [enums.ChatMemberStatus.MEMBER, 
                                    enums.ChatMemberStatus.ADMINISTRATOR, 
                                    enums.ChatMemberStatus.OWNER]:
                missing.append(channel)
        except Exception:
            missing.append(channel)  # Error or not member
    return missing


# Deliver content to user
async def deliver_share(client: Client, share: dict, chat_id: int):
    protect = bool(share.get("protect_content", 0))
    caption = share.get("custom_caption") or ""
    btn_text = share.get("custom_button_text") or ""
    btn_url = share.get("custom_button_url") or ""
    
    content_list = share["content"]
    if not content_list:
        await client.send_message(chat_id, "⚠️ මෙම ලින්ක් එකේ අන්තර්ගතයක් නැත (No content in this link).")
        return

    keyboard = None
    if btn_text and btn_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=btn_text, url=btn_url)]
        ])

    sent_count = 0
    for item in content_list:
        try:
            # Use copy_message with protect_content
            await client.copy_message(
                chat_id=chat_id,
                from_chat_id=item["chat_id"],
                message_id=item["message_id"],
                protect_content=protect,
                caption=caption if caption else None,  # override if custom
                reply_markup=keyboard if (sent_count == len(content_list) - 1) else None  # button on last
            )
            sent_count += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.error(f"Deliver error for item {item}: {e}")
            await client.send_message(chat_id, f"⚠️ එක් අයිටම් එකක් යැවීමට අසමත් විය: {e}")

    if sent_count > 0:
        status = "🔒 Protected (Forwarding blocked)" if protect else "🔓 Not protected"
        await client.send_message(
            chat_id, 
            f"✅ Content delivered! ({sent_count} items)\n{status}\n\nThank you for using our service."
        )

# ==================== HANDLERS ====================

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    
    if len(args) > 1:
        # Telegram passes the value after ?start=CODE as the first argument to /start
        code = args[1].strip()
        share = get_share(code)
        if share:
            # Force Subscribe Check - Multiple channels
            missing_channels = await get_missing_force_sub_channels(client, user_id)
            if missing_channels:
                buttons = []
                for ch in missing_channels:
                    clean_ch = ch.replace('@', '')
                    buttons.append([InlineKeyboardButton(f"🔗 Join {clean_ch}", url=f"https://t.me/{clean_ch}")])
                
                await message.reply(
                    "🚫 **Force Subscribe Required**\n\n"
                    "මෙම link එකේ අන්තර්ගතය ලබා ගැනීමට පෙර පහත channel එකට/එකට join විය යුතුයි:\n\n"
                    "Join වෙලා ආයෙත් link එක open කරන්න.",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return
            
            await message.reply("📥 ඔබේ අන්තර්ගතය ලබා ගැනීම සඳහා රැඳී සිටින්න... (Fetching your content...)")
            await deliver_share(client, share, user_id)
            return
        else:
            await message.reply("❌ වලංගු නොවන ලින්ක් කේතයකි. (Invalid share code.)")
            return

    # Normal start
    settings = get_user_settings(user_id)
    protect_status = "✅ Enabled" if settings["protect_content"] else "❌ Disabled"
    
    text = (
        "👋 **Welcome to Ape De File Saver!**\n\n"
        "📁 Send me files, photos, videos or documents to store.\n"
        "🔗 I will give you a permanent shareable link.\n\n"
        f"🛡️ **Protect Content**: {protect_status}\n\n"
        "Commands:\n"
        "/genlink - Store single file (reply to message or send file)\n"
        "/batch - Store multiple files (send several then /done)\n"
        "/custom_batch - Quick custom batch\n"
        "/settings - Customize caption, button, protect & more\n\n"
        "💡 Tip: Use /settings to enable Protect Content before creating links!"
    )
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("genlink"))
async def genlink_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    
    # If replied to a message (e.g. in channel or previous)
    if message.reply_to_message:
        target = message.reply_to_message
    else:
        # Or if the command message has media? Rare, usually send media then command or use reply
        await message.reply("📎 Please reply to a file/message with /genlink or send the file directly and I will auto-store it.\n\nFor batch use /batch or /custom_batch.")
        return

    try:
        ref = await store_message(client, target)
        settings = get_user_settings(user_id)
        protect = settings["protect_content"]
        
        code = create_share(
            owner_id=user_id,
            content_list=[ref],
            protect=protect,
            caption=settings.get("custom_caption", ""),
            button_text=settings.get("custom_button_text", ""),
            button_url=settings.get("custom_button_url", "")
        )
        
        link = f"https://t.me/{(await client.get_me()).username}?start={code}"
        protect_text = "🔒 Protected (forwarding restricted)" if protect else "🔓 Standard link"
        
        await message.reply(
            f"✅ **Link Created!**\n\n"
            f"🔗 {link}\n\n"
            f"{protect_text}\n"
            f"Share this link. Recipient will receive the file{'s' if protect else ''}."
        )
    except Exception as e:
        await message.reply(f"❌ Error storing: {str(e)}")

# For direct media upload (auto store single) - only if NOT in batch session
@app.on_message(filters.media & ~filters.command(["start", "genlink", "batch", "custom_batch", "settings", "done"]))
async def auto_store_single(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in batch_sessions:
        return  # Let batch collector handle it
    try:
        ref = await store_message(client, message)
        settings = get_user_settings(user_id)
        protect = settings["protect_content"]
        
        code = create_share(
            owner_id=user_id,
            content_list=[ref],
            protect=protect,
            caption=settings.get("custom_caption", ""),
            button_text=settings.get("custom_button_text", ""),
            button_url=settings.get("custom_button_url", "")
        )
        
        link = f"https://t.me/{(await client.get_me()).username}?start={code}"
        protect_text = "🔒 **Protected** (forwarding blocked for recipients)" if protect else "🔓 Standard"
        
        await message.reply(
            f"✅ File stored successfully!\n\n"
            f"🔗 Shareable Link:\n{link}\n\n"
            f"Status: {protect_text}\n\n"
            "Use /settings to change protection or add custom caption/button for future links."
        )
    except Exception as e:
        await message.reply(f"❌ Failed to store file: {e}")

# ==================== BATCH / CUSTOM_BATCH ====================
@app.on_message(filters.command("batch"))
async def batch_start(client: Client, message: Message):
    user_id = message.from_user.id
    batch_sessions[user_id] = []
    await message.reply(
        "📦 **Batch Mode Started**\n\n"
        "Send all the files, photos, videos or messages you want to include (one by one).\n"
        "When finished, send /done\n\n"
        "⚠️ Max ~15 items recommended per batch."
    )

@app.on_message(filters.command("custom_batch"))
async def custom_batch_start(client: Client, message: Message):
    # Similar to batch, or you can make it different (e.g. random selection or editable later)
    user_id = message.from_user.id
    batch_sessions[user_id] = []
    await message.reply(
        "🧩 **Custom Batch Mode**\n\n"
        "Send your files/messages now.\n"
        "Send /done when ready to generate the link.\n\n"
        "This creates a shareable link for multiple items with your current settings."
    )

@app.on_message(filters.command("done"))
async def batch_done(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in batch_sessions or not batch_sessions[user_id]:
        await message.reply("No items collected. Send files first or use /genlink for single.")
        return
    
    items = batch_sessions.pop(user_id)
    settings = get_user_settings(user_id)
    protect = settings["protect_content"]
    
    code = create_share(
        owner_id=user_id,
        content_list=items,
        protect=protect,
        caption=settings.get("custom_caption", ""),
        button_text=settings.get("custom_button_text", ""),
        button_url=settings.get("custom_button_url", "")
    )
    
    link = f"https://t.me/{(await client.get_me()).username}?start={code}"
    protect_text = "🔒 Protected (recipients cannot forward)" if protect else "🔓 Not protected"
    
    await message.reply(
        f"✅ **Batch Link Created!** ({len(items)} items)\n\n"
        f"🔗 {link}\n\n"
        f"{protect_text}\n\n"
        "Share this link safely."
    )

# Collect items during batch mode
@app.on_message(filters.media | filters.text, group=1)  # group to not conflict with other handlers
async def collect_batch_items(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in batch_sessions:
        # Don't collect commands
        if message.text and message.text.startswith("/"):
            return
        try:
            ref = await store_message(client, message)
            batch_sessions[user_id].append(ref)
            await message.reply(f"✅ Item {len(batch_sessions[user_id])} stored in batch. Send more or /done", quote=True)
        except Exception as e:
            await message.reply(f"⚠️ Failed to store this item: {e}", quote=True)

# ==================== SETTINGS ====================
def get_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    protect = settings.get("protect_content", False)
    protect_btn = "✅ Protect Content (ON)" if protect else "❌ Protect Content (OFF)"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(protect_btn, callback_data="toggle_protect")],
        [InlineKeyboardButton("✏️ Set Custom Caption", callback_data="set_caption")],
        [InlineKeyboardButton("🔘 Set Custom Button", callback_data="set_button")],
        [InlineKeyboardButton("🔗 Link Shortener (coming soon)", callback_data="shortener")],
        [InlineKeyboardButton("🔄 Reset Settings", callback_data="reset_settings")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")]
    ])

@app.on_message(filters.command("settings"))
async def settings_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    protect_status = "✅ ON - Forwarding will be blocked for new links" if settings["protect_content"] else "❌ OFF - Standard links"
    
    text = (
        "⚙️ **Your Settings**\n\n"
        f"🛡️ Protect Content: {protect_status}\n\n"
        f"📝 Custom Caption: {settings.get('custom_caption') or 'Not set (uses original)'}\n"
        f"🔘 Custom Button: {settings.get('custom_button_text') or 'Not set'}\n\n"
        "Changes apply to **new** links you create.\n"
        "Existing links keep their original protection setting."
    )
    await message.reply(text, reply_markup=get_settings_keyboard(settings))

@app.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    settings = get_user_settings(user_id)
    
    if data == "toggle_protect":
        new_val = not settings["protect_content"]
        update_user_setting(user_id, "protect_content", new_val)
        await callback.answer(f"Protect Content {'Enabled ✅' if new_val else 'Disabled ❌'}")
        # Refresh
        new_settings = get_user_settings(user_id)
        await callback.message.edit_reply_markup(get_settings_keyboard(new_settings))
        
    elif data == "set_caption":
        await callback.message.reply(
            "✏️ Send the new custom caption you want for future links.\n"
            "Use {filename} or leave empty to keep original.\n"
            "Example: 🔥 Exclusive Content - {filename}\n\n"
            "Send /cancel to abort."
        )
        # For full production, use FSM or a temp state. Here simple: next text message sets it.
        # To keep simple in this version, user can reply or we can use a conversation but for demo:
        await callback.answer("Please send the caption text in next message (or implement FSM for better UX).")
        
    elif data == "set_button":
        await callback.message.reply(
            "🔘 Send in format: ButtonText|https://example.com\n"
            "Example: Join Our Channel|https://t.me/yourchannel\n\n"
            "Send /cancel to abort."
        )
        await callback.answer("Send button config in next message.")
        
    elif data == "reset_settings":
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        await callback.answer("Settings reset to default!")
        new_settings = get_user_settings(user_id)
        await callback.message.edit_text("Settings have been reset.", reply_markup=get_settings_keyboard(new_settings))
        
    elif data == "back_main":
        await callback.message.delete()
        await client.send_message(user_id, "Back to main. Use /start or send files.")
        
    elif data == "shortener":
        await callback.answer("Link shortener integration coming soon! (You can add pyshorteners or GPLinks API)")

# Simple text handler for setting caption/button (basic, no full FSM for brevity)
@app.on_message(filters.text & ~filters.command(["start", "genlink", "batch", "custom_batch", "settings", "done"]))
async def handle_text_settings(client: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    # This is a simple way; in production use aiogram FSM or pyrogram states or temp DB flag
    if text.lower() == "/cancel":
        await message.reply("Cancelled.")
        return
    
    # Detect if it's button format
    if "|" in text and "http" in text.lower():
        try:
            btn_text, btn_url = text.split("|", 1)
            update_user_setting(user_id, "custom_button_text", btn_text.strip())
            update_user_setting(user_id, "custom_button_url", btn_url.strip())
            await message.reply(f"✅ Custom button set: [{btn_text.strip()}]({btn_url.strip()})")
        except:
            await message.reply("Invalid format. Use: Button Text|https://url")
    else:
        # Assume caption
        update_user_setting(user_id, "custom_caption", text)
        await message.reply(f"✅ Custom caption saved for future links:\n{text[:100]}...")

# Admin broadcast example (optional)
@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast(client: Client, message: Message):
    # Simple broadcast to all users who have settings or shares
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM user_settings UNION SELECT DISTINCT owner_id FROM shares")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    
    text = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else "Test broadcast"
    count = 0
    for uid in users:
        try:
            await client.send_message(uid, f"📢 Broadcast:\n{text}")
            count += 1
        except:
            pass
    await message.reply(f"Broadcast sent to {count} users.")

# Run
if __name__ == "__main__":
    logger.info("Starting Ape De File Saver Bot...")
    app.run()
