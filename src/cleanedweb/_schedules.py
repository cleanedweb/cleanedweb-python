"""Request bodies for ``/v1/schedules`` (no I/O)."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

EVERY = ("hour", "day", "week")
SCHEDULES_PATH = "/v1/schedules"
_AT_UTC = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def schedule_body(
    *,
    source_id: Optional[str],
    api_id: Optional[str],
    endpoint_id: Optional[str],
    input: Optional[Dict[str, Any]],
    every: str,
    at_utc: Optional[str],
    weekday: Optional[int],
    max_items: Optional[int],
    timeout_seconds: Optional[int],
    fields: Optional[Iterable[str]],
    version: Optional[int],
    run_now: bool,
    name: Optional[str],
) -> Dict[str, Any]:
    """The create body, checked the way the server checks it (the server stays the authority)."""
    has_catalog = bool(api_id or endpoint_id)
    if bool(source_id) == has_catalog or (has_catalog and not (api_id and endpoint_id)):
        raise ValueError("give api_id and endpoint_id, or source_id, not both")
    if every not in EVERY:
        raise ValueError("every must be 'hour', 'day' or 'week'")
    if at_utc is not None and not _AT_UTC.match(at_utc):
        raise ValueError("at_utc must be a UTC time 'HH:MM'")
    if weekday is not None and (
        isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6
    ):
        raise ValueError("weekday must be 0 (Monday) to 6 (Sunday)")
    if max_items is not None and (
        isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1
    ):
        raise ValueError("max_items must be an integer >= 1")
    body: Dict[str, Any] = {"every": every}
    for key, value in (
        ("source_id", source_id),
        ("api_id", api_id),
        ("endpoint_id", endpoint_id),
        ("at_utc", at_utc),
        ("name", name),
    ):
        if value:
            body[key] = value
    if input is not None:
        body["input"] = dict(input)
    if fields is not None:
        body["fields"] = list(fields)
    for key, number in (
        ("version", version),
        ("max_items", max_items),
        ("timeout_seconds", timeout_seconds),
        ("weekday", weekday),
    ):
        if number is not None:
            body[key] = number
    if run_now:
        body["run_now"] = True
    return body
