# Changelog

## 0.1.0 - 2026-11-20

- First version: sync `CleanedWeb` and async `AsyncCleanedWeb` clients for the public catalog
  (`list`, `get`, `find`, `run`, `result`), runs (`get`, `items`, `result`, `abort`, `list`) and
  private sources (`run`).
- `catalog.run` generates and returns an idempotency key, follows `202` + `Retry-After` by
  reading `/result` with the same key, and never resends the POST while it waits.
- Typed exceptions for the documented error codes.
