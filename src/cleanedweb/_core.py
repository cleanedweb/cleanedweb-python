"""Request building, response parsing and catalog matching shared by both clients (no I/O)."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import httpx

from ._version import __version__
from .exceptions import CleanedWebError, error_from_response
from .models import ApiContract, CatalogApi, EndpointContract, RunResult

DEFAULT_BASE_URL = "https://api.cleanedweb.com"
DEFAULT_APP_URL = "https://app.cleanedweb.com"
DEFAULT_SITE_URL = "https://cleanedweb.com"
USER_AGENT = f"cleanedweb-python/{__version__}"

#: ``409`` codes of ``/result`` that only mean "not ready yet": retry with the same key.
RESULT_PENDING_CODES = frozenset({"result_not_available", "result_not_complete", "result_not_running"})
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
LIST_PAGE_SIZE = 100
MIN_POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 30.0

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def validate_idempotency_key(key: str) -> str:
    if not isinstance(key, str) or not _IDEMPOTENCY_RE.match(key):
        raise ValueError("idempotency_key must be 1-200 characters: letters, digits, '.', '_', ':', '-'")
    return key


def seg(value: str) -> str:
    """Percent-encode one path segment."""
    return quote(str(value), safe="")


def catalog_run_path(api_id: str, endpoint_id: str) -> str:
    return f"/v1/catalog/public/apis/{seg(api_id)}/endpoints/{seg(endpoint_id)}"


def build_run_body(
    *,
    input: Optional[Dict[str, Any]],
    max_items: Optional[int],
    timeout_seconds: Optional[int],
    wait_seconds: Optional[int],
    fields: Optional[Iterable[str]] = None,
    nulls: Optional[bool] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if max_items is not None and (
        isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1
    ):
        raise ValueError("max_items must be an integer >= 1")
    body: Dict[str, Any] = {"input": dict(input or {})}
    if max_items is not None:
        body["max_items"] = max_items
    if timeout_seconds is not None:
        body["timeout_seconds"] = timeout_seconds
    if wait_seconds is not None:
        body["wait_seconds"] = wait_seconds
    if fields is not None:
        body["fields"] = list(fields)
    if nulls is not None:
        body["nulls"] = nulls
    for key, value in (extra or {}).items():
        if value is not None:
            body[key] = value
    return body


def effective_wait_seconds(wait: bool, wait_seconds: Optional[int]) -> Optional[int]:
    """``wait=False`` asks the server not to hold the request: it answers ``202`` right away."""
    if not wait and wait_seconds is None:
        return 0
    return wait_seconds


def http_timeout_for(wait_seconds: Optional[int], default: float) -> float:
    """The server holds a run request up to ``wait_seconds`` (default 90): leave room for it."""
    held = 90 if wait_seconds is None else wait_seconds
    return max(default, held + 30.0)


def parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return json.loads(response.content)
    except ValueError:
        return response.text[:500]


def raise_for_response(response: httpx.Response, *, idempotency_key: Optional[str] = None) -> Any:
    """Return the JSON body of a 2xx answer, or raise the typed error."""
    body = parse_body(response)
    if response.status_code >= 300:
        if response.status_code < 400:  # a redirect means the request went to the wrong origin
            raise CleanedWebError(
                f"HTTP {response.status_code} redirect from {response.request.url}: "
                "nothing ran; check the base_url (runs go to https://api.cleanedweb.com)"
            )
        raise error_from_response(
            response.status_code, body, dict(response.headers), idempotency_key=idempotency_key
        )
    return body


def retry_delay(attempt: int, retry_after: Optional[float], max_delay: float) -> Optional[float]:
    """Seconds to wait before retrying, or ``None`` when the server asks for longer than allowed."""
    if retry_after is not None:
        return retry_after if retry_after <= max_delay else None
    return float(min(max_delay, 0.5 * (2**attempt)))


def poll_delay(retry_after: Optional[float], poll_after: Optional[float]) -> float:
    value = retry_after if retry_after is not None else poll_after
    if value is None:
        value = DEFAULT_POLL_SECONDS
    return min(MAX_POLL_SECONDS, max(MIN_POLL_SECONDS, float(value)))


def deadline_after(timeout: Optional[float]) -> Optional[float]:
    return None if timeout is None else time.monotonic() + timeout


def out_of_time(deadline: Optional[float], delay: float) -> bool:
    """True when sleeping ``delay`` more seconds would pass ``deadline`` (``None``: never)."""
    return deadline is not None and time.monotonic() + delay > deadline


def run_result(response: httpx.Response, body: Any, *, idempotency_key: Optional[str]) -> RunResult:
    if not isinstance(body, dict):
        raise CleanedWebError(f"HTTP {response.status_code} without a JSON object body")
    return RunResult.from_response(
        response.status_code, body, dict(response.headers), idempotency_key=idempotency_key
    )


# --- catalog matching -------------------------------------------------------------------------


def normalize_site(value: str) -> str:
    """``https://www.Example.com./a?b`` -> ``example.com``; a bare name stays a lowercase name.

    Drops the scheme, credentials, port, path, query, fragment, a leading ``www.`` and a trailing dot.
    """
    text = value.strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1].split(":", 1)[0].strip(".")
    return text[4:] if text.startswith("www.") else text


def _is_host(text: str) -> bool:
    return "." in text and " " not in text


def _same_site_host(needle: str, host: str) -> bool:
    """``host`` is ``needle`` or a subdomain of it, or the other way round (a page of the site)."""
    return needle == host or host.endswith("." + needle) or needle.endswith("." + host)


def _names_host(needle: str, text: str) -> bool:
    """``needle`` appears in a free-text name delimited as a host, so ``seek.com.au`` is not found
    inside ``notseek.com.au``."""
    return re.search(rf"(?<![a-z0-9.\-]){re.escape(needle)}(?![a-z0-9\-])", text) is not None


def merge_index(apis: List[CatalogApi], index: Iterable[Dict[str, Any]]) -> List[CatalogApi]:
    """Add ``slug`` and ``key_fields`` from ``catalog/index.json`` to live-list items."""
    by_id = {str(e.get("api_id")): e for e in index if isinstance(e, dict)}
    for api in apis:
        entry = by_id.get(api.api_id)
        if entry:
            api.slug = entry.get("slug") or api.slug
            api.key_fields = [str(f) for f in entry.get("key_fields") or []] or api.key_fields
    return apis


def matches_site(api: CatalogApi, site: str) -> bool:
    """Does ``api`` belong to ``site`` (a domain, a full URL, a name or a part of one)?

    A host-shaped ``site`` (it has a dot) matches the API's domain and every ``site.aliases`` host
    on label boundaries: equal, or one a subdomain of the other. ``seek.com.au`` finds an API whose
    domain is ``au.seek.com`` when ``www.seek.com.au`` is an alias, and never ``notseek.com.au``.
    The site name and the API name match as a host only on boundaries too. A name without a dot
    (``github``) is a substring of the domain, an alias, the site name or the API name.
    """
    needle = normalize_site(site)
    if not needle:
        return True
    hosts = [h for h in (normalize_site(x) for x in (api.site_domain, *api.site_aliases)) if h]
    names = [t for t in (api.site_name.lower(), api.name.lower()) if t]
    if _is_host(needle):
        return any(_same_site_host(needle, h) for h in hosts) or any(_names_host(needle, t) for t in names)
    return any(needle in t for t in (*hosts, *names))


def matches_text(api: CatalogApi, q: str) -> bool:
    words = [w for w in re.split(r"\s+", q.lower().strip()) if w]
    text = " ".join(
        [api.name, api.description, api.site_domain, *api.site_aliases, api.site_name, *api.key_fields]
    ).lower()
    return all(w in text for w in words)


def has_fields(known: Iterable[str], wanted: Iterable[str]) -> bool:
    have = {k.lower() for k in known}
    return all(w.lower() in have for w in wanted)


def contract_has_fields(contract: ApiContract, wanted: Iterable[str]) -> bool:
    return any(has_fields(e.output_fields, wanted) for e in contract.endpoints)


def split_by_fields(
    candidates: List[CatalogApi], wanted: List[str]
) -> Tuple[List[CatalogApi], List[CatalogApi]]:
    matched = [c for c in candidates if has_fields(c.key_fields, wanted)]
    unresolved = [c for c in candidates if c not in matched]
    return matched, unresolved


def find_slug(index: Iterable[Dict[str, Any]], api_id_or_slug: str) -> Optional[Dict[str, Any]]:
    for entry in index:
        if isinstance(entry, dict) and api_id_or_slug in (entry.get("slug"), entry.get("api_id")):
            return entry
    return None


def contract_from_site(data: Dict[str, Any]) -> ApiContract:
    """Build an :class:`ApiContract` from ``cleanedweb.com/catalog/<slug>/contract.json``."""
    raw_source = data.get("source")
    source: Dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    endpoints = [EndpointContract.from_dict(e) for e in data.get("endpoints") or [] if isinstance(e, dict)]
    return ApiContract(
        api_id=str(data.get("api_id", "")),
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        slug=data.get("slug"),
        site_domain=str(source.get("domain", "")),
        category=str(data.get("category", "")),
        limitations=[str(x) for x in data.get("limitations") or []],
        version=data.get("version"),
        versions=list(data.get("versions") or []),
        endpoints=endpoints,
        raw=data,
    )


def contract_from_live(detail: Dict[str, Any], contracts: List[Dict[str, Any]]) -> ApiContract:
    """Build an :class:`ApiContract` from the live API detail and its endpoint contracts."""
    api = CatalogApi.from_dict(detail)
    names = {e.endpoint_id: e for e in api.endpoints}
    endpoints = []
    for item in contracts:
        meta = names.get(str(item.get("endpoint_id", "")))
        endpoints.append(EndpointContract.from_dict(item, default_name=meta.name if meta else ""))
        if not endpoints[-1].description and meta:
            endpoints[-1].description = meta.description
    return ApiContract(
        api_id=api.api_id,
        name=api.name,
        description=api.description,
        slug=api.slug,
        site_domain=api.site_domain,
        category=api.category,
        limitations=api.limitations,
        version=contracts[0].get("version") if contracts else None,
        versions=list(contracts[0].get("versions") or []) if contracts else [],
        endpoints=endpoints,
        raw={"api": detail, "contracts": contracts},
    )
