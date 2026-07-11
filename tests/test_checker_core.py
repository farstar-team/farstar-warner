from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect

from bot.checker import (
    CheckOutcome,
    CurlResponse,
    DeliveryResult,
    InstagramChecker,
    NotificationPayload,
    ProfileResult,
)
from bot.config import Settings
from bot.credential_store import CredentialStore
from bot.models import (
    Base,
    NotificationOutbox,
    NotificationSettings,
    PageSnapshot,
    PageSnapshotHistory,
    PageStatus,
    TargetPage,
    User,
)
from bot.profile_preview import PreviewOutcome, ProfilePreviewService


def _settings(key: str) -> Settings:
    return Settings(
        telegram_bot_token="123456:abcdefghijklmnopqrstuvwxyz",
        admin_telegram_id=1,
        postgres_password="postgres-secret",
        redis_password="redis-secret",
        credential_encryption_key=key,
    )


@pytest.mark.asyncio
async def test_deactivated_to_active_without_profile_id_queues_one_alert() -> None:
    added: list[object] = []
    target = TargetPage(
        id=12,
        instagram_username="reactivated.user",
        user_id=321,
        last_known_status=PageStatus.DEACTIVATED,
        last_known_id=None,
        status_confirmed=True,
        consecutive_active_checks=0,
        consecutive_deactivated_checks=4,
    )
    owner = User(telegram_id=321)
    owner.admin_report_copy = False
    owner.admin_report_categories = ""
    notification = NotificationSettings(user_id=321, target_page_id=12)
    notification.notify_activation = True

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object | None:
            return {
                TargetPage: target,
                User: owner,
                NotificationSettings: notification,
            }.get(model)

        async def scalar(self, _: object) -> TargetPage:
            return target

        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    class Redis:
        async def delete(self, *_: object) -> None:
            return None

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker = InstagramChecker.__new__(InstagramChecker)
    checker.session_factory = Session
    checker.redis = Redis()
    checker.diagnostics = Diagnostics()
    checker.settings = SimpleNamespace(
        deactivation_confirmations=2,
        deactivation_confirmation_delay_seconds=0,
        admin_telegram_id=999,
    )
    evidence = ProfileResult(
        CheckOutcome.ACTIVE,
        source="http_public_embed",
        metadata_complete=False,
        canonical_username="reactivated.user",
        profile_id=None,
        is_private=True,
        http_status=200,
    )
    checker.fetch_profile = AsyncMock(return_value=evidence)
    checker._notify = AsyncMock()

    result = await checker._check_target(target.id, positive_only=True)

    assert result == evidence
    assert target.last_known_status is PageStatus.ACTIVE
    assert target.consecutive_active_checks == 1
    assert target.consecutive_deactivated_checks == 0
    outbox = [value for value in added if isinstance(value, NotificationOutbox)]
    assert len(outbox) == 1
    assert outbox[0].recipient_id == 321
    assert outbox[0].category == "ACTIVATED"
    checker._notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limited_worker_defers_every_target_with_truthful_counters() -> None:
    checker = InstagramChecker.__new__(InstagramChecker)
    checker._rate_limited = asyncio.Event()
    checker._rate_limited.set()
    checker._lock_lost = asyncio.Event()
    checker._official_ready = False
    checker.settings = SimpleNamespace(
        page_check_delay_min_seconds=0,
        page_check_delay_max_seconds=0,
    )
    checker._check_target = AsyncMock()
    checker._record_deferred_check = AsyncMock()

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker.diagnostics = Diagnostics()
    target_ids = (21, 22, 23, 24, 25)
    counters = {
        "planned": len(target_ids),
        "processed": 0,
        "active": 0,
        "deactivated": 0,
        "unknown": 0,
        "rate_limited": 0,
        "deferred": 0,
        "failed": 0,
    }
    queue: asyncio.Queue[int] = asyncio.Queue()
    for target_id in target_ids:
        queue.put_nowait(target_id)

    workers = [
        asyncio.create_task(checker._worker(queue, counters=counters))
        for _ in range(3)
    ]
    await asyncio.wait_for(queue.join(), timeout=1)
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    assert checker._record_deferred_check.await_count == len(target_ids)
    deferred_ids = {
        call.args[0] for call in checker._record_deferred_check.await_args_list
    }
    assert deferred_ids == set(target_ids)
    assert all(
        call.args[1] == "CircuitDeferred"
        for call in checker._record_deferred_check.await_args_list
    )
    checker._check_target.assert_not_awaited()
    assert counters["planned"] == len(target_ids)
    assert counters["deferred"] == len(target_ids)
    assert counters["processed"] == 0
    assert counters["failed"] == 0
    assert counters["planned"] == counters["processed"] + counters["deferred"]


