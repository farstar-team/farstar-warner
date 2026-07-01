from __future__ import annotations

import html
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.checker import CheckOutcome, InstagramChecker, ProfileResult
from bot.config import Settings
from bot.database import SessionFactory
from bot.keyboards.inline import (
    confirm_delete_keyboard,
    notification_settings_keyboard,
    page_details_keyboard,
    pages_keyboard,
    purchase_methods_keyboard,
    purchase_plans_keyboard,
    receipt_review_keyboard,
    registration_confirmation_keyboard,
    required_channels_keyboard,
    security_tools_keyboard,
    settings_pages_keyboard,
)
from bot.keyboards.reply import cancel_keyboard, main_menu_keyboard
from bot.models import (
    NotificationSettings,
    PaymentConfig,
    PaymentReceipt,
    PageEvent,
    PageSnapshot,
    PageStatus,
    PlanTier,
    ReceiptStatus,
    RequiredChannel,
    SubscriptionPlan,
    TargetPage,
    User,
    UserSubscription,
    UserStatus,
)
from bot.profile_preview import EmbedProfile, PreviewOutcome, ProfilePreviewService
from bot.version import APP_VERSION, version_message


router = Router(name="user")
logger = logging.getLogger(__name__)

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9_])?$")
PLAN_NAMES = {
    PlanTier.FREE: "رایگان",
    PlanTier.PREMIUM: "پریمیوم",
    PlanTier.VIP: "ویژه",
}
PAGE_STATUS_NAMES = {
    PageStatus.ACTIVE: "فعال 🟢",
    PageStatus.DEACTIVATED: "غیرفعال / منتظر فعال‌شدن 🔴",
    None: "در انتظار اولین بررسی ⚪",
}


class AddPageState(StatesGroup):
    waiting_for_username = State()
    waiting_for_confirmation = State()


class PurchaseState(StatesGroup):
    waiting_for_receipt = State()


class UserAccessMiddleware(BaseMiddleware):
    def __init__(self, session_factory: SessionFactory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user = data.get("event_from_user")
        if telegram_user is None:
            return await handler(event, data)

        async with self.session_factory() as session:
            user = await session.get(User, telegram_user.id)
            is_admin = telegram_user.id == self.settings.admin_telegram_id
            if user is None:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    subscription_expiry=datetime.now(timezone.utc)
                    + timedelta(
                        days=3650 if is_admin else self.settings.free_trial_days
                    ),
                    plan_tier=PlanTier.VIP if is_admin else PlanTier.FREE,
                )
                session.add(user)
            elif user.username != telegram_user.username:
                user.username = telegram_user.username
            if is_admin:
                minimum_admin_expiry = datetime.now(timezone.utc) + timedelta(days=3650)
                user.status = UserStatus.ACTIVE
                user.plan_tier = PlanTier.VIP
                if user.subscription_expiry < minimum_admin_expiry:
                    user.subscription_expiry = minimum_admin_expiry
            await session.commit()

        if user.status == UserStatus.BANNED:
            text = (
                "دسترسی شما به ربات مسدود شده است. برای پیگیری با پشتیبانی تماس بگیرید."
            )
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
            return None

        if not is_admin:
            async with self.session_factory() as session:
                channels = list(
                    await session.scalars(
                        select(RequiredChannel)
                        .where(RequiredChannel.is_active.is_(True))
                        .order_by(RequiredChannel.id)
                    )
                )
            bot: Bot | None = data.get("bot")
            redis: Redis | None = data.get("redis")
            missing_channels: list[RequiredChannel] = []
            if bot is not None:
                for channel in channels:
                    membership_key = (
                        f"farstar:membership:{telegram_user.id}:{channel.id}"
                    )
                    if redis is not None and await redis.exists(membership_key):
                        continue
                    chat_id: int | str = channel.chat_identifier
                    if channel.chat_identifier.lstrip("-").isdigit():
                        chat_id = int(channel.chat_identifier)
                    try:
                        membership = await bot.get_chat_member(
                            chat_id=chat_id,
                            user_id=telegram_user.id,
                        )
                        joined = membership.status in {
                            ChatMemberStatus.CREATOR,
                            ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.MEMBER,
                        } or (
                            membership.status == ChatMemberStatus.RESTRICTED
                            and bool(getattr(membership, "is_member", False))
                        )
                    except TelegramAPIError as exc:
                        logger.warning(
                            "Could not verify membership in %s: %s",
                            channel.chat_identifier,
                            exc,
                        )
                        joined = False
                    if not joined:
                        missing_channels.append(channel)
                    elif redis is not None:
                        await redis.set(membership_key, "1", ex=300)
            elif channels:
                missing_channels = channels

            if missing_channels:
                text = (
                    "برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید، "
                    "سپس دکمه بررسی دوباره را بزنید."
                )
                markup = required_channels_keyboard(missing_channels)
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        "ابتدا عضویت کانال‌ها را تکمیل کنید.", show_alert=True
                    )
                    if event.message:
                        await event.message.answer(text, reply_markup=markup)
                elif isinstance(event, Message):
                    await event.answer(text, reply_markup=markup)
                return None

        data["db_user"] = user
        data["is_primary_admin"] = is_admin
        return await handler(event, data)


def register_middlewares(session_factory: SessionFactory, settings: Settings) -> None:
    middleware = UserAccessMiddleware(session_factory, settings)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)


