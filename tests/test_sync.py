from __future__ import annotations

import json
import re
from typing import Any, List

import httpx
import pytest
import respx

from cleanedweb import (
    APIConnectionError,
    AuthenticationError,
    CleanedWeb,
    CleanedWebError,
    IdempotencyConflictError,
    InsufficientCreditError,
    InvalidRequestError,
    MissingAPIKeyError,
    NotFoundError,
    RateLimitError,
    RunFailedError,
    ServerError,
    UsageBudgetExceededError,
    VersionConflictError,
    VersionNotFoundError,
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
    SITE,
    SITE_CONTRACT,
    final_body,
    run_object,
)

KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


@pytest.fixture
def cw() -> Any:
    with CleanedWeb(api_key="secret-key") as client:
        yield client


# -- catalog reads --------------------------------------------------------------------------


@respx.mock
def test_list_sends_params_and_needs_no_key() -> None:
    route = respx.get(f"{APP}/api/catalog/apis").mock(
        return_value=httpx.Response(200, json={"items": LIVE_ITEMS})
    )
    with CleanedWeb(api_key=None) as client:
        apis = client.catalog.list(q="git", category="commerce", limit=5, offset=10, sort="name")
    request = route.calls[0].request
    assert dict(request.url.params) == {
        "limit": "5",
        "offset": "10",
        "q": "git",
        "category": "commerce",
        "sort": "name",
    }
    assert "authorization" not in request.headers
    assert [a.api_id for a in apis][0] == API_ID
    assert apis[0].site_domain == "github.com" and apis[0].endpoints[0].endpoint_id == ENDPOINT_ID


@respx.mock
def test_iter_follows_offset_pages() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        items = [{"api_id": f"a{n}", "name": str(n)} for n in range(offset, min(offset + 2, 5))]
        return httpx.Response(200, json={"items": items})

    respx.get(f"{APP}/api/catalog/apis").mock(side_effect=page)
    with CleanedWeb() as client:
        ids = [a.api_id for a in client.catalog.iter(page_size=2)]
    assert ids == ["a0", "a1", "a2", "a3", "a4"]


@respx.mock
def test_get_uses_site_contract_for_indexed_slug_or_id() -> None:
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(200, json=INDEX))
    route = respx.get(f"{SITE}/catalog/github-topics-repositories-api/contract.json").mock(
        return_value=httpx.Response(200, json=SITE_CONTRACT)
    )
    with CleanedWeb() as client:
        by_slug = client.catalog.get("github-topics-repositories-api")
        by_id = client.catalog.get(API_ID)
    assert route.call_count == 2
    endpoint = by_slug.endpoint()
    assert endpoint.endpoint_id == ENDPOINT_ID and endpoint.required_inputs == []
    assert endpoint.output_fields == ["title", "url", "stargazer_count"]
    assert by_id.site_domain == "github.com"


@respx.mock
def test_get_falls_back_to_live_contract_when_not_indexed() -> None:
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(200, json={"apis": []}))
    respx.get(f"{APP}/api/catalog/apis/{API_ID}").mock(return_value=httpx.Response(200, json=LIVE_ITEMS[0]))
    respx.get(f"{APP}/api/catalog/apis/{API_ID}/endpoints/{ENDPOINT_ID}/contract").mock(
        return_value=httpx.Response(200, json={**SITE_CONTRACT["endpoints"][0], "version": "1.0"})  # type: ignore[dict-item]
    )
    with CleanedWeb() as client:
        contract = client.catalog.get(API_ID)
    assert contract.name == "GitHub Topics Repositories"
    assert contract.endpoint(ENDPOINT_ID).output_fields == ["title", "url", "stargazer_count"]
    assert contract.endpoint().name == "Extract records"


@respx.mock
def test_get_unknown_is_not_found() -> None:
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(200, json={"apis": []}))
    respx.get(f"{APP}/api/catalog/apis/nope").mock(
        return_value=httpx.Response(404, json={"detail": {"code": "catalog_api_not_found", "message": "no"}})
    )
    with CleanedWeb() as client, pytest.raises(NotFoundError):
        client.catalog.get("nope")


