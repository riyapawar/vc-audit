# VC Audit Tool

Fair value estimation for private venture portfolio companies, built so that every number
traces back to a formula, an input, and a source.

## Problem and approach

Auditors reviewing a VC portfolio must estimate fair value for companies that have no market
price and sparse, non-standardised financials. The output that matters is not the number —
it is the *defensible* number: one a reviewer can challenge line by line.

So the centrepiece here is an **audit ledger**, not a valuation model. No code in this package
may compute a value and simply return it. Every arithmetic operation is written to an
`AuditTrail` as a `Step` carrying its formula (with values substituted), its inputs, its output,
and its sources. Three methodologies are implemented as plugins around that ledger:

| Method | Logic | Weight |
|---|---|---:|
| **Comps** | Peer-median EV/Revenue, outliers trimmed by a Tukey fence, illiquidity-discounted | 0.35 |
| **DCF** | Unlevered FCF discounted at WACC + Gordon terminal value | 0.30 |
| **Last Round** | Most recent post-money, marked forward on a sector-matched public index | 0.35 |

When the data supports more than one, all run and the engine reports **whether they agree** —
convergence is corroboration; wide dispersion is a finding, raised as an exception rather than
averaged away.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate   # Unix: source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
vc-audit value examples/basis_ai.json --as-of 2026-08-22 --detail --out out
```

```bash
vc-audit methods examples/inflo.json                    # what can I run, and why not?
vc-audit peers saas                                     # inspect the comparability screen
vc-audit value examples/basis_ai.json -s wacc=0.18      # override an assumption
vc-audit value examples/basis_ai.json --simulate-outage public_comps   # graceful degradation
vc-audit runs && vc-audit explain <run-id>              # reopen an archived valuation
vc-audit serve                                          # web UI + API docs at :8000
pytest                                                  # 174 tests
```

## Key design decisions

| Decision | Rationale | Tradeoff accepted |
|---|---|---|
| `trail.record()` returns its own output | The ergonomic path is the audited path, so steps can't be forgotten | Slightly noisier calculation code |
| Deterministic `run_id` + SHA-256 fingerprint over every trail | Re-running identical inputs reproduces the memo byte for byte; a changed fingerprint means something moved | Memos carry no timestamp |
| `as_of` injected, never `date.today()` inside logic | Last quarter's inputs must reproduce last quarter's answer | Callers must always supply a date |
| Every assumption read through `ctx.assume()`, tagged `user_provided` / `engine_default` / `derived` | Yields the **review queue** (what the tool decided for you) and free sensitivity analysis — perturbing an assumption is just a different override map | One indirection on every constant |
| Methods declare `DriverSpec`s; sensitivity is method-agnostic | Adding a methodology needs no change to the sweep, engine, CLI, API or reports | Drivers must be numeric assumptions |
| Recoverable vs fatal errors are distinct types | A vendor outage skips one method and is reported; an inconsistent assumption set (WACC ≤ terminal growth) stops the run rather than publishing a blend drawn from a model known to be invalid | Fatal errors are strict, by intent |
| Money as `float`, not `Decimal` | Uncertainty in a discount rate swamps float error by orders of magnitude | Not suitable as-is for moving money |
| Market data behind a `MarketDataProvider` protocol | Swapping fixtures for a live vendor feed is a constructor argument | Fixtures must stay realistic |

Comps deliberately trims outliers **by rule, not by eye** (a peer at 28x revenue is excluded and
named in the trail), and takes its range from the peer interquartile spread — an empirical range
is evidence; a ±20% band is only a convention.

## With more time

- **Real data adapters** — the provider protocol is ready; a Yahoo Finance/CapIQ client is the next commit.
- **Assumption sign-off workflow** — the review queue currently reports; it should let a reviewer approve an assumption and carry that approval into the next quarter's run.
- **Quarter-over-quarter diffing** — deterministic fingerprints make "what changed since Q2, and was it inputs or code?" mechanical. High value, not yet built.
- **Correlated sensitivity** — the sweep is one-at-a-time; a two-way WACC × growth grid and a Monte Carlo option would bound the estimate better.
- **Precedent-transactions method** and per-company beta rather than a flat 1.0.
- **Persistence** — the evidence archive is a directory of JSON; real use wants a database and an immutable audit log.

## Documentation

[`docs/workflow.md`](docs/workflow.md) — architecture, data flow diagram, and a walkthrough of a
worked example. Generated memos land in `out/<run-id>/` as `memo.md`, `report.json`, `inputs.json`.

> Market data is served from checked-in fixtures standing in for a vendor feed. The comparable
> universe is **synthetic** — invented tickers and fabricated figures, not data about real issuers.
