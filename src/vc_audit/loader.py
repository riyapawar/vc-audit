"""Input ingestion: JSON on disk to a validated :class:`PortfolioCompany`.

Pydantic already validates the shape. What this module adds is the error
*message*: an auditor who mistypes a field should be told which field, in which
file, and what was expected -- not handed a traceback. Bad input is the most
common failure in a tool like this, so it gets a first-class error path rather
than an exception bubbling out of a parser.
"""

from __future__ import annotations

import json
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


def load_company(path: str | Path) -> PortfolioCompany:
    """Read and validate a company record.

    Raises:
        FatalError: The file is missing, is not JSON, or fails validation. The
            message names the offending field.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FatalError(f"company file not found: {path}") from exc
    except OSError as exc:
        raise FatalError(f"could not read company file {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FatalError(f"'{path.name}' is not valid JSON: line {exc.lineno}, {exc.msg}") from exc

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
