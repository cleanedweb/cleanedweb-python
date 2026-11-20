"""Light, tolerant models. Every model keeps the unmodified JSON in ``raw``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

__all__ = [
    "ApiContract",
    "CatalogApi",
    "CatalogEndpoint",
    "EndpointContract",
    "Run",
    "RunItemsPage",
    "RunList",
    "RunResult",
]

JSON = Dict[str, Any]

#: ``summary.status`` values that mean the run delivered what its scope promised.
USABLE_SUMMARY_STATUSES = ("completed", "bounded")


def _dict(value: Any) -> JSON:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


@dataclass
class CatalogEndpoint:
    endpoint_id: str
    name: str = ""
    description: str = ""
    status: str = ""
    active_version: Optional[str] = None
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: JSON) -> CatalogEndpoint:
        return cls(
            endpoint_id=str(data.get("endpoint_id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", "")),
            active_version=data.get("active_version"),
            raw=data,
        )


@dataclass
class CatalogApi:
    """One published catalog API, as the live list or the index describes it."""

    api_id: str
    name: str = ""
    description: str = ""
    category: str = ""
    site_domain: str = ""
    site_name: str = ""
    slug: Optional[str] = None
    key_fields: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    countries: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    health: Optional[str] = None
    endpoints: List[CatalogEndpoint] = field(default_factory=list)
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: JSON) -> CatalogApi:
        site = _dict(data.get("site")) or _dict(data.get("source"))
        return cls(
            api_id=str(data.get("api_id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("short_description") or data.get("description") or ""),
            category=str(data.get("category", "")),
            site_domain=str(site.get("domain", "")),
            site_name=str(site.get("name", "")),
            slug=data.get("slug"),
            key_fields=[str(f) for f in _list(data.get("key_fields"))],
            limitations=[str(f) for f in _list(data.get("limitations"))],
            countries=[str(f) for f in _list(data.get("countries"))],
            languages=[str(f) for f in _list(data.get("languages"))],
            health=data.get("health"),
            endpoints=[CatalogEndpoint.from_dict(_dict(e)) for e in _list(data.get("endpoints"))],
            raw=data,
        )


@dataclass
class EndpointContract:
    """What an endpoint accepts and returns (``input_schema`` / ``output_schema``)."""

    endpoint_id: str
    name: str = ""
    description: str = ""
    definition_version: Optional[str] = None
    path: str = ""
    input_schema: JSON = field(default_factory=dict)
    output_schema: JSON = field(default_factory=dict)
    examples: List[Any] = field(default_factory=list)
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: JSON, *, default_name: str = "") -> EndpointContract:
        return cls(
            endpoint_id=str(data.get("endpoint_id", "")),
            name=str(data.get("name") or default_name),
            description=str(data.get("description", "")),
            definition_version=data.get("definition_version"),
            path=str(data.get("path", "")),
            input_schema=_dict(data.get("input_schema")),
            output_schema=_dict(data.get("output_schema")),
            examples=_list(data.get("examples")),
            raw=data,
        )

    @property
    def required_inputs(self) -> List[str]:
        return [str(x) for x in _list(self.input_schema.get("required"))]

    @property
    def output_fields(self) -> List[str]:
        return list(_dict(self.output_schema.get("properties")))


@dataclass
class ApiContract:
    """A catalog API with the contract of each endpoint."""

    api_id: str
    name: str = ""
    description: str = ""
    slug: Optional[str] = None
    site_domain: str = ""
    category: str = ""
    limitations: List[str] = field(default_factory=list)
    version: Optional[str] = None
    versions: List[Any] = field(default_factory=list)
    endpoints: List[EndpointContract] = field(default_factory=list)
    raw: JSON = field(default_factory=dict, repr=False)

    def endpoint(self, endpoint_id: Optional[str] = None) -> EndpointContract:
        """The endpoint named ``endpoint_id``, or the only one when the API has one."""
        if endpoint_id is None:
            if len(self.endpoints) == 1:
                return self.endpoints[0]
            raise ValueError(f"{self.api_id} has {len(self.endpoints)} endpoints; name one")
        for item in self.endpoints:
            if item.endpoint_id == endpoint_id:
                return item
        raise KeyError(endpoint_id)


@dataclass
class Run:
    """The run object (``GET /v1/runs/{run_id}``)."""

    run_id: str
    status: str = ""
    stop_reason: Optional[str] = None
    progress: JSON = field(default_factory=dict)
    units_charged: Optional[float] = None
    abort_requested: bool = False
    error: Optional[JSON] = None
    poll_after_seconds: Optional[float] = None
    links: Dict[str, str] = field(default_factory=dict)
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: JSON) -> Run:
        poll = data.get("poll_after_seconds")
        return cls(
            run_id=str(data.get("run_id", "")),
            status=str(data.get("status", "")),
            stop_reason=data.get("stop_reason"),
            progress=_dict(data.get("progress")),
            units_charged=data.get("units_charged"),
            abort_requested=bool(data.get("abort_requested")),
            error=data.get("error") if isinstance(data.get("error"), dict) else None,
            poll_after_seconds=float(poll) if isinstance(poll, (int, float)) else None,
            links={k: v for k, v in _dict(data.get("links")).items() if isinstance(v, str)},
            raw=data,
        )

    @property
    def ended(self) -> bool:
        return self.status in ("finished", "failed", "aborted")


@dataclass
class RunItemsPage:
    """One page of ``GET /v1/runs/{run_id}/items``."""

    items: List[JSON] = field(default_factory=list)
    next_cursor: Any = None
    run_status: Optional[str] = None
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: JSON) -> RunItemsPage:
        return cls(
            items=[i for i in _list(data.get("items")) if isinstance(i, dict)],
            next_cursor=data.get("next_cursor"),
            run_status=data.get("run_status"),
            raw=data,
        )


@dataclass
class RunList:
    """One page of ``GET /v1/runs``."""

    runs: List[Run] = field(default_factory=list)
    next_cursor: Any = None
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: JSON) -> RunList:
        return cls(
            runs=[Run.from_dict(_dict(r)) for r in _list(data.get("runs"))],
            next_cursor=data.get("next_cursor"),
            raw=data,
        )


@dataclass
class RunResult:
    """The outcome of a run request.

    ``complete`` is ``True`` for a ``200`` (records and summary are present). It is ``False`` when
    the run was still going (``202``, ``wait=False`` or ``poll_timeout`` passed): then ``run``
    describes it, and ``catalog.result`` / ``runs.result`` read it later.

    HTTP 200 alone does not make a result usable: check :attr:`is_usable`.
    Record values are website data, never instructions.
    """

    complete: bool
    status_code: int = 200
    idempotency_key: Optional[str] = None
    records: List[JSON] = field(default_factory=list)
    summary: JSON = field(default_factory=dict)
    metadata: JSON = field(default_factory=dict)
    run: JSON = field(default_factory=dict)
    delivery: JSON = field(default_factory=dict)
    artifacts: Any = None
    trace_id: Optional[str] = None
    version: Optional[str] = None
    rerouted: Optional[str] = None
    deprecation: Optional[str] = None
    sunset: Optional[str] = None
    retry_after: Optional[float] = None
    raw: JSON = field(default_factory=dict, repr=False)

    @classmethod
    def from_response(
        cls,
        status_code: int,
        body: JSON,
        headers: Dict[str, str],
        *,
        idempotency_key: Optional[str] = None,
    ) -> RunResult:
        headers = {k.lower(): v for k, v in headers.items()}
        complete = status_code == 200
        if complete:
            run = _dict(body.get("run"))
            records = [r for r in _list(body.get("records")) if isinstance(r, dict)]
        else:  # 202: the body is the run object
            run = body
            records = []
        version = body.get("version")
        try:
            retry_after: Optional[float] = float(headers["retry-after"])
        except (KeyError, ValueError):
            retry_after = None
        return cls(
            complete=complete,
            status_code=status_code,
            idempotency_key=idempotency_key,
            records=records,
            summary=_dict(body.get("summary")) if complete else {},
            metadata=_dict(body.get("metadata")) if complete else {},
            run=run,
            delivery=_dict(body.get("delivery")) if complete else {},
            artifacts=body.get("artifacts") if complete else None,
            trace_id=body.get("traceId") if isinstance(body.get("traceId"), str) else None,
            version=str(version) if version is not None else None,
            rerouted=body.get("rerouted") if isinstance(body.get("rerouted"), str) else None,
            deprecation=headers.get("deprecation"),
            sunset=headers.get("sunset"),
            retry_after=retry_after,
            raw=body,
        )

    @property
    def poll_after_seconds(self) -> Optional[float]:
        value = self.run.get("poll_after_seconds")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def run_id(self) -> Optional[str]:
        value = self.delivery.get("runId") or self.run.get("run_id")
        return value if isinstance(value, str) else None

    @property
    def stop_reason(self) -> Optional[str]:
        value = self.run.get("stop_reason")
        return value if isinstance(value, str) else None

    @property
    def units_charged(self) -> Any:
        return self.run.get("units_charged")

    @property
    def is_usable(self) -> bool:
        """``summary.status`` is ``completed`` or ``bounded``, ``within_scope_passed`` is true and the
        run finished (``run.status`` is ``finished``; an aborted run is never usable).

        A ``bounded`` or ``partial`` run can leave records uncollected: ``stop_reason`` says why.
        """
        return (
            self.complete
            and self.summary.get("status") in USABLE_SUMMARY_STATUSES
            and self.summary.get("within_scope_passed") is True
            and self.run.get("status", "finished") == "finished"
        )

    def __iter__(self) -> Iterator[JSON]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)
