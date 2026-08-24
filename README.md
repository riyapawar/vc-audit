# VC Audit Tool

Fair value estimation for private venture portfolio companies, built so that every number traces
back to a formula, an input, and a filing.

```bash
git clone https://github.com/riyapawar/vc-audit.git && cd vc-audit
python -m venv .venv && .venv/Scripts/activate   # Unix: source .venv/bin/activate
pip install -e ".[dev]"
pytest                                            # 282 tests, no network, no API keys
vc-audit value examples/basis_ai.json --as-of 2026-08-21 --detail
```

## The problem

An auditor valuing a private portfolio company has no market price and sparse, non-standardised
financials. The output that actually gets reviewed is not the number, it is the evidence behind it.
A reviewer needs to challenge the estimate line by line, six months later, without the person who
produced it in the room.

So this system is designed around producing that evidence as a first-class output rather than as a
by-product.

## The approach: an audit ledger, not a valuation model

The brief states that accurate financial modelling is not required and that a traceable, auditable
process is. That points at a specific architecture: the centrepiece is an **audit ledger**, and the
valuation methods are plugins around it.

The rule the whole codebase is built on: **no code may compute a value and simply return it.** Every
arithmetic operation is written into an `AuditTrail` as a step carrying its formula with the values
already substituted, its inputs, its output, and its sources.

```python
enterprise_value = trail.record(
    label="apply_multiple",
    description="Apply the discounted multiple to the subject's LTM revenue.",
    formula="6.58x * $10,000,000",
    inputs={"adjusted_multiple": 6.58, "ltm_revenue_usd": 10_000_000},
    output=adjusted_multiple * revenue,
    unit="usd",
)
```

`record()` returns the value it just recorded. That single detail is the load-bearing design choice:
the ergonomic way to write the calculation is also the audited way, so a step cannot be forgotten,
because recording it *is* how you obtain the number.

### Three methods, and whether they agree

| Method | Logic | Weight |
|---|---|---:|
| **Comps** | Peer-median EV/Revenue, outliers trimmed by a Tukey fence, illiquidity-discounted | 0.35 |
| **DCF** | Unlevered free cash flow discounted at WACC, plus a Gordon terminal value | 0.30 |
| **Last Round** | Most recent post-money valuation, marked forward on a sector-matched index | 0.35 |

Whichever methods the available data supports all run, and the engine reports **whether they agree**.
Convergence between independent methods is corroborating evidence. Wide dispersion is a finding that
needs explaining before anyone books a number, so it is raised as an exception rather than averaged
into silence.

## Design decisions

### 1. The audit ledger enforces itself ergonomically, not by policy

**Decision.** `AuditTrail.record()` appends a step and returns its output, so calculations read as
expressions.

**Why.** A convention like "remember to log each step" degrades the moment someone is in a hurry.
Making the audited path the path of least resistance means the trail cannot drift out of sync with
the arithmetic, because they are the same code.

**Tradeoff.** Calculation code is noisier than the equivalent bare arithmetic. Accepted: the noise
is the deliverable.

### 2. Every assumption is read through one accessor

**Decision.** Methods never read a constant directly. They call `ctx.assume(key, default, rationale=...)`,
which resolves the auditor's override against the engine default and records which one it used, tagged
`user_provided`, `engine_default`, or `derived`.

**Why.** Two features fall out of this for free, and neither needed separate machinery:

- **The assumption review queue.** Because origin is tracked, the report can list exactly the
  judgement calls the tool made on the reviewer's behalf. Those are the inputs nobody has signed off
  on yet, and surfacing them is the difference between a tool that hides its assumptions and one
  that hands them over.
- **Sensitivity analysis.** Perturbing an assumption is nothing more than re-running a method with a
  different override map. `sensitivity.py` therefore contains zero knowledge of any specific method,
  and a new methodology gets stress-testing without writing any.

**Tradeoff.** One layer of indirection on every constant in the system.

### 3. Peer fundamentals come from primary sources, with no API key

**Decision.** Rather than read a vendor's pre-computed `enterpriseValue`, the live provider pulls
shares outstanding, cash, debt and revenue as tagged XBRL facts from each peer's filed 10-K or 10-Q
on SEC EDGAR, and computes the bridge itself:

