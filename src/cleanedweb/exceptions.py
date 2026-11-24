"""Exceptions raised by the CleanedWeb clients.

Every error derives from :class:`CleanedWebError`. Errors that come from an HTTP answer derive
from :class:`APIStatusError` and carry the status, the documented ``code`` and the parsed body.

Deciding what to do after an error that belongs to a run:

* ``err.run_failed`` is ``True``: the run failed and the failure is final for that idempotency
  key (the same answer is replayed for 7 days). Use a **new** key to run again.
* A connection error, a timeout or a gateway error without that marker proves nothing: the run may
  have started. Keep the idempotency key and read the result with it (``client.catalog.result``)
  before you decide.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "APIConnectionError",
    "APIStatusError",
    "AuthenticationError",
    "BuildFailedError",
    "CleanedWebError",
    "IdempotencyConflictError",
    "InsufficientCreditError",
    "InvalidRequestError",
    "MissingAPIKeyError",
    "NotFoundError",
    "PermissionDeniedError",
    "PreviewLimitError",
    "RateLimitError",
    "RunFailedError",
    "ScopeRequiredError",
    "ServerError",
    "UsageBudgetExceededError",
    "VersionConflictError",
    "VersionNotFoundError",
    "error_from_response",
]

_RUN_FAILED_MARKERS = ("run_failed", "failed_run_replayed")


class CleanedWebError(Exception):
    """Base class of every error raised by this package."""


class MissingAPIKeyError(CleanedWebError):
    """A call that needs a key was made without one: pass ``api_key=`` or set
    ``CLEANEDWEB_API_KEY``. Catalog reads (``list``, ``get``, ``find``) need no key."""


class APIConnectionError(CleanedWebError):
    """The request did not get an answer (timeout, lost connection, DNS, TLS).

    The outcome is unknown: for a run, it may have started. ``idempotency_key`` is set when the
    request carried one; reuse it to recover (see ``client.catalog.result``).
    """

    def __init__(self, message: str, *, idempotency_key: Optional[str] = None) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key


class APIStatusError(CleanedWebError):
    """The API answered with an error status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: Optional[str] = None,
        body: Any = None,
        detail: Any = None,
        headers: Optional[Dict[str, str]] = None,
        retry_after: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.body = body
        self.detail = detail
        self.headers = headers or {}
        self.retry_after = retry_after
        self.idempotency_key = idempotency_key

    def __str__(self) -> str:
        parts = [f"HTTP {self.status_code}"]
        if self.code:
            parts.append(self.code)
        text = f"{' '.join(parts)}: {self.message}" if self.message else " ".join(parts)
        if self.run_id:
            text += f" (run {self.run_id})"
        return text

    @property
    def detail_dict(self) -> Dict[str, Any]:
        return self.detail if isinstance(self.detail, dict) else {}

    @property
    def run_id(self) -> Optional[str]:
        """The failed run, when the error is a failed run's."""
        value = self.detail_dict.get("run_id")
        return value if isinstance(value, str) else None

    @property
    def error_code(self) -> Optional[str]:
        """``run_failed`` or ``failed_run_replayed`` when the error marks a failed run."""
        value = self.detail_dict.get("error_code")
        return value if value in _RUN_FAILED_MARKERS else None

    @property
    def run_failed(self) -> bool:
        """True when the run failed: final for the idempotency key, use a new key to run again."""
        return self.error_code is not None

    @property
    def replayed(self) -> bool:
        return bool(self.detail_dict.get("replayed"))

    @property
    def unsure(self) -> bool:
        """True when the error does not show whether the run failed (gateway error, timeout)."""
        return not self.run_failed and (self.status_code >= 500 or self.status_code == 408)


class AuthenticationError(APIStatusError):
    """401: no credential, or a key that is invalid, expired or revoked."""


class PermissionDeniedError(APIStatusError):
    """403: the key lacks the scope or the access, or is not tied to a workspace account."""


class ScopeRequiredError(PermissionDeniedError):
    """403 ``api_key_scope_required``: the key lacks a scope the route needs.

    Building an API from a URL needs a personal key with the creation scopes ``discover`` and
    ``build`` (the console's "Create APIs" option); a plain ``run`` key cannot build.
    ``required_scopes`` names what the route needs, ``granted_scopes`` what the key has and
    ``missing_scopes`` the difference.
    """

    @property
    def required_scopes(self) -> List[str]:
        value = self.detail_dict.get("required_scopes")
        if isinstance(value, list):
            return [str(v) for v in value]
        one = self.detail_dict.get("required_scope")
        return [one] if isinstance(one, str) else []

    @property
    def granted_scopes(self) -> List[str]:
        value = self.detail_dict.get("granted_scopes")
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def missing_scopes(self) -> List[str]:
        granted = set(self.granted_scopes)
        return [s for s in self.required_scopes if s not in granted]


class NotFoundError(APIStatusError):
    """404: API, endpoint, run or delivery not found (or not accessible)."""


class VersionNotFoundError(NotFoundError):
    """404 ``catalog_version_not_found``: the pinned major never existed. See ``available``."""

    @property
    def available(self) -> List[int]:
        value = self.detail_dict.get("available")
        return [v for v in value if isinstance(v, int)] if isinstance(value, list) else []


class InsufficientCreditError(APIStatusError):
    """402 ``insufficient_units``: the balance cannot fund the run."""

    @property
    def required(self) -> Any:
        return self.detail_dict.get("required")

    @property
    def available(self) -> Any:
        return self.detail_dict.get("available")


