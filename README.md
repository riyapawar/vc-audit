# VC Audit Tool

Fair value estimation for private venture portfolio companies, built so that every number
traces back to a formula, an input, and a filing.

## Problem and approach

Auditors valuing a private portfolio company have no market price and sparse financials. The
output that matters is not the number but the *defensible* number — one a reviewer can challenge
line by line.

So the centrepiece is an **audit ledger**, not a valuation model. No code here may compute a value
and simply return it: every operation is written to an `AuditTrail` as a step carrying its formula
(values substituted), inputs, output, and sources. Three methodologies are plugins around it:

| Method | Logic | Weight |
|---|---|---:|
| **Comps** | Peer-median EV/Revenue, outliers trimmed by a Tukey fence, illiquidity-discounted | 0.35 |
| **DCF** | Unlevered FCF discounted at WACC + Gordon terminal value | 0.30 |
| **Last Round** | Most recent post-money, marked forward on a sector-matched index | 0.35 |

When the data supports more than one, all run and the engine reports **whether they agree**.
Convergence is corroboration; wide dispersion is raised as an exception rather than averaged away.

**Peer fundamentals come from primary sources, with no API key.** Rather than read a vendor's
pre-computed `enterpriseValue`, the live provider pulls shares, cash, debt and revenue as tagged
XBRL facts from each peer's filed 10-K/10-Q and computes the bridge itself — so every component of
every multiple links to the document it came from. Fixtures take over automatically if the network
is unreachable, and the substitution is disclosed in the memo rather than hidden.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate   # Unix: source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
vc-audit value examples/basis_ai.json --as-of 2026-08-21 --detail --out out
```

```bash
vc-audit serve                                   # web UI + OpenAPI docs on :8000
vc-audit peers saas                              # the comparability screen, with filing links
vc-audit methods examples/inflo.json             # what can I run, and why not?
vc-audit value examples/inflo.json -s wacc=0.18  # override an assumption
vc-audit value examples/inflo.json --data fixtures            # fully offline
vc-audit value examples/inflo.json --simulate-outage public_comps   # graceful degradation
vc-audit runs && vc-audit explain <run-id>       # reopen an archived valuation
pytest                                           # 240 tests, no network, no keys
```

The **web UI** shows where each method lands on a shared axis, the funnel from candidate universe to
valued peer set, each peer's source filing, and every calculation step behind the number.

## Key design decisions

| Decision | Rationale | Tradeoff accepted |
|---|---|---|
| `trail.record()` returns its own output | The ergonomic path is the audited path, so steps can't be forgotten | Slightly noisier calculation code |
| Enterprise value built from XBRL components, not a vendor field | A reviewer can open the linked 10-Q and find the cash balance used | Slower, and tag mapping varies by filer — so the tags used are recorded |
| Deterministic `run_id` + SHA-256 fingerprint over every trail | Identical inputs reproduce the memo byte for byte; a changed fingerprint means something moved | Memos carry no timestamp |
| `as_of` injected, never `date.today()` in logic; no source may return data dated after it | Last quarter's inputs must reproduce last quarter's answer | Callers must always supply a date |
| Every assumption read via `ctx.assume()`, tagged `user_provided` / `engine_default` / `derived` | Yields the **review queue** (what the tool decided for you) and free sensitivity analysis | One indirection on every constant |
| Rejected candidates are returned, never dropped | "Which companies did you consider and reject" is the first challenge a comps conclusion attracts | Every screen must carry reasons |
| Recoverable vs fatal errors are distinct types | A source outage skips one method and is reported; an inconsistent assumption set (WACC ≤ terminal growth) stops the run | Fatal errors are strict, by intent |
| Model-assisted peer selection is **optional and off by default** | Determinism is worth more than comparability by default; when on, every call is recorded with its model id and prompt hash, and the model produces no figures | Needs `ANTHROPIC_API_KEY`; peer set may vary between runs |
| Money as `float`, not `Decimal` | Discount-rate uncertainty swamps float error by orders of magnitude | Not suitable as-is for moving money |

Comps trims outliers **by rule, not by eye** (a peer at 28x revenue is excluded and named), and
takes its range from the peer interquartile spread — an empirical range is evidence; a ±20% band is
only a convention.

## With more time

- **Equity-bridge inputs for peers** — peer EV uses reported debt and cash; a full treatment would
  handle operating leases and preferred stock, which the current tag mapping folds in inconsistently.
- **Assumption sign-off workflow** — the review queue reports; it should let a reviewer approve an
  assumption and carry that approval into the next quarter's run.
- **Quarter-over-quarter diffing** — deterministic fingerprints make "what changed since Q2, and was
  it inputs or code?" mechanical. High value, not yet built.
- **Correlated sensitivity** — the sweep is one-at-a-time; a two-way WACC × growth grid would bound
  the estimate better.
- **Precedent transactions** as a fourth method, and per-company beta rather than a flat 1.0.
- **Persistence** — the evidence archive is a directory of JSON; real use wants a database with an
  immutable audit log.

## Documentation

[`docs/workflow.md`](docs/workflow.md) — architecture, data flow diagram, and a worked example.
Runs write `memo.md`, `report.json` and `inputs.json` to `out/<run-id>/`.

> Live market data comes from SEC EDGAR (no key required) plus a public quote endpoint; a production
> deployment would sit on a licensed feed, which is a one-class change behind the provider protocol.
> The offline fixtures are **synthetic** — invented tickers and fabricated figures, not data about
> any real issuer.
