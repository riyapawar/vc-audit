"""Tests for the live provider and the live/fixture fallback.

The live provider's job is to turn filings into peers *or explain why it
couldn't*. Most of these tests are about the second half: a candidate that
silently vanishes is a hole in the audit trail.
"""

from __future__ import annotations

from datetime import date

import pytest

from vc_audit.data.factory import build_provider
from vc_audit.data.live_provider import LiveMarketDataProvider
from vc_audit.data.mock_provider import MockMarketDataProvider
from vc_audit.data.quotes import Quote
from vc_audit.data.resilient import ResilientMarketDataProvider
from vc_audit.data.sec import Fundamentals
from vc_audit.domain.errors import DataUnavailableError
from vc_audit.domain.models import FilingReference

AS_OF = date(2026, 8, 21)

FILING = FilingReference(
    form="10-Q",
    filed_at=date(2026, 8, 1),
    period_end=date(2026, 6, 30),
    accession_number="0-1",
    url="https://sec.gov/x.htm",
)


def fundamentals(ticker: str, **kwargs) -> Fundamentals:
    defaults = dict(
        ticker=ticker,
        cik="0000000001",
        company_name=f"{ticker} Inc.",
        shares_outstanding=1_000_000.0,
        ltm_revenue_usd=100_000_000.0,
        revenue_basis="trailing four quarters",
        revenue_tag="Revenues",
        cash_usd=10_000_000.0,
        debt_usd=5_000_000.0,
        period_end=date(2026, 6, 30),
        filing=FILING,
        components={
            "cash": "us-gaap:Cash",
            "debt": "us-gaap:LongTermDebt",
            "revenue": "us-gaap:Revenues",
        },
    )
    defaults.update(kwargs)
    return Fundamentals(**defaults)


class StubSEC:
    """Serves canned fundamentals; raises for tickers it does not know."""

    def __init__(self, table: dict[str, Fundamentals | Exception]) -> None:
        self.table = table

    def fundamentals(self, ticker, *, as_of):
        value = self.table.get(ticker)
        if value is None:
            raise DataUnavailableError("sec_edgar", "companyfacts", f"no filer '{ticker}'")
        if isinstance(value, Exception):
            raise value
        return value


class StubQuotes:
    def __init__(self, prices: dict[str, float], history=None) -> None:
        self.prices = prices
        self.history = history or []

    def close_on(self, symbol, *, on):
        if symbol not in self.prices:
            raise DataUnavailableError("public_quotes", symbol, "no close")
        return Quote(symbol=symbol, close=self.prices[symbol], observed_on=on, requested_date=on)

    def monthly_history(self, symbol, *, start, end):
        if not self.history:
            raise DataUnavailableError("public_quotes", symbol, "empty series")
        return self.history


def live(sec_table, prices, history=None, universe=("AAA", "BBB", "CCC")) -> LiveMarketDataProvider:
    provider = LiveMarketDataProvider(sec=StubSEC(sec_table), quotes=StubQuotes(prices, history))
    provider._universe = universe  # type: ignore[attr-defined]
    return provider


@pytest.fixture(autouse=True)
def stub_universe(monkeypatch):
    """Pin the candidate universe so tests do not depend on the real ticker list."""
    monkeypatch.setattr(
        "vc_audit.data.live_provider.candidates_for",
        lambda sector: ("AAA", "BBB", "CCC") if sector == "saas" else (),
    )


class TestLivePeerAssembly:
    def test_builds_enterprise_value_from_its_components(self):
        provider = live({"AAA": fundamentals("AAA")}, {"AAA": 200.0})
        screen = provider.get_peers(sector="saas", as_of=AS_OF)
        peer = screen.peers[0]

        # 1,000,000 shares x $200 = $200M market cap, +$5M debt, -$10M cash.
        assert peer.market_cap_usd == 200_000_000
        assert peer.enterprise_value_usd == 195_000_000
        assert peer.ev_to_revenue == pytest.approx(1.95)

    def test_attaches_the_source_filing(self):
        provider = live({"AAA": fundamentals("AAA")}, {"AAA": 200.0})
        peer = provider.get_peers(sector="saas", as_of=AS_OF).peers[0]

        assert peer.latest_filing.url == "https://sec.gov/x.htm"
        assert "10-Q" in peer.latest_filing.cite()

    def test_records_how_each_figure_was_derived(self):
        provider = live({"AAA": fundamentals("AAA")}, {"AAA": 200.0})
        basis = provider.get_peers(sector="saas", as_of=AS_OF).peers[0].fundamentals_basis

        assert "trailing four quarters" in basis
        assert "1,000,000 shares at $200.00" in basis

    def test_uses_the_sec_registrant_name_not_the_ticker(self):
        """A hallucinated ticker resolves to whoever really owns it."""
        provider = live({"AAA": fundamentals("AAA", company_name="Brixmor Property Group")},
                        {"AAA": 200.0})
        peer = provider.get_peers(sector="saas", as_of=AS_OF).peers[0]
        assert peer.name == "Brixmor Property Group"

    def test_peers_are_ordered_deterministically(self):
        provider = live(
            {t: fundamentals(t) for t in ("CCC", "AAA", "BBB")},
            {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0},
        )
        assert provider.get_peers(sector="saas", as_of=AS_OF).tickers == ["AAA", "BBB", "CCC"]


