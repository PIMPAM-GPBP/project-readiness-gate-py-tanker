"""Serving under Posit Connect's ``root_path``: Connect forwards the full
path and announces the mount location in the ASGI ``root_path``.
``FoldRootPath`` normalises that root for every request under it and folds
it into the path only for Dash's page and ``_dash-*`` HTTP requests, leaving
the caller's scope as it was. End to end, on a fresh server with Dash's
prefix set to the content URL, the page, its callbacks, assets, FastAPI's
docs and the WebSocket all work with the root given with or without its
trailing slash, and development serving without a root is unchanged."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from pcn_clone.app.root_path import FoldRootPath
from tests.app_helpers import in_fresh_process

PREFIX = "/content/abc/"
ROOTS = ("/content/abc", "/content/abc/")
"""Connect's ``root_path`` for ``PREFIX``, without and with its slash."""

ENV = {"DASH_REQUESTS_PATHNAME_PREFIX": PREFIX}

INDEX_PATHS = (PREFIX, PREFIX + "?page=projects")
PATHS = (
    *INDEX_PATHS,
    PREFIX + "_dash-layout",
    PREFIX + "_dash-dependencies",
    PREFIX + "assets/brand.css",
    PREFIX + "docs",
    PREFIX + "openapi.json",
)
SOCKET_PATH = PREFIX + "_dash-ws-callback"
"""Dash's WebSocket: its routes prefix (``/``) plus ``_dash-ws-callback``."""

BRAND_TOKEN = "--dark-blue: #374291;"
"""A line of ``assets/brand.css``."""

DASH_CONFIG = re.compile(
    r'<script id="_dash-config" type="application/json">(.*?)</script>', re.S
)


# --- The middleware alone ------------------------------------------------------


def scope_for(path: str, root_path: str, kind: str = "http") -> dict[str, Any]:
    """A minimal ASGI scope for ``path`` forwarded with ``root_path``."""
    return {
        "type": kind,
        "path": path,
        "raw_path": path.encode(),
        "root_path": root_path,
        "query_string": b"",
        "headers": [],
    }


def received(scope: dict[str, Any]) -> dict[str, Any]:
    """The scope the inner app receives when ``FoldRootPath`` (with Dash's
    default routes prefix) is called with ``scope``; asserts the caller's
    scope is left as it was."""
    before = copy.deepcopy(scope)
    seen: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope)

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        pass

    asyncio.run(FoldRootPath(inner, routes_prefix="/")(scope, receive, send))
    assert scope == before
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("root_path", ROOTS)
@pytest.mark.parametrize(
    ("path", "rest"),
    [
        ("/content/abc/", "/"),
        ("/content/abc", "/"),
        ("/content/abc/_dash-layout", "/_dash-layout"),
        ("/content/abc/_dash-update-component", "/_dash-update-component"),
    ],
)
def test_dash_requests_are_folded(root_path: str, path: str, rest: str) -> None:
    inner = received(scope_for(path, root_path))
    assert (inner["path"], inner["root_path"]) == (rest, "")
    assert inner["raw_path"] == path.encode()


@pytest.mark.parametrize("root_path", ROOTS)
@pytest.mark.parametrize(
    "path",
    [
        "/content/abc/assets/brand.css",
        "/content/abc/docs",
        "/content/abc/openapi.json",
    ],
)
def test_other_requests_are_normalised_only(root_path: str, path: str) -> None:
    inner = received(scope_for(path, root_path))
    assert (inner["path"], inner["root_path"]) == (path, "/content/abc")


@pytest.mark.parametrize("root_path", ROOTS)
def test_a_websocket_is_normalised_only(root_path: str) -> None:
    inner = received(scope_for(SOCKET_PATH, root_path, "websocket"))
    assert (inner["path"], inner["root_path"]) == (SOCKET_PATH, "/content/abc")


@pytest.mark.parametrize(
    "scope",
    [
        scope_for("/content/abcd/", "/content/abc"),
        scope_for("/content/abcd/_dash-layout", "/content/abc/"),
        scope_for("/elsewhere/", "/content/abc"),
        scope_for("/", ""),
        scope_for("/_dash-layout", ""),
        scope_for("/", "/"),
        scope_for("/content/abcd/", "/content/abc", "websocket"),
        {"type": "lifespan"},
    ],
    ids=[
        "sibling-root",
        "sibling-root-trailing-slash",
        "outside-root",
        "no-root",
        "no-root-dash",
        "slash-root",
        "sibling-root-websocket",
        "lifespan",
    ],
)
def test_requests_outside_the_root_pass_through(scope: dict[str, Any]) -> None:
    assert received(scope) is scope


