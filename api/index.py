"""Vercel serverless entry point.

Vercel's Python runtime looks for an ASGI application in ``api/``, so this module
exists to expose the one built in :mod:`vc_audit.api.main`. It adds no valuation
behaviour, so the deployed service and a local ``vc-audit serve`` cannot diverge.

The one thing it does add is **restoring the request path**, which the platform
otherwise discards. That is not cosmetic: without it the deployment answers every
route with the wrong thing while local development works perfectly, which is the
worst shape a bug can have.

Two environment variables matter on a serverless host, both set in
``vercel.json``:

* ``VC_AUDIT_OUTPUT_DIR`` must point at ``/tmp``. Everything else on the
  instance is read-only, so writing an evidence pack anywhere else raises.
* ``VC_AUDIT_EXAMPLES_DIR`` is deliberately *not* set. A relative path would be
  resolved against a working directory this function does not control; the
  package instead walks up from its own location, which is absolute and correct
  on every host.

**A serverless deployment cannot keep an evidence archive.** ``/tmp`` does not
outlive the instance, so a run is retrievable by id only until the next cold
start. That is a limitation of the host rather than of the tool: the valuation,
the memo and the full audit trail all still come back in the response body, but
``GET /api/valuations/{run_id}`` will start returning 404. A deployment needing a
durable archive wants a host with a persistent volume, or an object store behind
``vc_audit.reporting.evidence``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

# The package lives under src/, which a serverless bundle does not put on the
# import path the way `pip install -e .` does locally. Prepending it here keeps
# the deployment working without needing the build step to install the project
# itself, and is a no-op anywhere the package is already importable.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vc_audit.api.main import app as _app  # noqa: E402  (import follows the path fix above)

#: Query parameter carrying the URL the browser actually requested.
#:
#: ``vercel.json`` rewrites every incoming path to this function and hands it the
#: *rewritten* path, so by the time the request arrives, ``/`` and
#: ``/api/examples`` are indistinguishable: both read as ``/api/index``. Any shim
#: that guesses from the path alone must therefore get one of them wrong, which
#: is exactly what happened -- an earlier version mapped the function path back
#: to ``/`` and consequently served the HTML page in answer to every API call.
#:
#: The rewrite now captures the original path into this parameter, so it is
#: carried explicitly rather than inferred. It is stripped from the query string
#: before the application sees it.
PATH_PARAM = "__vc_path"

#: The path this file is served at, used only as a fallback for a host that
#: rewrites without supplying the parameter above.
_FUNCTION_PATH = "/api/index"


class _RestoreRequestPath:
    """Put the browser's original path back before routing runs.

    A minimal ASGI wrapper rather than FastAPI middleware, because the path must
    be correct *before* the router looks at it, not after it has already failed
    to match. Hosts that pass the original path through are unaffected: neither
    branch fires and the scope is forwarded untouched.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        # Must be `async def`, not a sync function returning the coroutine:
        # ASGI servers infer the protocol version by inspecting this callable,
        # and a synchronous one is read as the legacy two-argument style.
        if scope.get("type") == "http":
            scope = self._restore(scope)
        await self._app(scope, receive, send)

    @staticmethod
    def _restore(scope: dict) -> dict:
        raw_query = scope.get("query_string", b"").decode("latin-1")
        params = parse_qsl(raw_query, keep_blank_values=True)

        original = next((value for key, value in params if key == PATH_PARAM), None)
        if original:
            remaining = [(k, v) for k, v in params if k != PATH_PARAM]
            # Collapse the doubled slash a "/$1" capture produces for the root.
            path = "/" + original.lstrip("/")
            return {
                **scope,
                "path": path,
                "raw_path": path.encode("utf-8"),
                "query_string": urlencode(remaining).encode("latin-1"),
            }

        # Fallback: strip the function prefix. Only an exact match or a
        # `/api/index/...` prefix, so the real API routes are never touched.
        path = scope.get("path", "")
        if path == _FUNCTION_PATH or path.startswith(_FUNCTION_PATH + "/"):
            restored = path[len(_FUNCTION_PATH) :] or "/"
            return {**scope, "path": restored, "raw_path": restored.encode("utf-8")}

        return scope


app = _RestoreRequestPath(_app)

__all__ = ["app"]
