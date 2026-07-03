"""Parse JaCoCo XML into per-class branch coverage (DESIGN.md §4.4).

Branch coverage is the primary metric; when a class has no branches, we fall
back to line coverage so a fully-covered branchless class scores 1.0 rather than
0.0. Class names are normalised from JVM form (``com/acme/Foo``) to dotted FQCNs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET


def _counter_fraction(class_el: ET.Element, counter_type: str) -> tuple[int, int] | None:
    for counter in class_el.findall("counter"):
        if counter.get("type") == counter_type:
            covered = int(counter.get("covered", "0"))
            missed = int(counter.get("missed", "0"))
            return covered, missed
    return None


def _class_coverage(class_el: ET.Element) -> float:
    branch = _counter_fraction(class_el, "BRANCH")
    if branch and (branch[0] + branch[1]) > 0:
        covered, missed = branch
        return covered / (covered + missed)
    line = _counter_fraction(class_el, "LINE")
    if line and (line[0] + line[1]) > 0:
        covered, missed = line
        return covered / (covered + missed)
    return 1.0  # no branches and no lines to miss -> trivially covered


def parse_branch_coverage(xml_text: str) -> dict[str, float]:
    """Return ``{fqcn: branch_coverage_fraction}`` for every class in the report."""
    if not xml_text or not xml_text.strip():
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    coverage: dict[str, float] = {}
    for class_el in root.iter("class"):
        raw_name = class_el.get("name")
        if not raw_name:
            continue
        fqcn = raw_name.replace("/", ".")
        coverage[fqcn] = round(_class_coverage(class_el), 6)
    return coverage