@respx.mock
def test_find_filters_site_and_fields_with_deep_contract_check() -> None:
    respx.get(f"{APP}/api/catalog/apis").mock(return_value=httpx.Response(200, json={"items": LIVE_ITEMS}))
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(200, json=INDEX))
    respx.get(f"{SITE}/catalog/github-topics-repositories-api/contract.json").mock(
        return_value=httpx.Response(200, json=SITE_CONTRACT)
    )
    with CleanedWeb() as client:
        by_site = client.catalog.find(site="https://www.GitHub.com/topics")
        assert [a.api_id for a in by_site] == [API_ID]
        assert by_site[0].slug == "github-topics-repositories-api" and "url" in by_site[0].key_fields
        by_key_field = client.catalog.find(site="github", fields=["Title"])
        assert [a.api_id for a in by_key_field] == [API_ID]
        deep = client.catalog.find(site="github", fields=["stargazer_count"])
        assert [a.api_id for a in deep] == [API_ID]
        assert client.catalog.find(site="github", fields=["stargazer_count"], deep=False) == []
        assert client.catalog.find(site="github", fields=["nope"]) == []
        assert [a.api_id for a in client.catalog.find(site="blocket")] == [
            "generated_bbbbbbbbbbbbbbbbbbbbbbbb"
        ]


@respx.mock
def test_find_works_when_the_index_is_down() -> None:
    respx.get(f"{APP}/api/catalog/apis").mock(return_value=httpx.Response(200, json={"items": LIVE_ITEMS}))
    respx.get(f"{SITE}/catalog/index.json").mock(return_value=httpx.Response(503, text="down"))
    with CleanedWeb(max_retries=0) as client:
        assert len(client.catalog.find(site="github.com")) == 1


# -- run ------------------------------------------------------------------------------------


@respx.mock
def test_run_200_sends_documented_request(cw: CleanedWeb) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(
            200,
            json=final_body(),
            headers={"Deprecation": "@1795046400", "Sunset": "Sun, 17 Jan 2027 00:00:00 GMT"},
        )
    )
    result = cw.catalog.run(
        API_ID,
        ENDPOINT_ID,
        {"sort": "desc_stars"},
        max_items=25,
        timeout_seconds=3600,
        version=2,
        fields=["title"],
        nulls=True,
        idempotency_key="my-key_1:a.b",
    )
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer secret-key"
    assert request.headers["idempotency-key"] == "my-key_1:a.b"
    assert json.loads(request.content) == {
        "input": {"sort": "desc_stars"},
        "max_items": 25,
        "timeout_seconds": 3600,
        "fields": ["title"],
        "nulls": True,
        "version": 2,
    }
    assert result.complete and result.is_usable and len(result) == 2 and list(result)[0]["title"] == "a"
    assert result.idempotency_key == "my-key_1:a.b"
    assert result.run_id == RUN_ID and result.stop_reason == "max_items" and result.units_charged == 2
    assert result.deprecation == "@1795046400" and result.sunset and result.version == "1.0"


@respx.mock
def test_run_generates_and_returns_an_idempotency_key(cw: CleanedWeb) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(200, json=final_body()))
    first = cw.catalog.run(API_ID, ENDPOINT_ID, max_items=1)
    second = cw.catalog.run(API_ID, ENDPOINT_ID, max_items=1)
    assert first.idempotency_key and KEY_RE.match(first.idempotency_key)
    assert first.idempotency_key != second.idempotency_key
    assert route.calls[0].request.headers["idempotency-key"] == first.idempotency_key


@respx.mock
def test_idempotency_reuse_sends_the_same_key_and_returns_the_same_run(cw: CleanedWeb) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(200, json=final_body()))
    one = cw.catalog.run(API_ID, ENDPOINT_ID, max_items=2, idempotency_key="job-42")
    two = cw.catalog.run(API_ID, ENDPOINT_ID, max_items=2, idempotency_key="job-42")
    assert [c.request.headers["idempotency-key"] for c in route.calls] == ["job-42", "job-42"]
    assert one.run_id == two.run_id and one.records == two.records


@pytest.mark.parametrize("bad", ["has space", "x" * 201, "a/b", "é"])
def test_invalid_idempotency_key_is_rejected_before_sending(cw: CleanedWeb, bad: str) -> None:
    with respx.mock:
        with pytest.raises(ValueError):
            cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key=bad)


def test_invalid_max_items(cw: CleanedWeb) -> None:
    with pytest.raises(ValueError):
        cw.catalog.run(API_ID, ENDPOINT_ID, max_items=0)


