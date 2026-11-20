from __future__ import annotations

from typing import Any, Dict, List

import pytest

API_ORIGIN = "https://api.cleanedweb.com"
APP = "https://app.cleanedweb.com"
SITE = "https://cleanedweb.com"
API_ID = "generated_aaaaaaaaaaaaaaaaaaaaaaaa"
ENDPOINT_ID = f"{API_ID}_search"
RUN_BASE = f"{API_ORIGIN}/v1/catalog/public/apis/{API_ID}/endpoints/{ENDPOINT_ID}"
RUN_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    """Record sleeps instead of waiting."""
    recorded: List[float] = []

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    async def fake_async_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("cleanedweb._sync.time.sleep", fake_sleep)
    monkeypatch.setattr("cleanedweb._async.asyncio.sleep", fake_async_sleep)
    return recorded


def final_body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "records": [{"title": "a", "url": "https://x.test/a"}, {"title": "b", "url": "https://x.test/b"}],
        "summary": {"status": "bounded", "within_scope_passed": True, "records": 2},
        "metadata": {"definition_version": "d" * 64},
        "run": {"run_id": RUN_ID, "status": "finished", "stop_reason": "max_items", "units_charged": 2},
        "delivery": {"runId": RUN_ID, "chargeReceiptId": RUN_ID, "recordCount": 2},
        "traceId": None,
        "version": "1.0",
    }
    body.update(overrides)
    return body


def run_object(status: str = "running") -> Dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "status": status,
        "stop_reason": None,
        "progress": {"pages": 1, "items": 1, "duplicates": 0},
        "units_charged": 1,
        "poll_after_seconds": 30,
        "links": {"self": f"/v1/runs/{RUN_ID}", "result": f"/v1/runs/{RUN_ID}/result"},
    }


LIVE_ITEMS = [
    {
        "api_id": API_ID,
        "name": "GitHub Topics Repositories",
        "short_description": "Repos under a topic",
        "category": "uncategorized",
        "site": {"name": "GitHub", "domain": "github.com"},
        "endpoints": [{"endpoint_id": ENDPOINT_ID, "name": "Extract records", "status": "available"}],
    },
    {
        "api_id": "generated_bbbbbbbbbbbbbbbbbbbbbbbb",
        "name": "Blocket Cars",
        "short_description": "Cars on blocket",
        "category": "commerce",
        "site": {"name": "Blocket", "domain": "www.blocket.se"},
        "endpoints": [
            {
                "endpoint_id": "generated_bbbbbbbbbbbbbbbbbbbbbbbb_search",
                "name": "Search",
                "status": "available",
            }
        ],
    },
]

INDEX = {
    "apis": [
        {
            "name": "GitHub Topics Repositories API",
            "api_id": API_ID,
            "slug": "github-topics-repositories-api",
            "source": {"name": "GitHub", "domain": "github.com"},
            "key_fields": ["title", "url", "description"],
        }
    ]
}

SITE_CONTRACT = {
    "name": "GitHub Topics Repositories API",
    "api_id": API_ID,
    "slug": "github-topics-repositories-api",
    "description": "Repos",
    "source": {"name": "GitHub", "domain": "github.com"},
    "category": "uncategorized",
    "limitations": [],
    "version": "1.0",
    "versions": [{"major": 1, "latest_minor": "1.0", "status": "current"}],
    "endpoints": [
        {
            "endpoint_id": ENDPOINT_ID,
            "name": "Extract records",
            "definition_version": "d" * 64,
            "path": f"/v1/catalog/public/apis/{API_ID}/endpoints/{ENDPOINT_ID}/run",
            "input_schema": {"type": "object", "properties": {"sort": {"type": "string"}}, "required": []},
            "output_schema": {
                "type": "object",
                "properties": {"title": {}, "url": {}, "stargazer_count": {}},
                "required": ["title", "url"],
            },
            "examples": [],
        }
    ],
}
