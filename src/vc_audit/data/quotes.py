"""Public market quotes: prices and index levels, without an API key.

SEC filings supply everything about a company except what the market currently
pays for it, so this module covers the one remaining input: a closing price, and
index levels for the mark-to-market method.

It reads a public JSON chart endpoint. That is a pragmatic choice for a
take-home rather than a production one -- it is undocumented and unsupported,
and a real deployment would sit on a licensed feed. The cost of swapping is one
class, because everything above this talks to
:class:`~vc_audit.data.base.MarketDataProvider`, not to this module.

Every lookup is bounded by the valuation date. A price from after the valuation
date is information that did not exist when the valuation was struck, and
letting it in would quietly invalidate every historical run.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from vc_audit.data.http import RetryPolicy, fetch_json
from vc_audit.domain.errors import DataUnavailableError

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

#: An undocumented endpoint blocks the default urllib agent outright.
_USER_AGENT = "Mozilla/5.0 (compatible; vc-audit/0.1; +https://github.com/)"

#: Extra lookback when resolving a price, so a valuation date falling on a
#: holiday or a long weekend still finds a prior close.
_PRICE_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class Quote:
    """A closing price, and the date it actually closed on."""

    symbol: str
    close: float
    observed_on: date
    requested_date: date

    @property
    def is_exact_date(self) -> bool:
        return self.observed_on == self.requested_date


class QuoteClient:
    """Closing prices and index histories from a public chart endpoint."""

    name = "public_quotes"

    def __init__(self, *, timeout_s: float = 20.0) -> None:
        self._timeout = timeout_s
        self._cache: dict[tuple[str, str, date, date], list[tuple[date, float]]] = {}
        self._lock = threading.Lock()

    # ---- transport -------------------------------------------------------

    def _series(
        self, symbol: str, *, start: date, end: date, interval: str
    ) -> list[tuple[date, float]]:
        """Closing levels for ``symbol`` between two dates, ascending."""
        key = (symbol, interval, start, end)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        # The endpoint takes an inclusive-exclusive unix range, so the end is
        # pushed a day forward to include `end` itself.
        period1 = datetime(start.year, start.month, start.day, tzinfo=UTC)
        period2 = datetime(end.year, end.month, end.day, tzinfo=UTC)
        params = urllib.parse.urlencode(
            {
                "period1": int(period1.timestamp()),
                "period2": int(period2.timestamp()) + 86400,
                "interval": interval,
            }
        )
        url = f"{CHART_URL.format(symbol=urllib.parse.quote(symbol))}?{params}"
        payload = fetch_json(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout_s=self._timeout,
            provider=self.name,
            dataset=symbol,
            policy=RetryPolicy(),
        )

        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            error = ((payload.get("chart") or {}).get("error") or {}).get("description")
            raise DataUnavailableError(self.name, symbol, error or "no data returned")

        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        series = [
            (datetime.fromtimestamp(ts, tz=UTC).date(), float(close))
            for ts, close in zip(timestamps, closes, strict=False)
            if close is not None
        ]
        series.sort(key=lambda pair: pair[0])

        with self._lock:
            self._cache[key] = series
        return series

    # ---- public API ------------------------------------------------------

    def close_on(self, symbol: str, *, on: date) -> Quote:
        """The closing price on ``on``, or the nearest prior trading day.

        Raises:
            DataUnavailableError: No close exists on or before ``on``.
        """
        series = self._series(
            symbol,
            start=on - timedelta(days=_PRICE_LOOKBACK_DAYS),
            end=on,
            interval="1d",
        )
        candidates = [point for point in series if point[0] <= on]
        if not candidates:
            raise DataUnavailableError(
                self.name,
                symbol,
                f"no close on or before {on.isoformat()} "
                f"(searched back {_PRICE_LOOKBACK_DAYS} days)",
            )
        observed_on, close = candidates[-1]
        return Quote(symbol=symbol, close=close, observed_on=observed_on, requested_date=on)

    def monthly_history(self, symbol: str, *, start: date, end: date) -> list[tuple[date, float]]:
        """Month-end closes between two dates, for index mark-to-market.

        Raises:
            DataUnavailableError: The series is empty over the window.
        """
        series = self._series(symbol, start=start, end=end, interval="1mo")
        if not series:
            raise DataUnavailableError(
                self.name,
                symbol,
                f"no monthly observations between {start.isoformat()} and {end.isoformat()}",
            )
        return series
