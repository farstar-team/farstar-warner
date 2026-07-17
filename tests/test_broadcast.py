from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import create_engine, inspect

from bot.broadcast import (
    BroadcastView,
    _send_broadcast_message,
    broadcast_progress_text,
    broadcast_status_keyboard,
    cleanup_broadcast_campaigns,
    validate_broadcast_content,
)
from bot.models import Base, BroadcastCampaign, BroadcastDelivery


def test_broadcast_content_rejects_expanded_html_over_limit() -> None:
    assert validate_broadcast_content("پیام کوتاه", "<b>" + ("x" * 4000) + "</b>")
    assert validate_broadcast_content("پیام کوتاه", "<b>پیام کوتاه</b>") is None


def test_broadcast_models_enforce_idempotency_constraints() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    campaign_uniques = inspect(engine).get_unique_constraints(
        BroadcastCampaign.__tablename__
    )
    campaign_indexes = inspect(engine).get_indexes(BroadcastCampaign.__tablename__)
    delivery_uniques = inspect(engine).get_unique_constraints(
        BroadcastDelivery.__tablename__
    )
    assert any("campaign_key" in item["column_names"] for item in campaign_uniques) or any(
        item.get("unique") and "campaign_key" in item["column_names"]
        for item in campaign_indexes
    )
    assert any(
        set(item["column_names"]) == {"campaign_id", "recipient_id"}
        for item in delivery_uniques
    )


@pytest.mark.asyncio
async def test_broadcast_sender_obeys_telegram_retry_after() -> None:
    bot = AsyncMock()
    method = SendMessage(chat_id=123, text="پیام")
    bot.send_message.side_effect = TelegramRetryAfter(
        method=method,
        message="Flood control exceeded",
        retry_after=17,
    )

    result = await _send_broadcast_message(bot, 123, "پیام")

    assert result.delivered is False
    assert result.retry_after == 17
    assert result.terminal is False


@pytest.mark.asyncio
async def test_broadcast_sender_marks_blocked_user_terminal() -> None:
    bot = AsyncMock()
    method = SendMessage(chat_id=123, text="پیام")
    bot.send_message.side_effect = TelegramForbiddenError(
        method=method,
        message="bot was blocked by the user",
    )

    result = await _send_broadcast_message(bot, 123, "پیام")

    assert result.delivered is False
    assert result.terminal is True
    assert result.retry_after is None


def test_active_broadcast_requires_confirmed_stop() -> None:
    view = BroadcastView(
        campaign_id=42,
        status="Delivering",
        total=100,
        sent=40,
        failed=2,
        skipped=0,
        pending=58,
        created_at=datetime.now(timezone.utc),
        completed_at=None,
        progress_chat_id=1,
        progress_message_id=2,
    )

    markup = broadcast_status_keyboard(view)
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "admin:broadcast:cancel_prompt:42" in callbacks
    assert "admin:broadcast:cancel:42" not in callbacks
    text = broadcast_progress_text(view)
    assert "ارسال موفق: ۴۰" in text
    assert "در صف/تلاش مجدد: ۵۸" in text


@pytest.mark.asyncio
async def test_broadcast_cleanup_commits_terminal_retention_delete() -> None:
    class Result:
        rowcount = 3

    class Session:
        committed = False

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, _: object) -> Result:
            return Result()

        async def commit(self) -> None:
            self.committed = True

    session = Session()

    def session_factory() -> Session:
        return session

    deleted = await cleanup_broadcast_campaigns(session_factory, retention_days=90)

    assert deleted == 3
    assert session.committed is True