@respx.mock
def test_run_202_polls_result_with_same_key_and_never_reposts(cw: CleanedWeb, sleeps: List[float]) -> None:
    post = respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(
            202, json=run_object("queued"), headers={"Retry-After": "7", "Location": f"/v1/runs/{RUN_ID}"}
        )
    )
    result_route = respx.get(f"{RUN_BASE}/result").mock(
        side_effect=[
            httpx.Response(202, json=run_object(), headers={"Retry-After": "3"}),
            httpx.Response(200, json=final_body()),
        ]
    )
    result = cw.catalog.run(API_ID, ENDPOINT_ID, max_items=2, idempotency_key="slow-1")
    assert post.call_count == 1 and result_route.call_count == 2
    assert all(c.request.headers["idempotency-key"] == "slow-1" for c in result_route.calls)
    assert all(c.request.headers["authorization"] == "Bearer secret-key" for c in result_route.calls)
    assert sleeps == [7.0, 3.0]
    assert result.complete and len(result.records) == 2 and result.idempotency_key == "slow-1"


@respx.mock
def test_poll_treats_result_not_ready_conflicts_as_pending(cw: CleanedWeb, sleeps: List[float]) -> None:
    respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(202, json=run_object()))
    respx.get(f"{RUN_BASE}/result").mock(
        side_effect=[
            httpx.Response(
                409, json={"detail": {"code": "result_not_complete"}}, headers={"Retry-After": "2"}
            ),
            httpx.Response(200, json=final_body()),
        ]
    )
    result = cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="k1")
    assert result.complete and sleeps == [30.0, 2.0]  # no Retry-After on the 202: poll_after_seconds


@respx.mock
def test_wait_false_returns_right_after_acceptance(cw: CleanedWeb) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(202, json=run_object("queued")))
    result = cw.catalog.run(API_ID, ENDPOINT_ID, wait=False, idempotency_key="k2")
    assert json.loads(route.calls[0].request.content)["wait_seconds"] == 0
    assert not result.complete and result.run_id == RUN_ID and not result.is_usable and result.records == []


@respx.mock
def test_poll_timeout_returns_incomplete_result(cw: CleanedWeb) -> None:
    respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(202, json=run_object(), headers={"Retry-After": "30"})
    )
    result = cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="k3", poll_timeout=5)
    assert not result.complete and result.idempotency_key == "k3"


@respx.mock
def test_result_recovers_with_the_saved_key(cw: CleanedWeb) -> None:
    route = respx.get(f"{RUN_BASE}/result").mock(return_value=httpx.Response(200, json=final_body()))
    result = cw.catalog.result(API_ID, ENDPOINT_ID, "saved-key")
    assert route.calls[0].request.headers["idempotency-key"] == "saved-key" and result.complete


@respx.mock
def test_result_unknown_key_is_not_found(cw: CleanedWeb) -> None:
    respx.get(f"{RUN_BASE}/result").mock(
        return_value=httpx.Response(404, json={"detail": {"code": "catalog_delivery_not_found"}})
    )
    with pytest.raises(NotFoundError):
        cw.catalog.result(API_ID, ENDPOINT_ID, "unknown")


@respx.mock
def test_unusable_summary_is_flagged(cw: CleanedWeb) -> None:
    body = final_body(summary={"status": "partial", "within_scope_passed": True})
    respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(200, json=body))
    assert not cw.catalog.run(API_ID, ENDPOINT_ID).is_usable


@respx.mock
def test_aborted_run_is_not_usable(cw: CleanedWeb) -> None:
    body = final_body(summary={"status": "bounded", "within_scope_passed": True})
    body["run"] = {**body.get("run", {}), "status": "aborted", "stop_reason": "aborted"}
    respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(200, json=body))
    assert not cw.catalog.run(API_ID, ENDPOINT_ID).is_usable


# -- errors ---------------------------------------------------------------------------------


def _post(status: int, body: Any, headers: Any = None) -> None:
    respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(status, json=body, headers=headers))


@respx.mock
def test_401_and_403(cw: CleanedWeb) -> None:
    _post(401, {"detail": "Invalid API key"})
    with pytest.raises(AuthenticationError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="e1")
    assert (
        info.value.status_code == 401
        and info.value.idempotency_key == "e1"
        and "Invalid API key" in str(info.value)
    )


