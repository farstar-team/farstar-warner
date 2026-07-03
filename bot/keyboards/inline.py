from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.money import format_money
from bot.models import (
    DiscountCode,
    InstagramMonitoringAccount,
    NotificationSettings,
    RequiredChannel,
    SubscriptionPlan,
    PageStatus,
    TargetPage,
    StoreProduct,
    User,
)
from bot.reporting import ADMIN_REPORT_OPTIONS


def _enabled_icon(enabled: bool) -> str:
    return "✅" if enabled else "❌"


def _persian_number(value: int) -> str:
    return f"{value:,}".translate(str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬"))


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
            [
                InlineKeyboardButton(
                    text=f"{_enabled_icon(settings.notify_verification_change)} اعلان دریافت/حذف تیک آبی",
                    callback_data=f"toggle:{page_id}:verification",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{_enabled_icon(settings.notify_follower_change)} گزارش تغییر فالوور",
                    callback_data=f"toggle:{page_id}:follower",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "✅ حالت آستانه‌ای"
                        if settings.follower_report_mode == "threshold"
                        else "حالت آستانه‌ای"
                    ),
                    callback_data=f"follower:mode:{page_id}:threshold",
                ),
                InlineKeyboardButton(
                    text=(
                        "✅ گزارش ساعتی"
                        if settings.follower_report_mode == "hourly"
                        else "گزارش ساعتی"
                    ),
                    callback_data=f"follower:mode:{page_id}:hourly",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🔢 آستانه فعلی: "
                        f"{_persian_number(settings.follower_change_threshold)}"
                    ),
                    callback_data=f"follower:threshold:{page_id}",
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
        builder.button(
            text=f"◈ {plan.name} — {format_money(plan.price, plan.price_currency)}",
            callback_data=f"buy:plan:{plan.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def purchase_methods_keyboard(
    plan_id: int,
    support_username: str | None,
    card_enabled: bool,
    zarinpal_enabled: bool = False,
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
    if zarinpal_enabled:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🌐 پرداخت آنلاین با زرین‌پال",
                    callback_data=f"buy:zarinpal:{plan_id}",
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
            text=(
                f"🛍 {product.name} — "
                f"{format_money(product.price, product.price_currency)}"
            ),
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
            [
                InlineKeyboardButton(
                    text="➕ کد تخفیف جدید", callback_data="admin:discount:add"
                )
            ],
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
                    text=(
                        f"🗑 {product.name} — "
                        f"{format_money(product.price, product.price_currency)}"
                    ),
                    callback_data=f"admin:store:delete:{product.id}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="➕ افزودن محصول", callback_data="admin:store:add"
                )
            ],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def currency_selection_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇮🇷 تومان",
                    callback_data=f"{callback_prefix}:TOMAN",
                ),
                InlineKeyboardButton(
                    text="🇺🇸 دلار",
                    callback_data=f"{callback_prefix}:USD",
                ),
            ],
            [InlineKeyboardButton(text="لغو ↩️", callback_data="admin:cancel")],
        ]
    )


def admin_report_copy_keyboard(
    user_id: int,
    selected: set[str],
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{_enabled_icon(key in selected)} {label}",
                callback_data=f"admin:reportcopy:{user_id}:{key}",
            )
        ]
        for key, label in ADMIN_REPORT_OPTIONS
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="❌ غیرفعال‌کردن همه رونوشت‌ها",
                    callback_data=f"admin:reportcopy:{user_id}:NONE",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 اطلاعات کاربر",
                    callback_data=f"admin:user:view:{user_id}",
                )
            ],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_users_keyboard(
    users: list[User],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        username = f"@{user.username}" if user.username else "بدون نام کاربری"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {username} — {_persian_number(user.telegram_id)}",
                    callback_data=f"admin:user:view:{user.telegram_id}",
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="صفحه قبل ⬅️",
                callback_data=f"admin:users:{page - 1}",
            )
        )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(
                text="➡️ صفحه بعد",
                callback_data=f"admin:users:{page + 1}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="🔎 جست‌وجو با شناسه عددی",
                    callback_data="admin:user:search",
                )
            ],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_detail_keyboard(user_id: int, *, banned: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 پیج‌های ثبت‌شده",
                    callback_data=f"admin:user:pages:{user_id}:0",
                ),
                InlineKeyboardButton(
                    text="🧾 خریدها و فیش‌ها",
                    callback_data=f"admin:user:payments:{user_id}:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📨 انتخاب رونوشت گزارش‌ها",
                    callback_data=f"admin:user:reports:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 کنترل اشتراک کاربر",
                    callback_data=f"admin:user:subview:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ رفع مسدودی" if banned else "⛔ مسدودکردن کاربر",
                    callback_data=f"admin:user:status:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ فهرست کاربران",
                    callback_data="admin:users:0",
                )
            ],
        ]
    )