```
market cap = shares outstanding (SEC XBRL, dei) x close (public quote)
EV         = market cap + debt (SEC XBRL) - cash (SEC XBRL)
EV/Revenue = EV / LTM revenue (SEC XBRL, four trailing quarters)
```

**Why.** This is deliberately more work than reading one field. The payoff is that no step of the
multiple rests on an unauditable number. Asked "where did $39.5B come from", a vendor-sourced tool
can only answer "the vendor said so". Here a reviewer opens the linked 10-Q and finds the cash
balance the calculation used. EDGAR needs no API key, only a descriptive User-Agent and a polite
request rate, both of which the client honours.

**Tradeoff.** Slower (roughly 13 to 20 seconds for a full sector screen), and XBRL is not a clean
data source. Two realities shape the code, both found by reading actual filings:

- **Filers migrate between tags.** ServiceNow's last `LongTermDebtNoncurrent` fact is from 2021; it
  now files `LongTermDebt`. Taking the first tag that has data would have valued a 2026 company on a
  2021 balance sheet. So candidate tags are gathered together, anything older than fifteen months is
  discarded outright, and the freshest fact wins, with the tag preference order breaking ties.
  Salesforce's last `MarketableSecuritiesCurrent` fact is from 2014, which was polluting its cash
  balance by about $2.8B until this was fixed.
- **Periods overlap.** A 10-K carries a twelve-month figure spanning the same period as its
  quarters, so summing naively double-counts. LTM revenue is assembled from four non-overlapping
  quarters and falls back to the latest annual figure, recording which basis it used, because a
  reviewer comparing two peers needs to know they are not on the same basis.

Debt and cash decomposition genuinely varies by filer, so rather than pretend to a precision the
mapping does not have, the provider **records the tags it actually used** into the trail
(`us-gaap:LongTermDebt @2026-06-30 + us-gaap:ShortTermBorrowings @2026-06-30`). An imprecision a
reviewer can see and judge is worth more than one hidden behind a single number.

### 4. Market data sits behind a protocol, with a disclosed fallback

**Decision.** Valuation methods never touch a file or an HTTP client. They talk to a
`MarketDataProvider` protocol. Three implementations exist: live (SEC plus quotes), fixtures
(checked-in JSON, fully offline), and resilient (live first, falling back to fixtures on a typed
data failure). `--data auto|live|fixtures` selects.

**Why.** Swapping in a licensed vendor feed is a constructor argument rather than a refactor, and
methods stay testable without a network. The resilient mode matters because live sources are the
better evidence but also the thing most likely to be unavailable during a review.

Critically, the fallback is **never silent**. The substitution is recorded as an audit step and
raised as an exception in the memo. A silent fallback would be the worst of the three outcomes,
because fixture figures would then carry the authority of filed ones.

**Tradeoff.** Fixtures have to stay realistic enough to be useful, and they are clearly labelled as
synthetic wherever they appear.

### 5. Errors are split into recoverable and fatal

**Decision.** Two exception hierarchies. `RecoverableError` (a data outage, too few peers, a missing
input) skips one method and is reported with its reason. `FatalError` (an internally inconsistent
assumption set, such as WACC at or below terminal growth) stops the run.

**Why.** The engine needs to know what a failure *means*. One method failing should not take down
the others, and which methods could not run is itself audit evidence, so skips are recorded rather
than silently dropped. But continuing past an assumption set the engine knows to be invalid would
mean publishing a blended conclusion drawn partly from a model that cannot be right, which is worse
than returning nothing.

**Tradeoff.** Fatal errors are strict by intent. An auditor who supplies a bad WACC gets a refusal
rather than a partial answer, and has to fix it.

### 6. The valuation date is injected and bounds every source

**Decision.** `as_of` is a required argument to the engine. Nothing in the calculation layers calls
`date.today()`. No data source may return a fact, filing or price dated after it.

**Why.** Re-running last quarter's inputs has to reproduce last quarter's answer. A price or a
filing from after the mark was struck is information that did not exist at the time, and letting it
in would quietly invalidate every historical run.

**Tradeoff.** Callers always have to supply a date. The CLI defaults it to today for convenience,
but the engine itself never does.

### 7. Runs are deterministic and tamper-evident

