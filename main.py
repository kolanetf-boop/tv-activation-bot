import os
import asyncio
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from playwright.async_api import async_playwright

# --- الإعدادات المتغيرة والمفاتيح ---
TOKEN = os.getenv("BOT_TOKEN", "ضع_التوكن_الخاص_بك_هنا")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # رقم الـ ID الخاص بك على تليجرام

LINKS_FILE = "links.txt"
DATA_FILE = "bot_data.json"

# --- إدارة حفظ البيانات (المحظورين + الحسابات الممتلئة مؤقتاً) ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"banned_users": [], "full_links": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_links():
    if not os.path.exists(LINKS_FILE):
        return []
    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

# --- وظيفة إرسال تنبيهات للأدمن ---
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    if ADMIN_ID != 0:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔔 **تنبيه الإدارة:**\n{message}", parse_mode="Markdown")
        except Exception as e:
            print(f"Error sending admin notification: {e}")

# --- الأوامر الرئيسية ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **أهلاً بك في بوت تفعيل الشاشات الذكي!**\n\n"
        "📺 **للتفعيل:** أرسل الأمر متبوعاً بكود الشاشة (8 أرقام):\n"
        "`/login 12345678`\n\n"
        "⚙️ **للأدمن:**\n"
        "• `/addlinks`: لرفع ملف الروابط\n"
        "• `/stats`: لإحصائيات البوت\n"
        "• `/banned`: لمشاهدة المحظورين\n"
        "• `/unban USER_ID`: لإلغاء حظر صديق",
        parse_mode="Markdown"
    )

# 1. رفع ملف الروابط (للأدمن)
async def add_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ عذراً، هذا الأمر مخصص للمشرف فقط!")
        return
    await update.message.reply_text("📥 يرجى إرسال ملف `.txt` يحتوي على الروابط الآن.")
    context.user_data['awaiting_file'] = True

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.user_data.get('awaiting_file'):
        return

    file = await update.message.document.get_file()
    await file.download_to_drive(LINKS_FILE)
    context.user_data['awaiting_file'] = False
    
    links = get_links()
    await update.message.reply_text(f"✅ تم تحديث قائمة الروابط بنجاح!\nعدد الروابط المتاحة: {len(links)}")
    await notify_admin(context, f"تم رفع ملف روابط جديد يحتوي على **{len(links)}** رابط.")

