# Changelog

## 0.2.0 - 2026-11-25

- `apis`: build an API from a URL (`create`, `resume`, `get_build`, `get_draft`, `create_many`), sync and async.
  Drives the key-authorized builder routes on `app_url`: preview, inspection, create, build job, result
  (`source_id`, `version`, `schema`). Waiting only reads: the create request is sent once and `draft_id` /
  `job_id` are the resume handles. New `Build` and `Draft` models; `BuildFailedError` (`code`, `reason`,
  `stage`), `PreviewLimitError` (a `RateLimitError` with `cause`, `limit`, `retry_after`) and
  `ScopeRequiredError` (a `PermissionDeniedError` naming `missing_scopes`). `create_many` waits out the
  hourly preview limit. Needs a personal key with the `discover` and `build` scopes.
- `schedules`: `create`, `list`, `get`, `pause`, `resume`, `delete` against `/v1/schedules`, with the idempotency
  key handling of the other SDKs (`schedule.replayed`, `schedule.idempotency_key`), plus `runs`.
- Only new records, on the client: `SeenStore` (JSON or SQLite file, atomic writes), `sources.run(..., new_only=)`
  and `schedules.new_records(schedule_id, store)`. Identity is the contract's `record_identity_path(s)`, else a hash
  of the record. Runs are still billed for every record they deliver.
- `catalog.find(site=)` matches the site's `aliases` (new optional field of the catalog `site` object) on host
  boundaries and normalizes the input (scheme, path, `www.`, trailing dot, case): `seek.com.au` finds an API listed
  as `au.seek.com`, and `notseek.com.au` is not a match. `CatalogApi.site_aliases`; responses without aliases work.
- `Run` carries `source_id`, `definition_version`, `created_at` and `finished_at`.

## 0.1.0 - 2026-11-20

- First version: sync `CleanedWeb` and async `AsyncCleanedWeb` clients for the public catalog
  (`list`, `get`, `find`, `run`, `result`), runs (`get`, `items`, `result`, `abort`, `list`) and
  private sources (`run`).
- `catalog.run` generates and returns an idempotency key, follows `202` + `Retry-After` by
  reading `/result` with the same key, and never resends the POST while it waits.
- Typed exceptions for the documented error codes.