@respx.mock
def test_402_insufficient_credit_has_required_and_available(cw: CleanedWeb) -> None:
    _post(402, {"detail": {"code": "insufficient_units", "message": "low", "required": 100, "available": 3}})
    with pytest.raises(InsufficientCreditError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID)
    assert (info.value.required, info.value.available, info.value.code) == (100, 3, "insufficient_units")


@respx.mock
def test_409_codes_map_to_typed_errors(cw: CleanedWeb) -> None:
    cases = [
        ("delivery_identity_conflict", IdempotencyConflictError),
        ("usage_budget_exceeded", UsageBudgetExceededError),
        ("builder_definition_version_conflict", VersionConflictError),
    ]
    for code, cls in cases:
        _post(409, {"detail": {"code": code}})
        with pytest.raises(cls):
            cw.catalog.run(API_ID, ENDPOINT_ID)


@respx.mock
def test_404_version_not_found_lists_majors(cw: CleanedWeb) -> None:
    _post(404, {"detail": {"code": "catalog_version_not_found", "available": [1, 2]}})
    with pytest.raises(VersionNotFoundError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID, version=7)
    assert info.value.available == [1, 2] and isinstance(info.value, NotFoundError)


@respx.mock
def test_422_is_invalid_request(cw: CleanedWeb) -> None:
    _post(422, {"detail": {"code": "run_option_invalid", "option": "max_items", "minimum": 1}})
    with pytest.raises(InvalidRequestError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID)
    assert info.value.code == "run_option_invalid"


@respx.mock
def test_422_validation_list_message(cw: CleanedWeb) -> None:
    _post(422, {"detail": [{"loc": ["body"], "msg": "field required"}]})
    with pytest.raises(InvalidRequestError, match="field required"):
        cw.catalog.run(API_ID, ENDPOINT_ID)


@respx.mock
def test_429_is_retried_with_the_same_key_honoring_retry_after(cw: CleanedWeb, sleeps: List[float]) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(
        side_effect=[
            httpx.Response(
                429, json={"detail": {"code": "engine_request_limited"}}, headers={"Retry-After": "4"}
            ),
            httpx.Response(200, json=final_body()),
        ]
    )
    result = cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="rl-1")
    assert result.complete and sleeps == [4.0]
    assert [c.request.headers["idempotency-key"] for c in route.calls] == ["rl-1", "rl-1"]


@respx.mock
def test_429_raises_with_retry_after_when_too_long_or_out_of_retries() -> None:
    _post(
        429,
        {"detail": {"code": "engine_request_limited", "retry_after_seconds": 120}},
        {"Retry-After": "120"},
    )
    with CleanedWeb(api_key="k") as client, pytest.raises(RateLimitError) as info:
        client.catalog.run(API_ID, ENDPOINT_ID)
    assert info.value.retry_after == 120.0 and info.value.status_code == 429
    assert len(respx.calls) == 1


@respx.mock
def test_429_retry_after_from_detail_body_only() -> None:
    _post(429, {"detail": {"code": "engine_request_limited", "retry_after_seconds": 2}})
    with CleanedWeb(api_key="k", max_retries=0) as client, pytest.raises(RateLimitError) as info:
        client.catalog.run(API_ID, ENDPOINT_ID)
    assert info.value.retry_after == 2.0


@respx.mock
def test_503_is_retried_then_raised_as_unsure(sleeps: List[float]) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(503, json={"detail": {"code": "runs_unavailable"}})
    )
    with CleanedWeb(api_key="k", max_retries=2) as client, pytest.raises(ServerError) as info:
        client.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="s1")
    assert route.call_count == 3 and sleeps == [0.5, 1.0]
    assert info.value.unsure and not info.value.run_failed


@respx.mock
def test_failed_run_marker_is_final_and_not_retried(cw: CleanedWeb) -> None:
    route = respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(
            502,
            json={
                "detail": {
                    "code": "source_blocked",
                    "run_id": RUN_ID,
                    "error_code": "run_failed",
                    "replayed": False,
                }
            },
            headers={"Location": f"/v1/runs/{RUN_ID}"},
        )
    )
    with pytest.raises(CleanedWebError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="f1")
    err = info.value
    assert isinstance(err, ServerError) and err.run_failed and err.run_id == RUN_ID and not err.unsure
    assert route.call_count == 1


