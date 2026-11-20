import os
import logging
import base64
import requests
from datetime import datetime
from flask import Flask, request, redirect, jsonify
import telebot
from notion_helper import create_buffer_page, fetch_unsynced_pages, mark_page_as_synced, extract_page_content

# --- تنظیمات ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")

NOTION_CLIENT_ID = os.getenv("NOTION_CLIENT_ID")
NOTION_CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET")
NOTION_REDIRECT_URI = os.getenv("NOTION_REDIRECT_URI")
OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault")

# مسیرهای رسمی OAuth نوشن
NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ==================== بخش ۱: وب‌هوک تلگرام ====================
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    # بررسی هدر امنیتی برای جلوگیری از درخواست‌های جعلی
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != TELEGRAM_WEBHOOK_SECRET:
        return 'Unauthorized', 403

    try:
        update = telebot.types.Update.de_json(request.stream.read().decode('utf-8'))
        bot.process_new_updates([update])
        return 'ok', 200
    except Exception as e:
        logging.error(f"Webhook processing error: {e}")
        return 'error', 500

@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(message, "ربات آماده است. پیام بفرستید تا در Notion ذخیره شود.\n"
                         "برای اتصال حساب Notion از /connect استفاده کنید.")

@bot.message_handler(commands=['connect'])
def handle_connect(message):
    """شروع جریان OAuth نوشن از داخل تلگرام."""
    if not NOTION_CLIENT_ID or not NOTION_REDIRECT_URI:
        bot.reply_to(message, "❌ OAuth نوشن پیکربندی نشده است.")
        return

    # ساخت URL مجوز با پارامترهای استاندارد OAuth 2.0
    auth_url = (
        f"{NOTION_AUTHORIZE_URL}"
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
        create_buffer_page(text)
        bot.reply_to(message, "✅ در بافر Notion ذخیره شد.")
    except Exception as e:
        logging.error(f"Buffer error: {e}")
        bot.reply_to(message, f"❌ خطا در ذخیره‌سازی: {e}")

# ==================== بخش ۲: مسیرهای OAuth نوشن ====================
@app.route('/auth/notion')
def notion_authorize():
    """شروع OAuth از مرورگر (اختیاری، همان منطق /connect)."""
    auth_url = (
        f"{NOTION_AUTHORIZE_URL}"
        f"?client_id={NOTION_CLIENT_ID}"
        f"&response_type=code"
        f"&owner=user"
        f"&redirect_uri={NOTION_REDIRECT_URI}"
    )
    return redirect(auth_url)

@app.route('/auth/notion/callback')
def notion_callback():
    """دریافت code و تبدیل آن به access_token با استفاده مستقیم از API."""
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return f"❌ مجوز رد شد: {error}", 400
    if not code:
        return "❌ خطا: کد مجوز دریافت نشد.", 400

    # نوشن برای تبادل توکن به Basic Auth نیاز دارد
    auth_str = f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()

    try:
        response = requests.post(
            NOTION_TOKEN_URL,
            headers={
                "Authorization": f"Basic {encoded_auth}",
                "Content-Type": "application/json"
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": NOTION_REDIRECT_URI
            },
            timeout=15
        )
        response.raise_for_status()
        token_data = response.json()

        if "access_token" not in token_data:
            return f"❌ پاسخ غیرمنتظره از نوشن: {token_data}", 400

        workspace_name = token_data.get("workspace_name", "نامشخص")
        # ⚠️ در محیط عملیاتی، access_token را در یک دیتابیس امن ذخیره کنید
        logging.info(f"Notion OAuth success. Workspace: {workspace_name}")
        return (
            f"✅ اتصال به Notion با موفقیت انجام شد.<br>"
            f"Workspace: {workspace_name}<br>"
            f"Access Token (فقط نمایش جزئی): {token_data['access_token'][:15]}..."
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Token exchange failed: {e}")
        return f"❌ خطا در تبادل توکن: {e}", 500

# ==================== بخش ۳: همگام‌سازی با Obsidian ====================
@app.route('/sync')
def sync_to_obsidian():
    """همگام‌سازی صفحات سینک‌نشده از Notion به فایل‌های Markdown."""
    unsynced_pages = fetch_unsynced_pages()

    if not unsynced_pages:
        return jsonify({"status": "success", "message": "هیچ صفحه جدیدی برای سینک وجود ندارد."})

    synced_count = 0
    for page in unsynced_pages:
        try:
            title, content = extract_page_content(page)

            # ساخت نام فایل امن
            safe_title = "".join(c for c in title[:30] if c.isalnum() or c in (' ', '_', '-')).strip()
            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_title or 'untitled'}.md"
            filepath = os.path.join(OBSIDIAN_VAULT_PATH, filename)

            # اطمینان از وجود پوشه
            os.makedirs(OBSIDIAN_VAULT_PATH, exist_ok=True)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\n{content}\n")

            mark_page_as_synced(page["id"])
            synced_count += 1
            logging.info(f"Synced to Obsidian: {filename}")

        except Exception as e:
            logging.error(f"Failed to sync page {page.get('id')}: {e}")

    return jsonify({
        "status": "success",
        "synced_count": synced_count,
        "message": f"{synced_count} صفحه با موفقیت سینک شد."
    })

# ==================== سلامت ====================
@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
