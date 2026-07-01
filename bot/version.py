from __future__ import annotations


APP_VERSION = "2.0.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "بازسازی موتور پایش با curl"
RELEASE_NOTES = (
    "جایگزینی کامل HTTPX چکر با curl تست‌شده روی سرور",
    "اجرای ناهمگام و امن curl بدون shell یا grep",
    "استخراج JSON کامل پیج و حفظ تأیید متنی بدون وابستگی به عکس",
    "نمایش نسخه فعال و اعلان خودکار پس از rebuild موفق",
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