class UsageBudgetExceededError(APIStatusError):
    """409 ``usage_budget_exceeded``: the workspace's monthly usage budget cannot cover it."""


class VersionConflictError(APIStatusError):
    """409 ``builder_definition_version_conflict``: the active definition differs from the
    version the request required (``expected_version``). Review the active contract; do not
    silently adopt the new version on a saved request."""


class IdempotencyConflictError(APIStatusError):
    """409 ``delivery_identity_conflict``: the key belongs to a different endpoint, version or
    request. Use a new key for a new request."""


class InvalidRequestError(APIStatusError):
    """400, 415 or 422: the request is invalid (input, run option, idempotency key, fields).
    Correct it; do not retry unchanged."""


class RateLimitError(APIStatusError):
    """429: respect ``retry_after`` (seconds) when present and reduce request frequency."""


class PreviewLimitError(RateLimitError):
    """429 ``preview_limit``: the workspace or network used its previews for the hour.

    ``cause`` says which check refused (``owner_hourly``, ``network_hourly``, ``guest_hourly``,
    ``guest_slots_full``), ``limit`` the count and ``retry_after`` when a preview frees up.
    Nothing was created: wait ``retry_after`` seconds and send the same request again.
    """

    @property
    def cause(self) -> Optional[str]:
        value = self.detail_dict.get("cause")
        return value if isinstance(value, str) else None

    @property
    def limit(self) -> Optional[int]:
        value = self.detail_dict.get("limit")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def window_seconds(self) -> Optional[int]:
        value = self.detail_dict.get("window_seconds")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class ServerError(APIStatusError):
    """5xx. Without a failed-run marker the outcome is unknown; keep the idempotency key."""


class RunFailedError(APIStatusError):
    """An error that marks a failed run and has no more specific class. Final for the key."""


class BuildFailedError(CleanedWebError):
    """Building an API ended without one: the build failed or was cancelled, or the page was
    not inspectable.

    ``code`` is the stable reason code, ``reason`` the specific code-only reason and ``stage`` where
    it stopped (``inspection``, ``create`` or the build's own stage). ``draft_id`` and ``job_id`` are
    the handles; ``build`` is the last :class:`~cleanedweb.models.Build` state. Building again
    from the same URL starts a new preview (and counts against the preview limit).
    """

    def __init__(
        self,
        code: str,
        *,
        reason: Optional[str] = None,
        stage: Optional[str] = None,
        draft_id: Optional[str] = None,
        job_id: Optional[str] = None,
        build: Any = None,
    ) -> None:
        text = f"build failed: {code}"
        if reason and reason != code:
            text += f" ({reason})"
        if stage:
            text += f" at {stage}"
        super().__init__(text)
        self.code = code
        self.reason = reason
        self.stage = stage
        self.draft_id = draft_id
        self.job_id = job_id
        self.build = build


def _detail_and_code(body: Any) -> tuple[Any, Optional[str], str]:
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail is None and isinstance(body, dict):
        detail = body
    code: Optional[str] = None
    message = ""
    if isinstance(detail, dict):
        raw_code = detail.get("code") or detail.get("error_code")
        code = raw_code if isinstance(raw_code, str) else None
        raw_message = detail.get("message")
        message = raw_message if isinstance(raw_message, str) else ""
    elif isinstance(detail, str):
        message = detail
    elif isinstance(detail, list) and detail:
        message = "; ".join(
            str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail[:3]
        )
    return detail, code, message


def _parse_retry_after(headers: Dict[str, str], detail: Any) -> Optional[float]:
    raw = headers.get("retry-after")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass  # an HTTP date: not worth parsing here
    if isinstance(detail, dict):
        for key in ("retry_after_seconds", "retryAfterSeconds", "retry_after"):
            value = detail.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, float(value))
    return None


def error_from_response(
    status_code: int,
    body: Any,
    headers: Dict[str, str],
    *,
    idempotency_key: Optional[str] = None,
) -> APIStatusError:
    """Map an error answer to the typed exception the documentation describes."""
    headers = {k.lower(): v for k, v in headers.items()}
    detail, code, message = _detail_and_code(body)
    if not message and not isinstance(body, (dict, list)) and body:
        message = str(body)[:300]
    retry_after = _parse_retry_after(headers, detail)
    kwargs: Dict[str, Any] = dict(
        status_code=status_code,
        code=code,
        body=body,
        detail=detail,
        headers=headers,
        retry_after=retry_after,
        idempotency_key=idempotency_key,
    )
    cls: type = APIStatusError
    if status_code == 401:
        cls = AuthenticationError
    elif status_code == 403:
        cls = ScopeRequiredError if code == "api_key_scope_required" else PermissionDeniedError
    elif status_code == 402 or code == "insufficient_units":
        cls = InsufficientCreditError
    elif status_code == 404:
        cls = VersionNotFoundError if code == "catalog_version_not_found" else NotFoundError
    elif status_code == 409:
        if code == "usage_budget_exceeded":
            cls = UsageBudgetExceededError
        elif code == "delivery_identity_conflict":
            cls = IdempotencyConflictError
        elif code is not None and code.startswith("builder_definition_version"):
            cls = VersionConflictError
    elif status_code == 429:
        cls = PreviewLimitError if code == "preview_limit" else RateLimitError
    elif status_code in (400, 415, 422):
        cls = InvalidRequestError
    elif status_code >= 500:
        cls = ServerError
    error: APIStatusError = cls(message, **kwargs)
    if cls is APIStatusError and error.run_failed:
        error = RunFailedError(message, **kwargs)
    return error
