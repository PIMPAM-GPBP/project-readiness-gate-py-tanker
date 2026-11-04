# Connect root_path fix for the Dash FastAPI backend

Status: revised 4 November 2026 after two team reviews (all points adopted below); awaiting sign-off, implementation has not started.

Target: the app serves its pages on Posit Connect, which forwards requests with the content prefix left in the path and announced through the ASGI `root_path`, instead of stripping it as the README and the Connect-like test scaffold assume. Diagnosed on 4 November 2026 from the prod Connect job log `rstudio-connect.1002.0.6002.run_fastapi_app.Jfwrrmo18NMTuneh.log` against `main` at `3f85094` (Python 3.11.9 backport merged).

**Expected surface: four existing files plus one new application module and one new test module.** No feature, UI, prompt, schema, stored-data or dependency changes. The Python 3.11.9 backport is confirmed working by the same log and is not touched.

## What the log shows

| Fact | Evidence (log line) |
| --- | --- |
| Connect 2026.08.0 on RHEL 8.10 selected Python 3.11.9 and built the environment | `Bootstrapping environment using Python 3.11.9 …`, `Environment ready` |
| The app loaded and started with the fake provider | `Loading code from "pcn_clone.app.server:server"`, `PCN_AI_PROVIDER=fake: …`, `Application startup complete.` |
| The startup self-request to `/` succeeded | `HTTP Request: GET http://app/ "HTTP/1.1 200 OK"` |
| Connect's own probe from localhost got a 403 (see "Open question") | `127.0.0.1:45136 - "GET / HTTP/1.1" 403 Forbidden` |
| The first real page request failed | `10.168.232.70:0 - "GET /content/7ce5f8ae-…/ HTTP/1.1" 500`, ending in `RuntimeError: No active request in context` raised from `dash/backends/_fastapi.py:109` via the index route (`_fastapi.py:321`) |

Content settings confirmed by the hosting colleague: `PCN_AI_PROVIDER=fake` and `DASH_REQUESTS_PATHNAME_PREFIX=/content/7ce5f8ae-39ef-4c80-a1ea-7d67e030679b/`. The content GUID is `7ce5f8ae-39ef-4c80-a1ea-7d67e030679b`.

## Cause

uvicorn's access log prints `scope["path"]`, so the app received the **full** path `/content/<GUID>/`, not `/`. The request still reached Dash's index route (registered at `/`, the routes prefix) because Starlette 1.7 strips `scope["root_path"]` before matching routes (`starlette/_utils.py:get_route_path`). So Connect sets `root_path` to `/content/<GUID>` and leaves the path intact.

Dash 4.4.1's FastAPI middleware (`dash/backends/_fastapi.py:220-240`) does not use `root_path`. It compares the raw `scope["path"]` with the routes prefix (`/`) and `/_dash-…`; on a mismatch it passes the request through **without** storing it in the request context variable. The index endpoint then calls `request_adapter()`, which needs that variable, and raises.

The same mechanism affects every page document request. Dash's `_dash-*` routes happen to work (they set the context themselves), which is why the local reproduction shows 200 for `/content/abc/_dash-layout` but 500 for `/content/abc/`.

Nothing on the Connect side is misconfigured. Posit's automatic prefix handling applies to the Flask-based **Dash app mode**; this app runs in **python-fastapi** mode (it needs the FastAPI backend for the review WebSocket), where Connect sets no Dash variables, so `DASH_REQUESTS_PATHNAME_PREFIX` remains required and is set correctly.

## Evidence collected locally (Python 3.11.9, `main` at 3f85094)

Requests were sent with `httpx.ASGITransport(app=server, root_path="/content/abc")` and `DASH_REQUESTS_PATHNAME_PREFIX=/content/abc/`, the shape the log shows.

