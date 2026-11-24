"""GitLab REST v4 client implementing the pipeline's duck-typed host contract.

Only the methods used by ``ReviewPipeline`` / ``Publisher`` / ``RepoWorkspace``
are implemented. GitLab has no GitHub-Review equivalent: publishing is a
summary note plus one discussion per inline comment. The write path lives in
``create_review`` below.

Diff retrieval deliberately prefers ``GET .../diffs?unidiff=true`` because it
exposes ``too_large``/``collapsed``; a truncated diff must not silently produce
a half review. ``raw_diffs`` is the fallback for older instances.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

import httpx

from codereview.config import Settings
from codereview.errors import GitLabAPIError, GitLabAuthError
from codereview.logging_utils import get_logger
from codereview.models import PRInfo
from codereview.retry import retry_after_seconds
from codereview.scm import project_path, qualify_repo

log = get_logger(__name__)

_INLINE_MARKER_RE = re.compile(
    r"<!--\s*codereview-inline:(?P<fingerprint>[0-9a-f]{16}):(?P<index>\d+)\s*-->"
)
_SUMMARY_MARKER_RE = re.compile(
    r"<!--\s*codereview:(?P<fingerprint>[0-9a-f]{16})\s*-->"
)
_PENDING_MARKER_RE = re.compile(
    r"<!--\s*codereview-pending:(?P<fingerprint>[0-9a-f]{16})\s*-->"
)
_DIFF_GIT_RE = re.compile(r"^diff --git ", re.MULTILINE)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">")
            if start != -1 and end != -1:
                return part[start + 1:end]
    return None


def _changes_count(value) -> tuple[int | None, bool]:
    """Return ``(count, is_plus)`` for GitLab's string ``changes_count``."""
    if value is None:
        return None, False
    text = str(value).strip()
    if not text:
        return None, False
    plus = text.endswith("+")
    match = re.match(r"(\d+)", text)
    return (int(match.group(1)) if match else None), plus


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:300]
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            if isinstance(message, (dict, list)):
                return json.dumps(message, ensure_ascii=False)[:300]
            return str(message)[:300]
    return (response.text or "")[:300]


