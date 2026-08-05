import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# إعداد التسجيل (Logging) لمعاينة الأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- إعدادات النظام ---
BOT_TOKEN = "8998319897:AAE1GVQ-gg7dtOYwZwhAC1NcxiPmbnZatqU"  # ضع توكن البوت الخاص بك هنا
BATCH_SIZE = 10         # عدد الحسابات في الدفعة الواحدة
TIMEOUT_SECONDS = 180   # مهلة الانتظار (3 دقائق = 180 ثانية)

# تخزين بيانات الجلسات في الذاكرة
# Structure:
# user_data_store[user_id] = {
#     "all_accounts": [...],             # قائمة الحسابات المستهدفة بالفحص
#     "saved_working_accounts": [...],   # الحسابات التي ثبت أنها شغالة وتفتح الشاشة
#     "current_index": 0,                # مؤشر الفحص الحالي
#     "active_code": "123456",           # الكود المرسل من المستخدم
#     "timeout_task": asyncio.Task       # مهمة مؤقت الـ 3 دقائق
# }
user_data_store = {}

# -------------------------------------------------------------------
# 1. دالة فحص الحساب برمجياً (تستبدلها بمنطق الفحص الخاص بك)
# -------------------------------------------------------------------
async def check_account_status(account: str, code: str):
    """
    تقوم هذه الدالة بفحص الحساب وترجع قيمتين:
    - is_working: (True/False) هل الحساب شغال ويفتح الشاشة؟
    - is_activated: (True/False) هل نجح التفعيل بواسطة الكود؟
    """
    await asyncio.sleep(1)  # محاكاة وقت الفحص البرمجي (Selenium / API / Requests)
    
    # --- مثال توضيحي للمخرجات ---
    # is_working: True إذا كان الحساب سليماً، False إذا كانت الشاشة لا تفتح
    # is_activated: True إذا تم التفعيل بنجاح
    is_working = True
    is_activated = False
    
    return is_working, is_activated


# -------------------------------------------------------------------
# 2. أمر البداية /start
# -------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك في بوت التفعيل الفحص التفاعلي.\n\n"
        "يرجى إرسال **كود التفعيل** لبدء عملية الفحص على الدفعة الأولى (10 حسابات)."
    )


# -------------------------------------------------------------------
# 3. استقبال الكود من المستخدم وبدء الجلسة
# -------------------------------------------------------------------
async def handle_code_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_code = update.message.text.strip()

    # إلغاء أي مؤقت سابق للمستخدم إن وجد
    if user_id in user_data_store and user_data_store[user_id].get("timeout_task"):
        user_data_store[user_id]["timeout_task"].cancel()

    # في حال وجود حسابات شغالة محفوظة سابقاً يتم اعتمادها، وإلا يتم جلب الـ 100 حساب من الأدمن
    if user_id in user_data_store and user_data_store[user_id].get("saved_working_accounts"):
        base_accounts = user_data_store[user_id]["saved_working_accounts"]
    else:
        # محاكاة: جلب 100 حساب من قاعدة بيانات الأدمن
        base_accounts = [f"account_{i}@example.com" for i in range(1, 101)]

    # إنشاء / تحديث جلسة المستخدم
    user_data_store[user_id] = {
        "all_accounts": base_accounts,
        "saved_working_accounts": user_data_store.get(user_id, {}).get("saved_working_accounts", []),
        "current_index": 0,
        "active_code": user_code,
        "timeout_task": None
    }

    await update.message.reply_text(f"📥 تم استقبال الكود: `{user_code}`\nجاري بدء التجهيز للفحص...", parse_mode="Markdown")
    await process_next_batch(update, context, user_id)


