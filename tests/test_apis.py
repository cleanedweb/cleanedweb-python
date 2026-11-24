"""``apis``: build an API from a URL, sync and async, against the portal builder routes."""

from __future__ import annotations

import json
from typing import Any, List

import httpx
import pytest
import respx

from cleanedweb import (
    AsyncCleanedWeb,
    Build,
    BuildFailedError,
    CleanedWeb,
    MissingAPIKeyError,
    PreviewLimitError,
    RateLimitError,
    ScopeRequiredError,
)

from .conftest import (
    BUILDER,
    CAREERS_URL,
    DRAFT_ID,
    JOB_ID,
    RESULT_BODY,
    SOURCE_ID,
    VERSION,
    draft_body,
    job_body,
)

PREVIEWS = f"{BUILDER}/previews"
DRAFT = f"{PREVIEWS}/{DRAFT_ID}"
CREATE = f"{DRAFT}/create"
JOB = f"{BUILDER}/builds/{JOB_ID}"


@pytest.fixture
def cw() -> Any:
    with CleanedWeb(api_key="cw_creation") as client:
        yield client


def respond(*bodies: Any) -> Any:
    """Answer successive calls with ``bodies`` (the last repeats)."""
    items = [b if isinstance(b, httpx.Response) else httpx.Response(200, json=b) for b in bodies]
    return {"side_effect": items + [items[-1]] * 3}


def mock_happy_path() -> dict[str, respx.Route]:
    routes = {
        "preview": respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body("running"))),
        "draft": respx.get(DRAFT).mock(**respond(draft_body("running"), draft_body("ready"))),
        "create": respx.post(CREATE).mock(return_value=httpx.Response(202, json={"state": "running"})),
        "status": respx.get(CREATE).mock(
            **respond({"state": "running"}, {"state": "started", "job_id": JOB_ID})
        ),
        "job": respx.get(JOB).mock(**respond(job_body("queued"), job_body("running"), job_body("succeeded"))),
        "result": respx.get(f"{JOB}/result").mock(return_value=httpx.Response(200, json=RESULT_BODY)),
    }
    return routes


@respx.mock
def test_create_drives_inspect_create_build_result(cw: CleanedWeb, sleeps: List[float]) -> None:
    routes = mock_happy_path()
    seen: List[str] = []
    build = cw.apis.create(CAREERS_URL, want="open jobs", on_progress=lambda b: seen.append(b.state))
    assert build.succeeded and build.terminal
    assert (build.source_id, build.version, build.draft_id, build.job_id) == (
        SOURCE_ID, VERSION, DRAFT_ID, JOB_ID,
    )  # fmt: skip
    assert build.schema == RESULT_BODY["schema"]
    assert routes["create"].call_count == 1  # waiting only reads: the create POST is never repeated
    assert routes["preview"].call_count == 1
    assert json.loads(routes["preview"].calls[0].request.content) == {"url": CAREERS_URL, "want": "open jobs"}
    assert json.loads(routes["create"].calls[0].request.content) == {}
    assert routes["preview"].calls[0].request.headers["authorization"] == "Bearer cw_creation"
    assert routes["job"].calls[0].request.url.params["wait"] == "30"
    assert seen == ["inspecting", "inspecting", "creating", "creating", "queued", "running", "succeeded"]
    assert sleeps, "polling sleeps between reads"


@respx.mock
def test_create_inspection_unsupported_raises(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(
        return_value=httpx.Response(
            201,
            json=draft_body(
                "unsupported", inspection={"status": "unsupported", "failure": {"code": "no_gateway"}}
            ),
        )
    )
    create = respx.post(CREATE)
    with pytest.raises(BuildFailedError) as err:
        cw.apis.create(CAREERS_URL)
    assert err.value.code == "no_gateway" and err.value.stage == "inspection"
    assert err.value.draft_id == DRAFT_ID and err.value.build.state == "failed"
    assert not create.called


@respx.mock
def test_create_typed_inspection_failure_raises(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body(None)))
    respx.get(DRAFT).mock(
        return_value=httpx.Response(
            200,
            json=draft_body(
                None, state="inspection_unknown", failure={"step": "inspection", "code": "page_blocked"}
            ),
        )
    )
    with pytest.raises(BuildFailedError) as err:
        cw.apis.create(CAREERS_URL)
    assert err.value.code == "page_blocked" and err.value.stage == "inspection"