**Decision.** `run_id` is a UUIDv5 over the canonical inputs. The report fingerprint is a SHA-256
over every trail in it, composed from the per-trail hashes so a mismatch localises to one method.
Memos deliberately carry no generation timestamp.

**Why.** Identical inputs and identical code reproduce the memo byte for byte, so a reviewer can
diff this quarter against last and see only the changes that are real. A timestamp would make two
identical valuations diff as different, which defeats the point. A changed fingerprint means
something moved, and the per-method composition says where.

Deterministic ids also mean a re-run overwrites its own evidence directory rather than accumulating
near-duplicate workpapers someone then has to reconcile.

**Tradeoff.** With live data and a past `--as-of`, runs stay reproducible because historical filings
and closes do not change. Only `--as-of` today drifts, and only because the market is still open.

### 8. Rejected candidates are returned, never dropped

**Decision.** A peer screen returns both the peers it kept and the candidates it rejected, each with
a specific reason and a stage: `data`, `comparability`, or `statistical`. Comps reports the full
funnel from candidate universe to valued peer set.

**Why.** "Which companies did you consider and reject" is the first challenge a comps conclusion
attracts. A peer set presented without its rejects looks curated even when it is not, and how much
judgement stood between the universe and the multiple is itself evidence.

**Tradeoff.** Every screen has to carry reasons, which is more plumbing than returning a list.

### 9. Comps trims outliers by rule, not by eye

**Decision.** Peers outside a Tukey fence (`Q1 - 1.5*IQR`, `Q3 + 1.5*IQR`) are excluded
mechanically, and every excluded peer is named in the trail with its multiple. The method uses the
**median**, not the mean. It declines to conclude below three surviving peers.

**Why.** Discretionary exclusion is the classic way a comps analysis becomes unfalsifiable: with
enough freedom to drop inconvenient peers, any answer is reachable. A stated rule plus a record of
what it dropped is the fix. The median is used because multiples are right-skewed and one
hyper-growth peer would drag a mean past anything a buyer would pay. Below three peers a "median" is
a small-sample artefact rather than a market observation, so the method returns an error explaining
that instead of a number.

**Tradeoff.** A mechanical rule occasionally drops a peer a human would have kept. That is the price
of reproducibility, and the drop is visible.

### 10. Ranges come from the data where the data has one

**Decision.** Comps takes its low and high from the peer **interquartile** multiples. DCF re-runs at
WACC plus and minus 200 basis points. Last Round brackets the unadjusted round value and the fully
marked value.

**Why.** An empirical range is evidence; a plus-or-minus twenty percent band is only a convention.
The comps range reports actual disagreement among comparables. The Last Round bracket spans the
genuine question at issue, which is whether to mark to market at all, rather than an arbitrary
tolerance.

**Tradeoff.** Ranges are not comparable in width between methods, because they measure different
kinds of uncertainty. The memo says which is which.

### 11. Reconciliation measures agreement instead of hiding it

**Decision.** Confidence weights (0.35 / 0.30 / 0.35) are declared on the methods and renormalised
over whichever ones actually ran. Dispersion is measured as the coefficient of variation across the
method conclusions and classified:

| CV | Classification | Meaning |
|---|---|---|
| At or below 10% | tight | Independent methods converging, which is real corroboration |
| At or below 25% | moderate | Normal for a private company; quote the range alongside |
| Above 25% | wide | **Exception raised.** Reconcile before booking |
| n/a | single-method | Uncorroborated; rests entirely on one method's assumptions |

**Why.** Running three methods and quoting the average is not analysis. The useful signal is whether
independent approaches land in the same place. A weighted average of conflicting evidence is not
itself evidence, and the report says so in as many words when the spread is wide.

Renormalising means a skipped method redistributes its weight rather than quietly dragging the
conclusion toward zero.

**Tradeoff.** The weights are a judgement call. They are declared on each method with a written
rationale, and shown in the output.

### 12. DCF guards against its own failure modes

**Decision.** WACC at or below terminal growth is fatal. Terminal-value concentration is computed,
and past 75% of enterprise value the run raises an exception.

**Why.** Below the growth rate the Gordon formula has no finite solution, so there is no defensible
number on the other side of it. And when the terminal value carries most of the conclusion, the
"discounted cash flow" is really a perpetuity assumption wearing a forecast. That is a finding a
reviewer should see, not a detail to bury.

