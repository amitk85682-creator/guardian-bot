import os
import threading
from flask import Flask
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from collections import defaultdict
import psycopg2

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get("GUARDIAN_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get('PORT', 8080))

# --- AI Setup for Spam Detection ---
SPAM_DETECTION_PROMPT = "You are a spam detection AI for a Telegram group. Analyze the message and reply with only 'SPAM' if it is promotion, selling something, or junk, and 'OK' if it is a normal message."
genai.configure(api_key=GEMINI_API_KEY)
spam_model = genai.GenerativeModel(model_name='gemini-1.5-flash', system_instruction=SPAM_DETECTION_PROMPT)

# --- In-memory stores ---
user_warnings = defaultdict(int)
blacklist_words = set()

# --- Database Functions ---
def db_connect():
    return psycopg2.connect(DATABASE_URL)

def setup_database():
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS blacklist (id SERIAL PRIMARY KEY, word TEXT NOT NULL UNIQUE);")
    conn.commit()
    conn.close()

def load_blacklist():
    global blacklist_words
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("SELECT word FROM blacklist;")
        blacklist_words = {row[0] for row in cur.fetchall()}
    conn.close()
    print(f"Loaded {len(blacklist_words)} words from blacklist.")

# --- Flask App for Keep-Alive ---
flask_app = Flask('')
@flask_app.route('/')
def home():
    return "Guardian Bot is vigilant!"

def run_flask():
    from waitress import serve
    serve(flask_app, host='0.0.0.0', port=PORT)

# --- Telegram Bot Logic ---
async def addword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    words_to_add = {word.lower() for word in context.args}
    if not words_to_add:
        await update.message.reply_text("Usage: /addword <word1> <word2>...")
        return
    conn = db_connect()
    with conn.cursor() as cur:
        for word in words_to_add:
            cur.execute("INSERT INTO blacklist (word) VALUES (%s) ON CONFLICT DO NOTHING;", (word,))
    conn.commit()
    conn.close()
    load_blacklist() # Refresh the in-memory list
    await update.message.reply_text(f"Added {len(words_to_add)} word(s) to the blacklist.")

async def delword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    words_to_del = {word.lower() for word in context.args}
    if not words_to_del:
        await update.message.reply_text("Usage: /delword <word1> <word2>...")
        return
    conn = db_connect()
    with conn.cursor() as cur:
        for word in words_to_del:
            cur.execute("DELETE FROM blacklist WHERE word = %s;", (word,))
    conn.commit()
    conn.close()
    load_blacklist() # Refresh the in-memory list
    await update.message.reply_text(f"Removed {len(words_to_del)} word(s) from the blacklist.")

async def listwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    if not blacklist_words:
        await update.message.reply_text("The blacklist is currently empty.")
        return
    await update.message.reply_text("Blacklisted words:\n- " + "\n- ".join(sorted(list(blacklist_words))))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user: return
    user = update.message.from_user
    chat_id = update.effective_chat.id
    message = update.message

    try:
        chat_admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = {admin.user.id for admin in chat_admins}
        if user.id in admin_ids or user.id == ADMIN_USER_ID:
            return
    except Exception:
        if user.id == ADMIN_USER_ID: return

    text = message.text or message.caption or ""
    text_lower = text.lower()
    is_spam = False
    reason = ""

    # Strict Rules
    if any(entity.type in ['url', 'text_link'] for entity in message.entities or []): is_spam, reason = True, "Links are not allowed."
    if not is_spam and '@' in text: is_spam, reason = True, "Mentions are not allowed."
    if not is_spam and (message.forward_from or message.forward_from_chat): is_spam, reason = True, "Forwarded messages are not allowed."

    # Dynamic Blacklist
    if not is_spam and any(word in text_lower for word in blacklist_words): is_spam, reason = True, "A forbidden word was used."

    # AI Analysis
    if not is_spam and text and len(text) > 20: # Only check longer messages with AI
        try:
            response = await spam_model.generate_content_async(text)
            if "SPAM" in response.text.upper(): is_spam, reason = True, "This looks like a promotional message."
        except Exception as e:
            print(f"Gemini error during spam check: {e}")

    # Take Action
    if is_spam:
        try:
            await message.delete()
            user_warnings[user.id] += 1
            warning_count = user_warnings[user.id]

            if warning_count >= 3:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ {user.mention_html()} has been banned after 3 warnings.", parse_mode='HTML')
                del user_warnings[user.id]
            else:
                await context.bot.send_message(chat_id=chat_id, text=f"Hey {user.mention_html()}, please don't spam. {reason} (Warning {warning_count}/3)", parse_mode='HTML')
        except Exception as e:
            print(f"Could not take action on user {user.id}: {e}")

async def main():
    setup_database()
    load_blacklist()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("addword", addword))
    application.add_handler(CommandHandler("delword", delword))
    application.add_handler(CommandHandler("listwords", listwords))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("Guardian Bot is now vigilant...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    asyncio.run(main())