# 2. أمر التفعيل التلقائي /login
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()

    # فحص الحظر
    if user.id in data["banned_users"]:
        await update.message.reply_text("⛔ أنت محظور مؤقتاً من استخدام البوت لعدم تأكيد نتيجة التفعيل السابقة خلال 5 دقائق.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ يرجى كتابة كود التفعيل المكون من 8 أرقام، مثال:\n`/login 12345678`", parse_mode="Markdown")
        return

    tv_code = context.args[0]
    links = get_links()

    if not links:
        await update.message.reply_text("❌ لا توجد روابط متاحة حالياً، يرجى التواصل مع المشرف.")
        return

    msg = await update.message.reply_text("⏳ جاري بدء محرك الأتمتة وتجربة الحسابات...")

    successful_link = None
    failed_reason = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # محاكاة متصفح حقيقي لتفادي الحظر (Anti-Bot)
        browser_context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
        )
        page = await browser_context.new_page()

        for idx, link in enumerate(links, 1):
            await msg.edit_text(f"🔄 جاري محاولة التفعيل على الحساب رقم ({idx}/{len(links)})... ⏳")
            
            try:
                # الانتقال للرابط
                response = await page.goto(link, timeout=12000)
                await asyncio.sleep(2)

                # فحص انتهاء الصلاحية
                content = await page.content()
                if "login" in page.url.lower() or "expired" in content.lower():
                    await notify_admin(context, f"⚠️ **الرابط رقم {idx} منتهي الصلاحية!**\nيرجى تجديده.")
                    continue

                # تعبئة كود الـ 8 أرقام
                await page.fill("input[type='text'], input[name='code']", tv_code)
                await page.click("button[type='submit']")
                await asyncio.sleep(4)

                # فحص امتلاء الحساب بشاشات
                page_text = await page.content()
                if "too many screens" in page_text.lower() or "limit" in page_text.lower():
                    await notify_admin(context, f"ℹ️ **الحساب رقم {idx} ممتلئ حالياً بالشاشات.** (تم تخطيه تلقائياً).")
                    continue

                # إذا نجحت العملية
                successful_link = idx
                break

            except Exception as e:
                continue

        await browser.close()

    if successful_link:
        keyboard = [
            [
                InlineKeyboardButton("🟢 تم التفعيل وتعمل الشاشة", callback_data=f"ok_{user.id}_{successful_link}"),
                InlineKeyboardButton("🔴 لم تعمل الشاشة", callback_data=f"fail_{user.id}_{successful_link}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.edit_text(
            f"🎉 **تمت محاولة التفعيل على الحساب رقم {successful_link}!**\n\n"
            "⚠️ **تنبيه هام:** يرجى تأكيد النتيجة بالضغط على أحد الأزرار أسفله خلال **5 دقائق** وإلا سيتم حظرك تلقائياً من البوت.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        # تنبيه الأدمن ببدء العملية
        await notify_admin(context, f"👤 قام المستخدم [{user.full_name}](tg://user?id={user.id}) بالتفعيل على الحساب رقم **{successful_link}** (في انتظار تأكيده).")

        # تشغيل مؤقت الـ 5 دقائق
        asyncio.create_task(start_feedback_timer(context, user.id, msg.message_id, update.effective_chat.id, user.full_name))
    else:
        await msg.edit_text("❌ فشلت المحاولة على جميع الحسابات المتاحة. قد تكون الروابط منتهية أو الأرقام خاطئة.")

# --- نظام التقييم والحظر التلقائي ---

async def start_feedback_timer(context, user_id, message_id, chat_id, user_name):
    await asyncio.sleep(300) # 5 دقائق

    responded = context.bot_data.get("responded_users", set())
    if user_id not in responded:
        # إضافة المستخدم لقائمة المحظورين
        data = load_data()
        if user_id not in data["banned_users"]:
            data["banned_users"].append(user_id)
            save_data(data)

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ **تم حظرك مؤقتاً!**\nانتهت مهلة الـ 5 دقائق دون تأكيد نتيجة التفعيل.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # إرسال إشعار فوري للأدمن
        await notify_admin(context, f"🚫 **حظر تلقائي:** تم حظر المستخدم [{user_name}](tg://user?id={user_id}) لعدم التجاوب خلال 5 دقائق.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_code = query.data
    user_id = query.from_user.id

    if "responded_users" not in context.bot_data:
        context.bot_data["responded_users"] = set()
    context.bot_data["responded_users"].add(user_id)

    if data_code.startswith("ok_"):
        link_num = data_code.split("_")[2]
        await query.edit_message_text("✅ شكراً لتأكيدك! مشاهدة ممتعة 🍿")
        await notify_admin(context, f"🟢 أكّد المستخدم [{query.from_user.full_name}](tg://user?id={user_id}) أن التفعيل **نجح بنجاح** على الحساب {link_num}.")

    elif data_code.startswith("fail_"):
        link_num = data_code.split("_")[2]
        await query.edit_message_text("⚠️ تم تسليم إبلاغك للمشرف لفحص الحساب.")
        await notify_admin(context, f"🔴 أبلغ المستخدم [{query.from_user.full_name}](tg://user?id={user_id}) أن التفعيل **فشل** على الحساب {link_num}!")

# --- الأوامر الإدارية (Admin Commands) ---

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    links = get_links()
    data = load_data()
    await update.message.reply_text(
        f"📊 **إحصائيات البوت الحالية:**\n\n"
        f"🔗 عدد الروابط المتاحة: `{len(links)}`\n"
        f"⛔ عدد المحظورين حالياً: `{len(data['banned_users'])}`",
        parse_mode="Markdown"
    )

async def banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_data()
    banned = data.get("banned_users", [])
    if not banned:
        await update.message.reply_text("✅ لا يوجد أي مستخدم محظور حالياً.")
    else:
        text = "⛔ **قائمة المحظورين (IDs):**\n" + "\n".join([f"• `{uid}`" for uid in banned])
        await update.message.reply_text(text, parse_mode="Markdown")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("⚠️ يرجى كتابة الـ ID للمستخدم، مثال:\n`/unban 123456789`", parse_mode="Markdown")
        return
    
    target_id = int(context.args[0])
    data = load_data()
    if target_id in data["banned_users"]:
        data["banned_users"].remove(target_id)
        save_data(data)
        await update.message.reply_text(f"✅ تم إلغاء حظر المستخدم `{target_id}` بنجاح!", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ هذا المستخدم غير موجود في قائمة الحظر.")

# --- تشغيل البوت ---
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addlinks", add_links_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("banned", banned_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل ومستعد لاستقبال الأوامر...")
    app.run_polling()

if __name__ == "__main__":
    main()
