"""HTTP service.

A thin transport layer over :mod:`vc_audit.engine`. It parses, delegates, and
serialises -- no valuation logic lives here, which is what keeps the CLI and the
API guaranteed to produce identical answers for identical inputs.

Successful valuations persist an evidence pack by default, so a run can be
reopened at ``/api/valuations/{run_id}`` after the request that created it is
long gone. The run id is deterministic, so re-posting identical inputs returns
the same id and overwrites the same pack rather than accumulating duplicates.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from vc_audit import engine
from vc_audit.api.schemas import (
    ExcludedPeerInfo,
    MethodInfo,
    PeerInfo,
    PeerScreenResponse,
    RunSummary,
    ValuationRequest,
)
from vc_audit.data.factory import build_provider
from vc_audit.domain.errors import FatalError, VcAuditError
from vc_audit.domain.models import ValuationReport
from vc_audit.env import load_env_file
from vc_audit.loader import load_company
from vc_audit.methods.registry import all_methods
from vc_audit.reporting import evidence, memo
from vc_audit.research import build_researcher, research_available

# Loaded at import so `uvicorn vc_audit.api.main:app` sees it too, not just
# the `vc-audit serve` path.
load_env_file()

STATIC_DIR = Path(__file__).parent / "static"
OUTPUT_DIR = Path("out")

#: Sample company records shipped with the repository, offered by the web UI so
#: a reviewer can run something immediately. Resolved relative to the source
#: tree and treated as optional, since an installed wheel will not carry them.
EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"

app = FastAPI(
    title="VC Audit Tool",
    version="0.1.0",
    description=(
        "Auditable fair-value estimation for private venture portfolio companies. "
        "Every response carries the full calculation trail behind its number."
    ),
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the auditor-facing front end."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/api/capabilities", tags=["ops"])
def capabilities() -> dict[str, object]:
    """What this deployment can do, so the UI can offer only what will work."""
    return {
        "research_available": research_available(),
        "data_modes": ["auto", "live", "fixtures"],
        "default_data_mode": "auto",
    }


@app.get("/api/methods", response_model=list[MethodInfo], tags=["methods"])
def list_methods() -> list[MethodInfo]:
    """Describe every registered valuation method."""
    return [
        MethodInfo(
            id=method.id,
            name=method.name,
            summary=method.summary,
            required_inputs=sorted(method.required_inputs),
            default_weight=method.default_weight,
            weight_rationale=method.weight_rationale,
            drivers=[spec.label for spec in method.drivers()],
        )
        for method in all_methods()
    ]


@app.get("/api/examples", tags=["data"])
def list_examples() -> list[dict]:
    """Sample company records bundled with the repository.

    Returns an empty list rather than failing when the source tree is not
    present, so an installed package still serves the UI.
    """
    if not EXAMPLES_DIR.is_dir():
        return []
    examples = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        try:
            # Load through the same path the CLI uses, so a record referencing a
            # separate projections file arrives expanded. Anything that is not a
            # valid company record -- a projections fragment, say -- is skipped
            # rather than offered to the UI as something it could value.
            company = load_company(path)
        except FatalError:
            continue
        examples.append({"file": path.name, "company": company.model_dump(mode="json")})
    return examples


@app.get("/api/peers", response_model=PeerScreenResponse, tags=["data"])
def screen_peers(
    sector: str = Query(description="Sector to screen, e.g. 'saas'."),
    revenue: float | None = Query(default=None, description="Subject LTM revenue for size band."),
    size_band: float = Query(default=500.0, description="Revenue band either side of subject."),
    as_of: date | None = Query(default=None, description="Screen date; defaults to today."),
    data: str = Query(default="auto", description="Data source: auto, live or fixtures."),
) -> PeerScreenResponse:
    """Run the comparability screen on its own, without valuing anything."""
    try:
        provider = build_provider(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        screen = provider.get_peers(
            sector=sector,
            as_of=as_of or date.today(),
            subject_revenue_usd=revenue,
            size_band=size_band,
        )
    except VcAuditError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    peers, source = screen.peers, screen.source
    if not peers:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no peers found for sector '{sector}'; "
                f"available: {', '.join(provider.known_sectors())}"
            ),
        )
    return PeerScreenResponse(
        sector=sector,
        peers=[
            PeerInfo(
                ticker=peer.ticker,
                name=peer.name,
                sector=peer.sector,
                market_cap_usd=peer.market_cap_usd,
                enterprise_value_usd=peer.enterprise_value_usd,
                ltm_revenue_usd=peer.ltm_revenue_usd,
                ev_to_revenue=peer.ev_to_revenue,
                inclusion_rationale=peer.inclusion_rationale,
                filing_url=peer.latest_filing.url if peer.latest_filing else None,
                filing_label=peer.latest_filing.cite() if peer.latest_filing else None,
            )
            for peer in peers
        ],
        excluded=[
            ExcludedPeerInfo(ticker=e.ticker, stage=e.stage, reason=e.reason)
            for e in screen.excluded
        ],
        universe_size=screen.universe_size,
        citation=source.cite(),
        provider=provider.describe(),
    )


@app.post("/api/valuations", response_model=ValuationReport, tags=["valuations"])
def create_valuation(request: ValuationRequest) -> ValuationReport:
    """Value a company and return the conclusion with its full audit trail."""
    try:
        provider = build_provider(request.data_mode)
        researcher = build_researcher(request.research)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Research was asked for but cannot run -- a 503, since the client's
        # request is well-formed and the server is the thing that is missing.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        report = engine.value_company(
            request.company,
            provider=provider,
            as_of=request.as_of or date.today(),
            methods=request.methods,
            overrides=request.overrides,
            run_sensitivity=request.run_sensitivity,
            researcher=researcher,
        )
    except FatalError as exc:
        # The inputs are structurally valid but cannot support a valuation --
        # a 422 rather than a 500, because the client can fix this.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.persist:
        evidence.write(
            report,
            request.company,
            output_dir=OUTPUT_DIR,
            methods=request.methods,
            overrides=request.overrides,
        )
    return report


@app.get("/api/valuations", response_model=list[RunSummary], tags=["valuations"])
def list_valuations() -> list[RunSummary]:
    """List archived runs."""
    return [
        RunSummary(run_id=run_id, company_name=name, as_of=as_of)
        for run_id, name, as_of in evidence.list_runs(output_dir=OUTPUT_DIR)
    ]


@app.get("/api/valuations/{run_id}", response_model=ValuationReport, tags=["valuations"])
def get_valuation(run_id: str) -> ValuationReport:
    """Reopen an archived valuation without recomputing it."""
    try:
        return evidence.load_report(run_id, output_dir=OUTPUT_DIR)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no archived run '{run_id}'") from exc


@app.get(
    "/api/valuations/{run_id}/memo",
    response_class=PlainTextResponse,
    tags=["valuations"],
)
def get_memo(run_id: str) -> str:
    """Return the Markdown memorandum for an archived run."""
    try:
        report = evidence.load_report(run_id, output_dir=OUTPUT_DIR)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no archived run '{run_id}'") from exc
    return memo.render(report)