@pytest.mark.asyncio
async def test_notification_outbox_retries_then_marks_successful_delivery_sent() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    payload = NotificationPayload(
        message="profile activated",
        username="reactivated.user",
        title="activation",
        category="ACTIVATED",
        primary_label="status",
        primary_value="active",
    )
    row = NotificationOutbox(
        id=91,
        event_key="target:12:event:ACTIVATED:owner:0",
        recipient_id=321,
        target_page_id=12,
        category="ACTIVATED",
        payload_json=InstagramChecker._notification_payload_json(payload),
        status="Pending",
        attempt_count=0,
        next_attempt_at=now,
    )

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def scalars(self, _: object) -> list[NotificationOutbox]:
            return [row]

        async def scalar(self, _: object) -> NotificationOutbox:
            return row

        async def commit(self) -> None:
            return None

    class Lock:
        async def acquire(self, *_: object, **__: object) -> bool:
            return True

        async def release(self) -> None:
            return None

    class Redis:
        def lock(self, *_: object, **__: object) -> Lock:
            return Lock()

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker = InstagramChecker.__new__(InstagramChecker)
    checker.session_factory = Session
    checker.redis = Redis()
    checker.diagnostics = Diagnostics()
    checker.settings = SimpleNamespace(
        outbox_batch_size=10,
        outbox_max_attempts=5,
    )
    checker._deliver_notification = AsyncMock(
        side_effect=[
            DeliveryResult(False, error="temporary Telegram failure", retry_after=1),
            DeliveryResult(True),
        ]
    )

    assert await checker.dispatch_notification_outbox() == 0
    assert row.status == "Retry"
    assert row.attempt_count == 1
    assert row.last_error == "temporary Telegram failure"

    row.next_attempt_at = now
    assert await checker.dispatch_notification_outbox() == 1
    assert row.status == "Sent"
    assert row.attempt_count == 2
    assert row.sent_at is not None
    assert row.last_error is None


@pytest.mark.asyncio
async def test_existing_cooldown_ttl_is_not_extended() -> None:
    checker = InstagramChecker.__new__(InstagramChecker)
    checker.settings = SimpleNamespace(rate_limit_cooldown_seconds=900)
    checker.redis = SimpleNamespace(
        ttl=AsyncMock(return_value=347),
        delete=AsyncMock(),
        set=AsyncMock(),
    )

    remaining = await checker._activate_cooldown(1800)

    assert remaining == 347
    checker.redis.ttl.assert_awaited_once_with(checker.STATUS_COOLDOWN_KEY)
    checker.redis.delete.assert_not_awaited()
    checker.redis.set.assert_not_awaited()


