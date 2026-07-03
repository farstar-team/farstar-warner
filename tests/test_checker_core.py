from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect

from bot.checker import CheckOutcome, CurlResponse, InstagramChecker, ProfileResult
from bot.config import Settings
from bot.credential_store import CredentialStore
from bot.models import (
    Base,
    NotificationSettings,
    PageSnapshot,
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


def test_public_parser_reads_only_root_user_node() -> None:
    body = json.dumps(
        {
            "data": {
                "user": {
                    "id": "47796612144",
                    "username": "mahdy.security",
                    "full_name": "Mahdy",
                    "biography": "security",
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
        InstagramChecker._parse_graphql_search_response(failed, "target", 200)
        is None
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
async def test_official_health_uses_bearer_header_without_token_in_url() -> None:
    token = "EAAB-secret-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert token not in str(request.url)
        assert request.url.params["fields"] == "id,username"
        return httpx.Response(
            200,
            json={"id": "17841400000000000", "username": "admin.account"},
        )

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

    assert healthy is True
    assert username == "admin.account"


@pytest.mark.asyncio
async def test_confirmed_active_to_deactivated_transition_notifies_owner() -> None:
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

        def add(self, _: object) -> None:
            return None

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
    checker._notify.assert_awaited_once()


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
async def test_three_profile_baseline_accepts_one_verified_active_result() -> None:
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
        async def delete(self, *_: object) -> None:
            return None

        async def set(self, *_: object, **__: object) -> bool:
            return True

    class Diagnostics:
        async def add(self, **_: object) -> None:
            return None

    checker.redis = Redis()
    checker.diagnostics = Diagnostics()
    checker._notify = AsyncMock()
    checker.settings = SimpleNamespace(
        admin_telegram_id=999,
        baseline_usernames=("instagram", "cristiano", "nasa"),
    )

    assert await checker.reference_profile_preflight() is True
    assert checker.fetch_profile.await_count == 3
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

        def add(self, _: object) -> None:
            return None

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
    checker._notify.assert_awaited_once()
    notification_payload = checker._notify.await_args.args[1]
    assert notification_payload.category == "CONTENT"
    assert "دنبال‌شونده" in notification_payload.message
