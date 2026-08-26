from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import AuthenticationError, SyncTokenExpiredError
from .logging_config import log_event
from .retry import retry_call

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded")


@dataclass
class EventsPage:
    items: list
    next_page_token: Optional[str]
    next_sync_token: Optional[str]


class CalendarClient(ABC):
    """Abstraction over the Google Calendar API so the sync engine can be tested without it."""

    @abstractmethod
    def list_calendars(self) -> list[dict]: ...

    @abstractmethod
    def list_events(
        self, calendar_id: str, sync_token: Optional[str] = None, page_token: Optional[str] = None
    ) -> EventsPage: ...

    @abstractmethod
    def insert_event(self, calendar_id: str, body: dict) -> dict: ...

    @abstractmethod
    def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict: ...

    @abstractmethod
    def delete_event(self, calendar_id: str, event_id: str) -> None: ...


def _http_status(exc: HttpError) -> Optional[int]:
    return exc.resp.status if exc.resp is not None else None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if not isinstance(exc, HttpError):
        return False
    status = _http_status(exc)
    if status in _RETRYABLE_STATUSES:
        return True
    if status == 403 and any(reason in str(exc) for reason in _RATE_LIMIT_REASONS):
        return True
    return False


class GoogleCalendarClient(CalendarClient):
    """Real Google Calendar API client with retry/backoff and error classification."""

    def __init__(self, credentials, account_label: str, max_attempts: int = 5):
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._account_label = account_label
        self._max_attempts = max_attempts

    def _invoke(self, fn, operation: str):
        def on_retry(attempt, exc, delay):
            log_event(
                logger,
                "retry",
                level="warning",
                account=self._account_label,
                operation=operation,
                attempt=attempt,
                delay_seconds=round(delay, 2),
            )

        try:
            return retry_call(
                fn,
                max_attempts=self._max_attempts,
                is_retryable=_is_retryable,
                on_retry=on_retry,
            )
        except HttpError as exc:
            status = _http_status(exc)
            if status == 410:
                raise SyncTokenExpiredError(str(exc)) from exc
            if status == 401:
                log_event(logger, "authentication_error", level="error", account=self._account_label, operation=operation)
                raise AuthenticationError(str(exc)) from exc
            log_event(
                logger,
                "api_error",
                level="error",
                account=self._account_label,
                operation=operation,
                status=status,
            )
            raise

    def list_calendars(self) -> list[dict]:
        result = self._invoke(lambda: self._service.calendarList().list().execute(), "list_calendars")
        return result.get("items", [])

    def list_events(self, calendar_id, sync_token=None, page_token=None) -> EventsPage:
        params = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 250,
        }
        if sync_token:
            params["syncToken"] = sync_token
        if page_token:
            params["pageToken"] = page_token

        result = self._invoke(lambda: self._service.events().list(**params).execute(), "list_events")
        return EventsPage(
            items=result.get("items", []),
            next_page_token=result.get("nextPageToken"),
            next_sync_token=result.get("nextSyncToken"),
        )

    def insert_event(self, calendar_id, body) -> dict:
        return self._invoke(
            lambda: self._service.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates="none")
            .execute(),
            "insert_event",
        )

    def patch_event(self, calendar_id, event_id, body) -> dict:
        return self._invoke(
            lambda: self._service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body, sendUpdates="none")
            .execute(),
            "patch_event",
        )

    def delete_event(self, calendar_id, event_id) -> None:
        def op():
            try:
                self._service.events().delete(
                    calendarId=calendar_id, eventId=event_id, sendUpdates="none"
                ).execute()
            except HttpError as exc:
                if _http_status(exc) in (404, 410):
                    return  # already gone: deletion is idempotent
                raise

        self._invoke(op, "delete_event")
