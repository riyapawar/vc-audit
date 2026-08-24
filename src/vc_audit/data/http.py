"""Shared HTTP transport for the live data sources.

A dropped peer is not a cosmetic failure here. The conclusion is a *median* over
the surviving peer set, so losing one company to a momentary network fault moves
the multiple, and the run that produced it cannot be reproduced. Retrying is
therefore a correctness control rather than a convenience, and this module exists
so both the EDGAR client and the quote client get the same one.

Three properties matter:

* **Transient and permanent failures are told apart.** A 404 means the ticker is
  not a filer and will never be; retrying it wastes time and hammers a free
  public service. A 503 or a timeout means try again. Only the second is retried.
* **Exhausted retries are labelled as such.** The caller receives an error that
  says whether the data is genuinely absent or merely unreachable, because those
  have opposite implications for whether to trust the run.
* **Requests are rate limited across threads.** Peers are fetched in parallel,
  and EDGAR asks callers to stay under ten requests a second.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from vc_audit.domain.errors import DataUnavailableError

#: HTTP statuses worth trying again. 429 is rate limiting, 5xx is the server
#: having a bad moment. Everything else is a statement about the request itself.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransientDataError(DataUnavailableError):
    """The source was unreachable, as distinct from having no such data.

    Carried as its own type so the peer screen can say "excluded because EDGAR
    was unreachable after 3 attempts" rather than "excluded because no filings
    exist". A reviewer reading the memo needs to tell those apart: the first
    means the run is incomplete and should be repeated, the second is a finding
    about the company.
    """


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try before giving up on a transient failure."""

    attempts: int = 3
    backoff_base_s: float = 0.6
    #: Multiplier per attempt. 0.6s, then 1.2s, then 1.8s of waiting in total.
    backoff_factor: float = 2.0

    def delay_before(self, attempt: int) -> float:
        """Seconds to wait before ``attempt`` (1-indexed; the first has no delay)."""
        if attempt <= 1:
            return 0.0
        return self.backoff_base_s * (self.backoff_factor ** (attempt - 2))


class RateLimiter:
    """Serialises outbound requests so parallel fetches stay polite."""

    def __init__(self, min_interval_s: float) -> None:
        self._min_interval = min_interval_s
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


def fetch_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout_s: float,
    provider: str,
    dataset: str,
    limiter: RateLimiter | None = None,
    policy: RetryPolicy | None = None,
    sleep=time.sleep,
) -> Any:
    """GET ``url`` and decode JSON, retrying transient failures.

    Args:
        provider: Source name, for the error message.
        dataset: What was being fetched, for the error message.
        limiter: Shared rate limiter, applied before every attempt.
        policy: Retry behaviour. Defaults to three attempts.
        sleep: Injected so tests do not actually wait.

    Raises:
        TransientDataError: Every attempt failed for a retryable reason.
        DataUnavailableError: The failure was permanent, or the body was not JSON.
    """
    policy = policy or RetryPolicy()
    last_reason = "no attempt was made"

    for attempt in range(1, policy.attempts + 1):
        delay = policy.delay_before(attempt)
        if delay:
            sleep(delay)
        if limiter is not None:
            limiter.wait()

        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_STATUSES:
                # A statement about the request, not the server. Retrying it
                # would be pointless and impolite.
                raise DataUnavailableError(provider, dataset, f"HTTP {exc.code}") from exc
            last_reason = f"HTTP {exc.code}"

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_reason = f"network error: {exc}"

        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A malformed body is not worth a second request: the source is
            # answering, it is just not answering with JSON.
            raise DataUnavailableError(provider, dataset, f"malformed response: {exc}") from exc

    raise TransientDataError(
        provider,
        dataset,
        f"unreachable after {policy.attempts} attempts, last failure: {last_reason}",
    )