class TestLiveExclusions:
    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"ltm_revenue_usd": None}, "no usable revenue"),
            ({"shares_outstanding": None}, "shares outstanding not reported"),
        ],
    )
    def test_unusable_fundamentals_are_excluded_with_a_reason(self, kwargs, fragment):
        provider = live(
            {"AAA": fundamentals("AAA"), "BBB": fundamentals("BBB", **kwargs)},
            {"AAA": 10.0, "BBB": 10.0},
        )
        screen = provider.get_peers(sector="saas", as_of=AS_OF)
        dropped = next(e for e in screen.excluded if e.ticker == "BBB")

        assert dropped.stage == "data"
        assert fragment in dropped.reason

    def test_a_missing_price_excludes_rather_than_crashes(self):
        provider = live({"AAA": fundamentals("AAA"), "BBB": fundamentals("BBB")}, {"AAA": 10.0})
        screen = provider.get_peers(sector="saas", as_of=AS_OF)

        assert screen.tickers == ["AAA"]
        assert "no closing price" in next(e for e in screen.excluded if e.ticker == "BBB").reason

    def test_net_cash_above_market_cap_is_excluded(self):
        provider = live(
            {"AAA": fundamentals("AAA", cash_usd=500_000_000.0)}, {"AAA": 1.0}
        )
        with pytest.raises(DataUnavailableError, match="no usable fundamentals"):
            provider.get_peers(sector="saas", as_of=AS_OF)

    def test_the_size_band_records_what_it_dropped(self):
        """BBB is priced so its multiple stays plausible: the point of this test is
        the size screen, and a corrupt figure would be caught earlier, at the data
        stage, before comparability is ever considered."""
        provider = live(
            {"AAA": fundamentals("AAA"), "BBB": fundamentals("BBB", ltm_revenue_usd=5e8)},
            {"AAA": 10.0, "BBB": 2000.0},
        )
        screen = provider.get_peers(
            sector="saas", as_of=AS_OF, subject_revenue_usd=100_000_000, size_band=2.0
        )
        dropped = next(e for e in screen.excluded if e.ticker == "BBB")

        assert dropped.stage == "comparability"
        assert "size band" in dropped.reason

    def test_the_funnel_counts_the_whole_universe(self):
        provider = live({"AAA": fundamentals("AAA")}, {"AAA": 10.0})
        screen = provider.get_peers(sector="saas", as_of=AS_OF)

        assert screen.universe_size == 3, "BBB and CCC were considered and dropped"
        assert len(screen.excluded) == 2

    def test_an_unmapped_sector_is_a_typed_failure(self):
        provider = live({"AAA": fundamentals("AAA")}, {"AAA": 10.0})
        with pytest.raises(DataUnavailableError, match="no candidate universe"):
            provider.get_peers(sector="widgets", as_of=AS_OF)


class TestLiveIndex:
    """Index levels read the daily series, so the reported observation date is a
    real trading day. Monthly bars are labelled by the period they open, which
    made the memo cite a date a month before the figure actually came from."""

    def test_an_exact_trading_day_needs_no_caveat(self):
        provider = live({}, {"^IXIC": 110.0})
        observation, source = provider.get_index_level(index_id="^IXIC", on=AS_OF)

        assert observation.level == 110.0
        assert observation.observed_on == AS_OF
        assert observation.is_exact_date is True
        assert source.note is None

    def test_a_non_trading_day_is_flagged_with_the_date_actually_used(self):
        prior = date(2026, 8, 20)

        class HolidayQuotes(StubQuotes):
            def close_on(self, symbol, *, on):
                return Quote(symbol=symbol, close=110.0, observed_on=prior, requested_date=on)

        provider = LiveMarketDataProvider(sec=StubSEC({}), quotes=HolidayQuotes({}))
        observation, source = provider.get_index_level(index_id="^IXIC", on=AS_OF)

        assert observation.observed_on == prior
        assert observation.is_exact_date is False
        assert "was not a trading day" in source.note
        assert source.as_of == prior, "the citation dates to the close actually used"

    def test_an_unreachable_index_propagates_rather_than_guessing(self):
        provider = live({}, {})  # no prices at all
        with pytest.raises(DataUnavailableError):
            provider.get_index_level(index_id="^IXIC", on=AS_OF)


