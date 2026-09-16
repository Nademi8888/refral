"""لایه دیتابیس.

روی Railway اگر سرویس MySQL به پروژه وصل باشد، آدرس اتصال خودکار خوانده می‌شود.
اگر هیچ دیتابیسی تنظیم نشده باشد، ربات روی SQLite بالا می‌آید تا بدون تنظیمات
هم قابل تست باشد (روی Railway دیسک موقت است و با هر دیپلوی پاک می‌شود).
"""

import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

import pymysql
from pymysql.cursors import DictCursor

from .settings import DATA_DIR

SQLITE_PATH = os.path.join(DATA_DIR, "ragnar.db")


class DatabaseError(RuntimeError):
    pass


def parse_dsn(dsn: str) -> dict:
    """آدرس اتصال را به اجزای آن تبدیل می‌کند."""
    dsn = (dsn or "").strip()
    if not dsn or dsn.startswith("sqlite"):
        return {"backend": "sqlite", "path": SQLITE_PATH}

    parsed = urlparse(dsn)
    if parsed.scheme not in ("mysql", "mysql+pymysql", "mariadb"):
        raise DatabaseError("آدرس دیتابیس باید با mysql:// شروع شود.")
    if not parsed.hostname:
        raise DatabaseError("هاست دیتابیس در آدرس پیدا نشد.")

    return {
        "backend": "mysql",
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or "root"),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "/").lstrip("/") or "railway",
    }


def describe_dsn(dsn: str) -> str:
    """توضیح خوانا برای نمایش در پنل، بدون افشای رمز."""
    try:
        info = parse_dsn(dsn)
    except DatabaseError:
        return "نامعتبر"
    if info["backend"] == "sqlite":
        return "SQLite (موقت)"
    return "MySQL · {}:{}/{}".format(info["host"], info["port"], info["database"])


def test_connection(dsn: str) -> str:
    """اتصال را تست می‌کند و نسخه سرور را برمی‌گرداند."""
    info = parse_dsn(dsn)
    if info["backend"] == "sqlite":
        os.makedirs(DATA_DIR, exist_ok=True)
        con = sqlite3.connect(info["path"])
        con.close()
        return "SQLite {}".format(sqlite3.sqlite_version)

    con = pymysql.connect(
        host=info["host"], port=info["port"], user=info["user"],
        password=info["password"], database=info["database"],
        charset="utf8mb4", cursorclass=DictCursor, connect_timeout=8,
    )
    try:
        with con.cursor() as cur:
            cur.execute("SELECT VERSION() AS v")
            return str(cur.fetchone()["v"])
    finally:
        con.close()


