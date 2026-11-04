"""Helpers for the web application tests: layout tree walking, an HTTP test
client, and checks that run against a fresh process.

Run as a module, this file is that fresh process:
``python -m tests.app_helpers first-request /methodology`` builds the app,
starts it, requests the path first and prints the response as JSON;
``python -m tests.app_helpers urls`` prints every internal URL the shell and
the static pages render, plus those in the served index page;
``connect-request <root_path> <path>...`` requests each path with the ASGI
``root_path`` set as Posit Connect sets it and prints the responses by
path; ``connect-websocket <root_path> <path>`` opens a WebSocket the same
way and prints whether it was accepted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dash import Dash
from dash.development.base_component import Component

if TYPE_CHECKING:
    from starlette.testclient import TestClient

    from pcn_clone.app.routing import Page

ROOT = Path(__file__).resolve().parents[1]

STATIC_PAGES = ("home", "assess", "methodology", "release-notes")
"""Pages whose layout renders without store data or query parameters."""

URL_ATTRIBUTES = ("href", "src", "data-href")


def walk(node: Any) -> Iterator[Component]:
    """Every component in a layout tree, depth first."""
    if isinstance(node, Component):
        yield node
        for prop in node._prop_names:
            yield from walk(getattr(node, prop, None))
    elif isinstance(node, list | tuple):
        for child in node:
            yield from walk(child)


def find(root: Any, predicate: Callable[[Component], bool]) -> list[Component]:
    """The components in ``root`` that satisfy ``predicate``."""
    return [node for node in walk(root) if predicate(node)]


def render(page: Page) -> Component:
    """A page's layout, with a placeholder for each required parameter."""
    return page.layout(**dict.fromkeys(page.required, "missing"))


@contextmanager
def started_client(
    app: Dash, *, root_path: str = "", raise_errors: bool = True
) -> Iterator[TestClient]:
    """A started HTTP client on ``app`` whose requests carry ``root_path``;
    with ``raise_errors`` off, a server error is answered as a 500 instead of
    raised. Starlette deprecates its client's use of httpx; that notice is
    not about this code, so it is muted."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with")
        from starlette.testclient import TestClient

    with TestClient(
        app.server, root_path=root_path, raise_server_exceptions=raise_errors
    ) as client:
        yield client


def in_fresh_process(*args: str, env: dict[str, str] | None = None) -> Any:
    """Run this module in a new interpreter with ``args`` and return the JSON
    it prints. ``env`` adds environment variables."""
    result = subprocess.run(
        [sys.executable, "-m", "tests.app_helpers", *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"fresh process failed:\n{result.stderr}")
    return json.loads(result.stdout)


def _first_request(path: str) -> dict[str, Any]:
    from pcn_clone.app import create_app

    with started_client(create_app()) as client:
        response = client.get(path)
    return {"status": response.status_code, "text": response.text}


def _urls() -> dict[str, Any]:
    from pcn_clone.app import create_app
    from pcn_clone.app.pages import NOT_FOUND, PAGES

    app = create_app()
    trees = {
        "shell": app.layout,
        **{name: render(PAGES[name]) for name in STATIC_PAGES},
        "not-found": render(NOT_FOUND),
    }
    urls = [
        [source, attribute, value]
        for source, tree in trees.items()
        for node in walk(tree)
        for attribute in URL_ATTRIBUTES
        if isinstance(value := getattr(node, attribute, None), str)
    ]
    with started_client(app) as client:
        index = client.get("/").text
    urls += [
        ["index", attribute, value]
        for attribute, value in re.findall(r'\b(href|src)="([^"]*)"', index)
    ]
    return {"prefix": app.config.requests_pathname_prefix, "urls": urls}


def _connect_request(root_path: str, *paths: str) -> dict[str, Any]:
    from pcn_clone.app import create_app

    responses: dict[str, dict[str, Any]] = {}
    app = create_app()
    with started_client(app, root_path=root_path, raise_errors=False) as client:
        for path in paths:
            response = client.get(path)
            responses[path] = {
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "text": response.text,
            }
    return responses


def _connect_websocket(root_path: str, path: str) -> dict[str, Any]:
    from pcn_clone.app import create_app

    with started_client(create_app(), root_path=root_path) as client:
        try:
            with client.websocket_connect(
                path, headers={"origin": str(client.base_url)}
            ):
                pass
        except Exception as error:
            return {"accepted": False, "error": repr(error)}
    return {"accepted": True, "error": None}


if __name__ == "__main__":
    mode, *rest = sys.argv[1:]
    modes: dict[str, Callable[..., dict[str, Any]]] = {
        "first-request": _first_request,
        "urls": _urls,
        "connect-request": _connect_request,
        "connect-websocket": _connect_websocket,
    }
    print(json.dumps(modes[mode](*rest)))
