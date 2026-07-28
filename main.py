import os
import asyncio
import json
import time
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from playwright.async_api import async_playwright

# --- الإعدادات والمفاتيح ---
TOKEN = os.getenv("BOT_TOKEN", "ضع_التوكن_هنا")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

LINKS_FILE = "links.txt"
DATA_FILE = "bot_data.json"
SIGNATURE = "\n\n🌐 *By: LanGoos*"

# --- إدارة البيانات وتخزين الإحصائيات والحظر ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "temp_banned": {},   # user_id_str: unban_timestamp
        "link_stats": {},    # url: {"success": int, "fail": int}
        "full_accounts": []
    }

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

# --- فحص حالة حظر المستخدم ---
def is_user_banned(user_id: int, data: dict) -> tuple[bool, str]:
    uid_str = str(user_id)
    if uid_str in data.get("temp_banned", {}):
        unban_time = data["temp_banned"][uid_str]
        now = time.time()
        if now < unban_time:
            remaining_seconds = int(unban_time - now)
            days = remaining_seconds // 86400
            hours = (remaining_seconds % 86400) // 3600
            minutes = (remaining_seconds % 3600) // 60
            return True, f"{days} يوم و {hours} ساعة و {minutes} دقيقة"
        else:
            # انتهت مدة الحظر
            del data["temp_banned"][uid_str]
            save_data(data)
    return False, ""

