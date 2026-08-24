"""Loading credentials from a local ``.env`` file.

Credentials belong in the environment, not in the repository. But "export it in
your shell first" is a step that reviewers forget and that does not survive
closing the terminal, so this reads a gitignored ``.env`` at startup as a
convenience.

Two rules keep it safe:

* **A real environment variable always wins.** The file only fills in keys that
  are not already set, so a deliberately exported value or a CI secret can never
  be silently overridden by a stale file on disk.
* **It is never required.** A missing or malformed file is not an error. The
  tool runs without any credentials at all; this only saves a step for the
  optional research layer.

Hand-rolled rather than pulling in ``python-dotenv``: the format understood here
is a dozen lines of parsing, and a dependency that exists to read
``KEY=value`` is not worth the supply chain.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = ".env"


def _unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines, ignoring blanks, comments and malformed rows.

    Supports a leading ``export`` for people who paste from shell history, and
    optional surrounding quotes. Anything it cannot parse is skipped rather than
    raised: a typo in a convenience file should not stop a valuation.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        values[key] = _unquote(value.strip())
    return values


def load_env_file(path: str | Path = DEFAULT_ENV_FILE) -> list[str]:
    """Load ``path`` into ``os.environ`` without overriding what is already set.

    Args:
        path: The file to read. Missing files are ignored.

    Returns:
        The names of the keys actually applied. Names only -- never values, so
        that logging the result cannot leak a secret.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, UnicodeDecodeError):
        return []

    applied = []
    for key, value in parse_env_text(text).items():
        if key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied
