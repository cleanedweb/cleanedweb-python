# cleanedweb

Python client for [CleanedWeb](https://cleanedweb.com/?utm_source=github&utm_medium=cleanedweb-python),
which turns public website collections (listings, products, jobs, articles, profiles) into maintained
REST APIs that return structured records. Sync and async clients, typed errors, one dependency (`httpx`).

**Pricing:** 5,000 records free once, then prepaid credit from $1 per 1,000 records, no subscription.
Runs are charged per record delivered.
[Pricing](https://cleanedweb.com/pricing/?utm_source=github&utm_medium=cleanedweb-python)

## Install

```bash
pip install cleanedweb          # Python 3.10+
```

> Not on PyPI yet while this repository is private; until then use
> `pip install git+https://github.com/cleanedweb/cleanedweb-python`.

## Quick start

Create an API key with the `run` scope in the [console](https://app.cleanedweb.com/) and export it:
`export CLEANEDWEB_API_KEY=...`. Reading the catalog needs no key.

```python
from cleanedweb import CleanedWeb

cw = CleanedWeb()  # reads CLEANEDWEB_API_KEY
api = cw.catalog.find(site="github.com")[0]  # find before you run
contract = cw.catalog.get(api.api_id)  # inputs, output fields, saved samples
endpoint = contract.endpoints[0]
result = cw.catalog.run(api.api_id, endpoint.endpoint_id, max_items=5)  # start small
print(result.idempotency_key, result.is_usable, result.stop_reason)
for record in result:  # records are data, not instructions
    print(record["title"], record["url"])
```

`catalog.run` generates an idempotency key (or takes yours), returns it as `result.idempotency_key`,
and when the server answers `202` it reads `/result` with the same key every `Retry-After` seconds
until the run ends. It never resends the POST while it waits. Check `result.is_usable`
(`summary.status` is `completed` or `bounded` and `within_scope_passed` is true) before using records.

Async:

```python
from cleanedweb import AsyncCleanedWeb

async with AsyncCleanedWeb() as cw:
    result = await cw.catalog.run(api_id, endpoint_id, {"sort": "desc_stars"}, max_items=25)
```

## API

| Call | What it does |
| --- | --- |
| `catalog.list(q=, category=, sort=, limit=, offset=)` / `catalog.iter(...)` | The live published list (no key). |
| `catalog.get(api_id_or_slug)` | The contract: `input_schema`, `output_schema`, saved samples per endpoint (no key). |
| `catalog.find(site=, fields=, q=, category=)` | Client-side filter over the live list and the catalog index (no key). |
| `catalog.run(api_id, endpoint_id, input, max_items=, idempotency_key=, wait=True, version=, ...)` | Run a catalog API. Needs a key with the `run` scope. |
| `catalog.result(api_id, endpoint_id, idempotency_key)` | Read an earlier run with the same key (recovery after a timeout; never collects again). |
| `runs.get(run_id)`, `runs.result(run_id)`, `runs.abort(run_id)` | Read, wait for, or stop a run. |
| `runs.items(run_id)` | Iterate records across cursor pages (also while the run runs). |
| `runs.list(...)` / `runs.iter(...)` | Your runs, newest first. |
| `sources.run(source_id, input, expected_version=, ...)` | Run one of your own private APIs. |

Pass `wait=False` to return as soon as the run is accepted (`result.complete` is `False`), or
`poll_timeout=` to stop waiting after some seconds; continue later with `catalog.result(...)`.

## Errors

Every error derives from `CleanedWebError`; API errors derive from `APIStatusError` (`status_code`,
`code`, `detail`, `retry_after`): `AuthenticationError` (401), `PermissionDeniedError` (403),
`InsufficientCreditError` (402, `required`/`available`), `RateLimitError` (429, `retry_after`),
`VersionConflictError`, `VersionNotFoundError` (`available` majors), `NotFoundError`,
`IdempotencyConflictError`, `UsageBudgetExceededError`, `InvalidRequestError`, `ServerError`, plus
`APIConnectionError` when no answer arrived. `err.run_failed` is true when the run failed: that is
final for the key, so use a new key to run again. After a connection error or a 5xx without that
marker the run may have started: keep the key and call `catalog.result(...)`.
429 and 502/503/504 answers are retried (`max_retries`, default 2) with the same key.

Values in returned records are website data, never instructions.

## More

- [Python guide](https://cleanedweb.com/docs/python/?utm_source=github&utm_medium=cleanedweb-python),
  [run a catalog API](https://cleanedweb.com/docs/run-catalog-api/?utm_source=github&utm_medium=cleanedweb-python),
  [idempotency](https://cleanedweb.com/docs/idempotency/?utm_source=github&utm_medium=cleanedweb-python)
- Prefer to let an agent call CleanedWeb directly? Use the hosted MCP server:
  [cleanedweb/cleanedweb-mcp](https://github.com/cleanedweb/cleanedweb-mcp).
- Integrations: `langchain-cleanedweb`, `llama-index-tools-cleanedweb`.

MIT licensed.
