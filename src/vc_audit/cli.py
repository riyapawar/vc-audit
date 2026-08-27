"""Command-line interface.

The CLI is the auditor-facing surface, so it is built around their workflow
rather than around the library's structure:

* ``value`` runs a valuation and writes the evidence pack.
* ``methods`` answers "what can I even run for this company?" *before* running.
* ``peers`` exposes the comparability screen on its own, because "which peers
  did you use" is the first challenge a comps conclusion attracts.
* ``runs`` / ``explain`` reopen archived valuations without recomputing them,
  which is what makes an evidence archive worth keeping.

Every command exits non-zero with a plain-language message on failure. An
auditor should never see a traceback.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vc_audit import engine
from vc_audit.data.factory import build_provider
from vc_audit.domain.errors import VcAuditError
from vc_audit.env import load_env_file
from vc_audit.loader import load_company, parse_overrides
from vc_audit.methods.registry import all_methods, eligibility_report
from vc_audit.reporting import console as console_report
from vc_audit.reporting import evidence, memo
from vc_audit.reporting.formatting import money, multiple
from vc_audit.research import build_researcher

# Pick up a local .env before any command reads a credential. A real
# environment variable still wins; see vc_audit.env.
load_env_file()

app = typer.Typer(
    name="vc-audit",
    help="Auditable fair-value estimation for private venture portfolio companies.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)


def _fail(message: str) -> None:
    """Report a failure the way an auditor should see it, then exit non-zero."""
    error_console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(code=1)


@app.command()
def value(
    company_file: Annotated[Path, typer.Argument(help="Path to the company record JSON.")],
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="Valuation date (YYYY-MM-DD). Defaults to today."),
    ] = None,
    method: Annotated[
        list[str] | None,
        typer.Option("--method", "-m", help="Restrict to these methods. Repeatable."),
    ] = None,
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", "-s", help="Override an assumption, e.g. --set wacc=0.18."),
    ] = None,
    detail: Annotated[
        bool, typer.Option("--detail/--no-detail", help="Print the full calculation trail.")
    ] = False,
    sensitivity: Annotated[
        bool, typer.Option("--sensitivity/--no-sensitivity", help="Stress each assumption.")
    ] = True,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Directory for the evidence pack. Omit to skip writing."),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of a table.")
    ] = False,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Emit the Markdown memo instead of a table.")
    ] = False,
    data: Annotated[
        str,
        typer.Option(
            "--data",
            help="Data source: auto (live, fixtures on failure), live, or fixtures.",
        ),
    ] = "auto",
    research: Annotated[
        bool,
        typer.Option(
            "--research/--no-research",
            help=(
                "Use a language model to propose and vet comparables. Needs "
                "ANTHROPIC_API_KEY. Off by default so runs stay reproducible."
            ),
        ),
    ] = False,
    simulate_outage: Annotated[
        list[str] | None,
        typer.Option(
            "--simulate-outage",
            help="Force a fixture dataset to fail, e.g. public_comps. Demonstrates degradation.",
        ),
    ] = None,
) -> None:
    """Value a portfolio company and show how the number was produced."""
    try:
        valuation_date = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        _fail(f"'{as_of}' is not a valid date; expected YYYY-MM-DD")

    try:
        company = load_company(company_file)
        overrides = parse_overrides(set_ or [])
        provider = build_provider(
            data, simulate_outage_for=set(simulate_outage or []) or None
        )
        researcher = build_researcher(research)
        report = engine.value_company(
            company,
            provider=provider,
            as_of=valuation_date,
            methods=method or None,
            overrides=overrides,
            run_sensitivity=sensitivity,
            researcher=researcher,
            today=date.today(),
        )
    except VcAuditError as exc:
        _fail(str(exc))
    except (ValueError, RuntimeError) as exc:
        _fail(str(exc))

    if json_out:
        console.print_json(json.dumps(report.model_dump(mode="json")))
    elif markdown:
        # print() rather than console.print(): Rich would interpret the memo's
        # square brackets as markup and mangle the document.
        print(memo.render(report))
    else:
        console_report.render(report, detail=detail, console=console)

    if out is not None:
        pack = evidence.write(
            report,
            company,
            output_dir=out,
            methods=list(method) if method else None,
            overrides=overrides,
        )
        if not json_out and not markdown:
            console.print(f"\n[green]Evidence pack written to[/] {pack.directory}")
            for path in pack.as_list():
                console.print(f"  · {path.name}")


@app.command()
def methods(
    company_file: Annotated[
        Path | None,
        typer.Argument(help="Optional company record; shows which methods it supports."),
    ] = None,
) -> None:
    """List the available valuation methods, and their eligibility for a company."""
    table = Table(title="Valuation methods", title_justify="left")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Required inputs")
    table.add_column("Weight", justify="right")
    if company_file is not None:
        table.add_column("Eligible")

    eligibility = {}
    if company_file is not None:
        try:
            eligibility = eligibility_report(load_company(company_file))
        except VcAuditError as exc:
            _fail(str(exc))

    for method_impl in all_methods():
        row = [
            method_impl.id,
            method_impl.name,
            ", ".join(sorted(method_impl.required_inputs)),
            f"{method_impl.default_weight:.2f}",
        ]
        if company_file is not None:
            missing = eligibility[method_impl.id]
            row.append(
                "[green]yes[/]"
                if not missing
                else f"[yellow]no — needs {', '.join(sorted(missing))}[/]"
            )
        table.add_row(*row)

    console.print(table)
    for method_impl in all_methods():
        console.print(f"\n[bold]{method_impl.id}[/] — {method_impl.summary}")
        console.print(f"  [dim]Weighting basis: {method_impl.weight_rationale}[/]")


@app.command()
def peers(
    sector: Annotated[str, typer.Argument(help="Sector to screen, e.g. saas.")],
    revenue: Annotated[
        float | None, typer.Option("--revenue", help="Subject LTM revenue, to apply a size band.")
    ] = None,
    size_band: Annotated[
        float, typer.Option("--size-band", help="Revenue band multiplier either side of subject.")
    ] = 500.0,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Screen date (YYYY-MM-DD). Defaults to today.")
    ] = None,
    data: Annotated[
        str, typer.Option("--data", help="Data source: auto, live, or fixtures.")
    ] = "auto",
) -> None:
    """Show the comparable-company screen on its own, before any valuation runs."""
    try:
        screen_date = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        _fail(f"'{as_of}' is not a valid date; expected YYYY-MM-DD")

    try:
        provider = build_provider(data)
        screen = provider.get_peers(
            sector=sector,
            as_of=screen_date,
            subject_revenue_usd=revenue,
            size_band=size_band,
        )
    except VcAuditError as exc:
        _fail(str(exc))
    except ValueError as exc:
        _fail(str(exc))

    if not screen.peers:
        available = ", ".join(provider.known_sectors())
        _fail(f"no peers found for sector '{sector}'; available sectors: {available}")

    table = Table(title=f"Public comparables — {sector}", title_justify="left")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Market cap", justify="right")
    table.add_column("Enterprise value", justify="right")
    table.add_column("LTM revenue", justify="right")
    table.add_column("EV/Revenue", justify="right")
    table.add_column("Filing")
    for peer in screen.peers:
        table.add_row(
            peer.ticker,
            peer.name,
            money(peer.market_cap_usd),
            money(peer.enterprise_value_usd),
            money(peer.ltm_revenue_usd),
            multiple(peer.ev_to_revenue),
            peer.latest_filing.cite() if peer.latest_filing else "—",
        )
    console.print(table)

    if screen.excluded:
        rejected = Table(
            title=f"Candidates rejected ({len(screen.excluded)})",
            title_justify="left",
        )
        rejected.add_column("Ticker")
        rejected.add_column("Stage")
        rejected.add_column("Reason")
        for exclusion in screen.excluded:
            rejected.add_row(exclusion.ticker, exclusion.stage, exclusion.reason)
        console.print(rejected)

    console.print(f"[dim]{screen.source.cite()}[/]")
    console.print(f"[dim]{provider.describe()}[/]")


@app.command()
def runs(
    out: Annotated[Path, typer.Option("--out", help="Evidence archive directory.")] = Path("out"),
) -> None:
    """List archived valuation runs."""
    archived = evidence.list_runs(output_dir=out)
    if not archived:
        console.print(f"[yellow]No archived runs under {out}/[/]")
        return

    table = Table(title=f"Archived runs — {out}/", title_justify="left")
    table.add_column("Run ID")
    table.add_column("Company")
    table.add_column("As of")
    for run_id, company_name, as_of in archived:
        table.add_row(run_id, company_name, as_of.isoformat())
    console.print(table)


@app.command()
def explain(
    run_id: Annotated[str, typer.Argument(help="Run ID of an archived valuation.")],
    out: Annotated[Path, typer.Option("--out", help="Evidence archive directory.")] = Path("out"),
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Print the archived memo instead of a table.")
    ] = False,
) -> None:
    """Reopen an archived valuation and walk through how it was produced."""
    try:
        report = evidence.load_report(run_id, output_dir=out)
    except FileNotFoundError as exc:
        _fail(str(exc))

    if markdown:
        print(memo.render(report))
    else:
        console_report.render(report, detail=True, console=console)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
    reload: Annotated[bool, typer.Option("--reload/--no-reload")] = False,
) -> None:
    """Run the HTTP service and web front end."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - uvicorn is a declared dependency
        _fail("uvicorn is not installed; run `pip install -e .`")

    console.print(f"[green]Serving[/] http://{host}:{port}  (API docs at /docs)")
    uvicorn.run("vc_audit.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":  # pragma: no cover
    app()
