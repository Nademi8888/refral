"""منطق ربات RaGNaR GEM."""

import asyncio
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from .settings import MANDATORY_CHANNEL, settings

logger = logging.getLogger("ragnar.bot")

pending_uid = set()
pending_support = set()

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ["💎 جم رایگان", "👤 حساب کاربری"],
    ["👥 زیرمجموعه‌گیری", "📖 راهنما"],
    ["👨‍💻 پشتیبانی"],
], resize_keyboard=True)


async def db_call(func, *args):
    """کوئری‌های دیتابیس در ترد جدا اجرا می‌شوند تا حلقه‌ی ربات قفل نشود."""
    return await asyncio.to_thread(func, *args)


def join_markup():
    rows = [[InlineKeyboardButton("📢 " + ch, url="https://t.me/" + ch.lstrip("@"))]
            for ch in settings.channels]
    rows.append([InlineKeyboardButton("✅ عضو شدم", callback_data="check")])
    return InlineKeyboardMarkup(rows)


async def is_member(context, user_id, channel) -> bool:
    try:
        member = await context.bot.get_chat_member(channel, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


async def joined_all(context, user_id) -> bool:
    for channel in settings.channels:
        if not await is_member(context, user_id, channel):
            return False
    return True


class BotRunner:
    """ربات را داخل همان حلقه‌ی asyncio وب‌سرور اجرا و در صورت نیاز ری‌استارت می‌کند."""

    def __init__(self, database):
        self.db = database
        self.app = None
        self.status = "stopped"
        self.error = ""
        self.username = ""
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.app is not None and self.status == "running"

    async def start(self):
        async with self._lock:
            if self.app is not None:
                return
            token = settings.bot_token
            if not token:
                self.status = "stopped"
                self.error = "توکن ربات تنظیم نشده است."
                return
            if settings.channels[0] != MANDATORY_CHANNEL:
                self.status = "error"
                self.error = "کانال اجباری تغییر کرده است."
                return
            try:
                app = Application.builder().token(token).build()
                self._register(app)
                app.bot_data["db"] = self.db
                await app.initialize()
                me = await app.bot.get_me()
                self.username = me.username or ""
                settings.apply({"bot_username": self.username})
                await asyncio.to_thread(self.db.save_settings, {"bot_username": self.username})
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                self.app = app
                self.status = "running"
                self.error = ""
                logger.info("ربات @%s راه‌اندازی شد.", self.username)
            except Exception as exc:  # نصب نباید به‌خاطر خطای توکن کرش کند
                self.status = "error"
                self.error = str(exc)
                logger.exception("راه‌اندازی ربات ناموفق بود")
                self.app = None

    async def stop(self):
        async with self._lock:
            app, self.app = self.app, None
            self.status = "stopped"
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            logger.exception("توقف ربات با خطا مواجه شد")

    async def restart(self):
        await self.stop()
        await self.start()

    def _register(self, app: Application):
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("broadcast", cmd_broadcast))
        app.add_handler(CommandHandler("stats", cmd_stats))
        app.add_handler(CallbackQueryHandler(on_check, pattern="^check$"))
        app.add_handler(CallbackQueryHandler(on_admin_action, pattern="^(paid|reply):"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


def db_of(context) -> "Database":  # noqa: F821
    return context.bot_data["db"]


# ---------------------------- هندلرها ----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = db_of(context)
    user = update.effective_user
    await db_call(db.upsert_user, user.id, user.username, user.first_name)

    if context.args:
        try:
            referrer = int(context.args[0])
        except (TypeError, ValueError):
            referrer = 0
        if referrer and referrer != user.id and await db_call(db.get_user, referrer):
            await db_call(db.set_referrer, user.id, referrer)

    if not await joined_all(context, user.id):
        await update.message.reply_text(
            "👋 به RaGNaR GEM خوش آمدید.\n\n"
            "⚠️ برای استفاده از ربات ابتدا در همه‌ی کانال‌های زیر عضو شوید "
            "و سپس «✅ عضو شدم» را بزنید.",
            reply_markup=join_markup())
        return

    await activate_referral(context, user.id)
    await update.message.reply_text("✅ عضویت تأیید شد.\n\n💎 منوی اصلی:", reply_markup=MAIN_KEYBOARD)


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await joined_all(context, query.from_user.id):
        await query.answer("❌ هنوز عضو همه‌ی کانال‌ها نشده‌اید.", show_alert=True)
        return
    await query.answer()
    await activate_referral(context, query.from_user.id)
    await query.edit_message_text("✅ عضویت شما تأیید شد.")
    await context.bot.send_message(query.from_user.id, "💎 منوی اصلی:", reply_markup=MAIN_KEYBOARD)


async def activate_referral(context, user_id):
    db = db_of(context)
    row = await db_call(db.get_user, user_id)
    if not row or not row.get("referrer_id") or row["referrer_id"] == user_id:
        return
    referrer = row["referrer_id"]
    if await db_call(db.add_referral, user_id, referrer):
        name = row.get("first_name") or row.get("username") or str(user_id)
        try:
            await context.bot.send_message(
                referrer,
                f"🎉 تبریک! کاربر «{name}» با لینک دعوت شما وارد ربات شد و رفرال شما ثبت شد.")
        except Exception:
            pass


async def show_referral(update, context):
    user = update.effective_user
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user.id}"
    await update.message.reply_text(
        f"👥 لینک اختصاصی شما:\n\n{link}\n\n"
        f"🎁 هر {settings.referrals_required} رفرال = {settings.gems_per_withdrawal} جم")


async def show_account(update, context):
    db = db_of(context)
    row = await db_call(db.get_user, update.effective_user.id)
    if not row:
        await update.message.reply_text("برای شروع /start را بزنید.")
        return
    await update.message.reply_text(
        f"👤 حساب کاربری\n\n📝 نام: {row['first_name']}\n🆔 آیدی: {row['user_id']}\n"
        f"👥 رفرال: {row['referrals']}\n💎 مجموع جم: {row['total_gems']}\n"
        f"📦 برداشت‌ها: {row['withdrawals']}")


async def show_guide(update, context):
    required = settings.referrals_required
    await update.message.reply_text(
        "📖 راهنمای دریافت جم رایگان\n\n"
        f"🎁 برای دریافت {settings.gems_per_withdrawal} جم، باید {required} زیرمجموعه فعال داشته باشید.\n\n"
        "👥 لینک خود را از بخش «زیرمجموعه‌گیری» بگیرید.\n\n"
        "✅ رفرال زمانی ثبت می‌شود که فرد با لینک شما وارد شود، عضو همه‌ی کانال‌ها شود "
        "و «عضو شدم» را بزند.\n\n"
        f"💎 پس از {required} رفرال، UID را ارسال کنید.\n"
        "🕖 واریز حداکثر تا ۳ ساعت پس از بررسی انجام می‌شود.")


async def show_gems(update, context):
    db = db_of(context)
    user_id = update.effective_user.id
    row = await db_call(db.get_user, user_id)
    if not row:
        await update.message.reply_text("برای شروع /start را بزنید.")
        return
    required = settings.referrals_required
    if row["referrals"] < required:
        me = await context.bot.get_me()
        await update.message.reply_text(
            "❌ هنوز شرایط دریافت جم را ندارید.\n\n"
            f"👥 رفرال شما: {row['referrals']} از {required}\n\n"
            f"🔗 https://t.me/{me.username}?start={user_id}")
        return
    pending_uid.add(user_id)
    await update.message.reply_text(
        "🎉 دکمه جم رایگان برای شما باز شد. 💫\n\n"
        f"شما {required} رفرال جمع کردید و صاحب {settings.gems_per_withdrawal} جم شدید.\n"
        "🆔 آیدی اکانت فری‌فایر خود را وارد کنید.")


async def show_support(update, context):
    pending_support.add(update.effective_user.id)
    await update.message.reply_text(
        "👨‍💻 پشتیبانی RaGNaR GEM\n\n"
        "📝 لطفاً پیام خود را بفرستید. پیام شما به ادمین ارسال می‌شود و پاسخ از همین ربات می‌آید.")


async def publish_withdrawal(context, game_uid):
    channel = settings.withdrawal_channel
    if not channel or channel.startswith("@YOUR_"):
        return
    text = ("🎉 #برداشت_جدید 💖\n\n"
            f"💎 تعداد جم: {settings.gems_per_withdrawal}\n\n"
            f"🎮 UID برای واریز جم:\n{game_uid}\n\n"
            f"📅 تاریخ برداشت:\n{datetime.now().strftime('%Y/%m/%d')}\n\n"
            "🤖 RaGNaR GEM")
    try:
        await context.bot.send_message(channel, text)
    except Exception:
        logger.exception("ارسال به کانال برداشت ناموفق بود")


MENU = {
    "💎 جم رایگان": show_gems,
    "👤 حساب کاربری": show_account,
    "👥 زیرمجموعه‌گیری": show_referral,
    "📖 راهنما": show_guide,
    "👨‍💻 پشتیبانی": show_support,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = db_of(context)
    user = update.effective_user
    text = (update.message.text or "").strip()

    # پاسخ ادمین به کاربر
    if user.id == settings.admin_id:
        target = context.user_data.pop("reply_target", None)
        if target:
            try:
                await context.bot.send_message(target, f"📩 پاسخ پشتیبانی:\n\n{text}")
                await update.message.reply_text("✅ پاسخ ارسال شد.")
            except Exception:
                await update.message.reply_text("❌ ارسال پاسخ ناموفق بود؛ کاربر ربات را بلاک کرده است.")
            return

    if user.id in pending_uid:
        if not text.isdigit():
            await update.message.reply_text("❌ UID باید فقط شامل عدد باشد.")
            return
        required = settings.referrals_required
        gems = settings.gems_per_withdrawal
        row = await db_call(db.get_user, user.id)
        if not row or row["referrals"] < required:
            pending_uid.discard(user.id)
            await update.message.reply_text("❌ شرایط دریافت جم دیگر کامل نیست.")
            return
        if not await db_call(db.consume_referrals, user.id, required, gems):
            await update.message.reply_text("❌ ثبت درخواست ناموفق بود.")
            return
        wid = await db_call(db.create_withdrawal, user.id, text, gems)
        pending_uid.discard(user.id)
        remaining = (await db_call(db.get_user, user.id))["referrals"]
        await update.message.reply_text(
            "✅ درخواست برداشت برای ادمین ارسال شد.\n"
            "بعد از بررسی جم شما واریز می‌شود.\n"
            f"{required} رفرال از شما کسر شد.\n"
            f"رفرال باقی‌مانده: {remaining}")
        await context.bot.send_message(
            settings.admin_id,
            f"📥 درخواست جدید جم\n\n👤 {user.first_name}\n🆔 {user.id}\n"
            f"🔹 @{user.username or 'ندارد'}\n🎮 UID: {text}\n"
            f"💎 {gems} جم\n🧾 درخواست #{wid}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ جم واریز شد", callback_data=f"paid:{wid}")]]))
        await publish_withdrawal(context, text)
        return

    if user.id in pending_support:
        pending_support.discard(user.id)
        await context.bot.send_message(
            settings.admin_id,
            f"📨 پیام پشتیبانی\n\n👤 {user.first_name}\n🆔 {user.id}\n"
            f"🔹 @{user.username or 'ندارد'}\n\n💬 {text}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ پاسخ به کاربر", callback_data=f"reply:{user.id}")]]))
        await update.message.reply_text("✅ پیام شما برای ادمین ارسال شد.")
        return

    handler = MENU.get(text)
    if handler:
        await handler(update, context)