@respx.mock
def test_failed_build_raises_with_code_reason_stage(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body("ready")))
    respx.post(CREATE).mock(return_value=httpx.Response(200, json={"state": "started", "job_id": JOB_ID}))
    respx.get(JOB).mock(
        **respond(
            job_body("running"),
            job_body(
                "failed",
                error_code="build_failed",
                failure={
                    "code": "records_not_found",
                    "reason": "no_record_collection",
                    "stage": "record_preview",
                },
            ),
        )
    )
    with pytest.raises(BuildFailedError) as err:
        cw.apis.create(CAREERS_URL)
    e = err.value
    assert (e.code, e.reason, e.stage) == ("records_not_found", "no_record_collection", "record_preview")
    assert e.job_id == JOB_ID and e.build.state == "failed" and "records_not_found" in str(e)


@respx.mock
def test_cancelled_build_raises(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body("ready")))
    respx.post(CREATE).mock(return_value=httpx.Response(200, json={"state": "started", "job_id": JOB_ID}))
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("cancelled", error_code="cancelled")))
    with pytest.raises(BuildFailedError) as err:
        cw.apis.create(CAREERS_URL)
    assert err.value.code == "cancelled" and err.value.build.state == "cancelled"


@respx.mock
def test_create_that_ends_idle_without_a_job_raises_the_drafts_failure(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body("ready")))
    respx.post(CREATE).mock(return_value=httpx.Response(202, json={"state": "running"}))
    respx.get(CREATE).mock(return_value=httpx.Response(200, json={"state": "idle"}))
    respx.get(DRAFT).mock(
        return_value=httpx.Response(
            200, json=draft_body("ready", failure={"code": "catalog_create_failed", "step": "record-preview"})
        )
    )
    with pytest.raises(BuildFailedError) as err:
        cw.apis.create(CAREERS_URL)
    assert err.value.code == "catalog_create_failed" and err.value.stage == "record-preview"


@respx.mock
def test_poll_timeout_returns_unfinished_then_resume_never_resends_create(cw: CleanedWeb) -> None:
    routes = mock_happy_path()
    # Nothing started the creation yet when resume reads its status: it is sent once.
    routes["status"].mock(
        **respond({"state": "idle"}, {"state": "running"}, {"state": "started", "job_id": JOB_ID})
    )
    first = cw.apis.create(CAREERS_URL, poll_timeout=0)
    assert first.state == "inspecting" and not first.terminal and first.draft_id == DRAFT_ID
    assert not routes["create"].called
    # Continue from the draft: inspection is done, the create POST goes out once.
    done = cw.apis.resume(draft_id=DRAFT_ID)
    assert done.succeeded and routes["create"].call_count == 1
    # Resume again from the job handle: reads only.
    again = cw.apis.resume(job_id=JOB_ID)
    assert again.succeeded and routes["create"].call_count == 1 and routes["preview"].call_count == 1


@respx.mock
def test_poll_timeout_while_creating_and_while_building_returns_the_unfinished_build(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body("ready")))
    respx.post(CREATE).mock(return_value=httpx.Response(202, json={"state": "running"}))
    status = respx.get(CREATE).mock(**respond({"state": "running"}))
    creating = cw.apis.create(CAREERS_URL, poll_timeout=0)
    assert creating.state == "creating" and creating.draft_id == DRAFT_ID and creating.job_id is None
    assert not status.called
    respx.post(CREATE).mock(return_value=httpx.Response(200, json={"state": "started", "job_id": JOB_ID}))
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("running")))
    building = cw.apis.create(CAREERS_URL, poll_timeout=0)
    assert building.state == "running" and building.job_id == JOB_ID and not building.terminal


@respx.mock
def test_resume_while_creation_runs_does_not_post(cw: CleanedWeb) -> None:
    respx.get(DRAFT).mock(return_value=httpx.Response(200, json=draft_body("ready")))
    status = respx.get(CREATE).mock(**respond({"state": "running"}, {"state": "started", "job_id": JOB_ID}))
    post = respx.post(CREATE)
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("succeeded")))
    respx.get(f"{JOB}/result").mock(return_value=httpx.Response(200, json=RESULT_BODY))
    assert cw.apis.resume(draft_id=DRAFT_ID).source_id == SOURCE_ID
    assert not post.called and status.call_count == 2


