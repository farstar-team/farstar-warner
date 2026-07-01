from __future__ import annotations


APP_VERSION = "3.0.1"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "پایداری نمایش پیج و امکانات فروش"
RELEASE_NOTES = (
    "رفع محدودیت‌های متناوب نمایش پیج با کش سالم و کنترل نرخ سراسری",
    "افزودن تمدید تجمیعی و یادآوری روزانه سه روز پایانی",
    "افزودن کد تخفیف با کنترل مصرف و تأیید فیش",
    "افزودن فروشگاه قابل مدیریت و خاموش یا روشن‌شدن از پنل مدیر",
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
