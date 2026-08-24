"""Input ingestion: JSON on disk to a validated :class:`PortfolioCompany`.

Pydantic already validates the shape. What this module adds is the error
*message*: an auditor who mistypes a field should be told which field, in which
file, and what was expected -- not handed a traceback. Bad input is the most
common failure in a tool like this, so it gets a first-class error path rather
than an exception bubbling out of a parser.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vc_audit.domain.errors import FatalError
from vc_audit.domain.models import PortfolioCompany


def _format_validation_error(exc: ValidationError, path: Path) -> str:
    """Turn a pydantic error into something an auditor can act on."""
    lines = [f"'{path.name}' is not a valid company record:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def _read_json(path: Path, *, what: str) -> Any:
    """Read one JSON file, reporting failures in an auditor's language."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FatalError(f"{what} not found: {path}") from exc
    except OSError as exc:
        raise FatalError(f"could not read {what} {path}: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FatalError(
            f"'{path.name}' is not valid JSON: line {exc.lineno}, {exc.msg}"
        ) from exc


def _resolve_projections(payload: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    """Expand a ``projections`` string into the forecast it points at.

    Five-year forecasts usually arrive as their own file from a different team,
    so ``"projections": "basis_ai_forecast.json"`` is supported alongside an
    inline array. The path is resolved relative to the company record, so a pair
    of files can be moved together.

    Resolution happens here rather than in the model because the domain layer
    performs no I/O, and because this must never apply to the HTTP API: taking a
    filesystem path from an HTTP client would let a caller read arbitrary files
    off the server.

    The referenced file may hold either a bare array or an object with a
    ``projections`` key, since both are natural ways to save a forecast.
    """
    projections = payload.get("projections")
    if not isinstance(projections, str):
        return payload

    referenced = Path(projections)
    if not referenced.is_absolute():
        referenced = base_dir / referenced

    loaded = _read_json(referenced, what="projections file")
    if isinstance(loaded, dict):
        loaded = loaded.get("projections", loaded)
    if not isinstance(loaded, list):
        raise FatalError(
            f"'{referenced.name}' should contain a list of projected years, or an "
            f"object with a 'projections' key holding one; found "
            f"{type(loaded).__name__}"
        )

    resolved = dict(payload)
    resolved["projections"] = loaded
    resolved.setdefault(
        "projections_source",
        {
            "provider": "internal",
            "dataset": referenced.name,
            "as_of": date.fromtimestamp(referenced.stat().st_mtime).isoformat(),
            "note": "Loaded from a referenced projections file.",
        },
    )
    return resolved


def load_company(path: str | Path) -> PortfolioCompany:
    """Read and validate a company record.

    A ``projections`` value may be an inline array or a path to a separate JSON
    file, resolved relative to this record.

    Raises:
        FatalError: The file is missing, is not JSON, or fails validation. The
            message names the offending field.
    """
    path = Path(path)
    payload = _read_json(path, what="company file")
    if not isinstance(payload, dict):
        raise FatalError(
            f"'{path.name}' should contain a company object; found {type(payload).__name__}"
        )

    payload = _resolve_projections(payload, base_dir=path.parent)
    return parse_company(payload, source_name=path)


def parse_company(
    payload: dict[str, Any], *, source_name: str | Path = "<request>"
) -> PortfolioCompany:
    """Validate an already-decoded company record.

    Shared by the file loader and the HTTP API so both reject bad input on
    identical rules and report it in identical language.
    """
    try:
        return PortfolioCompany.model_validate(payload)
    except ValidationError as exc:
        raise FatalError(_format_validation_error(exc, Path(str(source_name)))) from exc


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """Parse ``key=value`` assumption overrides from the command line.

    Values are coerced to float where possible so ``wacc=0.18`` arrives as a
    number, while ``market_index=^IXIC`` stays a string.
    """
    overrides: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise FatalError(
                f"malformed override '{pair}'; expected key=value, e.g. --set wacc=0.18"
            )
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            raise FatalError(f"malformed override '{pair}'; the key is empty")
        try:
            overrides[key] = float(value)
        except ValueError:
            overrides[key] = value
    return overrides