@respx.mock
def test_resume_does_not_retry_a_failed_creation(cw: CleanedWeb) -> None:
    respx.get(DRAFT).mock(
        return_value=httpx.Response(200, json=draft_body("ready", failure={"code": "catalog_create_failed"}))
    )
    post = respx.post(CREATE)
    with pytest.raises(BuildFailedError):
        cw.apis.resume(draft_id=DRAFT_ID)
    assert not post.called
    with pytest.raises(ValueError):
        cw.apis.resume()


@respx.mock
def test_wait_false_returns_after_the_preview_is_accepted(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(202, json=draft_body(None)))
    build = cw.apis.create(CAREERS_URL, wait=False)
    assert build.state == "inspecting" and build.draft_id == DRAFT_ID


@respx.mock
def test_get_build_reads_once_and_fetches_the_result_when_done(cw: CleanedWeb) -> None:
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("running")))
    assert cw.apis.get_build(JOB_ID).state == "running"
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("failed", failure={"code": "x"})))
    failed = cw.apis.get_build(JOB_ID)
    assert failed.state == "failed" and failed.failure == {"code": "x"}  # returned, not raised
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("succeeded")))
    respx.get(f"{JOB}/result").mock(return_value=httpx.Response(200, json=RESULT_BODY))
    assert cw.apis.get_build(JOB_ID).source_id == SOURCE_ID


@respx.mock
def test_queue_position_reaches_on_progress(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=httpx.Response(201, json=draft_body("ready")))
    respx.post(CREATE).mock(return_value=httpx.Response(202, json={"state": "queued", "queue_position": 3}))
    respx.get(CREATE).mock(return_value=httpx.Response(200, json={"state": "started", "job_id": JOB_ID}))
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("succeeded")))
    respx.get(f"{JOB}/result").mock(return_value=httpx.Response(200, json=RESULT_BODY))
    positions: List[Any] = []
    cw.apis.create(CAREERS_URL, on_progress=lambda b: positions.append(b.queue_position))
    assert 3 in positions


# -- errors -------------------------------------------------------------------------------------


def limit_response(retry_after: str = "1200") -> httpx.Response:
    return httpx.Response(
        429,
        headers={"Retry-After": retry_after},
        json={
            "detail": {
                "code": "preview_limit",
                "message": "This workspace has used its previews for the hour.",
                "cause": "owner_hourly",
                "limit": 20,
                "window_seconds": 3600,
            }
        },
    )


@respx.mock
def test_preview_limit_is_a_typed_rate_limit_error(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=limit_response())
    with pytest.raises(PreviewLimitError) as err:
        cw.apis.create(CAREERS_URL)
    e = err.value
    assert isinstance(e, RateLimitError)
    assert (e.cause, e.limit, e.window_seconds, e.retry_after) == ("owner_hourly", 20, 3600, 1200.0)