class Database:
    """اتصال‌ها را در یک استخر کوچک نگه می‌دارد و کوئری‌ها را اجرا می‌کند."""

    def __init__(self, dsn: str, pool_size: int = 5):
        self.info = parse_dsn(dsn)
        self.backend = self.info["backend"]
        self.dsn = dsn
        self._pool = queue.LifoQueue(maxsize=pool_size)
        self._lock = threading.Lock()
        self._sqlite_lock = threading.RLock()
        self._sqlite_con = None
        if self.backend == "sqlite":
            os.makedirs(DATA_DIR, exist_ok=True)
            self._sqlite_con = sqlite3.connect(self.info["path"], check_same_thread=False)
            self._sqlite_con.row_factory = sqlite3.Row
            self._sqlite_con.execute("PRAGMA journal_mode=WAL")

    # ---------- اتصال ----------

    def _new_mysql(self):
        return pymysql.connect(
            host=self.info["host"], port=self.info["port"], user=self.info["user"],
            password=self.info["password"], database=self.info["database"],
            charset="utf8mb4", cursorclass=DictCursor, autocommit=False,
            connect_timeout=8, read_timeout=30, write_timeout=30,
        )

    @contextmanager
    def connection(self):
        if self.backend == "sqlite":
            with self._sqlite_lock:
                yield self._sqlite_con
            return

        try:
            con = self._pool.get_nowait()
        except queue.Empty:
            con = self._new_mysql()

        try:
            con.ping(reconnect=True)
        except Exception:
            try:
                con.close()
            except Exception:
                pass
            con = self._new_mysql()

        try:
            yield con
        finally:
            try:
                self._pool.put_nowait(con)
            except queue.Full:
                try:
                    con.close()
                except Exception:
                    pass

    def _sql(self, sql: str) -> str:
        return sql.replace("%s", "?") if self.backend == "sqlite" else sql

    def run(self, sql, params=(), fetch=None):
        with self.connection() as con:
            cur = con.cursor()
            try:
                cur.execute(self._sql(sql), params)
                if fetch == "one":
                    row = cur.fetchone()
                    return dict(row) if row else None
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                con.commit()
                return cur.lastrowid
            finally:
                cur.close()

    def close(self):
        if self.backend == "sqlite":
            if self._sqlite_con:
                self._sqlite_con.close()
            return
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                return
            except Exception:
                continue

    # ---------- ساخت جدول‌ها ----------

    def init_schema(self):
        mysql = self.backend == "mysql"
        pk = "BIGINT PRIMARY KEY AUTO_INCREMENT" if mysql else "INTEGER PRIMARY KEY AUTOINCREMENT"
        suffix = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if mysql else ""

        statements = [
            """CREATE TABLE IF NOT EXISTS settings(
                name VARCHAR(64) PRIMARY KEY,
                value TEXT
            )""" + suffix,
            """CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                referrer_id BIGINT NULL,
                referrals INT NOT NULL DEFAULT 0,
                total_gems INT NOT NULL DEFAULT 0,
                withdrawals INT NOT NULL DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""" + suffix,
            """CREATE TABLE IF NOT EXISTS referrals(
                invited_user_id BIGINT PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""" + suffix,
            """CREATE TABLE IF NOT EXISTS withdrawals(
                id """ + pk + """,
                user_id BIGINT NOT NULL,
                uid VARCHAR(100) NOT NULL,
                gems INT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""" + suffix,
        ]
        indexes = [
            ("idx_users_referrer", "users(referrer_id)"),
            ("idx_referrals_referrer", "referrals(referrer_id)"),
            ("idx_withdrawals_user", "withdrawals(user_id)"),
            ("idx_withdrawals_status", "withdrawals(status)"),
        ]

        with self.connection() as con:
            cur = con.cursor()
            for statement in statements:
                cur.execute(statement)
            for name, target in indexes:
                try:
                    cur.execute("CREATE INDEX {} ON {}".format(name, target)
                                if self.backend == "mysql" else
                                "CREATE INDEX IF NOT EXISTS {} ON {}".format(name, target))
                except Exception:
                    pass  # ایندکس از قبل وجود دارد
            cur.close()
            con.commit()

    # ---------- تنظیمات ----------

    def load_settings(self) -> dict:
        rows = self.run("SELECT name, value FROM settings", fetch="all")
        return {row["name"]: row["value"] for row in rows}

    def save_settings(self, data: dict):
        for name, value in data.items():
            if self.backend == "mysql":
                sql = ("INSERT INTO settings(name, value) VALUES(%s, %s) "
                       "ON DUPLICATE KEY UPDATE value=VALUES(value)")
            else:
                sql = ("INSERT INTO settings(name, value) VALUES(%s, %s) "
                       "ON CONFLICT(name) DO UPDATE SET value=excluded.value")
            self.run(sql, (name, str(value)))

    # ---------- کاربران ----------

    def upsert_user(self, user_id, username, first_name):
        if self.backend == "mysql":
            sql = ("INSERT INTO users(user_id, username, first_name) VALUES(%s, %s, %s) "
                   "ON DUPLICATE KEY UPDATE username=VALUES(username), first_name=VALUES(first_name)")
        else:
            sql = ("INSERT INTO users(user_id, username, first_name) VALUES(%s, %s, %s) "
                   "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
                   "first_name=excluded.first_name")
        self.run(sql, (user_id, username or "", first_name or ""))

    def get_user(self, user_id):
        return self.run("SELECT * FROM users WHERE user_id=%s", (user_id,), fetch="one")

    def get_all_user_ids(self):
        rows = self.run("SELECT user_id FROM users", fetch="all")
        return [row["user_id"] for row in rows]

    def set_referrer(self, user_id, referrer_id):
        self.run("UPDATE users SET referrer_id=%s WHERE user_id=%s AND referrer_id IS NULL",
                 (referrer_id, user_id))

    def add_referral(self, invited_user_id, referrer_id) -> bool:
        with self.connection() as con:
            cur = con.cursor()
            try:
                cur.execute(self._sql("INSERT INTO referrals(invited_user_id, referrer_id) VALUES(%s, %s)"),
                            (invited_user_id, referrer_id))
                cur.execute(self._sql("UPDATE users SET referrals=referrals+1 WHERE user_id=%s"),
                            (referrer_id,))
                con.commit()
                return True
            except (pymysql.err.IntegrityError, sqlite3.IntegrityError):
                con.rollback()
                return False
            finally:
                cur.close()

    def consume_referrals(self, user_id, amount, gems) -> bool:
        with self.connection() as con:
            cur = con.cursor()
            try:
                if self.backend == "mysql":
                    cur.execute("SELECT referrals FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
                else:
                    cur.execute("SELECT referrals FROM users WHERE user_id=?", (user_id,))
                row = cur.fetchone()
                current = (dict(row)["referrals"] if row else 0)
                if current < amount:
                    con.rollback()
                    return False
                cur.execute(self._sql(
                    "UPDATE users SET referrals=referrals-%s, total_gems=total_gems+%s, "
                    "withdrawals=withdrawals+1 WHERE user_id=%s"), (amount, gems, user_id))
                con.commit()
                return True
            finally:
                cur.close()

    # ---------- برداشت‌ها ----------

    def create_withdrawal(self, user_id, uid, gems):
        return self.run("INSERT INTO withdrawals(user_id, uid, gems) VALUES(%s, %s, %s)",
                        (user_id, uid, gems))

    def get_withdrawal(self, wid):
        return self.run("SELECT * FROM withdrawals WHERE id=%s", (wid,), fetch="one")

    def mark_withdrawal_done(self, wid):
        self.run("UPDATE withdrawals SET status='paid' WHERE id=%s", (wid,))

    def get_stats(self):
        one = lambda sql: (self.run(sql, fetch="one") or {"n": 0})["n"]
        return {
            "users": one("SELECT COUNT(*) AS n FROM users"),
            "referrals": one("SELECT COALESCE(SUM(referrals), 0) AS n FROM users"),
            "pending": one("SELECT COUNT(*) AS n FROM withdrawals WHERE status='pending'"),
            "paid": one("SELECT COUNT(*) AS n FROM withdrawals WHERE status='paid'"),
            "gems": one("SELECT COALESCE(SUM(total_gems), 0) AS n FROM users"),
        }
