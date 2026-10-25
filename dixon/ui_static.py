"""Charts UI static files, served so the shell and script cannot drift.

Cloudflare caches `.js` and `.css` by extension and does not cache HTML.
Starlette's `StaticFiles` sends no `Cache-Control`, so a browser (and the
edge) can keep a fresh copy of an older `app.js` while `/ui/` is revalidated
and returns the new `index.html`. The Top 50 controls then render with no
click handler and `boot()` never calls `/screener/top50`. A normal refresh
revalidates the document and still uses that fresh script.

The shell is rewritten on each response to reference content-hashed asset
URLs (`/ui/app.<sha256>.js`). Those URLs are immutable. The HTML document and
the unversioned asset paths use `Cache-Control: no-cache` (browsers must
revalidate) and `CDN-Cache-Control: no-store` (Cloudflare must not retain a
copy). A 304 on `/ui/` is safe: its ETag covers the rewritten HTML, including
the current asset hashes.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from starlette.datastructures import URL, Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

# Browser must revalidate. Cloudflare does not cache a response whose
# Cache-Control is no-cache / no-store / private / max-age=0.
REVALIDATE = "no-cache"
# Explicit edge instruction. Cloudflare prefers CDN-Cache-Control over
# Cache-Control when both are present, and still forwards Cache-Control.
CDN_NO_STORE = "no-store"
IMMUTABLE = "public, max-age=31536000, immutable"
CDN_IMMUTABLE = "public, max-age=31536000"

# Assets the shell references. Anything else under /ui/ stays unversioned
# and revalidated (including the vendor chart bundle).
VERSIONED_ASSETS = ("styles.css", "app.js")

_FP_RE = re.compile(r"^(?P<stem>[A-Za-z0-9_-]+)\.(?P<fp>[0-9a-f]{16})\.(?P<ext>js|css)$")
_SHELL_MARKERS = (
    ('href="/ui/styles.css"', "styles.css", 'href="{url}"'),
    ('src="/ui/app.js"', "app.js", 'src="{url}"'),
)
_KEEP_304 = set(NotModifiedResponse.NOT_MODIFIED_HEADERS) | {"cdn-cache-control"}


def content_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def versioned_filename(directory: Path, filename: str) -> str:
    stem, ext = filename.rsplit(".", 1)
    return f"{stem}.{content_fingerprint(directory / filename)}.{ext}"


def versioned_url(directory: Path, filename: str) -> str:
    return "/ui/" + versioned_filename(directory, filename)


def render_index_html(directory: Path) -> str:
    """Return index.html with app.js and styles.css pointed at hashed URLs."""
    index = directory / "index.html"
    html = index.read_text(encoding="utf-8")
    for needle, filename, template in _SHELL_MARKERS:
        if needle not in html:
            raise RuntimeError(f"{index} is missing {needle}; cannot cache-bust the UI shell")
        html = html.replace(needle, template.format(url=versioned_url(directory, filename)), 1)
    return html


def resolve_versioned(directory: Path, request_path: str) -> Path | None:
    """Map `app.<fp>.js` to `app.js` when the fingerprint matches.

    Returns None when `request_path` is not a hashed asset URL.
    Raises FileNotFoundError when it is hashed but does not name the current file.
    """
    match = _FP_RE.match(request_path)
    if match is None:
        return None
    filename = f"{match.group('stem')}.{match.group('ext')}"
    if filename not in VERSIONED_ASSETS:
        raise FileNotFoundError(filename)
    root = directory.resolve()
    full = (root / filename).resolve()
    if full != root and root not in full.parents:
        raise FileNotFoundError(filename)
    if not full.is_file():
        raise FileNotFoundError(filename)
    if content_fingerprint(full) != match.group("fp"):
        raise FileNotFoundError(filename)
    return full


def _not_modified(headers: Headers) -> Response:
    # Starlette's NotModifiedResponse drops CDN-Cache-Control. Keep it so a
    # 304 cannot be stored at the edge under different rules than the 200.
    kept = {name: value for name, value in headers.items() if name.lower() in _KEEP_304}
    return Response(status_code=304, headers=kept)


def _revalidate(response: Response) -> None:
    response.headers["Cache-Control"] = REVALIDATE
    response.headers["CDN-Cache-Control"] = CDN_NO_STORE


class UiStaticFiles(StaticFiles):
    """`/ui/` mount: hashed app.js and styles.css, revalidated everything else."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True)
        self._root = Path(directory)

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        _revalidate(response)
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return _not_modified(response.headers)
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})

        if path in {"", "."}:
            if not str(scope.get("path", "")).endswith("/"):
                url = URL(scope=scope)
                response = RedirectResponse(url=url.replace(path=url.path + "/"))
                _revalidate(response)
                return response
            return self._html(scope)

        if path == "index.html":
            return self._html(scope)

        try:
            versioned = resolve_versioned(self._root, path)
        except FileNotFoundError:
            raise HTTPException(status_code=404)
        if versioned is not None:
            return self._immutable(versioned, scope)
        return await super().get_response(path, scope)

    def _html(self, scope: Scope) -> Response:
        body = render_index_html(self._root).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        response = HTMLResponse(
            content=body,
            headers={
                "Cache-Control": REVALIDATE,
                "CDN-Cache-Control": CDN_NO_STORE,
                "ETag": etag,
            },
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return _not_modified(response.headers)
        return response

    def _immutable(self, full: Path, scope: Scope) -> Response:
        stat_result = full.stat()
        response = FileResponse(
            full,
            stat_result=stat_result,
            headers={
                "Cache-Control": IMMUTABLE,
                "CDN-Cache-Control": CDN_IMMUTABLE,
            },
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return _not_modified(response.headers)
        return response