class TestResilientFallback:
    def test_uses_the_primary_when_it_works(self):
        primary = live({"AAA": fundamentals("AAA")}, {"AAA": 10.0})
        provider = ResilientMarketDataProvider(primary, MockMarketDataProvider())
        screen = provider.get_peers(sector="saas", as_of=AS_OF)

        assert screen.tickers == ["AAA"]
        assert provider.degradations == []

    def test_falls_back_to_fixtures_and_records_the_substitution(self):
        primary = live({}, {})  # every candidate fails
        provider = ResilientMarketDataProvider(primary, MockMarketDataProvider())
        screen = provider.get_peers(sector="saas", as_of=AS_OF)

        assert "ATLS" in screen.tickers, "the fixture universe answered instead"
        assert len(provider.degradations) == 1
        assert "synthetic" in provider.degradations[0]

    def test_the_index_falls_back_independently(self):
        primary = live({"AAA": fundamentals("AAA")}, {"AAA": 10.0})  # no index history
        provider = ResilientMarketDataProvider(primary, MockMarketDataProvider())

        provider.get_peers(sector="saas", as_of=AS_OF)
        observation, _ = provider.get_index_level(index_id="^IXIC", on=AS_OF)

        assert observation.level > 0
        assert provider.degradations == [] or "index" in provider.degradations[0]

    def test_known_sectors_survive_a_dead_primary(self):
        provider = ResilientMarketDataProvider(live({}, {}), MockMarketDataProvider())
        assert "saas" in provider.known_sectors()

    def test_describe_names_both_layers(self):
        provider = ResilientMarketDataProvider(live({}, {}), MockMarketDataProvider())
        described = provider.describe()

        assert "SEC EDGAR" in described
        assert "Fallback" in described


class TestFactory:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("fixtures", MockMarketDataProvider),
            ("live", LiveMarketDataProvider),
            ("auto", ResilientMarketDataProvider),
        ],
    )
    def test_builds_the_requested_provider(self, mode, expected):
        assert isinstance(build_provider(mode), expected)

    def test_an_unknown_mode_is_rejected_by_name(self):
        with pytest.raises(ValueError, match="unknown data mode 'offline'"):
            build_provider("offline")


class TestCuratedUniverse:
    """The universe is hand-maintained, so it can go stale silently."""

    def test_no_ticker_is_duplicated_within_a_sector(self):
        from vc_audit.data.universe import SECTOR_UNIVERSE

        for sector, tickers in SECTOR_UNIVERSE.items():
            assert len(tickers) == len(set(tickers)), f"{sector} repeats a ticker"

    def test_every_sector_carries_headroom_over_the_peer_floor(self):
        """Candidates are lost to missing filings, so the universe needs slack
        above the three-peer minimum comps enforces."""
        from vc_audit.data.universe import SECTOR_UNIVERSE
        from vc_audit.methods.comps import MIN_PEERS

        for sector, tickers in SECTOR_UNIVERSE.items():
            assert len(tickers) >= MIN_PEERS * 2, (
                f"{sector} has too few candidates to survive data attrition"
            )


class TestBalanceSheetReconciliation:
    """XBRL tag mapping is the least certain step in the pipeline, and picking the
    wrong concept moves every multiple derived from it without changing anything a
    reader would notice. These are the controls that make such a fault visible."""

    def test_cash_above_total_assets_is_rejected(self):
        """Cash cannot exceed assets. If it does, the cash tag is not cash."""
        provider = live(
            {"AAA": fundamentals("AAA", cash_usd=9e11, total_assets_usd=1e9)},
            {"AAA": 10.0},
        )
        with pytest.raises(DataUnavailableError):
            provider.get_peers(sector="saas", as_of=AS_OF)

    def test_the_rejection_names_the_offending_tag_mapping(self):
        provider = live(
            {
                "AAA": fundamentals("AAA"),
                "BBB": fundamentals("BBB", debt_usd=9e11, total_liabilities_usd=1e9),
            },
            {"AAA": 10.0, "BBB": 10.0},
        )
        dropped = next(
            e for e in provider.get_peers(sector="saas", as_of=AS_OF).excluded
            if e.ticker == "BBB"
        )

        assert dropped.stage == "data"
        assert "does not reconcile" in dropped.reason
        assert "us-gaap:LongTermDebt" in dropped.reason, "must name the mapping at fault"

    def test_a_filer_that_reports_no_totals_is_not_penalised(self):
        """An absent control is not evidence of a fault."""
        provider = live(
            {"AAA": fundamentals("AAA", total_assets_usd=None, total_liabilities_usd=None)},
            {"AAA": 200.0},
        )
        assert provider.get_peers(sector="saas", as_of=AS_OF).tickers == ["AAA"]

    def test_a_reconciling_balance_sheet_passes(self):
        provider = live(
            {"AAA": fundamentals("AAA", total_assets_usd=1e9, total_liabilities_usd=5e8)},
            {"AAA": 200.0},
        )
        assert provider.get_peers(sector="saas", as_of=AS_OF).tickers == ["AAA"]