def to_persian_digits(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def format_count(value: int | None) -> str:
    if value is None:
        return "نامشخص"
    return f"{value:,}".replace(",", "٬").translate(PERSIAN_DIGITS)


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "ثبت نشده"
    utc_value = value.astimezone(timezone.utc)
    return to_persian_digits(utc_value.strftime("%Y/%m/%d - %H:%M")) + " به وقت جهانی"


def profile_result_to_embed(
    result: ProfileResult,
    requested_username: str,
) -> EmbedProfile:
    return EmbedProfile(
        outcome=PreviewOutcome.ACTIVE,
        username=result.canonical_username or requested_username,
        full_name=result.full_name,
        biography=result.biography,
        profile_picture_url=result.profile_picture_url,
        follower_count=result.follower_count,
        following_count=result.following_count,
        post_count=result.post_count,
        is_private=result.is_private,
        is_verified=result.is_verified,
        diagnostic="web_profile_api",
    )


def normalize_instagram_username(raw_value: str) -> str | None:
    value = raw_value.strip()
    if value.startswith("@"):
        value = value[1:]
    elif value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = parsed.netloc.lower().split(":", 1)[0]
        if host not in {"instagram.com", "www.instagram.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            return None
        value = parts[0]

    if not USERNAME_RE.fullmatch(value) or ".." in value:
        return None
    return value.lower()


async def _load_pages(
    session_factory: SessionFactory, user_id: int
) -> list[TargetPage]:
    async with session_factory() as session:
        result = await session.scalars(
            select(TargetPage)
            .where(TargetPage.user_id == user_id)
            .order_by(TargetPage.created_at, TargetPage.id)
        )
        return list(result)


async def _owned_page(
    session_factory: SessionFactory,
    user_id: int,
    page_id: int,
) -> TargetPage | None:
    async with session_factory() as session:
        return await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == user_id,
            )
        )