| Variant | Result |
| --- | --- |
| Current code, Connect-shaped request `GET /content/abc/` | **500**, same `RuntimeError` |
| Current code, Connect-shaped `GET /content/abc/_dash-layout` | 200 |
| Current code, prefix stripped by the proxy (the scaffold's assumption) | 200 for `/` and `/_dash-layout` |
| Configuration-only attempt: also set `DASH_ROUTES_PATHNAME_PREFIX=/content/abc/` | **404** for both the index and `_dash-layout` (Dash registers routes under the prefix, Starlette strips `root_path` before matching). Not viable. |
| First proposal: outermost ASGI middleware that folds `root_path` into `path` for **every** request | **200** for `/content/abc/`, `/content/abc`, `/content/abc/_dash-layout`, `/content/abc/_dash-dependencies`, `/content/abc/assets/brand.css`, `/content/abc/?page=projects`; the index HTML still references `/content/abc/_dash-…`. **Team review:** clearing `root_path` everywhere breaks FastAPI's `/content/abc/docs`, whose HTML then points at `/openapi.json` outside the content root; `root_path` is the app's mount location and must survive for everything that is not Dash's own. |
| Team prototype: fold only for the HTTP index and `/_dash-*` paths | Fixes the failure while `/docs`, assets and the WebSocket handshake keep working unchanged. Adopted below. |
| Team finding: `root_path` **with** a trailing slash (`/content/abc/`) | `GET /content/abc/_dash-layout` returns **200 HTML** from Dash's catch-all instead of JSON, because Starlette's `get_route_path` only strips `root_path` at a `/` boundary. A status-only test passes wrongly; tests must assert content. |

The fold is a no-op when a proxy strips the prefix (local runs, the current test scaffold) and when `root_path` is empty, so development serving is unaffected. Connect's vanity URL arrives as `root_path` `/<name>` with path `/<name>/`; the remainder is `/`, so the index is folded there too.

## Change surface

| File | Planned change |
| --- | --- |
| `pcn_clone/app/root_path.py` (new) | A pure-ASGI middleware class, `FoldRootPath`, with the rule below, in its own module so it can be unit-tested with a recording inner app. Docstring: Connect forwards the full path and announces the mount location in `root_path`; Dash 4.4.1's FastAPI middleware matches the raw path against its routes prefix, so the prefix is folded away only for the requests Dash handles itself. |
| [pcn_clone/app/__init__.py](../pcn_clone/app/__init__.py) | In `create_app`, after the `Dash(...)` constructor (line 85) and before `_finish_setup_on_startup`, register it with `app.server.add_middleware(FoldRootPath, routes_prefix=app.config.routes_pathname_prefix)`. Registered last, it runs outermost, ahead of Dash's middleware. Update the module docstring's prefix note (lines 17-21). |
| [tests/browser/connect_like.py](../tests/browser/connect_like.py) | Forward requests the way Connect does: keep `scope["path"]` intact and set `scope["root_path"]` to the root without its trailing slash, instead of stripping the prefix. Update the module docstring (lines 1-19), which currently states that Connect strips the root. The vanity-root and refusal rules stay as they are. |
| [tests/app_helpers.py](../tests/app_helpers.py) | Add a fresh-process mode, `connect-request <root_path> <path>`, that builds the app with the environment's prefix, starts it through `started_client` (Starlette's `TestClient` runs the lifespan) with `TestClient(..., root_path=...)`, and prints status, content type and body. Mirrors the existing `first-request` and `urls` modes. |
| `tests/test_connect_root_path.py` (new) | Two layers, see "Tests" below. |
| [README.md](../README.md) | In "Deploying on Posit Connect", replace the third first-deploy bullet (Connect strips `/content/<GUID>/`) with the observed behaviour: Connect forwards the full path with the ASGI `root_path`; the app folds it away only for Dash's page and `_dash-*` requests, and everything else (assets, the WebSocket, FastAPI's `/docs`) keeps the mount location. Keep the prefix setting instruction. |

### Rule (precise): normalise the mount path, then fold Dash paths

Two separate steps, for `http` and `websocket` scopes (lifespan and anything else pass through untouched):

1. Let `root` be `scope["root_path"]` with any trailing `/` removed. If `root` is empty, pass the original scope through.
2. The path must be `root` exactly, or start with `root + "/"`. `/content/abcd` does **not** match `root` `/content/abc`. Otherwise pass the original scope through.
3. **Normalise.** Work on a **copy** of the scope with `root_path` set to `root` (trailing slash removed, mount prefix preserved). This alone is what assets, FastAPI's `/docs` and `/openapi.json`, and the WebSocket handshake receive: Starlette then strips the root at a `/` boundary and routes them correctly whether or not Connect's `root_path` carries a trailing slash. The original scope dict is never mutated.
4. **Fold.** Let `rest` be the path after `root`, or `/` when it is empty. Only for an `http` scope where `rest` is the routes prefix (`/`, with or without the trailing slash) or starts with the routes prefix followed by `_dash-`, additionally set `path` to `rest` and `root_path` to `""` on the copy. This is what Dash's own middleware needs.
5. Call the inner app with the copy.

### Tests

