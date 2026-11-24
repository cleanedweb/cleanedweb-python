"""``SeenStore``: identity, persistence, atomic writes, and ``sources.run(new_only=...)``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import respx

from cleanedweb import AsyncCleanedWeb, CleanedWeb, SeenStore
from cleanedweb.seen import identity_paths_from_contract, record_key

from .conftest import API_ORIGIN, RUN_ID, SOURCE_ID, VERSION, final_body

RUN_URL = f"{API_ORIGIN}/v1/sources/{SOURCE_ID}/run"
CONTRACT_URL = f"{API_ORIGIN}/v1/sources/{SOURCE_ID}/versions/{VERSION}/contract"


def rec(url: str, **extra: Any) -> Dict[str, Any]:
    return {"title": url[-1], "url": url, **extra}


def test_identity_paths_come_from_the_contract_marker() -> None:
    one = {"openapi": {"x-cleanedweb-contract": {"record_identity_path": "url"}}}
    many = {
        "openapi": {
            "x-cleanedweb-contract": {"record_identity_paths": ["a", "b"], "record_identity_path": "x"}
        }
    }
    assert identity_paths_from_contract(one) == ("url",)
    assert identity_paths_from_contract(many) == ("a", "b")
    assert identity_paths_from_contract({"openapi": {}}) == ()
    assert identity_paths_from_contract(None) == ()


def test_record_key_uses_declared_identity_else_a_whole_record_hash() -> None:
    a, declared = record_key({"url": "u", "n": 1}, ("url",))
    b, _ = record_key({"url": "u", "n": 2}, ("url",))
    assert declared and a == b  # a changed field is the same record
    nested, ok = record_key({"entity": {"url": "u"}}, ("entity.url",))
    assert ok and nested == a  # the key is the identity value, not the path that found it
    h1, d1 = record_key({"url": "u", "n": 1}, ())
    h2, d2 = record_key({"n": 1, "url": "u"}, ())
    h3, _ = record_key({"url": "u", "n": 2}, ())
    assert not d1 and not d2 and h1 == h2 != h3  # stable under key order, sensitive to fields
    missing, miss_declared = record_key({"n": 1}, ("url",))
    assert not miss_declared and missing == record_key({"n": 1}, ())[0]


def test_filter_new_dedupes_within_a_batch_and_across_namespaces(tmp_path: Path) -> None:
    store = SeenStore()
    first = store.filter_new("src1", [rec("a"), rec("b"), rec("a")], ("url",))
    assert [r["url"] for r in first] == ["a", "b"] and first.seen_count == 1
    assert store.filter_new("src1", [rec("a"), rec("c")], ("url",)).seen_count == 1
    assert len(store.filter_new("src2", [rec("a")], ("url",))) == 1  # another source: new
    assert store.count("src1") == 3


def test_mixed_identity_is_reported() -> None:
    store = SeenStore()
    fresh = store.filter_new("s", [{"url": "a"}, {"title": "no url"}], ("url",))
    assert fresh.identity == "mixed"


@pytest.mark.parametrize("name", ["seen.json", "seen.sqlite", "seen.db"])
def test_store_persists_between_objects(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    SeenStore(path).filter_new("s", [rec("a"), rec("b")], ("url",))
    again = SeenStore(str(path)).filter_new("s", [rec("b"), rec("c")], ("url",))
    assert [r["url"] for r in again] == ["c"]
    assert SeenStore(path).count("s") == 3


def test_json_store_is_written_atomically_and_merges_another_writer(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    one, two = SeenStore(path), SeenStore(path)
    one.filter_new("s", [rec("https://x.test/secret-a")], ("url",))
    two.filter_new("s", [rec("b")], ("url",))  # a second object on the same file
    assert one.count("s") == 2, "the first object sees what the second wrote"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["seen.json"], "no temp files are left behind"
    data = json.loads(path.read_text())
    assert data["version"] == 1 and len(data["seen"]["s"]) == 2
    assert "secret" not in path.read_text(), "only digests are stored, never record content"


def test_a_failed_write_leaves_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "seen.json"
    store = SeenStore(path)
    store.filter_new("s", [rec("a")], ("url",))
    before = path.read_text()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.filter_new("s", [rec("b")], ("url",))
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["seen.json"]


def test_a_file_that_is_not_a_store_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text('{"hello": 1}')
    with pytest.raises(ValueError):
        SeenStore(path)


def test_mark_false_then_commit() -> None:
    store = SeenStore()
    pending = store.filter_new("s", [rec("a")], ("url",), mark=False)
    assert len(store.filter_new("s", [rec("a")], ("url",), mark=False)) == 1
    pending.commit()
    pending.commit()
    assert len(store.filter_new("s", [rec("a")], ("url",))) == 0


# -- sources.run(new_only=...) ------------------------------------------------------------------


def run_with(*urls: str) -> httpx.Response:
    body = final_body(
        records=[rec(u, posted="now") for u in urls],
        metadata={"definition_version": VERSION},
    )
    return httpx.Response(200, json=body)


CONTRACT = {"openapi": {"x-cleanedweb-contract": {"record_identity_path": "url"}}}


@respx.mock
def test_sources_run_new_only_across_two_runs(tmp_path: Path) -> None:
    contract = respx.get(CONTRACT_URL).mock(return_value=httpx.Response(200, json=CONTRACT))
    respx.post(RUN_URL).mock(side_effect=[run_with("a", "b"), run_with("b", "c")])
    store = SeenStore(tmp_path / "seen.json")
    with CleanedWeb(api_key="k") as cw:
        first = cw.sources.run(SOURCE_ID, {}, new_only=store)
        second = cw.sources.run(SOURCE_ID, {}, new_only=str(tmp_path / "seen.json"))
    assert [r["url"] for r in first.new_records or []] == ["a", "b"] and first.seen_count == 0
    assert [r["url"] for r in second.new_records or []] == ["c"] and second.seen_count == 1
    assert len(second.records) == 2, "records still holds everything the run delivered (and billed)"
    assert contract.call_count == 1, "the contract is read once per version"
    assert contract.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
def test_sources_run_without_new_only_has_no_new_records() -> None:
    respx.post(RUN_URL).mock(return_value=run_with("a"))
    with CleanedWeb(api_key="k") as cw:
        result = cw.sources.run(SOURCE_ID, {})
    assert result.new_records is None and result.seen_count is None


@respx.mock
def test_sources_run_new_only_follows_a_202_and_marks_late(tmp_path: Path) -> None:
    respx.get(CONTRACT_URL).mock(return_value=httpx.Response(200, json=CONTRACT))
    respx.post(RUN_URL).mock(return_value=httpx.Response(202, json={"run_id": RUN_ID, "status": "running"}))
    respx.get(f"{API_ORIGIN}/v1/runs/{RUN_ID}/result").mock(return_value=run_with("a"))
    store = SeenStore()
    with CleanedWeb(api_key="k") as cw:
        result = cw.sources.run(SOURCE_ID, {}, new_only=store, mark_seen=False)
        assert len(result.new_records or []) == 1 and store.count(SOURCE_ID) == 0
        (result.new_records or None).commit()  # type: ignore[union-attr]
    assert store.count(SOURCE_ID) == 1


def test_new_only_needs_wait() -> None:
    with CleanedWeb(api_key="k") as cw, pytest.raises(ValueError):
        cw.sources.run(SOURCE_ID, {}, wait=False, new_only=SeenStore())


@respx.mock
def test_new_only_falls_back_to_a_hash_when_the_contract_is_not_readable() -> None:
    respx.get(CONTRACT_URL).mock(
        return_value=httpx.Response(403, json={"detail": {"code": "api_key_forbidden"}})
    )
    respx.post(RUN_URL).mock(side_effect=[run_with("a"), run_with("a")])
    store = SeenStore()
    with CleanedWeb(api_key="k") as cw:
        assert len(cw.sources.run(SOURCE_ID, {}, new_only=store).new_records or []) == 1
        assert len(cw.sources.run(SOURCE_ID, {}, new_only=store).new_records or []) == 0


@respx.mock
def test_a_server_error_reading_the_contract_is_not_hidden() -> None:
    """A 503 must not silently switch to hash keys: every record would look new."""
    respx.get(CONTRACT_URL).mock(return_value=httpx.Response(503, json={"detail": {"code": "x"}}))
    respx.post(RUN_URL).mock(return_value=run_with("a"))
    with CleanedWeb(api_key="k", max_retries=0) as cw:
        with pytest.raises(Exception) as err:
            cw.sources.run(SOURCE_ID, {}, new_only=SeenStore())
    assert getattr(err.value, "status_code", None) == 503


@respx.mock
async def test_async_sources_run_new_only(tmp_path: Path) -> None:
    respx.get(CONTRACT_URL).mock(return_value=httpx.Response(200, json=CONTRACT))
    respx.post(RUN_URL).mock(side_effect=[run_with("a", "b"), run_with("b", "c")])
    store = SeenStore(tmp_path / "seen.sqlite")
    async with AsyncCleanedWeb(api_key="k") as cw:
        first = await cw.sources.run(SOURCE_ID, {}, new_only=store)
        second = await cw.sources.run(SOURCE_ID, {}, new_only=store)
        with pytest.raises(ValueError):
            await cw.sources.run(SOURCE_ID, {}, wait=False, new_only=store)
    assert [r["url"] for r in first.new_records or []] == ["a", "b"]
    assert [r["url"] for r in second.new_records or []] == ["c"] and second.seen_count == 1