class GitLabClient:
    provider_name = "gitlab"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.time,
    ):
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(
            timeout=settings.gitlab_timeout,
            transport=transport,
            # Redirects are handled manually so PRIVATE-TOKEN is never
            # forwarded to a different origin (httpx only strips Authorization).
            follow_redirects=False,
        )
        # Per-thread diff warnings: get_pull_diff and create_review run in the
        # same review thread, while the server may run several reviews at once.
        self._local = threading.local()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitLabClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- transport ---------------------------------------------------------

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "codereview-agent/0.1",
        }
        if self.settings.gitlab_token:
            headers["PRIVATE-TOKEN"] = self.settings.gitlab_token
        return headers

    def _same_origin(self, url: str) -> bool:
        base = urlsplit(self.settings.gitlab_api_base())
        target = urlsplit(url)
        return (target.scheme, target.netloc) == (base.scheme, base.netloc)

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            if not self._same_origin(path):
                raise GitLabAPIError(502, "refusing cross-origin URL", path)
            return path
        return f"{self.settings.gitlab_api_base()}{path}"

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        accept: str,
        params: dict | None,
        data: dict | None,
        json_body: dict | None,
    ) -> httpx.Response:
        current_url = url
        current_params = params
        current_method = method
        current_data = data
        current_json = json_body
        headers = self._headers(accept)

        for _ in range(6):
            response = self._client.request(
                current_method,
                current_url,
                headers=headers,
                params=current_params,
                data=current_data,
                json=current_json,
            )
            if response.status_code not in _REDIRECT_STATUSES:
                return response

            location = response.headers.get("Location")
            if not location:
                raise GitLabAPIError(502, "redirect without Location", current_url)
            target = urljoin(current_url, location)
            if urlsplit(target).scheme not in ("http", "https"):
                raise GitLabAPIError(502, "refusing non-http redirect", target)
            if not self._same_origin(target):
                raise GitLabAPIError(502, "refusing cross-origin redirect", target)
            current_url = target
            current_params = None
            if response.status_code == 303 or (
                response.status_code in (301, 302)
                and current_method not in ("GET", "HEAD")
            ):
                current_method = "GET"
                current_data = None
                current_json = None

        raise GitLabAPIError(502, "too many redirects", current_url)

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str = "application/json",
        params: dict | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        full_url = self._url(url)
        retries = max(1, self.settings.llm_retries)
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                response = self._request_once(
                    method,
                    full_url,
                    accept=accept,
                    params=params,
                    data=data,
                    json_body=json_body,
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= retries:
                    raise GitLabAPIError(0, f"transport error: {exc}", full_url) from exc
                self._sleep(1.5 * attempt)
                continue

            if response.status_code == 401:
                raise GitLabAuthError(_error_message(response), full_url)

            if response.status_code in (403, 429):
                remaining = response.headers.get("RateLimit-Remaining")
                limited = remaining == "0" or response.status_code == 429
                if limited and attempt < retries:
                    self._sleep(self._rate_limit_delay(response.headers))
                    continue

            if 500 <= response.status_code < 600:
                if attempt < retries:
                    self._sleep(1.5 * attempt)
                    continue

            if response.status_code >= 400:
                raise GitLabAPIError(
                    response.status_code, _error_message(response), full_url
                )

            return response

        raise GitLabAPIError(0, f"request failed: {last_error}", full_url)

    def _rate_limit_delay(self, headers) -> float:
        reset = headers.get("RateLimit-Reset")
        if reset and str(reset).isdigit():
            try:
                delay = int(reset) - int(self._clock())
            except (TypeError, ValueError):
                delay = 0
            return max(1.0, min(60.0, float(delay)))
        return retry_after_seconds(headers.get("Retry-After"))

    def _paginate(
        self,
        url: str,
        *,
        params: dict | None = None,
        max_pages: int = 100,
    ) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        next_params: dict | None = dict(params or {})
        next_params.setdefault("per_page", 100)
        pages = 0

        while next_url:
            pages += 1
            if pages > max_pages:
                raise GitLabAPIError(
                    502, f"pagination exceeded {max_pages} pages", next_url
                )
            response = self._request("GET", next_url, params=next_params)
            try:
                payload = response.json()
            except ValueError as exc:
                raise GitLabAPIError(502, "response is not JSON", next_url) from exc
            if not isinstance(payload, list):
                raise GitLabAPIError(502, "expected a JSON array", next_url)
            items.extend(item for item in payload if isinstance(item, dict))

            next_page = (response.headers.get("X-Next-Page") or "").strip()
            if next_page:
                next_url = url
                next_params = dict(params or {})
                next_params["per_page"] = 100
                next_params["page"] = next_page
                continue

            link = _next_link(response.headers.get("Link"))
            if link:
                if not self._same_origin(link):
                    raise GitLabAPIError(
                        502, "refusing cross-origin pagination link", link
                    )
                next_url = link
                next_params = None
                continue
            break

        return items

    # --- identity ----------------------------------------------------------

    def _project(self, repo: str) -> tuple[str, str]:
        qualified = qualify_repo("gitlab", self.settings.gitlab_base_url, repo)
        path = project_path(qualified, self.settings.gitlab_base_url)
        return qualified, quote(path, safe="")

    def _mr_url(self, encoded: str, number: int) -> str:
        return f"/projects/{encoded}/merge_requests/{number}"

    def _get_mr_raw(self, encoded: str, number: int) -> dict:
        response = self._request("GET", self._mr_url(encoded, number))
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitLabAPIError(502, "merge request payload is not an object")
        return payload

    # --- API ---------------------------------------------------------------

    def get_pull(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> PRInfo:
        qualified, encoded = self._project(repo)
        payload = self._get_mr_raw(encoded, number)
        diff_refs = payload.get("diff_refs") or {}
        state = str(payload.get("state") or "opened")
        merged = state == "merged" or bool(payload.get("merged_at"))
        if state in ("opened", "locked") and not merged:
            state_value = "open"
        else:
            state_value = state
        author = payload.get("author") or {}
        changed, _ = _changes_count(payload.get("changes_count"))
        return PRInfo(
            repo=qualified,
            number=int(payload.get("iid") or number),
            title=payload.get("title") or "",
            body=payload.get("description") or "",
            author=author.get("username") or "",
            base_sha=diff_refs.get("base_sha") or "",
            head_sha=diff_refs.get("head_sha") or payload.get("sha") or "",
            state=state_value,
            draft=bool(payload.get("draft") or payload.get("work_in_progress")),
            merged=merged,
            html_url=payload.get("web_url") or "",
            changed_files=changed or 0,
            additions=0,
            deletions=0,
        )

    def get_pull_diff(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> str:
        _, encoded = self._project(repo)
        self._local.diff_warnings = []

        for attempt in range(5):
            try:
                text, warnings = self._diff_via_unidiff(encoded, number)
            except GitLabAPIError as exc:
                if exc.status in (400, 404):
                    text, warnings = self._diff_via_raw(encoded, number)
                else:
                    raise
            if text.strip():
                self._local.diff_warnings = warnings
                return text

            mr = self._get_mr_raw(encoded, number)
            state = str(mr.get("state") or "")
            if state not in ("opened", "locked", "merged"):
                return ""
            changes, _ = _changes_count(mr.get("changes_count"))
            if changes == 0:
                return ""
            if attempt < 4:
                self._sleep(2)
                continue
            raise GitLabAPIError(
                409,
                "MR diff is not ready (GitLab populates diff_refs asynchronously)",
                self._mr_url(encoded, number),
            )
        return ""

    def _mr_expected_changes(self, encoded: str, number: int) -> tuple[int | None, bool]:
        mr = self._get_mr_raw(encoded, number)
        return _changes_count(mr.get("changes_count"))

    def _diff_via_unidiff(self, encoded: str, number: int) -> tuple[str, list[str]]:
        files = self._paginate(
            f"{self._mr_url(encoded, number)}/diffs",
            params={"unidiff": "true", "per_page": 100},
        )
        expected, plus = self._mr_expected_changes(encoded, number)
        return self._render_diffs(files, expected=expected, plus=plus)

    def _diff_via_raw(self, encoded: str, number: int) -> tuple[str, list[str]]:
        response = self._request(
            "GET",
            f"{self._mr_url(encoded, number)}/raw_diffs",
            accept="text/plain",
        )
        text = response.text or ""
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("<"):
            raise GitLabAPIError(502, "raw_diffs returned a non-diff payload")
        if not text.strip():
            return "", []
        expected, plus = self._mr_expected_changes(encoded, number)
        found = len(_DIFF_GIT_RE.findall(text))
        truncated = plus or (expected is not None and found < expected)
        warnings: list[str] = []
        if truncated:
            message = (
                f"GitLab raw diff appears truncated: found {found} file(s), "
                f"changes_count={expected}{'+' if plus else ''}"
            )
            if not self.settings.gitlab_allow_truncated_diff:
                raise GitLabAPIError(413, message, self._mr_url(encoded, number))
            warnings.append(message)
        return text, warnings

    def _render_diffs(
        self,
        files: list[dict],
        *,
        expected: int | None,
        plus: bool,
        source: str = "diffs",
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []
        if not files:
            # A newly created MR populates diffs asynchronously. Let the caller
            # retry instead of reporting a truncated diff.
            return "", warnings
        truncated_flags = [
            f
            for f in files
            if bool(f.get("too_large")) or bool(f.get("collapsed"))
        ]
        count_truncated = plus or (expected is not None and len(files) < expected)

        if truncated_flags or count_truncated:
            detail = (
                f"{source}: {len(truncated_flags)} file(s) flagged too_large/collapsed, "
                f"files={len(files)}, changes_count={expected}{'+' if plus else ''}"
            )
            if not self.settings.gitlab_allow_truncated_diff:
                raise GitLabAPIError(413, f"GitLab truncated the MR diff ({detail})")
            warnings.append(f"GitLab truncated the MR diff ({detail})")

        parts: list[str] = []
        for file in files:
            if bool(file.get("too_large")) or bool(file.get("collapsed")):
                continue
            new_path = file.get("new_path") or file.get("old_path") or ""
            old_path = file.get("old_path") or new_path
            if file.get("new_file"):
                old_path = new_path
            body = file.get("diff") or ""
            if not new_path:
                continue
            if body.startswith("diff --git"):
                parts.append(body if body.endswith("\n") else body + "\n")
                continue
            parts.append(f"diff --git a/{old_path} b/{new_path}\n")
            if file.get("new_file"):
                parts.append("new file mode 100644\n")
            elif file.get("deleted_file"):
                parts.append("deleted file mode 100644\n")
            if file.get("renamed_file"):
                parts.append(f"rename from {old_path}\nrename to {new_path}\n")
            if not body.lstrip().startswith("--- "):
                old_header = "/dev/null" if file.get("new_file") else f"a/{old_path}"
                new_header = "/dev/null" if file.get("deleted_file") else f"b/{new_path}"
                parts.append(f"--- {old_header}\n+++ {new_header}\n")
            if body:
                parts.append(body if body.endswith("\n") else body + "\n")

        if not parts and (expected or 0) > 0:
            raise GitLabAPIError(413, "GitLab diff contains no reviewable content")
        return "".join(parts), warnings

    def get_compare_diff(
        self, repo: str, base: str, head: str, installation_id: int | None = None
    ) -> str:
        _, encoded = self._project(repo)
        response = self._request(
            "GET",
            f"/projects/{encoded}/repository/compare",
            params={"from": base, "to": head, "unidiff": "true"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitLabAPIError(502, "compare payload is not an object")
        if payload.get("compare_timeout"):
            message = "GitLab compare timed out; the diff is incomplete"
            if not self.settings.gitlab_allow_truncated_diff:
                raise GitLabAPIError(413, message)
            self._local.diff_warnings = [message]
        diffs = payload.get("diffs") or []
        if not isinstance(diffs, list):
            raise GitLabAPIError(502, "compare diffs is not a list")
        text, warnings = self._render_diffs(
            [item for item in diffs if isinstance(item, dict)],
            expected=None,
            plus=False,
            source="compare",
        )
        if warnings:
            self._local.diff_warnings = warnings
        return text

    def list_reviews(
        self, repo: str, number: int, installation_id: int | None = None
    ) -> list[dict]:
        _, encoded = self._project(repo)
        url = f"{self._mr_url(encoded, number)}/notes"
        try:
            notes = self._paginate(
                url, params={"per_page": 100, "sort": "desc"}
            )
        except GitLabAPIError as exc:
            if exc.status != 400:
                raise
            notes = self._paginate(url, params={"per_page": 100})
        return [
            {
                "id": note.get("id"),
                "body": note.get("body") or "",
                "created_at": note.get("created_at") or "",
                "author": (note.get("author") or {}).get("username") or "",
            }
            for note in notes
            if not note.get("system")
        ]

    def download_tarball(
        self, repo: str, ref: str, installation_id: int | None = None
    ) -> bytes:
        _, encoded = self._project(repo)
        url = f"{self.settings.gitlab_api_base()}/projects/{encoded}/repository/archive.tar.gz"
        limit = self.settings.repo_max_mb * 1024 * 1024
        retries = max(1, self.settings.llm_retries)
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            current_url = url
            current_params: dict | None = {"sha": ref}
            current_headers = self._headers("application/octet-stream")
            retry_attempt = False
            try:
                for _ in range(6):
                    with self._client.stream(
                        "GET",
                        current_url,
                        headers=current_headers,
                        params=current_params,
                    ) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("Location")
                            if not location:
                                raise GitLabAPIError(
                                    502, "redirect without Location", current_url
                                )
                            target = urljoin(current_url, location)
                            if urlsplit(target).scheme not in ("http", "https"):
                                raise GitLabAPIError(
                                    502, "refusing non-http redirect", target
                                )
                            if not self._same_origin(target):
                                # GitLab may redirect archives to object
                                # storage; that URL is signed and must not
                                # receive PRIVATE-TOKEN.
                                current_headers = {
                                    key: value
                                    for key, value in current_headers.items()
                                    if key != "PRIVATE-TOKEN"
                                }
                            current_url = target
                            current_params = None
                            continue

                        if response.status_code == 401:
                            raise GitLabAuthError(_error_message(response), current_url)
                        if 500 <= response.status_code < 600 and attempt < retries:
                            response.read()
                            self._sleep(1.5 * attempt)
                            retry_attempt = True
                            break
                        if response.status_code in (403, 429) and attempt < retries:
                            response.read()
                            self._sleep(self._rate_limit_delay(response.headers))
                            retry_attempt = True
                            break
                        if response.status_code >= 400:
                            response.read()
                            raise GitLabAPIError(
                                response.status_code,
                                _error_message(response),
                                current_url,
                            )

                        declared = response.headers.get("Content-Length")
                        if declared and declared.isdigit() and int(declared) > limit:
                            raise GitLabAPIError(
                                413,
                                f"tarball exceeds REPO_MAX_MB={self.settings.repo_max_mb}",
                                current_url,
                            )

                        buffer = bytearray()
                        for chunk in response.iter_bytes(64 * 1024):
                            buffer.extend(chunk)
                            if len(buffer) > limit:
                                raise GitLabAPIError(
                                    413,
                                    f"tarball exceeds REPO_MAX_MB={self.settings.repo_max_mb}",
                                    current_url,
                                )
                        return bytes(buffer)
                else:
                    raise GitLabAPIError(502, "too many redirects", current_url)
                if retry_attempt:
                    continue
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= retries:
                    raise GitLabAPIError(0, f"transport error: {exc}", url) from exc
                self._sleep(1.5 * attempt)

        raise GitLabAPIError(0, f"download failed: {last_error}", url)

    # --- write path --------------------------------------------------------

    def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict],
        event: str = "COMMENT",
        installation_id: int | None = None,
    ) -> dict:
        """Publish a pending summary, inline discussions, then final summary.

        GitLab has no atomic review object and sorts notes by creation time, so
        posting the summary last would leave it at the bottom of the MR page.
        Instead, create a pending summary note first (it stays at the top), post
        missing inline discussions, then edit that note into the final summary.
        Inline markers and the pending marker make retries idempotent: a crash
        before the final edit reuses the pending note and only fills gaps. The
        final ``codereview:<fingerprint>`` marker is written last and is the
        signal that the whole review is complete.
        """
        _, encoded = self._project(repo)
        mr_url = self._mr_url(encoded, number)
        mr = self._get_mr_raw(encoded, number)

        state = str(mr.get("state") or "")
        if state not in ("opened", "locked") or mr.get("merged_at"):
            raise GitLabAPIError(409, f"merge request is not open (state={state!r})", mr_url)

        diff_refs = mr.get("diff_refs") or {}
        head_sha = diff_refs.get("head_sha") or ""
        base_sha = diff_refs.get("base_sha") or ""
        start_sha = diff_refs.get("start_sha") or ""
        if commit_id and head_sha and commit_id != head_sha:
            raise GitLabAPIError(
                409,
                "stale head: the merge request advanced while it was being reviewed",
                mr_url,
            )

        warnings = list(getattr(self._local, "diff_warnings", []) or [])
        if warnings:
            warning_block = "\n".join(f"> {item}" for item in warnings)
            body = f"> **Diff warning**\n{warning_block}\n\n{body}"

        fingerprint = self._fingerprint_from_body(body, comments)
        posted = 0
        skipped = 0
        pending_note_id: int | None = None

        if comments:
            if not (head_sha and base_sha and start_sha):
                raise GitLabAPIError(409, "merge request diff_refs is empty", mr_url)
            pending_note_id = self._find_pending_summary(
                encoded, number, fingerprint
            )
            if pending_note_id is None:
                response = self._request(
                    "POST",
                    f"{mr_url}/notes",
                    data={"body": self._pending_summary_body(fingerprint)},
                )
                payload = response.json()
                note_id = payload.get("id") if isinstance(payload, dict) else None
                if not isinstance(note_id, int):
                    raise GitLabAPIError(
                        502, "GitLab did not return a summary note id", mr_url
                    )
                pending_note_id = note_id
            existing = self._existing_inline_markers(encoded, number, fingerprint)
            old_paths = self._old_path_map(encoded, number)
            for index, comment in enumerate(comments):
                if index in existing:
                    skipped += 1
                    continue
                path = str(comment.get("path") or "")
                line = comment.get("line")
                if not path or not isinstance(line, int):
                    skipped += 1
                    continue
                if comment.get("side") not in (None, "", "RIGHT"):
                    # GitLab positions only support the new side here.
                    skipped += 1
                    continue
                form = {
                    "body": self._inline_body(comment, fingerprint, index),
                    "position[position_type]": "text",
                    "position[base_sha]": base_sha,
                    "position[head_sha]": head_sha,
                    "position[start_sha]": start_sha,
                    "position[new_path]": path,
                    "position[old_path]": old_paths.get(path, path),
                    "position[new_line]": str(line),
                }
                try:
                    self._request("POST", f"{mr_url}/discussions", data=form)
                    posted += 1
                except GitLabAPIError as exc:
                    if exc.status not in (400, 422):
                        # Auth, rate-limit, stale-head and 5xx errors must fail
                        # closed: posting a summary would make the run look
                        # complete while findings were silently dropped.
                        raise
                    skipped += 1
                    log.warning(
                        "gitlab_inline_comment_skipped",
                        extra={
                            "repo": repo,
                            "mr": number,
                            "path": path,
                            "line": line,
                            "status": exc.status,
                        },
                    )

            # A head advance during inline posting can make a position stale.
            # Re-check before the summary so we never anchor old findings to a
            # newer head; the next webhook/CI run will review the new head.
            current = self._get_mr_raw(encoded, number)
            current_state = str(current.get("state") or "")
            current_refs = current.get("diff_refs") or {}
            current_head = (
                current_refs.get("head_sha") or current.get("sha") or ""
            )
            if current_state not in ("opened", "locked") or current.get("merged_at"):
                raise GitLabAPIError(
                    409,
                    "merge request is no longer open before summary "
                    f"(state={current_state!r})",
                    mr_url,
                )
            if current_head and head_sha and current_head != head_sha:
                raise GitLabAPIError(
                    409,
                    "stale head: the merge request advanced while inline "
                    "comments were being posted",
                    mr_url,
                )

        if pending_note_id is not None:
            response = self._request(
                "PUT",
                f"{mr_url}/notes/{pending_note_id}",
                data={"body": body},
            )
        else:
            response = self._request(
                "POST", f"{mr_url}/notes", data={"body": body}
            )
        payload = response.json()
        note_id = payload.get("id") if isinstance(payload, dict) else None
        return {
            "id": note_id if note_id is not None else pending_note_id,
            "inline_posted": posted,
            "inline_skipped": skipped,
        }

    @staticmethod
    def _fingerprint_from_body(body: str, comments: list[dict]) -> str:
        match = _SUMMARY_MARKER_RE.search(body or "")
        if match:
            return match.group("fingerprint")
        blob = body + json.dumps(comments, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _pending_summary_body(fingerprint: str) -> str:
        return (
            "> **AI code review in progress.** Inline comments are being "
            "posted; this note will be updated when the review completes.\n\n"
            f"<!-- codereview-pending:{fingerprint} -->"
        )

    def _find_pending_summary(
        self, encoded: str, number: int, fingerprint: str
    ) -> int | None:
        notes = self._paginate(
            f"{self._mr_url(encoded, number)}/notes",
            params={"per_page": 100},
        )
        found: list[int] = []
        for note in notes:
            body = str(note.get("body") or "")
            for match in _PENDING_MARKER_RE.finditer(body):
                if match.group("fingerprint") == fingerprint:
                    note_id = note.get("id")
                    if isinstance(note_id, int):
                        found.append(note_id)
        # Oldest wins if two attempts raced; deleting the extra note would be
        # more destructive than leaving one pending note behind.
        return min(found) if found else None

    @staticmethod
    def _inline_body(comment: dict, fingerprint: str, index: int) -> str:
        text = str(comment.get("body") or "")
        marker = f"<!-- codereview-inline:{fingerprint}:{index} -->"
        return f"{text}\n\n{marker}"

    def _existing_inline_markers(
        self, encoded: str, number: int, fingerprint: str
    ) -> set[int]:
        found: set[int] = set()
        discussions = self._paginate(
            f"{self._mr_url(encoded, number)}/discussions",
            params={"per_page": 100},
        )
        for discussion in discussions:
            for note in discussion.get("notes") or []:
                for match in _INLINE_MARKER_RE.finditer(note.get("body") or ""):
                    if match.group("fingerprint") == fingerprint:
                        found.add(int(match.group("index")))
        return found

    def _old_path_map(self, encoded: str, number: int) -> dict[str, str]:
        try:
            files = self._paginate(
                f"{self._mr_url(encoded, number)}/diffs",
                params={"unidiff": "true", "per_page": 100},
            )
        except GitLabAPIError:
            return {}
        mapping: dict[str, str] = {}
        for file in files:
            new_path = file.get("new_path") or file.get("old_path") or ""
            if not new_path:
                continue
            old_path = file.get("old_path") or new_path
            if file.get("new_file"):
                old_path = new_path
            mapping[new_path] = old_path
        return mapping
