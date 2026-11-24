"""``client.apis``: build an API from a URL (synchronous)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional

from . import _builds as b
from . import _core as core
from .exceptions import BuildFailedError, CleanedWebError, PreviewLimitError
from .models import Build, Draft

if TYPE_CHECKING:
    from ._sync import CleanedWeb

ProgressCallback = Callable[[Build], Any]


class Apis:
    """Turn a URL into an API of your own: inspect the page, build it, read its ``source_id``.

    Needs a personal API key with the creation scopes ``discover`` and ``build`` (the console's
    "Create APIs" option); a key with only ``run`` raises ``ScopeRequiredError``. The key is sent
    to ``app_url``. The create request is sent once per page: waiting only reads, and
    ``draft_id`` / ``job_id`` are the handles to continue with (``resume``, ``get_build``).
    Builds are slow (minutes); previews are limited per hour (``PreviewLimitError``).
    """

    def __init__(self, client: CleanedWeb) -> None:
        self._c = client

    # -- plumbing ----------------------------------------------------------------------------

    def _call(
        self, method: str, suffix: str, *, json: Optional[Dict[str, Any]] = None, hold: bool = False
    ) -> Dict[str, Any]:
        _, data = self._c._send(
            method,
            f"{self._c.app_url}{b.BUILDER}{suffix}",
            auth=True,
            json=json,
            params={"wait": b.HOLD_SECONDS} if hold else None,
            timeout=core.http_timeout_for(b.HOLD_SECONDS, self._c.timeout) if hold else None,
        )
        if not isinstance(data, dict):
            raise CleanedWebError(f"{method} {b.BUILDER}{suffix} answered without a JSON object")
        return data

    @staticmethod
    def _notify(on_progress: Optional[ProgressCallback], build: Build) -> None:
        if on_progress is not None:
            on_progress(build)

    def _draft(self, data: Dict[str, Any]) -> Draft:
        draft = Draft.from_dict(data)
        if not draft.draft_id:
            raise CleanedWebError("the preview answer carried no draft_id")
        return draft

    # -- reads -------------------------------------------------------------------------------

    def get_draft(self, draft_id: str) -> Draft:
        """The preview (``GET /api/builder/previews/{draft_id}``): its inspection and failure."""
        return self._draft(self._call("GET", f"/previews/{core.seg(draft_id)}"))

    def get_build(self, job_id: str, *, wait: bool = False) -> Build:
        """One read of a build job. A succeeded build also carries ``source_id``, ``version`` and
        ``schema``. A failed build is returned, not raised. ``wait`` lets the server hold the
        read up to 30 seconds for a change."""
        job = self._call("GET", f"/builds/{core.seg(job_id)}", hold=wait)
        build = Build.from_job(job)
        build.job_id = build.job_id or job_id
        return self._with_result(build) if build.succeeded else build

    def _with_result(self, build: Build) -> Build:
        assert build.job_id
        result = self._call("GET", f"/builds/{core.seg(build.job_id)}/result")
        build.source_id = result.get("source_id") or build.source_id
        build.version = result.get("version") or build.version
        schema = result.get("schema")
        build.schema = schema if isinstance(schema, dict) else build.schema
        build.raw = {**build.raw, "result": result}
        return build

    # -- building ----------------------------------------------------------------------------

    def create(
        self,
        url: str,
        *,
        want: Optional[str] = None,
        page_kind: Optional[str] = None,
        country: Optional[str] = None,
        wait: bool = True,
        poll_timeout: Optional[float] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> Build:
        """Build an API from ``url`` (a careers page, a listing, a catalog page).

        Inspects the page, starts the build with the router's choice (every observed field) and
        waits until the build ends, then returns it with ``source_id``, ``version`` and ``schema``.
        ``want`` says in your own words what the records are. ``on_progress(build)`` is called at
        each read. ``wait=False`` returns right after the preview is accepted; ``poll_timeout``
        stops waiting after some seconds and returns the unfinished build: continue it with
        ``resume`` (it never starts a second build). Raises ``BuildFailedError`` when the page
        cannot be built or the build fails, ``PreviewLimitError`` (429) when the hourly preview
        limit is used up, ``ScopeRequiredError`` for a key without ``discover`` and ``build``.
        """
        body = b.preview_body(url, want=want, page_kind=page_kind, country=country)
        draft = self._draft(self._call("POST", "/previews", json=body))
        if not wait:
            return Build(draft_id=draft.draft_id, state="inspecting", raw=draft.raw)
        return self._drive(draft, core.deadline_after(poll_timeout), on_progress, post_create=True)

    def resume(
        self,
        *,
        draft_id: Optional[str] = None,
        job_id: Optional[str] = None,
        poll_timeout: Optional[float] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> Build:
        """Continue a build from its handle (``job_id`` wins over ``draft_id``) and wait for its end.

        With a ``draft_id`` it reads where the build stands: waiting for the page, for the build to
        start, or for the job; the create request is sent again only when nothing started it.
        """
        deadline = core.deadline_after(poll_timeout)
        if job_id is not None:
            return self._follow(draft_id, job_id, deadline, on_progress)
        if draft_id is None:
            raise ValueError("give draft_id or job_id")
        draft = self.get_draft(draft_id)
        return self._drive(draft, deadline, on_progress, post_create=None)

    def create_many(
        self,
        urls: Iterable[str],
        *,
        want: Optional[str] = None,
        page_kind: Optional[str] = None,
        country: Optional[str] = None,
        poll_timeout: Optional[float] = None,
        on_progress: Optional[ProgressCallback] = None,
        wait_for_limit: bool = True,
        max_limit_wait: Optional[float] = None,
    ) -> List[Build]:
        """Build one API per URL, one after the other, and return a ``Build`` per URL in order.

        A failed build is returned (``state`` ``failed``, ``failure``), not raised, so one bad page
        does not stop the rest. When the hourly preview limit answers 429 the call waits the
        server's ``Retry-After`` and sends the same URL again (``wait_for_limit=False`` raises
        ``PreviewLimitError`` instead; so does a wait above ``max_limit_wait`` seconds).
        """
        builds: List[Build] = []
        for url in urls:
            while True:
                try:
                    build = self.create(
                        url,
                        want=want,
                        page_kind=page_kind,
                        country=country,
                        poll_timeout=poll_timeout,
                        on_progress=on_progress,
                    )
                except PreviewLimitError as exc:
                    delay = exc.retry_after if exc.retry_after is not None else core.DEFAULT_POLL_SECONDS
                    if not wait_for_limit or (max_limit_wait is not None and delay > max_limit_wait):
                        raise
                    time.sleep(max(delay, core.MIN_POLL_SECONDS))
                    continue
                except BuildFailedError as exc:
                    build = exc.build or Build(state="failed")
                break
            builds.append(build)
        return builds

    # -- the stages --------------------------------------------------------------------------

    def _drive(
        self,
        draft: Draft,
        deadline: Optional[float],
        on_progress: Optional[ProgressCallback],
        *,
        post_create: Optional[bool],
    ) -> Build:
        """Inspect, start and follow. ``post_create`` None: decide from the create status."""
        draft_id = draft.draft_id
        job_id = draft.job_id
        if job_id is None:
            delay = b.INSPECTION_POLL_FIRST
            while draft.inspection_status in b.INSPECTING and draft.failure is None:
                self._notify(on_progress, Build(draft_id=draft_id, state="inspecting", raw=draft.raw))
                if core.out_of_time(deadline, delay):
                    return Build(draft_id=draft_id, state="inspecting", raw=draft.raw)
                time.sleep(delay)
                delay = min(b.INSPECTION_POLL_CEILING, delay * 1.5)
                draft = self.get_draft(draft_id)
            error = b.inspection_error(draft)
            if error is not None:
                raise error
            job_id = draft.job_id
        if job_id is None:
            if draft.failure is not None:  # the creation already ended on a failure: not resent
                raise b.create_refusal({}, draft)
            job_id = self._start(draft_id, deadline, on_progress, post_create)
            if job_id is None:
                return Build(draft_id=draft_id, state="creating")
        return self._follow(draft_id, job_id, deadline, on_progress)

    def _start(
        self,
        draft_id: str,
        deadline: Optional[float],
        on_progress: Optional[ProgressCallback],
        post_create: Optional[bool],
    ) -> Optional[str]:
        """The job id of the build of this draft, starting it once if nothing did."""
        path = f"/previews/{core.seg(draft_id)}/create"
        if post_create:
            status = self._call("POST", path, json={})
        else:
            status = self._call("GET", path)
            if status.get("state") == "idle" and not b.create_job_id(status):
                draft = self.get_draft(draft_id)
                if draft.job_id:
                    return draft.job_id
                if draft.failure is not None:
                    raise b.create_refusal(status, draft)
                status = self._call("POST", path, json={})
        while True:
            job_id = b.create_job_id(status)
            if job_id:
                return job_id
            if status.get("state") == "idle":  # it ended with no job: the draft says why
                draft = self.get_draft(draft_id)
                if draft.job_id:
                    return draft.job_id
                raise b.create_refusal(status, draft)
            progress = Build(draft_id=draft_id, state="creating", raw=status)
            position = status.get("queue_position")
            progress.queue_position = position if isinstance(position, int) else None
            self._notify(on_progress, progress)
            if core.out_of_time(deadline, b.MIN_BUILD_POLL_SECONDS):
                return None
            time.sleep(b.MIN_BUILD_POLL_SECONDS)
            status = self._call("GET", path, hold=True)

    def _follow(
        self,
        draft_id: Optional[str],
        job_id: str,
        deadline: Optional[float],
        on_progress: Optional[ProgressCallback],
    ) -> Build:
        path = f"/builds/{core.seg(job_id)}"
        while True:
            build = Build.from_job(self._call("GET", path, hold=True), draft_id=draft_id)
            build.job_id = build.job_id or job_id
            self._notify(on_progress, build)
            if build.terminal:
                break
            if core.out_of_time(deadline, b.MIN_BUILD_POLL_SECONDS):
                return build
            time.sleep(b.MIN_BUILD_POLL_SECONDS)
        if not build.succeeded:
            raise b.job_error(build)
        return self._with_result(build)
