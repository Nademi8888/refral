"""پیکربندی ربات.

ترتیب اولویت خواندن مقادیر:
    1) مقادیری که در ویزارد نصب ذخیره شده‌اند (جدول settings در دیتابیس)
    2) متغیرهای محیطی Railway
    3) مقادیر پیش‌فرض

آدرس دیتابیس در فایل data/config.json نگهداری می‌شود، ولی اگر Railway
متغیرهای MySQL را در اختیار بگذارد به‌صورت خودکار تشخیص داده می‌شود.
"""

import json
import os
import secrets
import threading
from urllib.parse import quote_plus

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# کانال اجباری — قابل تغییر از پنل یا متغیرهای Railway نیست.
MANDATORY_CHANNEL = "@kodisavpn"

DEFAULTS = {
    "bot_token": "",
    "admin_id": 0,
    "channel_2": "@RaGNaR_New",
    "channel_3": "@RaGNaR_SoLd",
    "channel_4": "@RaGNaR_GaP1",
    "withdrawal_channel": "@RaGNaR_Sold",
    "referrals_required": 5,
    "gems_per_withdrawal": 110,
    "bot_username": "",
}

INT_KEYS = {"admin_id", "referrals_required", "gems_per_withdrawal"}

ENV_MAP = {
    "bot_token": "BOT_TOKEN",
    "admin_id": "ADMIN_ID",
    "channel_2": "CHANNEL_2",
    "channel_3": "CHANNEL_3",
    "channel_4": "CHANNEL_4",
    "withdrawal_channel": "WITHDRAWAL_CHANNEL",
    "referrals_required": "REFERRALS_REQUIRED",
    "gems_per_withdrawal": "GEMS_PER_WITHDRAWAL",
}


def detect_database_dsn() -> str:
    """آدرس دیتابیس را از متغیرهای Railway یا متغیر دستی پیدا می‌کند."""
    for key in ("DATABASE_URL", "MYSQL_URL", "MYSQL_PUBLIC_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value

    host = os.getenv("MYSQLHOST", "").strip()
    if host:
        user = os.getenv("MYSQLUSER", "root")
        password = os.getenv("MYSQLPASSWORD", "")
        port = os.getenv("MYSQLPORT", "3306")
        name = os.getenv("MYSQLDATABASE", "railway")
        return build_mysql_dsn(host, port, user, password, name)

    return ""


def build_mysql_dsn(host, port, user, password, name) -> str:
    return "mysql://{}:{}@{}:{}/{}".format(
        quote_plus(str(user)), quote_plus(str(password)), host, port or 3306, name
    )


class Settings:
    """مقادیر تنظیمات را در حافظه نگه می‌دارد و با دیتابیس همگام می‌کند."""

    def __init__(self):
        self._lock = threading.RLock()
        self._values = dict(DEFAULTS)
        self._local = {}
        self.load_local()
        self._apply_env()

    # ---------- فایل محلی ----------

    def load_local(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                self._local = json.load(handle)
        except (OSError, ValueError):
            self._local = {}
        return self._local

    def save_local(self, **changes):
        with self._lock:
            self._local.update(changes)
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._local, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        return self._local

    @property
    def database_dsn(self) -> str:
        dsn = detect_database_dsn() or self._local.get("database_dsn", "")
        if dsn:
            return dsn
        # خارج از ریلوی (اجرای محلی) برای تست روی SQLite بالا می‌آید.
        on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        if not on_railway or os.getenv("ALLOW_SQLITE") == "1":
            return "sqlite://local"
        return ""

    @property
    def panel_key(self) -> str:
        key = self._local.get("panel_key")
        if not key:
            key = secrets.token_urlsafe(18)
            self.save_local(panel_key=key)
        return key

    @property
    def installed(self) -> bool:
        return bool(self._values.get("bot_token")) and bool(self._values.get("admin_id"))

    # ---------- مقادیر ----------

    def _apply_env(self):
        for key, env_name in ENV_MAP.items():
            raw = os.getenv(env_name, "").strip()
            if raw:
                self._values[key] = self._coerce(key, raw)

    def _coerce(self, key, value):
        if key in INT_KEYS:
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return DEFAULTS[key]
        value = str(value).strip()
        if key.startswith("channel") or key == "withdrawal_channel":
            if value and not value.startswith("@") and not value.startswith("http"):
                value = "@" + value.lstrip("@")
        return value

    def apply(self, data: dict):
        """مقادیر ذخیره‌شده در دیتابیس یا فرم نصب را اعمال می‌کند."""
        with self._lock:
            for key, value in data.items():
                if key in DEFAULTS:
                    self._values[key] = self._coerce(key, value)
        return self._values

    def get(self, key, default=None):
        return self._values.get(key, default if default is not None else DEFAULTS.get(key))

    def as_dict(self):
        return dict(self._values)

    # ---------- میان‌برها ----------

    @property
    def bot_token(self):
        return self._values["bot_token"]

    @property
    def admin_id(self):
        return int(self._values["admin_id"] or 0)

    @property
    def channels(self):
        items = [MANDATORY_CHANNEL]
        for key in ("channel_2", "channel_3", "channel_4"):
            value = (self._values.get(key) or "").strip()
            if value and value not in items:
                items.append(value)
        return items

    @property
    def withdrawal_channel(self):
        return self._values.get("withdrawal_channel") or ""

    @property
    def referrals_required(self):
        return max(1, int(self._values.get("referrals_required") or 1))

    @property
    def gems_per_withdrawal(self):
        return int(self._values.get("gems_per_withdrawal") or 0)


settings = Settings()