class TestMultiplePlausibility:
    """A corrupt figure and an aggressive valuation are different findings and are
    recorded at different funnel stages, so the cause is never mislabelled."""

    def test_an_absurdly_high_multiple_is_a_data_fault(self):
        # 1,000,000 shares at $200 with revenue of $1,000 implies 195,000x.
        provider = live(
            {"AAA": fundamentals("AAA"), "BBB": fundamentals("BBB", ltm_revenue_usd=1_000.0)},
            {"AAA": 10.0, "BBB": 200.0},
        )
        dropped = next(
            e for e in provider.get_peers(sector="saas", as_of=AS_OF).excluded
            if e.ticker == "BBB"
        )

        assert dropped.stage == "data", "a corrupt figure is not a statistical outlier"
        assert "plausible band" in dropped.reason

    def test_an_absurdly_low_multiple_is_also_caught(self):
        provider = live(
            {"AAA": fundamentals("AAA"), "BBB": fundamentals("BBB", ltm_revenue_usd=1e13)},
            {"AAA": 10.0, "BBB": 10.0},
        )
        dropped = next(
            e for e in provider.get_peers(sector="saas", as_of=AS_OF).excluded
            if e.ticker == "BBB"
        )
        assert "plausible band" in dropped.reason

    def test_a_genuinely_high_but_real_multiple_survives(self):
        """40x revenue is aggressive, not corrupt. The Tukey fence handles it later."""
        provider = live(
            {"AAA": fundamentals("AAA", ltm_revenue_usd=5_000_000.0)}, {"AAA": 200.0}
        )
        peers = provider.get_peers(sector="saas", as_of=AS_OF).peers

        assert [p.ticker for p in peers] == ["AAA"]
        assert peers[0].ev_to_revenue == pytest.approx(39.0)


class TestTransientFailures:
    """A peer lost to a momentary network fault changes the median, so it is
    reported as a fact about the run rather than about the company."""

    def test_an_unreachable_source_is_staged_separately_from_absent_data(self):
        from vc_audit.data.http import TransientDataError

        provider = live(
            {
                "AAA": fundamentals("AAA"),
                "BBB": TransientDataError("sec_edgar", "facts", "unreachable after 3 attempts"),
            },
            {"AAA": 10.0},
        )
        dropped = next(
            e for e in provider.get_peers(sector="saas", as_of=AS_OF).excluded
            if e.ticker == "BBB"
        )

        assert dropped.stage == "unreachable", "not a finding about the company"
        assert "Re-run before relying" in dropped.reason

    def _four_peer_screen(self, monkeypatch):
        """Four candidates, so losing one still clears the three-peer floor and the
        run reaches the funnel rather than declining outright."""
        from vc_audit.data.http import TransientDataError

        tickers = ("AAA", "BBB", "CCC", "DDD")
        monkeypatch.setattr(
            "vc_audit.data.live_provider.candidates_for",
            lambda sector: tickers if sector == "saas" else (),
        )
        provider = live(
            {t: fundamentals(t, ltm_revenue_usd=5_000_000.0) for t in tickers},
            {t: 20.0 + i for i, t in enumerate(tickers)},
        )
        provider._sec.table["DDD"] = TransientDataError("sec_edgar", "facts", "timed out")
        return provider

    def test_the_comps_method_warns_that_the_run_is_not_reproducible(
        self, company, monkeypatch
    ):
        from tests.conftest import make_context
        from vc_audit.methods.comps import ComparableCompanyAnalysis

        ctx = make_context(self._four_peer_screen(monkeypatch), "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert outcome.funnel.dropped_unreachable == 1
        assert any("not reproducible" in w for w in ctx.trail.warnings)

    def test_the_funnel_still_reconciles_with_unreachable_peers(self, company, monkeypatch):
        from tests.conftest import make_context
        from vc_audit.methods.comps import ComparableCompanyAnalysis

        provider = self._four_peer_screen(monkeypatch)
        f = ComparableCompanyAnalysis().compute(company, make_context(provider, "comps")).funnel

        assert f.proposed == f.retained + f.total_dropped
