"""Request bodies and state reading for building an API from a URL (no I/O).

The routes are the key-authorized builder routes of the console origin (``app_url``):

* ``POST /api/builder/previews`` ``{url}`` then ``GET /api/builder/previews/{draft_id}`` until the
  inspection ends;
* ``POST /api/builder/previews/{draft_id}/create`` ``{}`` (the router's choice) and
  ``GET .../create?wait=`` until it names a ``job_id``;
* ``GET /api/builder/builds/{job_id}?wait=`` until the job ends, then
  ``GET /api/builder/builds/{job_id}/result`` for ``source_id``, ``version`` and ``schema``.

The create request is sent once per draft; waiting only reads.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .exceptions import BuildFailedError
from .models import Build, Draft

#: ``inspection.status`` values that mean the page is still being read.
INSPECTING = frozenset({None, "", "queued", "running", "inspecting"})
#: ``inspection.status`` values that end the build before it starts.
INSPECTION_FAILED = frozenset({"unsupported", "needs_scope", "failed"})
#: Longest the server holds a status read (``?wait=``, at most 60 seconds).
HOLD_SECONDS = 30
#: Seconds between reads while waiting; the server holds the read itself where ``wait`` applies.
MIN_BUILD_POLL_SECONDS = 1.0
INSPECTION_POLL_FIRST = 2.0
INSPECTION_POLL_CEILING = 15.0

BUILDER = "/api/builder"


def preview_body(
    url: str,
    *,
    want: Optional[str] = None,
    page_kind: Optional[str] = None,
    country: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    body: Dict[str, Any] = {"url": url.strip()}
    for key, value in (("want", want), ("page_kind", page_kind), ("country", country)):
        if value:
            body[key] = value
    return body


def failure_parts(
    failure: Optional[Dict[str, Any]], fallback: str
) -> Tuple[str, Optional[str], Optional[str]]:
    """``(code, reason, stage)`` of a typed failure object (draft ``failure`` or job ``failure``)."""
    data = failure or {}
    code = data.get("code") or data.get("reason") or fallback
    reason = data.get("reason")
    stage = data.get("stage") or data.get("step")
    return (
        str(code),
        str(reason) if isinstance(reason, str) and reason != code else None,
        str(stage) if isinstance(stage, str) else None,
    )


def inspection_error(draft: Draft) -> Optional[BuildFailedError]:
    """The error of an inspection that ended without a buildable page, or ``None``."""
    status = draft.inspection_status
    if draft.failure is not None and status in INSPECTING:
        code, reason, stage = failure_parts(draft.failure, "inspection_failed")
        return BuildFailedError(
            code,
            reason=reason,
            stage=stage or "inspection",
            draft_id=draft.draft_id,
            build=Build(draft_id=draft.draft_id, state="failed", failure=draft.failure),
        )
    if status in INSPECTION_FAILED:
        inner = draft.inspection.get("failure")
        failure = inner if isinstance(inner, dict) else None
        code, reason, stage = failure_parts(failure, str(status))
        return BuildFailedError(
            code,
            reason=reason or (str(status) if code != status else None),
            stage=stage or "inspection",
            draft_id=draft.draft_id,
            build=Build(draft_id=draft.draft_id, state="failed", failure=failure or {"code": code}),
        )
    return None


def job_error(build: Build) -> BuildFailedError:
    """The error of a job that ended ``failed`` or ``cancelled``."""
    code, reason, stage = failure_parts(build.failure, build.error_code or build.state)
    if build.failure is None and build.error_code:
        code = build.error_code
    return BuildFailedError(
        code,
        reason=reason,
        stage=stage or ("cancelled" if build.state == "cancelled" else "build"),
        draft_id=build.draft_id,
        job_id=build.job_id,
        build=build,
    )


def create_job_id(status: Dict[str, Any]) -> Optional[str]:
    value = status.get("job_id")
    return value if isinstance(value, str) and value else None


def create_refusal(status: Dict[str, Any], draft: Optional[Draft]) -> BuildFailedError:
    """An idle create with no job: the creation stopped; its reason is the draft's failure or
    the status's ``refused``."""
    refused = status.get("refused")
    failure: Optional[Dict[str, Any]] = None
    if draft is not None and draft.failure is not None:
        failure = draft.failure
    elif isinstance(refused, dict):
        failure = refused
    elif isinstance(refused, str):
        failure = {"code": refused}
    code, reason, stage = failure_parts(failure, "build_not_started")
    draft_id = draft.draft_id if draft is not None else None
    return BuildFailedError(
        code,
        reason=reason,
        stage=stage or "create",
        draft_id=draft_id,
        build=Build(draft_id=draft_id, state="failed", failure=failure or {"code": code}),
    )
