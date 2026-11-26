import json
import xml.etree.ElementTree as ET

import pytest

from mcpsheriff.badge import (
    DEFAULT_LINK,
    badge_text,
    load_summary,
    main,
    markdown_snippet,
    render_svg,
)


def write_report(tmp_path, **summary):
    payload = {"summary": {"total": 9, "passed": 9, "failed": 0, "skipped": 0}, "checks": []}
    payload["summary"].update(summary)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_clean_run_renders_green_badge(tmp_path):
    summary = load_summary(str(write_report(tmp_path)))

    svg = render_svg(summary)

    assert badge_text(summary) == "9/9"
    assert ">9/9<" in svg
    assert "#2f855a" in svg
    assert 'aria-label="MCP conformance: 9/9"' in svg
    # The SVG must be well-formed: it is pasted into READMEs and rendered by GitHub.
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")


def test_failed_run_renders_red_badge(tmp_path):
    summary = load_summary(str(write_report(tmp_path, passed=8, failed=1)))

    svg = render_svg(summary)

    assert ">8/9<" in svg
    assert "#c53030" in svg
    assert "#2f855a" not in svg


def test_label_with_markup_is_escaped():
    svg = render_svg({"total": 1, "passed": 1, "failed": 0}, label='A&B "quoted"')

    assert "A&amp;B" in svg
    assert '&quot;quoted&quot;' in svg
    assert '<title>A&amp;B "quoted": 1/1</title>' in svg
    ET.fromstring(svg)


def test_markdown_snippet_links_to_project():
    snippet = markdown_snippet("badge.svg", summary={"total": 9, "passed": 9, "failed": 0})

    assert snippet == f"[![MCP conformance 9/9](badge.svg)]({DEFAULT_LINK})"


def test_main_writes_svg_and_prints_snippet(tmp_path, capsys):
    report = write_report(tmp_path)
    target = tmp_path / "badge.svg"

    assert main(["--report", str(report), "--out", str(target)]) == 0

    assert target.read_text(encoding="utf-8").startswith("<svg")
    printed = capsys.readouterr().out
    assert f"badge:    {target}" in printed
    assert DEFAULT_LINK in printed
    assert "9/9" in printed


def test_main_rejects_bad_input(tmp_path, capsys):
    assert main(["--report", str(tmp_path / "missing.json")]) == 2

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert main(["--report", str(broken)]) == 2

    no_summary = tmp_path / "no_summary.json"
    no_summary.write_text('{"checks": []}', encoding="utf-8")
    assert main(["--report", str(no_summary)]) == 2
    assert "summary" in capsys.readouterr().err


def test_summary_fields_must_be_integers(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"summary": {"total": 9, "passed": "9"}}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_summary(str(path))
