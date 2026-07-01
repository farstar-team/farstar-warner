from __future__ import annotations


APP_VERSION = "3.0.2"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "اصلاح قطعی نمایش پیج و گزارش زمانی"
RELEASE_NOTES = (
    "همسان‌سازی مسیر دریافت اطلاعات پیج با curl تست‌شده روی سرور",
    "حذف توقف کلی چکر هنگام پاسخ نامطمئن و جلوگیری از تغییر وضعیت اشتباه",
    "بررسی live برای فعال‌شدن پیج‌ها بدون اتکا به کش پنج‌دقیقه‌ای",
    "نمایش تاریخ و ساعت به‌صورت میلادی UTC و شمسی تهران",
    "اصلاح رندر فارسی/انگلیسی در تصویر گزارش و افزودن فونت‌های کامل‌تر",
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
