from __future__ import annotations

import json
from typing import Any, AsyncIterator, List

import httpx
import pytest
import respx

from cleanedweb import (
    APIConnectionError,
    AsyncCleanedWeb,
    IdempotencyConflictError,
    InsufficientCreditError,
    MissingAPIKeyError,
    RateLimitError,
    ServerError,
)

from .conftest import (
    API_ID,
    API_ORIGIN,
    APP,
    ENDPOINT_ID,
    INDEX,
    LIVE_ITEMS,
    RUN_BASE,
    RUN_ID,
    SEEK_ITEMS,
    SITE,
    SITE_CONTRACT,
    final_body,
    run_object,
)


@pytest.fixture
async def cw() -> AsyncIterator[AsyncCleanedWeb]:
    async with AsyncCleanedWeb(api_key="secret-key") as client:
        yield client


@respx.mock
async def test_list_iter_get_find(cw: AsyncCleanedWeb) -> None:
    respx.get(f"{APP}/api/catalog/apis").mock(return_value=httpx.Response(200, json={"items": LIVE_ITEMS}))
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(200, json=INDEX))
    respx.get(f"{SITE}/catalog/github-topics-repositories-api/contract.json").mock(
        return_value=httpx.Response(200, json=SITE_CONTRACT)
    )
    assert [a.api_id for a in await cw.catalog.list()][0] == API_ID
    assert len([a async for a in cw.catalog.iter()]) == 2
    contract = await cw.catalog.get("github-topics-repositories-api")
    assert contract.endpoint().output_fields == ["title", "url", "stargazer_count"]
    found = await cw.catalog.find(site="github.com", fields=["stargazer_count"])
    assert [a.api_id for a in found] == [API_ID]
    assert await cw.catalog.find(site="github.com", fields=["stargazer_count"], deep=False) == []


@respx.mock
async def test_run_200(cw: AsyncCleanedWeb) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(200, json=final_body()))
    result = await cw.catalog.run(API_ID, ENDPOINT_ID, {"a": 1}, max_items=2, idempotency_key="async-1")
    request = route.calls[0].request
    assert (
        request.headers["idempotency-key"] == "async-1"
        and request.headers["authorization"] == "Bearer secret-key"
    )
    assert json.loads(request.content) == {"input": {"a": 1}, "max_items": 2}
    assert result.is_usable and result.idempotency_key == "async-1"


@respx.mock
async def test_run_202_polls_with_same_key(cw: AsyncCleanedWeb, sleeps: List[float]) -> None:
    post = respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(202, json=run_object(), headers={"Retry-After": "7"})
    )
    result_route = respx.get(f"{RUN_BASE}/result").mock(
        side_effect=[
            httpx.Response(
                409, json={"detail": {"code": "result_not_running"}}, headers={"Retry-After": "2"}
            ),
            httpx.Response(200, json=final_body()),
        ]
    )
    result = await cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="async-2")
    assert post.call_count == 1 and result_route.call_count == 2 and sleeps == [7.0, 2.0]
    assert all(c.request.headers["idempotency-key"] == "async-2" for c in result_route.calls)
    assert result.complete


@respx.mock
async def test_wait_false_and_poll_timeout(cw: AsyncCleanedWeb) -> None:
    respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(202, json=run_object(), headers={"Retry-After": "30"})
    )
    assert not (await cw.catalog.run(API_ID, ENDPOINT_ID, wait=False)).complete
    assert not (await cw.catalog.run(API_ID, ENDPOINT_ID, poll_timeout=1)).complete


@respx.mock
async def test_errors(cw: AsyncCleanedWeb, sleeps: List[float]) -> None:
    route = respx.post(f"{RUN_BASE}/run")
    route.mock(
        return_value=httpx.Response(
            402, json={"detail": {"code": "insufficient_units", "required": 5, "available": 1}}
        )
    )
    with pytest.raises(InsufficientCreditError) as info:
        await cw.catalog.run(API_ID, ENDPOINT_ID)
    assert info.value.required == 5
    route.mock(return_value=httpx.Response(409, json={"detail": {"code": "delivery_identity_conflict"}}))
    with pytest.raises(IdempotencyConflictError):
        await cw.catalog.run(API_ID, ENDPOINT_ID)
    route.mock(return_value=httpx.Response(429, headers={"Retry-After": "99"}, json={"detail": "slow down"}))
    with pytest.raises(RateLimitError) as limited:
        await cw.catalog.run(API_ID, ENDPOINT_ID)
    assert limited.value.retry_after == 99.0
    route.mock(return_value=httpx.Response(503, json={"detail": "down"}))
    with pytest.raises(ServerError):
        await cw.catalog.run(API_ID, ENDPOINT_ID)
    assert sleeps == [0.5, 1.0]
    route.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(APIConnectionError) as lost:
        await cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="lost-1")
    assert lost.value.idempotency_key == "lost-1"