@respx.mock
def test_failed_run_replay_on_a_client_error_is_run_failed_error(cw: CleanedWeb) -> None:
    _post(
        400,
        {"detail": {"run_id": RUN_ID, "error_code": "failed_run_replayed", "replayed": True, "message": "x"}},
    )
    with pytest.raises(InvalidRequestError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID)
    assert info.value.run_failed and info.value.replayed
    respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(410, json={"detail": {"run_id": RUN_ID, "error_code": "run_failed"}})
    )
    with pytest.raises(RunFailedError) as failed:
        cw.catalog.run(API_ID, ENDPOINT_ID)
    assert failed.value.run_id == RUN_ID


@respx.mock
def test_failure_while_polling_raises(cw: CleanedWeb) -> None:
    respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(202, json=run_object()))
    respx.get(f"{RUN_BASE}/result").mock(
        return_value=httpx.Response(
            500, json={"detail": {"run_id": RUN_ID, "error_code": "failed_run_replayed", "replayed": True}}
        )
    )
    with pytest.raises(ServerError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="p1")
    assert info.value.run_failed


@respx.mock
def test_connection_error_keeps_the_key(cw: CleanedWeb) -> None:
    respx.post(f"{RUN_BASE}/run").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(APIConnectionError) as info:
        cw.catalog.run(API_ID, ENDPOINT_ID, idempotency_key="t1")
    assert info.value.idempotency_key == "t1" and "idempotency_key" in str(info.value)
    assert len(respx.calls) == 1  # no blind resend


@respx.mock
def test_redirect_means_wrong_origin(cw: CleanedWeb) -> None:
    respx.post(f"{RUN_BASE}/run").mock(
        return_value=httpx.Response(302, headers={"Location": "https://x.test/"})
    )
    with pytest.raises(CleanedWebError, match="nothing ran"):
        cw.catalog.run(API_ID, ENDPOINT_ID)


@respx.mock
def test_missing_key_raises_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLEANEDWEB_API_KEY", raising=False)
    with CleanedWeb() as client, pytest.raises(MissingAPIKeyError):
        client.catalog.run(API_ID, ENDPOINT_ID)
    assert len(respx.calls) == 0


@respx.mock
def test_key_from_environment_and_never_sent_to_read_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLEANEDWEB_API_KEY", "env-key")
    read = respx.get(f"{APP}/api/catalog/apis").mock(return_value=httpx.Response(200, json={"items": []}))
    run = respx.post(f"{RUN_BASE}/run").mock(return_value=httpx.Response(200, json=final_body()))
    with CleanedWeb() as client:
        client.catalog.list()
        client.catalog.run(API_ID, ENDPOINT_ID)
        assert "env-key" not in repr(client)
    assert "authorization" not in read.calls[0].request.headers
    assert run.calls[0].request.headers["authorization"] == "Bearer env-key"


@respx.mock
def test_base_url_is_overridable() -> None:
    route = respx.post(
        f"https://broker.example.test/v1/catalog/public/apis/{API_ID}/endpoints/{ENDPOINT_ID}/run"
    ).mock(return_value=httpx.Response(200, json=final_body()))
    with CleanedWeb(api_key="k", base_url="https://broker.example.test/") as client:
        client.catalog.run(API_ID, ENDPOINT_ID)
    assert route.called


# -- runs -----------------------------------------------------------------------------------


@respx.mock
def test_runs_get_and_abort(cw: CleanedWeb) -> None:
    respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}").mock(return_value=httpx.Response(200, json=run_object()))
    abort = respx.post(f"{API_ORIGIN}/v1/runs/{RUN_ID}/abort").mock(
        return_value=httpx.Response(202, json={**run_object(), "abort_requested": True})
    )
    run = cw.runs.get(RUN_ID)
    assert run.status == "running" and run.progress["items"] == 1 and not run.ended
    aborted = cw.runs.abort(RUN_ID)
    assert aborted.abort_requested and abort.calls[0].request.headers["authorization"] == "Bearer secret-key"