# --- End to end, on a fresh server ---------------------------------------------


@pytest.fixture(scope="module")
def served() -> dict[str, dict[str, Any]]:
    """Each root's responses to ``PATHS`` by path, plus its WebSocket
    handshake under ``"socket"``, and the development index (no root) under
    ``""``; one fresh server per root and check."""
    jobs = {
        **{(root, "http"): ("connect-request", root, *PATHS) for root in ROOTS},
        **{
            (root, "socket"): ("connect-websocket", root, SOCKET_PATH) for root in ROOTS
        },
        ("", "http"): ("connect-request", "", "/"),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = dict(
            zip(
                jobs,
                pool.map(lambda args: in_fresh_process(*args, env=ENV), jobs.values()),
                strict=True,
            )
        )
    served: dict[str, dict[str, Any]] = {}
    for (root, kind), result in results.items():
        if kind == "socket":
            served.setdefault(root, {})["socket"] = result
        else:
            served.setdefault(root, {}).update(result)
    return served


def assert_index(response: dict[str, Any]) -> None:
    """A complete index page whose Dash URLs all carry ``PREFIX``."""
    assert response["status"] == 200
    assert response["content_type"].startswith("text/html")
    text = response["text"]
    assert 'id="react-entry-point"' in text
    urls = [
        url
        for url in re.findall(r'\b(?:href|src)="([^"]*)"', text)
        if "_dash-" in url or "/assets/" in url
    ]
    assert any("/assets/brand.css" in url for url in urls)
    assert any("_dash-component-suites" in url for url in urls)
    assert [url for url in urls if not url.startswith(PREFIX)] == []
    match = DASH_CONFIG.search(text)
    assert match is not None
    config = json.loads(match.group(1))
    assert config["requests_pathname_prefix"] == PREFIX
    assert config["websocket"]["url"] == SOCKET_PATH


@pytest.mark.parametrize("root_path", ROOTS)
@pytest.mark.parametrize("path", INDEX_PATHS)
def test_the_page_is_served_under_the_root(
    served: dict[str, dict[str, Any]], root_path: str, path: str
) -> None:
    assert_index(served[root_path][path])


@pytest.mark.parametrize("root_path", ROOTS)
def test_the_layout_is_served_under_the_root(
    served: dict[str, dict[str, Any]], root_path: str
) -> None:
    response = served[root_path][PREFIX + "_dash-layout"]
    assert response["status"] == 200
    assert response["content_type"].startswith("application/json")
    assert json.loads(response["text"])["type"] == "MantineProvider"


@pytest.mark.parametrize("root_path", ROOTS)
def test_the_callbacks_are_served_under_the_root(
    served: dict[str, dict[str, Any]], root_path: str
) -> None:
    response = served[root_path][PREFIX + "_dash-dependencies"]
    assert response["status"] == 200
    assert response["content_type"].startswith("application/json")
    callbacks = json.loads(response["text"])
    assert isinstance(callbacks, list)
    assert callbacks
    assert all("output" in callback for callback in callbacks)


@pytest.mark.parametrize("root_path", ROOTS)
def test_assets_are_served_under_the_root(
    served: dict[str, dict[str, Any]], root_path: str
) -> None:
    response = served[root_path][PREFIX + "assets/brand.css"]
    assert response["status"] == 200
    assert response["content_type"].startswith("text/css")
    assert BRAND_TOKEN in response["text"]


@pytest.mark.parametrize("root_path", ROOTS)
def test_the_api_docs_keep_the_mount_location(
    served: dict[str, dict[str, Any]], root_path: str
) -> None:
    docs = served[root_path][PREFIX + "docs"]
    assert docs["status"] == 200
    assert docs["content_type"].startswith("text/html")
    assert PREFIX + "openapi.json" in docs["text"]
    schema = served[root_path][PREFIX + "openapi.json"]
    assert schema["status"] == 200
    assert schema["content_type"].startswith("application/json")
    assert "openapi" in json.loads(schema["text"])


@pytest.mark.parametrize("root_path", ROOTS)
def test_the_websocket_is_accepted_under_the_root(
    served: dict[str, dict[str, Any]], root_path: str
) -> None:
    assert served[root_path]["socket"] == {"accepted": True, "error": None}


def test_the_page_is_served_without_a_root(
    served: dict[str, dict[str, Any]],
) -> None:
    response = served[""]["/"]
    assert response["status"] == 200
    assert response["content_type"].startswith("text/html")
    assert 'id="react-entry-point"' in response["text"]