def test_public_parser_reads_only_root_user_node() -> None:
    body = json.dumps(
        {
            "data": {
                "user": {
                    "id": "47796612144",
                    "username": "mahdy.security",
                    "full_name": "Mahdy",
                    "biography": "security",
                    "external_url": "https://security.example/report",
                    "is_business_account": True,
                    "is_professional_account": True,
                    "category_name": "Security Service",
                    "edge_followed_by": {"count": 168727},
                    "edge_follow": {"count": 1175},
                    "edge_owner_to_timeline_media": {
                        "count": 1,
                        "edges": [{"node": {"id": "WRONG_POST_ID"}}],
                    },
                }
            }
        }
    ).encode()

    result = InstagramChecker._parse_profile_response(body, "mahdy.security", 200)

    assert result is not None
    assert result.outcome is CheckOutcome.ACTIVE
    assert result.profile_id == "47796612144"
    assert result.post_count == 1
    assert result.metadata_complete is True
    assert result.external_link == "https://security.example/report"
    assert result.external_link_observed is True
    assert result.account_type == "business"
    assert result.account_type_observed is True
    assert result.category_name == "Security Service"


def test_embed_parser_extracts_security_metadata_from_root_profile_node() -> None:
    payload = {
        "require": [
            {
                "data": {
                    "user": {
                        "id": "55",
                        "username": "secure.page",
                        "full_name": "Secure Page",
                        "is_private": False,
                        "is_business_account": False,
                        "is_professional_account": False,
                        "external_url": "https://safe.example/home",
                        "edge_followed_by": {"count": 25},
                    }
                }
            }
        ]
    }
    raw_html = f"""
    <html><head>
      <meta property="og:title" content="Secure Page (@secure.page) • Instagram" />
      <meta property="og:description" content="25 Followers, 4 Following, 2 Posts" />
    </head><body>@secure.page
      <script type="application/json">{json.dumps(payload)}</script>
    </body></html>
    """

    profile = ProfilePreviewService._parse_embed_html(raw_html, "secure.page")

    assert profile is not None
    assert profile.external_link == "https://safe.example/home"
    assert profile.external_link_observed is True
    assert profile.account_type == "personal"
    assert profile.account_type_observed is True


def test_malicious_link_radar_is_local_and_signature_based() -> None:
    reasons = InstagramChecker._malicious_link_reasons(
        "https://verify-wallet-connect.example/claim-bonus"
    )

    assert "الگوی ورود یا تأیید هویت" in reasons
    assert "الگوی جایزه یا رمزارز مشکوک" in reasons


def test_forensic_history_schema_contains_required_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    columns = {
        column["name"]
        for column in inspect(engine).get_columns(PageSnapshotHistory.__tablename__)
    }

    assert {
        "observed_at",
        "username",
        "follower_count",
        "following_count",
        "post_count",
        "external_link",
    } <= columns
    outbox_columns = {
        column["name"]
        for column in inspect(engine).get_columns(NotificationOutbox.__tablename__)
    }
    assert {
        "event_key",
        "recipient_id",
        "payload_json",
        "status",
        "attempt_count",
        "next_attempt_at",
        "sent_at",
    } <= outbox_columns


def test_graphql_search_exact_match_is_active() -> None:
    body = json.dumps(
        {
            "data": {
                "xdt_api__v1__fbsearch__non_profiled_serp": {
                    "users": [
                        {
                            "id": "8939876413",
                            "username": "sajjad_janalizadeh",
                            "full_name": "Sajjad Janalizadeh",
                            "is_private": True,
                        },
                        {"id": "999", "username": "suggested_account"},
                    ]
                }
            },
            "status": "ok",
        }
    ).encode()

    result = InstagramChecker._parse_graphql_search_response(
        body,
        "sajjad_janalizadeh",
        200,
    )

    assert result is not None
    assert result.outcome is CheckOutcome.ACTIVE
    assert result.profile_id == "8939876413"
    assert result.source == "graphql_username_search"
    assert result.metadata_complete is False


def test_graphql_search_valid_absence_is_deactivated_evidence() -> None:
    body = json.dumps(
        {
            "data": {
                "xdt_api__v1__fbsearch__non_profiled_serp": {
                    "users": [{"id": "999", "username": "another_account"}]
                }
            },
            "status": "ok",
        }
    ).encode()

    result = InstagramChecker._parse_graphql_search_response(
        body,
        "missing_account",
        200,
    )

    assert result is not None
    assert result.outcome is CheckOutcome.DEACTIVATED
    assert result.source == "graphql_username_search_absence"


