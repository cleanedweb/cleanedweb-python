"""``schedules``: create, list, get, pause, resume, delete, runs and only-new records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
import respx

from cleanedweb import (
    AsyncCleanedWeb,
    CleanedWeb,
    CleanedWebError,
    InvalidRequestError,
    NotFoundError,
    SeenStore,
)
from cleanedweb.exceptions import APIStatusError

from .conftest import API_ID, API_ORIGIN, ENDPOINT_ID, SOURCE_ID, VERSION

SCHEDULES = f"{API_ORIGIN}/v1/schedules"
RUNS = f"{API_ORIGIN}/v1/runs"
SID = "sch_0123456789abcdef"
RUN1 = "1" * 32
RUN2 = "2" * 32
RUN3 = "3" * 32
CONTRACT_URL = f"{API_ORIGIN}/v1/sources/{SOURCE_ID}/versions/{VERSION}/contract"


def schedule_body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "schedule_id": SID,
        "name": "Daily jobs",
        "state": "active",
        "pause_reason": None,
        "target": {"source_id": SOURCE_ID},
        "source_id": SOURCE_ID,
        "pinned_version": VERSION,
        "input": {"query": "python"},
        "fields": None,
        "max_items": 100,
        "timeout_seconds": 10800,
        "cadence": {"every": "day", "at_utc": "06:30", "weekday": None},
        "next_due_at": "2026-11-25T06:30:00Z",
        "last_slot_at": None,
        "last_run_id": None,
        "last_status": None,
        "consecutive_failures": 0,
        "skipped_slots": 0,
        "attention": [],
        "created_at": "2026-11-24T10:00:00Z",
    }
    body.update(overrides)
    return body


@pytest.fixture
def cw() -> Any:
    with CleanedWeb(api_key="secret-key") as client:
        yield client


@respx.mock
def test_create_sends_the_body_and_key_and_reads_the_schedule(cw: CleanedWeb) -> None:
    route = respx.post(SCHEDULES).mock(return_value=httpx.Response(201, json=schedule_body()))
    schedule = cw.schedules.create(
        source_id=SOURCE_ID,
        input={"query": "python"},
        every="day",
        at_utc="06:30",
        max_items=100,
        run_now=True,
        name="Daily jobs",
        idempotency_key="daily-jobs-1",
    )
    request = route.calls[0].request
    assert request.headers["idempotency-key"] == "daily-jobs-1"
    assert request.headers["authorization"] == "Bearer secret-key"
    assert json.loads(request.content) == {
        "source_id": SOURCE_ID,
        "input": {"query": "python"},
        "every": "day",
        "at_utc": "06:30",
        "max_items": 100,
        "run_now": True,
        "name": "Daily jobs",
    }
    assert schedule.schedule_id == SID and schedule.state == "active" and not schedule.paused
    assert schedule.replayed is False and schedule.idempotency_key == "daily-jobs-1"
    assert schedule.cadence["at_utc"] == "06:30" and schedule.max_items == 100
    assert schedule.source_id == SOURCE_ID and schedule.next_due_at == "2026-11-25T06:30:00Z"


@respx.mock
def test_create_generates_a_key_and_flags_a_replay(cw: CleanedWeb) -> None:
    route = respx.post(SCHEDULES).mock(
        return_value=httpx.Response(200, headers={"Idempotent-Replayed": "true"}, json=schedule_body())
    )
    schedule = cw.schedules.create(api_id=API_ID, endpoint_id=ENDPOINT_ID, every="week", weekday=0, version=2)
    assert schedule.replayed is True
    key = route.calls[0].request.headers["idempotency-key"]
    assert key and schedule.idempotency_key == key
    body = json.loads(route.calls[0].request.content)
    assert body == {"api_id": API_ID, "endpoint_id": ENDPOINT_ID, "every": "week", "weekday": 0, "version": 2}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"every": "day"},
        {"every": "day", "source_id": "s", "api_id": "a", "endpoint_id": "e"},
        {"every": "day", "api_id": "a"},
        {"every": "month", "source_id": "s"},
        {"every": "day", "source_id": "s", "at_utc": "25:00"},
        {"every": "week", "source_id": "s", "weekday": 7},
        {"every": "day", "source_id": "s", "max_items": 0},
    ],
)
def test_create_rejects_a_malformed_request_before_sending(cw: CleanedWeb, kwargs: Dict[str, Any]) -> None:
    with respx.mock, pytest.raises(ValueError):
        cw.schedules.create(**kwargs)


@respx.mock
def test_create_conflict_and_validation_errors_are_typed(cw: CleanedWeb) -> None:
    respx.post(SCHEDULES).mock(
        side_effect=[
            httpx.Response(
                409, json={"detail": {"code": "schedule_idempotency_conflict", "message": "other"}}
            ),
            httpx.Response(422, json={"detail": {"code": "schedule_input_invalid", "message": "bad"}}),
        ]
    )
    with pytest.raises(APIStatusError) as conflict:
        cw.schedules.create(source_id=SOURCE_ID, every="day", idempotency_key="k")
    assert conflict.value.status_code == 409 and conflict.value.code == "schedule_idempotency_conflict"
    with pytest.raises(InvalidRequestError):
        cw.schedules.create(source_id=SOURCE_ID, every="day")


@respx.mock
def test_list_get_pause_resume_delete(cw: CleanedWeb) -> None:
    respx.get(SCHEDULES).mock(
        return_value=httpx.Response(
            200, json={"schedules": [schedule_body(), schedule_body(schedule_id="b")], "limit": 50}
        )
    )
    listed = cw.schedules.list()
    assert [s.schedule_id for s in listed] == [SID, "b"] and listed.limit == 50 and len(listed) == 2
    respx.get(f"{SCHEDULES}/{SID}").mock(
        return_value=httpx.Response(200, json=schedule_body(last_run_id=RUN1))
    )
    assert cw.schedules.get(SID).last_run_id == RUN1
    pause = respx.post(f"{SCHEDULES}/{SID}/pause").mock(
        return_value=httpx.Response(
            200, json=schedule_body(state="paused", pause_reason="paused_by_customer", inflight_run_id=RUN2)
        )
    )
    paused = cw.schedules.pause(SID)
    assert paused.paused and paused.pause_reason == "paused_by_customer" and paused.inflight_run_id == RUN2
    assert pause.calls[0].request.headers["authorization"] == "Bearer secret-key"
    respx.post(f"{SCHEDULES}/{SID}/resume").mock(
        return_value=httpx.Response(200, json=schedule_body(skipped_slots=2))
    )
    assert cw.schedules.resume(SID).skipped_slots == 2
    respx.delete(f"{SCHEDULES}/{SID}").mock(
        return_value=httpx.Response(200, json={"schedule_id": SID, "deleted": True, "inflight_run_id": None})
    )
    deleted = cw.schedules.delete(SID)
    assert deleted.deleted and deleted.inflight_run_id is None
    respx.get(f"{SCHEDULES}/nope").mock(
        return_value=httpx.Response(404, json={"detail": {"code": "schedule_not_found", "message": "no"}})
    )
    with pytest.raises(NotFoundError):
        cw.schedules.get("nope")


# -- runs and only-new records ------------------------------------------------------------------


def run_row(run_id: str, status: str = "finished", version: str = VERSION) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "status": status,
        "source_id": SOURCE_ID,
        "definition_version": version,
        "created_at": "2026-11-24T06:30:00Z",
        "finished_at": "2026-11-24T06:31:00Z",
    }


def job(title: str, url: str) -> Dict[str, Any]:
    return {"title": title, "url": url, "posted": "today"}


CONTRACT = {
    "source_id": SOURCE_ID,
    "definition_version": VERSION,
    "openapi": {"x-cleanedweb-contract": {"version": 1, "record_identity_path": "url"}},
    "request": {},
}


class Server:
    """A run list that grows between calls, with per-run items."""

    def __init__(self) -> None:
        self.runs: List[Dict[str, Any]] = []
        self.items: Dict[str, List[Dict[str, Any]]] = {}
        self.list_params: List[Dict[str, str]] = []

    def install(self) -> Dict[str, respx.Route]:
        routes: Dict[str, respx.Route] = {}
        routes["schedule"] = respx.get(f"{SCHEDULES}/{SID}").mock(
            return_value=httpx.Response(200, json=schedule_body())
        )
        routes["contract"] = respx.get(CONTRACT_URL).mock(return_value=httpx.Response(200, json=CONTRACT))

        def runs_list(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            self.list_params.append(params)
            rows = [r for r in self.runs if params.get("status") in (None, r["status"])]
            return httpx.Response(200, json={"runs": rows, "next_cursor": None})

        routes["runs"] = respx.get(RUNS).mock(side_effect=runs_list)
        for run_id in (RUN1, RUN2, RUN3):
            routes[run_id] = respx.get(f"{RUNS}/{run_id}/items").mock(
                side_effect=lambda request, run_id=run_id: httpx.Response(
                    200, json={"items": self.items.get(run_id, []), "next_cursor": None}
                )
            )
        return routes


@respx.mock
def test_runs_lists_the_schedules_source_runs(cw: CleanedWeb) -> None:
    server = Server()
    server.runs = [run_row(RUN2), run_row(RUN1, "failed")]
    server.install()
    assert [r.run_id for r in cw.schedules.runs(SID)] == [RUN2, RUN1]
    assert server.list_params[-1]["source_id"] == SOURCE_ID
    assert [r.run_id for r in cw.schedules.runs(SID, status="finished")] == [RUN2]
    run = next(iter(cw.schedules.runs(SID)))
    assert run.source_id == SOURCE_ID and run.definition_version == VERSION and run.finished_at


@respx.mock
def test_new_records_across_two_runs_returns_only_what_was_not_seen(cw: CleanedWeb, tmp_path: Path) -> None:
    server = Server()
    routes = server.install()
    store = SeenStore(tmp_path / "seen.json")

    server.runs = [run_row(RUN1)]
    server.items[RUN1] = [job("a", "https://x.test/a"), job("b", "https://x.test/b")]
    first = cw.schedules.new_records(SID, store)
    assert [r["url"] for r in first] == ["https://x.test/a", "https://x.test/b"]
    assert first.run_ids == [RUN1] and first.identity == "contract" and first.seen_count == 0

    # The next slot: b again (its "posted" text changed, identity is the url), c is new.
    server.runs = [run_row(RUN2), run_row(RUN1)]
    server.items[RUN2] = [{**job("b", "https://x.test/b"), "posted": "1d ago"}, job("c", "https://x.test/c")]
    second = cw.schedules.new_records(SID, SeenStore(tmp_path / "seen.json"))  # reopened from disk
    assert [r["url"] for r in second] == ["https://x.test/c"]
    assert second.seen_count == 1 and second.run_ids == [RUN2]
    assert routes[RUN1].call_count == 1, "a handled run is not read again"
    assert routes["contract"].call_count == 1

    # Nothing new: no unread run.
    third = cw.schedules.new_records(SID, store)
    assert list(third) == [] and third.run_ids == []


@respx.mock
def test_new_records_without_mark_waits_for_commit(cw: CleanedWeb) -> None:
    server = Server()
    server.install()
    store = SeenStore()
    server.runs = [run_row(RUN1)]
    server.items[RUN1] = [job("a", "https://x.test/a")]
    pending = cw.schedules.new_records(SID, store, mark=False)
    assert len(pending) == 1
    assert len(cw.schedules.new_records(SID, store, mark=False)) == 1  # a crash before commit: seen again
    pending.commit()
    assert len(cw.schedules.new_records(SID, store)) == 0


@respx.mock
def test_new_records_skips_unfinished_runs_and_scans_all_with_full_scan(cw: CleanedWeb) -> None:
    server = Server()
    server.install()
    store = SeenStore()
    server.runs = [run_row(RUN2), run_row(RUN1)]
    server.items[RUN1] = [job("a", "https://x.test/a")]
    server.items[RUN2] = [job("b", "https://x.test/b")]
    assert len(cw.schedules.new_records(SID, store, max_runs=1)) == 1  # only the newest run
    # RUN1 is older than the handled RUN2: the default scan stops at RUN2, a full scan reaches it.
    assert len(cw.schedules.new_records(SID, store)) == 0
    assert [r["url"] for r in cw.schedules.new_records(SID, store, full_scan=True)] == ["https://x.test/a"]
    assert all(p.get("status") == "finished" for p in server.list_params)


@respx.mock
def test_new_records_falls_back_to_a_record_hash_without_the_contract(cw: CleanedWeb) -> None:
    server = Server()
    routes = server.install()
    routes["contract"].mock(return_value=httpx.Response(403, json={"detail": {"code": "forbidden"}}))
    server.runs = [run_row(RUN1)]
    server.items[RUN1] = [job("a", "https://x.test/a")]
    store = SeenStore()
    result = cw.schedules.new_records(SID, store)
    assert len(result) == 1 and result.identity == "hash"


@respx.mock
def test_new_records_needs_a_source(cw: CleanedWeb) -> None:
    respx.get(f"{SCHEDULES}/{SID}").mock(return_value=httpx.Response(200, json=schedule_body(source_id=None)))
    with pytest.raises(CleanedWebError):
        cw.schedules.new_records(SID, SeenStore())


# -- async parity -------------------------------------------------------------------------------


@pytest.fixture
async def acw() -> Any:
    async with AsyncCleanedWeb(api_key="secret-key") as client:
        yield client


@respx.mock
async def test_async_crud(acw: AsyncCleanedWeb) -> None:
    create = respx.post(SCHEDULES).mock(return_value=httpx.Response(201, json=schedule_body()))
    schedule = await acw.schedules.create(source_id=SOURCE_ID, every="hour", idempotency_key="k-1")
    assert schedule.idempotency_key == "k-1" and create.calls[0].request.headers["idempotency-key"] == "k-1"
    respx.get(SCHEDULES).mock(
        return_value=httpx.Response(200, json={"schedules": [schedule_body()], "limit": 50})
    )
    assert (await acw.schedules.list()).limit == 50
    respx.get(f"{SCHEDULES}/{SID}").mock(return_value=httpx.Response(200, json=schedule_body()))
    assert (await acw.schedules.get(SID)).schedule_id == SID
    respx.post(f"{SCHEDULES}/{SID}/pause").mock(
        return_value=httpx.Response(200, json=schedule_body(state="paused"))
    )
    assert (await acw.schedules.pause(SID)).paused
    respx.post(f"{SCHEDULES}/{SID}/resume").mock(return_value=httpx.Response(200, json=schedule_body()))
    assert not (await acw.schedules.resume(SID)).paused
    respx.delete(f"{SCHEDULES}/{SID}").mock(
        return_value=httpx.Response(200, json={"schedule_id": SID, "deleted": True})
    )
    assert (await acw.schedules.delete(SID)).deleted
    with pytest.raises(ValueError):
        await acw.schedules.create(every="day")


@respx.mock
async def test_async_new_records_across_two_runs(acw: AsyncCleanedWeb, tmp_path: Path) -> None:
    server = Server()
    server.install()
    store = SeenStore(tmp_path / "seen.sqlite")
    server.runs = [run_row(RUN1)]
    server.items[RUN1] = [job("a", "https://x.test/a"), job("b", "https://x.test/b")]
    first = await acw.schedules.new_records(SID, store)
    assert len(first) == 2
    server.runs = [run_row(RUN2), run_row(RUN1)]
    server.items[RUN2] = [job("b", "https://x.test/b"), job("c", "https://x.test/c")]
    second = await acw.schedules.new_records(SID, store)
    assert [r["url"] for r in second] == ["https://x.test/c"]
    assert [r.run_id async for r in acw.schedules.runs(SID)] == [RUN2, RUN1]
