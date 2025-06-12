import os
import asyncio
from pyrogram import Client, filters
import requests
from plugins.startbot import start_background_tasks  # وارد کردن تابع از پلاگین

# تنظیمات Tor
TOR_PROXY = {
    'http': 'socks5h://127.0.0.1:9150',
    'https': 'socks5h://127.0.0.1:9150'
}

# تست اتصال به Tor
try:
    response = requests.get('https://api.ipify.org', proxies=TOR_PROXY, timeout=10)
    print(f"Tor روی پورت 9150 کار می‌کند. IP: {response.text}")
except requests.exceptions.RequestException as e:
    print(f"خطا در اتصال به Tor روی پورت 9150: {str(e)}")
    exit(1)

# تنظیمات پروکسی برای Pyrogram
proxy_settings = {
    "scheme": "socks5",
    "hostname": "127.0.0.1",
    "port": 9150
}

Plugins = dict(root="plugins")

class CustomClient(Client):
    async def start(self):
        await super().start()  # اجرای متد start اصلی
        print("ربات آماده شد! فراخوانی start_background_tasks...")  # دیباگ
        try:
            await start_background_tasks(self)  # فراخوانی تسک‌های پس‌زمینه
        except Exception as e:
            print(f"خطا در فراخوانی start_background_tasks: {str(e)}")

app = CustomClient(
    name="eghtesad",
    plugins=Plugins,
    bot_token=os.getenv("BOT_TOKEN", "7520024265:AAHCxgqBxQbOB4F_eLAAM622waB_HuNInvQ"),
    proxy=proxy_settings
)

# اضافه کردن هندلر برای /start (اگر پلاگین این رو مدیریت نکنه)
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text("سلام")

async def main():
    """تابع اصلی اجرای ربات"""
    try:
        await app.start()
        print("ربات با موفقیت شروع به کار کرد (با استفاده از Tor)")
        await asyncio.Event().wait()  # نگه داشتن ربات فعال
    except Exception as e:
        print(f"خطای اصلی: {e}")
    finally:
        try:
            await app.stop()
        except Exception as stop_error:
            print(f"خطا در توقف ربات: {stop_error}")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nربات متوقف شد")
    finally:
        loop.close()

# import os
# import asyncio
# from pyrogram import Client
# from plugins.startbot import start_background_tasks  # وارد کردن تابع از پلاگین
#
# Plugins = dict(root="plugins")
#
# class CustomClient(Client):
#     async def start(self):
#         await super().start()  # اجرای متد start اصلی
#         print("ربات آماده شد! فراخوانی start_background_tasks...")  # دیباگ
#         try:
#             await start_background_tasks(self)  # فراخوانی تسک‌های پس‌زمینه
#         except Exception as e:
#             print(f"خطا در فراخوانی start_background_tasks: {str(e)}")
#
# app = CustomClient(
#     name="eghtesad",
#     plugins=Plugins,
#     bot_token=os.getenv("BOT_TOKEN", "7520024265:AAHCxgqBxQbOB4F_eLAAM622waB_HuNInvQ")
# )
#
# app.run()
#