def test_graphql_search_rejects_failed_or_malformed_payload() -> None:
    failed = json.dumps({"status": "fail", "data": {}}).encode()
    malformed = json.dumps({"status": "ok", "data": {}}).encode()

    assert (
        InstagramChecker._parse_graphql_search_response(failed, "target", 200) is None
    )
    assert (
        InstagramChecker._parse_graphql_search_response(malformed, "target", 200)
        is None
    )


def test_business_discovery_parser_maps_official_fields() -> None:
    result = InstagramChecker._parse_business_discovery(
        {
            "id": "17841401441775531",
            "username": "bluebottle",
            "name": "Blue Bottle Coffee",
            "biography": "Coffee",
            "followers_count": 267793,
            "follows_count": 12,
            "media_count": 1205,
            "profile_picture_url": "https://example.invalid/photo.jpg",
        },
        "bluebottle",
    )

    assert result is not None
    assert result.profile_id == "17841401441775531"
    assert result.follower_count == 267793
    assert result.following_count == 12
    assert result.post_count == 1205


def test_graph_error_code_is_strict() -> None:
    assert InstagramChecker._graph_error_code({"error": {"code": 100}}) == 100
    assert InstagramChecker._graph_error_code({"error": {"code": "100"}}) is None
    assert InstagramChecker._graph_error_code(None) is None


def test_monitoring_token_is_encrypted_at_rest() -> None:
    key = Fernet.generate_key().decode("ascii")
    store = CredentialStore(_settings(key))
    token = "EAAB-test-token-that-must-not-be-stored-in-plain-text"

    encrypted = store.encrypt(token)

    assert token not in encrypted
    assert store.decrypt(encrypted) == token


def test_monitoring_account_table_is_part_of_schema() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("instagram_monitoring_accounts")
    }

    assert {
        "instagram_user_id",
        "access_token_encrypted",
        "last_health_status",
        "last_checked_at",
    } <= columns

    target_columns = {
        column["name"] for column in inspect(engine).get_columns("target_pages")
    }
    assert {
        "last_evidence_source",
        "last_evidence_at",
        "last_deactivation_evidence_at",
    } <= target_columns


@pytest.mark.asyncio
async def test_official_health_is_disabled_in_osint_only_mode() -> None:
    token = "EAAB-secret-token-value"

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("OSINT-only mode must not call Meta Graph API")

    checker = InstagramChecker.__new__(InstagramChecker)
    checker.settings = _settings(Fernet.generate_key().decode("ascii"))
    checker._graph_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker.diagnostics = Diagnostics()
    try:
        healthy, _, username = await checker.validate_monitoring_account(
            "17841400000000000",
            token,
            force=True,
        )
    finally:
        await checker._graph_http.aclose()

    assert healthy is False
    assert username is None