async def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLEANEDWEB_API_KEY", raising=False)
    async with AsyncCleanedWeb() as client:
        with pytest.raises(MissingAPIKeyError):
            await client.catalog.run(API_ID, ENDPOINT_ID)


@respx.mock
async def test_result_get_abort_list(cw: AsyncCleanedWeb) -> None:
    respx.get(f"{RUN_BASE}/result").mock(return_value=httpx.Response(200, json=final_body()))
    assert (await cw.catalog.result(API_ID, ENDPOINT_ID, "k")).complete
    respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}").mock(return_value=httpx.Response(200, json=run_object()))
    respx.post(f"{API_ORIGIN}/v1/runs/{RUN_ID}/abort").mock(
        return_value=httpx.Response(202, json={**run_object(), "abort_requested": True})
    )
    assert (await cw.runs.get(RUN_ID)).status == "running"
    assert (await cw.runs.abort(RUN_ID)).abort_requested

    def pages(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"runs": [run_object("finished")], "next_cursor": "c2"})
        return httpx.Response(200, json={"runs": [run_object("failed")], "next_cursor": None})

    respx.get(f"{API_ORIGIN}/v1/runs").mock(side_effect=pages)
    assert [r.status for r in [r async for r in cw.runs.iter()]] == ["finished", "failed"]


@respx.mock
async def test_runs_items_pagination(cw: AsyncCleanedWeb, sleeps: List[float]) -> None:
    responses: List[Any] = [
        {"items": [{"n": 1}, {"n": 2}], "next_cursor": 2},
        {"items": [], "next_cursor": 2},
        {"items": [{"n": 3}], "next_cursor": 3},
        {"items": [], "next_cursor": None},
    ]
    route = respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/items").mock(
        side_effect=[httpx.Response(200, json=r) for r in responses]
    )
    assert [i["n"] for i in [i async for i in cw.runs.items(RUN_ID)]] == [1, 2]
    route.side_effect = [httpx.Response(200, json=r) for r in responses]
    assert [i["n"] for i in [i async for i in cw.runs.items(RUN_ID, follow=True)]] == [1, 2, 3]
    assert len(sleeps) == 1


@respx.mock
async def test_runs_result_and_sources_run(cw: AsyncCleanedWeb, sleeps: List[float]) -> None:
    respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/result").mock(
        side_effect=[
            httpx.Response(202, json=run_object(), headers={"Retry-After": "4"}),
            httpx.Response(200, json=final_body()),
        ]
    )
    assert (await cw.runs.result(RUN_ID)).complete and sleeps == [4.0]
    respx.post(f"{API_ORIGIN}/v1/sources/src_1/run").mock(return_value=httpx.Response(200, json=final_body()))
    assert (await cw.sources.run("src_1", {"q": 1}, max_items=2)).complete
    with pytest.raises(ValueError):
        await cw.sources.run("src_1", expected_version="a", definition_version="b")


@respx.mock
async def test_find_matches_a_site_by_its_alias_host(cw: AsyncCleanedWeb) -> None:
    respx.get(f"{APP}/api/catalog/apis").mock(return_value=httpx.Response(200, json={"items": SEEK_ITEMS}))
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(200, json={"apis": []}))
    for site in ("seek.com.au", "www.seek.com.au", "https://www.seek.com.au/jobs"):
        assert [a.site_domain for a in await cw.catalog.find(site=site)] == ["au.seek.com"]
    assert [a.site_domain for a in await cw.catalog.find(site="notseek.com.au")] == ["notseek.com.au"]
