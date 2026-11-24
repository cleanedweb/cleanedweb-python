"""The synchronous client."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union

import httpx

from . import _core as core
from ._apis_sync import Apis
from ._schedules_sync import Schedules
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    CleanedWebError,
    MissingAPIKeyError,
    error_from_response,
)
from .models import (
    ApiContract,
    CatalogApi,
    Run,
    RunItemsPage,
    RunList,
    RunResult,
)
from .seen import SeenStore, identity_paths_from_contract

__all__ = ["CleanedWeb"]


class CleanedWeb:
    """Client for the CleanedWeb catalog and run APIs.

    ``api_key`` defaults to ``CLEANEDWEB_API_KEY``. Reading the catalog (``catalog.list``,
    ``get``, ``find``) needs no key; running and schedules do (scope ``run``; ``schedules.list`` and
    ``get`` need ``read``). Building an API from a URL (``apis``) needs a key with the creation
    scopes ``discover`` and ``build``. The key is only ever sent to ``base_url`` (runs, schedules)
    and, for ``apis``, to ``app_url``; never to the catalog read origins.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        app_url: Optional[str] = None,
        site_url: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_retry_delay: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("CLEANEDWEB_API_KEY") or None
        self.base_url = (base_url or os.environ.get("CLEANEDWEB_RUN_BASE") or core.DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.app_url = (app_url or core.DEFAULT_APP_URL).rstrip("/")
        self.site_url = (site_url or core.DEFAULT_SITE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_delay = max_retry_delay
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._index_cache: Optional[List[Dict[str, Any]]] = None
        self._identity_cache: Dict[Tuple[str, str], Tuple[str, ...]] = {}
        self.catalog = Catalog(self)
        self.runs = Runs(self)
        self.sources = Sources(self)
        self.apis = Apis(self)
        self.schedules = Schedules(self)

    def __repr__(self) -> str:
        return f"CleanedWeb(base_url={self.base_url!r}, api_key={'set' if self._api_key else 'unset'})"

    def __enter__(self) -> CleanedWeb:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # -- transport ---------------------------------------------------------------------------

    def _headers(self, auth: bool, idempotency_key: Optional[str]) -> Dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": core.USER_AGENT}
        if auth:
            if not self._api_key:
                raise MissingAPIKeyError(
                    "This call needs an API key (scope `run`; building APIs needs `discover` and `build`): "
                    "pass api_key= or set CLEANEDWEB_API_KEY"
                )
            headers["Authorization"] = f"Bearer {self._api_key}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _send(
        self,
        method: str,
        url: str,
        *,
        auth: bool = False,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> tuple[httpx.Response, Any]:
        headers = self._headers(auth, idempotency_key)
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout if timeout is not None else self.timeout,
                )
            except httpx.HTTPError as exc:
                raise APIConnectionError(
                    f"{type(exc).__name__}: {exc}. The request may have reached the server"
                    + ("; reuse the same idempotency_key to recover it" if idempotency_key else ""),
                    idempotency_key=idempotency_key,
                ) from exc
            if response.status_code in core.RETRYABLE_STATUSES and attempt < self.max_retries:
                error = error_from_response(
                    response.status_code, core.parse_body(response), dict(response.headers)
                )
                delay = (
                    None
                    if error.run_failed
                    else core.retry_delay(attempt, error.retry_after, self.max_retry_delay)
                )
                if delay is not None:
                    time.sleep(delay)
                    attempt += 1
                    continue
            body = core.raise_for_response(response, idempotency_key=idempotency_key)
            return response, body

    def _get_json(self, url: str, **kwargs: Any) -> Any:
        return self._send("GET", url, **kwargs)[1]

    def _index(self) -> List[Dict[str, Any]]:
        if self._index_cache is None:
            data = self._get_json(f"{self.site_url}/catalog/index.json")
            apis = data.get("apis") if isinstance(data, dict) else None
            self._index_cache = [a for a in apis or [] if isinstance(a, dict)]
        return self._index_cache

    def _poll(
        self,
        result: RunResult,
        fetch: Any,
        poll_timeout: Optional[float],
    ) -> RunResult:
        """Read ``fetch()`` until the run ends. Never resends the run request."""
        deadline = None if poll_timeout is None else time.monotonic() + poll_timeout
        while not result.complete:
            delay = core.poll_delay(result.retry_after, result.poll_after_seconds)
            if deadline is not None and time.monotonic() + delay > deadline:
                return result
            time.sleep(delay)
            try:
                result = fetch()
            except APIStatusError as exc:
                if exc.status_code == 409 and exc.code in core.RESULT_PENDING_CODES:
                    result = RunResult(
                        complete=False,
                        status_code=202,
                        idempotency_key=result.idempotency_key,
                        run=result.run,
                        retry_after=exc.retry_after,
                    )
                    continue
                raise
        return result


class Catalog:
    def __init__(self, client: CleanedWeb) -> None:
        self._c = client

    def list(
        self,
        *,
        q: Optional[str] = None,
        category: Optional[str] = None,
        sort: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[CatalogApi]:
        """One page of the live catalog list (no key needed)."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        for key, value in (("q", q), ("category", category), ("sort", sort)):
            if value:
                params[key] = value
        data = self._c._get_json(f"{self._c.app_url}/api/catalog/apis", params=params)
        items = data.get("items") if isinstance(data, dict) else None
        return [CatalogApi.from_dict(i) for i in items or [] if isinstance(i, dict)]

    def iter(
        self,
        *,
        q: Optional[str] = None,
        category: Optional[str] = None,
        sort: Optional[str] = None,
        page_size: int = core.LIST_PAGE_SIZE,
    ) -> Iterator[CatalogApi]:
        """Every API of the live list, page by page."""
        offset = 0
        while True:
            page = self.list(q=q, category=category, sort=sort, limit=page_size, offset=offset)
            yield from page
            if len(page) < page_size:
                return
            offset += page_size

    def get(self, api_id_or_slug: str) -> ApiContract:
        """The contract of one API: inputs, output fields, saved samples, per endpoint.

        Takes an ``api_id`` or a catalog slug. Uses the catalog site's ``contract.json`` when the
        API is in the site index, otherwise the live detail and endpoint contracts.
        """
        try:
            entry = core.find_slug(self._c._index(), api_id_or_slug)
        except CleanedWebError:
            entry = None
        if entry and entry.get("slug"):
            data = self._c._get_json(f"{self._c.site_url}/catalog/{core.seg(entry['slug'])}/contract.json")
            return core.contract_from_site(data)
        detail = self._c._get_json(f"{self._c.app_url}/api/catalog/apis/{core.seg(api_id_or_slug)}")
        contracts = []
        for endpoint in CatalogApi.from_dict(detail).endpoints:
            url = (
                f"{self._c.app_url}/api/catalog/apis/{core.seg(api_id_or_slug)}"
                f"/endpoints/{core.seg(endpoint.endpoint_id)}/contract"
            )
            contracts.append(self._c._get_json(url))
        return core.contract_from_live(detail, contracts)

    def find(
        self,
        *,
        site: Optional[str] = None,
        fields: Optional[Iterable[str]] = None,
        q: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
        deep: bool = True,
        max_contract_checks: int = 20,
    ) -> List[CatalogApi]:
        """Published APIs for a site (domain, URL or name) that return the given fields.

        A client-side filter over the live list, joined with the catalog index (which adds
        ``slug`` and ``key_fields``). Field names are matched against ``key_fields``; with
        ``deep`` the contracts of the remaining candidates (at most ``max_contract_checks``)
        are read and their full ``output_schema`` is checked. Nothing runs.
        """
        candidates = list(self._c.catalog.iter(q=q, category=category))
        try:
            core.merge_index(candidates, self._c._index())
        except CleanedWebError:
            pass  # the index only adds slug and key_fields
        if site:
            candidates = [c for c in candidates if core.matches_site(c, site)]
        wanted = [f for f in (fields or []) if f]
        if not wanted:
            return candidates[:limit]
        matched, unresolved = core.split_by_fields(candidates, wanted)
        if deep:
            for api in unresolved[:max_contract_checks]:
                if len(matched) >= limit:
                    break
                try:
                    contract = self.get(api.slug or api.api_id)
                except CleanedWebError:
                    continue
                if core.contract_has_fields(contract, wanted):
                    matched.append(api)
        return matched[:limit]

    def run(
        self,
        api_id: str,
        endpoint_id: str,
        input: Optional[Dict[str, Any]] = None,
        *,
        max_items: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        wait: bool = True,
        wait_seconds: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        fields: Optional[Iterable[str]] = None,
        nulls: Optional[bool] = None,
        version: Optional[int] = None,
        poll_timeout: Optional[float] = None,
    ) -> RunResult:
        """Run a catalog endpoint. Charged per record delivered; start with a small ``max_items``.

        ``idempotency_key`` is generated when omitted and returned as ``result.idempotency_key``:
        save it before you rely on the call. Resending the same key with the same request returns
        the same run, never a second one. On ``202`` and ``wait=True`` the run is read from
        ``/result`` with that key every ``Retry-After`` seconds until it ends (or ``poll_timeout``
        seconds pass, which returns an incomplete result, ``complete=False``). The POST is never
        resent while waiting. ``wait=False`` returns right after the request is accepted.
        Pin a major with ``version``. Raises a typed error for an API error; see
        :mod:`cleanedweb.exceptions`.
        """
        key = (
            core.validate_idempotency_key(idempotency_key) if idempotency_key else core.new_idempotency_key()
        )
        wait_s = core.effective_wait_seconds(wait, wait_seconds)
        body = core.build_run_body(
            input=input,
            max_items=max_items,
            timeout_seconds=timeout_seconds,
            wait_seconds=wait_s,
            fields=fields,
            nulls=nulls,
            extra={"version": version},
        )
        base = f"{self._c.base_url}{core.catalog_run_path(api_id, endpoint_id)}"
        response, payload = self._c._send(
            "POST",
            f"{base}/run",
            auth=True,
            json=body,
            idempotency_key=key,
            timeout=core.http_timeout_for(wait_s, self._c.timeout),
        )
        result = core.run_result(response, payload, idempotency_key=key)
        if result.complete or not wait:
            return result
        return self._c._poll(result, lambda: self.result(api_id, endpoint_id, key, wait=False), poll_timeout)

    def result(
        self,
        api_id: str,
        endpoint_id: str,
        idempotency_key: str,
        *,
        wait: bool = True,
        poll_timeout: Optional[float] = None,
    ) -> RunResult:
        """Read the result of an earlier ``run`` with the same key (never collects again).

        Use it to recover after a timeout or a lost response. Returns an incomplete result
        (``complete=False``) while the run goes on and ``wait`` is false or ``poll_timeout`` passed.
        """
        key = core.validate_idempotency_key(idempotency_key)
        url = f"{self._c.base_url}{core.catalog_run_path(api_id, endpoint_id)}/result"

        def fetch() -> RunResult:
            response, payload = self._c._send(
                "GET",
                url,
                auth=True,
                idempotency_key=key,
                timeout=core.http_timeout_for(None, self._c.timeout),
            )
            return core.run_result(response, payload, idempotency_key=key)

        result = fetch()
        if result.complete or not wait:
            return result
        return self._c._poll(result, fetch, poll_timeout)


class Runs:
    def __init__(self, client: CleanedWeb) -> None:
        self._c = client

    def _url(self, suffix: str = "") -> str:
        return f"{self._c.base_url}/v1/runs{suffix}"

    def get(self, run_id: str) -> Run:
        return Run.from_dict(self._c._get_json(self._url(f"/{core.seg(run_id)}"), auth=True))

    def items_page(self, run_id: str, *, cursor: Any = 0, limit: int = 100) -> RunItemsPage:
        data = self._c._get_json(
            self._url(f"/{core.seg(run_id)}/items"), auth=True, params={"cursor": cursor, "limit": limit}
        )
        return RunItemsPage.from_dict(data if isinstance(data, dict) else {})

    def items(
        self,
        run_id: str,
        *,
        limit: int = 100,
        cursor: Any = 0,
        follow: bool = False,
        poll_interval: float = 2.0,
    ) -> Iterator[Dict[str, Any]]:
        """Iterate the records committed so far, following ``next_cursor`` page by page.

        Readable while the run runs. With ``follow=True`` it keeps waiting (``poll_interval``
        seconds) for new records until the run ended and every record was read; otherwise it
        stops at the first empty page.
        """
        while True:
            page = self.items_page(run_id, cursor=cursor, limit=limit)
            yield from page.items
            if page.next_cursor is None:
                return
            if not page.items:
                if not follow:
                    return
                time.sleep(poll_interval)
            cursor = page.next_cursor

    def result(self, run_id: str, *, wait: bool = True, poll_timeout: Optional[float] = None) -> RunResult:
        """The final body of a run (``200``), or while it runs an incomplete result (``202``).
        With ``wait`` it polls every ``Retry-After`` seconds until the run ends."""
        url = self._url(f"/{core.seg(run_id)}/result")

        def fetch() -> RunResult:
            response, payload = self._c._send("GET", url, auth=True)
            return core.run_result(response, payload, idempotency_key=None)

        result = fetch()
        if result.complete or not wait:
            return result
        return self._c._poll(result, fetch, poll_timeout)

    def abort(self, run_id: str) -> Run:
        """Stop a run (scope ``run``). Records committed so far stay readable."""
        _, data = self._c._send("POST", self._url(f"/{core.seg(run_id)}/abort"), auth=True)
        return Run.from_dict(data if isinstance(data, dict) else {})

    def list(
        self,
        *,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
        cursor: Any = None,
        limit: int = 50,
    ) -> RunList:
        """Your runs, newest first (one page)."""
        params: Dict[str, Any] = {"limit": limit}
        for key, value in (("source_id", source_id), ("status", status), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        data = self._c._get_json(self._url(), auth=True, params=params)
        return RunList.from_dict(data if isinstance(data, dict) else {})

    def iter(
        self, *, source_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50
    ) -> Iterator[Run]:
        """Every run, following ``next_cursor``."""
        cursor: Any = None
        while True:
            page = self.list(source_id=source_id, status=status, cursor=cursor, limit=limit)
            yield from page.runs
            if page.next_cursor is None or not page.runs:
                return
            cursor = page.next_cursor


class Sources:
    def __init__(self, client: CleanedWeb) -> None:
        self._c = client

    def run(
        self,
        source_id: str,
        input: Optional[Dict[str, Any]] = None,
        *,
        max_items: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        wait: bool = True,
        wait_seconds: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        expected_version: Optional[str] = None,
        definition_version: Optional[str] = None,
        endpoint: Optional[str] = None,
        fields: Optional[Iterable[str]] = None,
        nulls: Optional[bool] = None,
        poll_timeout: Optional[float] = None,
        base_url: Optional[str] = None,
        new_only: Union[SeenStore, str, os.PathLike[str], None] = None,
        mark_seen: bool = True,
    ) -> RunResult:
        """Run one of your own private APIs (``POST /v1/sources/{source_id}/run``).

        Same idempotency and waiting behavior as ``catalog.run``; a ``202`` is followed with
        ``GET /v1/runs/{run_id}/result``. ``expected_version`` (the 64-character definition
        version) makes the call fail with ``VersionConflictError`` if the active definition
        changed; it cannot be combined with ``definition_version``. ``base_url`` overrides the
        origin your API's connection details name.

        ``new_only`` (a :class:`~cleanedweb.seen.SeenStore` or a path for one) keeps the records
        you have not seen before in ``result.new_records`` (``result.seen_count`` counts the rest);
        they are marked seen unless ``mark_seen=False`` (then call ``result.new_records.commit()``
        once handled). ``result.records`` still holds everything the run delivered, and the run is
        charged for all of it. It needs ``wait=True`` and a finished result.
        """
        if new_only is not None and not wait:
            raise ValueError("new_only needs wait=True: the records are read from the finished run")
        if expected_version is not None and definition_version is not None:
            raise ValueError("pass expected_version or definition_version, not both")
        key = (
            core.validate_idempotency_key(idempotency_key) if idempotency_key else core.new_idempotency_key()
        )
        wait_s = core.effective_wait_seconds(wait, wait_seconds)
        body = core.build_run_body(
            input=input,
            max_items=max_items,
            timeout_seconds=timeout_seconds,
            wait_seconds=wait_s,
            fields=fields,
            nulls=nulls,
            extra={
                "expected_version": expected_version,
                "definition_version": definition_version,
                "endpoint": endpoint,
            },
        )
        origin = (base_url or self._c.base_url).rstrip("/")
        response, payload = self._c._send(
            "POST",
            f"{origin}/v1/sources/{core.seg(source_id)}/run",
            auth=True,
            json=body,
            idempotency_key=key,
            timeout=core.http_timeout_for(wait_s, self._c.timeout),
        )
        result = core.run_result(response, payload, idempotency_key=key)
        if result.complete or not wait:
            return (
                self._only_new(result, source_id, new_only, mark_seen, origin)
                if new_only is not None
                else result
            )
        run_id = result.run_id
        if not run_id:
            raise CleanedWebError("the 202 answer carried no run_id to read the result with")

        def fetch() -> RunResult:
            resp, data = self._c._send("GET", f"{origin}/v1/runs/{core.seg(run_id)}/result", auth=True)
            return core.run_result(resp, data, idempotency_key=key)

        result = self._c._poll(result, fetch, poll_timeout)
        return (
            self._only_new(result, source_id, new_only, mark_seen, origin) if new_only is not None else result
        )

    def _only_new(
        self,
        result: RunResult,
        source_id: str,
        new_only: Union[SeenStore, str, os.PathLike[str]],
        mark_seen: bool,
        origin: str,
    ) -> RunResult:
        if not result.complete:
            return result
        store = new_only if isinstance(new_only, SeenStore) else SeenStore(new_only)
        paths = self.identity_paths(source_id, result.metadata.get("definition_version"), base_url=origin)
        fresh = store.filter_new(source_id, result.records, paths, mark=mark_seen)
        result.new_records = fresh
        result.seen_count = fresh.seen_count
        return result

    def identity_paths(
        self, source_id: str, version: Optional[str], *, base_url: Optional[str] = None
    ) -> Tuple[str, ...]:
        """The record identity paths the version's contract declares, or ``()`` (no ``read``
        scope, unknown version, or none declared). Cached per client."""
        if not isinstance(version, str) or not version:
            return ()
        cached = self._c._identity_cache.get((source_id, version))
        if cached is not None:
            return cached
        origin = (base_url or self._c.base_url).rstrip("/")
        url = f"{origin}/v1/sources/{core.seg(source_id)}/versions/{core.seg(version)}/contract"
        try:
            contract = self._c._get_json(url, auth=True)
        except APIStatusError as exc:
            if exc.status_code in (403, 404, 409, 422):  # a 5xx must not silently change the keys
                return ()
            raise
        paths = identity_paths_from_contract(contract)
        self._c._identity_cache[(source_id, version)] = paths
        return paths