@pytest.mark.asyncio
async def test_confirmed_active_to_deactivated_transition_notifies_owner() -> None:
    added: list[object] = []
    target = TargetPage(
        id=7,
        instagram_username="missing_account",
        user_id=123,
        last_known_status=PageStatus.ACTIVE,
        last_known_id="778899",
        status_confirmed=True,
        consecutive_active_checks=3,
        consecutive_deactivated_checks=0,
    )
    owner = User(telegram_id=123)
    owner.admin_report_copy = False
    owner.admin_report_categories = ""
    notification = NotificationSettings(user_id=123, target_page_id=7)
    notification.notify_deactivation = True

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object | None:
            if model is TargetPage:
                return target
            if model is User:
                return owner
            if model is NotificationSettings:
                return notification
            return None

        async def scalar(self, _: object) -> TargetPage:
            return target

        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    class Redis:
        async def delete(self, *_: object) -> None:
            return None

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker = InstagramChecker.__new__(InstagramChecker)
    checker.session_factory = Session
    checker.redis = Redis()
    checker.diagnostics = Diagnostics()
    checker.settings = SimpleNamespace(
        deactivation_confirmations=2,
        deactivation_confirmation_delay_seconds=0,
        admin_telegram_id=999,
    )
    evidence = ProfileResult(
        CheckOutcome.DEACTIVATED,
        source="graphql_username_search_absence",
        http_status=404,
    )
    checker.fetch_profile = AsyncMock(side_effect=[evidence, evidence])
    checker._notify = AsyncMock()

    result = await checker._check_target(target.id)

    assert result is evidence
    assert target.last_known_status is PageStatus.DEACTIVATED
    assert target.consecutive_deactivated_checks == 2
    assert target.last_evidence_source == "graphql_username_search_absence"
    assert target.last_deactivation_evidence_at is not None
    outbox = [value for value in added if isinstance(value, NotificationOutbox)]
    assert len(outbox) == 1
    assert outbox[0].category == "DEACTIVATED"
    checker._notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_profile_401_uses_discovery_before_circuit_breaker() -> None:
    checker = InstagramChecker.__new__(InstagramChecker)
    checker._official_ready = False
    evidence = ProfileResult(
        CheckOutcome.DEACTIVATED,
        source="graphql_username_search_absence",
        http_status=404,
    )
    checker._search_profile_via_graphql = AsyncMock(return_value=evidence)
    checker._activate_cooldown = AsyncMock(return_value=900)
    checker._record_final_failure = AsyncMock()

    result = await checker._profile_result_from_response(
        CurlResponse(
            401,
            b'{"message":"Please wait a few minutes","status":"fail"}',
            transport="httpx-HTTP/2-proxy",
        ),
        "missing_account",
    )

    assert result is evidence
    checker._activate_cooldown.assert_not_awaited()
    checker._record_final_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_queue_completes_without_sentinel_tokens() -> None:
    checker = InstagramChecker.__new__(InstagramChecker)
    checker._rate_limited = asyncio.Event()
    checker._lock_lost = asyncio.Event()
    checker._official_ready = False
    checker.settings = SimpleNamespace(
        page_check_delay_min_seconds=0,
        page_check_delay_max_seconds=0,
    )
    checker._check_target = AsyncMock()

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker.diagnostics = Diagnostics()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for target_id in (1, 2, 3, 4):
        queue.put_nowait(target_id)
    workers = [asyncio.create_task(checker._worker(queue)) for _ in range(2)]
    await asyncio.wait_for(queue.join(), timeout=1)
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    assert checker._check_target.await_count == 4


@pytest.mark.asyncio
async def test_preflight_stops_after_first_verified_active_result() -> None:
    checker = InstagramChecker.__new__(InstagramChecker)
    checker._rate_limited = asyncio.Event()
    checker.fetch_profile = AsyncMock(
        side_effect=[
            ProfileResult(
                CheckOutcome.ACTIVE,
                source="graphql_username_search",
                canonical_username="instagram",
                profile_id="1",
            ),
            ProfileResult(CheckOutcome.RATE_LIMITED),
            ProfileResult(CheckOutcome.UNKNOWN),
        ]
    )

    class Redis:
        async def get(self, *_: object) -> None:
            return None

        async def delete(self, *_: object) -> None:
            return None

        async def set(self, *_: object, **__: object) -> bool:
            return True

        async def incr(self, *_: object) -> int:
            return 1

        async def expire(self, *_: object) -> None:
            return None

        def lock(self, *_: object, **__: object) -> object:
            class Lock:
                async def acquire(self, *_: object, **__: object) -> bool:
                    return True

                async def release(self) -> None:
                    return None

            return Lock()

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker.redis = Redis()
    checker.diagnostics = Diagnostics()
    checker._notify = AsyncMock()
    checker.settings = SimpleNamespace(
        admin_telegram_id=999,
        baseline_usernames=("instagram", "cristiano", "nasa"),
        instagram_request_timeout_seconds=1,
        preflight_cache_seconds=300,
        health_failure_alert_threshold=2,
        health_alert_reminder_seconds=21600,
        rate_limit_cooldown_seconds=900,
    )

    assert await checker.reference_profile_preflight() is True
    assert checker.fetch_profile.await_count == 1
    checker._notify.assert_not_awaited()


