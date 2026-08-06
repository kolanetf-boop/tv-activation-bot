"""
LanGoos TV Activation Bot
=========================
Telegram bot (python-telegram-bot v20+) + Playwright (Chromium) for
automatic TV activation through configurable links.

Author: LanGoos
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
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
    token: str
    admin_id: int

    links_file: Path = Path("links.txt")
    data_file: Path = Path("bot_data.json")
    screenshots_dir: Path = Path("screenshots")

    signature: str = "\n\n🌐 *By: LanGoos*"

    confirm_timeout_seconds: int = 180         # 3 دقائق مهلة الانتظار
    ban_duration_seconds: int = 5 * 24 * 3600  # حظر 5 أيام عند عدم التفاعل

    progress_min_interval: float = 1.5

    page_load_timeout_ms: int = 30_000
    input_selector_timeout_ms: int = 15_000
    post_submit_wait_ms: int = 6_000
    post_submit_settle_ms: int = 2_500

    @classmethod
    def from_env(cls) -> "Config":
        default_token = "8777412311:AAEW32qe5Tf_X-5jpEH5PIMz8DYrinHVQOg"
        default_admin_id = "6243526869"

        token = os.getenv("BOT_TOKEN", default_token).strip()
        admin_id_raw = os.getenv("ADMIN_ID", default_admin_id).strip()

        if not token:
            raise RuntimeError("❌ BOT_TOKEN is not configured.")
        try:
            admin_id = int(admin_id_raw)
        except ValueError as e:
            raise RuntimeError("❌ ADMIN_ID must be an integer.") from e
        return cls(token=token, admin_id=admin_id)


CFG = Config.from_env()
CFG.screenshots_dir.mkdir(parents=True, exist_ok=True)


# =====================================================================
# Data Store
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
# Helpers & UI Formatting
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
    is_done: bool,
    current_idx: int,
    total: int,
    elapsed_sec: int,
) -> str:
    percent = max(0, min(100, percent))
    filled = int(10 * percent // 100)
    bar = "█" * filled + "░" * (10 - filled)
    status_icon = "✅" if is_done else "⏳"

    return (
        "⏳ *جاري فحص دفعة التفعيل...*\n\n"
        f"`[{bar}]` *{percent}%*\n\n"
        f"📌 *الحالة:* {status_icon}\n"
        f"🔗 *الرابط الحالي:* {current_idx} من {total}\n"
        f"⏱ *الوقت المنقضي:* {elapsed_sec} ثانية\n"
        "───────────────────\n"
        "⚠️ *تعليمات مهمة:*\n"
        "1️⃣ عند نجاح التفعيل، يرجى التأكيد خلال *3 دقائق* فقط.\n"
        "2️⃣ *التجاهل سيؤدي لحظر حسابك تلقائياً لمدة 5 أيام!*"
    )


# =====================================================================
# Success / failure detection
# =====================================================================
SUCCESS_MARKERS_AR = (
    "تم التفعيل", "تم بنجاح", "تم تفعيل", "نجح", "ناجح",
    "تم الإضافة", "تم تسجيل", "تم القبول", "تم بنجاح ادخال",
    "شكراً", "تم تأكيد", "تم بنجاح إرسال", "تم اضافة",
    "تم اضافه", "تم الادخال", "تم إدخال", "كود صحيح", "تم بنجاح اضافه",
)
SUCCESS_MARKERS_EN = (
    "success", "successfully", "activated", "activation successful",
    "code accepted", "code valid", "device added", "device paired",
    "registered", "linked", "ok", "done", "completed", "welcome",
    "thank you", "congratulations", "approved", "accepted",
)

FAILURE_MARKERS_AR = (
    "كود خاطئ", "كود غير صحيح", "كود منتهي", "كود غير صالح", "منتهي الصلاحية",
    "خطأ", "غير صحيح", "غير صالح", "فشل", "غير موجود", "مستخدم",
    "تم استخدام", "تم استعمال", "غير مصرح", "غير مسموح", "مكرر",
)
FAILURE_MARKERS_EN = (
    "invalid", "incorrect", "expired", "used", "not found", "error",
    "wrong", "denied", "unauthorized", "duplicate", "already",
    "bad request", "fail", "failed", "try again",
)


def _normalize_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), p.params, p.query, ""))


def _text_blob(page_text: str) -> str:
    return re.sub(r"\s+", " ", (page_text or "").lower())


async def _get_visible_text(page: Page) -> str:
    try:
        return await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""


def _check_failure(text: str) -> Optional[str]:
    blob = _text_blob(text)
    for m in FAILURE_MARKERS_AR + FAILURE_MARKERS_EN:
        if m in blob:
            return m
    return None


def _check_success(text: str) -> Optional[str]:
    blob = _text_blob(text)
    for m in SUCCESS_MARKERS_AR + SUCCESS_MARKERS_EN:
        if m in blob:
            return m
    return None


@dataclass
class AttemptResult:
    success: bool
    reason: str
    screenshot_path: Optional[Path] = None
    final_url: str = ""
    http_status: Optional[int] = None


# =====================================================================
# Playwright Manager
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
# Activation Engine
# =====================================================================
async def try_activate_tv(
    url: str,
    code: str,
    update_progress_cb,
    start_time: float,
    acc_idx: int,
    total_accs: int,
    *,
    take_screenshot: bool = False,
) -> AttemptResult:
    if PW._browser is None:
        await PW.start()

    last_response: Optional[Response] = None
    response_status: Optional[int] = None
    initial_url: str = url

    async def _capture_response(response: Response) -> None:
        nonlocal last_response, response_status
        if response.request.resource_type in ("document", "xhr", "fetch"):
            last_response = response
            response_status = response.status

    screenshot_path: Optional[Path] = None
    final_url: str = url
    verdict_reason: str = "no-input"
    success: bool = False

    try:
        await update_progress_cb(
            20, False, start_time, acc_idx, total_accs
        )

        async with PW.new_page() as (_ctx, page):
            page.on("response", _capture_response)

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=CFG.page_load_timeout_ms,
                )
            except Exception as e:
                log.warning("goto failed for %s: %s", url, e)
                return AttemptResult(False, f"goto-failed:{e}", None, url, None)

            await page.wait_for_timeout(800)
            initial_url = page.url

            await update_progress_cb(
                50, False, start_time, acc_idx, total_accs
            )

            input_selectors = (
                'input[type="text"]',
                'input[name*="code" i]',
                'input[id*="code" i]',
                'input[type="search"]',
                'input[type="tel"]',
                'input[type="number"]',
                'input:not([type="hidden"])',
            )
            input_el = None
            for sel in input_selectors:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(
                        state="visible",
                        timeout=CFG.input_selector_timeout_ms // len(input_selectors),
                    )
                    if await el.is_visible():
                        input_el = el
                        break
                except Exception:
                    continue

            if input_el is None:
                return AttemptResult(
                    False, "no-input-field", None, page.url, response_status
                )

            try:
                await input_el.click()
                await input_el.fill("")
                await input_el.type(code, delay=80)
            except Exception as e:
                return AttemptResult(
                    False, f"type-failed:{e}", None, page.url, response_status
                )

            await page.wait_for_timeout(700)

            await update_progress_cb(
                80, False, start_time, acc_idx, total_accs
            )

            submitted = False
            submit_selectors = (
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("موافق")',
                'button:has-text("تفعيل")',
                'button:has-text("OK")',
                'button:has-text("Submit")',
                'button:has-text("إرسال")',
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
                    return AttemptResult(
                        False, "submit-failed", None, page.url, response_status
                    )

            try:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=CFG.post_submit_wait_ms
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(CFG.post_submit_settle_ms)
            except Exception:
                pass

            final_url = page.url

            visible_text = await _get_visible_text(page)
            html_content = ""
            try:
                html_content = (await page.content()).lower()
            except Exception:
                pass

            failure_match = _check_failure(visible_text)
            success_match = _check_success(visible_text)

            if failure_match:
                verdict_reason = f"failure-marker:{failure_match}"
                success = False
            elif success_match:
                verdict_reason = f"success-marker:{success_match}"
                success = True
            elif _normalize_url(final_url) != _normalize_url(initial_url):
                verdict_reason = "url-changed"
                success = True
            elif response_status is not None and 200 <= response_status < 300:
                if "error" in html_content or "خطأ" in html_content or "fail" in html_content:
                    verdict_reason = "http-2xx-but-html-has-error"
                    success = False
                else:
                    verdict_reason = f"http-{response_status}-no-error"
                    success = True
            else:
                verdict_reason = "no-evidence-of-success"
                success = False

            if take_screenshot:
                try:
                    fname = (
                        f"attempt_{int(time.time())}_{acc_idx}_"
                        f"{'ok' if success else 'fail'}.png"
                    )
                    spath = CFG.screenshots_dir / fname
                    await page.screenshot(path=str(spath), full_page=True)
                    screenshot_path = spath
                except Exception:
                    log.exception("screenshot failed")

            return AttemptResult(
                success=success,
                reason=verdict_reason,
                screenshot_path=screenshot_path,
                final_url=final_url,
                http_status=response_status,
            )

    except Exception as e:
        log.exception("Unexpected error in try_activate_tv for %s", url)
        return AttemptResult(False, f"exception:{e}", None, final_url, response_status)


# =====================================================================
# Throttled Progress Editor
# =====================================================================
class ProgressEditor:
    def __init__(self, message, start_time: float) -> None:
        self.message = message
        self.start_time = start_time
        self._last_edit_ts: float = 0.0
        self._pending: Optional[tuple[int, bool, int, int]] = None
        self._task: Optional[asyncio.Task] = None

    async def __call__(self, percent: int, is_done: bool, s_time: float, acc_idx: int, total: int) -> None:
        now = time.time()
        self._pending = (percent, is_done, acc_idx, total)
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
        percent, is_done, acc_idx, total = self._pending
        self._pending = None
        self._last_edit_ts = time.time()
        elapsed = int(time.time() - self.start_time)
        text = generate_progress_bar(percent, is_done, acc_idx, total, elapsed)
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
        generate_progress_bar(5, False, 1, total, 0),
        parse_mode=ParseMode.MARKDOWN,
    )

    progress = ProgressEditor(status_msg, start_time)
    typing_task = asyncio.create_task(_keep_typing(context, update.effective_chat.id))

    success = False
    used_link = ""

    try:
        for idx, link in enumerate(links, start=1):
            result = await try_activate_tv(
                link, code, progress, start_time, idx, total,
                take_screenshot=False,
            )
            if result.success:
                success = True
                used_link = link
                break
            log.info(
                "attempt %d/%d failed on %s: %s (status=%s)",
                idx, total, link, result.reason, result.http_status,
            )
            if idx < total:
                await progress(
                    int((idx / total) * 100),
                    False,
                    start_time,
                    idx + 1,
                    total,
                )
                await asyncio.sleep(0.6)
    except Exception:
        log.exception("Unhandled error in /login")
    finally:
        typing_task.cancel()

    elapsed = int(time.time() - start_time)

    # إنشاء أزرار التأكيد دائماً عند اكتمال الفحص (100%)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تم التفعيل",
                    callback_data=f"confirm_ok|{user_id}|{used_link}",
                ),
                InlineKeyboardButton(
                    "❌ لم يتم التفعيل",
                    callback_data=f"confirm_fail|{user_id}|{used_link}",
                ),
            ]
        ]
    )

    final_text = generate_progress_bar(100, True, total, total, elapsed) + CFG.signature

    try:
        await status_msg.edit_text(
            final_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    except BadRequest as e:
        log.warning("Could not attach keyboard to message: %s", e)
        await update.message.reply_text(
            final_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )

    # تشغيل مؤقت الحظر (3 دقائق) في حال لم يتفاعل المستخدم
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


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
    except Exception:
        return


# =====================================================================
# Button Callbacks
# =====================================================================
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

    # إزالة مؤقت الحظر التلقائي فور تفاعل المستخدم
    for job in context.job_queue.get_jobs_by_name(f"ban_{target_user_id}_{query.message.message_id}"):
        job.schedule_removal()

    if action == "confirm_ok":
        def _inc(data: dict[str, Any]) -> None:
            if used_link:
                stats = data["link_stats"].setdefault(used_link, {"success": 0, "fail": 0})
                stats["success"] = stats.get("success", 0) + 1

        await DATA.update(_inc)
        await query.edit_message_text(
            f"🎉 *رائع جداً! تم تسجيل نجاح التفعيل.*\n\n"
            f"📸 يرجى الآن **مشاركة صورة (Screenshot)** لشاشة التلفاز لتأكيد العملية بشكل نهائي."
            f"{CFG.signature}",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "confirm_fail":
        def _inc(data: dict[str, Any]) -> None:
            if used_link:
                stats = data["link_stats"].setdefault(used_link, {"success": 0, "fail": 0})
                stats["fail"] = stats.get("fail", 0) + 1

        await DATA.update(_inc)
        await query.edit_message_text(
            f"❌ *تم تسجيل عدم التفعيل.*\n\n"
            f"🔄 يرجى **إعادة إرسال الكود مرة ثانية** ليتسنى لنا فحص الروابط أو الدفعة التالية."
            f"{CFG.signature}",
            parse_mode=ParseMode.MARKDOWN,
        )


async def auto_ban_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    user_id = job_data["user_id"]
    chat_id = job_data["chat_id"]
    msg_id = job_data["msg_id"]

    def _ban(data: dict[str, Any]) -> None:
        data["temp_banned"][str(user_id)] = time.time() + CFG.ban_duration_seconds

    await DATA.update(_ban)

    text = (
        "🚫 *تم حظر هذا الحساب تلقائياً لمدة 5 أيام.*\n\n"
        "السبب: عدم التفاعل مع البوت واختيار النتيجة خلال المهلة المحددة (3 دقائق)."
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


# =====================================================================
# Additional Commands & Document Handler
# =====================================================================
async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != CFG.admin_id:
        await update.message.reply_text("⛔️ هذا الأمر مخصص للأدمن فقط.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ الاستخدام: `/debug <code>`\nمثال: `/debug 69246469`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code = context.args[0].strip()
    if not code.isdigit():
        await update.message.reply_text("⚠️ الكود يجب أن يكون أرقاماً فقط.")
        return

    links = await LINKS.read()
    if not links:
        await update.message.reply_text("⚠️ لا توجد روابط للاختبار.")
        return

    status = await update.message.reply_text(
        "🔍 جاري تنفيذ محاولة واحدة وتسجيل screenshot...",
        parse_mode=ParseMode.MARKDOWN,
    )

    progress = ProgressEditor(status, time.time())
    link = links[0]

    result = await try_activate_tv(
        link, code, progress, time.time(), 1, 1,
        take_screenshot=True,
    )

    report = (
        "🔬 *تقرير التشخيص*\n"
        "───────────────────\n"
        f"🔗 *الرابط:* `{link}`\n"
        f"🔑 *الكود:* `{code}`\n"
        f"📊 *HTTP Status:* `{result.http_status}`\n"
        f"🌐 *URL النهائي:* `{result.final_url}`\n"
        f"📝 *السبب:* `{result.reason}`\n"
        f"✅ *النتيجة:* {'نجاح' if result.success else 'فشل'}"
    )

    try:
        await status.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

    if result.screenshot_path and result.screenshot_path.exists():
        try:
            with result.screenshot_path.open("rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=result.screenshot_path.name,
                    caption=f"📸 Screenshot — {result.reason}",
                )
        except Exception:
            log.exception("Failed to send screenshot.")


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
    app = (
        Application.builder()
        .token(CFG.token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("stat", stats_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
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
