from __future__ import annotations


APP_VERSION = "1.4.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "تأیید پیج بدون وابستگی به تصویر"
RELEASE_NOTES = (
    "دریافت مستقیم مشخصات کامل پیج از Web Profile API",
    "ارسال اطلاعات متنی حتی در صورت شکست ساخت یا ارسال تصویر",
    "افزودن انتخاب فعال یا غیرفعال هنگام پاسخ نامشخص",
    "نمایش دنبال‌کننده، دنبال‌شونده، پست، بیوگرافی و نوع پیج",
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
