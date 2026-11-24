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
| `catalog.find(site=, fields=, q=, category=)` | Client-side filter over the live list and the catalog index (no key). `site` is a domain, a URL or a name; it also matches the site's other hosts (`site.aliases`), so `seek.com.au` finds an API listed as `au.seek.com`. |
| `catalog.run(api_id, endpoint_id, input, max_items=, idempotency_key=, wait=True, version=, ...)` | Run a catalog API. Needs a key with the `run` scope. |
| `catalog.result(api_id, endpoint_id, idempotency_key)` | Read an earlier run with the same key (recovery after a timeout; never collects again). |
| `runs.get(run_id)`, `runs.result(run_id)`, `runs.abort(run_id)` | Read, wait for, or stop a run. |
| `runs.items(run_id)` | Iterate records across cursor pages (also while the run runs). |
| `runs.list(...)` / `runs.iter(...)` | Your runs, newest first. |
| `sources.run(source_id, input, expected_version=, new_only=, ...)` | Run one of your own private APIs; `new_only=` keeps only records you have not seen. |
| `apis.create(url, want=, wait=True, poll_timeout=, on_progress=)` | Build an API of your own from a URL (a careers page, a listing). Needs a key with `discover` and `build`. |
| `apis.resume(draft_id= / job_id=)`, `apis.get_build(job_id)`, `apis.get_draft(draft_id)`, `apis.create_many(urls)` | Continue, read, or build several (sequentially, waiting out the preview limit). |
| `schedules.create(source_id= / api_id=, endpoint_id=, every=, at_utc=, ...)` | Run an API on a schedule (hour, day, week; UTC). |
| `schedules.list()`, `.get(id)`, `.pause(id)`, `.resume(id)`, `.delete(id)` | Manage schedules (`read` scope for reads, `run` for changes). |
| `schedules.runs(id)`, `schedules.new_records(id, store)` | The runs a schedule started; only the records not seen before. |
| `SeenStore(path)` | The client-side memory of records already handled (JSON or SQLite file). |

Pass `wait=False` to return as soon as the run is accepted (`result.complete` is `False`), or
`poll_timeout=` to stop waiting after some seconds; continue later with `catalog.result(...)`.

## From a careers page to "only the new jobs", from code

The whole job from Python: turn a URL into an API, run it every day, read only what is new.
Build keys need the creation scopes `discover` and `build` (the console's "Create APIs" option, personal
keys only); scheduling and running need `run` (and `read` to list schedules).

```python
from cleanedweb import CleanedWeb, SeenStore

cw = CleanedWeb()  # CLEANEDWEB_API_KEY

# 1. URL -> API. Takes minutes; on_progress sees each step. Never starts a second build while waiting.
build = cw.apis.create(
    "https://careers.example.org/jobs",
    want="open job postings",
    on_progress=lambda b: print(b.state),
)
print(build.source_id, build.version)  # your API; build.schema is its record schema
# Several sites: builds one after the other and waits out the hourly preview limit (429 Retry-After).
# builds = cw.apis.create_many(urls)  # a failed page comes back as a Build with state "failed"

# 2. Run it every morning at 06:30 UTC. Each slot is an ordinary run, charged per record delivered.
schedule = cw.schedules.create(
    source_id=build.source_id,
    every="day",
    at_utc="06:30",
    max_items=500,
    name="Careers page, daily",
    idempotency_key="careers-daily-1",  # a retry with the same key never makes a second schedule
)

# 3. Each morning: only the records you have not seen before.
store = SeenStore("seen-records.json")  # or "seen-records.sqlite"
for job in cw.schedules.new_records(schedule.schedule_id, store):
    print(job["title"], job["url"])
```

If the process stops mid-build, resume from the handles instead of starting again:
`cw.apis.resume(draft_id=build.draft_id)` or `cw.apis.resume(job_id=build.job_id)`. `poll_timeout=` returns the
unfinished `Build` after that many seconds. Failures are typed: `BuildFailedError` (`code`, `reason`, `stage`),
`PreviewLimitError` (429; `cause`, `limit`, `retry_after`) and `ScopeRequiredError` (403; `missing_scopes`).

**Only new records is done on the client.** The server has no "since" read yet, so every run still delivers, and
bills, all of its records; `SeenStore` only saves your own processing. A record is "the same" by the identity the
API's contract declares (`record_identity_path`, read with the `read` scope), else by a hash of the whole record,
where any changed field counts as new (`result.identity` says which). `new_records` reads the schedule source's
finished runs newer than the last one a previous call handled, so a run is read once; it marks records seen before
it returns, or pass `mark=False` and call `.commit()` after you handled them. Runs listed for a schedule are all
finished runs of its source, manual runs included. Without a schedule:

```python
result = cw.sources.run(source_id, {"query": "python"}, new_only="seen-records.json")
print(len(result.records), "delivered,", len(result.new_records), "new")
```

## Errors

Every error derives from `CleanedWebError`; API errors derive from `APIStatusError` (`status_code`,
`code`, `detail`, `retry_after`): `AuthenticationError` (401), `PermissionDeniedError` (403; `ScopeRequiredError` names the missing scopes),
`InsufficientCreditError` (402, `required`/`available`), `RateLimitError` (429, `retry_after`; `PreviewLimitError` for the hourly build previews),
`VersionConflictError`, `VersionNotFoundError` (`available` majors), `NotFoundError`,
`IdempotencyConflictError`, `UsageBudgetExceededError`, `InvalidRequestError`, `ServerError`, plus
`APIConnectionError` when no answer arrived; `BuildFailedError` when a build ends without an API. `err.run_failed` is true when the run failed: that is
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