def _profile_fingerprint(details: EmbedProfile) -> tuple[str, dict[str, object]]:
    identity: dict[str, object] = {
        "username": details.username.lower(),
        "full_name": details.full_name,
        "biography": details.biography,
        "profile_picture_url": details.profile_picture_url,
        "is_private": details.is_private,
        "is_verified": details.is_verified,
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper(), identity


def _risk_score(
    page: TargetPage,
    details: EmbedProfile,
    *,
    baseline_exists: bool,
    baseline_changed: bool,
    interval_seconds: int,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if page.last_known_status == PageStatus.DEACTIVATED:
        score += 70
        reasons.append("وضعیت ذخیره‌شده پیج غیرفعال است")
    if details.outcome != PreviewOutcome.ACTIVE:
        score += 20
        reasons.append("اطلاعات زنده عمومی در دسترس نیست")
    else:
        if not details.is_verified:
            score += 5
            reasons.append("پیج نشان تأیید عمومی ندارد")
        if not details.biography:
            score += 5
            reasons.append("بیوگرافی در نمای عمومی دیده نشد")
        if details.is_private is False:
            score += 5
            reasons.append("پیج عمومی است و سطح افشای بیشتری دارد")
    if not baseline_exists:
        score += 10
        reasons.append("خط مبنای هویت ثبت نشده است")
    elif baseline_changed:
        score += 45
        reasons.append("اثرانگشت هویت با خط مبنا تفاوت دارد")

    if page.last_checked_at is None:
        score += 10
        reasons.append("هنوز بررسی زمان‌بندی‌شده ثبت نشده است")
    else:
        checked_at = page.last_checked_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - checked_at).total_seconds() > max(
            interval_seconds * 3, 300
        ):
            score += 15
            reasons.append("آخرین بررسی از بازه مورد انتظار قدیمی‌تر است")
    return min(score, 100), reasons or ["نشانه هشدار قابل‌توجهی دیده نشد"]


def _add_guard_message(
    user: User,
    target_count: int,
    subscription: UserSubscription | None = None,
) -> str | None:
    now = datetime.now(timezone.utc)
    if subscription is not None and subscription.expires_at > now:
        target_limit = subscription.target_limit
    elif user.plan_tier != PlanTier.FREE and user.subscription_expiry > now:
        target_limit = user.plan_tier.target_limit
    else:
        target_limit = PlanTier.FREE.target_limit
    if target_count >= target_limit:
        limit = to_persian_digits(target_limit)
        return f"ظرفیت پلن شما تکمیل است. سقف پلن فعلی {limit} پیج است."
    return None


@router.message(CommandStart())
async def start(message: Message, db_user: User, settings: Settings) -> None:
    await message.answer(
        "سلام! به فارستار وارنر خوش آمدید. 🌟\n\n"
        "از اینجا می‌توانید وضعیت پیج‌های عمومی اینستاگرام را پایش کنید و هنگام تغییر وضعیت اعلان بگیرید.\n\n"
        f"نسخه فعال: <code>{APP_VERSION}</code>",
        reply_markup=main_menu_keyboard(
            is_admin=db_user.telegram_id == settings.admin_telegram_id
        ),
    )


@router.message(Command("version"))
async def version_command(message: Message) -> None:
    await message.answer(version_message())


@router.callback_query(F.data == "membership:check")
async def membership_recheck(
    callback: CallbackQuery,
    db_user: User,
    settings: Settings,
) -> None:
    await callback.answer("عضویت شما تأیید شد. ✅", show_alert=True)
    if callback.message:
        await callback.message.answer(
            "عضویت کانال‌ها تأیید شد؛ اکنون می‌توانید از ربات استفاده کنید.",
            reply_markup=main_menu_keyboard(
                is_admin=db_user.telegram_id == settings.admin_telegram_id
            ),
        )


@router.message(Command("help"))
async def help_command(message: Message, db_user: User, settings: Settings) -> None:
    await message.answer(
        "راهنمای فارستار وارنر 📘\n\n"
        "برای افزودن یا حذف پیج از «مدیریت پیج‌ها» استفاده کنید. "
        "در بخش «تنظیمات اعلان‌ها» می‌توانید اعلان هر پیج را جداگانه تغییر دهید. "
        "نام کاربری را به‌صورت @username یا لینک کامل اینستاگرام بفرستید.",
        reply_markup=main_menu_keyboard(
            is_admin=db_user.telegram_id == settings.admin_telegram_id
        ),
    )


@router.message(StateFilter("*"), F.text == "لغو عملیات ↩️")
async def cancel_operation(
    message: Message,
    state: FSMContext,
    db_user: User,
    settings: Settings,
) -> None:
    await state.clear()
    await message.answer(
        "عملیات لغو شد.",
        reply_markup=main_menu_keyboard(
            is_admin=db_user.telegram_id == settings.admin_telegram_id
        ),
    )


@router.message(F.text == "مدیریت پیج‌ها 📊")
async def manage_pages(
    message: Message, db_user: User, session_factory: SessionFactory
) -> None:
    pages = await _load_pages(session_factory, db_user.telegram_id)
    text = (
        "پیج‌های زیر در حال پایش هستند. برای مشاهده جزئیات، یک پیج را انتخاب کنید."
        if pages
        else "هنوز پیجی برای پایش ثبت نکرده‌اید."
    )
    await message.answer(text, reply_markup=pages_keyboard(pages))


@router.callback_query(F.data == "page:list")
async def list_pages_callback(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    pages = await _load_pages(session_factory, db_user.telegram_id)
    text = (
        "پیج‌های زیر در حال پایش هستند. برای مشاهده جزئیات، یک پیج را انتخاب کنید."
        if pages
        else "هنوز پیجی برای پایش ثبت نکرده‌اید."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=pages_keyboard(pages))
    await callback.answer()


@router.callback_query(F.data == "page:add")
async def begin_add_page(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        target_count = await session.scalar(
            select(func.count(TargetPage.id)).where(
                TargetPage.user_id == db_user.telegram_id
            )
        )
        subscription = await session.get(UserSubscription, db_user.telegram_id)
    guard_message = _add_guard_message(
        db_user,
        int(target_count or 0),
        subscription,
    )
    if guard_message:
        await callback.answer(guard_message, show_alert=True)
        return

    await state.set_state(AddPageState.waiting_for_username)
    if callback.message:
        await callback.message.answer(
            "نام کاربری پیج عمومی اینستاگرام را ارسال کنید.\nنمونه: <b>@instagram</b>",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AddPageState.waiting_for_username, F.text)
async def add_page(
    message: Message,
    state: FSMContext,
    db_user: User,
    session_factory: SessionFactory,
    checker: InstagramChecker,
    profile_preview: ProfilePreviewService,
) -> None:
    username = normalize_instagram_username(message.text or "")
    if username is None:
        await message.answer(
            "نام کاربری معتبر نیست. فقط حروف انگلیسی، عدد، نقطه و زیرخط مجاز است. دوباره تلاش کنید."
        )
        return

    async with session_factory() as session:
        existing = await session.scalar(
            select(TargetPage.id).where(
                TargetPage.user_id == db_user.telegram_id,
                TargetPage.instagram_username == username,
            )
        )
    if existing is not None:
        await message.answer("این پیج قبلاً به فهرست شما اضافه شده است.")
        return

    await message.answer("در حال بررسی پیج و ساخت تصویر تأیید… لطفاً کمی صبر کنید.")
    status = await checker.fetch_profile(username)
    if status.outcome == CheckOutcome.ACTIVE:
        confirmed_username = status.canonical_username or username
        preview = profile_result_to_embed(status, confirmed_username)
        await state.update_data(
            pending_username=confirmed_username,
            pending_status=PageStatus.ACTIVE.value,
            pending_profile_id=status.profile_id,
        )
        await state.set_state(AddPageState.waiting_for_confirmation)
        caption = (
            f"{_profile_details_caption(preview)}\n\n"
            "آیا همین پیج را می‌خواهید پایش کنید؟"
        )
        keyboard = registration_confirmation_keyboard(
            profile_url=f"https://www.instagram.com/{confirmed_username}/"
        )
        try:
            card = await profile_preview.render_card(preview)
            await message.answer_photo(
                BufferedInputFile(
                    card,
                    filename=f"farstar-{confirmed_username}.jpg",
                ),
                caption=caption,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception(
                "Could not render or send registration card for %s",
                confirmed_username,
            )
            await message.answer(caption, reply_markup=keyboard)
        return

    if status.outcome == CheckOutcome.DEACTIVATED:
        await state.update_data(
            pending_username=username,
            pending_status=PageStatus.DEACTIVATED.value,
            pending_profile_id=None,
        )
        await state.set_state(AddPageState.waiting_for_confirmation)
        await message.answer(
            f"پیج <b>@{html.escape(username)}</b> اکنون پیدا نشد. ممکن است دی‌اکتیو، حذف یا نام کاربری آن تغییر کرده باشد.\n\n"
            "اگر منتظر فعال‌شدن این نام کاربری هستید، می‌توانید آن را به‌صورت غیرفعال ثبت کنید.",
            reply_markup=registration_confirmation_keyboard(
                inactive=True,
                profile_url=f"https://www.instagram.com/{username}/",
            ),
        )
        return

    await state.update_data(
        pending_username=username,
        pending_status="Unknown",
        pending_profile_id=None,
    )
    await state.set_state(AddPageState.waiting_for_confirmation)
    reason = (
        "اینستاگرام موقتاً تعداد درخواست‌ها را محدود کرده است."
        if status.outcome == CheckOutcome.RATE_LIMITED
        else "اینستاگرام در این لحظه پاسخ قطعی نداد."
    )
    await message.answer(
        f"{reason}\n\n"
        f"نام کاربری: <b>@{html.escape(username)}</b>\n"
        "اگر پیج را می‌شناسید، وضعیت درست را انتخاب کنید. برای پیج دی‌اکتیو می‌توانید گزینه انتظار فعال‌شدن را بزنید.",
        reply_markup=registration_confirmation_keyboard(
            profile_url=f"https://www.instagram.com/{username}/",
            allow_status_choice=True,
        ),
    )


@router.callback_query(
    AddPageState.waiting_for_confirmation,
    F.data.startswith("register:confirm:"),
)
async def confirm_page_registration(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    data = await state.get_data()
    username = data.get("pending_username")
    stored_status = data.get("pending_status")
    requested_status = (callback.data or "").rsplit(":", 1)[1]
    if stored_status == "Unknown":
        status_is_valid = requested_status in {"active", "inactive"}
    else:
        expected_status = (
            "inactive" if stored_status == PageStatus.DEACTIVATED.value else "active"
        )
        status_is_valid = requested_status == expected_status
    if not isinstance(username, str) or not status_is_valid:
        await state.clear()
        await callback.answer(
            "درخواست ثبت معتبر نیست. دوباره تلاش کنید.", show_alert=True
        )
        return

    try:
        async with session_factory() as session:
            user = await session.scalar(
                select(User)
                .where(User.telegram_id == db_user.telegram_id)
                .with_for_update()
            )
            if user is None:
                await state.clear()
                await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
                return
            target_count = await session.scalar(
                select(func.count(TargetPage.id)).where(
                    TargetPage.user_id == user.telegram_id
                )
            )
            subscription = await session.get(UserSubscription, user.telegram_id)
            guard_message = _add_guard_message(
                user,
                int(target_count or 0),
                subscription,
            )
            if guard_message:
                await state.clear()
                await callback.answer(guard_message, show_alert=True)
                return
            existing = await session.scalar(
                select(TargetPage.id).where(
                    TargetPage.user_id == user.telegram_id,
                    TargetPage.instagram_username == username,
                )
            )
            if existing is not None:
                await state.clear()
                await callback.answer("این پیج قبلاً ثبت شده است.", show_alert=True)
                return
            page_status = (
                PageStatus.ACTIVE
                if requested_status == "active"
                else PageStatus.DEACTIVATED
            )
            target = TargetPage(
                instagram_username=username,
                user_id=user.telegram_id,
                last_known_status=page_status,
                last_known_id=data.get("pending_profile_id"),
                last_checked_at=datetime.now(timezone.utc),
            )
            session.add(target)
            await session.flush()
            session.add(
                NotificationSettings(user_id=user.telegram_id, target_page_id=target.id)
            )
            session.add(
                PageEvent(
                    target_page_id=target.id,
                    user_id=user.telegram_id,
                    event_type=(
                        "registered_active"
                        if page_status == PageStatus.ACTIVE
                        else "registered_inactive"
                    ),
                    description=(
                        "پیج فعال پس از تأیید کاربر ثبت شد."
                        if page_status == PageStatus.ACTIVE
                        else "نام کاربری غیرفعال برای انتظار فعال‌شدن ثبت شد."
                    ),
                )
            )
            await session.commit()
    except (IntegrityError, ValueError):
        await state.clear()
        await callback.answer("ثبت پیج انجام نشد یا قبلاً ثبت شده است.", show_alert=True)
        return

    await state.clear()
    status_text = "فعال" if requested_status == "active" else "غیرفعال و منتظر فعال‌شدن"
    if callback.message:
        await callback.message.answer(
            f"پیج <b>@{html.escape(username)}</b> با وضعیت «{status_text}» ثبت شد. ✅",
            reply_markup=main_menu_keyboard(
                is_admin=db_user.telegram_id == settings.admin_telegram_id
            ),
        )
    await callback.answer("پیج ثبت شد. ✅")


@router.callback_query(
    AddPageState.waiting_for_confirmation,
    F.data == "register:cancel",
)
async def cancel_page_registration(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    settings: Settings,
) -> None:
    await state.clear()
    if callback.message:
        await callback.message.answer(
            "ثبت پیج لغو شد.",
            reply_markup=main_menu_keyboard(
                is_admin=db_user.telegram_id == settings.admin_telegram_id
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("page:view:"))
async def view_page(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return

    status_text = PAGE_STATUS_NAMES[page.last_known_status]
    text = (
        f"جزئیات پیج <b>@{html.escape(page.instagram_username)}</b>\n\n"
        f"وضعیت: {status_text}\n"
        f"آخرین بررسی: {format_datetime(page.last_checked_at)}\n"
        f"شناسه اینستاگرام: <code>{html.escape(page.last_known_id or 'ثبت نشده')}</code>"
    )
    if callback.message:
        await callback.message.edit_text(
            text, reply_markup=page_details_keyboard(page.id)
        )
    await callback.answer()


def _profile_details_caption(details: EmbedProfile) -> str:
    username = html.escape(details.username)
    full_name = html.escape(details.full_name or "ثبت نشده")
    if details.is_private:
        privacy = "خصوصی 🔒"
    elif details.is_private is False:
        privacy = "عمومی 🌐"
    else:
        privacy = "نامشخص"
    verified = "تأییدشده ✅" if details.is_verified else "تأییدنشده"
    follower_text = (
        to_persian_digits(details.follower_display)
        if details.follower_display
        else format_count(details.follower_count)
    )
    lines = [
        f"اطلاعات زنده پیج <b>@{username}</b> 🔎",
        "",
        f"نام: <b>{full_name}</b>",
        f"نوع پیج: <b>{privacy}</b>",
        f"وضعیت تأیید: {verified}",
        f"تعداد دنبال‌کننده: <b>{follower_text}</b>",
        f"تعداد دنبال‌شونده: <b>{format_count(details.following_count)}</b>",
        f"تعداد پست: <b>{format_count(details.post_count)}</b>",
    ]
    if details.biography:
        biography = details.biography.strip()
        if len(biography) > 350:
            biography = biography[:347] + "…"
        lines.extend(("", f"بیوگرافی:\n{html.escape(biography)}"))
    else:
        lines.extend(("", "بیوگرافی در نمای عمومی اینستاگرام ارائه نشده است."))
    lines.extend(("", "اطلاعات از رابط وب اینستاگرام و بدون ورود به حساب دریافت شد."))
    return "\n".join(lines)


@router.callback_query(F.data.startswith("profile:details:"))
async def live_profile_details(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
    checker: InstagramChecker,
    profile_preview: ProfilePreviewService,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("نمایش اطلاعات در این پیام ممکن نیست.", show_alert=True)
        return

    await callback.answer("در حال دریافت اطلاعات زنده…")
    status = await checker.fetch_profile(page.instagram_username)
    if status.outcome == CheckOutcome.ACTIVE:
        details = profile_result_to_embed(status, page.instagram_username)
    elif status.outcome == CheckOutcome.DEACTIVATED:
        await callback.message.answer(
            f"پیج <b>@{html.escape(page.instagram_username)}</b> در حال حاضر در دسترس نیست یا دی‌اکتیو شده است."
        )
        return
    else:
        details = await profile_preview.inspect(page.instagram_username)
        if details.outcome != PreviewOutcome.ACTIVE:
            await callback.message.answer(
                "دریافت اطلاعات زنده این پیج فعلاً ممکن نیست. وضعیت ذخیره‌شده تغییر نکرد."
            )
            return

    caption = _profile_details_caption(details)
    try:
        card = await profile_preview.render_card(details)
        await callback.message.answer_photo(
            BufferedInputFile(card, filename=f"farstar-{page.instagram_username}.jpg"),
            caption=caption,
        )
    except Exception:
        logger.exception(
            "Could not render or send live profile card for %s",
            page.instagram_username,
        )
        await callback.message.answer(caption)


@router.callback_query(F.data.startswith("security:view:"))
async def security_tools_menu(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    page = await _owned_page(session_factory, db_user.telegram_id, page_id)
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    text = (
        f"مرکز امنیت پیج <b>@{html.escape(page.instagram_username)}</b> 🛡️\n\n"
        "بررسی زنده، امتیاز هشدار، ممیزی نمای عمومی، اثرانگشت هویت، "
        "خط مبنا، تاریخچه رخداد، گزارش حادثه، تست اعلان، سلامت پایش و تصویر شواهد در دسترس است."
    )
    if callback.message:
        await callback.message.edit_text(
            text, reply_markup=security_tools_keyboard(page.id)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("sec:"))
async def run_security_tool(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
    checker: InstagramChecker,
    profile_preview: ProfilePreviewService,
    redis: Redis,
    settings: Settings,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    action, page_id = parts[1], int(parts[2])
    page = await _owned_page(session_factory, db_user.telegram_id, page_id)
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("نمایش نتیجه ممکن نیست.", show_alert=True)
        return

    await callback.answer("در حال اجرای بررسی…")
    username = html.escape(page.instagram_username)
    keyboard = security_tools_keyboard(page.id)

    if action == "check":
        result = await checker.fetch_profile(page.instagram_username)
        current_names = {
            CheckOutcome.ACTIVE: "فعال و قابل مشاهده ✅",
            CheckOutcome.DEACTIVATED: "غیرفعال یا نام کاربری تغییر کرده ⚠️",
            CheckOutcome.RATE_LIMITED: "محدودیت موقت درخواست ⚠️",
            CheckOutcome.UNKNOWN: "نامشخص؛ وضعیت ذخیره‌شده تغییر نکرد",
        }
        await callback.message.answer(
            f"بررسی فوری <b>@{username}</b>\n\n"
            f"نتیجه زنده: <b>{current_names[result.outcome]}</b>\n"
            f"کد HTTP: <code>{to_persian_digits(result.http_status) if result.http_status else 'ثبت نشد'}</code>\n"
            f"وضعیت ذخیره‌شده: {PAGE_STATUS_NAMES[page.last_known_status]}",
            reply_markup=keyboard,
        )
        return

    if action == "history":
        async with session_factory() as session:
            events = list(
                await session.scalars(
                    select(PageEvent)
                    .where(
                        PageEvent.target_page_id == page.id,
                        PageEvent.user_id == db_user.telegram_id,
                    )
                    .order_by(PageEvent.created_at.desc(), PageEvent.id.desc())
                    .limit(10)
                )
            )
        lines = [f"تاریخچه رخدادهای <b>@{username}</b> 🧾", ""]
        if events:
            for event in events:
                lines.append(
                    f"• {format_datetime(event.created_at)}\n  {html.escape(event.description)}"
                )
        else:
            lines.append("هنوز رخدادی ثبت نشده است.")
        await callback.message.answer("\n".join(lines), reply_markup=keyboard)
        return

    if action == "testalert":
        async with session_factory() as session:
            session.add(
                PageEvent(
                    target_page_id=page.id,
                    user_id=db_user.telegram_id,
                    event_type="alert_test",
                    description="آزمون دستی کانال اعلان با موفقیت اجرا شد.",
                )
            )
            await session.commit()
        await callback.message.answer(
            f"این یک اعلان آزمایشی برای پیج <b>@{username}</b> است. 🔔\n\n"
            "کانال ارسال اعلان ربات سالم است.",
            reply_markup=keyboard,
        )
        return

    if action == "health":
        stored_interval = await redis.get("farstar:checker:interval")
        interval = int(stored_interval or settings.check_interval_seconds)
        preview_ttl = await redis.ttl(
            f"{profile_preview.CACHE_PREFIX}{page.instagram_username.lower()}"
        )
        cooldown_ttl = await redis.ttl(checker.STATUS_COOLDOWN_KEY)
        await callback.message.answer(
            f"سلامت پایش <b>@{username}</b> 💠\n\n"
            f"آخرین بررسی قطعی: {format_datetime(page.last_checked_at)}\n"
            f"فاصله زمان‌بندی: {to_persian_digits(interval)} ثانیه\n"
            f"کش اطلاعات زنده: {'فعال' if preview_ttl > 0 else 'غیرفعال'}"
            + (f" — {to_persian_digits(preview_ttl)} ثانیه" if preview_ttl > 0 else "")
            + "\n"
            f"توقف موقت چکر: {'فعال' if cooldown_ttl > 0 else 'غیرفعال'}"
            + (
                f" — {to_persian_digits(cooldown_ttl)} ثانیه"
                if cooldown_ttl > 0
                else ""
            )
            + "\nحالت دسترسی: نمای عمومی بدون ورود",
            reply_markup=keyboard,
        )
        return

    live_result = await checker.fetch_profile(page.instagram_username)
    if live_result.outcome == CheckOutcome.ACTIVE:
        details = profile_result_to_embed(live_result, page.instagram_username)
    else:
        details = await profile_preview.inspect(
            page.instagram_username, use_cache=False
        )
    if details.outcome != PreviewOutcome.ACTIVE and action in {
        "fingerprint",
        "baseline",
        "audit",
    }:
        await callback.message.answer(
            f"اطلاعات عمومی <b>@{username}</b> برای اجرای این ابزار در دسترس نیست. "
            "این نتیجه «نامشخص» است و به‌تنهایی به معنی دی‌اکتیوشدن پیج نیست.",
            reply_markup=keyboard,
        )
        return

    digest: str | None = None
    identity: dict[str, object] = {}
    if details.outcome == PreviewOutcome.ACTIVE:
        digest, identity = _profile_fingerprint(details)
    baseline_key = f"farstar:security:baseline:{db_user.telegram_id}:{page.id}"
    baseline_raw = await redis.get(baseline_key)
    baseline: dict[str, object] | None = None
    if baseline_raw:
        try:
            parsed = json.loads(baseline_raw)
            if isinstance(parsed, dict):
                baseline = parsed
        except json.JSONDecodeError:
            await redis.delete(baseline_key)
    baseline_digest = str(baseline.get("digest")) if baseline else None

    if action == "fingerprint":
        assert digest is not None
        comparison = (
            "خط مبنا ثبت نشده است"
            if baseline_digest is None
            else "مطابق خط مبنا ✅"
            if baseline_digest == digest
            else "با خط مبنا متفاوت است ⚠️"
        )
        await callback.message.answer(
            f"اثرانگشت هویت <b>@{username}</b> 🧬\n\n"
            f"اثر فعلی: <code>{digest}</code>\n"
            f"مقایسه: <b>{comparison}</b>\n\n"
            "اثر از نام کاربری، نام نمایشی، بیوگرافی، تصویر پروفایل، نوع پیج و نشان تأیید ساخته می‌شود.",
            reply_markup=keyboard,
        )
        return

    if action == "baseline":
        assert digest is not None
        baseline_payload = {
            "digest": digest,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "identity": identity,
        }
        await redis.set(
            baseline_key,
            json.dumps(baseline_payload, ensure_ascii=False, separators=(",", ":")),
        )
        async with session_factory() as session:
            session.add(
                PageEvent(
                    target_page_id=page.id,
                    user_id=db_user.telegram_id,
                    event_type="baseline_saved",
                    description=f"خط مبنای هویت با اثر {digest} ذخیره شد.",
                )
            )
            await session.commit()
        await callback.message.answer(
            f"خط مبنای هویت <b>@{username}</b> ذخیره شد. ✅\n"
            f"اثر مبنا: <code>{digest}</code>",
            reply_markup=keyboard,
        )
        return

    if action == "audit":
        await callback.message.answer(
            f"ممیزی نمای عمومی <b>@{username}</b> 🔍\n\n"
            f"نام نمایشی: <b>{html.escape(details.full_name or 'ثبت نشده')}</b>\n"
            f"نوع پیج: <b>{'خصوصی 🔒' if details.is_private else 'عمومی 🌐'}</b>\n"
            f"نشان تأیید: <b>{'دارد ✅' if details.is_verified else 'ندارد'}</b>\n"
            f"تصویر پروفایل: <b>{'قابل مشاهده' if details.profile_picture_url else 'دیده نشد'}</b>\n"
            f"بیوگرافی: <b>{'قابل مشاهده' if details.biography else 'دیده نشد'}</b>\n"
            f"دنبال‌کننده: <b>{format_count(details.follower_count)}</b>\n"
            f"پست: <b>{format_count(details.post_count)}</b>",
            reply_markup=keyboard,
        )
        return

    stored_interval = await redis.get("farstar:checker:interval")
    interval = int(stored_interval or settings.check_interval_seconds)
    score, reasons = _risk_score(
        page,
        details,
        baseline_exists=baseline_digest is not None,
        baseline_changed=(
            digest is not None
            and baseline_digest is not None
            and baseline_digest != digest
        ),
        interval_seconds=interval,
    )
    if action == "score":
        reasons_text = "\n".join(f"• {html.escape(reason)}" for reason in reasons)
        await callback.message.answer(
            f"امتیاز هشدار <b>@{username}</b>: <b>{to_persian_digits(score)} از ۱۰۰</b>\n\n"
            f"{reasons_text}\n\n"
            "این امتیاز یک شاخص عملیاتی تقریبی است و جایگزین بررسی تخصصی امنیتی نیست.",
            reply_markup=keyboard,
        )
        return

    if action == "report":
        async with session_factory() as session:
            snapshot = await session.get(PageSnapshot, page.id)
            events = list(
                await session.scalars(
                    select(PageEvent)
                    .where(PageEvent.target_page_id == page.id)
                    .order_by(PageEvent.created_at.desc(), PageEvent.id.desc())
                    .limit(20)
                )
            )
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "page": {
                "username": page.instagram_username,
                "stored_status": page.last_known_status.value
                if page.last_known_status
                else None,
                "last_checked_at": page.last_checked_at.isoformat()
                if page.last_checked_at
                else None,
            },
            "public_profile": identity
            | {
                "followers": details.follower_count,
                "following": details.following_count,
                "posts": details.post_count,
                "is_private": details.is_private,
                "is_verified": details.is_verified,
            },
            "monitoring_snapshot": (
                {
                    "profile_picture_key": snapshot.profile_picture_key,
                    "full_name": snapshot.full_name,
                    "biography": snapshot.biography,
                    "followers": snapshot.follower_count,
                    "following": snapshot.following_count,
                    "posts": snapshot.post_count,
                    "is_private": snapshot.is_private,
                    "is_verified": snapshot.is_verified,
                    "updated_at": snapshot.updated_at.isoformat(),
                }
                if snapshot
                else None
            ),
            "identity_fingerprint": digest,
            "baseline_fingerprint": baseline_digest,
            "risk_score": score,
            "risk_reasons": reasons,
            "events": [
                {
                    "type": event.event_type,
                    "description": event.description,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
        }
        payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
        await callback.message.answer_document(
            BufferedInputFile(
                payload, filename=f"farstar-incident-{page.instagram_username}.json"
            ),
            caption=f"گزارش امنیتی پیج <b>@{username}</b> آماده شد. 📄",
            reply_markup=keyboard,
        )
        return

    await callback.message.answer("این ابزار شناخته نشد.", reply_markup=keyboard)


@router.callback_query(F.data.startswith("page:delete:"))
async def ask_delete_page(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            f"آیا از حذف <b>@{html.escape(page.instagram_username)}</b> مطمئن هستید؟",
            reply_markup=confirm_delete_keyboard(page.id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("page:confirm_delete:"))
async def delete_page(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
        if page is None:
            await callback.answer("این پیج قبلاً حذف شده است.", show_alert=True)
            return
        await session.delete(page)
        await session.commit()

    pages = await _load_pages(session_factory, db_user.telegram_id)
    if callback.message:
        await callback.message.edit_text(
            "پیج با موفقیت حذف شد. ✅",
            reply_markup=pages_keyboard(pages),
        )
    await callback.answer()


@router.message(F.text == "تنظیمات اعلان‌ها ⚙️")
async def notification_settings_menu(
    message: Message,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    pages = await _load_pages(session_factory, db_user.telegram_id)
    if not pages:
        await message.answer("ابتدا از بخش مدیریت پیج‌ها یک پیج اضافه کنید.")
        return
    await message.answer(
        "برای تنظیم جداگانه اعلان‌ها، یک پیج را انتخاب کنید:",
        reply_markup=settings_pages_keyboard(pages),
    )


async def _get_page_settings(
    session_factory: SessionFactory,
    user_id: int,
    page_id: int,
) -> tuple[TargetPage | None, NotificationSettings | None]:
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == user_id,
            )
        )
        if page is None:
            return None, None
        settings = await session.get(NotificationSettings, (user_id, page_id))
        if settings is None:
            settings = NotificationSettings(user_id=user_id, target_page_id=page_id)
            session.add(settings)
            await session.commit()
        return page, settings


@router.callback_query(F.data.startswith("settings:view:"))
async def view_notification_settings(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    page, settings = await _get_page_settings(
        session_factory, db_user.telegram_id, page_id
    )
    if page is None or settings is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            f"تنظیم اعلان‌های <b>@{html.escape(page.instagram_username)}</b>\n\n"
            "برای فعال یا غیرفعال‌کردن هر اعلان، روی آن بزنید.",
            reply_markup=notification_settings_keyboard(page.id, settings),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_notification(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    _, page_id_raw, field = (callback.data or "").split(":", 2)
    page_id = int(page_id_raw)
    field_map = {
        "activation": "notify_activation",
        "deactivation": "notify_deactivation",
        "username": "notify_username_change",
    }
    attribute = field_map.get(field)
    if attribute is None:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return

    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
        if page is None:
            await callback.answer("این پیج پیدا نشد.", show_alert=True)
            return
        settings = await session.get(
            NotificationSettings, (db_user.telegram_id, page_id)
        )
        if settings is None:
            settings = NotificationSettings(
                user_id=db_user.telegram_id,
                target_page_id=page_id,
            )
            session.add(settings)
            await session.flush()
        setattr(settings, attribute, not getattr(settings, attribute))
        await session.commit()

    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=notification_settings_keyboard(page_id, settings)
        )
    await callback.answer("تنظیم اعلان ذخیره شد. ✅")


@router.message(F.text == "خرید اشتراک 💎")
async def subscription_menu(
    message: Message,
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        plans = list(
            await session.scalars(
                select(SubscriptionPlan)
                .where(SubscriptionPlan.is_active.is_(True))
                .order_by(SubscriptionPlan.price, SubscriptionPlan.id)
            )
        )
    if not plans:
        await message.answer(
            "در حال حاضر پلن فروشی فعالی تعریف نشده است. لطفاً با پشتیبانی تماس بگیرید."
        )
        return
    await message.answer(
        "پلن‌های اشتراک فارستار وارنر 💎\n\n"
        "کاربر رایگان می‌تواند ۱ پیج ثبت کند. برای افزایش ظرفیت، یک پلن را انتخاب کنید:",
        reply_markup=purchase_plans_keyboard(plans),
    )


@router.callback_query(F.data == "buy:list")
async def purchase_plan_list(
    callback: CallbackQuery,
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        plans = list(
            await session.scalars(
                select(SubscriptionPlan)
                .where(SubscriptionPlan.is_active.is_(True))
                .order_by(SubscriptionPlan.price, SubscriptionPlan.id)
            )
        )
    if callback.message:
        await callback.message.edit_text(
            "یک پلن اشتراک را انتخاب کنید:",
            reply_markup=purchase_plans_keyboard(plans),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("buy:plan:"))
async def purchase_plan_details(
    callback: CallbackQuery,
    session_factory: SessionFactory,
) -> None:
    plan_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        payment = await session.get(PaymentConfig, 1)
    if plan is None or not plan.is_active:
        await callback.answer("این پلن دیگر فعال نیست.", show_alert=True)
        return
    price = format_count(plan.price)
    text = (
        f"پلن <b>{html.escape(plan.name)}</b> 💎\n\n"
        f"مدت اعتبار: <b>{to_persian_digits(plan.duration_days)} روز</b>\n"
        f"ظرفیت پایش: <b>{to_persian_digits(plan.target_limit)} پیج</b>\n"
        f"مبلغ: <b>{price} تومان</b>\n\n"
        "روش پرداخت را انتخاب کنید:"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=purchase_methods_keyboard(
                plan.id,
                payment.support_username if payment else None,
                bool(payment and payment.card_number),
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("buy:card:"))
async def begin_card_payment(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
) -> None:
    plan_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        payment = await session.get(PaymentConfig, 1)
    if plan is None or not plan.is_active or payment is None or not payment.card_number:
        await callback.answer("پرداخت کارت‌به‌کارت در دسترس نیست.", show_alert=True)
        return
    await state.update_data(purchase_plan_id=plan.id)
    await state.set_state(PurchaseState.waiting_for_receipt)
    card_number = html.escape(payment.card_number)
    card_holder = html.escape(payment.card_holder or "ثبت نشده")
    if callback.message:
        await callback.message.answer(
            "پرداخت کارت‌به‌کارت 💳\n\n"
            f"مبلغ دقیق: <b>{format_count(plan.price)} تومان</b>\n"
            f"شماره کارت: <code>{card_number}</code>\n"
            f"به نام: <b>{card_holder}</b>\n\n"
            "پس از پرداخت، تصویر واضح فیش را همین‌جا ارسال کنید. "
            "هر فیش فقط یک‌بار قابل ثبت است و فعال‌سازی پس از بررسی دستی مدیر انجام می‌شود.",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(PurchaseState.waiting_for_receipt, F.photo | F.document)
async def receive_payment_receipt(
    message: Message,
    state: FSMContext,
    db_user: User,
    session_factory: SessionFactory,
    bot: Bot,
    settings: Settings,
) -> None:
    data = await state.get_data()
    plan_id = data.get("purchase_plan_id")
    if not isinstance(plan_id, int):
        await state.clear()
        await message.answer("اطلاعات خرید منقضی شده است؛ دوباره پلن را انتخاب کنید.")
        return

    if message.photo:
        media = message.photo[-1]
        file_id = media.file_id
        file_unique_id = media.file_unique_id
        file_type = "photo"
    elif message.document and (
        (message.document.mime_type or "").startswith("image/")
        or message.document.mime_type == "application/pdf"
    ):
        file_id = message.document.file_id
        file_unique_id = message.document.file_unique_id
        file_type = "document"
    else:
        await message.answer("فیش را به‌صورت تصویر یا فایل PDF ارسال کنید.")
        return

    try:
        async with session_factory() as session:
            plan = await session.get(SubscriptionPlan, plan_id)
            if plan is None or not plan.is_active:
                await state.clear()
                await message.answer("پلن انتخاب‌شده دیگر فعال نیست.")
                return
            receipt = PaymentReceipt(
                user_id=db_user.telegram_id,
                plan_id=plan.id,
                plan_name=plan.name,
                duration_days=plan.duration_days,
                target_limit=plan.target_limit,
                amount=plan.price,
                file_id=file_id,
                file_unique_id=file_unique_id,
                file_type=file_type,
                status=ReceiptStatus.PENDING,
            )
            session.add(receipt)
            await session.flush()
            receipt_id = receipt.id
            await session.commit()
    except IntegrityError:
        await state.clear()
        await message.answer(
            "این فایل قبلاً به‌عنوان فیش ثبت شده است و امکان استفاده دوباره از آن وجود ندارد."
        )
        return

    username_text = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    caption = (
        "فیش پرداخت جدید 🧾\n\n"
        f"شماره فیش: <code>{to_persian_digits(receipt_id)}</code>\n"
        f"شناسه کاربر: <code>{to_persian_digits(db_user.telegram_id)}</code>\n"
        f"نام کاربری: {username_text}\n"
        f"پلن: <b>{html.escape(plan.name)}</b>\n"
        f"مبلغ مورد انتظار: <b>{format_count(plan.price)} تومان</b>\n"
        f"مدت: {to_persian_digits(plan.duration_days)} روز\n"
        "وضعیت: در انتظار بررسی مدیر"
    )
    markup = receipt_review_keyboard(receipt_id)
    try:
        if file_type == "photo":
            await bot.send_photo(
                settings.admin_telegram_id,
                file_id,
                caption=caption,
                reply_markup=markup,
            )
        else:
            await bot.send_document(
                settings.admin_telegram_id,
                file_id,
                caption=caption,
                reply_markup=markup,
            )
    except TelegramAPIError:
        logger.exception(
            "Could not deliver receipt %s to the administrator", receipt_id
        )
    await state.clear()
    await message.answer(
        "فیش شما ثبت و برای مدیر ارسال شد. پس از بررسی، نتیجه در همین ربات اعلام می‌شود. ✅",
        reply_markup=main_menu_keyboard(is_admin=False),
    )


@router.message(PurchaseState.waiting_for_receipt)
async def reject_invalid_receipt(message: Message) -> None:
    await message.answer(
        "لطفاً تصویر فیش یا فایل PDF را ارسال کنید؛ برای خروج «لغو عملیات» را بزنید."
    )


@router.message(F.text == "حساب کاربری 👤")
async def account_info(
    message: Message,
    db_user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    async with session_factory() as session:
        target_count = await session.scalar(
            select(func.count(TargetPage.id)).where(
                TargetPage.user_id == db_user.telegram_id
            )
        )
        subscription = await session.get(UserSubscription, db_user.telegram_id)
    now = datetime.now(timezone.utc)
    is_admin = db_user.telegram_id == settings.admin_telegram_id
    role_text = "مدیر اصلی 🛡️" if is_admin else "کاربر"
    if subscription is not None and subscription.expires_at > now:
        plan_name = subscription.plan_name
        target_limit = subscription.target_limit
        validity = "فعال ✅"
        expiry = format_datetime(subscription.expires_at)
    elif is_admin or (
        db_user.plan_tier != PlanTier.FREE and db_user.subscription_expiry > now
    ):
        plan_name = PLAN_NAMES[db_user.plan_tier]
        target_limit = db_user.plan_tier.target_limit
        validity = "فعال ✅"
        expiry = format_datetime(db_user.subscription_expiry)
    else:
        plan_name = "رایگان"
        target_limit = PlanTier.FREE.target_limit
        validity = "فعال بدون انقضا ✅"
        expiry = "بدون انقضا"
    await message.answer(
        "اطلاعات حساب کاربری 👤\n\n"
        f"شناسه تلگرام: <code>{to_persian_digits(db_user.telegram_id)}</code>\n"
        f"نقش: <b>{role_text}</b>\n"
        f"پلن: <b>{html.escape(plan_name)}</b>\n"
        f"وضعیت اشتراک: {validity}\n"
        f"تاریخ پایان: {expiry}\n"
        f"تعداد پیج‌ها: {to_persian_digits(target_count or 0)} از "
        f"{to_persian_digits(target_limit)}",
        reply_markup=main_menu_keyboard(is_admin=is_admin),
    )


@router.message()
async def unknown_message(
    message: Message,
    db_user: User,
    settings: Settings,
) -> None:
    await message.answer(
        "متوجه درخواست شما نشدم. لطفاً یکی از گزینه‌های منوی اصلی را انتخاب کنید.",
        reply_markup=main_menu_keyboard(
            is_admin=db_user.telegram_id == settings.admin_telegram_id
        ),
    )
