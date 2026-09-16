"""نقطه شروع برنامه.

یک وب‌سرور بالا می‌آورد که هم ویزارد نصب و پنل مدیریت را سرو می‌کند و هم
ربات تلگرام را داخل همان حلقه‌ی asyncio اجرا می‌کند. روی Railway مقدار PORT
به‌صورت خودکار ست می‌شود.
"""

import logging
import os

from aiohttp import web

from app.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(create_app(), host="0.0.0.0", port=port, print=None)