# --- دالة رسم شريط التقدم التفاعلي مع التعليمات ---
def generate_progress_bar(percent: int, status_text: str, start_time: float, current_acc: int, total_accs: int) -> str:
    filled_length = int(10 * percent // 100)
    bar = "█" * filled_length + "░" * (10 - filled_length)
    elapsed_seconds = int(time.time() - start_time)
    
    return (
        f"⏳ **جاري معالجة طلب التفعيل...**\n\n"
        f"`[{bar}]` **{percent}%**\n\n"
        f"📌 **الحالة:** {status_text}\n"
        f"👤 **الحساب الحالي:** {current_acc} من {total_accs}\n"
        f"⏱ **الوقت المنقضي:** {elapsed_seconds} ثانية\n"
        f"───────────────────\n"
        f"⚠️ **شروط وتعليمات مهمة:**\n"
        f"1️⃣ بعد اكتمال التفعيل، لديك **5 دقائق فقط** لتأكيد نجاح العملية عبر الأزرار.\n"
        f"2️⃣ **تجاهل التفاعل أو عدم الضغط سيؤدي لحظر حسابك تلقائياً لمدة 5 أيام!**\n"
        f"3️⃣ يرجى التحقق من شاشة التلفاز مباشرة واختيار النتيجة بدقة."
    )

# --- محرك التفعيل بواسطة Playwright ---
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

            await update_progress_cb(50, "⌨️ تم فتح الصفحة، جاري كتابة الكود...", start_time, acc_idx, total_accs)
            
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
    banned, ban_str = is_user_banned(user_id, data)

    if banned:
        await update.message.reply_text(
            f"🚫 **أنت محظور حالياً من استخدام البوت.**\n⏱ متبقي على فك الحظر: **{ban_str}**",
            parse_mode="Markdown"
        )
        return

    welcome_msg = (
        "👋 **أهلاً بك في بوت تفعيل TV**\n\n"
        "لطلب تفعيل الشاشة، أرسل الأمر التالي:\n"
        "`/login 12345678` (مع استبدال الرقم بكود الشاشة الخاص بك)\n\n"
        "📊 **للإحصائيات:** `/stats`\n"
        "🛠 **للأدمن:** رفع ملف `.txt` لإضافة الروابط."
        f"{SIGNATURE}"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_data()
    banned, ban_str = is_user_banned(user_id, data)

    if banned:
        await update.message.reply_text(
            f"🚫 **أنت محظور من استخدام البوت.**\n⏱ متبقي على فك الحظر: **{ban_str}**",
            parse_mode="Markdown"
        )
        return

    if not context.args:
        await update.message.reply_text("⚠️ يرجى إدخال الكود مع الأمر، مثال:\n`/login 69246469`", parse_mode="Markdown")
        return

    code = context.args[0]
    links = get_links()

    if not links:
        await update.message.reply_text("⚠️ لا توجد روابط تفعيل متاحة حالياً.")
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
    used_link = ""

    for idx, link in enumerate(links, start=1):
        is_activated = await try_activate_tv(link, code, update_progress, start_time, idx, total_accs)
        
        if is_activated:
            success = True
            used_link = link
            break
        else:
            if idx < total_accs:
                await update_progress(10, f"❌ فشل الحساب {idx}.. جاري الانتقال للتالي...", start_time, idx + 1, total_accs)
                await asyncio.sleep(1)

    elapsed = int(time.time() - start_time)

    if success:
        keyboard = [
            [
                InlineKeyboardButton("✅ تم التفعيل بنجاح", callback_data=f"confirm_ok|{user_id}|{used_link}"),
                InlineKeyboardButton("❌ لم يتم التفعيل", callback_data=f"confirm_fail|{user_id}|{used_link}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        final_text = (
            f"🎉 **تم تفعيل الشاشة مبدئياً!**\n\n"
            f"`[██████████]` **100%**\n\n"
            f"🔑 **الكود:** `{code}`\n"
            f"⏱ **الوقت المستغرق:** {elapsed} ثانية\n\n"
            f"⚠️ **تنبيه هام:** اضغط على أحد الزرين أسفله لتأكيد التفعيل.\n"
            f"⏳ **لديك 5 دقائق فقط وإلا سيتم حظرك لمدة 5 أيام تلقائياً!**"
            f"{SIGNATURE}"
        )
        await status_msg.edit_text(final_text, parse_mode="Markdown", reply_markup=reply_markup)

        # جدولة مؤقت الـ 5 دقائق للحظر التلقائي
        context.job_queue.run_once(
            callback=auto_ban_job,
            when=300, # 5 دقائق = 300 ثانية
            data={"user_id": user_id, "msg_id": status_msg.message_id, "chat_id": status_msg.chat_id},
            name=f"ban_{user_id}_{status_msg.message_id}"
        )
    else:
        fail_text = (
            f"❌ **فشلت عملية التفعيل.**\n\n"
            f"قد تكون الروابط ممتلئة، الكود خاطئ، أو انتهت صلاحية الجلسة.\n"
            f"⏱ **الوقت المستغرق:** {elapsed} ثانية"
            f"{SIGNATURE}"
        )
        await status_msg.edit_text(fail_text, parse_mode="Markdown")

# --- معالجة الضغط على الأزرار التفاعلية ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split("|")
    action = data_parts[0]
    target_user_id = int(data_parts[1])
    used_link = data_parts[2] if len(data_parts) > 2 else ""

    if query.from_user.id != target_user_id:
        await query.answer("⛔️ هذه الأزرار خاصة بالمستخدم الذي طلب التفعيل فقط!", show_alert=True)
        return

    # إيقاف مؤقت الحظر التلقائي
    jobs = context.job_queue.get_jobs_by_name(f"ban_{target_user_id}_{query.message.message_id}")
    for job in jobs:
        job.schedule_removal()

    bot_data = load_data()
    if used_link not in bot_data["link_stats"]:
        bot_data["link_stats"][used_link] = {"success": 0, "fail": 0}

    if action == "confirm_ok":
        bot_data["link_stats"][used_link]["success"] += 1
        save_data(bot_data)
        
        await query.edit_message_text(
            f"✅ **تم تسجيل ردك بنجاح!**\nشكراً لتأكيدك، نتمنى لك مشاهدة ممتعة. 🍿"
            f"{SIGNATURE}",
            parse_mode="Markdown"
        )
    elif action == "confirm_fail":
        bot_data["link_stats"][used_link]["fail"] += 1
        save_data(bot_data)

        await query.edit_message_text(
            f"⚠️ **تم تسجيل ملاحظتك.**\nسنقوم بمراجعة هذا الرابط وإصلاحه قريباً."
            f"{SIGNATURE}",
            parse_mode="Markdown"
        )

# --- وظيفة الحظر التلقائي عند انتهاء الـ 5 دقائق ---
async def auto_ban_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data["user_id"]
    msg_id = job_data["msg_id"]
    chat_id = job_data["chat_id"]

    bot_data = load_data()
    # حظر لمدة 5 أيام (5 * 86400 ثانية)
    unban_timestamp = time.time() + (5 * 86400)
    bot_data["temp_banned"][str(user_id)] = unban_timestamp
    save_data(bot_data)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=f"🚫 **تم حظر هذا الحساب لمدة 5 أيام تلقائياً.**\nالسبب: عدم تأكيد التفعيل خلال المهلة المحددة (5 دقائق)."
                 f"{SIGNATURE}",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# --- أمر الإحصائيات الشامل /stats ---
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = load_data()
    links = get_links()
    link_stats = bot_data.get("link_stats", {})
    temp_banned = bot_data.get("temp_banned", {})

    total_links = len(links)
    total_success = sum(stat.get("success", 0) for stat in link_stats.values())
    total_fail = sum(stat.get("fail", 0) for stat in link_stats.values())
    total_ops = total_success + total_fail

    success_rate = (total_success / total_ops * 100) if total_ops > 0 else 0

    stats_text = (
        "📈 **تخريج وإحصائيات البوت الشاملة**\n"
        "───────────────────\n\n"
        f"🔗 **إجمالي الروابط المتاحة:** `{total_links}`\n"
        f"✅ **عمليات التفعيل الناجحة:** `{total_success}`\n"
        f"❌ **عمليات التفعيل الفاشلة:** `{total_fail}`\n"
        f"🎯 **نسبة نجاح البوت:** `{success_rate:.1f}%`\n"
        f"🚫 **عدد المحظورين حالياً:** `{len(temp_banned)}`\n\n"
        "📌 **تفاصيل الروابط بالتفصيل:**\n"
    )

    if links:
        for idx, link in enumerate(links, start=1):
            s = link_stats.get(link, {}).get("success", 0)
            f = link_stats.get(link, {}).get("fail", 0)
            stats_text += f"🔹 **رابط {idx}:** `{s}` نجاح | `{f}` فشل\n"
    else:
        stats_text += "⚠️ لا توجد روابط مسجلة حالياً.\n"

    stats_text += f"{SIGNATURE}"
    await update.message.reply_text(stats_text, parse_mode="Markdown")

# --- أمر فك الحظر يدويًا للأدمن /unban ---
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ هذا الأمر مخصص للأدمن فقط.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ اكتب آيدي المستخدم بعد الأمر، مثال:\n`/unban 12345678`", parse_mode="Markdown")
        return

    target_id = context.args[0]
    bot_data = load_data()

    if target_id in bot_data.get("temp_banned", {}):
        del bot_data["temp_banned"][target_id]
        save_data(bot_data)
        await update.message.reply_text(f"✅ تم فك الحظر بنجاح عن المستخدم `{target_id}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ هذا المستخدم غير محظور حالياً.")

# --- استقبال ملف الروابط من الأدمن ---
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
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("stat", stats_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("🚀 البوت يعمل ومستعد لاستقبال الأوامر... [By: LanGoos]")
    app.run_polling()

if __name__ == "__main__":
    main()