### 13. Model-assisted peer selection is optional and off by default

**Decision.** A `PeerResearcher` protocol allows a language model to propose comparables and to vet
each one. It is off unless `--research` is passed and a key is configured.

**Why it exists at all.** A sector tag is a blunt comparability screen. "SaaS" contains both a
90%-gross-margin infrastructure business and a services-heavy implementation shop, and those should
not trade at the same multiple. The deterministic alternative is a hand-curated ticker list, which is
transparent and reproducible but is itself a judgement one person made once and froze. Deciding which
businesses genuinely resemble each other is a real weakness of tag-matching and a real strength of a
language model.

**Why it is off by default.** It costs determinism, needs a key, adds latency and cost, and
introduces model risk into an audit tool, which is the one domain where "the computer decided and I
cannot tell you why" is disqualifying.

**What makes it admissible.** Six constraints, each in the code:

- **The model judges; Python calculates.** It may propose tickers and argue comparability. It never
  produces a number. Every figure remains computed from filings.
- **Every call is an audit step**, carrying the model id, the effort setting, token usage, and the
  SHA-256 of the exact prompt sent. A reviewer can see that a machine made a judgement, which
  machine, and on what input.
- **Proposals are additive, never substitutive.** They join the standing universe rather than
  replacing it, so a model returning nothing costs nothing and a model returning garbage cannot
  displace a real candidate.
- **The reviewer sees SEC registrant names.** A hallucinated ticker resolves to whoever really owns
  it (`BRX` is Brixmor Property Group, a REIT, not Brex), so the mismatch is visible and gets
  rejected on sight.
- **Failure degrades, never propagates.** An unreachable or out-of-quota model records the failure
  and the valuation continues on the deterministic universe. A model outage must not be able to stop
  an audit.
- **It cannot starve the peer set.** A review that would leave fewer than three peers is recorded
  but not applied, because too little evidence to compute a median is a worse failure than a peer
  the model disliked.

**Tradeoff.** Peer selection is no longer guaranteed identical between runs, and the report warns
about exactly that when research is on.

### 14. Money is `float`, not `Decimal`

**Decision.** All monetary quantities are floats. Rounding happens once, at the reporting boundary.

**Why.** `Decimal` is correct when a system *moves* money, because binary rounding error is a real
defect there. This system *estimates* a value whose honest precision is roughly two significant
figures. The uncertainty in a discount rate assumption swamps float error by many orders of
magnitude, so `Decimal` would buy precision the underlying analysis does not have, at the cost of
constant `Decimal * float` friction across every statistical operation.

**Tradeoff.** This code is not suitable as-is for a system that settles payments.

### 15. Methods are plugins behind one contract

**Decision.** A method declares its required inputs, its confidence weight, its sensitivity drivers,
and how to compute. The registry is the single place that knows which methods exist.

**Why.** Adding a fourth methodology is one file and one registry line, with no changes to the
engine, the sensitivity sweep, the reporting layer, the CLI, or the API. The base class owns
everything every method must do identically, so a method implementation contains arithmetic and
nothing else.

### 16. The presentation layer computes nothing

**Decision.** Console, Markdown memo, JSON evidence pack and web UI all read the same
`ValuationReport`. None of them recompute or reformat a valuation figure.

**Why.** The screen and the audit record cannot disagree if only one of them does arithmetic. A test
asserts that the HTTP API and the engine produce identical fingerprints, so the two front ends cannot
drift apart.

## Setup and usage

Requires Python 3.11 or newer. No API keys and no network access are needed for anything below
except the live data mode.

```bash
python -m venv .venv && .venv/Scripts/activate   # Unix: source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
# Value a company and write the evidence pack
vc-audit value examples/basis_ai.json --as-of 2026-08-21 --detail --out out

# Web UI and OpenAPI docs on :8000
vc-audit serve

# Inspect the comparability screen on its own, with links to source filings
vc-audit peers saas

# What can I run for this company, and why not the rest?
vc-audit methods examples/inflo.json

# Override an assumption; scope to one method with dcf.wacc
vc-audit value examples/inflo.json -s wacc=0.18

# Fully offline and deterministic
vc-audit value examples/inflo.json --data fixtures

# Force a data source to fail, to demonstrate graceful degradation
vc-audit value examples/basis_ai.json --simulate-outage public_comps

# Reopen an archived valuation without recomputing it
vc-audit runs
vc-audit explain <run-id> --markdown

pytest        # 282 tests
ruff check src tests
```

