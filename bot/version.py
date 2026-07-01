from __future__ import annotations


APP_VERSION = "3.0.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "اشتراک، پرداخت و امنیت حرفه‌ای"
RELEASE_NOTES = (
    "رفع فونت فارسی کارت و پایش دائمی سهمیه رایگان",
    "افزودن عضویت اجباری چند کانال از پنل مدیر",
    "افزودن پلن‌های مدت‌دار، کارت‌به‌کارت و تأیید دستی فیش",
    "افزودن اعلان تغییر عکس پروفایل و گزارش‌های تکمیلی",
)
RELEASE_REDIS_KEY = "farstar:release:notified-version"


def version_message(*, activated: bool = False) -> str:
    heading = "نسخه جدید ربات فعال شد! ✅" if activated else "نسخه فعال ربات ℹ️"
    notes = "\n".join(f"• {note}" for note in RELEASE_NOTES)
    return (
        f"{heading}\n\n"
        f"نسخه: <code>{APP_VERSION}</code>\n"
        f"عنوان انتشار: <b>{RELEASE_TITLE}</b>\n"
        f"تاریخ انتشار: {RELEASE_DATE}\n\n"
        f"تغییرات این نسخه:\n{notes}"
    )
