"""
LanGoos TV Activation Bot
=========================
Telegram bot (python-telegram-bot v20+) + Playwright (Chromium) for
automatic TV activation through configurable links.

Features
--------
- /start, /login <code>, /stats, /unban (admin)
- Admin uploads `links.txt` to update activation URLs
- Interactive progress bar with throttled edits (Telegram rate-limit safe)
- 5-minute confirm window; auto-ban (5 days) if user doesn't reply
- Per-link success/fail statistics with "By: LanGoos" signature
- Single shared Playwright instance (no per-call spin-up), robust
  try/finally cleanup, defensive error handling
- File-level asyncio.Lock to keep bot_data.json consistent
- Graceful shutdown

Author: LanGoos
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError

# =====================================================================
# Logging
# =====================================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
log = logging.getLogger("langoos")


# =====================================================================
# Config
# =====================================================================
@dataclass(frozen=True)
class Config:
    """Central configuration. Pull from env, fall back to safe defaults."""

    token: str
    admin_id: int

    links_file: Path = Path("links.txt")
    data_file: Path = Path("bot_data.json")

    signature: str = "\n\n🌐 *By: LanGoos*"

    confirm_timeout_seconds: int = 300
    ban_duration_seconds: int = 5 * 24 * 3600

    progress_min_interval: float = 1.5
    progress_max_interval: float = 4.0

    page_load_timeout_ms: int = 30_000
    input_selector_timeout_ms: int = 15_000
    post_submit_wait_ms: int = 4_000

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        admin_id_raw = os.getenv("ADMIN_ID", "0").strip()
        if not token or token == "ضع_التوكن_هنا":
            raise RuntimeError("❌ BOT_TOKEN is not configured. Set it in env.")
        try:
            admin_id = int(admin_id_raw)
        except ValueError as e:
            raise RuntimeError("❌ ADMIN_ID must be an integer.") from e
        return cls(token=token, admin_id=admin_id)


CFG = Config.from_env()


# =====================================================================
# Data layer (file I/O with asyncio lock)
# =====================================================================
class DataStore:
    DEFAULT: dict[str, Any] = {
        "temp_banned": {},
        "link_stats": {},
        "full_accounts": [],
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> dict[str, Any]:
        async with self._lock:
            if not self.path.exists():
                return json.loads(json.dumps(self.DEFAULT))
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Data file unreadable, starting fresh: %s", e)
                data = json.loads(json.dumps(self.DEFAULT))
            for k, v in self.DEFAULT.items():
                data.setdefault(k, json.loads(json.dumps(v)))
            return data

    async def save(self, data: dict[str, Any]) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp, self.path)

    async def update(self, mutator):
        async with self._lock:
            if not self.path.exists():
                data = json.loads(json.dumps(self.DEFAULT))
            else:
                try:
                    with self.path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    data = json.loads(json.dumps(self.DEFAULT))
            for k, v in self.DEFAULT.items():
                data.setdefault(k, json.loads(json.dumps(v)))
            result = mutator(data)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp, self.path)
            return data if result is None else result


class LinkStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def read(self) -> list[str]:
        async with self._lock:
            if not self.path.exists():
                return []
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    return [ln.strip() for ln in f if ln.strip()]
            except OSError as e:
                log.warning("Failed to read links file: %s", e)
                return []

    async def write(self, links: list[str]) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for ln in links:
                    f.write(f"{ln}\n")
            os.replace(tmp, self.path)


DATA = DataStore(CFG.data_file)
LINKS = LinkStore(CFG.links_file)


# =====================================================================
# Helpers
# =====================================================================
def _format_remaining(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days} يوم و {hours} ساعة و {minutes} دقيقة"


async def is_user_banned(user_id: int) -> tuple[bool, str]:
    banned_text = ""

    def _check(data: dict[str, Any]) -> None:
        nonlocal banned_text
        uid = str(user_id)
        unban_ts = data.get("temp_banned", {}).get(uid)
        if unban_ts is None:
            return
        now = time.time()
        if now < unban_ts:
            banned_text = _format_remaining(int(unban_ts - now))
        else:
            data["temp_banned"].pop(uid, None)

    await DATA.update(_check)
    return bool(banned_text), banned_text


def generate_progress_bar(
    percent: int,
    status_text: str,
    start_time: float,
    current_acc: int,
    total_accs: int,
) -> str:
    percent = max(0, min(100, percent))
    filled = int(10 * percent // 100)
    bar = "█" * filled + "░" * (10 - filled)
    elapsed = int(time.time() - start_time)
    return (
        "⏳ *جاري معالجة طلب التفعيل...*\n\n"
        f"`[{bar}]` *{percent}%*\n\n"
        f"📌 *الحالة:* {status_text}\n"
        f"👤 *الحساب الحالي:* {current_acc} من {total_accs}\n"
        f"⏱ *الوقت المنقضي:* {elapsed} ثانية\n"
        "───────────────────\n"
        "⚠️ *شروط وتعليمات مهمة:*\n"
        "1️⃣ بعد اكتمال التفعيل، لديك *5 دقائق فقط* لتأكيد العملية.\n"
        "2️⃣ *عدم التفاعل سيؤدي لحظر حسابك تلقائياً لمدة 5 أيام!*\n"
        "3️⃣ تحقق من شاشة التلفاز مباشرة واختر النتيجة بدقة."
    )


# =====================================================================
# Playwright manager (singleton)
# =====================================================================
class PlaywrightManager:
    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            log.info("Starting Playwright (Chromium)...")
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            log.info("Playwright ready.")

    async def stop(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    log.exception("Error while closing browser.")
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    log.exception("Error while stopping Playwright.")
                self._pw = None
            log.info("Playwright stopped.")

    @asynccontextmanager
    async def new_page(self):
        if self._browser is None:
            raise RuntimeError("PlaywrightManager not started.")
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None
        try:
            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )
            page = await context.new_page()
            yield context, page
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    log.exception("Error closing page.")
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    log.exception("Error closing context.")


PW = PlaywrightManager()


# =====================================================================
# Activation engine
# =====================================================================
ERROR_INDICATORS = (
    "incorrect", "invalid", "منتهي", "خطأ", "غير صحيح", "expired", "error",
)


async def try_activate_tv(
    url: str,
    code: str,
    update_progress_cb,
    start_time: float,
    acc_idx: int,
    total_accs: int,
) -> bool:
    if PW._browser is None:
        await PW.start()

    try:
        await update_progress_cb(20, "🌐 جاري الاتصال بالرابط...", start_time, acc_idx, total_accs)

        async with PW.new_page() as (_ctx, page):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=CFG.page_load_timeout_ms)
            except Exception as e:
                log.warning("goto failed for %s: %s", url, e)
                return False

            await page.wait_for_timeout(800)

            await update_progress_cb(50, "⌨️ تم فتح الصفحة، جاري كتابة الكود...", start_time, acc_idx, total_accs)

            input_selectors = (
                'input[type="text"]',
                'input[name*="code" i]',
                'input[id*="code" i]',
                'input[type="search"]',
                'input',
            )
            input_el = None
            for sel in input_selectors:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=CFG.input_selector_timeout_ms // len(input_selectors))
                    if await el.is_visible():
                        input_el = el
                        break
                except Exception:
                    continue
            if input_el is None:
                log.info("No input found on %s", url)
                return False

            try:
                await input_el.click()
                await input_el.fill("")
                await input_el.type(code, delay=80)
            except Exception as e:
                log.warning("Typing failed on %s: %s", url, e)
                return False

            await page.wait_for_timeout(700)

            await update_progress_cb(80, "🔄 جاري إرسال الطلب والتحقق...", start_time, acc_idx, total_accs)

            submitted = False
            submit_selectors = (
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("موافق")',
                'button:has-text("تفعيل")',
                'button:has-text("OK")',
                'button',
            )
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        submitted = True
                        break
                except Exception:
                    continue
            if not submitted:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    return False

            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=CFG.post_submit_wait_ms
                )
            except Exception:
                await page.wait_for_timeout(CFG.post_submit_wait_ms)

            try:
                content = (await page.content()).lower()
            except Exception:
                content = ""

            for err in ERROR_INDICATORS:
                if err in content:
                    return False
            return True

    except Exception:
        log.exception("Unexpected error in try_activate_tv for %s", url)
        return False


# =====================================================================
# Throttled progress message editor
# =====================================================================
class ProgressEditor:
    def __init__(self, message, start_time: float) -> None:
        self.message = message
        self.start_time = start_time
        self._last_edit_ts: float = 0.0
        self._pending: Optional[tuple[int, str, int, int]] = None
        self._task: Optional[asyncio.Task] = None

    async def __call__(self, percent: int, status: str, s_time: float, acc_idx: int, total: int) -> None:
        now = time.time()
        self._pending = (percent, status, acc_idx, total)
        if now - self._last_edit_ts >= CFG.progress_min_interval:
            await self._flush()
        else:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(CFG.progress_min_interval)
            if self._pending is not None:
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        if self._pending is None:
            return
        percent, status, acc_idx, total = self._pending
        self._pending = None
        self._last_edit_ts = time.time()
        text = generate_progress_bar(percent, status, self.start_time, acc_idx, total)
        try:
            await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except RetryAfter as e:
            log.info("FloodWait: sleeping %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 0.5)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("Edit failed: %s", e)
        except (TimedOut, NetworkError) as e:
            log.warning("Transient Telegram error: %s", e)
        except Exception:
            log.exception("Unexpected error editing progress message.")


# =====================================================================
# Commands
# =====================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    banned, ban_str = await is_user_banned(user_id)
    if banned:
        await update.message.reply_text(
            f"🚫 *أنت محظور حالياً من استخدام البوت.*\n⏱ متبقي على فك الحظر: *{ban_str}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "👋 *أهلاً بك في بوت تفعيل TV*\n\n"
        "لطلب تفعيل الشاشة، أرسل الأمر:\n"
        "`/login 12345678`\n\n"
        "📊 *للإحصائيات:* `/stats`\n"
        "🛠 *للأدمن:* ارفع ملف `.txt` لتحديث الروابط."
        f"{CFG.signature}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    banned, ban_str = await is_user_banned(user_id)
    if banned:
        await update.message.reply_text(
            f"🚫 *أنت محظور من استخدام البوت.*\n⏱ متبقي على فك الحظر: *{ban_str}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ يرجى إدخال الكود مع الأمر، مثال:\n`/login 69246469`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code = context.args[0].strip()
    if not code.isdigit() or not (4 <= len(code) <= 32):
        await update.message.reply_text(
            "⚠️ صيغة الكود غير صحيحة. يجب أن يكون أرقاماً فقط (4-32 رقم).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    links = await LINKS.read()
    if not links:
        await update.message.reply_text(
            "⚠️ لا توجد روابط تفعيل متاحة حالياً."
            f"{CFG.signature}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    start_time = time.time()
    total = len(links)
    status_msg = await update.message.reply_text(
        generate_progress_bar(5, "🚀 جاري بدء العملية...", start_time, 1, total),
        parse_mode=ParseMode.MARKDOWN,
    )

    progress = ProgressEditor(status_msg, start_time)
    typing_task = asyncio.create_task(_keep_typing(context, update.effective_chat.id))

    success = False
    used_link = ""
    failure_reason = ""

    try:
        for idx, link in enumerate(links, start=1):
            ok = await try_activate_tv(link, code, progress, start_time, idx, total)
            if ok:
                success = True
                used_link = link
                break
            if idx < total:
                await progress(
                    10,
                    f"❌ فشل الحساب {idx}.. جاري الانتقال للتالي...",
                    start_time,
                    idx + 1,
                    total,
                )
                await asyncio.sleep(0.8)
        else:
            failure_reason = "جميع الروابط رفضت الكود أو انتهت صلاحيتها."
    except Exception:
        log.exception("Unhandled error in /login")
        failure_reason = "حدث خطأ غير متوقع أثناء المعالجة."
    finally:
        typing_task.cancel()
        await progress(100, "✅" if success else "❌", start_time, total, total)

    elapsed = int(time.time() - start_time)

    if success:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ تم التفعيل بنجاح",
                        callback_data=f"confirm_ok|{user_id}|{used_link}",
                    ),
                    InlineKeyboardButton(
                        "❌ لم يتم التفعيل",
                        callback_data=f"confirm_fail|{user_id}|{used_link}",
                    ),
                ]
            ]
        )
        final_text = (
            "🎉 *تم تفعيل الشاشة مبدئياً!*\n\n"
            "`[██████████]` *100%*\n\n"
            f"🔑 *الكود:* `{code}`\n"
            f"⏱ *الوقت المستغرق:* {elapsed} ثانية\n\n"
            "⚠️ اضغط أحد الزرين لتأكيد التفعيل.\n"
            f"⏳ *لديك 5 دقائق فقط، وإلا سيتم حظرك لمدة 5 أيام تلقائياً!*"
            f"{CFG.signature}"
        )
        try:
            await status_msg.edit_text(
                final_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
        except BadRequest as e:
            log.warning("Could not attach keyboard to success message: %s", e)
            await update.message.reply_text(
                final_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )

        context.job_queue.run_once(
            auto_ban_job,
            when=CFG.confirm_timeout_seconds,
            data={
                "user_id": user_id,
                "msg_id": status_msg.message_id,
                "chat_id": status_msg.chat_id,
            },
            name=f"ban_{user_id}_{status_msg.message_id}",
        )
    else:
        fail_text = (
            "❌ *فشلت عملية التفعيل.*\n\n"
            f"{failure_reason or 'قد تكون الروابط ممتلئة أو الكود غير صحيح.'}\n"
            f"⏱ *الوقت المستغرق:* {elapsed} ثانية"
            f"{CFG.signature}"
        )
        try:
            await status_msg.edit_text(fail_text, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await update.message.reply_text(fail_text, parse_mode=ParseMode.MARKDOWN)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split("|")
    if len(parts) < 3:
        await query.answer("⚠️ بيانات الزر غير صالحة.", show_alert=True)
        return

    action, user_id_str, used_link = parts[0], parts[1], parts[2]
    try:
        target_user_id = int(user_id_str)
    except ValueError:
        await query.answer("⚠️ معرف المستخدم غير صالح.", show_alert=True)
        return

    if query.from_user.id != target_user_id:
        await query.answer("⛔️ هذه الأزرار خاصة بالمستخدم الذي طلب التفعيل فقط!", show_alert=True)
        return

    for job in context.job_queue.get_jobs_by_name(f"ban_{target_user_id}_{query.message.message_id}"):
        job.schedule_removal()

    if action == "confirm_ok":
        def _inc(data: dict[str, Any]) -> None:
            stats = data["link_stats"].setdefault(used_link, {"success": 0, "fail": 0})
            stats["success"] = stats.get("success", 0) + 1

        await DATA.update(_inc)
        await query.edit_message_text(
            f"✅ *تم تسجيل ردك بنجاح!*\nشكراً لتأكيدك، نتمنى لك مشاهدة ممتعة. 🍿"
            f"{CFG.signature}",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "confirm_fail":
        def _inc(data: dict[str, Any]) -> None:
            stats = data["link_stats"].setdefault(used_link, {"success": 0, "fail": 0})
            stats["fail"] = stats.get("fail", 0) + 1

        await DATA.update(_inc)
        await query.edit_message_text(
            f"⚠️ *تم تسجيل ملاحظتك.*\nسنقوم بمراجعة هذا الرابط وإصلاحه قريباً."
            f"{CFG.signature}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.answer("⚠️ إجراء غير معروف.", show_alert=True)


async def auto_ban_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    user_id = job_data["user_id"]
    chat_id = job_data["chat_id"]
    msg_id = job_data["msg_id"]

    def _ban(data: dict[str, Any]) -> None:
        data["temp_banned"][str(user_id)] = time.time() + CFG.ban_duration_seconds

    await DATA.update(_ban)

    text = (
        "🚫 *تم حظر هذا الحساب لمدة 5 أيام تلقائياً.*\n"
        "السبب: عدم تأكيد التفعيل خلال المهلة المحددة (5 دقائق)."
        f"{CFG.signature}"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text, parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("Could not edit banned message: %s", e)
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            log.exception("Failed to notify user about auto-ban.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = await DATA.load()
    links = await LINKS.read()
    link_stats = data.get("link_stats", {})
    temp_banned = data.get("temp_banned", {})

    total_success = sum(s.get("success", 0) for s in link_stats.values())
    total_fail = sum(s.get("fail", 0) for s in link_stats.values())
    total_ops = total_success + total_fail
    success_rate = (total_success / total_ops * 100) if total_ops else 0.0

    header = (
        "📈 *إحصائيات البوت الشاملة*\n"
        "───────────────────\n\n"
        f"🔗 *إجمالي الروابط:* `{len(links)}`\n"
        f"✅ *التفعيلات الناجحة:* `{total_success}`\n"
        f"❌ *التفعيلات الفاشلة:* `{total_fail}`\n"
        f"🎯 *نسبة النجاح:* `{success_rate:.1f}%`\n"
        f"🚫 *المحظورون حالياً:* `{len(temp_banned)}`\n"
    )

    body_lines: list[str] = ["\n📌 *تفاصيل الروابط:*\n"]
    if links:
        for idx, link in enumerate(links, start=1):
            s = link_stats.get(link, {}).get("success", 0)
            f = link_stats.get(link, {}).get("fail", 0)
            body_lines.append(f"🔹 *رابط {idx}:* `{s}` نجاح | `{f}` فشل")
    else:
        body_lines.append("⚠️ لا توجد روابط مسجلة حالياً.")

    full = header + "\n".join(body_lines) + CFG.signature

    if len(full) <= 4000:
        await update.message.reply_text(full, parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)
    chunk: list[str] = []
    cur_len = 0
    for line in body_lines:
        if cur_len + len(line) + 1 > 3800 and chunk:
            await update.message.reply_text("\n".join(chunk), parse_mode=ParseMode.MARKDOWN)
            chunk, cur_len = [], 0
        chunk.append(line)
        cur_len += len(line) + 1
    if chunk:
        await update.message.reply_text(
            "\n".join(chunk) + CFG.signature, parse_mode=ParseMode.MARKDOWN
        )


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != CFG.admin_id:
        await update.message.reply_text("⛔️ هذا الأمر مخصص للأدمن فقط.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ اكتب آيدي المستخدم بعد الأمر، مثال:\n`/unban 12345678`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    target_id = context.args[0].strip()

    def _unban(data: dict[str, Any]) -> bool:
        return data["temp_banned"].pop(target_id, None) is not None

    was_banned = await DATA.update(_unban)
    if was_banned:
        await update.message.reply_text(
            f"✅ تم فك الحظر عن المستخدم `{target_id}`."
            f"{CFG.signature}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("⚠️ هذا المستخدم غير محظور حالياً.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != CFG.admin_id:
        await update.message.reply_text("⛔️ هذا الأمر مخصص للأدمن فقط.")
        return

    document = update.message.document
    if not document.file_name or not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text("⚠️ يرجى رفع ملف بصيغة `.txt` فقط.")
        return

    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم الملف يتجاوز 5MB.")
        return

    src_path = Path("temp_links.txt")
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(str(src_path))
        with src_path.open("r", encoding="utf-8") as f:
            new_links = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        log.exception("Failed to process uploaded links file.")
        await update.message.reply_text("⚠️ تعذّر قراءة الملف.")
        return
    finally:
        try:
            if src_path.exists():
                src_path.unlink()
        except OSError:
            pass

    if not new_links:
        await update.message.reply_text("⚠️ الملف المرفوع فارغ.")
        return

    await LINKS.write(new_links)
    await update.message.reply_text(
        f"✅ *تم تحديث قائمة الروابط بنجاح!*\nعدد الروابط المتاحة: *{len(new_links)}*"
        f"{CFG.signature}",
        parse_mode=ParseMode.MARKDOWN,
    )


# =====================================================================
# Main
# =====================================================================
async def post_init(app: Application) -> None:
    await PW.start()


async def post_shutdown(app: Application) -> None:
    await PW.stop()


def build_app() -> Application:
    app = Application.builder().token(CFG.token).post_init(post_init).post_shutdown(post_shutdown).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("stat", stats_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    return app


def main() -> None:
    log.info("🚀 Bot starting up... [By: LanGoos]")
    app = build_app()
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)
    except KeyboardInterrupt:
        log.info("Shutting down by user request.")


if __name__ == "__main__":
    main()