def test_text_delta_normalization_coerces_none_and_empty_string() -> None:
    assert InstagramChecker._normalize_text(None) == ""
    assert InstagramChecker._normalize_text("") == ""
    assert InstagramChecker._normalize_text("  Name  ") == "Name"


def test_embed_html_parser_extracts_private_profile_metrics() -> None:
    raw_html = """
    <html><head>
      <meta property="og:title" content="Private User (@private.user) • Instagram" />
      <meta property="og:description" content="137 Followers, 158 Following, 12 Posts" />
      <meta property="og:image" content="https://example.invalid/avatar.jpg" />
    </head><body>@private.user This account is private "is_private":true</body></html>
    """

    profile = ProfilePreviewService._parse_embed_html(raw_html, "private.user")

    assert profile is not None
    assert profile.outcome is PreviewOutcome.ACTIVE
    assert profile.is_private is True
    assert profile.follower_count == 137
    assert profile.following_count == 158
    assert profile.post_count == 12
    assert profile.biography is None


@pytest.mark.asyncio
async def test_private_profile_skips_bio_name_picture_and_badge_deltas() -> None:
    added: list[object] = []
    target = TargetPage(
        id=11,
        instagram_username="private.user",
        user_id=123,
        last_known_status=PageStatus.ACTIVE,
        last_known_id="555",
        status_confirmed=True,
        consecutive_active_checks=2,
        consecutive_deactivated_checks=0,
    )
    owner = User(telegram_id=123)
    owner.admin_report_copy = False
    owner.admin_report_categories = ""
    notification = NotificationSettings(user_id=123, target_page_id=11)
    notification.notify_follower_change = False
    notification.notify_verification_change = True
    notification.follower_report_baseline = 100
    snapshot = PageSnapshot(
        target_page_id=11,
        user_id=123,
        profile_picture_key="/old.jpg",
        profile_picture_url="https://example.invalid/old.jpg",
        full_name="Old Name",
        biography="Old private biography",
        follower_count=100,
        following_count=50,
        post_count=10,
        is_private=True,
        is_verified=True,
    )

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object | None:
            return {
                TargetPage: target,
                User: owner,
                NotificationSettings: notification,
                PageSnapshot: snapshot,
            }.get(model)

        async def scalar(self, _: object) -> TargetPage:
            return target

        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    class Redis:
        async def delete(self, *_: object) -> None:
            return None

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker = InstagramChecker.__new__(InstagramChecker)
    checker.session_factory = Session
    checker.redis = Redis()
    checker.diagnostics = Diagnostics()
    checker.settings = SimpleNamespace(
        deactivation_confirmations=2,
        deactivation_confirmation_delay_seconds=0,
        admin_telegram_id=999,
    )
    checker.fetch_profile = AsyncMock(
        return_value=ProfileResult(
            CheckOutcome.ACTIVE,
            source="http_public_embed",
            metadata_complete=True,
            canonical_username="private.user",
            profile_id="555",
            full_name="New Name",
            biography=None,
            profile_picture_url="https://example.invalid/new.jpg",
            follower_count=100,
            following_count=51,
            post_count=10,
            is_private=True,
            is_verified=False,
            http_status=200,
        )
    )
    checker._notify = AsyncMock()

    await checker._check_target(target.id)

    assert snapshot.biography == "Old private biography"
    assert snapshot.full_name == "New Name"
    outbox = [value for value in added if isinstance(value, NotificationOutbox)]
    assert len(outbox) == 1
    assert outbox[0].category == "CONTENT"
    assert "دنبال‌شونده" in outbox[0].payload_json
    checker._notify.assert_not_awaited()
