"""Smoke test against the public, no-key catalog read endpoints only. Never runs an API.

Run with:  pytest -m live
"""

from __future__ import annotations

import pytest

from cleanedweb import CleanedWeb

pytestmark = pytest.mark.live


def test_catalog_reads_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLEANEDWEB_API_KEY", raising=False)
    with CleanedWeb(api_key=None) as cw:
        apis = cw.catalog.list(limit=5)
        assert apis and apis[0].api_id and apis[0].endpoints

        found = cw.catalog.find(site=apis[0].site_domain, limit=3)
        assert any(a.api_id == apis[0].api_id for a in found)

        slugged = [a for a in cw.catalog.find(limit=50) if a.slug]
        assert slugged, "the catalog index lists at least one API"
        contract = cw.catalog.get(slugged[0].slug or "")
        endpoint = contract.endpoint(contract.endpoints[0].endpoint_id)
        assert endpoint.endpoint_id and isinstance(endpoint.input_schema, dict)
        assert endpoint.output_schema