async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = db_of(context)
    query = update.callback_query
    if query.from_user.id != settings.admin_id:
        await query.answer("دسترسی ندارید.", show_alert=True)
        return

    data = query.data
    if data.startswith("paid:"):
        wid = int(data.split(":")[1])
        row = await db_call(db.get_withdrawal, wid)
        if not row:
            await query.answer("درخواست پیدا نشد.", show_alert=True)
            return
        await db_call(db.mark_withdrawal_done, wid)
        await query.answer("ثبت شد.")
        await query.edit_message_reply_markup(reply_markup=None)
        try:
            await context.bot.send_message(
                row["user_id"],
                f"🎉 تبریک! {row['gems']} جم با موفقیت برای شما واریز شد. 💎❤️")
        except Exception:
            pass
    elif data.startswith("reply:"):
        await query.answer()
        context.user_data["reply_target"] = int(data.split(":")[1])
        await query.message.reply_text("✍️ حالا متن پاسخ را ارسال کنید.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != settings.admin_id:
        return
    if not context.args:
        await update.message.reply_text("استفاده: /broadcast متن پیام")
        return
    db = db_of(context)
    message = " ".join(context.args)
    user_ids = await db_call(db.get_all_user_ids)
    sent = 0
    for index, user_id in enumerate(user_ids):
        try:
            await context.bot.send_message(user_id, message)
            sent += 1
        except Exception:
            pass
        if index % 20 == 19:
            await asyncio.sleep(1)  # رعایت محدودیت ارسال تلگرام
    await update.message.reply_text(f"📢 پیام همگانی برای {sent} کاربر از {len(user_ids)} نفر ارسال شد.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != settings.admin_id:
        return
    stats = await db_call(db_of(context).get_stats)
    await update.message.reply_text(
        f"📊 آمار ربات\n\n👤 کاربران: {stats['users']}\n👥 رفرال‌های فعال: {stats['referrals']}\n"
        f"⏳ برداشت‌های در انتظار: {stats['pending']}\n✅ برداشت‌های پرداخت‌شده: {stats['paid']}\n"
        f"💎 مجموع جم پرداختی: {stats['gems']}")
