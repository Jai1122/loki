"""Parse PIT ``mutations.xml`` into per-class mutation reports (DESIGN.md §7).

Mutation score is a SOFT signal: it is reported, never used to block a commit. A
mutation is "killed" when PIT detected it (``detected="true"``); otherwise it
survived (including ``NO_COVERAGE``). Surviving mutants are summarised so humans
can triage the weakest classes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from loki.state.model import MutationReport


def parse_mutations(xml_text: str) -> dict[str, MutationReport]:
    """Return ``{fqcn: MutationReport}`` aggregated from a PIT mutations report."""
    if not xml_text or not xml_text.strip():
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    reports: dict[str, MutationReport] = {}
    for mutation in root.iter("mutation"):
        cls_el = mutation.find("mutatedClass")
        if cls_el is None or not cls_el.text:
            continue
        fqcn = cls_el.text.strip()
        report = reports.setdefault(fqcn, MutationReport(class_fqcn=fqcn))
        detected = (mutation.get("detected") or "").strip().lower() == "true"
        if detected:
            report.killed += 1
        else:
            report.survived += 1
            report.surviving_details.append(_describe(mutation))
    return reports


def _describe(mutation: ET.Element) -> str:
    mutator = mutation.findtext("mutator", default="?").rsplit(".", 1)[-1]
    line = mutation.findtext("lineNumber", default="?")
    method = mutation.findtext("mutatedMethod", default="?")
    return f"{mutator} at {method}:{line}"
