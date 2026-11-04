"""The PIM-PAM web application: Dash on the FastAPI backend.

``create_app`` builds the shell (Mantine theme, toast container, suite
header) around the page content, which a small query router fills
(``routing.py``): every page is served at the app's root address and chosen
by ``?page=…``. Each module in ``pages/`` supplies one page's content only,
and ``pages/__init__.py`` lists them in the router's table; the stylesheets
and images in ``assets/`` are served and injected automatically.

Conventions for page modules:

- The element around the page content adds no width limit or padding, so a
  page can run full-bleed; pages that want the centred column wrap their
  content in ``.page``.
- A callback that waits on anything (a model call, a file, the network) is
  ``async def``: the FastAPI backend runs plain callbacks on its event loop.
- Page links are relative query URLs from ``routing.page_url``, so a visitor
  stays on whichever root address they arrived at. Asset URLs go through
  ``dash.get_asset_url``, so assets, callbacks and the WebSocket follow the
  path prefix (set ``DASH_REQUESTS_PATHNAME_PREFIX``: ``/content/<GUID>/`` on
  Posit Connect, or for example ``/pcn/`` behind a proxy). Connect forwards
  the full path and announces the mount location in the ASGI ``root_path``;
  ``root_path.py`` folds it away for Dash's own requests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from typing import Any, Final

import dash_mantine_components as dmc
import httpx
from dash import Dash, dcc, html

from pcn_clone import ai
from pcn_clone.app.notify import TOASTS_ID
from pcn_clone.app.pages import NOT_FOUND, PAGES
from pcn_clone.app.review import run_stores
from pcn_clone.app.review.run import WEBSOCKET_INACTIVITY_TIMEOUT_MS
from pcn_clone.app.root_path import FoldRootPath
from pcn_clone.app.routing import (
    CONTENT_ID,
    LOCATION_ID,
    STATE_ID,
    register_router,
)
from pcn_clone.app.shell import register_shell_callbacks, shell_header
from pcn_clone.app.theme import BRAND_THEME

FONTS_URL: Final = (
    "https://fonts.googleapis.com/css2"
    "?family=Fira+Sans:wght@400;500;600;700;800"
    "&family=Inter:wght@400..700"
    "&family=IBM+Plex+Mono:wght@400;500;600"
    "&display=swap"
)
"""Brand faces. Fira Sans ships static weights only, so each is listed."""

DESCRIPTION: Final = (
    "AI-assisted readiness gate for Public Investment Management Project Concept Notes."
)
"""The page's ``<meta name="description">``, the same for every page."""


@cache
def create_app() -> Dash:
    """Return the process's Dash application, building it on the first call.

    Dash keeps its callback registry and its notion of the current app per
    process, and importing the pages registers their callbacks there; a
    second app would register every callback again. So a process holds one
    app: later calls return the same instance.

    The AI settings and model client are resolved first, so a malformed or
    incomplete ``PCN_AI_*`` configuration (such as the OpenRouter provider
    without ``OPENROUTER_API_KEY``) stops the app at startup instead of at
    the first evaluation.

    The review run's stores sit in the shell, outside the pages, so a run
    keeps saving results wherever the reviewer goes. The run travels over
    the WebSocket, which the browser closes after a silence of
    ``websocket_inactivity_timeout``; that is set past the longest model
    call, so a run waiting on one is never cut off.
    """
    ai.startup()
    app = Dash(
        __name__,
        backend="fastapi",
        external_stylesheets=[FONTS_URL],
        title="PIM-PAM",
        update_title="",
        meta_tags=[{"name": "description", "content": DESCRIPTION}],
        suppress_callback_exceptions=True,
        websocket_inactivity_timeout=WEBSOCKET_INACTIVITY_TIMEOUT_MS,
    )
    app.layout = dmc.MantineProvider(
        theme=BRAND_THEME,
        forceColorScheme="light",
        children=[
            dmc.NotificationContainer(id=TOASTS_ID, position="top-center"),
            dcc.Location(id=LOCATION_ID),
            shell_header(),
            html.Main(id=CONTENT_ID, className="shell-main", disable_n_clicks=True),
            dcc.Store(id=STATE_ID),
            run_stores(),
        ],
    )
    register_router(app, PAGES, NOT_FOUND)
    register_shell_callbacks(app)
    app.server.add_middleware(
        FoldRootPath, routes_prefix=app.config.routes_pathname_prefix
    )
    _finish_setup_on_startup(app)
    return app


def _finish_setup_on_startup(app: Dash) -> None:
    """Run Dash's first-request setup while the server starts.

    Dash collects ``assets/`` in a hook that the FastAPI backend runs only
    before requests to ``/`` and ``/_dash-*``, while it serves the page at
    every path. A page requested first at any other path on a fresh server
    would otherwise be served before the hook, without the app's
    stylesheets. One internal request to ``/`` during startup runs the hook
    first; if it fails, startup fails with it. At shutdown the same lifespan
    closes the AI client's connections. Both ride on the ASGI lifespan, so
    they are off when the server runs with ``--lifespan off``.
    """
    server = app.server
    serve = server.router.lifespan_context

    @asynccontextmanager
    async def lifespan(asgi_app: Any) -> AsyncIterator[Any]:
        async with serve(asgi_app) as state:
            transport = httpx.ASGITransport(app=server)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://app"
            ) as client:
                response = await client.get(app.config.routes_pathname_prefix)
                response.raise_for_status()
            try:
                yield state
            finally:
                await ai.aclose()

    server.router.lifespan_context = lifespan
