import os
import asyncio
import json
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from playwright.async_api import async_playwright

# --- الإعدادات المتغيرة والمفاتيح ---
TOKEN = os.getenv("BOT_TOKEN", "ضع_التوكن_هنا")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

LINKS_FILE = "links.txt"
DATA_FILE = "bot_data.json"
SIGNATURE = "\n\n🌐 *By: LanGoos*"

# --- إدارة البيانات ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"banned_users": [], "full_accounts": []}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_links():
    if not os.path.exists(LINKS_FILE):
        return []
    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def save_links(links):
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for link in links:
            f.write(f"{link}\n")

# --- دالة دمج ورسم شريط التقدم التفاعلي ---
def generate_progress_bar(percent: int, status_text: str, start_time: float, current_acc: int, total_accs: int) -> str:
    filled_length = int(10 * percent // 100)
    bar = "█" * filled_length + "░" * (10 - filled_length)
    
    elapsed_seconds = int(time.time() - start_time)
    
    return (
        f"⏳ **جاري معالجة طلب التفعيل...**\n\n"
        f"`[{bar}]` **{percent}%**\n\n"
        f"📌 **الحالة:** {status_text}\n"
        f"👤 **الحساب الحالي:** {current_acc} من {total_accs}\n"
        f"⏱ **الوقت المنقضي:** {elapsed_seconds} ثانية"
    )

# --- محرك التفعيل بواسطة Playwright مع التحديث التفاعلي ---
async def try_activate_tv(url: str, code: str, update_progress_cb, start_time: float, acc_idx: int, total_accs: int) -> bool:
    async with async_playwright() as p:
        await update_progress_cb(20, "🌐 جاري فتح المتصفح والاتصال بالرابط...", start_time, acc_idx, total_accs)
        
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1000)

            await update_progress_cb(50, "⌨️ تم فتح الصفحة، جاري كتابة كود التفعيل...", start_time, acc_idx, total_accs)
            
            input_selector = 'input[type="text"], input[name*="code"], input[id*="code"], input'
            await page.wait_for_selector(input_selector, timeout=15000)
            
            input_element = page.locator(input_selector).first
            await input_element.click()
            await input_element.fill("")
            await input_element.type(code, delay=100)
            await page.wait_for_timeout(1000)

            await update_progress_cb(80, "🔄 تم إدخال الكود، جاري إرسال الطلب والتحقق...", start_time, acc_idx, total_accs)
            
            submit_button = page.locator('button[type="submit"], input[type="submit"], button:has-text("Continue"), button:has-text("موافق"), button:has-text("تفعيل"), button').first
            if await submit_button.is_visible():
                await submit_button.click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(4000)

            content = await page.content()
            error_indicators = ["incorrect", "invalid", "منتهي", "خطأ", "غير صحيح", "expired"]
            
            for err in error_indicators:
                if err in content.lower():
                    await browser.close()
                    return False

            await browser.close()
            return True

        except Exception as e:
            print(f"Error on {url}: {e}")
            await browser.close()
            return False

# --- أوامر التليجرام ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_data()

    if user_id in data["banned_users"]:
        await update.message.reply_text("❌ أنت محظور من استخدام هذا البوت.")
        return

    welcome_msg = (
        "👋 **أهلاً بك في بوت تفعيل TV**\n\n"
        "لطلب تفعيل الشاشة، أرسل الأمر التالي:\n"
        "`/login 12345678` (مع استبدال الرقم بكود الشاشة الخاص بك)\n\n"
        "🛠 **للأدمن:** يمكنك رفع ملف `.txt` يضم الروابط المتاحة."
        f"{SIGNATURE}"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_data()

    if user_id in data["banned_users"]:
        await update.message.reply_text("❌ أنت محظور من استخدام البوت.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ يرجى إدخال الكود مع الأمر، مثال:\n`/login 69246469`", parse_mode="Markdown")
        return

    code = context.args[0]
    links = get_links()

    if not links:
        await update.message.reply_text("⚠️ لا توجد روابط تفعيل متاحة حالياً. يرجى التواصل مع الإدارة.")
        return

    start_time = time.time()
    total_accs = len(links)

    status_msg = await update.message.reply_text(
        generate_progress_bar(5, "🚀 جاري بدء العملية...", start_time, 1, total_accs),
        parse_mode="Markdown"
    )

    async def update_progress(percent: int, status_text: str, s_time: float, acc_idx: int, t_accs: int):
        try:
            text = generate_progress_bar(percent, status_text, s_time, acc_idx, t_accs)
            await status_msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass

    success = False
    full_accounts = data.get("full_accounts", [])

    for idx, link in enumerate(links, start=1):
        if link in full_accounts:
            continue

        is_activated = await try_activate_tv(link, code, update_progress, start_time, idx, total_accs)
        
        if is_activated:
            success = True
            break
        else:
            if idx < total_accs:
                await update_progress(10, f"❌ فشل الحساب {idx}.. جاري الانتقال للحساب التالي...", start_time, idx + 1, total_accs)
                await asyncio.sleep(1)

    elapsed = int(time.time() - start_time)

    if success:
        final_text = (
            f"🎉 **تم تفعيل الشاشة بنجاح!**\n\n"
            f"`[██████████]` **100%**\n\n"
            f"🔑 **الكود:** `{code}`\n"
            f"⏱ **الوقت الإجمالي:** {elapsed} ثانية"
            f"{SIGNATURE}"
        )
        await status_msg.edit_text(final_text, parse_mode="Markdown")
    else:
        fail_text = (
            f"❌ **فشلت عملية التفعيل.**\n\n"
            f"قد تكون جميع الروابط المتاحة ممتلئة، الكود خاطئ، أو انتهت صلاحية الجلسة.\n"
            f"⏱ **الوقت المستغرق:** {elapsed} ثانية"
            f"{SIGNATURE}"
        )
        await status_msg.edit_text(fail_text, parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔️ هذا الأمر مخصص للأدمن فقط.")
        return

    document = update.message.document
    if not document.file_name.endswith(".txt"):
        await update.message.reply_text("⚠️ يرجى رفع ملف بصيغة `.txt` فقط.")
        return

    file = await context.bot.get_file(document.file_id)
    file_path = "temp_links.txt"
    await file.download_to_drive(file_path)

    with open(file_path, "r", encoding="utf-8") as f:
        new_links = [line.strip() for line in f if line.strip()]

    os.remove(file_path)

    if new_links:
        save_links(new_links)
        data = load_data()
        data["full_accounts"] = []
        save_data(data)

        await update.message.reply_text(
            f"✅ **تم تحديث قائمة الروابط بنجاح!**\n"
            f"عدد الروابط المتاحة: **{len(new_links)}**"
            f"{SIGNATURE}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ الملف المرفوع فارغ.")

def main():
    if TOKEN == "ضع_التوكن_هنا" or not TOKEN:
        print("❌ خطأ: لم يتم ضبط BOT_TOKEN بشكل صحيح!")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("🚀 البوت يعمل ومستعد لاستقبال الأوامر... [By: LanGoos]")
    app.run_polling()

if __name__ == "__main__":
    main()