@respx.mock
def test_runs_items_follows_cursor_pages(cw: CleanedWeb) -> None:
    pages = {
        "0": {"items": [{"n": 1}, {"n": 2}], "count": 2, "next_cursor": 2, "run_status": "running"},
        "2": {"items": [{"n": 3}], "count": 1, "next_cursor": 3, "run_status": "finished"},
        "3": {"items": [], "count": 0, "next_cursor": None, "run_status": "finished"},
    }
    route = respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/items").mock(
        side_effect=lambda request: httpx.Response(200, json=pages[request.url.params["cursor"]])
    )
    assert [i["n"] for i in cw.runs.items(RUN_ID, limit=2)] == [1, 2, 3]
    assert [c.request.url.params["limit"] for c in route.calls] == ["2", "2", "2"]


@respx.mock
def test_runs_items_stops_on_empty_page_unless_following(cw: CleanedWeb, sleeps: List[float]) -> None:
    responses = [
        {"items": [{"n": 1}], "next_cursor": 1, "run_status": "running"},
        {"items": [], "next_cursor": 1, "run_status": "running"},
        {"items": [{"n": 2}], "next_cursor": 2, "run_status": "running"},
        {"items": [], "next_cursor": None, "run_status": "finished"},
    ]
    route = respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/items").mock(
        side_effect=[httpx.Response(200, json=r) for r in responses]
    )
    assert [i["n"] for i in cw.runs.items(RUN_ID)] == [1]
    route.side_effect = [httpx.Response(200, json=r) for r in responses]
    assert [i["n"] for i in cw.runs.items(RUN_ID, follow=True, poll_interval=0.1)] == [1, 2]
    assert sleeps == [0.1]


@respx.mock
def test_runs_result_waits_for_the_end(cw: CleanedWeb, sleeps: List[float]) -> None:
    respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/result").mock(
        side_effect=[
            httpx.Response(202, json=run_object(), headers={"Retry-After": "5"}),
            httpx.Response(200, json=final_body()),
        ]
    )
    result = cw.runs.result(RUN_ID)
    assert result.complete and sleeps == [5.0]


@respx.mock
def test_runs_result_wait_false_returns_the_running_run(cw: CleanedWeb) -> None:
    respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/result").mock(
        return_value=httpx.Response(202, json=run_object())
    )
    result = cw.runs.result(RUN_ID, wait=False)
    assert not result.complete and result.run["status"] == "running"


@respx.mock
def test_runs_list_and_iter(cw: CleanedWeb) -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"runs": [run_object("finished")], "next_cursor": "c2"})
        return httpx.Response(
            200, json={"runs": [{**run_object("failed"), "run_id": "b" * 32}], "next_cursor": None}
        )

    route = respx.get(f"{API_ORIGIN}/v1/runs").mock(side_effect=pages)
    page = cw.runs.list(source_id="src", status="finished", limit=10)
    assert page.next_cursor == "c2" and page.runs[0].status == "finished"
    assert dict(route.calls[0].request.url.params) == {
        "limit": "10",
        "source_id": "src",
        "status": "finished",
    }
    assert [r.status for r in cw.runs.iter()] == ["finished", "failed"]


# -- sources --------------------------------------------------------------------------------


@respx.mock
def test_sources_run_200_and_202(cw: CleanedWeb, sleeps: List[float]) -> None:
    post = respx.post(f"{API_ORIGIN}/v1/sources/src_1/run").mock(
        side_effect=[httpx.Response(200, json=final_body()), httpx.Response(202, json=run_object())]
    )
    result_route = respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/result").mock(
        return_value=httpx.Response(200, json=final_body())
    )
    first = cw.sources.run("src_1", {"q": "x"}, max_items=3, expected_version="v" * 64, idempotency_key="s-1")
    assert first.complete
    body = json.loads(post.calls[0].request.content)
    assert body == {"input": {"q": "x"}, "max_items": 3, "expected_version": "v" * 64}
    second = cw.sources.run("src_1", idempotency_key="s-2")
    assert second.complete and result_route.call_count == 1 and sleeps == [30.0]


def test_sources_version_selectors_are_exclusive(cw: CleanedWeb) -> None:
    with pytest.raises(ValueError):
        cw.sources.run("src_1", expected_version="a", definition_version="b")


@respx.mock
def test_sources_version_conflict(cw: CleanedWeb) -> None:
    respx.post(f"{API_ORIGIN}/v1/sources/src_1/run").mock(
        return_value=httpx.Response(409, json={"detail": {"code": "builder_definition_version_conflict"}})
    )
    with pytest.raises(VersionConflictError):
        cw.sources.run("src_1", expected_version="v" * 64)
