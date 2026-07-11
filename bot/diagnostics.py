from __future__ import annotations

import json
import logging
import hashlib
import traceback
from asyncio import AbstractEventLoop, CancelledError, Task
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis


@dataclass(slots=True, frozen=True)
class DiagnosticEntry:
    timestamp: str
    level: str
    event: str
    message: str
    trace_id: str | None = None
    username: str | None = None
    transport: str | None = None
    http_status: int | None = None
    return_code: int | None = None
    elapsed_ms: int | None = None
    response_bytes: int | None = None
    detail: str | None = None


class DiagnosticStore:
    KEY = "farstar:diagnostics:entries:v1"
    MAX_ENTRIES = 2000
    DEDUP_PREFIX = "farstar:diagnostics:dedup:"
    DEDUP_SECONDS = 60
    NOISY_EVENTS = {
        "browser_fallback_result",
        "graphql_discovery_attempt",
        "instagram_access_denied",
        "profile_request_started",
        "profile_request_succeeded",
        "python_log",
        "target_result",
        "target_state_synchronized",
        "transport_attempt",
        "web_profile_routes_blocked",
    }

    def __init__(self, redis: Redis, redactions: tuple[str, ...] = ()) -> None:
        self.redis = redis
        self.redactions = tuple(value for value in redactions if value)

    async def add(
        self,
        *,
        level: str,
        event: str,
        message: str,
        trace_id: str | None = None,
        username: str | None = None,
        transport: str | None = None,
        http_status: int | None = None,
        return_code: int | None = None,
        elapsed_ms: int | None = None,
        response_bytes: int | None = None,
        detail: str | None = None,
    ) -> None:
        entry = DiagnosticEntry(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            level=self._clean(level, 16) or "INFO",
            event=self._clean(event, 64) or "unknown",
            message=self._clean(message, 300) or "بدون توضیح",
            trace_id=self._clean(trace_id, 32),
            username=self._clean(username, 64),
            transport=self._clean(transport, 64),
            http_status=http_status,
            return_code=return_code,
            elapsed_ms=elapsed_ms,
            response_bytes=response_bytes,
            detail=self._clean(detail, 700),
        )
        if entry.event in self.NOISY_EVENTS:
            routine_info = entry.level == "INFO" and entry.event in {
                "profile_request_started",
                "profile_request_succeeded",
                "target_result",
                "target_state_synchronized",
                "transport_attempt",
            }
            fingerprint = "|".join(
                (
                    entry.event,
                    "routine-sample" if routine_info else entry.username or "",
                    entry.transport or "",
                    str(entry.http_status or 0),
                    entry.message,
                    entry.detail or "",
                )
            )
            digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
            first = await self.redis.set(
                f"{self.DEDUP_PREFIX}{digest}",
                "1",
                ex=(self.DEDUP_SECONDS if routine_info else 900),
                nx=True,
            )
            if not first:
                return
        encoded = json.dumps(asdict(entry), ensure_ascii=False, separators=(",", ":"))
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.lpush(self.KEY, encoded)
        pipeline.ltrim(self.KEY, 0, self.MAX_ENTRIES - 1)
        await pipeline.execute()

    async def latest(self, limit: int = 20) -> list[DiagnosticEntry]:
        safe_limit = max(1, min(limit, self.MAX_ENTRIES))
        raw_entries = await self.redis.lrange(self.KEY, 0, safe_limit - 1)
        entries: list[DiagnosticEntry] = []
        for raw in raw_entries:
            try:
                payload: Any = json.loads(raw)
                if isinstance(payload, dict):
                    entries.append(DiagnosticEntry(**payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return entries

    async def count(self) -> int:
        return int(await self.redis.llen(self.KEY))

    async def clear(self) -> int:
        return int(await self.redis.delete(self.KEY))

    def _clean(self, value: object, limit: int) -> str | None:
        if value is None:
            return None
        text = str(value).replace("\x00", "").strip()
        for secret in self.redactions:
            text = text.replace(secret, "[REDACTED]")
        if not text:
            return None
        return text[:limit]


class RedisDiagnosticLogHandler(logging.Handler):
    def __init__(
        self,
        store: DiagnosticStore,
        loop: AbstractEventLoop,
    ) -> None:
        super().__init__(level=logging.WARNING)
        self.store = store
        self.loop = loop
        self._tasks: set[Task[None]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            detail = f"logger={record.name}; source={record.pathname}:{record.lineno}"
            if record.exc_info:
                exception_text = "".join(traceback.format_exception(*record.exc_info))
                detail = f"{detail}\n{exception_text}"
            task = self.loop.create_task(
                self.store.add(
                    level=record.levelname,
                    event="python_log",
                    message=message,
                    detail=detail,
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._task_done)
        except Exception:
            self.handleError(record)

    def _task_done(self, task: Task[None]) -> None:
        self._tasks.discard(task)
        with suppress(CancelledError, Exception):
            task.exception()

    async def drain(self) -> None:
        if not self._tasks:
            return
        tasks = tuple(self._tasks)
        for task in tasks:
            with suppress(CancelledError, Exception):
                await task
