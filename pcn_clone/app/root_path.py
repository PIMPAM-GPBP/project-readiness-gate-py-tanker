"""ASGI middleware that adapts Posit Connect's forwarding to Dash.

Connect forwards a request for ``/content/<GUID>/…`` with the path intact
and announces the mount location in the ASGI ``root_path``
(``/content/<GUID>``, possibly with a trailing slash). Starlette strips
``root_path`` at a ``/`` boundary before matching routes, so FastAPI's own
routes, the assets and the WebSocket are found. Dash 4.4.1's FastAPI
middleware does not: it matches the raw path against its routes prefix,
and a page request that misses it is served without Dash's request context
and fails.

So :class:`FoldRootPath` runs outermost and, for requests under the mount
location, first normalises ``root_path`` (no trailing slash, so Starlette
strips it cleanly), then folds the mount location away only for the
requests Dash handles itself: the page document at the routes prefix and
the ``_dash-*`` endpoints. Everything else keeps ``root_path``, which is the
app's mount location; FastAPI's ``/docs``, for example, uses it to point at
``openapi.json``. When a proxy strips the prefix, or ``root_path`` is empty,
the middleware changes nothing.
"""

from __future__ import annotations

from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

DASH_ENDPOINT: Final = "_dash-"
"""The name prefix of Dash's own endpoints under the routes prefix."""


class FoldRootPath:
    """Normalise Connect's ``root_path`` and fold it into the path of Dash's
    page and ``_dash-*`` HTTP requests; ``routes_prefix`` is Dash's routes
    prefix (``/`` unless configured otherwise)."""

    def __init__(self, app: ASGIApp, *, routes_prefix: str) -> None:
        self.app = app
        self.routes_prefix = routes_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(self.adapt(scope), receive, send)

    def adapt(self, scope: Scope) -> Scope:
        """The scope the inner app receives: ``scope`` itself when it is not
        an HTTP or WebSocket request under its ``root_path``, otherwise a
        copy with ``root_path`` normalised and, for Dash's own HTTP
        requests, folded into the path."""
        if scope["type"] not in ("http", "websocket"):
            return scope
        root: str = scope.get("root_path", "").rstrip("/")
        path: str = scope["path"]
        if not root or not (path == root or path.startswith(root + "/")):
            return scope
        adapted = {**scope, "root_path": root}
        rest = path.removeprefix(root) or "/"
        if scope["type"] == "http" and self._is_dash(rest):
            adapted["path"] = rest
            adapted["root_path"] = ""
        return adapted

    def _is_dash(self, path: str) -> bool:
        """Whether Dash's middleware handles ``path``: the routes prefix,
        with or without its trailing slash, or one of Dash's endpoints."""
        prefix = self.routes_prefix
        return path in (prefix, prefix.rstrip("/")) or path.startswith(
            prefix + DASH_ENDPOINT
        )
