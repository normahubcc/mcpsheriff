"""Reporters: human table, JSON and JUnit XML.

The JUnit output is what makes mcpsheriff usable as a CI gate without extra glue:
GitHub Actions, GitLab and Jenkins all read JUnit XML natively.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, List

from .checks import Result
from .protocol import _redact

_WIDTH = 22


def summary(results: Iterable[Result]) -> dict:
    results = list(results)
    failed = [r for r in results if not r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    return {
        "total": len(results),
        "passed": len(results) - len(failed) - len(skipped),
        "failed": len(failed),
        "skipped": len(skipped),
    }


def summary_line(results: Iterable[Result]) -> str:
    stats = summary(results)
    return (
        f"{stats['passed']} passed, {stats['failed']} failed, "
        f"{stats['skipped']} skipped ({stats['total']} checks)"
    )


def render_table(results: Iterable[Result]) -> str:
    results = list(results)
    lines: List[str] = []
    for result in results:
        marker = {"pass": "PASS", "FAIL": "FAIL", "skip": "SKIP"}[result.status]
        lines.append(f"{marker}  {result.name.ljust(_WIDTH)} {_redact(result.detail)}".rstrip())
    return "\n".join(lines)


def write_json(results: Iterable[Result], path: str) -> None:
    payload = {
        "summary": summary(results := list(results)),
        "checks": [
            {
                "name": r.name,
                "status": r.status,
                "severity": r.severity,
                "detail": _redact(r.detail),
            }
            for r in results
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_junit(results: Iterable[Result], path: str, suite_name: str = "mcpsheriff") -> None:
    # Redact before serialising: JUnit XML is uploaded to CI and kept as an artifact.
    results = [Result(r.name, r.ok, _redact(r.detail), r.severity, r.skipped) for r in results]
    stats = summary(results)
    suite = ET.Element(
        "testsuite",
        {
            "name": suite_name,
            "tests": str(stats["total"]),
            "failures": str(stats["failed"]),
            "errors": "0",
            "skipped": str(stats["skipped"]),
        },
    )
    for result in results:
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": "mcpsheriff", "name": result.name},
        )
        if result.skipped:
            ET.SubElement(case, "skipped", {"message": _redact(result.detail) or "not applicable"})
        elif not result.ok:
            failure = ET.SubElement(
                case,
                "failure",
                {"message": _redact(result.detail) or "check failed", "type": result.severity},
            )
            failure.text = f"{result.name} failed ({result.severity}): {result.detail}"
    tree = ET.ElementTree(suite)
    tree.write(path, encoding="utf-8", xml_declaration=True)
