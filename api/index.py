"""Vercel serverless entry point.

Vercel's Python runtime looks for an ASGI application in ``api/``, so this
module exists only to expose the one built in :mod:`vc_audit.api.main`. It adds
no behaviour, so the deployed service and a local ``vc-audit serve`` cannot
diverge.

Two environment variables matter on a serverless host, and both are set in
``vercel.json``:

* ``VC_AUDIT_OUTPUT_DIR`` must point at ``/tmp``. Everything else on the
  instance is read-only, so writing an evidence pack anywhere else raises.
* ``VC_AUDIT_EXAMPLES_DIR`` points at the bundled ``examples/`` directory, whose
  depth relative to this file differs from a normal checkout.

**A serverless deployment cannot keep an evidence archive.** ``/tmp`` does not
outlive the instance, so a run is retrievable by id only until the next cold
start. That is a real limitation of the host rather than of the tool: the
valuation, the memo and the full audit trail all still come back in the response
body, but ``GET /api/valuations/{run_id}`` will start returning 404. A
deployment that needs a durable archive wants a host with a persistent volume,
or an object store behind ``vc_audit.reporting.evidence``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The package lives under src/, which a serverless bundle does not put on the
# import path the way `pip install -e .` does locally. Prepending it here keeps
# the deployment working without needing the build step to install the project
# itself, and is a no-op anywhere the package is already importable.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vc_audit.api.main import app as _app  # noqa: E402  (import follows the path fix above)

#: The path this file is served at. `vercel.json` rewrites every incoming path
#: here, and Vercel hands the function the *rewritten* path rather than the one
#: the browser asked for. Without the shim below, a request for `/` arrives as
#: `/api/index`, matches no route, and the deployment answers every page with
#: `{"detail":"Not Found"}` while working perfectly in local development.
_FUNCTION_PATH = "/api/index"


class _RestoreRequestPath:
    """Undo the rewrite so routing sees the URL the browser actually requested.

    A minimal ASGI wrapper rather than FastAPI middleware, because the path has
    to be corrected before routing runs, not after. Hosts that already pass the
    original path through are unaffected: nothing matches the prefix, and the
    scope is forwarded untouched.

    Only an exact `/api/index` or a `/api/index/...` prefix is stripped, so the
    real API routes (`/api/examples`, `/api/valuations`) are never touched.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == _FUNCTION_PATH or path.startswith(_FUNCTION_PATH + "/"):
                restored = path[len(_FUNCTION_PATH) :] or "/"
                scope = {**scope, "path": restored, "raw_path": restored.encode("utf-8")}
        await self._app(scope, receive, send)


app = _RestoreRequestPath(_app)

__all__ = ["app"]