The bundled examples exercise the different shapes the tool has to handle. `basis_ai.json` has
complete data, so all three methods run and diverge. `inflo.json` has no projections, so DCF is
skipped and the remaining two converge. `northwind_labs.json` has only a priced round, so the result
is single-method and flagged as uncorroborated. `basis_ai_linked.json` keeps its forecast in a
separate file.

**Projections may be inline or referenced.** A five-year forecast usually arrives as its own file
from a different team, so `projections` accepts either an array or a path:

```json
{ "name": "Basis AI", "sector": "saas", "projections": "basis_ai_projections.json" }
```

The path resolves relative to the company record, and the referenced file is cited as the
projections source in the memo. This applies to the CLI only, never the HTTP API: accepting a
filesystem path from an HTTP client would let a caller read arbitrary files off the server.

### Optional: model-assisted peer selection

This is the only part of the tool that needs a credential. Everything above runs without one.

```bash
pip install -e ".[research]"
cp .env.example .env        # then paste your key into ANTHROPIC_API_KEY=
vc-audit value examples/basis_ai.json --research --detail
```

`.env` is gitignored and read at startup by both the CLI and the HTTP service. A real environment
variable always takes precedence over the file, so an exported value or a CI secret can never be
silently overridden by a stale file on disk. If you would rather not use a file:

```bash
$env:ANTHROPIC_API_KEY = "sk-ant-..."             # PowerShell
export ANTHROPIC_API_KEY="sk-ant-..."             # bash
```

Get a key from [console.anthropic.com](https://console.anthropic.com) under **API Keys**. The API
is not free, so add a few dollars of credit under **Billing** first; peer research is two short
calls per valuation at low reasoning effort.

Without a key the tool runs normally on the deterministic sector screen, and the web UI disables the
research toggle with an explanation rather than failing at request time.

## What I would do with more time

- **Peer equity-bridge refinement.** Peer enterprise value uses reported debt and cash. A full
  treatment would handle operating leases and preferred stock, which the current tag mapping folds
  in inconsistently. The tags used are recorded, so the imprecision is visible rather than hidden,
  but recording it is not the same as fixing it.
- **Assumption sign-off workflow.** The review queue currently reports. It should let a reviewer
  approve an assumption and carry that approval into the next quarter's run.
- **Quarter-over-quarter diffing.** Deterministic fingerprints make "what changed since Q2, and was
  it the inputs or the code?" a mechanical question. High value, and not yet built.
- **Correlated sensitivity.** The sweep moves one driver at a time. A two-way WACC by growth grid,
  and a Monte Carlo option, would bound the estimate better than a tornado does.
- **A fourth method.** Precedent transactions would add a genuinely independent view, and per-company
  beta would improve the mark-to-market rather than assuming 1.0.
- **Persistence.** The evidence archive is a directory of JSON files. Real use wants a database with
  an append-only audit log and retention policy.

## Documentation

[`docs/workflow.md`](docs/workflow.md) contains the data flow diagram, the module map, the XBRL
extraction detail, and a worked example.

[`docs/deploy.md`](docs/deploy.md) covers deployment, which is optional: everything in this project
runs locally with no hosting at all. Two targets are configured and committed. Vercel
(`vercel.json`) has a free tier and needs no card, at the cost of an ephemeral filesystem, so the
evidence archive does not survive a cold start there. Railway (`railway.json`) keeps the archive
when given a mounted volume, but its free tier is a time-limited trial rather than an ongoing
allowance. Pick on whether a durable archive matters more than a free host.

Every run writes `memo.md`, `report.json` and `inputs.json` into `out/<run-id>/`.

> Live market data comes from SEC EDGAR, which requires no key, plus a public quote endpoint for
> closing prices. A production deployment would sit on a licensed feed, which is a one-class change
> behind the provider protocol. The offline fixtures are **synthetic**: invented tickers and
> fabricated figures, not data about any real issuer.
