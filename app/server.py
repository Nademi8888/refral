"""وب‌سرور نصب‌کننده و پنل مدیریت."""

import asyncio
import logging
import os
import platform
import sys

import aiohttp
from aiohttp import web

from .bot import BotRunner
from .database import Database, describe_dsn, test_connection
from .settings import DEFAULTS, MANDATORY_CHANNEL, build_mysql_dsn, detect_database_dsn, settings

logger = logging.getLogger("ragnar.web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "2.0.0"
INSTALL_KEY = os.getenv("INSTALL_KEY", "").strip()


class AppState:
    def __init__(self):
        self.db: Database | None = None
        self.runner: BotRunner | None = None

    def attach(self, database: Database):
        self.db = database
        self.runner = BotRunner(database)


state = AppState()


def page(name: str) -> web.Response:
    with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as handle:
        return web.Response(text=handle.read(), content_type="text/html")


def ok(**kwargs):
    return web.json_response({"ok": True, **kwargs})


def fail(message: str, status: int = 200):
    return web.json_response({"ok": False, "error": message}, status=status)


async def check_token(token: str) -> str:
    """توکن را با getMe تست می‌کند و نام کاربری ربات را برمی‌گرداند."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                data = await response.json(content_type=None)
    except Exception:
        raise ValueError("ارتباط با سرور تلگرام برقرار نشد. اتصال اینترنت سرور را بررسی کنید.")

    if not isinstance(data, dict):
        raise ValueError("پاسخ نامعتبر از تلگرام دریافت شد.")
    if not data.get("ok"):
        raise ValueError("توکن ربات معتبر نیست. توکن را دوباره از @BotFather بگیرید.")
    return data["result"].get("username", "")


async def telegram_reachable() -> bool:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.telegram.org/bot0:0/getMe") as response:
                return response.status in (200, 401, 404)
    except Exception:
        return False


# ---------------------------- صفحه‌ها ----------------------------

def install_allowed(request) -> bool:
    """اگر INSTALL_KEY تنظیم شده باشد، صفحه نصب فقط با همان کلید باز می‌شود."""
    return not INSTALL_KEY or request.query.get("key") == INSTALL_KEY or \
        request.get("install_key", "") == INSTALL_KEY


async def home(request):
    if settings.installed and state.db is not None:
        return page("panel.html") if request.query.get("key") else web.Response(
            text="RaGNaR GEM نصب شده است. برای ورود به پنل، لینک همراه کلید را باز کنید.",
            content_type="text/plain")
    if not install_allowed(request):
        return web.Response(text="برای باز کردن صفحه نصب، کلید INSTALL_KEY لازم است.",
                            content_type="text/plain", status=403)
    return page("installer.html")


async def panel(request):
    return page("panel.html")


async def health(request):
    return web.json_response({
        "status": "ok",
        "installed": settings.installed,
        "bot": state.runner.status if state.runner else "stopped",
    })


# ---------------------------- API نصب ----------------------------

async def api_environment(request):
    dsn = settings.database_dsn
    database_ok = False
    detail = "تنظیم نشده"
    if dsn:
        try:
            await asyncio.to_thread(test_connection, dsn)
            database_ok = True
            detail = describe_dsn(dsn)
        except Exception as exc:
            detail = str(exc)[:120]

    python_ok = sys.version_info >= (3, 10)
    checks = [
        {"title": "نسخه پایتون", "ok": python_ok,
         "detail": platform.python_version()},
        {"title": "کتابخانه‌های ربات", "ok": True, "detail": "نصب شده"},
        {"title": "دسترسی به تلگرام", "ok": await telegram_reachable() or "warn",
         "detail": "api.telegram.org"},
        {"title": "دیتابیس", "ok": database_ok, "detail": detail},
        {"title": "پوشه داده", "ok": True, "detail": "قابل نوشتن"},
    ]

    values = {key: settings.get(key) for key in DEFAULTS if key != "bot_username"}
    values["bot_token"] = settings.get("bot_token")
    if not values.get("admin_id"):
        values["admin_id"] = ""

    return web.json_response({
        "checks": checks,
        "database": {"ok": database_ok, "detail": detail, "auto": bool(detect_database_dsn())},
        "values": values,
        "mandatory_channel": MANDATORY_CHANNEL,
        "version": VERSION,
    })


async def api_test_db(request):
    body = await request.json()
    host = (body.get("host") or "").strip()
    if not host:
        return fail("هاست دیتابیس را وارد کنید.")
    dsn = build_mysql_dsn(host, body.get("port") or 3306, body.get("user") or "root",
                          body.get("password") or "", body.get("name") or "railway")
    try:
        version = await asyncio.to_thread(test_connection, dsn)
    except Exception as exc:
        return fail("اتصال برقرار نشد: " + str(exc)[:160])
    return ok(dsn=dsn, version=version)


async def api_install(request):
    if settings.installed and state.db is not None:
        return fail("ربات قبلاً نصب شده است. برای تغییر تنظیمات از پنل استفاده کنید.")

    body = await request.json()
    if INSTALL_KEY and body.get("install_key") != INSTALL_KEY:
        return fail("کلید نصب نامعتبر است.", status=403)
    token = (body.get("bot_token") or "").strip()
    admin_id = (body.get("admin_id") or "").strip()

    if not token:
        return fail("توکن ربات را وارد کنید.")
    if not admin_id.isdigit():
        return fail("آیدی عددی ادمین باید فقط عدد باشد.")

    dsn = detect_database_dsn() or (body.get("database_dsn") or "").strip() or settings.database_dsn
    if not dsn:
        return fail("دیتابیس تنظیم نشده است؛ ابتدا اتصال را تست کنید.")

    try:
        username = await check_token(token)
    except Exception as exc:
        return fail(str(exc)[:200])

    try:
        database = await asyncio.to_thread(Database, dsn)
        await asyncio.to_thread(database.init_schema)
    except Exception as exc:
        return fail("ساخت جدول‌ها ناموفق بود: " + str(exc)[:160])

    values = {
        "bot_token": token,
        "admin_id": admin_id,
        "channel_2": body.get("channel_2", DEFAULTS["channel_2"]),
        "channel_3": body.get("channel_3", DEFAULTS["channel_3"]),
        "channel_4": body.get("channel_4", DEFAULTS["channel_4"]),
        "withdrawal_channel": body.get("withdrawal_channel", DEFAULTS["withdrawal_channel"]),
        "referrals_required": body.get("referrals_required") or DEFAULTS["referrals_required"],
        "gems_per_withdrawal": body.get("gems_per_withdrawal") or DEFAULTS["gems_per_withdrawal"],
        "bot_username": username,
    }
    settings.apply(values)
    await asyncio.to_thread(database.save_settings, values)
    settings.save_local(database_dsn=dsn, installed=True)

    if state.db is not None:
        await state.runner.stop()
    state.attach(database)
    await state.runner.start()

    if state.runner.status != "running":
        return fail("ربات اجرا نشد: " + (state.runner.error or "خطای نامشخص"))

    base = str(request.url.origin())
    return ok(bot_username=username, database=describe_dsn(dsn), channels=settings.channels,
              panel_url=f"{base}/panel?key={settings.panel_key}")


# ---------------------------- API پنل ----------------------------

def authorized(request) -> bool:
    key = request.query.get("key") or request.get("json_key", "")
    return bool(key) and key == settings.panel_key


async def api_status(request):
    if not authorized(request):
        return fail("کلید دسترسی نامعتبر است.", status=403)
    stats = await asyncio.to_thread(state.db.get_stats) if state.db else {}
    values = settings.as_dict()
    return ok(
        bot={"status": state.runner.status if state.runner else "stopped",
             "username": settings.get("bot_username"),
             "error": state.runner.error if state.runner else ""},
        database=describe_dsn(settings.database_dsn),
        mandatory_channel=MANDATORY_CHANNEL,
        version=VERSION,
        stats=stats,
        settings=values,
    )


async def api_settings(request):
    body = await request.json()
    if body.get("key") != settings.panel_key:
        return fail("کلید دسترسی نامعتبر است.", status=403)

    old_token = settings.bot_token
    values = {key: body[key] for key in DEFAULTS if key in body}
    if "admin_id" in values and not str(values["admin_id"]).strip().isdigit():
        return fail("آیدی ادمین باید عدد باشد.")

    settings.apply(values)
    await asyncio.to_thread(state.db.save_settings, settings.as_dict())

    if settings.bot_token != old_token:
        await state.runner.restart()
        if state.runner.status != "running":
            return fail("توکن جدید کار نکرد: " + state.runner.error)
    return ok()


async def api_restart(request):
    body = await request.json()
    if body.get("key") != settings.panel_key:
        return fail("کلید دسترسی نامعتبر است.", status=403)
    await state.runner.restart()
    if state.runner.status != "running":
        return fail(state.runner.error or "ربات اجرا نشد.")
    return ok()


# ---------------------------- راه‌اندازی ----------------------------

async def bootstrap(app):
    """اگر تنظیمات از قبل موجود باشد، بدون ویزارد ربات را بالا می‌آورد."""
    dsn = settings.database_dsn
    if not dsn:
        logger.warning("دیتابیسی تنظیم نشده است؛ صفحه نصب را باز کنید.")
        return
    try:
        database = await asyncio.to_thread(Database, dsn)
        await asyncio.to_thread(database.init_schema)
    except Exception:
        logger.exception("اتصال به دیتابیس ناموفق بود")
        return

    stored = await asyncio.to_thread(database.load_settings)
    if stored:
        settings.apply(stored)
    elif settings.bot_token:
        # مقادیر از متغیرهای Railway آمده‌اند؛ همان‌ها را ذخیره کن.
        await asyncio.to_thread(database.save_settings, settings.as_dict())

    state.attach(database)
    if settings.installed:
        settings.save_local(database_dsn=dsn, installed=True)
        await state.runner.start()
        logger.info("پنل مدیریت: /panel?key=%s", settings.panel_key)


async def cleanup(app):
    if state.runner:
        await state.runner.stop()
    if state.db:
        await asyncio.to_thread(state.db.close)


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", home),
        web.get("/panel", panel),
        web.get("/health", health),
        web.get("/api/environment", api_environment),
        web.post("/api/test-db", api_test_db),
        web.post("/api/install", api_install),
        web.get("/api/status", api_status),
        web.post("/api/settings", api_settings),
        web.post("/api/restart", api_restart),
    ])
    app.on_startup.append(bootstrap)
    app.on_cleanup.append(cleanup)
    return app
