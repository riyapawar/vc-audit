"""Writes the evidence pack: the artefact that survives the session.

A valuation that exists only as terminal output is not a workpaper. Each run
writes a directory keyed by its deterministic run id containing three files:

* ``inputs.json``  — exactly what went in, so the run can be replayed.
* ``report.json``  — the full structured result, every trail included.
* ``memo.md``      — the human-readable memorandum.

Re-running identical inputs overwrites the same directory rather than creating
a near-duplicate, which is what keeps an evidence archive reviewable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from vc_audit.domain.models import PortfolioCompany, ValuationReport
from vc_audit.reporting import memo

DEFAULT_OUTPUT_DIR = Path("out")


@dataclass(frozen=True)
class EvidencePack:
    """Paths written for one run."""

    directory: Path
    inputs: Path
    report: Path
    memo: Path

    def as_list(self) -> list[Path]:
        return [self.inputs, self.report, self.memo]


def write(
    report: ValuationReport,
    company: PortfolioCompany,
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    methods: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> EvidencePack:
    """Write the pack for ``report`` and return the paths written."""
    directory = Path(output_dir) / report.run_id
    directory.mkdir(parents=True, exist_ok=True)

    inputs_path = directory / "inputs.json"
    report_path = directory / "report.json"
    memo_path = directory / "memo.md"

    # Keys are written in their natural order, never sorted. Step inputs and
    # outputs are ordered dicts whose order is part of the record -- sorting
    # them would make a reloaded memo render differently from the original,
    # which is exactly the reproducibility this pack exists to provide.
    inputs_path.write_text(
        json.dumps(
            {
                "company": company.model_dump(mode="json"),
                "as_of": report.as_of.isoformat(),
                "methods": methods,
                "overrides": overrides or {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    memo_path.write_text(memo.render(report), encoding="utf-8")

    return EvidencePack(
        directory=directory, inputs=inputs_path, report=report_path, memo=memo_path
    )


def load_report(run_id: str, *, output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> ValuationReport:
    """Re-open an archived report so a past run can be inspected without re-running.

    Raises:
        FileNotFoundError: No pack exists for that run id.
    """
    path = Path(output_dir) / run_id / "report.json"
    if not path.exists():
        raise FileNotFoundError(f"no archived report at {path}")
    return ValuationReport.model_validate_json(path.read_text(encoding="utf-8"))


def list_runs(*, output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> list[tuple[str, str, date]]:
    """Enumerate archived runs as ``(run_id, company_name, as_of)``.

    Reads only the fields it needs from each report so a large archive stays
    cheap to list.
    """
    root = Path(output_dir)
    if not root.exists():
        return []

    runs: list[tuple[str, str, date]] = []
    for directory in sorted(root.iterdir()):
        report_path = directory / "report.json"
        if not report_path.is_file():
            continue
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            runs.append(
                (
                    payload["run_id"],
                    payload["company_name"],
                    date.fromisoformat(payload["as_of"]),
                )
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            # A corrupt or partially-written pack should not break the listing.
            continue
    return runs
