"""``client.schedules``: recurring runs (asynchronous)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Iterable, List, Optional

from . import _core as core
from ._schedules import SCHEDULES_PATH, schedule_body
from .exceptions import CleanedWebError
from .models import Run, Schedule, ScheduleDeleted, ScheduleList
from .seen import NewRecords, SeenStore

if TYPE_CHECKING:
    from ._async import AsyncCleanedWeb


class Schedules:
    """Run a catalog endpoint or one of your own APIs on a schedule (``/v1/schedules``).

    Each due slot starts an ordinary run, **charged for every record it delivers**, owned by your
    workspace; read them with ``runs`` or ``new_records``. The key that creates a schedule stands
    behind it and must keep the ``run`` scope (a workspace holds at most 50 schedules).
    """

    def __init__(self, client: AsyncCleanedWeb) -> None:
        self._c = client

    def _url(self, suffix: str = "") -> str:
        return f"{self._c.base_url}{SCHEDULES_PATH}{suffix}"

    async def create(
        self,
        *,
        every: str,
        source_id: Optional[str] = None,
        api_id: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        input: Optional[Dict[str, Any]] = None,
        at_utc: Optional[str] = None,
        weekday: Optional[int] = None,
        max_items: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        fields: Optional[Iterable[str]] = None,
        version: Optional[int] = None,
        run_now: bool = False,
        name: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Schedule:
        """Create a schedule for ``source_id`` (one of your APIs) or ``api_id`` + ``endpoint_id``
        (a catalog API). ``every`` is ``hour``, ``day`` or ``week`` (UTC, not more often than hourly);
        ``at_utc`` is ``"HH:MM"`` (for hourly, the minute past each hour; default ``00:00``);
        ``weekday`` 0 (Monday) to 6 for weekly. ``run_now`` also runs once at the next tick.

        ``idempotency_key`` is generated when omitted and returned as ``schedule.idempotency_key``:
        a retry with the same key and arguments answers the same schedule (``schedule.replayed``)
        and never makes a second one; another request under the same key is a ``409``.
        """
        body = schedule_body(
            source_id=source_id,
            api_id=api_id,
            endpoint_id=endpoint_id,
            input=input,
            every=every,
            at_utc=at_utc,
            weekday=weekday,
            max_items=max_items,
            timeout_seconds=timeout_seconds,
            fields=fields,
            version=version,
            run_now=run_now,
            name=name,
        )
        key = (
            core.validate_idempotency_key(idempotency_key) if idempotency_key else core.new_idempotency_key()
        )
        response, data = await self._c._send("POST", self._url(), auth=True, json=body, idempotency_key=key)
        replayed = response.headers.get("Idempotent-Replayed", "").lower() == "true"
        return Schedule.from_dict(_object(data), replayed=replayed, idempotency_key=key)

    async def list(self) -> ScheduleList:
        """Your schedules, paused ones included (scope ``read``)."""
        return ScheduleList.from_dict(_object(await self._c._get_json(self._url(), auth=True)))

    async def get(self, schedule_id: str) -> Schedule:
        return Schedule.from_dict(
            _object(await self._c._get_json(self._url(f"/{core.seg(schedule_id)}"), auth=True))
        )

    async def pause(self, schedule_id: str) -> Schedule:
        """No slot starts after this answer, except one already being started (``inflight_run_id``)."""
        return await self._post(schedule_id, "pause")

    async def resume(self, schedule_id: str) -> Schedule:
        """Resume a paused schedule from now; slots missed while paused count in ``skipped_slots``."""
        return await self._post(schedule_id, "resume")

    async def delete(self, schedule_id: str) -> ScheduleDeleted:
        _, data = await self._c._send("DELETE", self._url(f"/{core.seg(schedule_id)}"), auth=True)
        return ScheduleDeleted.from_dict(_object(data))

    async def _post(self, schedule_id: str, action: str) -> Schedule:
        _, data = await self._c._send("POST", self._url(f"/{core.seg(schedule_id)}/{action}"), auth=True)
        return Schedule.from_dict(_object(data))

    # -- the runs of a schedule --------------------------------------------------------------

    async def runs(self, schedule_id: str, *, status: Optional[str] = None) -> AsyncIterator[Run]:
        """The runs of the schedule's source, newest first (the server has no per-schedule list, so
        manual runs of the same source are included; ``schedule.last_run_id`` names the latest slot)."""
        source_id = self._source_of(await self.get(schedule_id))
        async for run in self._c.runs.iter(source_id=source_id, status=status):
            yield run

    async def new_records(
        self,
        schedule_id: str,
        store: SeenStore,
        *,
        mark: bool = True,
        max_runs: Optional[int] = None,
        full_scan: bool = False,
    ) -> NewRecords:
        """The records of the schedule's finished runs that ``store`` has not seen, oldest run first.

        Reads the runs newer than the last one a previous call handled (every finished run the first
        time, or the newest ``max_runs``) and returns only unseen records. Every record the runs
        delivered was still charged: this saves your processing, not the bill. Identity is the
        record identity the source contract declares, else a hash of the whole record (see
        :mod:`cleanedweb.seen`). ``mark=False`` leaves the records unmarked until you call
        ``result.commit()``. The scan stops at the first run already handled; ``full_scan=True``
        reads every finished run (for a run that finished after a newer one was read).
        """
        schedule = await self.get(schedule_id)
        source_id = self._source_of(schedule)
        run_ns = f"schedule:{schedule_id}"
        unread: List[Run] = []
        async for run in self._c.runs.iter(source_id=source_id, status="finished"):
            if await asyncio.to_thread(store.run_seen, run_ns, run.run_id):
                if full_scan:
                    continue
                break
            unread.append(run)
            if max_runs is not None and len(unread) >= max_runs:
                break
        unread.reverse()
        records: List[Dict[str, Any]] = []
        for run in unread:
            records.extend([record async for record in self._c.runs.items(run.run_id)])
        version = unread[-1].definition_version if unread else None
        paths = await self._c.sources.identity_paths(source_id, version) if version else ()
        result = await asyncio.to_thread(store.filter_new, source_id, records, paths, mark=False)
        result.run_ids = [r.run_id for r in unread]
        result._run_namespace = run_ns
        if mark:
            await asyncio.to_thread(result.commit)
        return result

    @staticmethod
    def _source_of(schedule: Schedule) -> str:
        if not schedule.source_id:
            raise CleanedWebError(f"schedule {schedule.schedule_id} names no source_id to read runs of")
        return schedule.source_id


def _object(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise CleanedWebError("the answer was not a JSON object")
    return data
