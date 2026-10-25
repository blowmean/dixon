"""UI shell must not be cacheable apart from the script that wires Top 50."""

from __future__ import annotations

import hashlib
import re

from pathlib import Path

from fastapi.testclient import TestClient

from dixon.api import app
from dixon.ui_static import content_fingerprint

STATIC = Path(__file__).resolve().parents[1] / "dixon" / "static"
_APP_JS = re.compile(r'src="(/ui/app\.[0-9a-f]{16}\.js)"')
_CSS = re.compile(r'href="(/ui/styles\.[0-9a-f]{16}\.css)"')


def _client() -> TestClient:
    return TestClient(app)


def _boot_body(js: str) -> str:
    body = js.split("async function boot()", 1)[1]
    return body.split("document.readyState", 1)[0]


def test_ui_shell_points_at_hashed_assets_and_revalidates():
    client = _client()
    page = client.get("/ui/")
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["cdn-cache-control"] == "no-store"
    assert "btnScreener" in page.text
    assert 'src="/ui/app.js"' not in page.text
    assert 'href="/ui/styles.css"' not in page.text

    js_url = _APP_JS.search(page.text)
    css_url = _CSS.search(page.text)
    assert js_url and css_url
    fp_js = content_fingerprint(STATIC / "app.js")
    fp_css = content_fingerprint(STATIC / "styles.css")
    assert js_url.group(1) == f"/ui/app.{fp_js}.js"
    assert css_url.group(1) == f"/ui/styles.{fp_css}.css"

    same = client.get("/ui/index.html")
    assert same.status_code == 200
    assert same.text == page.text
    assert same.headers["etag"] == page.headers["etag"]

    cached = client.get("/ui/", headers={"If-None-Match": page.headers["etag"]})
    assert cached.status_code == 304
    assert cached.headers["cache-control"] == "no-cache"
    assert cached.headers["cdn-cache-control"] == "no-store"


def test_hashed_app_js_is_immutable_and_wires_screener_before_charts():
    client = _client()
    page = client.get("/ui/")
    js_url = _APP_JS.search(page.text).group(1)
    js = client.get(js_url)
    assert js.status_code == 200, js.text
    assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert js.headers["cdn-cache-control"] == "public, max-age=31536000"
    assert hashlib.sha256(js.content).hexdigest()[:16] in js_url
    assert "/screener/top50" in js.text
    assert "function bindScreener" in js.text
    boot = _boot_body(js.text)
    assert boot.index("bindScreener()") < boot.index("await loadChart()")
    assert boot.index("loadScreener()") < boot.index("await loadChart()")

    plain = client.get("/ui/app.js")
    assert plain.status_code == 200
    assert plain.content == js.content
    assert plain.headers["cache-control"] == "no-cache"
    assert plain.headers["cdn-cache-control"] == "no-store"

    css = client.get("/ui/styles.css")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "no-cache"
    hashed_css = client.get(_CSS.search(page.text).group(1))
    assert hashed_css.status_code == 200
    assert hashed_css.content == css.content
    assert "immutable" in hashed_css.headers["cache-control"]

    stale = client.get("/ui/app.0000000000000000.js")
    assert stale.status_code == 404

    vendor = client.get("/ui/vendor/lightweight-charts.standalone.production.js")
    assert vendor.status_code == 200
    assert vendor.headers["cache-control"] == "no-cache"


def test_ui_cache_headers_do_not_weaken_api_auth(monkeypatch):
    monkeypatch.setattr("dixon.api.API_KEY", "secret")
    client = _client()
    public = {"Host": "dixon.robonet.cc"}

    shell = client.get("/ui/", headers=public)
    assert shell.status_code == 200

    denied = client.get("/screener/top50", params={"market": "US"}, headers=public)
    assert denied.status_code == 401

    by_key = client.get(
        "/screener/top50",
        params={"market": "US"},
        headers={**public, "X-API-Key": "secret"},
    )
    assert by_key.status_code == 200

    by_access = client.get(
        "/screener/top50",
        params={"market": "US"},
        headers={**public, "Cf-Access-Jwt-Assertion": "header-from-cloudflare"},
    )
    assert by_access.status_code == 200

    local = client.get(
        "/screener/top50",
        params={"market": "US"},
        headers={"Host": "127.0.0.1"},
    )
    assert local.status_code == 200

    wrong_key = client.get(
        "/screener/top50",
        params={"market": "US"},
        headers={**public, "X-API-Key": "nope"},
    )
    assert wrong_key.status_code == 401