def admin_user_subscription_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ تغییر پلن و افزودن روز",
                    callback_data=f"admin:user:sub:renew:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ ۳۰ روز به اشتراک فعلی",
                    callback_data=f"admin:user:sub:extend30:{user_id}",
                ),
                InlineKeyboardButton(
                    text="➕ ۹۰ روز به اشتراک فعلی",
                    callback_data=f"admin:user:sub:extend90:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏹ پایان‌دادن اشتراک ویژه",
                    callback_data=f"admin:user:sub:expire:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ اطلاعات کاربر",
                    callback_data=f"admin:user:view:{user_id}",
                )
            ],
        ]
    )


def admin_user_section_keyboard(
    user_id: int, page: int, *, has_next: bool, section: str
) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="صفحه قبل ⬅️",
                callback_data=f"admin:user:{section}:{user_id}:{page - 1}",
            )
        )
    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text="➡️ صفحه بعد",
                callback_data=f"admin:user:{section}:{user_id}:{page + 1}",
            )
        )
    rows = [navigation] if navigation else []
    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ اطلاعات کاربر",
                callback_data=f"admin:user:view:{user_id}",
            )
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


def payment_config_keyboard(zarinpal_enabled: bool = False) -> InlineKeyboardMarkup:
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
            [
                InlineKeyboardButton(
                    text="🔑 ثبت شناسه پذیرنده زرین‌پال",
                    callback_data="admin:payment:zarinpal:merchant",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌐 ثبت آدرس بازگشت زرین‌پال",
                    callback_data="admin:payment:zarinpal:callback",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🔴 غیرفعال‌کردن زرین‌پال"
                        if zarinpal_enabled
                        else "🟢 فعال‌کردن زرین‌پال"
                    ),
                    callback_data="admin:payment:zarinpal:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💱 تنظیم نرخ پشتیبان دلار",
                    callback_data="admin:payment:currency:fallback",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 دریافت و آزمایش نرخ زنده",
                    callback_data="admin:payment:currency:refresh",
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
                    text="◈ افزودن پیج هدف مدیر",
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
                    text="🧰 لاگ کامل و عیب‌یابی",
                    callback_data="admin:diagnostics",
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
            [
                InlineKeyboardButton(
                    text="📨 رونوشت گزارش کاربران",
                    callback_data="admin:report_copy",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 مدیریت کاربران",
                    callback_data="admin:users:0",
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


def admin_monitoring_accounts_keyboard(
    accounts: list[InstagramMonitoringAccount],
    *,
    encryption_available: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for account in accounts:
        status = "✅" if account.is_active else "⏸"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {account.label}",
                    callback_data=f"admin:monitoring:test:{account.id}",
                ),
                InlineKeyboardButton(
                    text="خاموش/روشن",
                    callback_data=f"admin:monitoring:toggle:{account.id}",
                ),
                InlineKeyboardButton(
                    text="حذف 🗑",
                    callback_data=f"admin:monitoring:delete:{account.id}",
                ),
            ]
        )
    if encryption_available:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ افزودن اتصال رسمی Meta",
                    callback_data="admin:monitoring:add",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_monitoring_delete_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="بله، اتصال حذف شود ✅",
                    callback_data=f"admin:monitoring:delete_confirm:{account_id}",
                ),
                InlineKeyboardButton(
                    text="انصراف ↩️",
                    callback_data="admin:monitoring_accounts",
                ),
            ]
        ]
    )


def admin_diagnostics_keyboard(*, confirm_clear: bool = False) -> InlineKeyboardMarkup:
    if confirm_clear:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="بله، لاگ پاک شود ✅",
                        callback_data="admin:diagnostics:clear_confirm",
                    ),
                    InlineKeyboardButton(
                        text="انصراف ↩️",
                        callback_data="admin:diagnostics",
                    ),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 تازه‌سازی",
                    callback_data="admin:diagnostics",
                ),
                InlineKeyboardButton(
                    text="🧪 تست اتصال",
                    callback_data="admin:instagram_health",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 دریافت فایل کامل لاگ",
                    callback_data="admin:diagnostics:download",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 پاک‌کردن لاگ",
                    callback_data="admin:diagnostics:clear",
                )
            ],
            [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")],
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
