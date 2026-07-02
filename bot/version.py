from __future__ import annotations


APP_VERSION = "3.0.4"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "مرکز لاگ فنی و اصلاح اعلان‌های مدیر"
RELEASE_NOTES = (
    "افزودن مرکز لاگ و عیب‌یابی کامل به پنل مدیر با شناسه رهگیری هر درخواست",
    "ثبت نتیجه HTTPX/HTTP2 و curl همراه با کد HTTP، زمان، حجم و علت فنی",
    "امکان دریافت فایل UTF-8 شامل ۵۰۰ رخداد اخیر و پاک‌سازی امن لاگ",
    "حذف ارسال خودکار خطاها و گزارش‌های بررسی پیج‌ها به مدیر سیستم",
    "حفظ اعلان تغییرات هر پیج فقط برای مالک همان پیج",
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
