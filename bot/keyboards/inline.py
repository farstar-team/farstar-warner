from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import (
    DiscountCode,
    NotificationSettings,
    RequiredChannel,
    SubscriptionPlan,
    PageStatus,
    TargetPage,
    StoreProduct,
)


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
    allow_status_choice: bool = False,
) -> InlineKeyboardMarkup:
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
    if allow_status_choice:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="✅ پیج فعال است؛ ثبت شود",
                        callback_data="register:confirm:active",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏳ پیج فعلاً غیرفعال است",
                        callback_data="register:confirm:inactive",
                    )
                ],
            ]
        )
    else:
        confirm_text = (
            "⏳ ثبت به‌عنوان پیج غیرفعال" if inactive else "✅ بله، همین پیج ثبت شود"
        )
        status = "inactive" if inactive else "active"
        rows.append(
            [
                InlineKeyboardButton(
                    text=confirm_text,
                    callback_data=f"register:confirm:{status}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ این پیج نیست؛ لغو",
                callback_data="register:cancel",
            )
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


def required_channels_keyboard(
    channels: list[RequiredChannel],
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"عضویت در {channel.title} 📢", url=channel.join_url
            )
        ]
        for channel in channels
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ بررسی دوباره عضویت",
                callback_data="membership:check",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_plans_keyboard(plans: list[SubscriptionPlan]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        price = f"{plan.price:,}".replace(",", "٬")
        builder.button(
            text=f"◈ {plan.name} — {price} تومان",
            callback_data=f"buy:plan:{plan.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def purchase_methods_keyboard(
    plan_id: int,
    support_username: str | None,
    card_enabled: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if card_enabled:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💳 پرداخت کارت‌به‌کارت",
                    callback_data=f"buy:card:{plan_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🏷 استفاده از کد تخفیف",
                callback_data=f"buy:discount:{plan_id}",
            )
        ]
    )
    if support_username:
        username = support_username.lstrip("@")
        rows.append(
            [
                InlineKeyboardButton(
                    text="👤 ارتباط با پشتیبانی",
                    url=f"https://t.me/{username}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="↩️ بازگشت", callback_data="buy:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_keyboard(reminders_enabled: bool = True) -> InlineKeyboardMarkup:
    reminder_text = (
        "🔕 قطع یادآوری پایان اشتراک"
        if reminders_enabled
        else "🔔 فعال‌کردن یادآوری پایان اشتراک"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 تمدید اشتراک", callback_data="buy:list")],
            [
                InlineKeyboardButton(
                    text=reminder_text,
                    callback_data="reminder:toggle",
                )
            ],
        ]
    )


def expiry_reminder_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 تمدید اشتراک", callback_data="buy:list")],
            [
                InlineKeyboardButton(
                    text="🔕 دیگر اطلاع‌رسانی نکن",
                    callback_data="reminder:disable",
                )
            ],
        ]
    )


def store_products_keyboard(products: list[StoreProduct]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.button(
            text=f"🛍 {product.name}",
            callback_data=f"store:view:{product.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def store_product_keyboard(
    product_id: int,
    purchase_url: str | None,
    support_username: str | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if purchase_url:
        rows.append([InlineKeyboardButton(text="🛒 خرید محصول", url=purchase_url)])
    elif support_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👤 خرید از پشتیبانی",
                    url=f"https://t.me/{support_username.lstrip('@')}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="↩️ فروشگاه", callback_data="store:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_discounts_keyboard(codes: list[DiscountCode]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for code in codes:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {code.code} — {code.percent}٪",
                    callback_data=f"admin:discount:delete:{code.id}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ کد تخفیف جدید", callback_data="admin:discount:add")],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_store_keyboard(
    products: list[StoreProduct],
    enabled: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if enabled else '❌'} نمایش فروشگاه در منوی کاربران",
                callback_data="admin:store:toggle",
            )
        ]
    ]
    for product in products:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 حذف {product.name}",
                    callback_data=f"admin:store:delete:{product.id}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ افزودن محصول", callback_data="admin:store:add")],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def receipt_review_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید فیش و فعال‌سازی",
                    callback_data=f"receipt:approve:{receipt_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ رد فیش",
                    callback_data=f"receipt:reject:{receipt_id}",
                )
            ],
        ]
    )


def admin_channels_keyboard(
    channels: list[RequiredChannel],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.button(
            text=f"🗑 حذف {channel.title}",
            callback_data=f"admin:channel:delete:{channel.id}",
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="➕ افزودن کانال اجباری",
            callback_data="admin:channel:add",
        )
    )
    builder.row(InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home"))
    return builder.as_markup()


def admin_plans_keyboard(plans: list[SubscriptionPlan]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✏️ {plan.name}",
                    callback_data=f"admin:planedit:{plan.id}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"admin:plandelete:{plan.id}",
                ),
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="➕ افزودن پلن",
                    callback_data="admin:planadd",
                )
            ],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_config_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ تغییر آیدی پشتیبانی",
                    callback_data="admin:payment:support",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 تغییر مشخصات کارت",
                    callback_data="admin:payment:card",
                )
            ],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
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
            [
                InlineKeyboardButton(
                    text="📢 کانال‌های عضویت اجباری",
                    callback_data="admin:channels",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 مدیریت پلن‌های اشتراک",
                    callback_data="admin:plans",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 تنظیمات پرداخت",
                    callback_data="admin:payment",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧾 فیش‌های در انتظار",
                    callback_data="admin:receipts",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏷 مدیریت کدهای تخفیف",
                    callback_data="admin:discounts",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛍 مدیریت فروشگاه",
                    callback_data="admin:store",
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
