from __future__ import annotations


APP_VERSION = "4.3.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "ماشین وضعیت قطعی و کنترل اشتراک کاربران"
RELEASE_NOTES = (
    "افزودن ماشین وضعیت قطعی برای مقایسه نتیجه فعلی با آخرین وضعیت معتبر",
    "ثبت آخرین تلاش، پاسخ قطعی، HTTP، زمان تغییر و تأییدهای متوالی هر پیج",
    "به‌روزرسانی زنده وضعیت هنگام ورود به جزئیات پیج با تست مرجع @instagram",
    "ارسال دقیق اعلان در انتقال فعال به غیرفعال و غیرفعال به فعال",
    "افزودن تغییر پلن، تمدید ۳۰/۹۰ روزه و پایان اشتراک در مدیریت کاربر",
    "نمایش وضعیت اولیه غیرقطعی تا اولین پاسخ معتبر موتور پایش",
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