- **Unit, no server** (`FoldRootPath` around a recording inner app, called directly): index with and without trailing slash is folded (`path` `/`, `root_path` `""`); `_dash-layout` and `_dash-update-component` are folded; the same with `root_path` `/content/abc/` gives identical results; `/content/abc/assets/brand.css`, `/content/abc/docs` and `/content/abc/openapi.json` are **normalised only** (`path` unchanged, `root_path` `/content/abc` even when given with a trailing slash); a `websocket` scope under the root is normalised only; `/content/abcd/` against root `/content/abc`, an empty `root_path`, and a lifespan scope are passed through untouched; the caller's scope dict is unchanged after every call.
- **End to end, fresh process with `DASH_REQUESTS_PATHNAME_PREFIX=/content/abc/` and startup run** (the regression test for the log): with `root_path` `/content/abc` and again with `/content/abc/`:
  - `GET /content/abc/` and `/content/abc/?page=projects`: 200, `text/html`, body contains the React entry point and every generated `_dash-`, asset and WebSocket URL starts with `/content/abc/`.
  - `GET /content/abc/_dash-layout`: 200, `application/json`, body parses as the layout (a `MantineProvider` at the top).
  - `GET /content/abc/_dash-dependencies`: 200, `application/json`, body parses as a list of callbacks.
  - `GET /content/abc/assets/brand.css`: 200, `text/css`, body contains a known brand token.
  - `GET /content/abc/docs`: 200, `text/html`, body references `/content/abc/openapi.json` (the mount location survives).
  - `GET /content/abc/openapi.json`: 200, `application/json`, body parses with an `openapi` key.
  - The WebSocket handshake at Dash's socket path under the root is accepted (Starlette `TestClient.websocket_connect` with the root path), with the trailing-slash root as well.
  - Without `root_path`, `GET /` is still 200 HTML (development serving).

## Implementation sequence and acceptance

1. **Add `root_path.py`, register it in `create_app`, update docstrings**; keep ruff, format and mypy strict clean.
2. **Add the tests** and confirm the end-to-end test fails on `main` without the middleware (the 500) and passes with it; confirm the `/docs` assertion fails against the first, fold-everything proposal.
3. **Switch the Connect-like scaffold** to `root_path` forwarding and run the browser suite: `uv run pytest -q -m browser`, in particular `tests/browser/test_connect_roots.py` (content URL, vanity URL, review run over the WebSocket). All must pass with Chrome present; a missing-Chrome skip is not a pass.
4. **Run the full checks on 3.11.9**: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy pcn_clone`, `uv run pytest -q -m "not live"`. No dependency or lock change is expected, so `requirements.txt` stays as is; regenerate `manifest.json` only because the README and `__init__.py` checksums change, using the README command (the manifest lists `pcn_clone/app/root_path.py` as a new file).
5. **Redeploy on prod Connect from `main`** with the same two variables, then run the first-deploy checks from the README: direct links to each page at both the content URL and the vanity URL, stylesheets and callbacks loading, and a complete review run over the WebSocket. Record the result in this document.

## Open question (not blocking)

Immediately after startup, a request to `/` from `127.0.0.1` received a 403. The app has no code path that returns 403 for a plain HTTP request (Dash only uses 403 for a WebSocket origin mismatch), so it most likely comes from Connect's runtime wrappers (`connect_fastapi_runtime.py`, `_starlette_support.py`) handling Connect's own readiness probe. Connect forwarded the real request a few milliseconds later, so it did not block the deployment. Watch for it in the next log; if the content shows as running, no action.

## Team review

Second review (4 November 2026) found that the first revision normalised the trailing slash only in a local variable, so assets, `/docs`, `/openapi.json` and the WebSocket still reached Starlette with the unnormalised root and fell into Dash's catch-all (200 HTML instead of CSS or JSON). The rule is now two steps: normalise the mount path for every matching HTTP and WebSocket request on a copied scope, and fold the path only for Dash's index and `_dash-*` HTTP requests. The "untouched" test assertions became "normalised only" for those requests, and `/openapi.json` and the WebSocket handshake were added to the end-to-end cases. The reviewer tested this adjustment with the trailing-slash root and all cases passed.

First review (4 November 2026) asked for three changes, all adopted: fold only the HTTP index and `_dash-*` requests so `root_path` survives for everything else; match the prefix exactly or at a `/` boundary, normalise trailing slashes, copy the scope, and test those cases; assert response content and generated URLs rather than status codes, with startup run and the prefix configured before the cached app is built. Please confirm the revised scope. Implementation begins after sign-off.
