import os
import threading
import asyncio
from flask import Flask
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from collections import defaultdict
import psycopg2
from datetime import datetime, timedelta

# Configuration
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get('PORT', 8080))

# AI Setup for Spam Detection
SPAM_DETECTION_PROMPT = """You are a vigilant spam detection AI for Telegram. Analyze messages and:
1. Reply with "SPAM" if it contains promotions, scams, ads, or suspicious links
2. Reply with "OK" for normal messages
3. Consider cultural context and multiple languages"""
genai.configure(api_key=GEMINI_API_KEY)
spam_model = genai.GenerativeModel(model_name='gemini-1.5-flash', system_instruction=SPAM_DETECTION_PROMPT)

# In-memory stores
user_warnings = defaultdict(int)
user_last_message = defaultdict(datetime)
blacklist_words = set()
allowed_chats = set()

# Database Functions
def db_connect():
    return psycopg2.connect(DATABASE_URL)

def setup_database():
    conn = db_connect()
    with conn.cursor() as cur:
        # Blacklist table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                id SERIAL PRIMARY KEY,
                word TEXT NOT NULL UNIQUE,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Allowed chats table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS allowed_chats (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL UNIQUE,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Custom commands table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS custom_commands (
                id SERIAL PRIMARY KEY,
                command TEXT NOT NULL UNIQUE,
                response TEXT NOT NULL,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.commit()
    conn.close()

def load_blacklist():
    global blacklist_words
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("SELECT word FROM blacklist")
        blacklist_words = {row[0].lower() for row in cur.fetchall()}
    conn.close()
    print(f"Loaded {len(blacklist_words)} words from blacklist")

def load_allowed_chats():
    global allowed_chats
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("SELECT chat_id FROM allowed_chats")
        allowed_chats = {row[0] for row in cur.fetchall()}
    conn.close()
    print(f"Loaded {len(allowed_chats)} allowed chats")

# Custom command system
async def handle_custom_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.split()[0][1:].lower()  # Remove slash
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("SELECT response FROM custom_commands WHERE command = %s", (command,))
        result = cur.fetchone()
    conn.close()
    
    if result:
        await update.message.reply_text(result[0])

# Admin commands for custom commands
async def addcommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Only admin can add commands")
        return
        
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addcommand <name> <response>")
        return
        
    command = context.args[0].lower()
    response = " ".join(context.args[1:])
    
    conn = db_connect()
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO custom_commands (command, response, added_by) VALUES (%s, %s, %s)",
                (command, response, update.effective_user.id)
            )
            conn.commit()
            await update.message.reply_text(f"✅ Command /{command} added successfully!")
        except psycopg2.IntegrityError:
            await update.message.reply_text(f"❌ Command /{command} already exists")
    conn.close()

# Flask App for Keep-Alive
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🛡️ Guardian Bot is running and vigilant!"

def run_flask():
    from waitress import serve
    serve(flask_app, host='0.0.0.0', port=PORT)

# Telegram Bot Logic
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ Hello! I'm Guardian Bot\n\n"
        "I protect groups from spam and scams with AI-powered detection.\n"
        "Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Only admin can use this command")
        return
        
    help_text = """
    🛡️ *Admin Commands:*
    /addword <words> - Add words to blacklist
    /delword <words> - Remove words from blacklist
    /listwords - Show blacklisted words
    /addcommand <name> <response> - Add custom command
    /allowchat <chat_id> - Allow a chat to use bot
    /stats - Show protection statistics
    
    ⚙️ *Features:*
    • AI-powered spam detection
    • Blacklist system
    • Flood protection
    • Link prevention
    • Auto-moderation
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def addword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Only admin can add words")
        return
        
    words_to_add = {word.lower() for word in context.args}
    if not words_to_add:
        await update.message.reply_text("Usage: /addword <word1> <word2>...")
        return
        
    conn = db_connect()
    with conn.cursor() as cur:
        added_count = 0
        for word in words_to_add:
            try:
                cur.execute(
                    "INSERT INTO blacklist (word, added_by) VALUES (%s, %s)",
                    (word, update.effective_user.id)
                )
                added_count += 1
            except psycopg2.IntegrityError:
                continue
                
    conn.commit()
    conn.close()
    load_blacklist()
    await update.message.reply_text(f"✅ Added {added_count} word(s) to blacklist")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Only admin can view stats")
        return
        
    stats_text = f"""
    📊 *Guardian Bot Statistics*
    
    • Blacklisted words: {len(blacklist_words)}
    • Allowed chats: {len(allowed_chats)}
    • Active warnings: {len(user_warnings)}
    • AI Model: Gemini 1.5 Flash
    """
    await update.message.reply_text(stats_text, parse_mode='Markdown')

# Message handling with advanced protection
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
        
    user = update.message.from_user
    chat_id = update.effective_chat.id
    message = update.message
    
    # Check if chat is allowed
    if chat_id not in allowed_chats:
    await update.message.reply_text("❌ This chat is not authorized to use this bot")
    return
    
    # Flood protection
    now = datetime.now()
    if user.id in user_last_message and (now - user_last_message[user.id]).seconds < 2:
        await message.delete()
        return
    user_last_message[user.id] = now
    
    # Skip admin checks in private chats
    if chat_id > 0:
        try:
            chat_admins = await context.bot.get_chat_administrators(chat_id)
            admin_ids = {admin.user.id for admin in chat_admins}
            if user.id in admin_ids or user.id == ADMIN_USER_ID:
                return
        except Exception:
            if user.id == ADMIN_USER_ID:
                return
    
    text = message.text or message.caption or ""
    text_lower = text.lower()
    is_spam = False
    reason = ""

    # Strict Rules
    if any(entity.type in ['url', 'text_link'] for entity in message.entities or []):
        is_spam, reason = True, "Links are not allowed"
    if not is_spam and '@' in text and not text.startswith('/'):
        is_spam, reason = True, "Mentions are not allowed"
    if not is_spam and (message.forward_from or message.forward_from_chat):
        is_spam, reason = True, "Forwarded messages are not allowed"
    
    # Dynamic Blacklist
    if not is_spam and any(word in text_lower for word in blacklist_words):
        is_spam, reason = True, "Blacklisted word detected"
    
    # AI Analysis for longer messages
    if not is_spam and text and len(text) > 15:
        try:
            response = await asyncio.wait_for(
                spam_model.generate_content_async(text),
                timeout=5.0
            )
            if "SPAM" in response.text.upper():
                is_spam, reason = True, "AI detected spam content"
        except Exception as e:
            print(f"Gemini error: {e}")

    # Take Action
    if is_spam:
        try:
            await message.delete()
            user_warnings[user.id] += 1
            warning_count = user_warnings[user.id]

            if warning_count >= 3:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=f"⚠️ {user.mention_html()} has been banned after 3 warnings.",
                    parse_mode='HTML'
                )
                del user_warnings[user.id]
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {user.mention_html()}, {reason}. Warning {warning_count}/3",
                    parse_mode='HTML'
                )
        except Exception as e:
            print(f"Action error: {e}")

async def main():
    # Setup and initialization
    setup_database()
    load_blacklist()
    load_allowed_chats()
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addword", addword))
    application.add_handler(CommandHandler("addcommand", addcommand))
    application.add_handler(CommandHandler("stats", stats))
    
    # Custom commands handler (must be after specific commands)
    application.add_handler(MessageHandler(filters.COMMAND & ~filters.Regex(r'^/(start|help|addword|addcommand|stats)'), handle_custom_command))
    
    # Message handler
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🛡️ Guardian Bot is now running...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Start Flask thread for keep-alive
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Run the bot
    asyncio.run(main())
