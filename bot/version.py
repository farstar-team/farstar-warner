from __future__ import annotations


APP_VERSION = "1.3.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "موتور Web Profile اینستاگرام"
RELEASE_NOTES = (
    "انتقال چکر وضعیت به Web Profile API بدون ورود",
    "استخراج شناسه یکتا و نام کاربری از JSON معتبر",
    "تشخیص تغییر نام کاربری بر اساس تطبیق شناسه پیج",
    "حفظ cooldown، تأیید متوالی دی‌اکتیوشدن و اعلان‌های فارسی",
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
