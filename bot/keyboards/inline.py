from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import NotificationSettings, PageStatus, TargetPage


def _enabled_icon(enabled: bool) -> str:
    return "✅" if enabled else "❌"


def _page_status_icon(status: PageStatus | None) -> str:
    if status == PageStatus.ACTIVE:
        return "🟢"
    if status == PageStatus.DEACTIVATED:
        return "🔴"
    return "⚪"


def pages_keyboard(pages: list[TargetPage]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for page in pages:
        builder.button(
            text=f"{_page_status_icon(page.last_known_status)} @{page.instagram_username}",
            callback_data=f"page:view:{page.id}",
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ افزودن پیج جدید", callback_data="page:add")
    )
    return builder.as_markup()


def page_details_keyboard(page_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 مشاهده اطلاعات زنده پیج",
                    callback_data=f"profile:details:{page_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ تنظیم اعلان‌های این پیج",
                    callback_data=f"settings:view:{page_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◈ مرکز امنیت و شواهد پیج",
                    callback_data=f"security:view:{page_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 حذف پیج", callback_data=f"page:delete:{page_id}"
                ),
                InlineKeyboardButton(text="↩️ بازگشت", callback_data="page:list"),
            ],
        ]
    )


def security_tools_keyboard(page_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◈ بررسی فوری وضعیت", callback_data=f"sec:check:{page_id}"
                ),
                InlineKeyboardButton(
                    text="◈ امتیاز هشدار", callback_data=f"sec:score:{page_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◈ ممیزی نمای عمومی", callback_data=f"sec:audit:{page_id}"
                ),
                InlineKeyboardButton(
                    text="◈ اثرانگشت هویت", callback_data=f"sec:fingerprint:{page_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◈ ذخیره خط مبنا", callback_data=f"sec:baseline:{page_id}"
                ),
                InlineKeyboardButton(
                    text="◈ تاریخچه رخدادها", callback_data=f"sec:history:{page_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◈ گزارش حادثه", callback_data=f"sec:report:{page_id}"
                ),
                InlineKeyboardButton(
                    text="◈ تست اعلان", callback_data=f"sec:testalert:{page_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◈ سلامت پایش", callback_data=f"sec:health:{page_id}"
                ),
                InlineKeyboardButton(
                    text="◈ تصویر شواهد", callback_data=f"profile:details:{page_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="↩️ بازگشت به پیج", callback_data=f"page:view:{page_id}"
                )
            ],
        ]
    )


def confirm_delete_keyboard(page_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="بله، حذف شود ✅",
                    callback_data=f"page:confirm_delete:{page_id}",
                ),
                InlineKeyboardButton(
                    text="خیر ↩️", callback_data=f"page:view:{page_id}"
                ),
            ]
        ]
    )


def registration_confirmation_keyboard(
    *,
    inactive: bool = False,
    profile_url: str | None = None,
) -> InlineKeyboardMarkup:
    confirm_text = (
        "⏳ ثبت به‌عنوان پیج غیرفعال" if inactive else "✅ بله، همین پیج ثبت شود"
    )
    status = "inactive" if inactive else "active"
    rows: list[list[InlineKeyboardButton]] = []
    if profile_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="◈ بازکردن پیج در اینستاگرام",
                    url=profile_url,
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=confirm_text,
                    callback_data=f"register:confirm:{status}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ خیر، لغو ثبت",
                    callback_data="register:cancel",
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_pages_keyboard(pages: list[TargetPage]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for page in pages:
        builder.button(
            text=f"⚙️ @{page.instagram_username}",
            callback_data=f"settings:view:{page.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def notification_settings_keyboard(
    page_id: int,
    settings: NotificationSettings,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{_enabled_icon(settings.notify_activation)} اعلان فعال‌شدن",
                    callback_data=f"toggle:{page_id}:activation",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{_enabled_icon(settings.notify_deactivation)} اعلان دی‌اکتیوشدن",
                    callback_data=f"toggle:{page_id}:deactivation",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{_enabled_icon(settings.notify_username_change)} اعلان تغییر نام کاربری",
                    callback_data=f"toggle:{page_id}:username",
                )
            ],
            [InlineKeyboardButton(text="↩️ بازگشت به پیج‌ها", callback_data="page:list")],
        ]
    )


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="درخواست پریمیوم 💎",
                    callback_data="subscription:request:Premium",
                )
            ],
            [
                InlineKeyboardButton(
                    text="درخواست ویژه 👑",
                    callback_data="subscription:request:VIP",
                )
            ],
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◈ افزودن پیج پایش ادمین",
                    callback_data="admin:add_target",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◈ وضعیت اتصال اینستاگرام",
                    callback_data="admin:instagram_health",
                )
            ],
            [InlineKeyboardButton(text="آمار سیستم 📈", callback_data="admin:stats")],
            [
                InlineKeyboardButton(
                    text="تمدید اشتراک کاربر 👥", callback_data="admin:renew"
                )
            ],
            [
                InlineKeyboardButton(
                    text="تنظیم زمان‌بندی چکر ⏱️", callback_data="admin:schedule"
                )
            ],
            [
                InlineKeyboardButton(
                    text="بررسی فوری همه پیج‌ها 🔄", callback_data="admin:check_now"
                )
            ],
        ]
    )


def admin_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="رایگان", callback_data="admin:plan:Free")],
            [InlineKeyboardButton(text="پریمیوم", callback_data="admin:plan:Premium")],
            [InlineKeyboardButton(text="ویژه", callback_data="admin:plan:VIP")],
            [InlineKeyboardButton(text="لغو ↩️", callback_data="admin:cancel")],
        ]
    )
