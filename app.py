import os
import logging
from datetime import datetime
from flask import Flask, request, redirect, url_for, jsonify
import telebot
import requests
from notion_client import Client
from parivaha import sync

# --- تنظیمات اولیه ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
NOTION_CLIENT_ID = os.getenv("NOTION_CLIENT_ID")
NOTION_CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET")
NOTION_REDIRECT_URI = os.getenv("NOTION_REDIRECT_URI")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
notion = Client(auth=os.getenv("NOTION_TOKEN"))  # برای بافر اولیه

# --- بخش ۱: وب‌هوک تلگرام ---
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    # بررسی هدر امنیتی
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != TELEGRAM_WEBHOOK_SECRET:
        return 'Unauthorized', 403
    
    update = telebot.types.Update.de_json(request.stream.read().decode('utf-8'))
    bot.process_new_updates([update])
    return 'ok', 200

@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(message, "ربات آماده است. پیام بفرستید تا در Notion ذخیره شود.\n"
                         "برای اتصال Notion از دستور /connect استفاده کنید.")

@bot.message_handler(commands=['connect'])
def handle_connect(message):
    """شروع جریان OAuth Notion"""
    auth_url = (
        f"https://api.notion.com/v1/oauth/authorize"
        f"?client_id={NOTION_CLIENT_ID}"
        f"&response_type=code"
        f"&owner=user"
        f"&redirect_uri={NOTION_REDIRECT_URI}"
    )
    bot.reply_to(message, f"برای اتصال Notion روی لینک زیر کلیک کنید:\n{auth_url}")

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = message.text
    if not text:
        return
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Name": {"title": [{"text": {"content": text[:50]}}]},
                "Content": {"rich_text": [{"text": {"content": text}}]},
                "Date": {"date": {"start": datetime.now().isoformat()}},
                "Synced": {"checkbox": False}
            }
        )
        bot.reply_to(message, "✅ در بافر Notion ذخیره شد.")
    except Exception as e:
        logging.error(f"Notion error: {e}")
        bot.reply_to(message, f"❌ خطا: {e}")

# --- بخش ۲: مسیرهای OAuth Notion ---
@app.route('/auth/notion')
def notion_authorize():
    """شروع جریان OAuth از مرورگر"""
    auth_url = (
        f"https://api.notion.com/v1/oauth/authorize"
        f"?client_id={NOTION_CLIENT_ID}"
        f"&response_type=code"
        f"&owner=user"
        f"&redirect_uri={NOTION_REDIRECT_URI}"
    )
    return redirect(auth_url)

@app.route('/auth/notion/callback')
def notion_callback():
    """دریافت code و تبدیل به access_token"""
    code = request.args.get('code')
    if not code:
        return "Authorization failed: no code provided", 400
    
    token_url = "https://api.notion.com/v1/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": NOTION_REDIRECT_URI
    }
    response = requests.post(
        token_url, data=data, auth=(NOTION_CLIENT_ID, NOTION_CLIENT_SECRET),
        headers={"Content-Type": "application/json"}
    )
    
    if response.status_code == 200:
        token_data = response.json()
        access_token = token_data.get("access_token")
        workspace_name = token_data.get("workspace_name", "Unknown")
        # در اینجا می‌توانید access_token را در یک دیتابیس امن ذخیره کنید
        logging.info(f"Notion connected: {workspace_name}")
        return f"✅ با موفقیت به Notion متصل شدید. Workspace: {workspace_name}\n" \
               f"Access Token: {access_token[:10]}..."
    else:
        return f"❌ خطا در دریافت توکن: {response.text}", 400

# --- بخش ۳: همگام‌سازی Obsidian ---
@app.route('/sync')
def sync_to_obsidian():
    """اجرای همگام‌سازی Notion → Obsidian"""
    try:
        sync.notion_to_obsidian(
            notion_token=os.getenv("NOTION_TOKEN"),
            obsidian_path=OBSIDIAN_VAULT_PATH
        )
        return jsonify({"status": "success", "message": "Sync completed."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- سلامت ---
@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
