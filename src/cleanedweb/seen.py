"""Only new records, on the client: a store of the records you have already seen.

The server has no "changed since" read yet, so every run still delivers (and bills) all of its
records. A :class:`SeenStore` remembers which of them you already handled, keyed by the record
identity the API's contract declares (``record_identity_path`` or ``record_identity_paths``);
without a declared identity (or when a record lacks it) the key is a stable hash of the whole
record, which treats any changed field as a new record.

Namespaces keep sources apart: one store file serves many APIs (the namespace is the
``source_id``, or a schedule's own name for run bookkeeping).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

__all__ = ["NewRecords", "SeenStore", "identity_paths_from_contract", "record_key"]

PathLike = Union[str, "os.PathLike[str]"]
_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")


def identity_paths_from_contract(contract: Any) -> Tuple[str, ...]:
    """The record identity paths a source version contract declares, or ``()``.

    The contract is ``GET /v1/sources/{id}/versions/{version}/contract``; the paths sit in its
    ``openapi["x-cleanedweb-contract"]`` as ``record_identity_paths`` or ``record_identity_path``."""
    if not isinstance(contract, dict):
        return ()
    openapi = contract.get("openapi")
    marker = openapi.get("x-cleanedweb-contract") if isinstance(openapi, dict) else None
    if not isinstance(marker, dict):
        marker = contract.get("x-cleanedweb-contract")
    if not isinstance(marker, dict):
        return ()
    many = marker.get("record_identity_paths")
    if isinstance(many, list) and many and all(isinstance(p, str) and p for p in many):
        return tuple(many)
    one = marker.get("record_identity_path")
    return (one,) if isinstance(one, str) and one else ()


def _lookup(record: Dict[str, Any], path: str) -> Any:
    if path in record:
        return record[path]
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def record_key(record: Dict[str, Any], identity_paths: Sequence[str] = ()) -> Tuple[str, bool]:
    """``(key, declared)``: the identity of ``record`` and whether it came from the declared paths.

    The key is the SHA-256 of the identity values (or of the whole record when no path resolves
    to a value), so keys have one size and the store never holds record content.
    """
    if identity_paths:
        values = [_lookup(record, p) for p in identity_paths]
        if all(v is not None and v != "" for v in values):
            text = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
            return hashlib.sha256(b"id:" + text.encode()).hexdigest(), True
    text = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(b"rec:" + text.encode()).hexdigest(), False


def _digest(value: str) -> str:
    return hashlib.sha256(b"key:" + value.encode()).hexdigest()


class _JsonBackend:
    """One JSON file, rewritten whole and atomically (temp file, fsync, rename)."""

    def __init__(self, path: Optional[str]) -> None:
        self._path = path
        self._data: Dict[str, Set[str]] = {}
        self._stamp: Optional[Tuple[int, int]] = None
        self._refresh()

    def _refresh(self) -> None:
        """Merge in what another store object or process wrote since this one last looked."""
        if not self._path or not os.path.exists(self._path):
            return
        info = os.stat(self._path)
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp != self._stamp:
            for ns, keys in self._read(self._path).items():
                self._data.setdefault(ns, set()).update(keys)
            self._stamp = stamp

    @staticmethod
    def _read(path: str) -> Dict[str, Set[str]]:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if not text.strip():
            return {}
        loaded = json.loads(text)
        seen = loaded.get("seen") if isinstance(loaded, dict) else None
        if not isinstance(seen, dict):
            raise ValueError(f"{path} is not a cleanedweb seen store")
        return {str(ns): {str(k) for k in keys} for ns, keys in seen.items() if isinstance(keys, list)}

    def known(self, namespace: str, keys: Iterable[str]) -> Set[str]:
        self._refresh()
        have = self._data.get(namespace, set())
        return {k for k in keys if k in have}

    def add(self, namespace: str, keys: Iterable[str]) -> None:
        self._refresh()  # keep what another process added since this one looked
        self._data.setdefault(namespace, set()).update(keys)
        if self._path:
            self._flush(self._path)

    def count(self, namespace: str) -> int:
        self._refresh()
        return len(self._data.get(namespace, ()))

    def _flush(self, path: str) -> None:
        payload = {"version": 1, "seen": {ns: sorted(keys) for ns, keys in sorted(self._data.items())}}
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix=".seen-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            info = os.stat(path)
            self._stamp = (info.st_mtime_ns, info.st_size)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class _SqliteBackend:
    """A SQLite file; every write is one transaction, so a crash leaves the store as it was."""

    def __init__(self, path: str) -> None:
        self._path = path
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS seen (namespace TEXT NOT NULL, key TEXT NOT NULL, "
                "PRIMARY KEY (namespace, key)) WITHOUT ROWID"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30.0)

    def known(self, namespace: str, keys: Iterable[str]) -> Set[str]:
        wanted = list(keys)
        found: Set[str] = set()
        conn = self._connect()
        try:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT key FROM seen WHERE namespace = ? AND key IN ({marks})", [namespace, *chunk]
                )
                found.update(row[0] for row in rows)
        finally:
            conn.close()
        return found

    def add(self, namespace: str, keys: Iterable[str]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO seen (namespace, key) VALUES (?, ?)",
                    [(namespace, k) for k in keys],
                )
        finally:
            conn.close()

    def count(self, namespace: str) -> int:
        conn = self._connect()
        try:
            return int(
                conn.execute("SELECT count(*) FROM seen WHERE namespace = ?", [namespace]).fetchone()[0]
            )
        finally:
            conn.close()


class SeenStore:
    """Remembers which records (and runs) you have already handled.

    ``path`` ending ``.sqlite``, ``.sqlite3`` or ``.db`` opens a SQLite file; any other path is a
    JSON file written atomically (temp file and rename); ``None`` keeps it in memory. Safe to share
    between threads; two processes on one JSON file merge what each added, on a SQLite file they
    are fully independent.
    """

    def __init__(self, path: Optional[PathLike] = None) -> None:
        self.path = os.fspath(path) if path is not None else None
        self._lock = threading.Lock()
        self._backend: Union[_JsonBackend, _SqliteBackend]
        if self.path is not None and self.path.lower().endswith(_SQLITE_SUFFIXES):
            self._backend = _SqliteBackend(self.path)
        else:
            self._backend = _JsonBackend(self.path)

    def __repr__(self) -> str:
        return f"SeenStore({self.path!r})"

    def count(self, namespace: str) -> int:
        """How many keys the namespace holds."""
        with self._lock:
            return self._backend.count(namespace)

    # -- records -----------------------------------------------------------------------------

    def filter_new(
        self,
        namespace: str,
        records: Iterable[Dict[str, Any]],
        identity_paths: Sequence[str] = (),
        *,
        mark: bool = True,
    ) -> NewRecords:
        """The records of ``records`` not seen before in ``namespace``, in order, once each.

        With ``mark`` (default) they are remembered before this returns; with ``mark=False``
        call ``result.commit()`` after you have handled them, so a crash in between shows them
        again next time instead of losing them.
        """
        keyed: List[Tuple[str, bool, Dict[str, Any]]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            key, declared = record_key(record, identity_paths)
            keyed.append((key, declared, record))
        with self._lock:
            known = self._backend.known(namespace, (k for k, _, _ in keyed))
        fresh: List[Dict[str, Any]] = []
        pending: List[str] = []
        declared_count = 0
        pending_set: Set[str] = set()
        for key, declared, record in keyed:
            if key in known or key in pending_set:
                continue
            pending_set.add(key)
            pending.append(key)
            fresh.append(record)
            declared_count += 1 if declared else 0
        if not fresh:
            identity = "contract" if identity_paths else "hash"
        elif declared_count == len(fresh):
            identity = "contract"
        else:
            identity = "hash" if declared_count == 0 else "mixed"
        result = NewRecords(
            fresh,
            seen_count=len(keyed) - len(fresh),
            identity=identity,
            store=self,
            namespace=namespace,
            pending=pending,
        )
        if mark:
            result.commit()
        return result

    # -- runs (a schedule's slots are read once) ---------------------------------------------

    def run_seen(self, namespace: str, run_id: str) -> bool:
        with self._lock:
            return bool(self._backend.known(namespace, [_digest(run_id)]))

    def mark_runs(self, namespace: str, run_ids: Iterable[str]) -> None:
        keys = [_digest(r) for r in run_ids]
        if keys:
            with self._lock:
                self._backend.add(namespace, keys)

    def _add_keys(self, namespace: str, keys: List[str]) -> None:
        if keys:
            with self._lock:
                self._backend.add(namespace, keys)


class NewRecords(List[Dict[str, Any]]):
    """The unseen records (a list), with what was left out.

    ``seen_count``: records skipped because they were seen before (or repeated in the batch).
    ``identity``: ``contract`` (the API's declared identity), ``hash`` (a hash of the whole record: a
    changed field makes it new) or ``mixed``. ``commit()`` remembers them; it already ran unless you
    passed ``mark=False``. ``run_ids``: for a schedule, the runs read.
    """

    def __init__(
        self,
        records: Iterable[Dict[str, Any]] = (),
        *,
        seen_count: int = 0,
        identity: str = "hash",
        store: Optional[SeenStore] = None,
        namespace: str = "",
        pending: Optional[List[str]] = None,
    ) -> None:
        super().__init__(records)
        self.seen_count = seen_count
        self.identity = identity
        self.run_ids: List[str] = []
        self._store = store
        self._namespace = namespace
        self._pending: List[str] = list(pending or [])
        self._run_namespace = ""

    def commit(self) -> None:
        """Remember these records (and the runs they came from); safe to call twice."""
        if self._store is None:
            return
        self._store._add_keys(self._namespace, self._pending)
        self._pending = []
        if self.run_ids and self._run_namespace:
            self._store.mark_runs(self._run_namespace, self.run_ids)