# -------------------------------------------------------------------
# 4. دالة معالجة وفحص 10 حسابات (Batch Processing)
# -------------------------------------------------------------------
async def process_next_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    data = user_data_store.get(user_id)
    if not data:
        return

    # تحديد الحسابات المتبقية التي لم تفحص بعد
    remaining_accounts = data["all_accounts"][data["current_index"]:]

    # إذا انتهت جميع الحسابات المتاحة
    if not remaining_accounts:
        working_count = len(data["saved_working_accounts"])
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ انتهت جميع الحسابات المتاحة ولم ينجح التفعيل.\n🔹 عدد الحسابات الشغالة المحفوظة: {working_count}"
        )
        del user_data_store[user_id]
        return

    # اقتطاع 10 حسابات فقط للفحص
    batch_to_test = remaining_accounts[:BATCH_SIZE]
    
    await context.bot.send_message(
        chat_id=user_id,
        text=f"⏳ جاري فحص دفعة مكونة من {len(batch_to_test)} حسابات..."
    )

    activation_success = False
    successful_account = None

    # فحص الـ 10 حسابات تسلسلياً
    for acc in batch_to_test:
        is_working, is_activated = await check_account_status(acc, data["active_code"])
        
        # تصفية الحسابات: حفظ الشغال فقط واستبعاد غير الشغال
        if is_working:
            if acc not in data["saved_working_accounts"]:
                data["saved_working_accounts"].append(acc)
        else:
            logging.info(f"تم استبعاد الحساب غير الشغال: {acc}")

        # التوقف الفوري عند نجاح التفعيل
        if is_activated:
            activation_success = True
            successful_account = acc
            break

    # تحديث المؤشر بـ 10 حسابات للامام
    data["current_index"] += len(batch_to_test)

    # --- الحالة الأولى: السكريبت اكتشف التفعيل بنجاح ---
    if activation_success:
        keyboard = [
            [
                InlineKeyboardButton("✅ تم التفعيل", callback_data="status_success"),
                InlineKeyboardButton("❌ لم يتم التفعيل", callback_data="status_failed")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎯 **أظهر السكريبت أنه تم التفعيل!**\n"
                f"📧 الحساب: `{successful_account}`\n\n"
                f"⏰ **يرجى التأكيد بالضغط على أحد الأزرار أدناه خلال 3 دقائق:**"
            ),
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        # تشغيل مؤقت الـ 3 دقائق لانتظار تفاعل المستخدم
        task = asyncio.create_task(handle_timeout(context, user_id, msg.message_id))
        data["timeout_task"] = task

    # --- الحالة الثانية: اكتمل فحص الـ 10 ولم ينجح التفعيل ---
    else:
        working_count = len(data["saved_working_accounts"])
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ لم يتم التفعيل في هذه الدفعة (10 حسابات).\n"
                f"🔹 إجمالي الحسابات الشغالة المحفوظة حتى الآن: {working_count}\n\n"
                f"👉 يرجى **إعادة إرسال الكود مرة أخرى** للبدء في فحص الـ 10 التالية."
            )
        )


# -------------------------------------------------------------------
# 5. معالجة الضغط على أزرار التفاعل (Inline Buttons)
# -------------------------------------------------------------------
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    action = query.data

    data = user_data_store.get(user_id)
    if not data:
        await query.edit_message_text("⚠️ انتهت هذه الجلسة أو غير صالحة.")
        return

    # إلغاء مؤقت الـ 3 دقائق فور تفاعل المستخدم
    if data.get("timeout_task"):
        data["timeout_task"].cancel()

    if action == "status_success":
        await query.edit_message_text("🎉 ممتاز! تم تأكيد التفعيل بنجاح. شكراً لك!")
        # إنهاء الجلسة بنجاح
        del user_data_store[user_id]

    elif action == "status_failed":
        await query.edit_message_text(
            "❌ تم تسجيل عدم التفعيل.\n\n"
            "يرجى **إعادة إرسال الكود** من جديد ليقوم البوت بفحص الـ 10 حسابات التالية من القائمة الشغالة."
        )


# -------------------------------------------------------------------
# 6. معالجة انقضاء مهلة الـ 3 دقائق بدون تفاعل
# -------------------------------------------------------------------
async def handle_timeout(context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int):
    try:
        await asyncio.sleep(TIMEOUT_SECONDS)
        
        # في حال انتهاء الـ 3 دقائق ولم يضغط المستخدم على الأزرار
        data = user_data_store.get(user_id)
        if data:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text="⚠️ **انتهت مهلة الـ 3 دقائق دون أي تفاعل.**\nتم إلغاء الجلسة الحالية. يرجى إرسال الكود من جديد لإعادة المحاولة.",
                parse_mode="Markdown"
            )
            # مسح الجلسة بسبب عدم التفاعل
            del user_data_store[user_id]

    except asyncio.CancelledError:
        # استدعاء هذا الاستثناء يعني أن المستخدم ضغط على أحد الأزرار وتم تمويل إلغاء المؤقت بنجاح
        pass


# -------------------------------------------------------------------
# 7. الدالة الرئيسية لتشغيل البوت
# -------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # الأوامر والرسائل
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code_submission))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    print("🤖 البوت يعمل الآن بنجاح...")
    app.run_polling()

if __name__ == "__main__":
    main()