@respx.mock
def test_scope_required_names_the_missing_scopes(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(
        return_value=httpx.Response(
            403,
            json={
                "detail": {
                    "code": "api_key_scope_required",
                    "message": "api key scope required",
                    "required_scope": "discover",
                    "required_scopes": ["discover"],
                    "granted_scopes": ["run"],
                }
            },
        )
    )
    with pytest.raises(ScopeRequiredError) as err:
        cw.apis.create(CAREERS_URL)
    assert err.value.required_scopes == ["discover"] and err.value.granted_scopes == ["run"]
    assert err.value.missing_scopes == ["discover"] and err.value.status_code == 403


def test_a_missing_key_fails_before_any_request() -> None:
    with CleanedWeb() as client, pytest.raises(MissingAPIKeyError):
        client.apis.create(CAREERS_URL)


@respx.mock
def test_create_many_waits_out_the_preview_limit_and_collects_failures(
    cw: CleanedWeb, sleeps: List[float]
) -> None:
    second = "https://careers.example.net/jobs"
    previews = respx.post(PREVIEWS).mock(
        side_effect=[
            httpx.Response(201, json=draft_body("ready")),  # url 1
            limit_response("90"),  # url 2 refused ...
            httpx.Response(
                201, json=draft_body("unsupported", inspection={"status": "unsupported"})
            ),  # ... then taken
        ]
    )
    respx.post(CREATE).mock(return_value=httpx.Response(200, json={"state": "started", "job_id": JOB_ID}))
    respx.get(JOB).mock(return_value=httpx.Response(200, json=job_body("succeeded")))
    respx.get(f"{JOB}/result").mock(return_value=httpx.Response(200, json=RESULT_BODY))
    builds = cw.apis.create_many([CAREERS_URL, second])
    assert [b.state for b in builds] == ["succeeded", "failed"]
    assert isinstance(builds[0], Build) and builds[1].failure
    assert 90.0 in sleeps  # the Retry-After was waited out
    assert previews.call_count == 3
    assert json.loads(previews.calls[1].request.content)["url"] == second
    assert json.loads(previews.calls[2].request.content)["url"] == second


@respx.mock
def test_create_many_can_refuse_to_wait(cw: CleanedWeb) -> None:
    respx.post(PREVIEWS).mock(return_value=limit_response("3000"))
    with pytest.raises(PreviewLimitError):
        cw.apis.create_many([CAREERS_URL], max_limit_wait=60)
    with pytest.raises(PreviewLimitError):
        cw.apis.create_many([CAREERS_URL], wait_for_limit=False)


# -- async parity -------------------------------------------------------------------------------


@pytest.fixture
async def acw() -> Any:
    async with AsyncCleanedWeb(api_key="cw_creation") as client:
        yield client


@respx.mock
async def test_async_create_happy_path(acw: AsyncCleanedWeb) -> None:
    routes = mock_happy_path()
    seen: List[str] = []

    async def progress(build: Build) -> None:  # a coroutine callback is awaited
        seen.append(build.state)

    build = await acw.apis.create(CAREERS_URL, on_progress=progress)
    assert build.succeeded and build.source_id == SOURCE_ID and build.version == VERSION
    assert routes["create"].call_count == 1 and seen[-1] == "succeeded"
    assert routes["preview"].calls[0].request.headers["authorization"] == "Bearer cw_creation"


@respx.mock
async def test_async_failures_scope_and_limit(acw: AsyncCleanedWeb) -> None:
    respx.post(PREVIEWS).mock(
        side_effect=[
            httpx.Response(201, json=draft_body("ready")),
            httpx.Response(
                403,
                json={"detail": {"code": "api_key_scope_required", "required_scopes": ["discover", "build"]}},
            ),
            limit_response("40"),
        ]
    )
    respx.post(CREATE).mock(return_value=httpx.Response(200, json={"state": "started", "job_id": JOB_ID}))
    respx.get(JOB).mock(
        return_value=httpx.Response(200, json=job_body("failed", failure={"code": "x", "reason": "y"}))
    )
    with pytest.raises(BuildFailedError) as failed:
        await acw.apis.create(CAREERS_URL)
    assert (failed.value.code, failed.value.reason) == ("x", "y")
    with pytest.raises(ScopeRequiredError) as scope:
        await acw.apis.create(CAREERS_URL)
    assert scope.value.missing_scopes == ["discover", "build"]
    with pytest.raises(PreviewLimitError) as limit:
        await acw.apis.create(CAREERS_URL)
    assert limit.value.retry_after == 40.0


@respx.mock
async def test_async_poll_timeout_resume_and_create_many(acw: AsyncCleanedWeb, sleeps: List[float]) -> None:
    routes = mock_happy_path()
    routes["status"].mock(
        **respond({"state": "idle"}, {"state": "running"}, {"state": "started", "job_id": JOB_ID})
    )
    partial = await acw.apis.create(CAREERS_URL, poll_timeout=0)
    assert partial.state == "inspecting" and not routes["create"].called
    assert (await acw.apis.resume(draft_id=DRAFT_ID)).succeeded
    assert (await acw.apis.resume(job_id=JOB_ID)).succeeded
    assert routes["create"].call_count == 1
    assert (await acw.apis.get_build(JOB_ID)).source_id == SOURCE_ID
    assert (await acw.apis.get_draft(DRAFT_ID)).draft_id == DRAFT_ID

    respx.post(PREVIEWS).mock(
        side_effect=[
            limit_response("45"),
            httpx.Response(201, json=draft_body("ready")),
        ]
    )
    builds = await acw.apis.create_many([CAREERS_URL])
    assert [b.state for b in builds] == ["succeeded"] and 45.0 in sleeps
